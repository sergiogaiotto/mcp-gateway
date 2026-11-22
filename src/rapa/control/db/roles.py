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

As tabelas das sessões de gravação (revisão 0003) têm um bloco próprio
(:func:`session_grants_sql`), que só concede quando as tabelas existem: assim
``--somente-permissoes`` funciona em qualquer revisão. A aplicação lê e inclui
sessões e gravações, e só muda a situação de uma sessão (e a renova enquanto está
aberta); nunca apaga nada. O despachante só lê a situação das sessões, para contar
as abertas na reserva de vagas interativas e achar as que nenhum processo segura.

As tabelas da publicação (revisão 0004: agendas, webhooks e lotes) também têm um
bloco próprio (:func:`publishing_grants_sql`). A aplicação lê e inclui; só muda as
colunas editáveis das agendas e dos webhooks (nunca a empresa, o robô ou o
autor), e as ocorrências, entregas e lotes só aceitam inclusões. O despachante só
lê a fila das agendas (empresa, identificador, ligada, próxima ocorrência e
arquivada): as ocorrências viram execuções pelo papel da aplicação, numa
transação fixada na empresa da agenda.

As tabelas do conserto (revisão 0005: retratos das falhas e consertos pedidos)
têm o bloco :func:`repair_grants_sql`. O despachante só inclui os retratos, junto
com o resultado da execução; a aplicação lê os retratos e lê e inclui os consertos.
As duas só aceitam inclusões.

As regras do certificado A1 (revisão 0006) têm o bloco :func:`a1_grants_sql`. A
aplicação lê e inclui credenciais, e só muda a validade, a impressão e a revogação
(nunca apaga); lê e inclui autorizações do custodiante e só as revoga; lê e inclui
aprovações de versões; e só lê os avisos de vencimento. O despachante lê só as
colunas de ``credentials`` e de ``a1_authorizations`` de que a conferência antes de
lançar precisa (nunca ``vault_path``) e inclui os avisos de vencimento. O
``UPDATE`` e o ``DELETE`` amplos que :func:`grants_sql` dá à aplicação em
``credentials`` (como na revisão 0001) saem em :func:`a1_revokes_sql`, que só age
quando as tabelas da 0006 existem: assim cada revisão anterior continua com as
permissões dela, e ``--somente-permissoes`` na 0006 não devolve o que ela tirou.

A operação por empresa (revisão 0007) tem o bloco :func:`company_grants_sql`. A
aplicação ganha só o ``EXECUTE`` de ``rapa_company_for_organization`` (achar a empresa
da organização do token, antes de saber a empresa); ela continua sem ``UPDATE`` em
``companies``, então nunca congela nem descongela uma empresa. O despachante lê só
``companies.id`` e ``companies.status``, para não pegar execução de empresa congelada.
``platform_events`` não tem permissão para nenhum dos dois: só o dono lê e escreve.

A retenção (revisão 0008) tem o bloco :func:`retention_grants_sql`: o despachante
ganha só ``EXECUTE`` nas funções que apagam os retratos e as chaves de idempotência
vencidos e removem as partições de eventos antigas (``SECURITY DEFINER``, donas: o
dono das tabelas). Nenhum papel ganha ``DELETE`` nessas tabelas; as marcas de
artefatos expirados são colunas de ``runs``, que o despachante já atualiza.
"""

import os
import re
from dataclasses import dataclass

__all__ = [
    "DEFAULT_APP_ROLE",
    "DEFAULT_DISPATCHER_ROLE",
    "RoleNames",
    "a1_grants_sql",
    "a1_restore_sql",
    "a1_revokes_sql",
    "all_grants_sql",
    "company_grants_sql",
    "company_revoke_sql",
    "grants_sql",
    "publishing_grants_sql",
    "repair_grants_sql",
    "retention_grants_sql",
    "session_grants_sql",
]

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


_SESSION_APP_GRANTS = """
GRANT SELECT, INSERT ON recording_sessions, recordings TO {role};
GRANT UPDATE (status, end_reason, robot_id, version_no, started_at, stopped_at, seen_at)
    ON recording_sessions TO {role};
"""

_SESSION_DISPATCHER_GRANTS = """
GRANT SELECT (company_id, id, status, expires_at, seen_at) ON recording_sessions TO {role};
"""

_SESSION_TABLES = ("recording_sessions", "recordings")

_PUBLISHING_APP_GRANTS = """
GRANT SELECT, INSERT ON schedules, schedule_occurrences, webhooks, webhook_deliveries,
    batches, batch_rows TO {role};
