"""Webhooks dos robôs publicados (:class:`PgWebhooks`).

Um webhook é um endereço ``POST /v1/webhooks/{id}`` que outro sistema chama,
sem token, para rodar um robô publicado. O corpo (um objeto JSON) são as
entradas do robô, conferidas pelo esquema da versão publicada: campo a mais ou
a menos é recusado (422), sem repetir os valores.

**A chave.** Cada webhook tem uma chave de assinatura que só aparece na criação e
na troca (``rotate_secret``), uma vez. O banco não a guarda: guarda um sal
aleatório de 32 bytes, e a chave é derivada a cada uso como
``HMAC-SHA256(RAPA_CHAVE_WEBHOOKS, empresa ‖ webhook ‖ versão ‖ sal)``. Trocar a
chave é trocar o sal (a versão sobe). Trocar ``RAPA_CHAVE_WEBHOOKS`` invalida as
chaves de todos os webhooks, que precisam ser trocadas.

**A assinatura.** Quem chama manda três cabeçalhos:

- ``X-RaPA-Timestamp``: o instante, em segundos desde 1970 (UTC);
- ``X-RaPA-Delivery``: o identificador da entrega (1 a 200 caracteres ASCII
  visíveis, sem ponto), único por webhook;
- ``X-RaPA-Signature``: ``sha256=`` e o HMAC-SHA256 em hexadecimal, com a chave
  (o texto inteiro, em UTF-8), de ``<timestamp>.<delivery>.<corpo>`` (veja
  :func:`sign_delivery`).

O identificador da entrega entra na assinatura: sem ele, quem copiasse uma
entrega válida poderia repeti-la com outro identificador. O horário só tem
dígitos e o identificador não tem ponto, então a mensagem assinada só se separa
de um jeito (um ponto no identificador deixaria mover a fronteira dele com o
corpo sem mudar a assinatura). A conferência usa
comparação em tempo constante. Webhook desconhecido ou removido, assinatura que
não confere e horário a mais de 5 minutos do relógio do servidor têm a mesma
resposta (401), que não diz qual foi o caso.

**Repetição.** A entrega é única por webhook e identificador
(``webhook_deliveries``): a mesma entrega de novo, com o mesmo corpo, devolve a
execução de antes (202, ``duplicate``), e com outro corpo é conflito (409). A
conferência e a inclusão rodam sob a trava de inícios da empresa, então duas
entregas iguais ao mesmo tempo nunca criam duas execuções.

**Limite.** Cada webhook aceita até ``rate_per_minute`` entregas por minuto
(contadas depois da assinatura, por processo; com várias réplicas, o limite é a
soma). Acima disso, 429 com ``Retry-After``. Antes de ler o webhook no banco, a
entrega passa por :class:`WebhookGate`: um identificador que não existia há
pouco é recusado (401) sem ir ao banco, um endereço de origem com recusas demais
e o processo com leituras demais por segundo recebem 429.

A execução nasce com origem ``webhook`` (prioridade de agenda e webhook) em nome
de ``webhook:<id>``. Nada é repetido sozinho: uma entrega recusada não cria
nada, e uma execução que falha fica como falhou.
"""

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import robots, runs, webhook_deliveries, webhooks
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import PgService, clean_title, now_utc
from rapa.control.services.inputs import validate_inputs
from rapa.control.services.robots import published_robot
from rapa.control.services.runs import enqueue_run, lock_company_starts
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RunState,
    RunTrigger,
    SignatureInvalid,
    WebhookChanges,
    WebhookDelivery,
    WebhookInfo,
    WebhookSecret,
    WebhookSpec,
    can_automate,
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = [
    "DEFAULT_FAILURES_PER_MINUTE",
    "DEFAULT_LOOKUPS_PER_S",
    "DEFAULT_UNKNOWN_TTL_S",
    "DELIVERY_HEADER",
    "MAX_CLOCK_SKEW_S",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "WEBHOOK_SCOPE",
    "PgWebhooks",
    "WebhookGate",
    "WebhookKeys",
    "WebhookLimiter",
    "sign_delivery",
    "webhook_path",
]

