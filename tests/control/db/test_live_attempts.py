"""Que tentativas estão vivas: a varredura dos pods de execução pergunta ao banco."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgPlatform, PgRunQueue
from rapa.core.contracts import RunTrigger
from rapa.core.ids import new_id

from .support import make_tenant, notas, publish, reset_queue, result

pytestmark = pytest.mark.database


async def test_only_the_current_attempt_of_a_running_run_is_live(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await make_tenant(app_db, "Pods vivos", guaranteed_slots=4)
    await publish(platform, tenant, notas())
    running = (await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)).run_id
    job = await queue.claim_next()
    assert job is not None and job.run_id == running and job.attempt == 1
    queued = (await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)).run_id
    unknown = new_id()

    asked = {(running, 1), (running, 2), (queued, 1), (unknown, 1)}
    # Só a tentativa atual de uma execução rodando: nem outra tentativa da mesma, nem uma
    # execução que ainda está na fila, nem uma que não existe.
    assert await queue.live_attempts(asked) == {(running, 1)}
    assert await queue.live_attempts(set()) == set()

    await queue.finish(running, 1, result(running, 1))
    assert await queue.live_attempts(asked) == set()
