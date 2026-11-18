"""Isolamento entre empresas, provado como ``rapa_app`` (o papel da API e da interface).

A empresa B não vê nada da A (robôs, versões, execuções, eventos, credenciais,
auditoria, usuários), não consegue gravar com o company_id da A, e uma
transação sem empresa não vê nada de ninguém.
"""

from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rapa.control.db import Database
from rapa.control.db.schema import TENANT_TABLES
from rapa.control.services import PgPlatform, PgProvisioning, PgRunQueue
from rapa.core.contracts import RunTrigger
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import CredentialRef

from .support import PROTOCOLO_INPUTS, Tenant, event, make_tenant, protocolo, publish, reset_queue

pytestmark = pytest.mark.database


@dataclass(frozen=True)
class World:
    a: Tenant
    b: Tenant
    a_robot: UUID
    a_run: UUID


async def _world(app_db: Database, dispatcher_db: Database, superuser: AsyncEngine) -> World:
    await reset_queue(superuser, total_slots=10)
    platform = PgPlatform(app_db)
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    provisioning = PgProvisioning(app_db)
    for tenant in (a, b):
        await provisioning.register_credential(
            tenant.company_id,
            tenant.area_id,
            CredentialRef(name="portal", kind="password", origin="https://portal.rapa.test"),
            vault_path=f"empresas/{tenant.company_id}/portal",
        )
    a_version = await publish(platform, a, protocolo())
    await publish(platform, b, protocolo())
    a_run = await platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k"
    )
    await platform.start(b.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k")
    queue = PgRunQueue(dispatcher_db)
    for _ in range(2):
        job = await queue.claim_next()
        assert job is not None
        await queue.ingest_events(
            job.run_id,
            job.attempt,
            [event(job.run_id, job.attempt, 0, RunEventKind.RUN_STARTED)],
        )
    return World(a=a, b=b, a_robot=a_version.robot_id, a_run=a_run.run_id)


async def _counts(session: AsyncSession, company_id: UUID) -> dict[str, tuple[int, int]]:
    """Por tabela: (linhas visíveis, linhas visíveis de outra empresa)."""
    out = {}
    for table in TENANT_TABLES:
        column = table.c.id if table.name == "companies" else table.c.company_id
        visible = await session.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
        foreign = await session.scalar(
            select(text("count(*)")).select_from(table).where(column != company_id)
        )
        out[table.name] = (int(visible or 0), int(foreign or 0))
    return out


async def test_company_b_sees_nothing_of_company_a(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.tenant_session(world.b.company_id) as session:
        counts = await _counts(session, world.b.company_id)
        a_ids = {
            "robots": await session.scalar(
                text("SELECT count(*) FROM robots WHERE id = :id"), {"id": world.a_robot}
            ),
            "runs": await session.scalar(
                text("SELECT count(*) FROM runs WHERE id = :id"), {"id": world.a_run}
            ),
            "run_events": await session.scalar(
                text("SELECT count(*) FROM run_events WHERE run_id = :id"), {"id": world.a_run}
            ),
        }
    for table, (visible, foreign) in counts.items():
        assert foreign == 0, f"{table}: B enxerga linhas de outra empresa"
        assert visible > 0, f"{table}: B deveria ver as próprias linhas"
    assert a_ids == {"robots": 0, "runs": 0, "run_events": 0}

    # Pelos serviços, com o principal da B.
    platform = PgPlatform(app_db)
    admin_b = world.b.admin
    assert all(s.company_id == world.b.company_id for s in await platform.list_published(admin_b))
    assert all(r.company_id == world.b.company_id for r in await platform.list_robots(admin_b))
    assert await platform.get_version(admin_b, world.a_robot, 1) is None
    assert await platform.get(admin_b, world.a_run) is None
    assert all(
        s.company_id == world.b.company_id for s in await platform.search(admin_b, "protocolo")
    )


async def test_writes_with_another_company_id_fail_with_check(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, dispatcher_db, superuser_engine)
    a, b = world.a, world.b
    attempts = {
        "companies": ("INSERT INTO companies (id, name) VALUES (:new, 'Intrusa')", {}),
        "areas": (
            "INSERT INTO areas (id, company_id, name) VALUES (:new, :a, 'Invasão')",
            {},
        ),
        "robots": (
            "INSERT INTO robots (id, company_id, area_id, slug, title, status) "
            "VALUES (:new, :a, :area, 'robo_intruso', 'x', 'draft')",
            {"area": a.area_id},
        ),
        "audit_log": (
            "INSERT INTO audit_log (company_id, actor, action, target, payload_digest) "
            "VALUES (:a, 'b', 'teste.invasao', '', repeat('0', 64))",
            {},
        ),
        "credentials": (
            "INSERT INTO credentials (id, company_id, area_id, name, kind, origin, vault_path) "
            "VALUES (:new, :a, :area, 'x', 'password', 'https://x.rapa.test', 'x')",
            {"area": a.area_id},
        ),
    }
    for table, (statement, extra) in attempts.items():
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(b.company_id) as session:
                await session.execute(
                    text(statement), {"new": new_id(), "a": a.company_id, **extra}
                )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", table
        assert "row-level security" in str(caught.value.orig), table

    # Mudar a empresa de uma linha própria para a da A também é barrado.
    with pytest.raises(DBAPIError) as caught:
        async with app_db.tenant_session(b.company_id) as session:
            await session.execute(text("UPDATE robots SET company_id = :a"), {"a": a.company_id})
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"

    # Atualizar ou apagar linhas da A não alcança nada.
    async with app_db.tenant_session(b.company_id) as session:
        updated = await session.execute(
            text("UPDATE robots SET title = 'tomado' WHERE id = :id"), {"id": world.a_robot}
        )
        deleted = await session.execute(
            text("DELETE FROM idempotency_keys WHERE company_id = :a"), {"a": a.company_id}
        )
    assert updated.rowcount == 0  # type: ignore[attr-defined]
    assert deleted.rowcount == 0  # type: ignore[attr-defined]


async def test_no_tenant_sees_nothing(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.engine.connect() as conn:
        # Conexão nova: o parâmetro nunca foi definido (NULL).
        for table in TENANT_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
            assert count == 0, table.name
        await conn.rollback()
        # Mesma conexão depois de uma transação com empresa: o valor local some ('').
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('rapa.company_id', :id, true)"), {"id": str(new_id())}
            )
        assert await conn.scalar(text("SELECT current_setting('rapa.company_id', true)")) == ""
        for table in TENANT_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
            assert count == 0, table.name
        await conn.rollback()
        # Sem empresa, nenhuma inclusão passa.
        with pytest.raises(DBAPIError) as caught:
            await conn.execute(
                text("INSERT INTO companies (id, name) VALUES (:id, 'x')"), {"id": new_id()}
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_session_level_tenant_does_not_leak_through_pool(app_db: Database) -> None:
    company = new_id()
    async with app_db.tenant_session(company) as session:
        assert await session.scalar(text("SELECT current_setting('rapa.company_id')")) == str(
            company
        )
    async with app_db.dispatcher_session() as session:
        assert await session.scalar(text("SELECT current_setting('rapa.company_id', true)")) in (
            "",
            None,
        )
    with pytest.raises(TypeError):
        async with app_db.tenant_session(str(company)):  # type: ignore[arg-type]
            pass


async def test_dispatcher_writes_events_with_run_company(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """O company_id do evento vem da execução, nunca do executor."""
    world = await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.tenant_session(world.a.company_id) as session:
        rows = (
            await session.execute(
                text("SELECT company_id, kind FROM run_events WHERE run_id = :id"),
                {"id": world.a_run},
            )
        ).all()
    assert rows == [(world.a.company_id, "run_started")]
