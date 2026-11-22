"""Retenção: o despachante apaga, uma vez por hora, o que passou do prazo.

Os prazos são da plataforma (``ServerSettings``, mínimo de 1 dia) e ainda estão
**a validar com o jurídico**:

- capturas (telas e vídeos) de execuções que deram certo (ou foram canceladas):
  ``RAPA_RETENCAO_SUCESSO_DIAS`` (7 dias); os arquivos baixados por elas ficam;
- as evidências, isto é, todos os artefatos de execuções que falharam ou ficaram
  indeterminadas e os arquivos baixados das que deram certo, e os retratos das
  falhas (``run_snapshots``): ``RAPA_RETENCAO_FALHAS_DIAS`` (90 dias);
- chaves de idempotência vencidas: apagadas (uma chave vencida já não vale);
- partições mensais de ``run_events`` que terminaram há mais de
  ``RAPA_RETENCAO_EVENTOS_DIAS`` (400 dias, para o consumo mensal e a auditoria).

O prazo conta do fim da execução (``finished_at``) e, nos retratos, da gravação.

Tudo em lotes pequenos, cada lote numa transação curta (com o limite de espera
por travas da manutenção), e cada volta com tempo limitado (``max_tick_s``): o que
não couber fica para a próxima (:attr:`Retention.behind`). O despachante não tem
``DELETE`` em nenhuma tabela: os retratos, as chaves e as partições são apagados
por funções ``SECURITY DEFINER`` da migração 0008, que só recebem a idade e o
tamanho do lote, nunca uma lista de identificadores. Os artefatos ficam no
:class:`rapa.control.artifacts.ArtifactStore`; a execução só ganha as marcas
``captures_expired_at`` (as telas e o vídeo saíram; os arquivos baixados ficam) e
``artifacts_expired_at`` (tudo saiu). A lista de artefatos fica, com os nomes,
tamanhos e hashes, e a API passa a mostrar "expirado" e a não servir mais o arquivo
de cada artefato que a marca cobre (:func:`rapa.core.artifacts.is_capture`). Os
arquivos de cada execução são apagados antes de a marca ser gravada, na mesma
transação: se a gravação falhar, a próxima volta apaga de novo (nada) e marca. Uma
execução cujos arquivos não puderam ser apagados fica sem a marca e volta na
próxima volta (só o tipo do erro vai para o log).

Cada execução expirada (as capturas ou tudo) e cada retrato apagado entram na
auditoria da empresa; a
remoção de uma partição de eventos, que não é de nenhuma empresa, vai só para o log,
e as chaves de idempotência vencidas (que já não valiam para nada) só são contadas.

Empresa congelada fica de fora de tudo: nada dela expira enquanto está congelada (as
evidências ficam para a exportação da saída da empresa, ``docs/OPERACAO.md``). Os
artefatos são conferidos sob a trava da situação da empresa (a mesma que congelar
toma), então um congelamento no meio do lote espera ou já é visto; os retratos e as
chaves, pelas funções do banco, que pulam a empresa congelada; e uma partição de
eventos com algum evento de empresa congelada não é removida até ela sair dessa
situação (descongelada ou apagada).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Column, RowMapping, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.artifacts import ArtifactStore
from rapa.control.db.schema import companies, runs
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import lock_company_status, now_utc
from rapa.control.services.queue import (
    DEFAULT_MAINTENANCE_LOCK_TIMEOUT,
    DEFAULT_MAINTENANCE_STATEMENT_TIMEOUT,
    DISPATCHER_ACTOR,
)
from rapa.core.artifacts import is_capture, valid_artifact_name
from rapa.core.contracts import CompanyStatus, RunState

__all__ = [
    "MAX_RETENTION_DAYS",
    "Retention",
    "RetentionPolicy",
    "RetentionReport",
]

logger = logging.getLogger("rapa.control.retention")

#: Maior prazo aceito (100 anos): o mesmo limite das funções do banco.
MAX_RETENTION_DAYS = 36_500
#: Estados cujas capturas (telas e vídeo) saem pelo prazo curto.
_SHORT_STATES = (RunState.SUCCEEDED.value, RunState.CANCELLED.value)
#: Estados cujos artefatos todos saem pelo prazo longo (o das evidências).
_FINISHED_STATES = (
    RunState.SUCCEEDED.value,
    RunState.CANCELLED.value,
    RunState.FAILED.value,
    RunState.INDETERMINATE.value,
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Prazos (em dias) e o tamanho dos lotes da retenção."""

    success_days: int = 7
    failure_days: int = 90
    events_days: int = 400
    batch_size: int = 50

    def __post_init__(self) -> None:
        for days in (self.success_days, self.failure_days, self.events_days):
            if isinstance(days, bool) or not 1 <= days <= MAX_RETENTION_DAYS:
                raise ValueError(f"os prazos de retenção vão de 1 a {MAX_RETENTION_DAYS} dias")
        if not 1 <= self.batch_size <= 1000:
            raise ValueError("o lote da retenção vai de 1 a 1000")


