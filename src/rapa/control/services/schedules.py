"""Agendas: robôs publicados que rodam por uma expressão cron (:class:`PgSchedules`).

Regras:

- **Quem pode.** Qualquer papel na área do robô vê as agendas dele; só quem opera
  ou administra a área (:func:`rapa.core.contracts.can_automate`, que passa por
  ``Principal.has_role``) cria, muda e remove. A empresa vem sempre do principal.
- **Qual versão.** A agenda é do robô, não de uma versão: cada ocorrência roda a
  versão publicada naquele momento, como o REST e o MCP. As entradas fixas são
  conferidas pelo esquema da versão publicada na criação, a cada mudança e de
  novo a cada ocorrência; se uma versão nova não as aceitar, a ocorrência é
  ignorada (``entradas_invalidas``) e fica registrada.
- **Quando.** :mod:`rapa.core.cron` calcula ``next_run_at`` no fuso da agenda
  (``America/Sao_Paulo`` por padrão), sempre a partir de um instante, de forma
  determinística. Desligar a agenda apaga ``next_run_at``; ligar de novo conta a
  partir de agora (o que passou com ela desligada não roda). Remover só arquiva.

O :class:`Scheduler` roda dentro do laço do despachante (uma etapa da manutenção,
:class:`rapa.control.services.dispatcher.Dispatcher`). A cada volta, ele pede ao
banco do despachante as agendas vencidas (só os identificadores) e trata cada uma
numa transação fixada na empresa dela, pelo papel da aplicação:

1. trava a linha da agenda (``FOR UPDATE SKIP LOCKED``): outro despachante que
   chegue junto pula a agenda, e o que chegar depois vê ``next_run_at`` já adiante;
2. junta as ocorrências vencidas desde ``next_run_at``. As que passaram da
   tolerância (``catch_up``, ``RAPA_AGENDA_TOLERANCIA_S``) são ignoradas como
   ``perdida`` (o serviço estava fora do ar); das que estão dentro dela, só a mais
   nova roda, e as anteriores ficam como ``substituida``;
3. a que roda vira uma execução com origem ``schedule`` (prioridade de agenda,
   abaixo do interativo, do MCP e do REST), a chave de idempotência
   ``<agenda>:<horário>`` no escopo ``agenda`` e a ocorrência registrada na mesma
   transação. A ocorrência é única por agenda e horário (a chave primária de
   ``schedule_occurrences``): mesmo com dois despachantes, uma ocorrência nunca
   vira duas execuções;
4. a ocorrência também é ignorada, e registrada com o motivo, quando o robô não
   está publicado (``robo_indisponivel``), quando as entradas não valem para a
   versão publicada (``entradas_invalidas``), quando a execução anterior da
   agenda ainda não terminou (``anterior_em_andamento``: uma agenda nunca se
   sobrepõe a ela mesma) e quando a fila da empresa está cheia (``fila_cheia``);
5. ``next_run_at`` passa para a ocorrência seguinte depois de agora.

Nada é repetido: uma execução da agenda que falhar fica como falhou, e a próxima
ocorrência roda no horário dela. As regras de ``write_started_at`` da fila valem
como para qualquer execução.
"""

import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import robots, runs, schedule_occurrences, schedules
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import PgService, clean_title, now_utc, visible_areas
from rapa.control.services.inputs import validate_inputs
from rapa.control.services.robots import published_robot
from rapa.control.services.runs import enqueue_run, request_hash
from rapa.core.contracts import (
    MAX_OCCURRENCES_LISTED,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RunState,
    RunTrigger,
    ScheduleChanges,
    ScheduleInfo,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleSpec,
    can_automate,
)
from rapa.core.cron import CronError, CronExpression, load_zone
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = [
    "DEFAULT_CATCH_UP",
    "SCHEDULE_ACTOR",
    "SCHEDULE_SCOPE",
    "PgSchedules",
    "Scheduler",
    "SkipReason",
]

logger = logging.getLogger("rapa.control.schedules")

