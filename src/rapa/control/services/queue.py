"""Fila de execuções: a tabela ``runs`` é a fila.

Funções para o despachante, todas recebendo uma sessão sem empresa (papel
``rapa_dispatcher``) e rodando dentro da transação dela:

- :func:`claim_next` pega a próxima execução numa transação curta, com uma trava
  consultiva global: lê a capacidade, conta o que está rodando, aplica as regras
  de :mod:`rapa.control.services.admission` e marca a escolhida como
  ``running`` com ``attempt + 1`` (o token de cerca) e um prazo de concessão;
- :func:`heartbeat` renova a concessão da tentativa atual;
- :func:`ingest_events` grava eventos do executor, idempotente por
  ``(run_id, seq)``;
- :func:`finish` grava o resultado do executor;
- :func:`reap_expired` recolhe concessões vencidas: volta para a fila o que não
  começou nenhum passo irreversível e marca o resto como indeterminado.

Escritas de uma tentativa antiga (``attempt`` diferente do atual, ou execução
já devolvida à fila) são recusadas com :class:`StaleAttempt`.

A numeração dos eventos é da execução, não da tentativa: :func:`claim_next`
entrega no trabalho o ``first_seq`` (o número seguinte ao último evento
gravado), e o executor numera a partir dele. Assim ``(run_id, seq)`` continua
único e crescente entre as tentativas, e um evento só é dado como gravado
quando está gravado para esta tentativa, com o mesmo conteúdo.

:class:`PgRunQueue` embrulha cada função na sua própria transação.
"""

import json
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    RowMapping,
    Text,
    and_,
    case,
    cast,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    company_quotas,
    platform_settings,
    robot_versions,
    run_events,
    runs,
)
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import INTERACTIVE_PRIORITY, now_utc
from rapa.control.services.admission import Capacity, CompanyLoad, plan
from rapa.core.contracts import (
    Conflict,
    InvalidInput,
    NotFound,
    RunMode,
    RunnerJob,
    RunState,
)
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Manifest

__all__ = [
    "DEFAULT_LEASE",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DURATION",
    "DISPATCHER_ACTOR",
    "QUEUE_LOCK_KEY",
    "ArtifactInfo",
    "EventConflict",
    "PgRunQueue",
    "ReapReport",
    "RunnerOutcome",
    "StaleAttempt",
    "claim_next",
    "ensure_event_partitions",
    "finish",
    "heartbeat",
    "ingest_events",
    "reap_expired",
]

#: Chave da trava consultiva que serializa as admissões (um único despachante
#: decide por vez, em transações curtas).
QUEUE_LOCK_KEY = 7_202_611_180
DEFAULT_LEASE = timedelta(seconds=60)
DEFAULT_MAX_DURATION = timedelta(minutes=30)
#: Quantas vezes uma execução pode perder a concessão antes de falhar de vez.
DEFAULT_MAX_ATTEMPTS = 3
DISPATCHER_ACTOR = "despachante"

_ACTIVE = (RunState.RUNNING.value, RunState.AWAITING_HUMAN.value)
_UNFINISHED = (RunState.QUEUED.value, *_ACTIVE)
_MAX_ERROR_MESSAGE = 2000
_MAX_INVALID_SKIPS = 20
_STEP_ACTIVITY = frozenset(
    {RunEventKind.STEP_STARTED, RunEventKind.STEP_SUCCEEDED, RunEventKind.STEP_HEALED}
)


class StaleAttempt(Conflict):
    """Escrita de uma tentativa que não é mais a atual (cerca)."""

    code = "tentativa_antiga"


class EventConflict(StaleAttempt):
    """Evento que não pode ser gravado para esta tentativa.

    Acontece quando o número do evento repete ou fica abaixo da numeração das
    tentativas anteriores, ou repete um evento desta tentativa com outro
    conteúdo. Nada do lote é gravado. Como :class:`StaleAttempt`, quem repassa
    os eventos deve parar o executor sem confirmar nenhum aviso: confirmar um
    aviso que não está gravado deixaria o passo irreversível rodar sem registro.
    """

    code = "evento_conflitante"