GRANT UPDATE (title, cron, timezone, inputs, enabled, next_run_at, last_occurrence_at,
    updated_by, updated_at, archived_at) ON schedules TO {role};
GRANT UPDATE (title, enabled, rate_per_minute, key_salt, key_version, rotated_at,
    updated_by, updated_at, archived_at) ON webhooks TO {role};
"""

_PUBLISHING_DISPATCHER_GRANTS = """
GRANT SELECT (company_id, id, enabled, next_run_at, archived_at) ON schedules TO {role};
"""

_PUBLISHING_TABLES = (
    "schedules",
    "schedule_occurrences",
    "webhooks",
    "webhook_deliveries",
    "batches",
    "batch_rows",
)


_REPAIR_APP_GRANTS = """
GRANT SELECT ON run_snapshots TO {role};
GRANT SELECT, INSERT ON repair_proposals TO {role};
"""

_REPAIR_DISPATCHER_GRANTS = """
GRANT INSERT ON run_snapshots TO {role};
"""

_REPAIR_TABLES = ("run_snapshots", "repair_proposals")


_A1_APP_GRANTS = """
GRANT UPDATE (not_after, fingerprint_sha256, revoked_at, revoked_by, updated_at)
    ON credentials TO {role};
GRANT SELECT, INSERT ON a1_authorizations, version_approvals TO {role};
GRANT UPDATE (revoked_at, revoked_by) ON a1_authorizations TO {role};
GRANT SELECT ON credential_alerts TO {role};
"""

_A1_DISPATCHER_GRANTS = """
GRANT SELECT (id, company_id, area_id, name, kind, origin, not_after, fingerprint_sha256,
    revoked_at) ON credentials TO {role};
GRANT SELECT (company_id, id, robot_id, credential_id, fingerprint_sha256, revoked_at)
    ON a1_authorizations TO {role};
GRANT SELECT, INSERT ON credential_alerts TO {role};
"""

#: A aplicação deixa de apagar credenciais e de mudar as colunas fixas (revisão 0006).
#: Vem antes de :data:`_A1_APP_GRANTS`: tirar o ``UPDATE`` da tabela tira também o das
#: colunas.
_A1_APP_REVOKES = """
REVOKE UPDATE, DELETE ON credentials FROM {role};
"""

#: O que a revisão 0005 concedia sobre as credenciais (para voltar atrás da 0006).
_A1_APP_RESTORE = """
GRANT UPDATE, DELETE ON credentials TO {role};
"""

#: Na revisão 0005 o despachante não lia nada de ``credentials``. Apagar as colunas
#: novas leva só as permissões delas; as das colunas antigas saem aqui.
_A1_DISPATCHER_RESTORE = """
REVOKE ALL ON credentials FROM {role};
"""

_A1_TABLES = ("a1_authorizations", "version_approvals", "credential_alerts")

_COMPANY_APP_GRANTS = """
GRANT EXECUTE ON FUNCTION rapa_company_for_organization(text) TO {role};
"""

_COMPANY_DISPATCHER_GRANTS = """
GRANT SELECT (id, status) ON companies TO {role};
"""

#: A tabela que a revisão 0007 cria (junto com a função e as colunas que os blocos usam).
_COMPANY_TABLES = ("platform_events",)

_RETENTION_DISPATCHER_GRANTS = """
GRANT EXECUTE ON FUNCTION rapa_purge_run_snapshots(integer, integer) TO {role};
GRANT EXECUTE ON FUNCTION rapa_purge_idempotency_keys(integer) TO {role};
GRANT EXECUTE ON FUNCTION rapa_drop_event_partitions(integer) TO {role};
"""

_RETENTION_FUNCTIONS = (
    "rapa_purge_run_snapshots(integer, integer)",
    "rapa_purge_idempotency_keys(integer)",
    "rapa_drop_event_partitions(integer)",
)


def _block(
    role: str,
    grants: str,
    *,
    tables: tuple[str, ...] = (),
    functions: tuple[str, ...] = (),
    what: str = "das sessões",
) -> str:
    body = grants.format(role=f'"{role}"').strip()
    condition = f"EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}')"
    missing = "papel % não existe; permissões não concedidas"
    for table in tables:
        condition += f" AND to_regclass('public.{table}') IS NOT NULL"
    for function in functions:
        condition += f" AND to_regprocedure('public.{function}') IS NOT NULL"
    if tables or functions:
        objects = "funções" if functions and not tables else "tabelas"
        missing = f"papel % ou {objects} {what} ausentes; permissões não concedidas"
    return (
        "DO $grants$\nBEGIN\n"
        f"    IF {condition} THEN\n"
        + "\n".join(f"        {line}" for line in body.splitlines())
        + "\n    ELSE\n"
        f"        RAISE NOTICE '{missing}', '{role}';\n"
        "    END IF;\nEND\n$grants$"
    )


def grants_sql(roles: RoleNames) -> list[str]:
    """Blocos SQL que concedem as permissões aos papéis que existirem.

    Papéis ausentes são ignorados com um aviso (``NOTICE``); rodar de novo
    depois de criá-los completa as permissões.
    """
    return [_block(roles.app, _APP_GRANTS), _block(roles.dispatcher, _DISPATCHER_GRANTS)]


def session_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões das tabelas das sessões de gravação (revisão 0003), se elas existirem."""
    return [
        _block(roles.app, _SESSION_APP_GRANTS, tables=_SESSION_TABLES),
        _block(roles.dispatcher, _SESSION_DISPATCHER_GRANTS, tables=_SESSION_TABLES),
    ]


