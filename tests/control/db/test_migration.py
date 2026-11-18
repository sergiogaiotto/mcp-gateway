"""Migração: volta atrás sem resto, reaplica e concede permissões pelo utilitário."""

import contextlib
import io
import secrets
from collections.abc import Iterator

import pytest
from alembic import command
from psycopg import sql

from rapa.control.db.migrate import alembic_config, main, upgrade
from rapa.control.db.roles import RoleNames

from .support import PgDatabase

pytestmark = pytest.mark.database


@contextlib.contextmanager
def _scratch_database(pg: PgDatabase) -> Iterator[str]:
    name = f"rapa_migracao_{secrets.token_hex(4)}"
    with pg.server.connect() as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(name), sql.Identifier(pg.owner)
            )
        )
    try:
        yield name
    finally:
        with pg.server.connect() as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _objects(pg: PgDatabase, database: str) -> tuple[int, int]:
    with pg.server.connect(database) as conn:
        tables = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relkind IN ('r', 'p') AND relname <> 'alembic_version'"
        ).fetchone()
        functions = conn.execute(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND proname LIKE 'rapa%%'"
        ).fetchone()
    assert tables is not None and functions is not None
    return int(tables[0]), int(functions[0])


def _app_can_read_runs(pg: PgDatabase, database: str) -> bool:
    with pg.server.connect(database) as conn:
        row = conn.execute(
            "SELECT has_table_privilege(%s, 'runs', 'SELECT')", (pg.roles.app,)
        ).fetchone()
    return bool(row and row[0])


def test_downgrade_and_upgrade_again(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        upgrade(url, roles=pg_database.roles)
        tables, functions = _objects(pg_database, name)
        assert tables > 12 and functions >= 10
        assert _app_can_read_runs(pg_database, name)
        command.downgrade(alembic_config(url, pg_database.roles), "base")
        assert _objects(pg_database, name) == (0, 0)
        upgrade(url, roles=pg_database.roles)
        assert _objects(pg_database, name) == (tables, functions)


def test_grants_skip_missing_roles_and_cli_reapplies(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        # Papéis que ainda não existem: a migração passa e só avisa.
        upgrade(url, roles=RoleNames(app="papel_que_nao_existe", dispatcher="outro_inexistente"))
        assert not _app_can_read_runs(pg_database, name)
        assert (
            main(
                [
                    "--url",
                    url,
                    "--somente-permissoes",
                    "--papel-app",
                    pg_database.roles.app,
                    "--papel-despachante",
                    pg_database.roles.dispatcher,
                ]
            )
            == 0
        )
        assert _app_can_read_runs(pg_database, name)


def test_offline_sql_is_generated(pg_database: PgDatabase) -> None:
    buffer = io.StringIO()
    config = alembic_config(pg_database.owner_url, pg_database.roles)
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    script = buffer.getvalue()
    assert "CREATE TABLE runs" in script
    assert "FORCE ROW LEVEL SECURITY" in script
    assert f'GRANT SELECT, INSERT ON runs TO "{pg_database.roles.app}"' in script
