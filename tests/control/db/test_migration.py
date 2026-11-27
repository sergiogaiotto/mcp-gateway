"""Migração: volta atrás sem resto, reaplica, concede permissões e preenche o que falta."""

import contextlib
import io
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.pool import NullPool

from rapa.control.db import Database, create_engine
from rapa.control.db.migrate import MIGRATIONS_DIR, alembic_config, apply_grants, main, upgrade
from rapa.control.db.roles import RoleNames
from rapa.control.services import PgCredentials, PgPlatform, PgRunQueue
from rapa.core.contracts import CredentialStatus, Principal, Role, RunTrigger
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id

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


def _app_can_read_runs(pg: PgDatabase, database: str, table: str = "runs") -> bool:
    with pg.server.connect(database) as conn:
        row = conn.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT')", (pg.roles.app, table)
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


#: A cadeia das revisões, na ordem: cada uma vem da anterior (uma ponta só).
_REVISIONS = ("0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009")


def test_revisions_form_a_single_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config("postgresql://ninguem@localhost/x"))
    assert script.get_heads() == [_REVISIONS[-1]]
    assert [rev.revision for rev in reversed(list(script.walk_revisions()))] == list(_REVISIONS)
    for previous, current in zip((None, *_REVISIONS), _REVISIONS, strict=False):
        revision = script.get_revision(current)
        assert revision is not None and revision.down_revision == previous, current
    assert {path.name[1:5] for path in (MIGRATIONS_DIR / "versions").glob("v*.py")} == set(
        _REVISIONS
    )


