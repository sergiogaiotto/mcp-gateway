"""Persistência do plano de controle no PostgreSQL.

- :mod:`rapa.control.db.session`: engine e transações por empresa;
- :mod:`rapa.control.db.schema`: tabelas para o SQLAlchemy Core;
- :mod:`rapa.control.db.roles`: papéis do banco e permissões;
- :mod:`rapa.control.db.migrate`: migrações (Alembic).
"""

from rapa.control.db.session import (
    TENANT_SETTING,
    Database,
    create_engine,
    dispatcher_session,
    psycopg_url,
    tenant_session,
)

__all__ = [
    "TENANT_SETTING",
    "Database",
    "create_engine",
    "dispatcher_session",
    "psycopg_url",
    "tenant_session",
]
