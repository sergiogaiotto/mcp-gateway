"""Fixtures do PostgreSQL para os testes da persistência.

- Com ``RAPA_TEST_DATABASE_URL`` (CI: superusuário no PostgreSQL 18), cria um
  banco novo e papéis com sufixo próprio para esta sessão de testes, e apaga
  tudo no fim.
- Sem ela, sobe um cluster temporário com os binários locais (``initdb`` e
  ``pg_ctl`` em ``/usr/lib/postgresql/*/bin`` ou no PATH), numa pasta
  temporária e numa porta livre. Sem binários, os testes são pulados.

Nos dois casos são criados os três papéis (dono, ``rapa_app`` e
``rapa_dispatcher``), a migração roda como o dono e os testes recebem um
engine para cada papel.
"""

import contextlib
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from rapa.control.db import Database, create_engine
from rapa.control.db.migrate import upgrade
from rapa.control.db.roles import RoleNames

from .support import PgDatabase, PgServer

_SKIP_MESSAGE = (
    "PostgreSQL não encontrado: defina RAPA_TEST_DATABASE_URL (superusuário) "
    "ou instale os binários do servidor (initdb e pg_ctl)"
)


def _version_key(path: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", path))


def _find_bindir() -> Path | None:
    initdb = shutil.which("initdb")
    if initdb and shutil.which("pg_ctl"):
        return Path(initdb).parent
    candidates = sorted(
        (Path(p) for p in Path("/usr/lib/postgresql").glob("*/bin")),
        key=lambda p: _version_key(str(p)),
        reverse=True,
    )
    for bindir in candidates:
        if (bindir / "initdb").exists() and (bindir / "pg_ctl").exists():
            return bindir
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _temporary_cluster(bindir: Path) -> Iterator[PgServer]:
    run_as: str | None = None
    if os.geteuid() == 0:
        # O initdb não roda como root: usa o usuário "postgres" do sistema.
        try:
            account = pwd.getpwnam("postgres")
        except KeyError:
            pytest.skip("rodando como root sem o usuário 'postgres' para o cluster temporário")
        run_as = account.pw_name
    base = Path(tempfile.mkdtemp(prefix="rapa-pg-"))
    if run_as is not None:
        os.chown(base, account.pw_uid, account.pw_gid)
    data = base / "data"
    port = _free_port()

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603 - binários do próprio PostgreSQL
            list(args), check=True, capture_output=True, text=True, user=run_as, cwd=base
        )

    try:
        run(
            str(bindir / "initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth=trust",
            "-E",
            "UTF8",
            "--no-locale",
        )
        options = (
            f"-p {port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off "
            "-c synchronous_commit=off -c full_page_writes=off -c max_connections=100"
        )
        run(
            str(bindir / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(base / "server.log"),
            "-w",
            "-t",
            "60",
            "-o",
            options,
            "start",
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(base, ignore_errors=True)
        pytest.fail(f"não foi possível subir o PostgreSQL temporário: {exc.stderr}")
    try:
        yield PgServer(host="127.0.0.1", port=port, user="postgres", password=None)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            run(str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop")
        shutil.rmtree(base, ignore_errors=True)


def _server_from_url(url: str) -> PgServer:
    parsed = make_url(url.replace("postgres://", "postgresql://", 1))
    return PgServer(
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 5432,
        user=parsed.username or "postgres",
        password=parsed.password,
        admin_database=parsed.database or "postgres",
    )


def _create_role(conn: psycopg.Connection, name: str, password: str, attributes: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {} " + attributes).format(
            sql.SQL(verb), sql.Identifier(name), sql.Literal(password)
        )
    )


def _drop_database(server: PgServer, name: str, roles: list[str]) -> None:
    with server.connect() as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        for role in roles:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture(scope="session")
def pg_database() -> Iterator[PgDatabase]:
    url = os.environ.get("RAPA_TEST_DATABASE_URL")
    with contextlib.ExitStack() as stack:
        if url:
            server = _server_from_url(url)
            # Servidor compartilhado: papéis com sufixo para não colidir com outra sessão.
            suffix = "_" + secrets.token_hex(3)
        else:
            bindir = _find_bindir()
            if bindir is None:
                pytest.skip(_SKIP_MESSAGE)
            server = stack.enter_context(_temporary_cluster(bindir))
            suffix = ""
        name = f"rapa_test_{secrets.token_hex(4)}"
        owner = f"rapa_owner{suffix}"
        roles = RoleNames(app=f"rapa_app{suffix}", dispatcher=f"rapa_dispatcher{suffix}")
        passwords = {role: secrets.token_hex(16) for role in (owner, roles.app, roles.dispatcher)}
        for _ in range(30):
            try:
                admin = server.connect()
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            pytest.fail("o PostgreSQL de testes não respondeu")
        with admin:
            _create_role(admin, owner, passwords[owner], "NOSUPERUSER NOBYPASSRLS")
            _create_role(
                admin, roles.app, passwords[roles.app], "NOSUPERUSER NOBYPASSRLS NOCREATEDB"
            )
            _create_role(
                admin,
                roles.dispatcher,
                passwords[roles.dispatcher],
                "NOSUPERUSER BYPASSRLS NOCREATEDB",
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                    sql.Identifier(name), sql.Identifier(owner)
                )
            )
        stack.callback(
            _drop_database, server, name, [owner, roles.app, roles.dispatcher] if suffix else []
        )
        database = PgDatabase(
            server=server, name=name, owner=owner, roles=roles, passwords=passwords
        )
        upgrade(database.owner_url, roles=roles)
        yield database


def _engine(url: str) -> AsyncEngine:
    return create_engine(url, poolclass=NullPool)


@pytest.fixture
async def app_db(pg_database: PgDatabase) -> AsyncIterator[Database]:
    """Banco como ``rapa_app`` (sujeito às políticas de isolamento)."""
    db = Database(_engine(pg_database.app_url))
    yield db
    await db.dispose()


@pytest.fixture
async def dispatcher_db(pg_database: PgDatabase) -> AsyncIterator[Database]:
    """Banco como ``rapa_dispatcher`` (sem empresa, só tabelas da fila)."""
    db = Database(_engine(pg_database.dispatcher_url))
    yield db
    await db.dispose()


@pytest.fixture
async def owner_engine(pg_database: PgDatabase) -> AsyncIterator[AsyncEngine]:
    """Engine do dono das tabelas (migrações e capacidade da plataforma)."""
    engine = _engine(pg_database.owner_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def superuser_engine(pg_database: PgDatabase) -> AsyncIterator[AsyncEngine]:
    """Engine de superusuário, só para preparar cenários e inspecionar o catálogo."""
    engine = _engine(pg_database.superuser_url)
    yield engine
    await engine.dispose()