def _catalog(pg: PgDatabase, database: str) -> dict[str, set[tuple[Any, ...]]]:
    """O que a migração cria, para comparar duas revisões: tabelas, colunas, restrições,
    índices, gatilhos, funções (o corpo sem os brancos), políticas e permissões dos papéis
    de execução."""
    roles = [pg.roles.app, pg.roles.dispatcher]
    queries = {
        "tables": (
            "SELECT relname, relkind, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p', 'v') "
            "AND relname <> 'alembic_version'"
        ),
        "columns": (
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped AND c.relname <> 'alembic_version'"
        ),
        "constraints": (
            "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.convalidated "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND c.relname <> 'alembic_version'"
        ),
        "indexes": (
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        ),
        "triggers": (
            "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal"
        ),
        "functions": (
            "SELECT proname, pg_get_function_identity_arguments(oid), prosecdef, "
            "coalesce(array_to_string(proconfig, ','), ''), "
            "md5(regexp_replace(prosrc, '\\s+', '', 'g')), "
            "coalesce((SELECT string_agg(r.rolname || ':' || a.privilege_type, ',' "
            "ORDER BY r.rolname, a.privilege_type) FROM aclexplode(proacl) a "
            "JOIN pg_roles r ON r.oid = a.grantee WHERE r.rolname = ANY(%(roles)s)), '') "
            "FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        ),
        "policies": (
            "SELECT c.relname, p.polname, p.polcmd, p.polpermissive, "
            "array_to_string(array(SELECT pg_get_userbyid(r) FROM unnest(p.polroles) r "
            "ORDER BY 1), ','), pg_get_expr(p.polqual, p.polrelid), "
            "pg_get_expr(p.polwithcheck, p.polrelid) FROM pg_policy p "
            "JOIN pg_class c ON c.oid = p.polrelid"
        ),
        "table_grants": (
            "SELECT c.relname, r.rolname, a.privilege_type "
            "FROM pg_class c, aclexplode(c.relacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE c.relnamespace = 'public'::regnamespace AND r.rolname = ANY(%(roles)s)"
        ),
        "column_grants": (
            "SELECT c.relname, att.attname, r.rolname, a.privilege_type "
            "FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid, "
            "aclexplode(att.attacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE c.relnamespace = 'public'::regnamespace AND att.attacl IS NOT NULL "
            "AND NOT att.attisdropped AND r.rolname = ANY(%(roles)s)"
        ),
    }
    with pg.server.connect(database) as conn:
        return {
            name: {tuple(row) for row in conn.execute(query, {"roles": roles}).fetchall()}
            for name, query in queries.items()
        }


def test_every_revision_goes_back_exactly_and_up_again(pg_database: PgDatabase) -> None:
    """De 0001 a 0009, um passo de cada vez: cada volta deixa o banco exatamente como na
    revisão anterior (tabelas, colunas, restrições, índices, gatilhos, corpos das funções,
    políticas e permissões), e subir de novo dá o mesmo de antes."""
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        config = alembic_config(url, pg_database.roles)
        seen = {"base": _catalog(pg_database, name)}
        for revision in _REVISIONS:
            command.upgrade(config, revision)
            seen[revision] = _catalog(pg_database, name)
        for previous, revision in reversed(
            list(zip(("base", *_REVISIONS), _REVISIONS, strict=False))
        ):
            command.downgrade(config, previous)
            back = _catalog(pg_database, name)
            for part, expected in seen[previous].items():
                assert back[part] == expected, (
                    f"{revision} -> {previous}: {part}",
                    sorted(back[part] ^ expected),
                )
            command.upgrade(config, revision)
            assert _catalog(pg_database, name) == seen[revision], revision
            command.downgrade(config, previous)
        assert _objects(pg_database, name) == (0, 0)
        upgrade(url, roles=pg_database.roles)
        assert _catalog(pg_database, name) == seen[_REVISIONS[-1]]


def test_grants_skip_missing_roles_and_cli_reapplies(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        # Papéis que ainda não existem: a migração passa e só avisa.
        upgrade(url, roles=RoleNames(app="papel_que_nao_existe", dispatcher="outro_inexistente"))
        assert not _app_can_read_runs(pg_database, name)
        assert not _app_can_read_runs(pg_database, name, "recording_sessions")
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
        # As permissões das tabelas das sessões de gravação (revisão 0003) também.
        assert _app_can_read_runs(pg_database, name, "recording_sessions")
        assert _app_can_read_runs(pg_database, name, "recordings")
        # E as da publicação (revisão 0004), as do conserto (revisão 0005) e as do A1 (0006).
        for table in (*_PUBLISHING_TABLES, *_REPAIR_TABLES, *_A1_TABLES):
            assert _app_can_read_runs(pg_database, name, table), table
        # E as da operação por empresa (revisão 0007).
        assert _company_schema(pg_database, name)["grants"] == (
            True,
            False,
            True,
            False,
            False,
            False,
        )


_PUBLISHING_TABLES = (
    "schedules",
    "schedule_occurrences",
    "webhooks",
    "webhook_deliveries",
    "batches",
    "batch_rows",
)


def _publishing_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    with pg.server.connect(database) as conn:
        tables = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s) ORDER BY relname",
            (list(_PUBLISHING_TABLES),),
        ).fetchall()
        privileges = (
            conn.execute(
                "SELECT has_table_privilege(%s, 'schedules', 'INSERT'), "
                "has_table_privilege(%s, 'schedules', 'DELETE'), "
                "has_column_privilege(%s, 'schedules', 'next_run_at', 'SELECT'), "
                "has_column_privilege(%s, 'schedules', 'inputs', 'SELECT'), "
                "has_table_privilege(%s, 'webhooks', 'SELECT')",
                (
                    pg.roles.app,
                    pg.roles.app,
                    pg.roles.dispatcher,
                    pg.roles.dispatcher,
                    pg.roles.dispatcher,
                ),
            ).fetchone()
            if tables
            else None
        )
    return {
        "tables": [tuple(row) for row in tables],
        "privileges": tuple(privileges) if privileges else None,
    }


def test_publishing_tables_migrate_back_and_forth(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        upgrade(url, roles=pg_database.roles)
        head = _publishing_schema(pg_database, name)
        assert head == {
            "tables": sorted((t, True, True) for t in _PUBLISHING_TABLES),
            # A aplicação inclui (nunca apaga); o despachante só lê a fila das agendas.
            "privileges": (True, False, True, False, False),
        }
        command.downgrade(alembic_config(url, pg_database.roles), "0003")
        assert _publishing_schema(pg_database, name) == {"tables": [], "privileges": None}
        with pg_database.server.connect(name) as conn:
            guards = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname IN "
                "('rapa_schedules_guard', 'rapa_webhooks_guard')"
            ).fetchone()
        assert guards is not None and guards[0] == 0
        upgrade(url, roles=pg_database.roles)
        assert _publishing_schema(pg_database, name) == head


_REPAIR_TABLES = ("run_snapshots", "repair_proposals")


