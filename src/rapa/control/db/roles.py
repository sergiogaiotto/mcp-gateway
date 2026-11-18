"""Papéis do banco e as permissões de cada um.

O RaPA usa três papéis, criados pelo operador (a migração nunca cria papéis):

``dono`` (por exemplo ``rapa_owner``)
    Dono das tabelas; roda as migrações. Não é usado pelo tráfego da aplicação.
    As tabelas têm ``FORCE ROW LEVEL SECURITY``, então nem o dono lê dados de
    empresa sem fixar a empresa na transação.

``rapa_app``
    API, gateway MCP e interface. ``NOSUPERUSER NOBYPASSRLS``, sem ser dono de
    nada. Tudo que ele vê passa pela política ``company_id =
    current_setting('rapa.company_id')``.

``rapa_dispatcher``
    Despachante da fila. ``BYPASSRLS`` (atende todas as empresas), mas só lê e
    atualiza execuções, lê versões, robôs e cotas, e inclui eventos e registros
    de auditoria. Lê ``run_events`` e ``audit_log`` porque a deduplicação dos
    eventos e o encadeamento da auditoria consultam a linha anterior.

Exemplo para o operador (PostgreSQL 16 ou mais novo)::

    CREATE ROLE rapa_owner LOGIN PASSWORD '...';
    CREATE ROLE rapa_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    CREATE ROLE rapa_dispatcher LOGIN PASSWORD '...' NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE;
    CREATE DATABASE rapa OWNER rapa_owner;

Depois, como ``rapa_owner``: ``python -m rapa.control.db.migrate --url ...``. Se
os papéis forem criados depois da migração, rode ``python -m
rapa.control.db.migrate --url ... --somente-permissoes``.

As partições de ``run_events`` não recebem permissões: o acesso é sempre pela
tabela-mãe, e cada partição também tem isolamento por linha.
"""

import os
import re
from dataclasses import dataclass

__all__ = ["DEFAULT_APP_ROLE", "DEFAULT_DISPATCHER_ROLE", "RoleNames", "grants_sql"]

DEFAULT_APP_ROLE = "rapa_app"
DEFAULT_DISPATCHER_ROLE = "rapa_dispatcher"

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True, slots=True)
class RoleNames:
    """Nomes dos papéis de execução (configuráveis por ambiente)."""

    app: str = DEFAULT_APP_ROLE
    dispatcher: str = DEFAULT_DISPATCHER_ROLE

    def __post_init__(self) -> None:
        for name in (self.app, self.dispatcher):
            if not _ROLE_NAME.fullmatch(name):
                raise ValueError(f"nome de papel inválido: {name!r}")
        if self.app == self.dispatcher:
            raise ValueError("os papéis da aplicação e do despachante precisam ser diferentes")

    @classmethod
    def from_env(cls) -> RoleNames:
        """Lê ``RAPA_DB_APP_ROLE`` e ``RAPA_DB_DISPATCHER_ROLE`` (com os padrões)."""
        return cls(
            app=os.environ.get("RAPA_DB_APP_ROLE", DEFAULT_APP_ROLE),
            dispatcher=os.environ.get("RAPA_DB_DISPATCHER_ROLE", DEFAULT_DISPATCHER_ROLE),
        )


_APP_GRANTS = """
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT, INSERT ON companies TO {role};
GRANT SELECT, INSERT, UPDATE, DELETE ON areas, users, memberships, credentials TO {role};
GRANT SELECT, INSERT, UPDATE ON company_quotas TO {role};
GRANT SELECT ON platform_settings TO {role};
GRANT SELECT, INSERT, UPDATE ON robots TO {role};
GRANT SELECT, INSERT ON robot_versions TO {role};
GRANT UPDATE (status, approved_by) ON robot_versions TO {role};
GRANT SELECT, INSERT ON runs TO {role};
GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO {role};
GRANT SELECT ON run_events TO {role};
GRANT SELECT, INSERT ON audit_log TO {role};
"""

_DISPATCHER_GRANTS = """
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT, UPDATE ON runs TO {role};
GRANT SELECT ON robots, robot_versions, company_quotas, platform_settings TO {role};
GRANT SELECT, INSERT ON run_events, audit_log TO {role};
GRANT EXECUTE ON FUNCTION rapa_ensure_event_partitions(integer) TO {role};
"""


def _block(role: str, grants: str) -> str:
    body = grants.format(role=f'"{role}"').strip()
    return (
        "DO $grants$\nBEGIN\n"
        f"    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN\n"
        + "\n".join(f"        {line}" for line in body.splitlines())
        + "\n    ELSE\n"
        f"        RAISE NOTICE 'papel % não existe; permissões não concedidas', '{role}';\n"
        "    END IF;\nEND\n$grants$"
    )


def grants_sql(roles: RoleNames) -> list[str]:
    """Blocos SQL que concedem as permissões aos papéis que existirem.

    Papéis ausentes são ignorados com um aviso (``NOTICE``); rodar de novo
    depois de criá-los completa as permissões.
    """
    return [_block(roles.app, _APP_GRANTS), _block(roles.dispatcher, _DISPATCHER_GRANTS)]