class ArtifactInfo(BaseModel):
    """Arquivo guardado pela execução (mesma forma do RESULT.json do executor)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=4000)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunnerOutcome(BaseModel):
    """Resultado de uma tentativa, no formato do RESULT.json (interface A)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    run_id: UUID
    attempt: int = Field(ge=1)
    state: RunState
    outputs: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = None
    failed_step_id: str | None = Field(default=None, max_length=100)
    irreversible_started: bool = False
    steps_done: int = Field(default=0, ge=0)
    artifacts: list[ArtifactInfo] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReapReport:
    """O que :func:`reap_expired` fez com as concessões vencidas."""

    requeued: list[UUID] = field(default_factory=list)
    indeterminate: list[UUID] = field(default_factory=list)
    failed: list[UUID] = field(default_factory=list)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _validation_errors(exc: ValidationError) -> list[str]:
    """Campos inválidos, sem repetir os valores recebidos."""
    return [
        f"{'.'.join(str(part) for part in error['loc']) or 'corpo'}: valor inválido"
        for error in exc.errors()
    ]


# ---------------------------------------------------------------------------
# Admissão
# ---------------------------------------------------------------------------


async def _capacity(session: AsyncSession) -> Capacity | None:
    row = (
        await session.execute(
            select(
                platform_settings.c.total_browser_slots,
                platform_settings.c.interactive_reserve_ratio,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Capacity(int(row[0]), Decimal(row[1]))


async def _loads(
    session: AsyncSession,
) -> tuple[dict[UUID, CompanyLoad], int, frozenset[UUID]]:
    """Uso não interativo por empresa, interativos rodando e empresas com interativo na fila."""
    interactive = (runs.c.priority == INTERACTIVE_PRIORITY).label("interactive")
    queued = (runs.c.state == RunState.QUEUED.value).label("queued")
    rows = (
        await session.execute(
            select(runs.c.company_id, interactive, queued, func.count())
            .where(runs.c.state.in_(_UNFINISHED))
            .group_by(runs.c.company_id, interactive, queued)
        )
    ).all()
    running: dict[UUID, int] = defaultdict(int)
    waiting: dict[UUID, int] = defaultdict(int)
    interactive_running = 0
    interactive_waiting: set[UUID] = set()
    for company_id, is_interactive, is_queued, count in rows:
        if is_interactive:
            if is_queued:
                interactive_waiting.add(company_id)
            else:
                interactive_running += int(count)
        elif is_queued:
            waiting[company_id] += int(count)
        else:
            running[company_id] += int(count)
    companies = set(running) | set(waiting)
    quotas: dict[UUID, tuple[int, Decimal]] = {}
    if companies:
        for company_id, guaranteed, factor in (
            await session.execute(
                select(
                    company_quotas.c.company_id,
                    company_quotas.c.guaranteed_slots,
                    company_quotas.c.max_borrow_factor,
                ).where(company_quotas.c.company_id.in_(companies))
            )
        ).all():
            quotas[company_id] = (int(guaranteed), Decimal(factor))
    loads: dict[UUID, CompanyLoad] = {}
    for company_id in companies:
        # Empresa sem cota cadastrada não recebe vagas não interativas (falha fechada).
        guaranteed, factor = quotas.get(company_id, (0, Decimal(1)))
        loads[company_id] = CompanyLoad(
            running=running[company_id],
            queued=waiting[company_id],
            guaranteed_slots=guaranteed,
            max_borrow_factor=factor,
        )
    return loads, interactive_running, frozenset(interactive_waiting)


async def claim_next(
    session: AsyncSession,
    *,
    now: datetime,
    lease: timedelta = DEFAULT_LEASE,
    max_duration: timedelta = DEFAULT_MAX_DURATION,
) -> RunnerJob | None:
    """Pega a próxima execução admissível e a marca como ``running``.

    Deve rodar numa transação curta e própria (ver :class:`PgRunQueue`): a
    trava consultiva só é solta no fim da transação.
    """
    now = _utc(now)
    await session.execute(select(func.pg_advisory_xact_lock(QUEUE_LOCK_KEY)))
    capacity = await _capacity(session)
    if capacity is None:
        return None
    loads, interactive_running, interactive_waiting = await _loads(session)
    admission = plan(capacity, loads, interactive_running, interactive_waiting)
    conditions = []
    interactive = runs.c.priority == INTERACTIVE_PRIORITY
    if admission.interactive and interactive_waiting:
        conditions.append(interactive)
    elif admission.interactive_overflow:
        # Reserva cheia: só o interativo das empresas que não tomam a garantia de outra.
        conditions.append(and_(interactive, runs.c.company_id.in_(admission.interactive_overflow)))
    eligible = admission.within_guarantee | admission.borrowing
    if eligible:
        conditions.append(
            and_(runs.c.priority > INTERACTIVE_PRIORITY, runs.c.company_id.in_(eligible))
        )
    if not conditions:
        return None
    # Na mesma prioridade, quem está dentro da garantia vem antes de quem pede emprestado.
    borrowing_last = (
        case(
            (
                and_(
                    runs.c.priority > INTERACTIVE_PRIORITY,
                    runs.c.company_id.in_(admission.borrowing),
                ),
                1,
            ),
            else_=0,
        )
        if admission.borrowing
        else literal(0)
    )
    candidates = (
        select(runs.c.id, runs.c.company_id, runs.c.version_id, runs.c.inputs, runs.c.mode)
        .where(runs.c.state == RunState.QUEUED.value, or_(*conditions))
        .order_by(runs.c.priority, borrowing_last, runs.c.queued_at, runs.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    for _ in range(_MAX_INVALID_SKIPS):
        candidate = (await session.execute(candidates)).mappings().one_or_none()
        if candidate is None:
            return None
        manifest_json = await session.scalar(
            select(robot_versions.c.manifest).where(robot_versions.c.id == candidate["version_id"])
        )
        try:
            manifest = Manifest.model_validate(manifest_json)
        except ValidationError:
            await _fail_unrunnable(session, candidate["id"], candidate["company_id"], now)
            continue
        deadline = now + max_duration
        attempt = await session.scalar(
            update(runs)
            .where(runs.c.id == candidate["id"])
            .values(
                state=RunState.RUNNING.value,
                attempt=runs.c.attempt + 1,
                started_at=now,
                lease_expires_at=now + lease,
                deadline_at=deadline,
                # A tentativa nova recomeça do primeiro passo: os passos contados
                # são só os dela, não se somam aos de uma tentativa perdida.
                steps_done=0,
            )
            .returning(runs.c.attempt)
        )
        # A tentativa nova continua a numeração dos eventos da execução. O valor é
        # definitivo: a cerca já recusa eventos das tentativas anteriores.
        first_seq = await session.scalar(
            select(func.coalesce(func.max(run_events.c.seq), -1) + 1).where(
                run_events.c.run_id == candidate["id"]
            )
        )
        return RunnerJob(
            run_id=candidate["id"],
            company_id=candidate["company_id"],
            attempt=int(attempt),
            manifest=manifest,
            inputs=dict(candidate["inputs"]),
            mode=RunMode(candidate["mode"]),
            deadline=deadline,
            first_seq=int(first_seq or 0),
        )
    return None


async def _fail_unrunnable(
    session: AsyncSession, run_id: UUID, company_id: UUID, now: datetime
) -> None:
    await session.execute(
        update(runs)
        .where(runs.c.id == run_id)
        .values(
            state=RunState.FAILED.value,
            finished_at=now,
            error_code="manifesto_invalido",
            error_message="A versão do robô não pôde ser lida por esta versão da plataforma.",
        )
    )
    await audit.append(
        session,
        company_id=company_id,
        actor=DISPATCHER_ACTOR,
        action="execucao.falhou",
        target=f"execucao:{run_id}",
        payload={"erro": "manifesto_invalido"},
    )


# ---------------------------------------------------------------------------
# Concessão, eventos e resultado
# ---------------------------------------------------------------------------


async def heartbeat(
    session: AsyncSession,
    run_id: UUID,
    attempt: int,
    *,
    now: datetime,
    lease: timedelta = DEFAULT_LEASE,
) -> bool:
    """Renova a concessão; ``False`` se a tentativa não é mais a atual."""
    result = await session.execute(
        update(runs)
        .where(runs.c.id == run_id, runs.c.attempt == attempt, runs.c.state.in_(_ACTIVE))
        .values(lease_expires_at=_utc(now) + lease)
        .returning(runs.c.id)
    )
    return result.first() is not None


async def _lock_run(session: AsyncSession, run_id: UUID) -> RowMapping:
    row = (
        (
            await session.execute(
                select(
                    runs.c.id,
                    runs.c.company_id,
                    runs.c.attempt,
                    runs.c.state,
                    runs.c.irreversible_started_at,
                )
                .where(runs.c.id == run_id)
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("execução não encontrada")
    return row


def _fence(run: RowMapping, attempt: int) -> None:
    if run["attempt"] != attempt or run["state"] == RunState.QUEUED.value:
        raise StaleAttempt(
            f"a tentativa {attempt} não é mais a atual desta execução; a escrita foi recusada"
        )


def _marks_irreversible(event: RunEvent) -> bool:
    """Se o evento mostra que um passo irreversível começou.

    Vale só o aviso (``"irreversivel": true``), o mesmo que o executor espera
    ver confirmado antes de agir. O interpretador também anuncia, sem o aviso, o
    passo irreversível que falhou antes de agir (por exemplo, botão não
    encontrado): nada foi enviado ao portal, então isso não marca a execução.
    """
    if event.kind is RunEventKind.STEP_STARTED:
        return event.data.get("irreversivel") is True
    if event.kind in (RunEventKind.RUN_SUCCEEDED, RunEventKind.RUN_FAILED):
        return event.data.get("irreversivel_iniciado") is True
    return False


def _event_data(event: RunEvent) -> Any:
    return event.model_dump(mode="json")["data"]


def _same_event(a: RunEvent, b: RunEvent) -> bool:
    return (a.kind, a.step_id, _event_json(a)) == (b.kind, b.step_id, _event_json(b))


def _canonical_number(value: Decimal) -> str:
    """O número pelo valor exato: dígitos sem zeros nas pontas e o expoente."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # NaN ou infinito: o JSONB não os aceita
        return str(value)
    significant = "".join(map(str, digits)).lstrip("0")
    if not significant:
        return "0"  # o JSONB não guarda o sinal do zero
    stripped = significant.rstrip("0")
    exponent += len(significant) - len(stripped)
    return f"{'-' if sign else ''}{stripped}e{exponent}"


def _canonical(value: Any) -> str:
    """Texto canônico de um valor JSON lido com os números em ``Decimal``.

    Chaves em ordem e cada número escrito pelo valor exato. Dois valores têm o
    mesmo texto quando o JSONB os guardaria iguais: ``1e23`` e
    ``100000000000000000000000`` são o mesmo número; ``true`` e ``1`` não.
    """
    if isinstance(value, dict):
        items = sorted(value.items())
        return "{" + ",".join(f"{json.dumps(k)}:{_canonical(v)}" for k, v in items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return _canonical_number(value)
    return json.dumps(value)  # texto, booleano ou nulo


def _canonical_json(raw: str) -> str:
    """Texto canônico do documento JSON ``raw`` (ver :func:`_canonical`)."""
    return _canonical(json.loads(raw, parse_float=Decimal, parse_int=Decimal))


def _event_json(event: RunEvent) -> str:
    """Texto canônico dos dados do evento, como o JSONB os guardaria."""
    return _canonical_json(json.dumps(_event_data(event)))


def _parse_events(
    run_id: UUID, attempt: int, events: Iterable[RunEvent | Mapping[str, Any]]
) -> list[RunEvent]:
    parsed: list[RunEvent] = []
    try:
        for event in events:
            parsed.append(event if isinstance(event, RunEvent) else RunEvent.model_validate(event))
    except ValidationError as exc:
        raise InvalidInput("evento de execução inválido", _validation_errors(exc)) from None
    for event in parsed:
        if event.run_id != run_id or event.attempt != attempt:
            raise InvalidInput("o evento é de outra execução ou de outra tentativa")
    return parsed


async def ingest_events(
    session: AsyncSession,
    run_id: UUID,
    attempt: int,
    events: Iterable[RunEvent | Mapping[str, Any]],
) -> int:
    """Grava os eventos novos da tentativa atual; devolve quantos foram incluídos.

    Ao voltar sem erro, todos os eventos recebidos estão gravados para esta
    tentativa: é isso que permite ao lançador confirmar o aviso de passo
    irreversível. Reenviar um evento já gravado (mesmo ``seq``, mesmo conteúdo,
    comparado pelo valor do JSON como o JSONB o guarda) não faz nada. Um
    ``seq`` que repete com outro conteúdo, ou que fica dentro
    da numeração das tentativas anteriores, levanta :class:`EventConflict`, e
    eventos de uma execução que já terminou (por exemplo, encerrada pelo
    recolhedor) levantam :class:`StaleAttempt`; nos dois casos nada é gravado.

    O aviso de passo irreversível marca ``irreversible_started_at``, o que
    impede a execução de voltar para a fila.
    """
    parsed = _parse_events(run_id, attempt, events)
    run = await _lock_run(session, run_id)
    _fence(run, attempt)
    if run["state"] not in _ACTIVE:
        # Concluída (pelo resultado ou pelo recolhedor) com o executor ainda vivo:
        # nada mais é gravado nem confirmado, e o lançador para o executor.
        raise StaleAttempt(
            f"a execução já foi concluída; os eventos da tentativa {attempt} foram recusados"
        )
    if not parsed:
        return 0
    by_seq: dict[int, RunEvent] = {}
    for event in parsed:
        first = by_seq.setdefault(event.seq, event)
        if first is not event and not _same_event(first, event):
            raise EventConflict(
                f"o lote repete o evento número {event.seq} com outro conteúdo; nada foi gravado"
            )
    earlier_max, previous_max = (
        await session.execute(
            select(
                func.max(run_events.c.seq).filter(run_events.c.attempt < attempt),
                func.max(run_events.c.seq),
            ).where(run_events.c.run_id == run_id)
        )
    ).one()
    lowest = min(by_seq)
    if earlier_max is not None and lowest <= earlier_max:
        raise EventConflict(
            f"o evento número {lowest} da tentativa {attempt} está dentro da numeração das "
            f"tentativas anteriores (até {earlier_max}); nada foi gravado"
        )
    # Os dados gravados vêm como o texto do JSONB, que reescreve os números pelo
    # valor (``1e23`` volta como ``100000000000000000000000``): os dois lados são
    # comparados na forma canônica, nunca como objetos Python.
    stored = (
        await session.execute(
            select(
                run_events.c.seq,
                run_events.c.attempt,
                run_events.c.kind,
                run_events.c.step_id,
                cast(run_events.c.data, Text),
            ).where(run_events.c.run_id == run_id, run_events.c.seq.in_(list(by_seq)))
        )
    ).all()
    for seq, stored_attempt, kind, step_id, data in stored:
        incoming = by_seq[seq]
        if (stored_attempt, kind, step_id, _canonical_json(data)) != (
            attempt,
            incoming.kind.value,
            incoming.step_id,
            _event_json(incoming),
        ):
            raise EventConflict(
                f"o evento número {seq} já foi gravado com outro conteúdo; nada foi gravado"
            )
    known = {row[0] for row in stored}
    new = [event for seq, event in sorted(by_seq.items()) if seq not in known]
    if not new:
        return 0
    await session.execute(
        insert(run_events),
        [
            {
                "company_id": run["company_id"],
                "run_id": run_id,
                "attempt": attempt,
                "seq": event.seq,
                "at": _utc(event.at),
                "kind": event.kind.value,
                "step_id": event.step_id,
                "data": _event_data(event),
            }
            for event in new
        ],
    )
    values: dict[str, Any] = {}
    succeeded = sum(1 for event in new if event.kind is RunEventKind.STEP_SUCCEEDED)
    if succeeded:
        values["steps_done"] = runs.c.steps_done + succeeded
    if run["irreversible_started_at"] is None:
        marks = [_utc(e.at) for e in new if _marks_irreversible(e)]
        if marks:
            values["irreversible_started_at"] = min(marks)
    state = run["state"]
    for event in new:
        # Evento que chega atrasado (abaixo do último gravado) não muda o estado.
        if previous_max is not None and event.seq <= previous_max:
            continue
        if event.kind is RunEventKind.HUMAN_NEEDED:
            state = RunState.AWAITING_HUMAN.value
        elif event.kind in _STEP_ACTIVITY:
            state = RunState.RUNNING.value
    if state != run["state"]:
        values["state"] = state
    if values:
        await session.execute(update(runs).where(runs.c.id == run_id).values(**values))
    return len(new)


def _final_state(reported: RunState, irreversible: bool) -> RunState:
    if reported is RunState.SUCCEEDED:
        return RunState.SUCCEEDED
    if reported is RunState.INDETERMINATE or irreversible:
        # Nada que gravou é repetido: depois de um passo irreversível, uma
        # falha (ou um cancelamento) fica para uma pessoa decidir.
        return RunState.INDETERMINATE
    if reported in (RunState.FAILED, RunState.CANCELLED):
        return reported
    return RunState.FAILED


async def finish(
    session: AsyncSession,
    run_id: UUID,
    attempt: int,
    result: RunnerOutcome | Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    """Grava o resultado da tentativa atual.

    Devolve ``False`` (sem mudar nada) se a execução já estava concluída, por
    exemplo quando o recolhedor a marcou como indeterminada antes do resultado
    chegar. Um resultado de tentativa antiga levanta :class:`StaleAttempt`.
    """
    try:
        outcome = (
            result if isinstance(result, RunnerOutcome) else RunnerOutcome.model_validate(result)
        )
    except ValidationError as exc:
        raise InvalidInput("resultado do executor inválido", _validation_errors(exc)) from None
    if outcome.run_id != run_id or outcome.attempt != attempt:
        raise InvalidInput("o resultado é de outra execução ou de outra tentativa")
    now = _utc(now)
    run = await _lock_run(session, run_id)
    _fence(run, attempt)
    if RunState(run["state"]).finished:
        return False
    irreversible = outcome.irreversible_started or run["irreversible_started_at"] is not None
    state = _final_state(outcome.state, irreversible)
    error_code = outcome.error_code
    error_message = outcome.error_message
    if not outcome.state.finished:
        error_code = error_code or "resultado_invalido"
        error_message = error_message or "O executor terminou sem informar um estado final."
    if state is RunState.INDETERMINATE and outcome.state is not RunState.INDETERMINATE:
        error_code = error_code or "indeterminado"
        error_message = error_message or (
            "A execução parou depois de começar um passo irreversível; "
            "confira o sistema de destino antes de repetir."
        )
    values: dict[str, Any] = {
        "state": state.value,
        "outputs": outcome.outputs,
        "error_code": error_code,
        "error_message": error_message[:_MAX_ERROR_MESSAGE] if error_message else None,
        "failed_step_id": outcome.failed_step_id,
        "steps_done": func.greatest(runs.c.steps_done, outcome.steps_done),
        "artifacts": [artifact.model_dump(mode="json") for artifact in outcome.artifacts],
        "finished_at": now,
        "lease_expires_at": None,
    }
    if run["irreversible_started_at"] is None and outcome.irreversible_started:
        values["irreversible_started_at"] = now
    await session.execute(update(runs).where(runs.c.id == run_id).values(**values))
    await audit.append(
        session,
        company_id=run["company_id"],
        actor=DISPATCHER_ACTOR,
        action="execucao.concluida",
        target=f"execucao:{run_id}",
        payload={"estado": state.value, "tentativa": attempt, "erro": error_code},
    )
    return True


async def reap_expired(
    session: AsyncSession,
    *,
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    skip: Collection[UUID] = (),
) -> ReapReport:
    """Recolhe execuções cuja concessão venceu (executor sumiu).

    Sem passo irreversível iniciado, a execução volta para a fila (até
    ``max_attempts`` tentativas; depois disso, falha). Com passo irreversível
    iniciado, vira indeterminada: uma pessoa confere o sistema de destino.

    As execuções em ``skip`` ficam de fora: são as que o próprio despachante
    ainda acompanha (por exemplo, gravando o resultado depois de uma queda do
    banco que deixou a concessão vencer).
    """
    now = _utc(now)
    conditions = [runs.c.state.in_(_ACTIVE), runs.c.lease_expires_at < now]
    if skip:
        conditions.append(runs.c.id.not_in(list(skip)))
    expired = (
        (
            await session.execute(
                select(runs.c.id, runs.c.company_id, runs.c.attempt, runs.c.irreversible_started_at)
                .where(*conditions)
                .order_by(runs.c.lease_expires_at)
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .all()
    )
    report = ReapReport()
    for row in expired:
        run_id: UUID = row["id"]
        if row["irreversible_started_at"] is not None:
            values: dict[str, Any] = {
                "state": RunState.INDETERMINATE.value,
                "finished_at": now,
                "lease_expires_at": None,
                "error_code": "executor_sem_resposta",
                "error_message": (
                    "O executor parou de responder depois de começar um passo irreversível; "
                    "confira o sistema de destino antes de repetir."
                ),
            }
            action = "execucao.indeterminada"
            report.indeterminate.append(run_id)
        elif row["attempt"] >= max_attempts:
            values = {
                "state": RunState.FAILED.value,
                "finished_at": now,
                "lease_expires_at": None,
                "error_code": "tentativas_esgotadas",
                "error_message": (
                    f"O executor parou de responder em {row['attempt']} tentativas seguidas."
                ),
            }
            action = "execucao.falhou"
            report.failed.append(run_id)
        else:
            values = {
                "state": RunState.QUEUED.value,
                "lease_expires_at": None,
                "started_at": None,
                "deadline_at": None,
            }
            action = "execucao.reenfileirada"
            report.requeued.append(run_id)
        await session.execute(update(runs).where(runs.c.id == run_id).values(**values))
        await audit.append(
            session,
            company_id=row["company_id"],
            actor=DISPATCHER_ACTOR,
            action=action,
            target=f"execucao:{run_id}",
            payload={"tentativa": row["attempt"], "estado": values["state"]},
        )
    return report


async def ensure_event_partitions(session: AsyncSession, months_ahead: int = 2) -> int:
    """Cria as partições mensais de eventos que faltam; devolve quantas criou."""
    created = await session.scalar(
        text("SELECT rapa_ensure_event_partitions(:months)"), {"months": months_ahead}
    )
    return int(created or 0)


# ---------------------------------------------------------------------------
# Fachada com transações próprias
# ---------------------------------------------------------------------------


class PgRunQueue:
    """As operações da fila, cada uma na sua transação (papel do despachante)."""

    def __init__(
        self,
        db: Database,
        *,
        lease: timedelta = DEFAULT_LEASE,
        max_duration: timedelta = DEFAULT_MAX_DURATION,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.db = db
        self.lease = lease
        self.max_duration = max_duration
        self.max_attempts = max_attempts
        self.clock = clock

    async def claim_next(self, *, now: datetime | None = None) -> RunnerJob | None:
        async with self.db.dispatcher_session() as session:
            return await claim_next(
                session,
                now=now or self.clock(),
                lease=self.lease,
                max_duration=self.max_duration,
            )

    async def heartbeat(self, run_id: UUID, attempt: int, *, now: datetime | None = None) -> bool:
        async with self.db.dispatcher_session() as session:
            return await heartbeat(
                session, run_id, attempt, now=now or self.clock(), lease=self.lease
            )

    async def ingest_events(
        self, run_id: UUID, attempt: int, events: Iterable[RunEvent | Mapping[str, Any]]
    ) -> int:
        async with self.db.dispatcher_session() as session:
            return await ingest_events(session, run_id, attempt, events)

    async def finish(
        self,
        run_id: UUID,
        attempt: int,
        result: RunnerOutcome | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        async with self.db.dispatcher_session() as session:
            return await finish(session, run_id, attempt, result, now=now or self.clock())

    async def reap_expired(
        self, *, now: datetime | None = None, skip: Collection[UUID] = ()
    ) -> ReapReport:
        async with self.db.dispatcher_session() as session:
            return await reap_expired(
                session, now=now or self.clock(), max_attempts=self.max_attempts, skip=skip
            )

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        async with self.db.dispatcher_session() as session:
            return await ensure_event_partitions(session, months_ahead)
