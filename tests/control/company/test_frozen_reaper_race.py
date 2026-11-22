"""O recolhedor e um congelamento ao mesmo tempo: a execução nunca fica na fila congelada."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgRunQueue
from rapa.control.services import queue as queue_module
from rapa.control.services.lifecycle import FreezeReport, unfreeze_company
from rapa.core.contracts import RunTrigger
from tests.control.db.support import PROTOCOLO_INPUTS
from tests.control.publishing.support import World, build_world

from .support import freeze

pytestmark = pytest.mark.database

SLUG = "emitir_protocolo"


@pytest.fixture
async def world(app_db: Database, superuser_engine: AsyncEngine) -> AsyncIterator[World]:
    yield await build_world(app_db, superuser_engine)


async def _freeze_waiting_or_done(superuser: AsyncEngine, task: asyncio.Task[Any]) -> None:
    """Espera o congelamento terminar ou ficar parado numa trava consultiva."""
    for _ in range(100):
        if task.done():
            return
        async with superuser.connect() as conn:
            waiting = await conn.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("o congelamento nem terminou nem esperou uma trava")


async def test_freeze_while_the_reaper_decides_never_leaves_the_run_queued(
    world: World,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = world.a
    queue = PgRunQueue(dispatcher_db)
    run_a = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert await queue.claim_next() is not None

    # Logo depois de o recolhedor decidir (a auditoria vem depois da mudança da execução e
    # antes de confirmar), um congelamento começa em outra conexão.
    original = queue_module.audit.append
    box: dict[str, asyncio.Task[FreezeReport]] = {}

    async def racing(*args: Any, **kwargs: Any) -> Any:
        if "freeze" not in box and kwargs.get("action", "").startswith("execucao."):
            box["freeze"] = asyncio.create_task(freeze(owner_engine, a.company_id))
            await _freeze_waiting_or_done(superuser_engine, box["freeze"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(queue_module.audit, "append", racing)
    await queue.reap_expired(now=datetime.now(UTC) + timedelta(hours=1))
    report = await box["freeze"]
    monkeypatch.setattr(queue_module.audit, "append", original)
    assert report.changed

    async with superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT state, error_code FROM runs WHERE id = :r"), {"r": run_a.run_id}
            )
        ).one()
    # Ou o recolhedor viu a empresa congelada e cancelou, ou devolveu à fila antes e o
    # congelamento cancelou a fila: nunca fica na fila de uma empresa congelada.
    assert (row.state, row.error_code) == ("cancelled", "empresa_congelada")
    # Descongelar não ressuscita nada.
    await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    assert await queue.claim_next() is None
