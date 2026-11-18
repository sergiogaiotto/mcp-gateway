"""Garantias do banco: versões imutáveis, auditoria encadeada e execuções que não voltam à fila."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgPlatform, PgRunQueue, verify_chain
from rapa.control.services import audit as audit_service
from rapa.core.contracts import RunTrigger

from .support import PROTOCOLO_INPUTS, make_tenant, protocolo, publish, reset_queue

pytestmark = pytest.mark.database


@asynccontextmanager
async def as_tenant(engine: AsyncEngine, company_id: UUID) -> AsyncIterator[AsyncConnection]:
    """Transação de um papel qualquer com a empresa fixada (FORCE vale para o dono)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('rapa.company_id', :id, true)"), {"id": str(company_id)}
        )
        yield conn


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def test_robot_versions_are_immutable(app_db: Database, owner_engine: AsyncEngine) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Imutável")
    version = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    params = {"id": version.version_id}

    # A aplicação nem tem permissão de mudar outras colunas.
    with pytest.raises(DBAPIError) as caught:
        async with app_db.tenant_session(tenant.company_id) as session:
            await session.execute(
                text("UPDATE robot_versions SET manifest = '{}' WHERE id = :id"), params
            )
    assert _sqlstate(caught.value) == "42501"

    # O dono tem a permissão, mas o gatilho barra.
    for statement in (
        'UPDATE robot_versions SET manifest = manifest || \'{"title": "outro"}\' WHERE id = :id',
        "UPDATE robot_versions SET fingerprint = repeat('a', 64) WHERE id = :id",
        "UPDATE robot_versions SET violations = '[\"x\"]' WHERE id = :id",
        "UPDATE robot_versions SET created_by = 'outra' WHERE id = :id",
        "UPDATE robot_versions SET version_no = 99 WHERE id = :id",
        "DELETE FROM robot_versions WHERE id = :id",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with as_tenant(owner_engine, tenant.company_id) as conn:
                await conn.execute(text(statement), params)
        assert _sqlstate(caught.value) == "23514", statement

    # Estado e aprovador podem mudar; o aprovador não pode ser quem criou nem ser trocado.
    async with as_tenant(owner_engine, tenant.company_id) as conn:
        await conn.execute(
            text("UPDATE robot_versions SET status = 'rejected' WHERE id = :id"), params
        )
    with pytest.raises(DBAPIError) as caught:
        async with as_tenant(owner_engine, tenant.company_id) as conn:
            await conn.execute(
                text("UPDATE robot_versions SET approved_by = created_by WHERE id = :id"), params
            )
    assert _sqlstate(caught.value) == "23514"
    async with as_tenant(owner_engine, tenant.company_id) as conn:
        await conn.execute(
            text("UPDATE robot_versions SET approved_by = 'pessoa-1' WHERE id = :id"), params
        )
    with pytest.raises(DBAPIError) as caught:
        async with as_tenant(owner_engine, tenant.company_id) as conn:
            await conn.execute(
                text("UPDATE robot_versions SET approved_by = 'pessoa-2' WHERE id = :id"), params
            )
    assert _sqlstate(caught.value) == "23514"
    info = await platform.get_version(tenant.creator, version.robot_id, 1)
    assert info is not None and info.manifest == protocolo()


async def test_audit_chain_is_continuous_and_append_only(
    app_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Auditoria")
    await publish(platform, tenant, protocolo())
    for _ in range(3):
        await platform.start(tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST)

    # Inclusões simultâneas não bifurcam a cadeia.
    async def append(n: int) -> None:
        async with app_db.tenant_session(tenant.company_id) as session:
            await audit_service.append(
                session,
                company_id=tenant.company_id,
                actor="teste",
                action="teste.simultaneo",
                target=f"item:{n}",
                payload={"n": n},
            )

    await asyncio.gather(*(append(n) for n in range(10)))

    async with app_db.tenant_session(tenant.company_id) as session:
        report = await verify_chain(session, tenant.company_id)
        rows = (
            await session.execute(
                text("SELECT seq, prev_hash, row_hash FROM audit_log ORDER BY seq")
            )
        ).all()
    assert report.ok, report.problems
    assert report.rows == 3 + 3 + 10
    assert [r[0] for r in rows] == list(range(1, report.rows + 1))
    assert rows[0][1] == audit_service.GENESIS_HASH
    assert all(rows[i][1] == rows[i - 1][2] for i in range(1, len(rows)))

    # A aplicação não tem permissão de alterar nem apagar.
    for statement in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(tenant.company_id) as session:
                await session.execute(text(statement))
        assert _sqlstate(caught.value) == "42501", statement
    # Nem o dono nem um superusuário conseguem, por causa do gatilho.
    for engine in (owner_engine, superuser_engine):
        for statement in (
            "UPDATE audit_log SET actor = 'x' WHERE company_id = :c",
            "DELETE FROM audit_log WHERE company_id = :c",
        ):
            with pytest.raises(DBAPIError) as caught:
                async with as_tenant(engine, tenant.company_id) as conn:
                    await conn.execute(text(statement), {"c": tenant.company_id})
            assert _sqlstate(caught.value) == "23514", statement
    with pytest.raises(DBAPIError) as caught:
        async with superuser_engine.begin() as conn:
            await conn.execute(text("TRUNCATE audit_log"))
    assert _sqlstate(caught.value) == "23514"

    # Quem forjar uma linha por fora (gatilho desligado) é pego na verificação.
    async with superuser_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
        await conn.execute(
            text("UPDATE audit_log SET actor = 'forjado' WHERE company_id = :c AND seq = 2"),
            {"c": tenant.company_id},
        )
        await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))
    async with app_db.tenant_session(tenant.company_id) as session:
        tampered = await verify_chain(session, tenant.company_id)
    assert tampered.problems == ["linha 2: o hash da linha não confere"]


