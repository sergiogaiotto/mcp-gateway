"""Despachante com um lançador falso: concorrência limitada, resultados e cerca."""

import asyncio
import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher
from rapa.control.services import (
    Dispatcher,
    PgPlatform,
    PgRunQueue,
    ReapReport,
    WorkerNotStarted,
)
from rapa.core.contracts import RunnerJob, RunState, RunTrigger
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id

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


class FakeLauncher:
    """Simula o worker: emite eventos pela fila e devolve um RESULT.json."""

    def __init__(self, queue: PgRunQueue, *, delay: float = 0.05) -> None:
        self.queue = queue
        self.delay = delay
        self.current = 0
        self.peak = 0
        self.jobs: list[RunnerJob] = []

    async def __call__(self, job: RunnerJob) -> Mapping[str, Any]:
        self.current += 1
        self.peak = max(self.peak, self.current)
        self.jobs.append(job)
        try:
            await self.queue.ingest_events(
                job.run_id,
                job.attempt,
                [event(job.run_id, job.attempt, job.first_seq, RunEventKind.RUN_STARTED)],
            )
            await asyncio.sleep(self.delay)
            return result(job.run_id, job.attempt, outputs={"notas": []}, steps_done=3)
        finally:
            self.current -= 1


async def _wait_finished(platform: PgPlatform, tenant: Tenant, ids: list[UUID]) -> None:
    async with asyncio.timeout(20):
        while True:
            states = [await platform.get(tenant.operator, run_id) for run_id in ids]
            if all(s is not None and s.state.finished for s in states):
                return
            await asyncio.sleep(0.05)