def _repair_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    with pg.server.connect(database) as conn:
        tables = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s) ORDER BY relname",
            (list(_REPAIR_TABLES),),
        ).fetchall()
        privileges = (
            conn.execute(
                "SELECT has_table_privilege(%s, 'run_snapshots', 'INSERT'), "
                "has_table_privilege(%s, 'run_snapshots', 'SELECT'), "
                "has_table_privilege(%s, 'repair_proposals', 'INSERT'), "
                "has_table_privilege(%s, 'repair_proposals', 'UPDATE'), "
                "has_table_privilege(%s, 'run_snapshots', 'INSERT'), "
                "has_table_privilege(%s, 'repair_proposals', 'SELECT')",
                (
                    pg.roles.app,
                    pg.roles.app,
                    pg.roles.app,
                    pg.roles.app,
                    pg.roles.dispatcher,
                    pg.roles.dispatcher,
                ),
            ).fetchone()
            if tables
            else None
        )
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname IN "
            "('run_snapshots_append_only', 'repair_proposals_append_only')"
        ).fetchone()
    assert triggers is not None
    return {
        "tables": [tuple(row) for row in tables],
        "privileges": tuple(privileges) if privileges else None,
        "append_only": int(triggers[0]),
    }


def test_repair_tables_migrate_back_and_forth(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        upgrade(url, roles=pg_database.roles)
        head = _repair_schema(pg_database, name)
        assert head == {
            "tables": sorted((t, True, True) for t in _REPAIR_TABLES),
            # A aplicação lê os retratos (nunca os inclui) e inclui consertos (nunca os
            # muda); o despachante só inclui retratos.
            "privileges": (False, True, True, False, True, False),
            "append_only": 2,
        }
        command.downgrade(alembic_config(url, pg_database.roles), "0004")
        assert _repair_schema(pg_database, name) == {
            "tables": [],
            "privileges": None,
            "append_only": 0,
        }
        upgrade(url, roles=pg_database.roles)
        assert _repair_schema(pg_database, name) == head


_A1_TABLES = ("a1_authorizations", "credential_alerts", "version_approvals")
_A1_TRIGGERS = (
    "robot_versions_approvals",
    "credentials_guard",
    "a1_authorizations_guard",
    "version_approvals_check",
    "version_approvals_append_only",
    "credential_alerts_append_only",
)
_A1_COLUMNS = ("not_after", "fingerprint_sha256", "revoked_at", "revoked_by", "updated_at")


def _a1_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    app, dispatcher = pg.roles.app, pg.roles.dispatcher
    with pg.server.connect(database) as conn:
        tables = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s) ORDER BY relname",
            (list(_A1_TABLES),),
        ).fetchall()
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'credentials' AND column_name = ANY(%s) ORDER BY column_name",
            (list(_A1_COLUMNS),),
        ).fetchall()
        triggers = conn.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = ANY(%s)", (list(_A1_TRIGGERS),)
        ).fetchone()
        credentials = conn.execute(
            "SELECT has_table_privilege(%s, 'credentials', 'UPDATE'), "
            "has_table_privilege(%s, 'credentials', 'DELETE')",
            (app, app),
        ).fetchone()
        privileges = (
            conn.execute(
                "SELECT has_column_privilege(%s, 'credentials', 'not_after', 'UPDATE'), "
                "has_column_privilege(%s, 'credentials', 'vault_path', 'UPDATE'), "
                "has_table_privilege(%s, 'version_approvals', 'INSERT'), "
                "has_table_privilege(%s, 'version_approvals', 'UPDATE'), "
                "has_column_privilege(%s, 'a1_authorizations', 'revoked_at', 'UPDATE'), "
                "has_column_privilege(%s, 'a1_authorizations', 'credential_id', 'UPDATE'), "
                "has_table_privilege(%s, 'credential_alerts', 'INSERT'), "
                "has_column_privilege(%s, 'credentials', 'not_after', 'SELECT'), "
                "has_column_privilege(%s, 'credentials', 'vault_path', 'SELECT'), "
                "has_column_privilege(%s, 'a1_authorizations', 'authorized_by', 'SELECT'), "
                "has_table_privilege(%s, 'credential_alerts', 'INSERT'), "
                "has_column_privilege(%s, 'version_approvals', 'approved_by', 'SELECT'), "
                "has_column_privilege(%s, 'version_approvals', 'approved_at', 'SELECT'), "
                "has_column_privilege(%s, 'credentials', 'custodian_user_id', 'SELECT'), "
                "has_column_privilege(%s, 'users', 'subject', 'SELECT'), "
                "has_column_privilege(%s, 'users', 'display_name', 'SELECT')",
                (app, app, app, app, app, app, app, *[dispatcher] * 9),
            ).fetchone()
            if tables
            else None
        )
    assert triggers is not None and credentials is not None
    return {
        "tables": [tuple(row) for row in tables],
        "columns": [row[0] for row in columns],
        "triggers": int(triggers[0]),
        "credentials": tuple(credentials),
        "privileges": tuple(privileges) if privileges else None,
    }


