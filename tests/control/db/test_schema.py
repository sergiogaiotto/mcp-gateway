"""Esquema migrado: isolamento por linha em toda tabela de empresa, papéis e partições."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.db.schema import TENANT_TABLES, metadata
from rapa.control.services.queue import PgRunQueue

from .support import PgDatabase

pytestmark = pytest.mark.database

_TENANT_TABLES_SQL = text(
    """
    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, c.relispartition,
           coalesce(array_agg(pg_get_expr(p.polqual, p.polrelid)) FILTER (WHERE p.oid IS NOT NULL), '{}'),
           coalesce(array_agg(pg_get_expr(p.polwithcheck, p.polrelid)) FILTER (WHERE p.oid IS NOT NULL), '{}')
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_policy p ON p.polrelid = c.oid AND p.polname NOT LIKE 'retention_sweep_%'
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p')
       AND EXISTS (
           SELECT 1 FROM pg_attribute a
            WHERE a.attrelid = c.oid AND a.attname = 'company_id' AND NOT a.attisdropped
       )
     GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity, c.relispartition
     ORDER BY c.relname
    """
)


async def test_every_company_table_has_forced_rls_and_policy(
    superuser_engine: AsyncEngine,
) -> None:
    async with superuser_engine.connect() as conn:
        rows = (await conn.execute(_TENANT_TABLES_SQL)).all()
        companies = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'companies' AND relnamespace = 'public'::regnamespace"
                )
            )
        ).one()
    names = {row[0] for row in rows}
    expected = {t.name for t in TENANT_TABLES if t.name != "companies"}
    assert expected <= names
    # As partições de eventos também têm company_id e precisam de política própria.
    assert any(row[3] for row in rows), "nenhuma partição de run_events encontrada"
    for relname, enabled, forced, _partition, quals, checks in rows:
        assert enabled, f"{relname} sem ENABLE ROW LEVEL SECURITY"
        assert forced, f"{relname} sem FORCE ROW LEVEL SECURITY"
        assert quals, f"{relname} sem política"
        for expression in [*quals, *checks]:
            assert "rapa.company_id" in expression, f"{relname}: política inesperada"
        assert checks, f"{relname} sem WITH CHECK"
    assert companies == (True, True)


async def test_retention_policies_are_only_for_the_owner_reading_and_deleting(
    superuser_engine: AsyncEngine, pg_database: PgDatabase
) -> None:
    """As únicas políticas fora do isolamento por empresa: as da retenção (revisão 0008).

    Nas duas tabelas que as funções da retenção apagam, só para ler e apagar (nunca
    incluir nem mudar); em ``companies`` e ``run_events``, só para ler (a situação das
    empresas e os eventos, para pular a empresa congelada). Sempre só para o dono das
    tabelas e só com a marca de uma das funções.
    """
    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, p.polname, p.polcmd, pg_get_expr(p.polqual, p.polrelid), "
                    "p.polwithcheck IS NULL, array(SELECT pg_get_userbyid(r) FROM "
                    "unnest(p.polroles) AS r) "
                    "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
                    "WHERE p.polname LIKE 'retention_sweep_%' ORDER BY c.relname, p.polname"
                )
            )
        ).all()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("companies", "retention_sweep_read", "r"),
        ("idempotency_keys", "retention_sweep_delete", "d"),
        ("idempotency_keys", "retention_sweep_read", "r"),
        ("run_events", "retention_sweep_read", "r"),
        ("run_snapshots", "retention_sweep_delete", "d"),
        ("run_snapshots", "retention_sweep_read", "r"),
    ]
    markers = "ARRAY['run_snapshots'::text, 'idempotency_keys'::text, 'run_events'::text]"
    for table, _name, _cmd, qual, no_check, roles in rows:
        if table == "companies":
            assert qual == f"(current_setting('rapa.retention'::text, true) = ANY ({markers}))"
        else:
            assert qual == f"(current_setting('rapa.retention'::text, true) = '{table}'::text)"
        assert no_check
        assert roles == [pg_database.owner]


async def test_runtime_roles_are_not_privileged(
    superuser_engine: AsyncEngine, pg_database: PgDatabase
) -> None:
    async with superuser_engine.connect() as conn:
        roles = {
            row[0]: (row[1], row[2])
            for row in await conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = ANY(:names)"
                ),
                {"names": [pg_database.roles.app, pg_database.roles.dispatcher]},
            )
        }
        owned = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname = ANY(:names)"
            ),
            {"names": [pg_database.roles.app, pg_database.roles.dispatcher]},
        )
    assert roles[pg_database.roles.app] == (False, False)
    assert roles[pg_database.roles.dispatcher] == (False, True)
    assert owned == 0


async def test_metadata_matches_migrated_tables(superuser_engine: AsyncEngine) -> None:
    async with superuser_engine.connect() as conn:
        for table in metadata.sorted_tables:
            columns = set(
                (
                    await conn.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :name"
                        ),
                        {"name": table.name},
                    )
                ).all()
            )
            assert columns == {c.name for c in table.columns}, table.name


async def test_dispatcher_cannot_touch_tenant_admin_tables(dispatcher_db: Database) -> None:
    for statement in (
        # Dos usuários, das credenciais e das aprovações, o despachante só lê o que a
        # conferência do A1 precisa (quem é custodiante e quem aprovou a versão).
        "SELECT display_name FROM users",
        "SELECT active FROM users",
        "SELECT * FROM users",
        "UPDATE users SET active = false",
        "SELECT count(*) FROM memberships",
        "SELECT vault_path FROM credentials",
        "SELECT * FROM credentials",
        "UPDATE credentials SET revoked_at = now()",
        "SELECT authorized_by FROM a1_authorizations",
        "UPDATE a1_authorizations SET revoked_at = now(), revoked_by = 'x'",
        "SELECT approved_at FROM version_approvals",
        "SELECT * FROM version_approvals",
        "DELETE FROM credential_alerts",
        "INSERT INTO companies (id, name) VALUES (gen_random_uuid(), 'x')",
        "UPDATE platform_settings SET total_browser_slots = 1",
        "DELETE FROM run_events",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with dispatcher_db.dispatcher_session() as session:
                await session.execute(text(statement))
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", statement


async def test_event_partitions_are_created_monthly(
    dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    queue = PgRunQueue(dispatcher_db)
    await queue.ensure_event_partitions(5)
    assert await queue.ensure_event_partitions(5) == 0  # idempotente
    async with superuser_engine.connect() as conn:
        partitions = (
            await conn.scalars(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = 'run_events'::regclass ORDER BY c.relname"
                )
            )
        ).all()
        rls = (
            await conn.execute(
                text(
                    "SELECT bool_and(c.relrowsecurity AND c.relforcerowsecurity), "
                    "bool_and(EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid)) "
                    "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                    "WHERE i.inhparent = 'run_events'::regclass"
                )
            )
        ).one()
    now = datetime.now(UTC)
    year, month = now.year, now.month
    expected = []
    for _ in range(6):
        expected.append(f"run_events_{year:04d}_{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    assert set(expected) <= set(partitions)
    assert rls == (True, True)
    with pytest.raises(DBAPIError) as caught:
        async with dispatcher_db.dispatcher_session() as session:
            await session.execute(text("SELECT rapa_ensure_event_partitions(-1)"))
    assert getattr(caught.value.orig, "sqlstate", None) == "22023"


async def test_app_cannot_create_partitions(app_db: Database) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with app_db.dispatcher_session() as session:
            await session.execute(text("SELECT rapa_ensure_event_partitions(1)"))
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_tables_without_company_are_known(superuser_engine: AsyncEngine) -> None:
    """Toda tabela nova sem company_id precisa ser uma decisão consciente."""
    async with superuser_engine.connect() as conn:
        tables = set(
            (
                await conn.scalars(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "WHERE c.relnamespace = 'public'::regnamespace "
                        "AND c.relkind IN ('r', 'p') AND NOT EXISTS ("
                        " SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"
                        " AND a.attname = 'company_id' AND NOT a.attisdropped)"
                    )
                )
            ).all()
        )
    # platform_events (revisão 0007): registros da plataforma, só o dono lê e escreve.
    assert tables == {"companies", "platform_settings", "platform_events", "alembic_version"}


async def test_partitions_are_only_reachable_through_parent(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    async with superuser_engine.connect() as conn:
        partition = await conn.scalar(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'run_events'::regclass ORDER BY c.relname LIMIT 1"
            )
        )
    assert partition is not None
    for db in (app_db, dispatcher_db):
        with pytest.raises(DBAPIError) as caught:
            async with db.dispatcher_session() as session:
                await session.execute(text(f'SELECT count(*) FROM "{partition}"'))  # noqa: S608
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"