TIMESTAMP_HEADER = "X-RaPA-Timestamp"
DELIVERY_HEADER = "X-RaPA-Delivery"
SIGNATURE_HEADER = "X-RaPA-Signature"
#: Diferença máxima entre o horário da assinatura e o relógio do servidor.
MAX_CLOCK_SKEW_S = 300
#: Escopo das chaves de idempotência das entregas.
WEBHOOK_SCOPE = "webhook"
#: Tamanho mínimo da chave do servidor (``RAPA_CHAVE_WEBHOOKS``).
MIN_SERVER_KEY = 32

#: Leituras de webhook por segundo, neste processo, de todas as entregas juntas.
DEFAULT_LOOKUPS_PER_S = 200.0
#: Entregas recusadas na assinatura, por minuto, de um mesmo endereço de origem.
DEFAULT_FAILURES_PER_MINUTE = 60
#: Por quanto tempo um webhook que não existe é respondido sem ir ao banco.
DEFAULT_UNKNOWN_TTL_S = 60.0

#: ASCII visível sem o ponto: o ponto separa os campos da mensagem assinada.
_DELIVERY_ID = re.compile(r"^[\x21-\x2d\x2f-\x7e]{1,200}$")
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")
_TIMESTAMP = re.compile(r"^[0-9]{1,12}$")
_SECRET_PREFIX = "whsec_"  # noqa: S105 - o prefixo da chave, não uma chave
_DERIVATION_LABEL = b"rapa/webhook/v1\x00"
_REJECTED = "assinatura inválida, horário fora da tolerância ou webhook desconhecido"
_TOO_MANY = "entregas demais agora; tente de novo em {wait} s"


def webhook_path(webhook_id: UUID) -> str:
    """Caminho da entrega do webhook (relativo à URL pública da API)."""
    return f"/v1/webhooks/{webhook_id}"


def sign_delivery(secret: str, timestamp: str, delivery_id: str, body: bytes) -> str:
    """O valor de ``X-RaPA-Signature`` para uma entrega (o que o outro sistema calcula).

    O horário só tem dígitos e o identificador não tem ponto: a mensagem
    ``<timestamp>.<delivery>.<corpo>`` se separa de um jeito só. Um horário ou um
    identificador fora do formato levanta :class:`ValueError`.
    """
    if not _TIMESTAMP.fullmatch(timestamp) or not _DELIVERY_ID.fullmatch(delivery_id):
        raise ValueError(
            "horário (só dígitos) ou identificador da entrega (1 a 200 caracteres ASCII "
            "visíveis, sem ponto) fora do formato"
        )
    message = timestamp.encode() + b"." + delivery_id.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class WebhookKeys:
    """Deriva a chave de cada webhook da chave do servidor (``RAPA_CHAVE_WEBHOOKS``)."""

    def __init__(self, server_key: str) -> None:
        if not isinstance(server_key, str) or len(server_key) < MIN_SERVER_KEY:
            raise ValueError(
                f"a chave dos webhooks precisa ter pelo menos {MIN_SERVER_KEY} caracteres"
            )
        self._key = server_key.encode()

    def __repr__(self) -> str:
        return "WebhookKeys(***)"

    def derive(self, company_id: UUID, webhook_id: UUID, salt: bytes, version: int) -> str:
        message = (
            _DERIVATION_LABEL
            + company_id.bytes
            + webhook_id.bytes
            + version.to_bytes(4, "big")
            + salt
        )
        digest = hmac.new(self._key, message, hashlib.sha256).digest()
        return _SECRET_PREFIX + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


def _refill(bucket: _Bucket, now: float, rate: float, burst: float) -> None:
    elapsed = max(0.0, now - bucket.updated)
    bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
    bucket.updated = now


def _wait_s(bucket: _Bucket, rate: float) -> int:
    return max(1, math.ceil((1.0 - bucket.tokens) / rate))