async def test_dispatcher_runs_queue_with_bounded_concurrency(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Despacho", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    ids = [
        (await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)).run_id
        for _ in range(7)
    ]
    queue = PgRunQueue(dispatcher_db)
    launcher = FakeLauncher(queue)
    dispatcher = Dispatcher(
        queue, launcher, concurrency=2, poll_interval=0.02, heartbeat_interval=0.01
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        await _wait_finished(platform, tenant, ids)
    finally:
        stop.set()
        await loop
    assert launcher.peak == 2
    assert sorted(j.run_id for j in launcher.jobs) == sorted(ids)
    for run_id in ids:
        status = await platform.get(tenant.operator, run_id)
        assert status is not None
        assert status.state is RunState.SUCCEEDED
        assert status.outputs == {"notas": []}
        assert status.steps_done == 3
    assert dispatcher.in_flight == 0


async def test_launcher_failure_becomes_failed_or_indeterminate(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Lançador quebrado", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    await publish(platform, tenant, protocolo())
    reading = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    writing = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    garbage = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.BATCH)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        if job.run_id == garbage.run_id:
            return {"estado": "ok"}
        raise RuntimeError("senha123 vazou na mensagem")

    queue = PgRunQueue(dispatcher_db)
    dispatcher = Dispatcher(queue, launch, concurrency=3, heartbeat_interval=0.01)
    assert await dispatcher.run_once() == 3
    await dispatcher.drain()

    failed = await platform.get(tenant.operator, reading.run_id)
    assert failed is not None and failed.state is RunState.FAILED
    assert failed.error_code == "falha_no_lancador"
    assert failed.error_message is not None and "senha123" not in failed.error_message
    unknown = await platform.get(tenant.operator, writing.run_id)
    assert unknown is not None and unknown.state is RunState.INDETERMINATE
    invalid = await platform.get(tenant.operator, garbage.run_id)
    assert invalid is not None and invalid.state is RunState.FAILED
    assert invalid.error_code == "resultado_invalido"


async def test_stale_attempt_is_cancelled_and_not_finished(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Antiga", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=30))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return result(job.run_id, job.attempt)  # pragma: no cover

    dispatcher = Dispatcher(queue, launch, concurrency=1, heartbeat_interval=0.02)
    assert await dispatcher.run_once() == 1
    await asyncio.wait_for(started.wait(), 5)
    # Outro despachante recolhe a concessão e pega a execução de novo (tentativa 2).
    reclaimer = PgRunQueue(dispatcher_db)
    report = await reclaimer.reap_expired(now=reclaimer.clock() + timedelta(minutes=5))
    assert report.requeued == [run.run_id]
    job = await reclaimer.claim_next()
    assert job is not None and job.attempt == 2
    await asyncio.wait_for(cancelled.wait(), 5)
    await dispatcher.drain()
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.RUNNING  # a tentativa 2 segue
    assert await reclaimer.finish(run.run_id, 2, result(run.run_id, 2))


def _db_down() -> sqlalchemy.exc.OperationalError:
    return sqlalchemy.exc.OperationalError("COMMIT", {}, ConnectionError("conexão perdida"))


async def test_result_is_recorded_after_a_database_outage(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Queda do banco", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    outage = asyncio.Event()
    outage.set()
    calls: Counter[str] = Counter()
    insisted = asyncio.Event()
    real_finish, real_heartbeat = queue.finish, queue.heartbeat

    async def finish(*args: Any, **kwargs: Any) -> bool:
        calls["finish"] += 1
        if calls["finish"] >= 6:
            insisted.set()
        if outage.is_set():
            raise _db_down()
        return await real_finish(*args, **kwargs)

    async def heartbeat(*args: Any, **kwargs: Any) -> bool:
        calls["heartbeat"] += 1
        if outage.is_set():
            raise _db_down()
        return await real_heartbeat(*args, **kwargs)

    monkeypatch.setattr(queue, "finish", finish)
    monkeypatch.setattr(queue, "heartbeat", heartbeat)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        return result(job.run_id, job.attempt, outputs={"notas": []}, steps_done=7)

    clock_offset = timedelta(0)
    dispatcher = Dispatcher(
        queue,
        launch,
        heartbeat_interval=0.01,
        reap_interval=0,
        finish_retry_s=0.01,
        clock=lambda: datetime.now(UTC) + clock_offset,
    )
    assert await dispatcher.run_once() == 1
    # A execução terminou, mas o banco caiu: o resultado não é descartado depois
    # de poucas tentativas.
    async with asyncio.timeout(10):
        await insisted.wait()
    assert calls["heartbeat"] >= 1  # a concessão continua sendo renovada

    # A concessão venceu durante a queda: o recolhedor deste despachante não devolve
    # à fila a execução cujo resultado ele ainda está gravando.
    clock_offset = timedelta(seconds=30)
    await dispatcher.maintenance()
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.RUNNING
    clock_offset = timedelta(0)

    # O banco volta: o resultado é gravado e a execução não roda de novo.
    outage.clear()
    async with asyncio.timeout(10):
        await dispatcher.drain()
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.SUCCEEDED
    assert status.outputs == {"notas": []} and status.steps_done == 7
    assert await queue.claim_next() is None
    # Depois de gravado, o recolhedor volta a valer para as concessões vencidas.
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(minutes=5))
    assert run.run_id not in report.requeued


async def test_result_retries_stop_at_the_deadline(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Banco não volta", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    # Prazo da execução já vencido: a folga para gravar o resultado é curta.
    queue = PgRunQueue(dispatcher_db, max_duration=timedelta(seconds=-1))
    calls: Counter[str] = Counter()

    async def finish(*args: Any, **kwargs: Any) -> bool:
        calls["finish"] += 1
        raise _db_down()

    monkeypatch.setattr(queue, "finish", finish)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        return result(job.run_id, job.attempt)

    dispatcher = Dispatcher(
        queue, launch, heartbeat_interval=0.01, finish_retry_s=0.01, finish_grace_s=0.3
    )
    assert await dispatcher.run_once() == 1
    async with asyncio.timeout(10):
        await dispatcher.drain()
    assert calls["finish"] > 3
    assert dispatcher.in_flight == 0
    # Sem o resultado, a execução segue para o recolhedor (nada fica preso).
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(minutes=5))
    assert report.requeued == [run.run_id]


async def test_result_the_database_refuses_becomes_a_failure(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Resultado recusado", guaranteed_slots=10)
    await publish(platform, tenant, notas())
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        # O PostgreSQL não guarda o caractere nulo em JSONB: erro que não passa repetindo.
        return result(job.run_id, job.attempt, outputs={"notas": ["a\u0000b"]})

    dispatcher = Dispatcher(queue, launch, heartbeat_interval=0.01, finish_retry_s=0.01)
    assert await dispatcher.run_once() == 1
    async with asyncio.timeout(10):
        await dispatcher.drain()
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == "resultado_invalido" and status.outputs is None


class _FlakyQueue:
    """Fila falsa: o banco responde com erros que não são do driver."""

    def __init__(self, job: RunnerJob | None = None) -> None:
        self.job = job
        self.calls: Counter[str] = Counter()
        self.finished: list[Mapping[str, Any]] = []

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        self.calls["partitions"] += 1
        if self.calls["partitions"] == 1:
            raise RuntimeError("falha inesperada")
        return 0

    async def reap_expired(self, *, now: datetime | None = None, skip: Any = ()) -> ReapReport:
        return ReapReport()

    async def claim_next(self, *, now: datetime | None = None) -> RunnerJob | None:
        self.calls["claim"] += 1
        if self.calls["claim"] <= 3:
            # Pool de conexões esgotado: não é um DBAPIError.
            raise sqlalchemy.exc.TimeoutError("limite do pool atingido")
        job, self.job = self.job, None
        return job

    async def heartbeat(self, run_id: UUID, attempt: int, *, now: datetime | None = None) -> bool:
        self.calls["heartbeat"] += 1
        raise sqlalchemy.exc.TimeoutError("limite do pool atingido")

    async def finish(
        self, run_id: UUID, attempt: int, result: Mapping[str, Any], *, now: datetime | None = None
    ) -> bool:
        self.finished.append(result)
        return True


def _job() -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=notas(),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_drain_returns_when_a_task_just_finished() -> None:
    # A tarefa termina, mas o callback que a tira da lista só roda na próxima volta
    # do laço; o ``gather`` de tarefas concluídas não dá a vez ao laço. O cenário
    # roda numa thread própria para o teste falhar, e não travar, se o laço girar.
    outcome: list[int] = []

    async def scenario() -> None:
        async def launch(job: RunnerJob) -> Mapping[str, Any]:  # pragma: no cover
            raise AssertionError("não deveria lançar")

        dispatcher = Dispatcher(_FlakyQueue(), launch)  # type: ignore[arg-type]

        async def quick() -> None:
            return None

        task = asyncio.create_task(quick())
        dispatcher._tasks[task] = _job()
        task.add_done_callback(dispatcher._done)
        await asyncio.sleep(0)
        assert task.done() and dispatcher.in_flight == 1
        await dispatcher.drain()
        outcome.append(dispatcher.in_flight)

    thread = threading.Thread(target=asyncio.run, args=(scenario(),), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "drain() ficou girando sem dar a vez ao laço"
    assert outcome == [0]


async def test_dispatch_loop_and_heartbeats_survive_unexpected_errors() -> None:
    job = _job()
    queue = _FlakyQueue(job)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        await asyncio.sleep(0.2)  # tempo para várias renovações falharem
        return result(job.run_id, job.attempt, outputs={"notas": []})

    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        launch,
        poll_interval=0.01,
        heartbeat_interval=0.01,
        partitions_interval=0,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(10):
            while not queue.finished:
                assert not loop.done(), "o laço de despacho parou sozinho"
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await loop
    # O laço sobreviveu ao pool esgotado e a um erro inesperado, sem girar em falso.
    assert queue.calls["claim"] > 3
    assert queue.calls["claim"] < 200
    assert queue.calls["partitions"] >= 2
    # As renovações falharam o tempo todo, e mesmo assim o resultado foi gravado.
    assert queue.calls["heartbeat"] >= 2
    assert [r["state"] for r in queue.finished] == ["succeeded"]


class _BrokenStepQueue:
    """Fila falsa em que uma etapa do laço (partições, recolhimento ou despacho) sempre falha."""

    def __init__(self, broken: str, job: RunnerJob | None = None) -> None:
        self.broken = {broken}
        self.job = job
        self.calls: Counter[str] = Counter()
        self.finished: list[Mapping[str, Any]] = []

    def _check(self, step: str) -> None:
        self.calls[step] += 1
        if step in self.broken:
            if step == "partitions":
                raise PermissionError("sem permissão para criar a partição de dados sigilosos")
            raise sqlalchemy.exc.ProgrammingError(
                "SELECT", {}, Exception(f"erro persistente em {step} com dados sigilosos")
            )

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        self._check("partitions")
        return 0

    async def reap_expired(self, *, now: datetime | None = None, skip: Any = ()) -> ReapReport:
        self._check("reap")
        return ReapReport()

    async def claim_next(self, *, now: datetime | None = None) -> RunnerJob | None:
        self._check("claim")
        job, self.job = self.job, None
        return job

    async def heartbeat(self, run_id: UUID, attempt: int, *, now: datetime | None = None) -> bool:
        return True

    async def finish(
        self, run_id: UUID, attempt: int, result: Mapping[str, Any], *, now: datetime | None = None
    ) -> bool:
        self.finished.append(result)
        return True


async def _succeed(job: RunnerJob) -> Mapping[str, Any]:
    return result(job.run_id, job.attempt, outputs={"notas": []})


@pytest.mark.parametrize("broken", ["partitions", "reap"])
async def test_persistent_maintenance_error_does_not_block_dispatch(broken: str) -> None:
    # Sem permissão para criar partições, ou um recolhedor que sempre falha: as
    # execuções da fila continuam sendo despachadas, e a outra etapa continua rodando.
    queue = _BrokenStepQueue(broken, _job())
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        _succeed,
        poll_interval=0.01,
        heartbeat_interval=0.01,
        reap_interval=0,
        partitions_interval=0,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(10):
            while not queue.finished:
                assert not loop.done(), "o laço de despacho parou sozinho"
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await loop
    assert [r["state"] for r in queue.finished] == ["succeeded"]
    assert queue.calls["claim"] >= 10  # o laço seguiu pegando execuções
    other = "reap" if broken == "partitions" else "partitions"
    assert queue.calls[other] >= 10
    # A etapa que falha é repetida com espera crescente, não a cada volta do laço.
    assert 2 <= queue.calls[broken] <= 12
    assert set(dispatcher.failing) == {broken}


async def test_failing_maintenance_step_backs_off_and_shows_in_health(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="rapa.control.dispatcher")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    now = [start]
    queue = _BrokenStepQueue("partitions")
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        _succeed,
        poll_interval=1.0,
        reap_interval=30.0,
        partitions_interval=3600.0,
        unhealthy_after_s=120.0,
        clock=lambda: now[0],
    )

    async def at(seconds: float) -> None:
        now[0] = start + timedelta(seconds=seconds)
        await dispatcher.maintenance()

    await at(0)
    assert queue.calls == Counter({"partitions": 1, "reap": 1})  # o recolhedor rodou
    assert dispatcher.failing == {"partitions": start}
    assert dispatcher.healthy  # uma falha só ainda não muda a saúde
    # Nova tentativa depois de um intervalo do laço, dobrando a cada falha, até 60 s.
    elapsed, tries = 0.0, 1
    for wait in (1, 2, 4, 8, 16, 32, 60, 60, 60):
        await at(elapsed + wait - 0.5)
        assert queue.calls["partitions"] == tries
        elapsed += wait
        await at(elapsed)
        tries += 1
        assert queue.calls["partitions"] == tries
    assert queue.calls["reap"] >= 5  # o recolhedor seguiu no próprio intervalo
    # Falhando há mais de ``unhealthy_after_s``: a saúde do despachante acusa.
    assert not dispatcher.healthy
    assert dispatcher.failing == {"partitions": start}

    # Uma sequência longa de falhas não estoura a conta da espera, que fica em 60 s.
    for _ in range(2000):
        elapsed += 60
        await at(elapsed)
    tries += 2000
    assert queue.calls["partitions"] == tries
    # Só o tipo do erro vai para o registro, nunca a mensagem.
    assert "PermissionError" in caplog.text and "sigilosos" not in caplog.text

    # A etapa volta a funcionar: a saúde volta e a próxima vez segue o intervalo normal.
    queue.broken.clear()
    elapsed += 60
    await at(elapsed)
    assert queue.calls["partitions"] == tries + 1
    assert dispatcher.failing == {} and dispatcher.healthy
    await at(elapsed + 3599)
    assert queue.calls["partitions"] == tries + 1
    await at(elapsed + 3600)
    assert queue.calls["partitions"] == tries + 2


async def test_persistent_claim_error_shows_in_health() -> None:
    queue = _BrokenStepQueue("claim")
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        _succeed,
        poll_interval=0.01,
        reap_interval=0,
        unhealthy_after_s=0.1,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))

    async def until(condition: Callable[[], bool]) -> None:
        for _ in range(1000):
            if condition():
                return
            assert not loop.done(), "o laço de despacho parou sozinho"
            await asyncio.sleep(0.01)
        raise AssertionError("a condição não ficou verdadeira em 10 s")

    try:
        await until(lambda: not dispatcher.healthy)
        assert set(dispatcher.failing) == {"claim"}
        assert queue.calls["reap"] >= 2  # a manutenção segue rodando
        queue.broken.clear()
        await until(lambda: dispatcher.healthy)
        assert dispatcher.failing == {}
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await loop


async def test_worker_that_never_started_is_a_plain_failure(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Cofre fora", guaranteed_slots=10)
    await publish(platform, tenant, protocolo())
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    queue = PgRunQueue(dispatcher_db)

    class VaultDown:
        async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
            raise ConnectionError("cofre indisponível")

    launcher = LocalProcessLauncher(
        queue,
        LocalLauncherSettings(work_dir=tmp_path / "trabalho", artifacts_dir=tmp_path / "artefatos"),
        VaultDown(),
    )
    dispatcher = Dispatcher(queue, launcher, heartbeat_interval=0.01)
    assert await dispatcher.run_once() == 1
    async with asyncio.timeout(10):
        await dispatcher.drain()
    # O navegador nem abriu: o robô irreversível falhou, sem ficar indeterminado.
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == "executor_nao_iniciado"
    assert list((tmp_path / "trabalho").iterdir()) == []  # pasta da tentativa apagada
    async with dispatcher_db.dispatcher_session() as session:
        irreversible = await session.scalar(
            text("SELECT irreversible_started_at FROM runs WHERE id = :id"), {"id": run.run_id}
        )
    assert irreversible is None


async def test_launcher_reports_failures_before_the_worker_starts(tmp_path: Path) -> None:
    class Events:
        async def ingest_events(self, *args: Any) -> int:
            raise AssertionError("nenhum evento deveria chegar")

    class VaultDown:
        async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
            raise ConnectionError("cofre indisponível")

    job = _job()
    work_dir = tmp_path / "trabalho"
    launcher = LocalProcessLauncher(
        Events(),
        LocalLauncherSettings(work_dir=work_dir, artifacts_dir=tmp_path / "artefatos"),
        VaultDown(),
    )
    with pytest.raises(WorkerNotStarted):
        await launcher.launch(job)
    assert list(work_dir.iterdir()) == []

    # Falha ao preparar a pasta da tentativa (o caminho é um arquivo).
    blocked = tmp_path / "bloqueado"
    blocked.write_text("x", encoding="utf-8")
    launcher = LocalProcessLauncher(
        Events(), LocalLauncherSettings(work_dir=blocked, artifacts_dir=tmp_path / "artefatos")
    )
    with pytest.raises(WorkerNotStarted):
        await launcher.launch(job)

    # Programa do worker inexistente.
    launcher = LocalProcessLauncher(
        Events(),
        LocalLauncherSettings(
            work_dir=work_dir,
            artifacts_dir=tmp_path / "artefatos",
            python=str(tmp_path / "nao-existe"),
        ),
    )
    with pytest.raises(WorkerNotStarted):
        await launcher.launch(job)
    assert list(work_dir.iterdir()) == []

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        raise WorkerNotStarted("o executor não pôde ser iniciado")

    queue = _FlakyQueue()
    queue.calls["claim"] = 3
    queue.job = job
    dispatcher = Dispatcher(queue, launch, heartbeat_interval=0.01)  # type: ignore[arg-type]
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    assert [(r["state"], r["error_code"]) for r in queue.finished] == [
        ("failed", "executor_nao_iniciado")
    ]


async def test_partition_lock_wait_does_not_stop_dispatch(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Criar a partição espera trava exclusiva atrás de uma transação longa: vira erro.

    Sem o limite de espera, a manutenção ficaria parada (sem falhar) e, na fila de
    travas, seguraria também as leituras de eventos de quem pega execuções.
    """
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Trava da partição")
    await publish(platform, tenant, notas())
    async with superuser_engine.begin() as conn:
        name = await conn.scalar(
            text(
                "SELECT 'run_events_' || to_char(date_trunc('month', now() AT TIME ZONE 'UTC')"
                " + interval '2 month', 'YYYY_MM')"
            )
        )
        await conn.execute(text(f'DROP TABLE IF EXISTS public."{name}"'))
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        return result(job.run_id, job.attempt, outputs={"notas": []})

    queue = PgRunQueue(dispatcher_db, maintenance_lock_timeout=timedelta(milliseconds=300))
    dispatcher = Dispatcher(queue, launch, poll_interval=0.05, unhealthy_after_s=0.5)
    blocker = await superuser_engine.connect()
    transaction = await blocker.begin()
    # Uma leitura de eventos numa transação aberta (relatório lento, pg_dump, sessão parada).
    await blocker.execute(text("SELECT count(*) FROM run_events"))
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(10):
            while True:
                status = await platform.get(tenant.operator, run.run_id)
                if status is not None and status.state is RunState.SUCCEEDED:
                    break
                await asyncio.sleep(0.05)
        assert "partitions" in dispatcher.failing
        await asyncio.sleep(0.6)  # mais que unhealthy_after_s, com a falha seguindo
        assert not dispatcher.healthy
    finally:
        await transaction.rollback()
        await blocker.close()
        stop.set()
        async with asyncio.timeout(10):
            await loop
    # Sem a trava, a partição sai na próxima tentativa e a saúde volta.
    await dispatcher.maintenance()
    assert "partitions" not in dispatcher.failing


def test_maintenance_limits_must_be_positive(dispatcher_db: Database) -> None:
    with pytest.raises(ValueError, match="positivos"):
        PgRunQueue(dispatcher_db, maintenance_lock_timeout=timedelta(0))
