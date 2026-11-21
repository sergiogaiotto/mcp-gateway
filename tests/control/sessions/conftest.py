"""Fixtures do PostgreSQL (as mesmas dos testes da persistência)."""

from tests.control.db.conftest import (  # noqa: F401
    app_db,
    dispatcher_db,
    owner_engine,
    pg_database,
    superuser_engine,
)