class WebhookGate:
    """O que a entrega confere antes de ler o webhook no banco (em memória, por processo).

    A entrega chega sem token, então qualquer um pode chamá-la com um identificador
    qualquer; cada leitura do webhook custa uma transação do pool que as outras
    rotas de todas as empresas usam. Por isso, antes de ler:

    - **Desconhecidos.** Um identificador que o banco disse não existir (ou estar
      removido) fica ``unknown_ttl_s`` segundos numa lista curta: nesse tempo, a
      resposta é 401 direto, sem ir ao banco. A lista guarda até ``max_keys``.
    - **Por cliente.** Cada endereço de origem tem um balde de fichas gasto pelas
      entregas recusadas na assinatura (webhook desconhecido, assinatura errada,
      horário fora da tolerância): ``failures_per_minute`` por minuto, com a
      mesma folga. Sem fichas, 429 sem ir ao banco. As entregas certas não gastam
      nada: um sistema que entrega muito não é limitado aqui.
    - **No processo.** Um balde de ``lookups_per_s`` leituras por segundo (folga de
      ``2 * lookups_per_s``) para todas as entregas juntas; sem fichas, 429.

    O endereço de origem é o que o servidor HTTP vê: atrás de um proxy, configure-o
    para repassar o do cliente (``FORWARDED_ALLOW_IPS`` do uvicorn), senão todos os
    clientes dividem o mesmo balde.
    """

    def __init__(
        self,
        *,
        lookups_per_s: float = DEFAULT_LOOKUPS_PER_S,
        failures_per_minute: int = DEFAULT_FAILURES_PER_MINUTE,
        unknown_ttl_s: float = DEFAULT_UNKNOWN_TTL_S,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if lookups_per_s <= 0 or failures_per_minute < 1 or unknown_ttl_s < 0 or max_keys < 1:
            raise ValueError("os limites da entrega dos webhooks precisam ser positivos")
        self.lookups_per_s = lookups_per_s
        self.failures_per_minute = failures_per_minute
        self._lookup_rate = lookups_per_s
        self._lookup_burst = max(1.0, 2.0 * lookups_per_s)
        self._failure_rate = failures_per_minute / 60.0
        self._failure_burst = float(failures_per_minute)
        self._unknown_ttl = unknown_ttl_s
        self._max_keys = max_keys
        self._clock = clock
        self._lookups = _Bucket(tokens=self._lookup_burst, updated=clock())
        self._failures: OrderedDict[str, _Bucket] = OrderedDict()
        self._unknown: OrderedDict[UUID, float] = OrderedDict()
        self._lock = threading.Lock()

    def known_unknown(self, webhook_id: UUID) -> bool:
        """Se o banco disse há pouco que este webhook não existe."""
        now = self._clock()
        with self._lock:
            expires = self._unknown.get(webhook_id)
            if expires is None:
                return False
            if now >= expires:
                del self._unknown[webhook_id]
                return False
            return True

    def remember_unknown(self, webhook_id: UUID) -> None:
        """Guarda o identificador que não existe (ou foi removido) por ``unknown_ttl_s``."""
        if self._unknown_ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            self._unknown[webhook_id] = now + self._unknown_ttl
            self._unknown.move_to_end(webhook_id)
            while len(self._unknown) > self._max_keys:
                self._unknown.popitem(last=False)

    def admit(self, client: str | None) -> None:
        """Libera uma leitura do webhook no banco ou levanta :class:`QuotaExceeded`."""
        key = client or "?"
        now = self._clock()
        with self._lock:
            failures = self._failures.get(key)
            if failures is not None:
                _refill(failures, now, self._failure_rate, self._failure_burst)
                self._failures.move_to_end(key)
                if failures.tokens < 1.0:
                    wait = _wait_s(failures, self._failure_rate)
                    raise QuotaExceeded(_TOO_MANY.format(wait=wait), retry_after_s=wait)
            _refill(self._lookups, now, self._lookup_rate, self._lookup_burst)
            if self._lookups.tokens < 1.0:
                wait = _wait_s(self._lookups, self._lookup_rate)
                raise QuotaExceeded(_TOO_MANY.format(wait=wait), retry_after_s=wait)
            self._lookups.tokens -= 1.0

    def failed(self, client: str | None) -> None:
        """Gasta uma ficha do cliente por uma entrega recusada na assinatura."""
        key = client or "?"
        now = self._clock()
        with self._lock:
            bucket = self._failures.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._failure_burst, updated=now)
                self._failures[key] = bucket
                while len(self._failures) > self._max_keys:
                    self._failures.popitem(last=False)
            else:
                _refill(bucket, now, self._failure_rate, self._failure_burst)
                self._failures.move_to_end(key)
            bucket.tokens = max(0.0, bucket.tokens - 1.0)