def publishing_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões das agendas, webhooks e lotes (revisão 0004), se as tabelas existirem."""
    what = "da publicação"
    return [
        _block(roles.app, _PUBLISHING_APP_GRANTS, tables=_PUBLISHING_TABLES, what=what),
        _block(
            roles.dispatcher, _PUBLISHING_DISPATCHER_GRANTS, tables=_PUBLISHING_TABLES, what=what
        ),
    ]


def repair_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões dos retratos das falhas e dos consertos (revisão 0005), se existirem."""
    what = "do conserto"
    return [
        _block(roles.app, _REPAIR_APP_GRANTS, tables=_REPAIR_TABLES, what=what),
        _block(roles.dispatcher, _REPAIR_DISPATCHER_GRANTS, tables=_REPAIR_TABLES, what=what),
    ]


def a1_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões das regras do certificado A1 (revisão 0006), se as tabelas existirem."""
    what = "do certificado A1"
    return [
        _block(roles.app, _A1_APP_GRANTS, tables=_A1_TABLES, what=what),
        _block(roles.dispatcher, _A1_DISPATCHER_GRANTS, tables=_A1_TABLES, what=what),
    ]


def a1_revokes_sql(roles: RoleNames) -> list[str]:
    """Tira da aplicação o ``UPDATE`` e o ``DELETE`` amplos de ``credentials`` (revisão 0006).

    Só quando as tabelas da 0006 existem: numa revisão anterior, :func:`grants_sql`
    (que a 0001 aplica) continua concedendo o que concedia. Roda antes de
    :func:`a1_grants_sql`, que devolve o ``UPDATE`` das colunas que mudam.
    """
    return [_block(roles.app, _A1_APP_REVOKES, tables=_A1_TABLES, what="do certificado A1")]


def a1_restore_sql(roles: RoleNames) -> list[str]:
    """Volta as permissões de ``credentials`` às da revisão 0005 (o ``downgrade`` da 0006).

    A aplicação recebe de novo o ``UPDATE`` e o ``DELETE``; o despachante perde a
    leitura das colunas antigas (as novas somem com elas).
    """
    return [
        _block(roles.app, _A1_APP_RESTORE),
        _block(roles.dispatcher, _A1_DISPATCHER_RESTORE),
    ]


def company_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões da operação por empresa (revisão 0007), se ela já foi aplicada."""
    what = "da operação por empresa"
    return [
        _block(roles.app, _COMPANY_APP_GRANTS, tables=_COMPANY_TABLES, what=what),
        _block(roles.dispatcher, _COMPANY_DISPATCHER_GRANTS, tables=_COMPANY_TABLES, what=what),
    ]


def company_revoke_sql(roles: RoleNames) -> list[str]:
    """Tira o que :func:`company_grants_sql` deu e não some com as colunas (volta da 0007)."""
    return [_block(roles.dispatcher, "REVOKE SELECT (id, status) ON companies FROM {role};")]


def retention_grants_sql(roles: RoleNames) -> list[str]:
    """Permissões das funções da retenção (revisão 0008), se elas existirem."""
    return [
        _block(
            roles.dispatcher,
            _RETENTION_DISPATCHER_GRANTS,
            functions=_RETENTION_FUNCTIONS,
            what="da retenção",
        )
    ]


def all_grants_sql(roles: RoleNames) -> list[str]:
    """Todas as permissões, das tabelas que existirem na revisão atual do banco."""
    return [
        *grants_sql(roles),
        *session_grants_sql(roles),
        *publishing_grants_sql(roles),
        *repair_grants_sql(roles),
        *a1_revokes_sql(roles),
        *a1_grants_sql(roles),
        *company_grants_sql(roles),
        *retention_grants_sql(roles),
    ]