def _credentials_acl(pg: PgDatabase, database: str) -> set[tuple[str, str, str]]:
    """As permissões exatas dos papéis de execução em ``credentials``: (papel, coluna ou
    ``*`` para a tabela, privilégio)."""
    roles = [pg.roles.app, pg.roles.dispatcher]
    with pg.server.connect(database) as conn:
        rows = conn.execute(
            "SELECT r.rolname, '*', a.privilege_type "
            "FROM pg_class c, aclexplode(c.relacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE c.oid = 'credentials'::regclass AND r.rolname = ANY(%s) "
            "UNION ALL "
            "SELECT r.rolname, att.attname, a.privilege_type "
            "FROM pg_attribute att, aclexplode(att.attacl) a JOIN pg_roles r ON r.oid = a.grantee "
            "WHERE att.attrelid = 'credentials'::regclass AND att.attacl IS NOT NULL "
            "AND NOT att.attisdropped "
            "AND r.rolname = ANY(%s)",
            (roles, roles),
        ).fetchall()
    return {(str(role), str(column), str(privilege)) for role, column, privilege in rows}


async def _published_robot(pg: PgDatabase, database: str) -> UUID:
    """Um robô publicado pelos serviços (aprovação de uma pessoa), no banco ``database``."""
    app = Database(
        create_engine(
            pg.server.url(database, pg.roles.app, pg.passwords[pg.roles.app]), poolclass=NullPool
        )
    )
    try:
        tenant = await make_tenant(app, "Aprovações antigas")
        version = await publish(PgPlatform(app), tenant, protocolo())
        return version.robot_id
    finally:
        await app.dispose()


def _approvals_of(pg: PgDatabase, database: str, robot_id: UUID) -> list[tuple[Any, ...]]:
    with pg.server.connect(database) as conn:
        rows = conn.execute(
            "SELECT version_no, approved_by FROM version_approvals WHERE robot_id = %s",
            (robot_id,),
        ).fetchall()
    return [tuple(row) for row in rows]