class WebhookLimiter:
    """Balde de fichas por webhook, com o limite por minuto de cada um (em memória)."""

    def __init__(self, *, max_keys: int = 10_000, clock: Callable[[], float] = time.monotonic):
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[UUID, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, webhook_id: UUID, per_minute: int) -> None:
        """Consome uma ficha ou levanta :class:`QuotaExceeded` com a espera."""
        rate = per_minute / 60.0
        burst = float(per_minute)
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(webhook_id)
            if bucket is None:
                bucket = _Bucket(tokens=burst, updated=now)
                self._buckets[webhook_id] = bucket
                while len(self._buckets) > self._max_keys:
                    self._buckets.popitem(last=False)
            else:
                _refill(bucket, now, rate, burst)
                self._buckets.move_to_end(webhook_id)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return
            wait = _wait_s(bucket, rate)
        raise QuotaExceeded(
            f"limite de entregas deste webhook excedido; tente de novo em {wait} s",
            retry_after_s=wait,
        )


def _select() -> Select[Any]:
    last = (
        select(func.max(webhook_deliveries.c.received_at))
        .where(
            webhook_deliveries.c.company_id == webhooks.c.company_id,
            webhook_deliveries.c.webhook_id == webhooks.c.id,
        )
        .scalar_subquery()
        .label("last_delivery_at")
    )
    return select(
        webhooks.c.company_id,
        webhooks.c.id,
        webhooks.c.robot_id,
        webhooks.c.title,
        webhooks.c.key_version,
        webhooks.c.enabled,
        webhooks.c.rate_per_minute,
        webhooks.c.created_by,
        webhooks.c.created_at,
        webhooks.c.updated_at,
        webhooks.c.rotated_at,
        robots.c.slug.label("robot_slug"),
        robots.c.area_id,
        last,
    ).select_from(
        webhooks.join(
            robots,
            and_(robots.c.company_id == webhooks.c.company_id, robots.c.id == webhooks.c.robot_id),
        )
    )


def _info(row: RowMapping) -> WebhookInfo:
    return WebhookInfo(
        webhook_id=row["id"],
        company_id=row["company_id"],
        area_id=row["area_id"],
        robot_id=row["robot_id"],
        robot_slug=row["robot_slug"],
        title=row["title"],
        enabled=row["enabled"],
        rate_per_minute=row["rate_per_minute"],
        path=webhook_path(row["id"]),
        key_version=row["key_version"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        rotated_at=row["rotated_at"],
        last_delivery_at=row["last_delivery_at"],
    )


#: Quantos níveis de objetos e listas um corpo de webhook pode ter.
MAX_BODY_DEPTH = 64


def _check_depth(body: bytes) -> None:
    """Recusa (``ValueError``) um corpo com mais de :data:`MAX_BODY_DEPTH` níveis de ``{`` e
    ``[`` fora de textos, sem ler o JSON."""
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # \
                escaped = True
            elif byte == 0x22:  # "
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > MAX_BODY_DEPTH:
                raise ValueError("aninhamento demais")
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"constante {name} não é JSON")


def _parse_timestamp(raw: str | None, now: datetime) -> None:
    if raw is None or not _TIMESTAMP.fullmatch(raw):
        raise SignatureInvalid(_REJECTED)
    if abs(now.timestamp() - int(raw)) > MAX_CLOCK_SKEW_S:
        raise SignatureInvalid(_REJECTED)