async def test_audit_rows_are_written_by_the_trigger(app_db: Database) -> None:
    tenant = await make_tenant(app_db, "Gatilho da auditoria")
    async with app_db.tenant_session(tenant.company_id) as session:
        # Valores informados para seq e hashes são ignorados: o gatilho decide.
        await session.execute(
            text(
                "INSERT INTO audit_log (company_id, seq, actor, action, target, payload_digest,"
                " prev_hash, row_hash) VALUES (:c, 999, 'x', 'teste.forca', '', repeat('1', 64),"
                " 'falso', 'falso')"
            ),
            {"c": tenant.company_id},
        )
        row = (await session.execute(text("SELECT seq, prev_hash, row_hash FROM audit_log"))).one()
        report = await verify_chain(session, tenant.company_id)
    assert row[0] == 1
    assert row[1] == audit_service.GENESIS_HASH
    assert row[2] != "falso"
    assert report.ok


async def test_run_cannot_return_to_queue_after_irreversible_step(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Gatilho da fila")
    await publish(platform, tenant, protocolo())
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    job = await PgRunQueue(dispatcher_db).claim_next()
    assert job is not None and job.run_id == run.run_id
    params = {"id": run.run_id}
    async with dispatcher_db.dispatcher_session() as session:
        await session.execute(
            text("UPDATE runs SET irreversible_started_at = now() WHERE id = :id"), params
        )
    for statement in (
        "UPDATE runs SET state = 'queued', lease_expires_at = NULL WHERE id = :id",
        "UPDATE runs SET state = 'queued', lease_expires_at = NULL,"
        " irreversible_started_at = NULL WHERE id = :id",
        "UPDATE runs SET irreversible_started_at = now() - interval '1 hour' WHERE id = :id",
        "UPDATE runs SET attempt = 0 WHERE id = :id",
        "UPDATE runs SET inputs = '{}' WHERE id = :id",
    ):
        for engine in (dispatcher_db.engine, superuser_engine):
            with pytest.raises(DBAPIError) as caught:
                async with engine.begin() as conn:
                    await conn.execute(text(statement), params)
            assert _sqlstate(caught.value) == "23514", statement
    # Concluída, a execução não volta a rodar.
    async with dispatcher_db.dispatcher_session() as session:
        await session.execute(
            text(
                "UPDATE runs SET state = 'indeterminate', finished_at = now(),"
                " lease_expires_at = NULL WHERE id = :id"
            ),
            params,
        )
    with pytest.raises(DBAPIError) as caught:
        async with dispatcher_db.dispatcher_session() as session:
            await session.execute(
                text("UPDATE runs SET state = 'running', lease_expires_at = now() WHERE id = :id"),
                params,
            )
    assert _sqlstate(caught.value) == "23514"
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state.value == "indeterminate"