#: Quanto tempo depois do horário uma ocorrência ainda roda (o serviço esteve fora).
DEFAULT_CATCH_UP = timedelta(minutes=10)
#: Escopo das chaves de idempotência das ocorrências.
SCHEDULE_SCOPE = "agenda"
#: Quem aparece na auditoria das ocorrências ignoradas.
SCHEDULE_ACTOR = "agendador"
#: Quantas ocorrências vencidas de uma agenda são tratadas por volta.
_MAX_DUE = 500
_UNFINISHED = (RunState.QUEUED.value, RunState.RUNNING.value, RunState.AWAITING_HUMAN.value)
#: A ocorrência já tinha sido tratada (nada a registrar de novo).
_ALREADY_HANDLED = "ja_tratada"


class SkipReason:
    """Motivos de uma ocorrência ignorada (gravados em ``schedule_occurrences.reason``)."""

    MISSED = "perdida"
    SUPERSEDED = "substituida"
    ROBOT_UNAVAILABLE = "robo_indisponivel"
    INVALID_INPUTS = "entradas_invalidas"
    PREVIOUS_RUNNING = "anterior_em_andamento"
    QUEUE_FULL = "fila_cheia"


def _cron(text: str, timezone: str) -> tuple[CronExpression, Any]:
    try:
        return CronExpression.parse(text), load_zone(timezone)
    except CronError as exc:
        raise InvalidInput("agenda inválida", [str(exc)]) from None


def _first_run(cron: CronExpression, zone: Any, now: datetime) -> datetime:
    following = cron.next_after(now, zone)
    if following is None:
        raise InvalidInput("agenda inválida", ["a expressão não acontece nos próximos 50 anos"])
    return following


def _select() -> Select[Any]:
    return select(schedules, robots.c.slug.label("robot_slug"), robots.c.area_id).select_from(
        schedules.join(
            robots,
            and_(
                robots.c.company_id == schedules.c.company_id, robots.c.id == schedules.c.robot_id
            ),
        )
    )


