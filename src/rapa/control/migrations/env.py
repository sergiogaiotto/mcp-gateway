"""Ambiente do Alembic para o banco do plano de controle.

Use :func:`rapa.control.db.migrate.upgrade` ou ``python -m
rapa.control.db.migrate``. A URL vem de ``config.attributes["url"]``, da opção
``sqlalchemy.url`` ou da variável ``RAPA_DATABASE_URL``; os nomes dos papéis
vêm de ``config.attributes["rapa_roles"]`` ou das variáveis
``RAPA_DB_APP_ROLE`` e ``RAPA_DB_DISPATCHER_ROLE``.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from rapa.control.db.session import psycopg_url

config = context.config


def _url() -> str:
    url = (
        config.attributes.get("url")
        or config.get_main_option("sqlalchemy.url")
        or os.environ.get("RAPA_DATABASE_URL")
    )
    if not url:
        raise RuntimeError("informe a URL do banco (RAPA_DATABASE_URL)")
    return psycopg_url(str(url))


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        _run(connection)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
