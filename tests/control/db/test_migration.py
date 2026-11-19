"""Migração: volta atrás sem resto, reaplica, concede permissões e preenche o que falta."""

import contextlib
import io
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from psycopg import sql
from sqlalchemy.pool import NullPool

from rapa.control.db import Database, create_engine
from rapa.control.db.migrate import alembic_config, main, upgrade
from rapa.control.db.roles import RoleNames
from rapa.control.services import PgPlatform, PgRunQueue
from rapa.core.contracts import RunTrigger
from rapa.core.events import RunEventKind

from .support import PROTOCOLO_INPUTS, PgDatabase, event, make_tenant, protocolo, publish, result

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


# -- 0002: início do primeiro passo que grava ------------------------------------------


def _write_started_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    """O que a revisão 0002 cria, e o isolamento por linha das tabelas da fila."""
    with pg.server.connect(database) as conn:
        column = conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runs' "
            "AND column_name = 'write_started_at'"
        ).fetchone()
        guard = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'runs'::regclass "
            "AND tgname = 'runs_write_guard'"
        ).fetchone()
        rls = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname IN ('runs', 'run_events') "
            "ORDER BY relname"
        ).fetchall()
        privileges = (
            conn.execute(
                "SELECT has_column_privilege(%s, 'runs', 'write_started_at', 'SELECT'), "
                "has_column_privilege(%s, 'runs', 'write_started_at', 'UPDATE')",
                (pg.roles.app, pg.roles.dispatcher),
            ).fetchone()
            if column
            else None
        )
    assert guard is not None
    return {
        "column": tuple(column) if column else None,
        "guard": int(guard[0]),
        "rls": [tuple(row) for row in rls],
        "privileges": tuple(privileges) if privileges else None,
    }


def _marks(pg: PgDatabase, database: str, ids: dict[str, UUID]) -> dict[str, Any]:
    with pg.server.connect(database) as conn:
        return {
            name: tuple(
                conn.execute(
                    "SELECT state, write_started_at, irreversible_started_at FROM runs "
                    "WHERE id = %s",
                    (run_id,),
                ).fetchone()
                or ()
            )
            for name, run_id in ids.items()
        }


async def _runs_in_every_situation(pg: PgDatabase, database: str) -> dict[str, UUID]:
    """Execuções em cada situação, criadas pelos serviços no banco ``database``."""
    app = Database(
        create_engine(
            pg.server.url(database, pg.roles.app, pg.passwords[pg.roles.app]), poolclass=NullPool
        )
    )
    dispatcher = Database(
        create_engine(
            pg.server.url(database, pg.roles.dispatcher, pg.passwords[pg.roles.dispatcher]),
            poolclass=NullPool,
        )
    )
    try:
        platform = PgPlatform(app)
        queue = PgRunQueue(dispatcher)
        tenant = await make_tenant(app, "Migração", guaranteed_slots=10)
        await publish(platform, tenant, protocolo())
        ids: dict[str, UUID] = {}
        for name in ("leitura", "efeito", "grava", "irreversivel", "concluida", "fila"):
            run = await platform.start(
                tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
            )
            ids[name] = run.run_id
        for _ in range(5):  # todas menos a "fila"
            assert await queue.claim_next() is not None

        def started(name: str, seq: int, **data: Any) -> Any:
            return event(ids[name], 1, seq, RunEventKind.STEP_STARTED, "passo", **data)

        await queue.ingest_events(ids["leitura"], 1, [started("leitura", 0, efeito="read")])
        # Evento de um executor anterior ao campo "grava": o efeito basta.
        await queue.ingest_events(ids["efeito"], 1, [started("efeito", 0, efeito="write")])
        await queue.ingest_events(
            ids["grava"], 1, [started("grava", 0, efeito="write", grava=True)]
        )
        await queue.ingest_events(
            ids["irreversivel"],
            1,
            [started("irreversivel", 0, efeito="irreversible", irreversivel=True)],
        )
        await queue.ingest_events(
            ids["concluida"], 1, [started("concluida", 0, efeito="write", grava=True)]
        )
        assert await queue.finish(ids["concluida"], 1, result(ids["concluida"], 1, "failed"))
        return ids
    finally:
        await app.dispose()
        await dispatcher.dispose()


async def _reap(pg: PgDatabase, database: str) -> Any:
    dispatcher = Database(
        create_engine(
            pg.server.url(database, pg.roles.dispatcher, pg.passwords[pg.roles.dispatcher]),
            poolclass=NullPool,
        )
    )
    try:
        return await PgRunQueue(dispatcher).reap_expired(now=datetime.now(UTC) + timedelta(hours=1))
    finally:
        await dispatcher.dispose()


async def test_write_started_migrates_back_and_forth_and_is_filled_from_the_events(
    pg_database: PgDatabase,
) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        forced = [("run_events", True, True), ("runs", True, True)]
        upgrade(url, roles=pg_database.roles)
        head = _write_started_schema(pg_database, name)
        assert head == {
            "column": ("timestamp with time zone", "YES"),
            "guard": 1,
            "rls": forced,
            "privileges": (True, True),
        }
        ids = await _runs_in_every_situation(pg_database, name)
        before = _marks(pg_database, name, ids)
        for wrote in ("efeito", "grava", "irreversivel", "concluida"):
            assert before[wrote][1] is not None, wrote
        assert before["leitura"][1] is None and before["fila"][1] is None

        # Volta para a 0001: a coluna, a trava e a restrição somem; o isolamento fica.
        command.downgrade(alembic_config(url, pg_database.roles), "0001")
        assert _write_started_schema(pg_database, name) == {
            "column": None,
            "guard": 0,
            "rls": forced,
            "privileges": None,
        }

        # De novo na 0002: a coluna é preenchida a partir dos eventos já gravados, sem
        # desligar o isolamento por linha.
        upgrade(url, roles=pg_database.roles)
        assert _write_started_schema(pg_database, name) == head
        assert _marks(pg_database, name, ids) == before

        # E o recolhedor segue a regra com o que foi preenchido.
        report = await _reap(pg_database, name)
        assert report.requeued == [ids["leitura"]]
        assert sorted(report.failed) == sorted([ids["efeito"], ids["grava"]])
        assert report.indeterminate == [ids["irreversivel"]]
