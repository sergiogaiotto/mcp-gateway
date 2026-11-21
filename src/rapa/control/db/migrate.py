"""Migrações do banco do plano de controle (Alembic).

Uso pelo operador, como o papel dono das tabelas::

    python -m rapa.control.db.migrate --url postgresql://rapa_owner@host/rapa

``--somente-permissoes`` só reaplica as permissões (por exemplo, depois de criar
os papéis ``rapa_app`` e ``rapa_dispatcher``). Os nomes dos papéis podem mudar
com ``--papel-app`` e ``--papel-despachante`` ou com as variáveis
``RAPA_DB_APP_ROLE`` e ``RAPA_DB_DISPATCHER_ROLE``.
"""

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, pool, text

from rapa.control.db.roles import RoleNames, all_grants_sql
from rapa.control.db.session import psycopg_url

__all__ = ["MIGRATIONS_DIR", "alembic_config", "apply_grants", "main", "upgrade"]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def alembic_config(url: str, roles: RoleNames | None = None) -> Config:
    """Configuração do Alembic sem arquivo ``alembic.ini``."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["url"] = url
    config.attributes["rapa_roles"] = roles or RoleNames.from_env()
    return config


def upgrade(url: str, *, roles: RoleNames | None = None, revision: str = "head") -> None:
    """Aplica as migrações até ``revision``, conectando como o dono das tabelas."""
    command.upgrade(alembic_config(url, roles), revision)


def apply_grants(url: str, roles: RoleNames | None = None) -> None:
    """Reaplica as permissões dos papéis de execução que existirem."""
    engine = create_engine(psycopg_url(url), poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            for statement in all_grants_sql(roles or RoleNames.from_env()):
                conn.execute(text(statement))
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.control.db.migrate",
        description="Aplica as migrações do banco do RaPA.",
    )
    parser.add_argument("--url", default=os.environ.get("RAPA_DATABASE_URL"), help="URL do banco")
    parser.add_argument("--revisao", default="head", help="revisão de destino (padrão: head)")
    parser.add_argument("--papel-app", default=None, help="papel da aplicação (rapa_app)")
    parser.add_argument(
        "--papel-despachante", default=None, help="papel do despachante (rapa_dispatcher)"
    )
    parser.add_argument(
        "--somente-permissoes", action="store_true", help="só reaplica as permissões"
    )
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("informe --url ou RAPA_DATABASE_URL")
    defaults = RoleNames.from_env()
    roles = RoleNames(
        app=args.papel_app or defaults.app,
        dispatcher=args.papel_despachante or defaults.dispatcher,
    )
    if args.somente_permissoes:
        apply_grants(args.url, roles)
    else:
        upgrade(args.url, roles=roles, revision=args.revisao)
    return 0


if __name__ == "__main__":
    sys.exit(main())
