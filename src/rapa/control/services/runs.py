"""Início e consulta de execuções no PostgreSQL (:class:`RunService`).

Iniciar uma execução só a coloca na fila (estado ``queued``); o despachante a
pega depois. A chave de idempotência vale por empresa: a mesma chave com a
mesma requisição devolve a mesma execução, e com outra requisição é conflito.

:func:`enqueue_run` e :func:`enqueue_many` são o único caminho que inclui
execuções: o REST, o MCP e a interface (por :class:`PgRunService`), as agendas,
os webhooks e os lotes. Todos tomam a mesma trava da empresa, respeitam o limite
da fila e auditam cada execução.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    company_quotas,
    idempotency_keys,
    robot_versions,
    robots,
    runs,
)
from rapa.control.services import audit
from rapa.control.services._common import (
    PRIORITY_BY_TRIGGER,
    PgService,
    run_status,
    visible_areas,
)
from rapa.control.services.inputs import validate_inputs
from rapa.control.services.robots import published_query
from rapa.core.contracts import (
    MAX_RUNS_LISTED,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    Role,
    RunMode,
    RunState,
    RunStatus,
    RunTrigger,
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = [
    "DEFAULT_MAX_QUEUED",
    "IDEMPOTENCY_SCOPE",
    "IDEMPOTENCY_TTL",
    "QUEUE_FULL_RETRY_AFTER_S",
    "Enqueued",
    "PgRunService",
    "enqueue_many",
    "enqueue_run",
    "ensure_queue_room",
    "lock_company_starts",
    "request_hash",
    "run_status_in",
]

#: Quem pode disparar uma execução (mesma regra da plataforma em memória).
_RUN_ROLES = frozenset({Role.OPERATOR, Role.CREATOR, Role.APPROVER, Role.AREA_ADMIN})
#: Quem pode disparar a validação de uma versão ainda não publicada.
_VALIDATION_ROLES = frozenset({Role.CREATOR, Role.APPROVER})

IDEMPOTENCY_SCOPE = "execucoes.iniciar"
IDEMPOTENCY_TTL = timedelta(hours=24)
#: Limite da fila por empresa quando ela ainda não tem cota cadastrada.
DEFAULT_MAX_QUEUED = 1000
QUEUE_FULL_RETRY_AFTER_S = 30
_MAX_KEY_LENGTH = 255

#: Classe das travas consultivas que serializam os inícios de uma empresa.
_START_LOCK_CLASS = 7104


def request_hash(slug: str, inputs: Mapping[str, Any]) -> str:
    """Resumo da requisição, comparado quando a chave de idempotência se repete."""
    canonical = json.dumps({"slug": slug, "inputs": inputs}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _status_query() -> Select[Any]:
    return select(
        runs.c.id,
        runs.c.state,
        runs.c.trigger,
        runs.c.priority,
        runs.c.queued_at,
        runs.c.created_at,
        runs.c.started_at,
        runs.c.finished_at,
        runs.c.outputs,
        runs.c.error_code,
        runs.c.error_message,
        runs.c.steps_done,
        robots.c.slug,
        robot_versions.c.version_no,
    ).select_from(
        runs.join(robots, robots.c.id == runs.c.robot_id).join(
            robot_versions, robot_versions.c.id == runs.c.version_id
        )
    )


async def run_status_in(session: AsyncSession, company_id: UUID, run_id: UUID) -> RunStatus | None:
    """Situação de uma execução da empresa, com a posição na fila (dentro da transação)."""
    row = (
        (
            await session.execute(
                _status_query().where(runs.c.id == run_id, runs.c.company_id == company_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    position = None
    if row["state"] == RunState.QUEUED.value:
        ahead = await session.scalar(
            select(func.count())
            .select_from(runs)
            .where(
                and_(
                    runs.c.company_id == company_id,
                    runs.c.state == RunState.QUEUED.value,
                    tuple_(runs.c.priority, runs.c.queued_at, runs.c.id)
                    < tuple_(literal(row["priority"]), literal(row["queued_at"]), literal(run_id)),
                )
            )
        )
        position = int(ahead or 0) + 1
    return run_status(row, position)


async def lock_company_starts(session: AsyncSession, company_id: UUID) -> None:
    """Serializa os inícios de execução da empresa até o fim da transação.

    A conferência de idempotência (de uma chave, de uma ocorrência de agenda ou
    de uma entrega de webhook) e o limite da fila ficam corretos mesmo com
    pedidos simultâneos. Pode ser pedida mais de uma vez na mesma transação.
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(_START_LOCK_CLASS, func.hashtext(str(company_id))))
    )


