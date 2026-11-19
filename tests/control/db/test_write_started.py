"""Nada que gravou volta para a fila.

Uma execução que começou um passo que grava (o evento ``step_started`` com
``"grava": true``, ou o aviso de passo irreversível) e perde o executor nunca
recomeça do primeiro passo: depois de um passo irreversível ela fica
indeterminada, e depois de um passo que grava ela falha, pedindo para conferir o
sistema de destino. Só o que não gravou nada volta para a fila.

Os cenários com o executor de verdade usam o despachante, o lançador local, o
worker, o Chromium e o PostgreSQL; a queda é simulada por um repasse de eventos
que para de responder, como um despachante que caiu.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher, StaticSecretSource
from rapa.control.services import Dispatcher, PgPlatform, PgRunQueue, StaleAttempt, verify_chain
from rapa.control.services.audit import payload_digest
from rapa.core.contracts import RunState, RunTrigger
from rapa.core.events import RunEvent, RunEventKind
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from tests.runner.support import SECRETS
from tests.runner.test_worker import _upload_manifest

from .support import (
    PROTOCOLO_INPUTS,
    Tenant,
    event,
    make_tenant,
    notas,
    protocolo,
    publish,
    reset_queue,
    result,
)

pytestmark = pytest.mark.database

_WRITE_CODE = "executor_parou_depois_de_gravar"


def _write_start(run_id: UUID, attempt: int, seq: int, step_id: str = "enviar") -> RunEvent:
    """Início de um passo que grava, como o interpretador o emite."""
    return event(
        run_id,
        attempt,
        seq,
        RunEventKind.STEP_STARTED,
        step_id,
        tipo="click",
        titulo="Enviar",
        efeito="write",
        grava=True,
    )


def _warning(run_id: UUID, attempt: int, seq: int) -> RunEvent:
    """Aviso de início do passo irreversível, como o interpretador o emite."""
    return event(
        run_id,
        attempt,
        seq,
        RunEventKind.STEP_STARTED,
        "emitir",
        tipo="click",
        titulo="Emitir protocolo",
        efeito="irreversible",
        irreversivel=True,
    )


def _reading(run_id: UUID, attempt: int, first: int) -> list[RunEvent]:
    return [
        event(run_id, attempt, first, RunEventKind.RUN_STARTED),
        event(run_id, attempt, first + 1, RunEventKind.STEP_STARTED, "abrir", efeito="read"),
        event(run_id, attempt, first + 2, RunEventKind.STEP_SUCCEEDED, "abrir"),
    ]


async def _row(db: Database, run_id: UUID) -> RowMapping:
    async with db.dispatcher_session() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT state, attempt, error_code, error_message, write_started_at, "
                        "irreversible_started_at, finished_at, lease_expires_at "
                        "FROM runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
            )
            .mappings()
            .one()
        )


async def _audit(db: Database, company_id: UUID, run_id: UUID) -> list[tuple[str, str]]:
    """Ações da auditoria sobre a execução, com o resumo (hash) dos dados de cada uma."""
    async with db.dispatcher_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action, payload_digest FROM audit_log "
                    "WHERE company_id = :c AND target = :t ORDER BY seq"
                ),
                {"c": company_id, "t": f"execucao:{run_id}"},
            )
        ).all()
        chain = await verify_chain(session, company_id)
    assert chain.ok, chain.problems
    return [(row[0], row[1]) for row in rows]


async def _tenant(platform: PgPlatform, app_db: Database, name: str) -> Tenant:
    tenant = await make_tenant(app_db, name)
    await publish(platform, tenant, protocolo())
    await publish(platform, tenant, notas())
    return tenant


async def _start(platform: PgPlatform, tenant: Tenant) -> UUID:
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    return run.run_id


# ---------------------------------------------------------------------------
# Recolhedor: só volta para a fila o que não gravou nada
# ---------------------------------------------------------------------------


async def test_reaper_requeues_only_what_wrote_nothing(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant(platform, app_db, "Recolhe só leitura")
    reading, writing, irreversible = [await _start(platform, tenant) for _ in range(3)]
    now = datetime.now(UTC)
    for _ in range(3):
        assert await queue.claim_next(now=now) is not None

    # Leitura: o executor sumiu depois de um passo que só lê.
    await queue.ingest_events(reading, 1, _reading(reading, 1, 0))
    # Gravação: o início do passo que grava foi gravado (e confirmado), e ele agiu.
    await queue.ingest_events(
        writing,
        1,
        [
            *_reading(writing, 1, 0),
            _write_start(writing, 1, 3),
            event(writing, 1, 4, RunEventKind.STEP_SUCCEEDED, "enviar"),
        ],
    )
    # Irreversível: o aviso foi gravado.
    await queue.ingest_events(
        irreversible, 1, [*_reading(irreversible, 1, 0), _warning(irreversible, 1, 3)]
    )

    report = await queue.reap_expired(now=now + timedelta(seconds=11))

    assert report.requeued == [reading]
    assert report.failed == [writing]
    assert report.indeterminate == [irreversible]

    wrote = await _row(dispatcher_db, writing)
    assert wrote["state"] == RunState.FAILED.value
    assert wrote["attempt"] == 1  # nunca recomeçou
    assert wrote["error_code"] == _WRITE_CODE
    message = wrote["error_message"]
    assert "passo que grava" in message and "confira o sistema de destino" in message
    assert "não foi repetida" in message
    assert wrote["write_started_at"] is not None and wrote["irreversible_started_at"] is None
    assert wrote["finished_at"] is not None and wrote["lease_expires_at"] is None

    unknown = await _row(dispatcher_db, irreversible)
    assert unknown["state"] == RunState.INDETERMINATE.value
    assert unknown["error_code"] == "executor_sem_resposta"
    assert unknown["write_started_at"] is not None
    assert unknown["irreversible_started_at"] is not None

    back = await _row(dispatcher_db, reading)
    assert back["state"] == RunState.QUEUED.value and back["write_started_at"] is None

    # Só a leitura volta a rodar, e uma vez só.
    job = await queue.claim_next(now=now + timedelta(seconds=12))
    assert job is not None and job.run_id == reading and job.attempt == 2
    assert await queue.claim_next(now=now + timedelta(seconds=12)) is None

    trail = await _audit(dispatcher_db, tenant.company_id, writing)
    assert [action for action, _ in trail] == ["execucao.iniciada", "execucao.falhou"]
    assert trail[-1][1] == payload_digest({"tentativa": 1, "estado": "failed", "erro": _WRITE_CODE})
    assert [a for a, _ in await _audit(dispatcher_db, tenant.company_id, irreversible)] == [
        "execucao.iniciada",
        "execucao.indeterminada",
    ]
    assert [a for a, _ in await _audit(dispatcher_db, tenant.company_id, reading)] == [
        "execucao.iniciada",
        "execucao.reenfileirada",
    ]

    # O executor que sumiu não grava mais nada, e o resultado atrasado não muda a falha.
    with pytest.raises(StaleAttempt):
        await queue.ingest_events(
            writing, 1, [event(writing, 1, 5, RunEventKind.STEP_STARTED, "ler", efeito="read")]
        )
    assert not await queue.finish(writing, 1, result(writing, 1, outputs={"recebido": "ok"}))
    assert (await _row(dispatcher_db, writing))["state"] == RunState.FAILED.value


async def test_a_write_on_the_last_attempt_is_still_reported_as_a_write(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=1), max_attempts=1)
    tenant = await _tenant(platform, app_db, "Última tentativa")
    writing = await _start(platform, tenant)
    now = datetime.now(UTC)
    assert await queue.claim_next(now=now) is not None
    await queue.ingest_events(writing, 1, [_write_start(writing, 1, 0)])
    report = await queue.reap_expired(now=now + timedelta(seconds=2))
    assert report.failed == [writing]
    # A pessoa precisa saber que algo pode ter sido gravado, não só que as tentativas acabaram.
    assert (await _row(dispatcher_db, writing))["error_code"] == _WRITE_CODE


@pytest.mark.parametrize(
    ("data", "marks"),
    [
        ({"efeito": "write", "grava": True}, True),
        # Evento de um executor anterior ao campo "grava": o efeito basta (na dúvida, grava).
        ({"efeito": "write"}, True),
        ({"efeito": "irreversible", "irreversivel": True}, True),
        ({"efeito": "read"}, False),
        # O passo irreversível anunciado sem o aviso falhou antes de agir: nada foi enviado.
        ({"efeito": "irreversible"}, False),
        # Só o booleano vale.
        ({"efeito": "read", "grava": "true"}, False),
    ],
)
async def test_what_marks_the_start_of_a_write(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    data: dict[str, Any],
    marks: bool,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant(platform, app_db, "Marca do passo")
    rid = await _start(platform, tenant)
    now = datetime.now(UTC)
    assert await queue.claim_next(now=now) is not None
    started = event(rid, 1, 1, RunEventKind.STEP_STARTED, "passo", **data)
    await queue.ingest_events(rid, 1, [event(rid, 1, 0, RunEventKind.RUN_STARTED), started])
    row = await _row(dispatcher_db, rid)
    assert (row["write_started_at"] is not None) is marks
    if marks:
        assert row["write_started_at"] == started.at
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    if marks:
        assert report.requeued == [] and rid in report.failed + report.indeterminate
    else:
        assert report.requeued == [rid]


async def test_the_mark_is_the_first_write_and_never_moves(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant(platform, app_db, "Primeira gravação")
    rid = await _start(platform, tenant)
    assert await queue.claim_next() is not None
    first = _write_start(rid, 1, 1, "salvar")
    second = _write_start(rid, 1, 2, "enviar").model_copy(
        update={"at": first.at + timedelta(seconds=5)}
    )
    # Um passo de leitura com "grava" em outro tipo de evento não conta.
    noise = event(rid, 1, 0, RunEventKind.STEP_SUCCEEDED, "ler", grava=True)
    await queue.ingest_events(rid, 1, [noise])
    assert (await _row(dispatcher_db, rid))["write_started_at"] is None
    await queue.ingest_events(rid, 1, [second, first])
    assert (await _row(dispatcher_db, rid))["write_started_at"] == first.at
    later = _write_start(rid, 1, 3, "confirmar")
    await queue.ingest_events(rid, 1, [later, _warning(rid, 1, 4)])
    row = await _row(dispatcher_db, rid)
    assert row["write_started_at"] == first.at
    assert row["irreversible_started_at"] is not None


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("failed", RunState.FAILED),
        ("cancelled", RunState.CANCELLED),
        ("running", RunState.FAILED),
        ("succeeded", RunState.SUCCEEDED),
    ],
)
async def test_finish_after_a_write_is_final(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    reported: str,
    expected: RunState,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant(platform, app_db, f"Fim depois de gravar {reported}")
    rid = await _start(platform, tenant)
    now = datetime.now(UTC)
    assert await queue.claim_next(now=now) is not None
    await queue.ingest_events(rid, 1, [_write_start(rid, 1, 0)])
    marked = (await _row(dispatcher_db, rid))["write_started_at"]
    assert await queue.finish(rid, 1, result(rid, 1, reported), now=now)
    row = await _row(dispatcher_db, rid)
    assert row["state"] == expected.value
    assert row["write_started_at"] == marked
    if expected is not RunState.SUCCEEDED:
        # Quem lê a falha fica sabendo que algo pode ter sido gravado.
        assert row["error_message"].startswith("Um passo que grava no sistema de destino")
        assert "confira o sistema de destino" in row["error_message"]
    # Concluída, nada a recolher e nada de volta na fila.
    report = await queue.reap_expired(now=now + timedelta(minutes=5))
    assert rid not in report.requeued + report.failed + report.indeterminate
    assert await queue.claim_next(now=now + timedelta(minutes=5)) is None


async def test_finish_with_an_irreversible_step_marks_the_write_too(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant(platform, app_db, "Fim irreversível")
    rid = await _start(platform, tenant)
    now = datetime.now(UTC)
    assert await queue.claim_next(now=now) is not None
    assert await queue.finish(rid, 1, result(rid, 1, "failed", irreversible_started=True), now=now)
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.INDETERMINATE.value
    assert row["irreversible_started_at"] == now and row["write_started_at"] == now


async def test_validation_marks_nothing_and_may_return_to_the_queue(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant(platform, app_db, "Validação")
    robot = await platform.get_published(tenant.creator, "emitir_protocolo")
    assert robot is not None
    run = await platform.start_validation(
        tenant.creator, robot.robot_id, robot.version_no, PROTOCOLO_INPUTS
    )
    rid = run.run_id
    now = datetime.now(UTC)
    job = await queue.claim_next(now=now)
    assert job is not None and job.run_id == rid
    # A validação para antes do primeiro passo que grava, sem anunciá-lo.
    await queue.ingest_events(
        rid,
        1,
        [
            *_reading(rid, 1, 0),
            event(
                rid,
                1,
                3,
                RunEventKind.RUN_STOPPED_BEFORE_WRITE,
                "emitir",
                titulo="Emitir",
                efeito="irreversible",
            ),
        ],
    )
    assert (await _row(dispatcher_db, rid))["write_started_at"] is None
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.requeued == [rid]


# ---------------------------------------------------------------------------
# Banco: a regra vale também para quem escreve direto na tabela
# ---------------------------------------------------------------------------


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


async def test_database_never_requeues_a_run_that_wrote(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant(platform, app_db, "Gatilho da gravação")
    rid = await _start(platform, tenant)
    waiting = await _start(platform, tenant)
    job = await queue.claim_next()
    assert job is not None and job.run_id == rid
    await queue.ingest_events(rid, 1, [_write_start(rid, 1, 0)])
    params = {"id": rid}
    for statement in (
        "UPDATE runs SET state = 'queued', lease_expires_at = NULL WHERE id = :id",
        "UPDATE runs SET state = 'queued', lease_expires_at = NULL,"
        " write_started_at = NULL WHERE id = :id",
        "UPDATE runs SET write_started_at = NULL WHERE id = :id",
        "UPDATE runs SET write_started_at = now() - interval '1 hour' WHERE id = :id",
    ):
        for engine in (dispatcher_db.engine, superuser_engine):
            with pytest.raises(DBAPIError) as caught:
                async with engine.begin() as conn:
                    await conn.execute(text(statement), params)
            assert _sqlstate(caught.value) == "23514", statement
    # Execução na fila não pode ter começado a gravar.
    for engine in (dispatcher_db.engine, superuser_engine):
        with pytest.raises(DBAPIError) as caught:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE runs SET write_started_at = now() WHERE id = :id"),
                    {"id": waiting},
                )
        assert _sqlstate(caught.value) == "23514"
    assert (await _row(dispatcher_db, rid))["state"] == RunState.RUNNING.value


# ---------------------------------------------------------------------------
# Com o executor de verdade: despachante, lançador, worker, Chromium e banco
# ---------------------------------------------------------------------------


class _CrashingIngestor:
    """Repassa os eventos ao banco até a queda; depois disso, para de responder.

    ``crash_when`` diz em que lote o despachante cai. Com ``recorded=False`` ele cai
    antes de gravar esse lote (nada é gravado nem confirmado); com ``recorded=True``,
    logo depois de gravá-lo. Daí em diante nenhuma chamada volta.
    """

    def __init__(
        self,
        queue: PgRunQueue,
        crash_when: Callable[[list[RunEvent]], bool],
        *,
        recorded: bool,
    ) -> None:
        self.queue = queue
        self.crash_when = crash_when
        self.recorded = recorded
        self.crashed = asyncio.Event()

    async def ingest_events(
        self, run_id: UUID, attempt: int, events: Sequence[RunEvent | Mapping[str, Any]]
    ) -> int:
        if self.crashed.is_set():
            await asyncio.Event().wait()
        parsed = [e if isinstance(e, RunEvent) else RunEvent.model_validate(e) for e in events]
        crash = self.crash_when(parsed)
        if crash and not self.recorded:
            self.crashed.set()
            await asyncio.Event().wait()
        count = await self.queue.ingest_events(run_id, attempt, events)
        if crash:
            self.crashed.set()
        return count


def _launcher(
    tmp_path: Path, chromium_path: str, events: Any, company_id: UUID
) -> LocalProcessLauncher:
    return LocalProcessLauncher(
        events,
        LocalLauncherSettings(
            work_dir=tmp_path / "trabalho",
            artifacts_dir=tmp_path / "artefatos",
            files_dir=tmp_path / "arquivos",
            chromium=chromium_path,
            browser_args=(HOST_RESOLVER_ARG,),
            # Longa o bastante para a queda (e não o prazo da confirmação) parar o worker.
            ack_timeout_s=60.0,
            kill_grace_s=2,
        ),
        StaticSecretSource({company_id: SECRETS}),
    )


async def _crash(
    dispatcher: Dispatcher, ingestor: _CrashingIngestor, *, settle_s: float = 0.0
) -> None:
    """Espera a queda e para o despachante sem gravar resultado (como no encerramento).

    ``settle_s`` é quanto o worker segue vivo depois da queda, antes de ser parado.
    """
    await asyncio.wait_for(ingestor.crashed.wait(), timeout=90)
    await asyncio.sleep(settle_s)
    tasks = list(dispatcher._tasks)
    assert tasks, "a execução já tinha terminado"
    for task in tasks:
        task.cancel()  # o lançador para o worker; o resultado não é gravado
    await asyncio.gather(*tasks, return_exceptions=True)
    assert dispatcher.in_flight == 0


async def _upload_robot(
    platform: PgPlatform, app_db: Database, portal: RunningPortal, tmp_path: Path, name: str
) -> tuple[Tenant, UUID]:
    tenant = await make_tenant(app_db, name)
    await publish(platform, tenant, _upload_manifest(portal))
    folder = tmp_path / "arquivos" / str(tenant.company_id)
    folder.mkdir(parents=True)
    (folder / "relatorio.csv").write_bytes(b"linha 1;valor\n")
    run = await platform.start(
        tenant.operator, "enviar_documento", {"documento": "relatorio.csv"}, RunTrigger.REST
    )
    return tenant, run.run_id


def _has(kind: RunEventKind, step_id: str | None = None, **data: Any) -> Callable[..., bool]:
    def check(events: list[RunEvent]) -> bool:
        return any(
            e.kind is kind
            and (step_id is None or e.step_id == step_id)
            and all(e.data.get(k) == v for k, v in data.items())
            for e in events
        )

    return check


@pytest.mark.browser
async def test_crash_before_the_write_is_recorded_requeues_and_writes_once(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=30))
    tenant, rid = await _upload_robot(platform, app_db, portal_v1, tmp_path, "Cai antes")
    # O despachante cai ao receber o início do passo que grava, antes de gravá-lo: o
    # executor espera a confirmação, que nunca vem, e não clica em "Enviar".
    ingestor = _CrashingIngestor(
        queue, _has(RunEventKind.STEP_STARTED, "enviar", grava=True), recorded=False
    )
    dispatcher = Dispatcher(
        queue,
        _launcher(tmp_path, chromium_path, ingestor, tenant.company_id),
        heartbeat_interval=0.2,
    )
    assert await dispatcher.run_once() == 1
    # O worker fica vivo uns segundos depois da queda: sem a confirmação, não clica.
    await _crash(dispatcher, ingestor, settle_s=3)

    assert portal_v1.state.uploads == []
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.RUNNING.value and row["write_started_at"] is None
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(seconds=31))
    assert report.requeued == [rid]

    # Nada foi gravado, então a nova tentativa pode rodar, e grava uma vez só.
    dispatcher = Dispatcher(queue, _launcher(tmp_path, chromium_path, queue, tenant.company_id))
    assert await dispatcher.run_once() == 1
    async with asyncio.timeout(90):
        await dispatcher.drain()
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.SUCCEEDED.value, row["error_message"]
    assert row["attempt"] == 2 and row["write_started_at"] is not None
    assert portal_v1.state.uploads == [{"nome": "relatorio.csv", "tamanho": 14}]


@pytest.mark.browser
async def test_crash_after_a_write_fails_without_returning_to_the_queue(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=30))
    tenant, rid = await _upload_robot(platform, app_db, portal_v1, tmp_path, "Cai depois")
    # O início do passo que grava foi gravado e confirmado, o executor enviou o arquivo,
    # e o despachante cai logo depois (antes do resultado).
    ingestor = _CrashingIngestor(queue, _has(RunEventKind.STEP_SUCCEEDED, "enviar"), recorded=True)
    dispatcher = Dispatcher(
        queue,
        _launcher(tmp_path, chromium_path, ingestor, tenant.company_id),
        heartbeat_interval=0.2,
    )
    assert await dispatcher.run_once() == 1
    await _crash(dispatcher, ingestor)

    assert portal_v1.state.uploads == [{"nome": "relatorio.csv", "tamanho": 14}]
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.RUNNING.value and row["write_started_at"] is not None
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(seconds=31))
    assert report.failed == [rid] and report.requeued == []
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.FAILED.value
    assert row["error_code"] == _WRITE_CODE
    assert "confira o sistema de destino" in row["error_message"]

    # Nada volta a rodar: o arquivo foi enviado uma vez só.
    dispatcher = Dispatcher(queue, _launcher(tmp_path, chromium_path, queue, tenant.company_id))
    assert await dispatcher.run_once() == 0
    assert portal_v1.state.uploads == [{"nome": "relatorio.csv", "tamanho": 14}]
    actions = [a for a, _ in await _audit(dispatcher_db, tenant.company_id, rid)]
    assert actions == ["execucao.iniciada", "execucao.falhou"]


@pytest.mark.browser
async def test_crash_after_the_irreversible_warning_is_indeterminate(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=30))
    tenant = await make_tenant(app_db, "Cai no irreversível")
    await publish(platform, tenant, emitir_protocolo(portal_v1.base_url, portal_v1.host))
    rid = await _start(platform, tenant)
    ingestor = _CrashingIngestor(
        queue, _has(RunEventKind.STEP_STARTED, "emitir", irreversivel=True), recorded=True
    )
    dispatcher = Dispatcher(
        queue,
        _launcher(tmp_path, chromium_path, ingestor, tenant.company_id),
        heartbeat_interval=0.2,
    )
    assert await dispatcher.run_once() == 1
    await _crash(dispatcher, ingestor)

    assert len(portal_v1.state.protocolos) <= 1  # ninguém sabe se emitiu
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(seconds=31))
    assert report.indeterminate == [rid] and report.requeued == []
    row = await _row(dispatcher_db, rid)
    assert row["state"] == RunState.INDETERMINATE.value
    assert row["write_started_at"] is not None and row["irreversible_started_at"] is not None
    assert await queue.claim_next() is None