async def test_a1_rules_migrate_back_and_forth(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        # As permissões de credentials numa 0005 nova (a 0001 continua concedendo o que
        # concedia): a volta da 0006 precisa chegar exatamente aqui.
        command.upgrade(alembic_config(url, pg_database.roles), "0005")
        app, dispatcher = pg_database.roles.app, pg_database.roles.dispatcher
        fresh = _credentials_acl(pg_database, name)
        assert {(role, column) for role, column, _ in fresh} == {(app, "*")}
        assert {privilege for *_, privilege in fresh} >= {"UPDATE", "DELETE"}
        upgrade(url, roles=pg_database.roles)
        head = _a1_schema(pg_database, name)
        acl = _credentials_acl(pg_database, name)
        # Reaplicar as permissões (--somente-permissoes) na 0006 não devolve o UPDATE e o
        # DELETE amplos da aplicação.
        apply_grants(url, pg_database.roles)
        assert _credentials_acl(pg_database, name) == acl
        assert _a1_schema(pg_database, name) == head
        assert head == {
            "tables": sorted((t, True, True) for t in _A1_TABLES),
            "columns": sorted(_A1_COLUMNS),
            "triggers": len(_A1_TRIGGERS),
            # A aplicação não apaga credenciais nem muda as colunas fixas.
            "credentials": (False, False),
            # Aplicação: muda só a validade, inclui aprovações (sem mudar), revoga
            # autorizações (sem trocar a credencial) e só lê os avisos. Despachante: lê a
            # validade e (desde a 0009) o caminho no cofre, nunca quem autorizou, e inclui
            # os avisos; lê quem aprovou a versão e quem é custodiante (o sujeito do
            # usuário), mais nada das aprovações e dos usuários.
            "privileges": (
                *(True, False, True, False, True, False, False, True, True, False, True),
                *(True, False, True, True, False),
            ),
        }
        robot_id = await _published_robot(pg_database, name)
        approver = _approvals_of(pg_database, name, robot_id)
        assert [number for number, _ in approver] == [1]

        command.downgrade(alembic_config(url, pg_database.roles), "0005")
        back = _a1_schema(pg_database, name)
        assert back == {
            "tables": [],
            "columns": [],
            "triggers": 0,
            # Volta o que a revisão 0005 concedia.
            "credentials": (True, True),
            "privileges": None,
        }
        # Exatamente: o despachante não fica lendo as colunas antigas de credentials.
        assert _credentials_acl(pg_database, name) == fresh
        with pg_database.server.connect(name) as conn:
            reads = conn.execute(
                "SELECT has_any_column_privilege(%s, 'credentials', 'SELECT'), "
                "has_any_column_privilege(%s, 'users', 'SELECT')",
                (dispatcher, dispatcher),
            ).fetchone()
        assert reads is not None and tuple(reads) == (False, False)
        with pg_database.server.connect(name) as conn:
            functions = conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname IN ('rapa_robot_versions_approvals', "
                "'rapa_version_approvals_check', 'rapa_a1_authorizations_guard', "
                "'rapa_credentials_guard')"
            ).fetchone()
        assert functions is not None and functions[0] == 0

        # De novo na 0006: a versão publicada antes ganha a aprovação de quem a publicou.
        upgrade(url, roles=pg_database.roles)
        assert _a1_schema(pg_database, name) == head
        assert _approvals_of(pg_database, name, robot_id) == approver


async def test_old_a1_rows_without_validity_stay_and_count_as_expired(
    pg_database: PgDatabase,
) -> None:
    """Um certificado A1 registrado antes da 0006 (sem validade) fica no banco, mas o
    serviço o trata como vencido (falha fechada)."""
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        command.upgrade(alembic_config(url, pg_database.roles), "0005")
        company, area, credential = new_id(), new_id(), new_id()
        with pg_database.server.connect(name) as conn:
            conn.execute("SELECT set_config('rapa.company_id', %s, false)", (str(company),))
            conn.execute("INSERT INTO companies (id, name) VALUES (%s, 'Antiga')", (company,))
            conn.execute(
                "INSERT INTO areas (id, company_id, name) VALUES (%s, %s, 'Fiscal')",
                (area, company),
            )
            conn.execute(
                "INSERT INTO credentials (id, company_id, area_id, name, kind, origin, vault_path)"
                " VALUES (%s, %s, %s, 'certificado', 'pfx_a1', 'https://portal.rapa.test', 'x/y')",
                (credential, company, area),
            )
        upgrade(url, roles=pg_database.roles)
        app = Database(
            create_engine(
                pg_database.server.url(
                    name, pg_database.roles.app, pg_database.passwords[pg_database.roles.app]
                ),
                poolclass=NullPool,
            )
        )
        try:
            admin = Principal(
                subject="admin@antiga",
                company_id=company,
                company_roles=frozenset({Role.COMPANY_ADMIN}),
            )
            credentials = PgCredentials(app)
            [info] = await credentials.list(admin)
            assert info.not_after is None and info.status is CredentialStatus.EXPIRED
            assert [c.credential_id for c in await credentials.expiring(admin)] == [credential]
            # Revogar a linha antiga continua possível (a restrição nova não a barra).
            revoked = await credentials.revoke(admin, credential)
            assert revoked.status is CredentialStatus.REVOKED
        finally:
            await app.dispose()


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


# -- 0007: operação por empresa ---------------------------------------------------------

_COMPANY_COLUMNS = ("idp_organization", "status", "status_changed_at", "status_reason")
_COMPANY_FUNCTIONS = (
    "rapa_company_active_guard",
    "rapa_company_admin_guard",
    "rapa_company_for_organization",
    "rapa_platform_events_guard",
    "rapa_purge_allowed",
)
#: Os gatilhos da empresa congelada (linhas novas, também nas tabelas do A1 da 0006) e
#: do último company_admin ativo.
_COMPANY_TRIGGERS = tuple(
    sorted(
        (
            *(
                f"{table}_company_active"
                for table in (
                    "runs",
                    "robots",
                    "robot_versions",
                    "schedules",
                    "webhooks",
                    "batches",
                    "recording_sessions",
                    "repair_proposals",
                    "credentials",
                    "a1_authorizations",
                    "version_approvals",
                )
            ),
            "memberships_keep_company_admin",
            "users_keep_company_admin",
        )
    )
)


def _company_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    with pg.server.connect(database) as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'companies' AND column_name = ANY(%s) ORDER BY column_name",
                (list(_COMPANY_COLUMNS),),
            ).fetchall()
        ]
        functions = [
            row[0]
            for row in conn.execute(
                "SELECT proname FROM pg_proc WHERE proname = ANY(%s) ORDER BY proname",
                (list(_COMPANY_FUNCTIONS),),
            ).fetchall()
        ]
        policies = [
            row[0]
            for row in conn.execute(
                "SELECT polname FROM pg_policy WHERE polrelid = 'companies'::regclass ORDER BY 1"
            ).fetchall()
        ]
        triggers = [
            row[0]
            for row in conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) ORDER BY tgname",
                (list(_COMPANY_TRIGGERS),),
            ).fetchall()
        ]
        events = conn.execute("SELECT to_regclass('public.platform_events') IS NOT NULL").fetchone()
        index = conn.execute(
            "SELECT to_regclass('public.recording_sessions_company_created') IS NOT NULL"
        ).fetchone()
        dispatcher_id = conn.execute(
            "SELECT has_column_privilege(%s, 'companies', 'id', 'SELECT')", (pg.roles.dispatcher,)
        ).fetchone()
        grants = (
            conn.execute(
                "SELECT has_function_privilege(%s, 'rapa_company_for_organization(text)', "
                "'EXECUTE'), has_function_privilege(%s, 'rapa_company_for_organization(text)', "
                "'EXECUTE'), has_column_privilege(%s, 'companies', 'status', 'SELECT'), "
                "has_table_privilege(%s, 'companies', 'UPDATE'), "
                "has_table_privilege(%s, 'platform_events', 'SELECT'), "
                "has_table_privilege(%s, 'platform_events', 'SELECT')",
                (
                    pg.roles.app,
                    pg.roles.dispatcher,
                    pg.roles.dispatcher,
                    pg.roles.app,
                    pg.roles.app,
                    pg.roles.dispatcher,
                ),
            ).fetchone()
            if functions
            else None
        )
    assert events is not None and index is not None and dispatcher_id is not None
    return {
        "columns": columns,
        "functions": functions,
        "policies": policies,
        "triggers": triggers,
        "platform_events": bool(events[0]),
        "index": bool(index[0]),
        "dispatcher_reads_company_id": bool(dispatcher_id[0]),
        "grants": tuple(grants) if grants else None,
    }


