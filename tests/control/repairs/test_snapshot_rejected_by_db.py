"""Um retrato que o banco recusa fica de fora; o resultado da execução é gravado igual."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import Dispatcher, PgPlatform, PgRunQueue
from rapa.core.contracts import RunnerJob, RunState, RunTrigger
from rapa.core.snapshot import MAX_SNAPSHOT_BYTES, PageSnapshot
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    Tenant,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
    result,
)
from tests.repair.support import login_page

pytestmark = pytest.mark.database


async def _world(app_db: Database, superuser: AsyncEngine) -> Tenant:
    await reset_queue(superuser, total_slots=10)
    tenant = await make_tenant(app_db, "Retrato")
    await publish(PgPlatform(app_db), tenant, protocolo())
    return tenant


async def _finish_with(
    app_db: Database, dispatcher_db: Database, tenant: Tenant, snapshot: dict[str, Any]
) -> UUID:
    run = await PgPlatform(app_db).start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    queue = PgRunQueue(dispatcher_db)
    for _ in range(20):
        job = await queue.claim_next()
        assert job is not None
        if job.run_id == run.run_id:
            break
    body = result(
        job.run_id,
        job.attempt,
        "failed",
        error_code="elemento_nao_encontrado",
        error_message="Não encontrei o elemento 'botão Entrar'.",
        failed_step_id="entrar",
        artifacts=[],
        failure_snapshot=snapshot,
    )
    assert await queue.finish(job.run_id, job.attempt, body)
    return run.run_id


async def _stored(app_db: Database, tenant: Tenant, run_id: UUID) -> tuple[Any, ...]:
    async with app_db.tenant_session(tenant.company_id) as session:
        row = (
            await session.execute(
                text("SELECT state, error_code, failed_step_id FROM runs WHERE id = :r"),
                {"r": run_id},
            )
        ).one()
        count = await session.scalar(
            text("SELECT count(*) FROM run_snapshots WHERE run_id = :r"), {"r": run_id}
        )
    return (*row, count)


async def test_snapshot_with_nul_is_left_out_and_result_is_kept(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await _world(app_db, superuser_engine)
    snapshot = login_page().model_dump(mode="json")
    snapshot["elements"][-1]["name"] = "Emitir\x00guia"
    snapshot["elements"][-1]["text"] = "Emitir\x00guia"
    PageSnapshot.model_validate(snapshot)  # passa no contrato
    run_id = await _finish_with(app_db, dispatcher_db, tenant, snapshot)
    state, code, step, count = await _stored(app_db, tenant, run_id)
    assert (state, code, step) == ("failed", "elemento_nao_encontrado", "entrar")
    # O banco não guarda o NUL em JSONB: o retrato fica de fora, o resultado fica.
    assert count == 0


def _big_snapshot() -> dict[str, Any]:
    base = login_page().model_dump(mode="json")
    element = {
        "frame": 0,
        "tag": "button",
        "role": "button",
        "counts": {"role_name": 1, "text": 1},
        "row": {"text": "r" * 500, "cells": ["c" * 300] * 20, "key": "k" * 200},
    }
    for key in ("name", "label", "placeholder", "test_id", "name_attr", "id", "title", "text"):
        element[key] = "x" * 300
    elements = [element] * 150
    base["elements"] = elements
    snapshot = PageSnapshot.model_validate(base)
    data = snapshot.model_dump(mode="json")

    # Corta elementos e depois o texto até a forma compacta ficar logo abaixo do limite.
    def size() -> int:
        compact = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return len(compact.encode())

    while size() > MAX_SNAPSHOT_BYTES:
        data["elements"] = data["elements"][:-1]
    data["elements"].append(json.loads(json.dumps(data["elements"][-1])))
    last = data["elements"][-1]
    excess = size() - MAX_SNAPSHOT_BYTES
    last["row"]["cells"] = [cell for cell in last["row"]["cells"]]
    for index in range(len(last["row"]["cells"])):
        if excess <= 0:
            break
        cut = min(excess, len(last["row"]["cells"][index]))
        last["row"]["cells"][index] = last["row"]["cells"][index][cut:]
        excess = size() - MAX_SNAPSHOT_BYTES
    assert MAX_SNAPSHOT_BYTES - 50 <= size() <= MAX_SNAPSHOT_BYTES
    PageSnapshot.model_validate(data)
    return data


async def test_snapshot_just_under_the_limit_compact_is_left_out_and_result_is_kept(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await _world(app_db, superuser_engine)
    snapshot = _big_snapshot()
    compact = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    spaced = json.dumps(snapshot, ensure_ascii=False)
    assert len(compact.encode()) <= MAX_SNAPSHOT_BYTES < len(spaced.encode())
    run_id = await _finish_with(app_db, dispatcher_db, tenant, snapshot)
    state, code, step, count = await _stored(app_db, tenant, run_id)
    assert (state, code, step) == ("failed", "elemento_nao_encontrado", "entrar")
    # Com espaços, como o jsonb o mede, passa de 1 MiB: o retrato fica de fora.
    assert count == 0


async def test_dispatcher_keeps_the_real_failure_when_the_snapshot_is_refused(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await _world(app_db, superuser_engine)
    platform = PgPlatform(app_db)
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    queue = PgRunQueue(dispatcher_db)
    snapshot = login_page().model_dump(mode="json")
    snapshot["elements"][-1]["name"] = "Emitir\x00guia"

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        return result(
            job.run_id,
            job.attempt,
            "failed",
            error_code="elemento_nao_encontrado",
            error_message="Não encontrei o elemento 'botão Entrar'.",
            failed_step_id="entrar",
            failure_snapshot=snapshot,
        )

    dispatcher = Dispatcher(queue, launch, heartbeat_interval=0.01, finish_retry_s=0.01)
    assert await dispatcher.run_once() >= 1
    async with asyncio.timeout(10):
        await dispatcher.drain()
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None
    assert status.state is RunState.FAILED, status.state
    assert status.error_code == "elemento_nao_encontrado"