class PgWebhooks(PgService):
    """Webhooks no PostgreSQL: administração e entrega.

    Segue :class:`rapa.core.contracts.WebhookService` (com o principal) e
    :class:`rapa.core.contracts.WebhookReceiver` (a entrega, sem token).
    """

    def __init__(
        self,
        db: Database,
        keys: WebhookKeys,
        *,
        limiter: WebhookLimiter | None = None,
        gate: WebhookGate | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        super().__init__(db)
        self.keys = keys
        self.limiter = limiter or WebhookLimiter()
        self.gate = gate or WebhookGate()
        self.clock = clock

    def __repr__(self) -> str:
        return "PgWebhooks()"

    # -- administração ---------------------------------------------------------------

    async def _robot(
        self, session: AsyncSession, principal: Principal, robot_id: UUID
    ) -> RowMapping:
        row: RowMapping | None = (
            (
                await session.execute(
                    select(robots.c.id, robots.c.area_id, robots.c.slug).where(
                        robots.c.company_id == principal.company_id, robots.c.id == robot_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("robô não encontrado")
        return row

    async def _webhook(
        self,
        session: AsyncSession,
        principal: Principal,
        robot_id: UUID,
        webhook_id: UUID,
        *,
        lock: bool = False,
    ) -> RowMapping:
        await self._robot(session, principal, robot_id)
        query = _select().where(
            webhooks.c.company_id == principal.company_id,
            webhooks.c.robot_id == robot_id,
            webhooks.c.id == webhook_id,
            webhooks.c.archived_at.is_(None),
        )
        if lock:
            query = query.with_for_update(of=webhooks)
        row: RowMapping | None = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise NotFound("webhook não encontrado")
        return row

    @staticmethod
    def _check_manage(principal: Principal, area_id: UUID) -> None:
        if not can_automate(principal, area_id):
            raise Forbidden("só quem opera robôs nesta área pode criar ou mudar webhooks")

    def _secret(self, row: RowMapping, salt: bytes, version: int) -> WebhookSecret:
        info = _info(row)
        return WebhookSecret(
            webhook=info,
            secret=self.keys.derive(info.company_id, info.webhook_id, salt, version),
        )

    async def list_webhooks(self, principal: Principal, robot_id: UUID) -> list[WebhookInfo]:
        async with self.db.tenant_session(principal.company_id) as session:
            await self._robot(session, principal, robot_id)
            rows = (
                (
                    await session.execute(
                        _select()
                        .where(
                            webhooks.c.company_id == principal.company_id,
                            webhooks.c.robot_id == robot_id,
                            webhooks.c.archived_at.is_(None),
                        )
                        .order_by(webhooks.c.created_at, webhooks.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [_info(row) for row in rows]

    async def get_webhook(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID
    ) -> WebhookInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            try:
                return _info(await self._webhook(session, principal, robot_id, webhook_id))
            except NotFound:
                return None

    async def create_webhook(
        self, principal: Principal, robot_id: UUID, spec: WebhookSpec
    ) -> WebhookSecret:
        company_id = principal.company_id
        webhook_id = new_id()
        salt = secrets.token_bytes(32)
        async with self.db.tenant_session(company_id) as session:
            robot = await self._robot(session, principal, robot_id)
            self._check_manage(principal, robot["area_id"])
            if await published_robot(session, company_id, robot_id) is None:
                raise Conflict("o robô precisa estar publicado para ganhar um webhook")
            await session.execute(
                insert(webhooks).values(
                    company_id=company_id,
                    id=webhook_id,
                    robot_id=robot_id,
                    title=clean_title(spec.title),
                    key_salt=salt,
                    key_version=1,
                    enabled=spec.enabled,
                    rate_per_minute=spec.rate_per_minute,
                    created_by=principal.subject,
                    updated_by=principal.subject,
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="webhook.criado",
                target=f"webhook:{webhook_id}",
                payload={
                    "robo": robot["slug"],
                    "por_minuto": spec.rate_per_minute,
                    "ligado": spec.enabled,
                },
            )
            row = await self._webhook(session, principal, robot_id, webhook_id)
        return self._secret(row, salt, 1)

    async def update_webhook(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID, changes: WebhookChanges
    ) -> WebhookInfo:
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            row = await self._webhook(session, principal, robot_id, webhook_id, lock=True)
            self._check_manage(principal, row["area_id"])
            values: dict[str, Any] = {"updated_by": principal.subject, "updated_at": func.now()}
            if changes.title is not None:
                values["title"] = clean_title(changes.title)
            if changes.rate_per_minute is not None:
                values["rate_per_minute"] = changes.rate_per_minute
            if changes.enabled is not None:
                values["enabled"] = changes.enabled
            await session.execute(
                update(webhooks)
                .where(webhooks.c.company_id == company_id, webhooks.c.id == webhook_id)
                .values(**values)
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="webhook.alterado",
                target=f"webhook:{webhook_id}",
                payload={
                    "por_minuto": values.get("rate_per_minute", row["rate_per_minute"]),
                    "ligado": values.get("enabled", row["enabled"]),
                },
            )
            return _info(await self._webhook(session, principal, robot_id, webhook_id))

    async def rotate_secret(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID
    ) -> WebhookSecret:
        company_id = principal.company_id
        salt = secrets.token_bytes(32)
        async with self.db.tenant_session(company_id) as session:
            row = await self._webhook(session, principal, robot_id, webhook_id, lock=True)
            self._check_manage(principal, row["area_id"])
            version = int(row["key_version"]) + 1
            await session.execute(
                update(webhooks)
                .where(webhooks.c.company_id == company_id, webhooks.c.id == webhook_id)
                .values(
                    key_salt=salt,
                    key_version=version,
                    rotated_at=func.now(),
                    updated_by=principal.subject,
                    updated_at=func.now(),
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="webhook.chave_trocada",
                target=f"webhook:{webhook_id}",
                payload={"versao_da_chave": version},
            )
            row = await self._webhook(session, principal, robot_id, webhook_id)
        return self._secret(row, salt, version)

    async def delete_webhook(self, principal: Principal, robot_id: UUID, webhook_id: UUID) -> None:
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            row = await self._webhook(session, principal, robot_id, webhook_id, lock=True)
            self._check_manage(principal, row["area_id"])
            await session.execute(
                update(webhooks)
                .where(webhooks.c.company_id == company_id, webhooks.c.id == webhook_id)
                .values(
                    enabled=False,
                    archived_at=func.now(),
                    updated_by=principal.subject,
                    updated_at=func.now(),
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="webhook.removido",
                target=f"webhook:{webhook_id}",
                payload={"robo": row["robot_slug"]},
            )

    # -- entrega -----------------------------------------------------------------------

    async def _lookup(self, webhook_id: UUID) -> RowMapping | None:
        """Lê o webhook sem saber a empresa (a política só mostra esta linha)."""
        async with self.db.webhook_lookup_session(webhook_id) as session:
            row: RowMapping | None = (
                (
                    await session.execute(
                        select(
                            webhooks.c.company_id,
                            webhooks.c.id,
                            webhooks.c.robot_id,
                            webhooks.c.key_salt,
                            webhooks.c.key_version,
                            webhooks.c.enabled,
                            webhooks.c.rate_per_minute,
                            webhooks.c.archived_at,
                        ).where(webhooks.c.id == webhook_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return row

    async def deliver(
        self,
        webhook_id: UUID,
        *,
        timestamp: str | None,
        delivery_id: str | None,
        signature: str | None,
        body: bytes,
        client: str | None = None,
    ) -> WebhookDelivery:
        """Confere a assinatura e põe a execução na fila (ou devolve a da mesma entrega).

        ``client`` é o endereço de origem (para o limite de recusas por cliente de
        :class:`WebhookGate`).
        """
        now = self.clock()
        if delivery_id is None or not _DELIVERY_ID.fullmatch(delivery_id):
            raise SignatureInvalid(_REJECTED)
        match = _SIGNATURE.fullmatch(signature or "")
        if match is None or timestamp is None or not _TIMESTAMP.fullmatch(timestamp):
            raise SignatureInvalid(_REJECTED)
        # Antes do banco: o desconhecido de há pouco é recusado direto, e as leituras
        # têm limite (por cliente que erra e no processo).
        if self.gate.known_unknown(webhook_id):
            raise SignatureInvalid(_REJECTED)
        self.gate.admit(client)
        hook = await self._lookup(webhook_id)
        if hook is None or hook["archived_at"] is not None:
            # Removido é como se não existisse (e não volta: a trava não desarquiva).
            self.gate.remember_unknown(webhook_id)
            hook = None
        reference = "sha256=" + "0" * 64
        if hook is not None:
            secret = self.keys.derive(
                hook["company_id"], hook["id"], bytes(hook["key_salt"]), hook["key_version"]
            )
            reference = sign_delivery(secret, timestamp, delivery_id, body)
        # Sem webhook, compara com um valor qualquer: o tempo da resposta não diz se ele existe.
        valid = hmac.compare_digest(reference, f"sha256={match.group(1)}")
        if hook is None or not valid:
            self.gate.failed(client)
            raise SignatureInvalid(_REJECTED)
        try:
            _parse_timestamp(timestamp, now)
        except SignatureInvalid:
            self.gate.failed(client)
            raise
        if not hook["enabled"]:
            raise Conflict("o webhook está desligado")
        self.limiter.check(webhook_id, int(hook["rate_per_minute"]))
        try:
            # O aninhamento é limitado antes de ler (a profundidade que o leitor aguenta
            # muda com a pilha de cada máquina). NaN e Infinity não são JSON (o banco os
            # recusaria): o corpo é recusado antes.
            _check_depth(body)
            inputs = json.loads(body, parse_constant=_refuse_constant)
        except UnicodeDecodeError, ValueError, RecursionError:
            raise InvalidInput(
                "o corpo precisa ser um objeto JSON", ["corpo: JSON inválido"]
            ) from None
        if not isinstance(inputs, dict):
            raise InvalidInput("o corpo precisa ser um objeto JSON", ["corpo: esperado objeto"])
        company_id: UUID = hook["company_id"]
        body_sha256 = hashlib.sha256(body).hexdigest()
        async with self.db.tenant_session(company_id) as session:
            # Sob a trava de inícios da empresa: duas entregas iguais não passam juntas.
            await lock_company_starts(session, company_id)
            # O webhook pode ter sido desligado, removido ou trocado de chave desde a leitura.
            current = (
                await session.execute(
                    select(webhooks.c.enabled, webhooks.c.archived_at, webhooks.c.key_version)
                    .where(webhooks.c.company_id == company_id, webhooks.c.id == webhook_id)
                    .with_for_update(read=True)
                )
            ).one_or_none()
            if current is None or current[1] is not None or current[2] != hook["key_version"]:
                raise SignatureInvalid(_REJECTED)
            if not current[0]:
                raise Conflict("o webhook está desligado")
            existing = (
                await session.execute(
                    select(webhook_deliveries.c.run_id, webhook_deliveries.c.body_sha256).where(
                        webhook_deliveries.c.company_id == company_id,
                        webhook_deliveries.c.webhook_id == webhook_id,
                        webhook_deliveries.c.delivery_id == delivery_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                if not hmac.compare_digest(existing[1], body_sha256):
                    raise Conflict(
                        "esta entrega já foi recebida com outro conteúdo; use outro identificador"
                    )
                return await self._delivery(
                    session, company_id, webhook_id, delivery_id, existing[0], True
                )
            robot = await published_robot(session, company_id, hook["robot_id"])
            if robot is None:
                raise Conflict("o robô deste webhook não está publicado")
            errors = validate_inputs(Manifest.model_validate(robot["manifest"]), inputs)
            if errors:
                raise InvalidInput("entradas inválidas", errors)
            queued = await enqueue_run(
                session,
                company_id=company_id,
                requested_by=f"webhook:{webhook_id}",
                robot=robot,
                slug=robot["slug"],
                inputs=inputs,
                trigger=RunTrigger.WEBHOOK,
                idempotency_key=f"{webhook_id}:{delivery_id}",
                scope=WEBHOOK_SCOPE,
                audit_extra={
                    "webhook": str(webhook_id),
                    "entrega": hashlib.sha256(delivery_id.encode()).hexdigest(),
                },
            )
            await session.execute(
                insert(webhook_deliveries).values(
                    company_id=company_id,
                    webhook_id=webhook_id,
                    delivery_id=delivery_id,
                    body_sha256=body_sha256,
                    run_id=queued.run_id,
                )
            )
            return await self._delivery(
                session, company_id, webhook_id, delivery_id, queued.run_id, False
            )

    @staticmethod
    async def _delivery(
        session: AsyncSession,
        company_id: UUID,
        webhook_id: UUID,
        delivery_id: str,
        run_id: UUID,
        duplicate: bool,
    ) -> WebhookDelivery:
        state = await session.scalar(
            select(runs.c.state).where(runs.c.company_id == company_id, runs.c.id == run_id)
        )
        return WebhookDelivery(
            webhook_id=webhook_id,
            delivery_id=delivery_id,
            run_id=run_id,
            state=RunState(str(state)),
            duplicate=duplicate,
        )