def _owner_purges_audit(pg: PgDatabase, database: str, company: str) -> str | None:
    """Tenta, como dono e com a marca, apagar a auditoria da empresa (e desfaz)."""
    url = pg.server.url(database, pg.owner, pg.passwords[pg.owner])
    with psycopg.connect(url) as conn:
        try:
            conn.execute("SELECT set_config('rapa.company_id', %s, true)", (company,))
            conn.execute("SELECT set_config('rapa.purge_company', %s, true)", (company,))
            conn.execute("DELETE FROM audit_log WHERE company_id = %s", (company,))
        except psycopg.Error as exc:
            return exc.sqlstate
        finally:
            conn.rollback()
    return None


def _a1_guard_accepts_purge(pg: PgDatabase, database: str) -> bool | None:
    """Se a trava das autorizações do A1 (revisão 0006) aceita a saída da empresa."""
    with pg.server.connect(database) as conn:
        row = conn.execute(
            "SELECT prosrc LIKE '%%rapa_purge_allowed%%' FROM pg_proc "
            "WHERE proname = 'rapa_a1_authorizations_guard'"
        ).fetchone()
    return None if row is None else bool(row[0])


def test_company_operation_migrates_back_and_forth(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        config = alembic_config(url, pg_database.roles)
        # Só a 0007, sobre a 0006 (a 0008 tem o próprio teste).
        command.upgrade(config, "0007")
        head = _company_schema(pg_database, name)
        assert _a1_guard_accepts_purge(pg_database, name) is True
        assert head == {
            "columns": list(_COMPANY_COLUMNS),
            "functions": list(_COMPANY_FUNCTIONS),
            "policies": ["organization_lookup", "tenant_isolation"],
            "triggers": list(_COMPANY_TRIGGERS),
            "platform_events": True,
            "index": True,
            "dispatcher_reads_company_id": True,
            # A aplicação chama a função (o despachante, não); o despachante lê a situação; a
            # aplicação nunca muda uma empresa; ninguém além do dono lê platform_events.
            "grants": (True, False, True, False, False, False),
        }
        company = str(uuid4())
        with psycopg.connect(url) as conn:
            conn.execute("SELECT set_config('rapa.company_id', %s, false)", (company,))
            conn.execute("INSERT INTO companies (id, name) VALUES (%s, 'Migração')", (company,))
            conn.execute(
                "INSERT INTO audit_log (company_id, actor, action, payload_digest) "
                "VALUES (%s, 'teste', 'teste.acao', %s)",
                (company, "0" * 64),
            )
            conn.commit()
        # Na 0007, o dono com a marca apaga; de volta na 0006, a trava antiga recusa.
        assert _owner_purges_audit(pg_database, name, company) is None
        command.downgrade(config, "0006")
        # A trava das autorizações do A1 volta a ser a da 0006 (nunca apaga).
        assert _a1_guard_accepts_purge(pg_database, name) is False
        assert _company_schema(pg_database, name) == {
            "columns": [],
            "functions": [],
            "policies": ["tenant_isolation"],
            "triggers": [],
            "platform_events": False,
            "index": False,
            "dispatcher_reads_company_id": False,
            "grants": None,
        }
        assert _owner_purges_audit(pg_database, name, company) == "23514"
        command.upgrade(config, "0007")
        assert _company_schema(pg_database, name) == head
        assert _a1_guard_accepts_purge(pg_database, name) is True
        assert _owner_purges_audit(pg_database, name, company) is None


# -- 0008: retenção --------------------------------------------------------------------


def _retention_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    with pg.server.connect(database) as conn:
        column = conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'runs' "
            "AND column_name IN ('artifacts_expired_at', 'captures_expired_at')"
        ).fetchone()
        functions = conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND proname IN ('rapa_purge_run_snapshots', 'rapa_purge_idempotency_keys', "
            "'rapa_drop_event_partitions', 'rapa_runs_artifacts_guard', "
            "'rapa_run_snapshots_append_only') ORDER BY proname"
        ).fetchall()
        definer = conn.execute(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND prosecdef AND proname IN ('rapa_purge_run_snapshots', "
            "'rapa_purge_idempotency_keys', 'rapa_drop_event_partitions')"
        ).fetchone()
        policies = conn.execute(
            "SELECT count(*) FROM pg_policy WHERE polname LIKE 'retention_sweep_%%'"
        ).fetchone()
        snapshot_guard = conn.execute(
            "SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgname = 'run_snapshots_append_only'"
        ).fetchone()
        execute = (
            conn.execute(
                "SELECT has_function_privilege(%s, 'rapa_purge_run_snapshots(integer, integer)', "
                "'EXECUTE'), has_function_privilege(%s, 'rapa_purge_run_snapshots(integer, "
                "integer)', 'EXECUTE'), has_table_privilege(%s, 'run_snapshots', 'DELETE')",
                (pg.roles.dispatcher, pg.roles.app, pg.roles.dispatcher),
            ).fetchone()
            if functions
            else None
        )
    assert column and definer and policies
    return {
        "columns": int(column[0]),
        "functions": [row[0] for row in functions],
        "definer": int(definer[0]),
        "policies": int(policies[0]),
        "snapshot_guard": snapshot_guard[0] if snapshot_guard else None,
        "execute": tuple(execute) if execute else None,
    }


async def _legacy_run(pg: PgDatabase, database: str) -> UUID:
    """Uma execução na fila, criada pelos serviços no banco ``database``."""
    app = Database(
        create_engine(
            pg.server.url(database, pg.roles.app, pg.passwords[pg.roles.app]), poolclass=NullPool
        )
    )
    try:
        platform = PgPlatform(app)
        tenant = await make_tenant(app, "Caminhos antigos")
        await publish(platform, tenant, protocolo())
        run = await platform.start(
            tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
        )
        return run.run_id
    finally:
        await app.dispose()


async def test_retention_migrates_back_and_forth_and_takes_paths_out(
    pg_database: PgDatabase,
) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        upgrade(url, roles=pg_database.roles, revision="0008")
        head = _retention_schema(pg_database, name)
        assert head == {
            "columns": 2,
            "functions": [
                "rapa_drop_event_partitions",
                "rapa_purge_idempotency_keys",
                "rapa_purge_run_snapshots",
                "rapa_run_snapshots_append_only",
                "rapa_runs_artifacts_guard",
            ],
            "definer": 3,
            # Ler e apagar os retratos e as chaves; ler a situação das empresas e os eventos.
            "policies": 6,
            "snapshot_guard": "rapa_run_snapshots_append_only",
            # Só o despachante executa as funções, e ninguém ganha DELETE nos retratos.
            "execute": (True, False, False),
        }
        run_id = await _legacy_run(pg_database, name)

        command.downgrade(alembic_config(url, pg_database.roles), "0007")
        assert _retention_schema(pg_database, name) == {
            "columns": 0,
            "functions": [],
            "definer": 0,
            "policies": 0,
            "snapshot_guard": "rapa_append_only",
            "execute": None,
        }
        # Como as execuções ficavam antes: o caminho do disco nos artefatos e na saída.
        artifact = {
            "kind": "download",
            "name": "nota.pdf",
            "path": "/tmp/a/nota.pdf",
            "size": 3,
            "sha256": "c" * 64,
        }
        with pg_database.server.connect(name) as conn:
            conn.execute(
                "UPDATE runs SET state = 'succeeded', finished_at = now(), artifacts = %s, "
                "outputs = %s WHERE id = %s",
                (
                    json.dumps([artifact, "estranho"]),
                    json.dumps({"nota": artifact, "total": "3"}),
                    run_id,
                ),
            )
        upgrade(url, roles=pg_database.roles, revision="0008")
        assert _retention_schema(pg_database, name) == head
        with pg_database.server.connect(name) as conn:
            row = conn.execute(
                "SELECT artifacts, outputs FROM runs WHERE id = %s", (run_id,)
            ).fetchone()
        assert row is not None
        stripped = {key: value for key, value in artifact.items() if key != "path"}
        assert row[0] == [stripped, "estranho"]
        assert row[1] == {"nota": "nota.pdf", "total": "3"}


# -- 0009: cofre e chaves ---------------------------------------------------------------


def _vault_schema(pg: PgDatabase, database: str) -> dict[str, Any]:
    app, dispatcher = pg.roles.app, pg.roles.dispatcher
    with pg.server.connect(database) as conn:
        table = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'run_keys' AND relnamespace = 'public'::regnamespace"
        ).fetchone()
        function = conn.execute(
            "SELECT prosecdef FROM pg_proc WHERE proname = 'rapa_purge_run_keys'"
        ).fetchone()
        vault_path = conn.execute(
            "SELECT has_column_privilege(%s, 'credentials', 'vault_path', 'SELECT'), "
            "has_column_privilege(%s, 'credentials', 'vault_path', 'UPDATE')",
            (dispatcher, dispatcher),
        ).fetchone()
        companies = conn.execute(
            "SELECT pg_get_expr(polqual, polrelid) FROM pg_policy "
            "WHERE polrelid = 'companies'::regclass AND polname = 'retention_sweep_read'"
        ).fetchone()
        runs_policy = conn.execute(
            "SELECT count(*) FROM pg_policy "
            "WHERE polrelid = 'runs'::regclass AND polname = 'retention_sweep_read'"
        ).fetchone()
        privileges = (
            conn.execute(
                "SELECT has_table_privilege(%s, 'run_keys', 'SELECT'), "
                "has_table_privilege(%s, 'run_keys', 'INSERT'), "
                "has_table_privilege(%s, 'run_keys', 'SELECT'), "
                "has_table_privilege(%s, 'run_keys', 'INSERT'), "
                "has_table_privilege(%s, 'run_keys', 'UPDATE'), "
                "has_table_privilege(%s, 'run_keys', 'DELETE'), "
                "has_function_privilege(%s, 'rapa_purge_run_keys(integer)', 'EXECUTE'), "
                "has_function_privilege(%s, 'rapa_purge_run_keys(integer)', 'EXECUTE')",
                (app, app, dispatcher, dispatcher, dispatcher, dispatcher, dispatcher, app),
            ).fetchone()
            if table
            else None
        )
    assert vault_path is not None and companies is not None and runs_policy is not None
    return {
        "table": tuple(table) if table else None,
        "definer": function[0] if function else None,
        "vault_path": tuple(vault_path),
        "companies_marks_run_keys": "run_keys" in str(companies[0]),
        "runs_policy": int(runs_policy[0]),
        "privileges": tuple(privileges) if privileges else None,
    }