async def ensure_queue_room(session: AsyncSession, company_id: UUID, needed: int = 1) -> None:
    """Levanta :class:`QuotaExceeded` se a fila da empresa não comporta ``needed`` execuções."""
    max_queued = await session.scalar(
        select(company_quotas.c.max_queued).where(company_quotas.c.company_id == company_id)
    )
    queued = await session.scalar(
        select(func.count())
        .select_from(runs)
        .where(runs.c.company_id == company_id, runs.c.state == RunState.QUEUED.value)
    )
    limit = DEFAULT_MAX_QUEUED if max_queued is None else int(max_queued)
    if int(queued or 0) + needed > limit:
        if needed > 1:
            raise QuotaExceeded(
                f"a fila de execuções da empresa não comporta {needed} execuções agora "
                f"(limite de {limit} na fila); tente de novo mais tarde",
                retry_after_s=QUEUE_FULL_RETRY_AFTER_S,
            )
        raise QuotaExceeded(
            "a fila de execuções da empresa está cheia; tente de novo em instantes",
            retry_after_s=QUEUE_FULL_RETRY_AFTER_S,
        )


@dataclass(frozen=True, slots=True)
class Enqueued:
    """A execução que um início devolveu: nova, ou a de antes com a mesma chave."""

    run_id: UUID
    created: bool


def _run_row(
    *,
    run_id: UUID,
    company_id: UUID,
    requested_by: str,
    robot: Mapping[Any, Any],
    inputs: Mapping[str, Any],
    trigger: RunTrigger,
    mode: RunMode,
    idempotency_key: str | None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "company_id": company_id,
        "area_id": robot["area_id"],
        "robot_id": robot["robot_id"],
        "version_id": robot["version_id"],
        "trigger": trigger.value,
        "mode": mode.value,
        "priority": PRIORITY_BY_TRIGGER[trigger],
        "state": RunState.QUEUED.value,
        "attempt": 0,
        "requested_by": requested_by,
        "inputs": dict(inputs),
        "steps_done": 0,
        "artifacts": [],
        "idempotency_key": idempotency_key,
    }


def _started_audit(
    run_id: UUID,
    *,
    slug: str,
    robot: Mapping[Any, Any],
    trigger: RunTrigger,
    mode: RunMode,
    digest: str,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "robo": slug,
        "versao": robot["version_no"],
        "origem": trigger.value,
        "modo": mode.value,
        "requisicao": digest,
    }
    payload.update(extra or {})
    return {"target": f"execucao:{run_id}", "payload": payload}