def _info(row: RowMapping) -> ScheduleInfo:
    return ScheduleInfo(
        schedule_id=row["id"],
        company_id=row["company_id"],
        area_id=row["area_id"],
        robot_id=row["robot_id"],
        robot_slug=row["robot_slug"],
        title=row["title"],
        cron=row["cron"],
        timezone=row["timezone"],
        inputs=dict(row["inputs"]),
        enabled=row["enabled"],
        next_run_at=row["next_run_at"],
        last_occurrence_at=row["last_occurrence_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_published = published_robot


class PgSchedules(PgService):
    """Agendas no PostgreSQL (:class:`rapa.core.contracts.ScheduleService`)."""

    def __init__(self, db: Database, *, clock: Callable[[], datetime] = now_utc) -> None:
        super().__init__(db)
        self.clock = clock

    async def _robot(
        self, session: AsyncSession, principal: Principal, robot_id: UUID
    ) -> RowMapping:
        row = (
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

    async def _schedule(
        self, session: AsyncSession, principal: Principal, schedule_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        query = _select().where(
            schedules.c.company_id == principal.company_id,
            schedules.c.id == schedule_id,
            schedules.c.archived_at.is_(None),
        )
        if lock:
            query = query.with_for_update(of=schedules)
        row: RowMapping | None = (await session.execute(query)).mappings().one_or_none()
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("agenda não encontrada")
        return row

    @staticmethod
    def _check_manage(principal: Principal, area_id: UUID) -> None:
        if not can_automate(principal, area_id):
            raise Forbidden("só quem opera robôs nesta área pode criar ou mudar agendas")

    @staticmethod
    def _check_inputs(robot: RowMapping, inputs: dict[str, Any]) -> None:
        errors = validate_inputs(Manifest.model_validate(robot["manifest"]), inputs)
        if errors:
            raise InvalidInput("entradas inválidas", errors)

    async def list_schedules(self, principal: Principal) -> list[ScheduleInfo]:
        query = (
            _select()
            .where(
                schedules.c.company_id == principal.company_id, schedules.c.archived_at.is_(None)
            )
            .order_by(schedules.c.created_at.desc(), schedules.c.id)
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(robots.c.area_id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [_info(row) for row in rows]

    async def get_schedule(self, principal: Principal, schedule_id: UUID) -> ScheduleInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            try:
                return _info(await self._schedule(session, principal, schedule_id))
            except NotFound:
                return None

    async def create_schedule(self, principal: Principal, spec: ScheduleSpec) -> ScheduleInfo:
        cron, zone = _cron(spec.cron, spec.timezone)
        now = self.clock()
        next_run = _first_run(cron, zone, now) if spec.enabled else None
        company_id = principal.company_id
        schedule_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            robot = await self._robot(session, principal, spec.robot_id)
            self._check_manage(principal, robot["area_id"])
            published = await _published(session, company_id, spec.robot_id)
            if published is None:
                raise Conflict("o robô precisa estar publicado para ganhar uma agenda")
            self._check_inputs(published, spec.inputs)
            await session.execute(
                insert(schedules).values(
                    company_id=company_id,
                    id=schedule_id,
                    robot_id=spec.robot_id,
                    title=clean_title(spec.title),
                    cron=cron.text,
                    timezone=spec.timezone,
                    inputs=spec.inputs,
                    enabled=spec.enabled,
                    next_run_at=next_run,
                    created_by=principal.subject,
                    updated_by=principal.subject,
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="agenda.criada",
                target=f"agenda:{schedule_id}",
                payload={
                    "robo": robot["slug"],
                    "cron": cron.text,
                    "fuso": spec.timezone,
                    "ligada": spec.enabled,
                    "entradas": request_hash(robot["slug"], spec.inputs),
                },
            )
            return _info(await self._schedule(session, principal, schedule_id))

    async def update_schedule(
        self, principal: Principal, schedule_id: UUID, changes: ScheduleChanges
    ) -> ScheduleInfo:
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            row = await self._schedule(session, principal, schedule_id, lock=True)
            self._check_manage(principal, row["area_id"])
            cron_text = changes.cron if changes.cron is not None else row["cron"]
            timezone = changes.timezone if changes.timezone is not None else row["timezone"]
            enabled = changes.enabled if changes.enabled is not None else row["enabled"]
            inputs = changes.inputs if changes.inputs is not None else dict(row["inputs"])
            cron, zone = _cron(cron_text, timezone)
            if changes.inputs is not None or (enabled and not row["enabled"]):
                published = await _published(session, company_id, row["robot_id"])
                if published is None:
                    raise Conflict("o robô precisa estar publicado para a agenda rodar")
                self._check_inputs(published, inputs)
            timing = changes.cron is not None or changes.timezone is not None
            next_run: datetime | None = row["next_run_at"]
            if not enabled:
                next_run = None
            elif timing or not row["enabled"] or next_run is None:
                next_run = _first_run(cron, zone, self.clock())
            values: dict[str, Any] = {
                "cron": cron.text,
                "timezone": timezone,
                "enabled": enabled,
                "inputs": inputs,
                "next_run_at": next_run,
                "updated_by": principal.subject,
                "updated_at": func.now(),
            }
            if changes.title is not None:
                values["title"] = clean_title(changes.title)
            await session.execute(
                update(schedules)
                .where(schedules.c.company_id == company_id, schedules.c.id == schedule_id)
                .values(**values)
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="agenda.alterada",
                target=f"agenda:{schedule_id}",
                payload={
                    "cron": cron.text,
                    "fuso": timezone,
                    "ligada": enabled,
                    "entradas": request_hash(row["robot_slug"], inputs),
                },
            )
            return _info(await self._schedule(session, principal, schedule_id))

    async def delete_schedule(self, principal: Principal, schedule_id: UUID) -> None:
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            row = await self._schedule(session, principal, schedule_id, lock=True)
            self._check_manage(principal, row["area_id"])
            await session.execute(
                update(schedules)
                .where(schedules.c.company_id == company_id, schedules.c.id == schedule_id)
                .values(
                    enabled=False,
                    next_run_at=None,
                    archived_at=func.now(),
                    updated_by=principal.subject,
                    updated_at=func.now(),
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="agenda.removida",
                target=f"agenda:{schedule_id}",
                payload={"robo": row["robot_slug"]},
            )

    async def list_occurrences(
        self, principal: Principal, schedule_id: UUID, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        limit = max(1, min(limit, MAX_OCCURRENCES_LISTED))
        async with self.db.tenant_session(principal.company_id) as session:
            await self._schedule(session, principal, schedule_id)
            rows = (
                (
                    await session.execute(
                        select(schedule_occurrences)
                        .where(
                            schedule_occurrences.c.company_id == principal.company_id,
                            schedule_occurrences.c.schedule_id == schedule_id,
                        )
                        .order_by(schedule_occurrences.c.scheduled_for.desc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [
            ScheduleOccurrence(
                scheduled_for=row["scheduled_for"],
                status=ScheduleOccurrenceStatus(row["status"]),
                reason=row["reason"],
                run_id=row["run_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


class Scheduler:
    """Transforma as ocorrências vencidas das agendas em execuções, uma vez cada.

    ``app_db`` é o banco do papel da aplicação (as execuções nascem numa transação
    fixada na empresa da agenda); ``dispatcher_db``, o do despachante, só para
    achar as agendas vencidas de todas as empresas.
    """

    def __init__(
        self,
        app_db: Database,
        dispatcher_db: Database,
        *,
        catch_up: timedelta = DEFAULT_CATCH_UP,
        batch_size: int = 100,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if catch_up < timedelta(0):
            raise ValueError("a tolerância das agendas não pode ser negativa")
        if batch_size < 1:
            raise ValueError("o agendador precisa tratar pelo menos uma agenda por volta")
        self.app_db = app_db
        self.dispatcher_db = dispatcher_db
        self.catch_up = catch_up
        self.batch_size = batch_size
        self.clock = clock

    async def due(self, now: datetime) -> list[tuple[UUID, UUID]]:
        """As agendas vencidas (empresa e identificador), das mais atrasadas às mais novas."""
        async with self.dispatcher_db.dispatcher_session() as session:
            rows = (
                await session.execute(
                    select(schedules.c.company_id, schedules.c.id)
                    .where(
                        schedules.c.enabled.is_(True),
                        schedules.c.archived_at.is_(None),
                        schedules.c.next_run_at <= now,
                    )
                    .order_by(schedules.c.next_run_at)
                    .limit(self.batch_size)
                )
            ).all()
        return [(row[0], row[1]) for row in rows]

    async def tick(self, now: datetime | None = None) -> int:
        """Trata as agendas vencidas; devolve quantas execuções entraram na fila.

        Uma agenda que falha por um erro inesperado (o banco, por exemplo) não
        impede as outras; o primeiro erro é levantado no fim, para a saúde do
        despachante acusar.
        """
        now = now or self.clock()
        enqueued = 0
        failure: Exception | None = None
        for company_id, schedule_id in await self.due(now):
            try:
                enqueued += await self.fire(company_id, schedule_id, now)
            except Exception as exc:  # um erro numa agenda não para as outras
                logger.error("falha ao disparar uma agenda: %s", type(exc).__name__)
                failure = failure or exc
        if failure is not None:
            raise failure
        return enqueued

    async def fire(self, company_id: UUID, schedule_id: UUID, now: datetime) -> int:
        """Trata as ocorrências vencidas de uma agenda; devolve 1 se uma virou execução."""
        async with self.app_db.tenant_session(company_id) as session:
            row = (
                (
                    await session.execute(
                        select(schedules)
                        .where(
                            schedules.c.company_id == company_id,
                            schedules.c.id == schedule_id,
                            schedules.c.enabled.is_(True),
                            schedules.c.archived_at.is_(None),
                            schedules.c.next_run_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return 0  # outro despachante já está nela, ou já foi tratada
            try:
                cron, zone = CronExpression.parse(row["cron"]), load_zone(row["timezone"])
            except CronError:
                await self._disable(session, row, "agenda_invalida")
                return 0
            first: datetime = row["next_run_at"]
            due = [first, *cron.occurrences(first, now, zone, limit=_MAX_DUE - 1)]
            truncated = len(due) >= _MAX_DUE
            recent = [at for at in due if now - at <= self.catch_up]
            skipped: list[tuple[datetime, str]] = [
                (at, SkipReason.MISSED) for at in due if now - at > self.catch_up
            ]
            skipped += [(at, SkipReason.SUPERSEDED) for at in recent[:-1]]
            created = 0
            if recent:
                chosen = recent[-1]
                reason = await self._enqueue(session, row, chosen)
                if reason is None:
                    created = 1
                elif reason != _ALREADY_HANDLED:
                    skipped.append((chosen, reason))
            await self._record_skipped(session, row, skipped)
            following = cron.next_after(due[-1] if truncated else max(now, due[-1]), zone)
            values: dict[str, Any] = {"next_run_at": following, "last_occurrence_at": due[-1]}
            if following is None:
                values["enabled"] = False
            await session.execute(
                update(schedules)
                .where(schedules.c.company_id == company_id, schedules.c.id == schedule_id)
                .values(**values)
            )
            return created

    async def _enqueue(self, session: AsyncSession, row: RowMapping, at: datetime) -> str | None:
        """Põe a ocorrência na fila; devolve o motivo se ela foi ignorada."""
        company_id: UUID = row["company_id"]
        schedule_id: UUID = row["id"]
        handled = await session.scalar(
            select(func.count())
            .select_from(schedule_occurrences)
            .where(
                schedule_occurrences.c.company_id == company_id,
                schedule_occurrences.c.schedule_id == schedule_id,
                schedule_occurrences.c.scheduled_for == at,
            )
        )
        if handled:
            return _ALREADY_HANDLED
        robot = await _published(session, company_id, row["robot_id"])
        if robot is None:
            return SkipReason.ROBOT_UNAVAILABLE
        inputs = dict(row["inputs"])
        if validate_inputs(Manifest.model_validate(robot["manifest"]), inputs):
            return SkipReason.INVALID_INPUTS
        running = await session.scalar(
            select(func.count())
            .select_from(
                schedule_occurrences.join(
                    runs,
                    and_(
                        runs.c.company_id == schedule_occurrences.c.company_id,
                        runs.c.id == schedule_occurrences.c.run_id,
                    ),
                )
            )
            .where(
                schedule_occurrences.c.company_id == company_id,
                schedule_occurrences.c.schedule_id == schedule_id,
                runs.c.state.in_(_UNFINISHED),
            )
        )
        if running:
            return SkipReason.PREVIOUS_RUNNING
        try:
            queued = await enqueue_run(
                session,
                company_id=company_id,
                requested_by=f"agenda:{schedule_id}",
                robot=robot,
                slug=robot["slug"],
                inputs=inputs,
                trigger=RunTrigger.SCHEDULE,
                idempotency_key=f"{schedule_id}:{at.isoformat()}",
                scope=SCHEDULE_SCOPE,
                audit_extra={"agenda": str(schedule_id), "ocorrencia": at.isoformat()},
            )
        except QuotaExceeded:
            return SkipReason.QUEUE_FULL
        # A chave primária (agenda, horário) garante: uma ocorrência, uma execução.
        await session.execute(
            insert(schedule_occurrences).values(
                company_id=company_id,
                schedule_id=schedule_id,
                scheduled_for=at,
                status=ScheduleOccurrenceStatus.ENQUEUED.value,
                run_id=queued.run_id,
            )
        )
        return None

    @staticmethod
    async def _record_skipped(
        session: AsyncSession, row: RowMapping, skipped: list[tuple[datetime, str]]
    ) -> None:
        if not skipped:
            return
        await session.execute(
            pg_insert(schedule_occurrences)
            .values(
                [
                    {
                        "company_id": row["company_id"],
                        "schedule_id": row["id"],
                        "scheduled_for": at,
                        "status": ScheduleOccurrenceStatus.SKIPPED.value,
                        "reason": reason,
                    }
                    for at, reason in skipped
                ]
            )
            .on_conflict_do_nothing()
        )
        reasons = Counter(reason for _, reason in skipped)
        moments = sorted(at for at, _ in skipped)
        await audit.append(
            session,
            company_id=row["company_id"],
            actor=SCHEDULE_ACTOR,
            action="agenda.ocorrencias_ignoradas",
            target=f"agenda:{row['id']}",
            payload={
                "motivos": dict(sorted(reasons.items())),
                "primeira": moments[0].isoformat(),
                "ultima": moments[-1].isoformat(),
            },
        )

    @staticmethod
    async def _disable(session: AsyncSession, row: RowMapping, reason: str) -> None:
        await session.execute(
            update(schedules)
            .where(schedules.c.company_id == row["company_id"], schedules.c.id == row["id"])
            .values(enabled=False, next_run_at=None)
        )
        await audit.append(
            session,
            company_id=row["company_id"],
            actor=SCHEDULE_ACTOR,
            action="agenda.desligada",
            target=f"agenda:{row['id']}",
            payload={"motivo": reason},
        )