def test_vault_keys_migrate_back_and_forth(pg_database: PgDatabase) -> None:
    with _scratch_database(pg_database) as name:
        url = pg_database.server.url(
            name, pg_database.owner, pg_database.passwords[pg_database.owner]
        )
        upgrade(url, roles=pg_database.roles)
        head = _vault_schema(pg_database, name)
        assert head == {
            "table": (True, True),
            "definer": True,
            # O despachante lê o caminho no cofre (uma referência), nunca o muda.
            "vault_path": (True, False),
            "companies_marks_run_keys": True,
            "runs_policy": 1,
            # A aplicação só lê as chaves; o despachante lê e inclui, nunca muda nem apaga
            # (só pela função), e só ele executa a função.
            "privileges": (True, False, True, True, False, False, True, False),
        }
        apply_grants(url, pg_database.roles)
        assert _vault_schema(pg_database, name) == head

        command.downgrade(alembic_config(url, pg_database.roles), "0008")
        back = {
            "table": None,
            "definer": None,
            "vault_path": (False, False),
            "companies_marks_run_keys": False,
            "runs_policy": 0,
            "privileges": None,
        }
        assert _vault_schema(pg_database, name) == back
        # Reaplicar as permissões na 0008 não dá ao despachante o caminho no cofre.
        apply_grants(url, pg_database.roles)
        assert _vault_schema(pg_database, name) == back
        upgrade(url, roles=pg_database.roles)
        assert _vault_schema(pg_database, name) == head