async def enqueue_run(
    session: AsyncSession,
    *,
    company_id: UUID,
    requested_by: str,
    robot: Mapping[Any, Any],
    slug: str,
    inputs: Mapping[str, Any],
    trigger: RunTrigger,
    mode: RunMode = RunMode.FULL,
    idempotency_key: str | None = None,
    scope: str = IDEMPOTENCY_SCOPE,
    audit_extra: Mapping[str, Any] | None = None,
) -> Enqueued:
    """Põe uma execução na fila, dentro da transação (já fixada na empresa).

    ``robot`` traz ``robot_id``, ``area_id``, ``version_id`` e ``version_no`` (a
    versão que vai rodar). Com ``idempotency_key``, a mesma chave no mesmo
    ``scope`` e com a mesma requisição devolve a execução de antes (enquanto a
    chave vale); com outra requisição, é conflito. Cada origem usa o seu escopo,
    então uma chave do REST nunca alcança a de uma agenda ou de um webhook.
    """
    await lock_company_starts(session, company_id)
    digest = request_hash(slug, inputs)
    existing = None
    if idempotency_key is not None:
        existing = (
            (
                await session.execute(
                    select(
                        idempotency_keys.c.request_hash,
                        idempotency_keys.c.run_id,
                        (idempotency_keys.c.expires_at > func.now()).label("live"),
                    )
                    .where(
                        idempotency_keys.c.company_id == company_id,
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.key == idempotency_key,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None and existing["live"]:
            if existing["request_hash"] != digest:
                raise Conflict("a chave de idempotência já foi usada com outra requisição")
            found = await session.scalar(
                select(runs.c.id).where(
                    runs.c.company_id == company_id, runs.c.id == existing["run_id"]
                )
            )
            if found is not None:
                return Enqueued(run_id=found, created=False)

    await ensure_queue_room(session, company_id)
    run_id = new_id()
    if idempotency_key is not None:
        expires = func.now() + literal(IDEMPOTENCY_TTL)
        if existing is None:
            await session.execute(
                insert(idempotency_keys).values(
                    company_id=company_id,
                    scope=scope,
                    key=idempotency_key,
                    request_hash=digest,
                    run_id=run_id,
                    expires_at=expires,
                )
            )
        else:
            await session.execute(
                update(idempotency_keys)
                .where(
                    idempotency_keys.c.company_id == company_id,
                    idempotency_keys.c.scope == scope,
                    idempotency_keys.c.key == idempotency_key,
                )
                .values(
                    request_hash=digest,
                    run_id=run_id,
                    created_at=func.now(),
                    expires_at=expires,
                )
            )
    await session.execute(
        insert(runs).values(
            **_run_row(
                run_id=run_id,
                company_id=company_id,
                requested_by=requested_by,
                robot=robot,
                inputs=inputs,
                trigger=trigger,
                mode=mode,
                idempotency_key=idempotency_key,
            )
        )
    )
    entry = _started_audit(
        run_id, slug=slug, robot=robot, trigger=trigger, mode=mode, digest=digest, extra=audit_extra
    )
    await audit.append(
        session,
        company_id=company_id,
        actor=requested_by,
        action="execucao.iniciada",
        target=entry["target"],
        payload=entry["payload"],
    )
    return Enqueued(run_id=run_id, created=True)


async def enqueue_many(
    session: AsyncSession,
    *,
    company_id: UUID,
    requested_by: str,
    robot: Mapping[Any, Any],
    slug: str,
    inputs: Sequence[Mapping[str, Any]],
    trigger: RunTrigger,
    audit_extra: Mapping[str, Any] | None = None,
) -> list[UUID]:
    """Põe várias execuções do mesmo robô na fila, todas ou nenhuma (um lote).

    O limite da fila da empresa vale para o conjunto: se não couberem todas,
    nenhuma entra (:class:`QuotaExceeded`). Cada execução tem a sua linha na
    auditoria, como as outras.
    """
    if not inputs:
        return []
    await lock_company_starts(session, company_id)
    await ensure_queue_room(session, company_id, len(inputs))
    run_ids = [new_id() for _ in inputs]
    rows = [
        _run_row(
            run_id=run_id,
            company_id=company_id,
            requested_by=requested_by,
            robot=robot,
            inputs=values,
            trigger=trigger,
            mode=RunMode.FULL,
            idempotency_key=None,
        )
        for run_id, values in zip(run_ids, inputs, strict=True)
    ]
    await session.execute(insert(runs), rows)
    entries = []
    for run_id, values in zip(run_ids, inputs, strict=True):
        entry = _started_audit(
            run_id,
            slug=slug,
            robot=robot,
            trigger=trigger,
            mode=RunMode.FULL,
            digest=request_hash(slug, values),
            extra=audit_extra,
        )
        entries.append(entry)
    await audit.append_many(
        session,
        company_id=company_id,
        actor=requested_by,
        action="execucao.iniciada",
        entries=entries,
    )
    return run_ids


class PgRunService(PgService):
    """Início e consulta de execuções."""

    async def start(
        self,
        principal: Principal,
        slug: str,
        inputs: dict[str, Any],
        trigger: RunTrigger,
        idempotency_key: str | None = None,
    ) -> RunStatus:
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= _MAX_KEY_LENGTH:
            raise InvalidInput(
                "chave de idempotência inválida",
                [f"a chave deve ter de 1 a {_MAX_KEY_LENGTH} caracteres"],
            )
        async with self.db.tenant_session(principal.company_id) as session:
            robot = (
                (await session.execute(published_query(principal).where(robots.c.slug == slug)))
                .mappings()
                .one_or_none()
            )
            if robot is None:
                raise NotFound(f"robô '{slug}' não encontrado")
            if not any(principal.has_role(role, robot["area_id"]) for role in _RUN_ROLES):
                raise Forbidden("sem permissão para executar este robô")
            manifest = Manifest.model_validate(robot["manifest"])
            errors = validate_inputs(manifest, inputs)
            if errors:
                raise InvalidInput("entradas inválidas", errors)
            return await self._enqueue(
                session,
                principal,
                robot=robot,
                slug=slug,
                inputs=inputs,
                trigger=trigger,
                mode=RunMode.FULL,
                idempotency_key=idempotency_key,
            )

    async def start_validation(
        self,
        principal: Principal,
        robot_id: UUID,
        version_no: int,
        inputs: dict[str, Any],
    ) -> RunStatus:
        """Enfileira a validação de uma versão: roda só até antes do primeiro passo que grava."""
        async with self.db.tenant_session(principal.company_id) as session:
            robot = (
                (
                    await session.execute(
                        select(
                            robots.c.id.label("robot_id"),
                            robots.c.area_id,
                            robots.c.slug,
                            robot_versions.c.id.label("version_id"),
                            robot_versions.c.version_no,
                            robot_versions.c.manifest,
                        )
                        .select_from(
                            robots.join(robot_versions, robot_versions.c.robot_id == robots.c.id)
                        )
                        .where(
                            robots.c.id == robot_id,
                            robots.c.company_id == principal.company_id,
                            robot_versions.c.version_no == version_no,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if robot is None or not principal.roles_in(robot["area_id"]):
                raise NotFound("versão não encontrada")
            if not any(principal.has_role(role, robot["area_id"]) for role in _VALIDATION_ROLES):
                raise Forbidden("sem permissão para validar")
            errors = validate_inputs(Manifest.model_validate(robot["manifest"]), inputs)
            if errors:
                raise InvalidInput("entradas inválidas", errors)
            return await self._enqueue(
                session,
                principal,
                robot=robot,
                slug=robot["slug"],
                inputs=inputs,
                trigger=RunTrigger.VALIDATION,
                mode=RunMode.UNTIL_FIRST_WRITE,
                idempotency_key=None,
            )

    async def _enqueue(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        robot: RowMapping,
        slug: str,
        inputs: dict[str, Any],
        trigger: RunTrigger,
        mode: RunMode,
        idempotency_key: str | None,
    ) -> RunStatus:
        queued = await enqueue_run(
            session,
            company_id=principal.company_id,
            requested_by=principal.subject,
            robot=robot,
            slug=slug,
            inputs=inputs,
            trigger=trigger,
            mode=mode,
            idempotency_key=idempotency_key,
        )
        status = await self._status(session, principal.company_id, queued.run_id)
        if status is None:  # pragma: no cover - acabou de ser incluída
            raise NotFound("execução não encontrada")
        return status

    @staticmethod
    async def _status(session: AsyncSession, company_id: UUID, run_id: UUID) -> RunStatus | None:
        return await run_status_in(session, company_id, run_id)

    async def get(self, principal: Principal, run_id: UUID) -> RunStatus | None:
        async with self.db.tenant_session(principal.company_id) as session:
            allowed = visible_areas(principal)
            if allowed is not None:
                area = await session.scalar(
                    select(runs.c.area_id).where(
                        runs.c.id == run_id, runs.c.company_id == principal.company_id
                    )
                )
                if area is None or area not in allowed:
                    return None
            return await self._status(session, principal.company_id, run_id)

    async def list_recent(self, principal: Principal, limit: int = 50) -> list[RunStatus]:
        limit = max(1, min(limit, MAX_RUNS_LISTED))
        query = (
            _status_query()
            .where(runs.c.company_id == principal.company_id)
            .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            .limit(limit)
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(runs.c.area_id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [run_status(row) for row in rows]