@dataclass(slots=True)
class RetentionReport:
    """O que uma volta da retenção apagou."""

    #: Execuções cujos artefatos expiraram todos (arquivos apagados e marca gravada).
    artifacts: list[UUID] = field(default_factory=list)
    #: Execuções que deram certo cujas telas e vídeo expiraram (os downloads ficam).
    captures: list[UUID] = field(default_factory=list)
    #: Execuções cujos arquivos não puderam ser apagados (ficam para a próxima volta).
    failed: list[UUID] = field(default_factory=list)
    snapshots: int = 0
    idempotency_keys: int = 0
    partitions: int = 0
    #: A volta parou no limite de tempo (ou de lotes) com mais coisa para apagar.
    behind: bool = False


class Retention:
    """Uma volta da retenção por vez, sobre o banco do papel do despachante."""

    def __init__(
        self,
        db: Database,
        store: ArtifactStore,
        policy: RetentionPolicy | None = None,
        *,
        max_tick_s: float = 2.0,
        max_batches: int = 20,
        lock_timeout: timedelta = DEFAULT_MAINTENANCE_LOCK_TIMEOUT,
        statement_timeout: timedelta = DEFAULT_MAINTENANCE_STATEMENT_TIMEOUT,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if max_tick_s <= 0 or max_batches < 1:
            raise ValueError("o tempo e os lotes de uma volta da retenção precisam ser positivos")
        if lock_timeout <= timedelta(0) or statement_timeout <= timedelta(0):
            raise ValueError("os limites de tempo da retenção precisam ser positivos")
        self.db = db
        self.store = store
        self.policy = policy or RetentionPolicy()
        self.max_tick_s = max_tick_s
        self.max_batches = max_batches
        self.lock_timeout = lock_timeout
        self.statement_timeout = statement_timeout
        self.clock = clock
        self._behind = False

    @property
    def behind(self) -> bool:
        return self._behind

    async def _limit(self, session: AsyncSession) -> None:
        lock_ms = max(1, int(self.lock_timeout.total_seconds() * 1000))
        statement_ms = max(1, int(self.statement_timeout.total_seconds() * 1000))
        # SET não aceita parâmetros; os valores são inteiros calculados aqui.
        await session.execute(text(f"SET LOCAL lock_timeout = {lock_ms}"))
        await session.execute(text(f"SET LOCAL statement_timeout = {statement_ms}"))

    async def tick(self, now: datetime | None = None) -> RetentionReport:
        """Uma volta: artefatos, retratos, chaves de idempotência e partições de eventos.

        As etapas se revezam, um lote de cada por vez (uma não espera a outra acabar),
        cada uma com até ``max_batches`` lotes, e a volta para no limite de tempo; com
        coisa vencida ainda por apagar, :attr:`behind` fica verdadeiro.
        """
        now = now or self.clock()
        loop = asyncio.get_running_loop()
        give_up = loop.time() + self.max_tick_s
        report = RetentionReport()
        policy = self.policy

        def everything() -> Awaitable[int]:
            cutoff = now - timedelta(days=policy.failure_days)
            return self._expire_artifacts(
                _FINISHED_STATES, policy.failure_days, cutoff, report, captures_only=False
            )

        def captures() -> Awaitable[int]:
            cutoff = now - timedelta(days=policy.success_days)
            return self._expire_artifacts(
                _SHORT_STATES, policy.success_days, cutoff, report, captures_only=True
            )

        async def keys() -> int:
            deleted = await self._purge_idempotency_keys()
            report.idempotency_keys += deleted
            return deleted

        stages: list[Callable[[], Awaitable[int]]] = [
            everything,
            captures,
            lambda: self._purge_snapshots(report),
            keys,
        ]
        batches = dict.fromkeys(range(len(stages)), 0)
        active = list(range(len(stages)))
        while active:
            for stage in list(active):
                if loop.time() >= give_up or batches[stage] >= self.max_batches:
                    # O último lote veio cheio: ainda há o que apagar.
                    report.behind = True
                    active.remove(stage)
                    continue
                batches[stage] += 1
                if await stages[stage]() < policy.batch_size:
                    active.remove(stage)
        report.partitions = await self._drop_event_partitions()
        if report.partitions:
            logger.info(
                "retenção: %d partição(ões) de eventos com mais de %d dias removida(s)",
                report.partitions,
                policy.events_days,
            )
        if report.failed:
            logger.warning(
                "retenção: os arquivos de %d execução(ões) não puderam ser apagados; "
                "ficam para a próxima volta",
                len(report.failed),
            )
        self._behind = report.behind
        return report

    async def _expire_artifacts(
        self,
        states: Iterable[str],
        days: int,
        cutoff: datetime,
        report: RetentionReport,
        *,
        captures_only: bool,
    ) -> int:
        """Um lote de execuções vencidas: apaga os arquivos e grava a marca, juntos.

        Com ``captures_only`` (o prazo curto das execuções que deram certo), saem só as
        telas e o vídeo, os arquivos baixados ficam, e a marca é ``captures_expired_at``;
        senão sai tudo e a marca é ``artifacts_expired_at``. Devolve quantas execuções o
        lote pegou (as que falharam ao apagar contam, mas ficam de fora das próximas
        buscas desta volta).
        """
        mark: Column[Any] = (
            runs.c.captures_expired_at if captures_only else runs.c.artifacts_expired_at
        )
        frozen = select(companies.c.id).where(companies.c.status == CompanyStatus.FROZEN.value)
        pending = [
            runs.c.artifacts_expired_at.is_(None),
            mark.is_(None),
            runs.c.company_id.not_in(frozen),
        ]
        skip = list(report.failed)
        async with self.db.dispatcher_session() as session:
            await self._limit(session)
            query = (
                select(runs.c.id, runs.c.company_id, runs.c.state, runs.c.attempt, runs.c.artifacts)
                .where(*pending, runs.c.state.in_(list(states)), runs.c.finished_at < cutoff)
                .order_by(runs.c.finished_at, runs.c.id)
                .limit(self.policy.batch_size)
                .with_for_update(skip_locked=True)
            )
            if skip:
                query = query.where(runs.c.id.not_in(skip))
            rows = (await session.execute(query)).mappings().all()
            # A empresa congelada depois da busca: a trava da situação (compartilhada, a
            # que congelar toma exclusiva) espera o congelamento em andamento, e a
            # situação lida depois dela decide. As linhas da congelada ficam como estão.
            allowed = {
                company
                for company in sorted({row["company_id"] for row in rows})
                if await lock_company_status(session, company) is CompanyStatus.ACTIVE
            }
            expired: list[Any] = []
            for row in rows:
                if row["company_id"] not in allowed:
                    continue
                keep = _downloads(row) if captures_only else ()
                try:
                    await self.store.delete_run(row["company_id"], row["id"], keep=keep)
                except Exception as exc:  # permissão, disco, store fora: tenta na próxima volta
                    logger.warning("retenção: falha ao apagar artefatos: %s", type(exc).__name__)
                    report.failed.append(row["id"])
                    continue
                expired.append(row)
            if expired:
                await session.execute(
                    update(runs)
                    .where(runs.c.id.in_([row["id"] for row in expired]), *pending)
                    .values({mark.name: self.clock()})
                )
                action = (
                    "execucao.capturas_expiradas"
                    if captures_only
                    else "execucao.artefatos_expirados"
                )
                for row in expired:
                    await audit.append(
                        session,
                        company_id=row["company_id"],
                        actor=DISPATCHER_ACTOR,
                        action=action,
                        target=f"execucao:{row['id']}",
                        payload={"estado": row["state"], "prazo_dias": days},
                    )
        (report.captures if captures_only else report.artifacts).extend(
            row["id"] for row in expired
        )
        return len(rows)

    async def _purge_snapshots(self, report: RetentionReport) -> int:
        async with self.db.dispatcher_session() as session:
            await self._limit(session)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT company_id, run_id, attempt "
                            "FROM rapa_purge_run_snapshots(:days, :batch)"
                        ),
                        {"days": self.policy.failure_days, "batch": self.policy.batch_size},
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await audit.append(
                    session,
                    company_id=row["company_id"],
                    actor=DISPATCHER_ACTOR,
                    action="execucao.retrato_apagado",
                    target=f"execucao:{row['run_id']}",
                    payload={"tentativa": row["attempt"], "prazo_dias": self.policy.failure_days},
                )
        report.snapshots += len(rows)
        return len(rows)

    async def _purge_idempotency_keys(self) -> int:
        async with self.db.dispatcher_session() as session:
            await self._limit(session)
            deleted = await session.scalar(
                text("SELECT rapa_purge_idempotency_keys(:batch)"),
                {"batch": self.policy.batch_size},
            )
        return int(deleted or 0)

    async def _drop_event_partitions(self) -> int:
        async with self.db.dispatcher_session() as session:
            await self._limit(session)
            dropped = await session.scalar(
                text("SELECT rapa_drop_event_partitions(:days)"),
                {"days": self.policy.events_days},
            )
        return int(dropped or 0)


def _downloads(row: RowMapping) -> list[tuple[int, str]]:
    """Os arquivos baixados da execução, como ``(tentativa, nome)``: o que fica no prazo curto.

    Lê a lista crua de ``runs.artifacts``: um item que diz ser um download fica, mesmo
    fora do formato de hoje (na dúvida, a evidência fica para o prazo longo).
    """
    kept: list[tuple[int, str]] = []
    for item in row["artifacts"] or []:
        if not isinstance(item, dict) or is_capture(str(item.get("kind"))):
            continue
        name = item.get("name")
        attempt = item.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            attempt = max(1, int(row["attempt"] or 1))
        if isinstance(name, str) and valid_artifact_name(name):
            kept.append((attempt, name))
    return kept
