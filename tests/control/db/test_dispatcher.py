"""Despachante com um lançador falso: concorrência limitada, resultados e cerca."""

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import Dispatcher, PgPlatform, PgRunQueue
from rapa.core.contracts import RunnerJob, RunState, RunTrigger
from rapa.core.events import RunEventKind

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
                [event(job.run_id, job.attempt, 0, RunEventKind.RUN_STARTED)],
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
