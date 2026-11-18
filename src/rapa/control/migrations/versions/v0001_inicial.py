"""Esquema inicial do plano de controle.

Revisão: 0001
Anterior: nenhuma

Escrito à mão: isolamento por linha (ENABLE e FORCE em toda tabela de empresa),
gatilhos de imutabilidade, cadeia de hashes da auditoria e partições mensais de
eventos não cabem no autogenerate. Os valores dos enums ficam copiados aqui de
propósito: uma migração não muda depois de aplicada.

A migração não cria papéis. Ela concede permissões aos papéis configurados
(``rapa_app`` e ``rapa_dispatcher`` por padrão) que já existirem; ver
:mod:`rapa.control.db.roles`.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, grants_sql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = (
    "platform_admin",
    "company_admin",
    "area_admin",
    "creator",
    "approver",
    "operator",
    "auditor",
    "viewer",
    "credential_custodian",
)
_ROBOT_STATUS = ("draft", "published", "disabled", "needs_repair")
_VERSION_STATUS = ("draft", "validated", "published", "rejected", "superseded")
_RUN_STATES = (
    "queued",
    "running",
    "awaiting_human",
    "succeeded",
    "failed",
    "cancelled",
    "indeterminate",
)
_FINISHED = ("succeeded", "failed", "cancelled", "indeterminate")
_TRIGGERS = ("mcp", "rest", "schedule", "webhook", "batch", "manual", "validation")
_MODES = ("full", "until_first_write")
_CREDENTIAL_KINDS = ("password", "totp", "pfx_a1")

#: Tabelas com company_id, na ordem de criação.
_TENANT_TABLES = (
    "areas",
    "users",
    "memberships",
    "company_quotas",
    "robots",
    "robot_versions",
    "credentials",
    "runs",
    "idempotency_keys",
    "run_events",
    "audit_log",
)

_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"

# Dobra de acentos sem a extensão unaccent: maiúsculas e minúsculas acentuadas
# viram a letra base; "_" vira espaço para separar as palavras do nome técnico.
_ACCENTS = {
    "a": "áàâãäå",
    "e": "éèêë",
    "i": "íìîï",
    "o": "óòôõö",
    "u": "úùûü",
    "c": "ç",
    "n": "ñ",
    "y": "ýÿ",
}


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _fold_maps() -> tuple[str, str]:
    source, target = [], []
    for base, chars in _ACCENTS.items():
        for char in chars:
            source += [char, char.upper()]
            target += [base, base.upper()]
    source.append("_")
    target.append(" ")
    return "".join(source), "".join(target)


def _functions() -> list[str]:
    source, target = _fold_maps()
    return [
        f"""
        CREATE FUNCTION rapa_fold(value text) RETURNS text
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            RETURN lower(translate(value, '{source}', '{target}'))
        """,
        """
        CREATE FUNCTION rapa_robot_search_document(manifest jsonb) RETURNS tsvector
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            RETURN to_tsvector(
                'portuguese'::regconfig,
                rapa_fold(
                    coalesce(manifest ->> 'name', '') || ' '
                    || coalesce(manifest ->> 'title', '') || ' '
                    || coalesce(manifest ->> 'description', '')
                )
            )
        """,
        """
        CREATE FUNCTION rapa_lp(value text) RETURNS text
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            RETURN length(value)::text || ':' || value || ';'
        """,
        """
        CREATE FUNCTION rapa_audit_hash(
            prev_hash text, company_id uuid, seq bigint, at timestamptz,
            actor text, action text, target text, payload_digest text
        ) RETURNS text
            LANGUAGE sql STABLE STRICT PARALLEL SAFE
            RETURN encode(sha256(convert_to(
                rapa_lp(prev_hash) || rapa_lp(company_id::text) || rapa_lp(seq::text)
                || rapa_lp(to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
                || rapa_lp(actor) || rapa_lp(action) || rapa_lp(target)
                || rapa_lp(payload_digest),
                'UTF8')), 'hex')
        """,
    ]


def _tables() -> list[str]:
    return [
        """
        CREATE TABLE companies (
            id uuid PRIMARY KEY,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE areas (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES companies (id),
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (company_id, id),
            UNIQUE (company_id, name)
        )
        """,
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES companies (id),
            subject text NOT NULL UNIQUE CHECK (length(subject) BETWEEN 1 AND 255),
            display_name text NOT NULL DEFAULT '',
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (company_id, id)
        )
        """,
        f"""
        CREATE TABLE memberships (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            area_id uuid,
            user_id uuid NOT NULL,
            role text NOT NULL CHECK (role IN ({_in(_ROLES)})),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (company_id, user_id) REFERENCES users (company_id, id) ON DELETE CASCADE,
            FOREIGN KEY (company_id, area_id) REFERENCES areas (company_id, id) ON DELETE CASCADE,
            UNIQUE NULLS NOT DISTINCT (company_id, user_id, area_id, role)
        )
        """,
        "CREATE INDEX memberships_user ON memberships (user_id)",
        """
        CREATE TABLE company_quotas (
            company_id uuid PRIMARY KEY REFERENCES companies (id),
            guaranteed_slots integer NOT NULL DEFAULT 1 CHECK (guaranteed_slots >= 0),
            max_borrow_factor numeric(5, 2) NOT NULL DEFAULT 2.0 CHECK (max_borrow_factor >= 1),
            max_queued integer NOT NULL DEFAULT 1000 CHECK (max_queued >= 0),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE platform_settings (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            total_browser_slots integer NOT NULL CHECK (total_browser_slots > 0),
            interactive_reserve_ratio numeric(4, 3) NOT NULL DEFAULT 0.10
                CHECK (interactive_reserve_ratio >= 0 AND interactive_reserve_ratio < 1),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        f"""
        CREATE TABLE robots (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES companies (id),
            area_id uuid NOT NULL,
            slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9_]{{2,63}}$'),
            title text NOT NULL,
            status text NOT NULL DEFAULT 'draft' CHECK (status IN ({_in(_ROBOT_STATUS)})),
            current_version_no integer CHECK (current_version_no >= 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (company_id, area_id) REFERENCES areas (company_id, id),
            UNIQUE (company_id, id),
            UNIQUE (company_id, slug),
            CHECK (status <> 'published' OR current_version_no IS NOT NULL)
        )
        """,
        f"""
        CREATE TABLE robot_versions (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            robot_id uuid NOT NULL,
            version_no integer NOT NULL CHECK (version_no >= 1),
            status text NOT NULL DEFAULT 'draft' CHECK (status IN ({_in(_VERSION_STATUS)})),
            manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
            fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{{64}}$'),
            violations jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(violations) = 'array'),
            created_by text NOT NULL,
            approved_by text,
            created_at timestamptz NOT NULL DEFAULT now(),
            search_document tsvector
                GENERATED ALWAYS AS (rapa_robot_search_document(manifest)) STORED,
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            UNIQUE (company_id, id),
            UNIQUE (company_id, robot_id, id),
            UNIQUE (robot_id, version_no),
            CHECK (approved_by IS NULL OR approved_by <> created_by)
        )
        """,
        """
        CREATE UNIQUE INDEX robot_versions_one_published
            ON robot_versions (robot_id) WHERE status = 'published'
        """,
        """
        CREATE INDEX robot_versions_search
            ON robot_versions USING gin (search_document) WHERE status = 'published'
        """,
        f"""
        CREATE TABLE credentials (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            area_id uuid NOT NULL,
            name text NOT NULL CHECK (name ~ '^[a-z_][a-z0-9_]{{0,63}}$'),
            kind text NOT NULL CHECK (kind IN ({_in(_CREDENTIAL_KINDS)})),
            origin text NOT NULL CHECK (origin ~ '^https?://[^/?#@]+$'),
            vault_path text NOT NULL
                CHECK (length(vault_path) <= 500 AND vault_path ~ '^[A-Za-z0-9_.:/-]+$'),
            custodian_user_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (company_id, area_id) REFERENCES areas (company_id, id),
            FOREIGN KEY (company_id, custodian_user_id) REFERENCES users (company_id, id),
            UNIQUE (company_id, area_id, name)
        )
        """,
        f"""
        CREATE TABLE runs (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            area_id uuid NOT NULL,
            robot_id uuid NOT NULL,
            version_id uuid NOT NULL,
            trigger text NOT NULL CHECK (trigger IN ({_in(_TRIGGERS)})),
            mode text NOT NULL DEFAULT 'full' CHECK (mode IN ({_in(_MODES)})),
            priority smallint NOT NULL CHECK (priority BETWEEN 0 AND 3),
            state text NOT NULL DEFAULT 'queued' CHECK (state IN ({_in(_RUN_STATES)})),
            attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            requested_by text NOT NULL,
            inputs jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(inputs) = 'object'),
            outputs jsonb CHECK (outputs IS NULL OR jsonb_typeof(outputs) = 'object'),
            error_code text,
            error_message text,
            failed_step_id text,
            steps_done integer NOT NULL DEFAULT 0 CHECK (steps_done >= 0),
            artifacts jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(artifacts) = 'array'),
            idempotency_key text,
            irreversible_started_at timestamptz,
            lease_expires_at timestamptz,
            deadline_at timestamptz,
            queued_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (company_id, robot_id, version_id)
                REFERENCES robot_versions (company_id, robot_id, id),
            FOREIGN KEY (company_id, area_id) REFERENCES areas (company_id, id),
            UNIQUE (company_id, id),
            CHECK (state NOT IN ('running', 'awaiting_human') OR lease_expires_at IS NOT NULL),
            CHECK (state NOT IN ({_in(_FINISHED)}) OR finished_at IS NOT NULL),
            CHECK (state <> 'queued' OR irreversible_started_at IS NULL)
        )
        """,
        "CREATE INDEX runs_queue ON runs (priority, queued_at, id) WHERE state = 'queued'",
        """
        CREATE INDEX runs_active ON runs (company_id, priority)
            WHERE state IN ('queued', 'running', 'awaiting_human')
        """,
        """
        CREATE INDEX runs_lease ON runs (lease_expires_at)
            WHERE state IN ('running', 'awaiting_human')
        """,
        "CREATE INDEX runs_company_created ON runs (company_id, created_at DESC)",
        """
        CREATE TABLE idempotency_keys (
            company_id uuid NOT NULL REFERENCES companies (id),
            scope text NOT NULL CHECK (length(scope) BETWEEN 1 AND 100),
            key text NOT NULL CHECK (length(key) BETWEEN 1 AND 255),
            request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            run_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            PRIMARY KEY (company_id, scope, key),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id)
                DEFERRABLE INITIALLY DEFERRED
        )
        """,
        "CREATE INDEX idempotency_keys_expires ON idempotency_keys (expires_at)",
        # A chave única de um evento é (run_id, seq). O PostgreSQL exige a
        # coluna de particionamento em toda restrição única, então a chave
        # primária inclui "at" e o gatilho rapa_run_events_unique_seq garante a
        # unicidade de (run_id, seq) entre partições.
        """
        CREATE TABLE run_events (
            company_id uuid NOT NULL,
            run_id uuid NOT NULL,
            attempt integer NOT NULL CHECK (attempt >= 1),
            seq integer NOT NULL CHECK (seq >= 0),
            at timestamptz NOT NULL,
            kind text NOT NULL CHECK (kind ~ '^[a-z][a-z_]{0,39}$'),
            step_id text,
            data jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
            ingested_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, seq, at),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id)
        ) PARTITION BY RANGE (at)
        """,
        """
        CREATE TABLE audit_log (
            company_id uuid NOT NULL REFERENCES companies (id),
            seq bigint NOT NULL DEFAULT 0,
            at timestamptz NOT NULL DEFAULT now(),
            actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 255),
            action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_.]{1,80}$'),
            target text NOT NULL DEFAULT '',
            payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
            prev_hash text NOT NULL DEFAULT '',
            row_hash text NOT NULL DEFAULT '',
            PRIMARY KEY (company_id, seq)
        )
        """,
    ]


def _row_level_security() -> list[str]:
    statements = [
        "ALTER TABLE companies ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE companies FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON companies
            USING (id = {_TENANT}) WITH CHECK (id = {_TENANT})
        """,
    ]
    for table in _TENANT_TABLES:
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (company_id = {_TENANT}) WITH CHECK (company_id = {_TENANT})
            """,
        ]
    return statements


def _triggers() -> list[str]:
    finished = _in(_FINISHED)
    return [
        # -- versões de robô: imutáveis, exceto estado e aprovador -------------
        """
        CREATE FUNCTION rapa_robot_versions_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'versões de robô não podem ser apagadas';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.version_no IS DISTINCT FROM OLD.version_no
                OR NEW.manifest IS DISTINCT FROM OLD.manifest
                OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
                OR NEW.violations IS DISTINCT FROM OLD.violations
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a versão do robô é imutável: só o estado e o aprovador podem mudar';
            END IF;
            IF OLD.approved_by IS NOT NULL AND NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o aprovador de uma versão não pode ser trocado';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER robot_versions_guard
            BEFORE UPDATE OR DELETE ON robot_versions
            FOR EACH ROW EXECUTE FUNCTION rapa_robot_versions_guard()
        """,
        # -- execuções: nada que gravou volta para a fila ----------------------
        f"""
        CREATE FUNCTION rapa_runs_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.area_id IS DISTINCT FROM OLD.area_id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.version_id IS DISTINCT FROM OLD.version_id
                OR NEW.trigger IS DISTINCT FROM OLD.trigger
                OR NEW.mode IS DISTINCT FROM OLD.mode
                OR NEW.inputs IS DISTINCT FROM OLD.inputs
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a execução não pode trocar de empresa, robô, versão, origem ou entradas';
            END IF;
            IF NEW.attempt < OLD.attempt THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o número da tentativa de uma execução nunca diminui';
            END IF;
            IF OLD.irreversible_started_at IS NOT NULL THEN
                IF NEW.irreversible_started_at IS DISTINCT FROM OLD.irreversible_started_at THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'o início do passo irreversível de uma execução não pode ser alterado';
                END IF;
                IF NEW.state = 'queued' THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'uma execução que já começou um passo irreversível não pode voltar para a fila';
                END IF;
            END IF;
            IF OLD.state IN ({finished}) AND NEW.state NOT IN ({finished}) THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma execução concluída não volta a rodar';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER runs_guard
            BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION rapa_runs_guard()
        """,
        # -- eventos: (run_id, seq) único entre partições ----------------------
        """
        CREATE FUNCTION rapa_run_events_unique_seq() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(7101, hashtext(NEW.run_id::text));
            IF EXISTS (
                SELECT 1 FROM run_events e WHERE e.run_id = NEW.run_id AND e.seq = NEW.seq
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'unique_violation',
                    MESSAGE = format('a execução %s já tem o evento número %s', NEW.run_id, NEW.seq);
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER run_events_unique_seq
            BEFORE INSERT ON run_events
            FOR EACH ROW EXECUTE FUNCTION rapa_run_events_unique_seq()
        """,
        # -- auditoria: cadeia de hashes por empresa, só inclusões -------------
        """
        CREATE FUNCTION rapa_audit_chain() RETURNS trigger
            LANGUAGE plpgsql AS $$
        DECLARE
            last_seq bigint;
            last_hash text;
        BEGIN
            PERFORM pg_advisory_xact_lock(7102, hashtext(NEW.company_id::text));
            SELECT a.seq, a.row_hash INTO last_seq, last_hash
              FROM audit_log a
             WHERE a.company_id = NEW.company_id
             ORDER BY a.seq DESC
             LIMIT 1;
            NEW.seq := coalesce(last_seq, 0) + 1;
            NEW.prev_hash := coalesce(last_hash, repeat('0', 64));
            NEW.at := clock_timestamp();
            NEW.row_hash := rapa_audit_hash(
                NEW.prev_hash, NEW.company_id, NEW.seq, NEW.at,
                NEW.actor, NEW.action, NEW.target, NEW.payload_digest
            );
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER audit_log_chain
            BEFORE INSERT ON audit_log
            FOR EACH ROW EXECUTE FUNCTION rapa_audit_chain()
        """,
        """
        CREATE FUNCTION rapa_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = format('a tabela %s só aceita inclusões', TG_TABLE_NAME);
        END
        $$
        """,
        """
        CREATE TRIGGER audit_log_append_only
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
        """,
        """
        CREATE TRIGGER audit_log_no_truncate
            BEFORE TRUNCATE ON audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION rapa_append_only()
        """,
    ]


def _partitions() -> list[str]:
    # A política vai dentro de um literal de format(): aspas simples dobradas.
    tenant = _TENANT.replace("'", "''")
    return [
        # SECURITY DEFINER: o despachante (sem CREATE no esquema) pode criar as
        # partições do mês seguinte. Cada partição ganha isolamento por linha
        # próprio, porque partições não herdam ENABLE/FORCE nem políticas.
        f"""
        CREATE FUNCTION rapa_ensure_event_partitions(months_ahead integer) RETURNS integer
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, public
            SET timezone = 'UTC'
        AS $$
        DECLARE
            first_month timestamptz := date_trunc('month', now());
            month_start timestamptz;
            part text;
            created integer := 0;
        BEGIN
            IF months_ahead IS NULL OR months_ahead < 0 OR months_ahead > 36 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'months_ahead deve estar entre 0 e 36';
            END IF;
            PERFORM pg_advisory_xact_lock(7103, 0);
            FOR i IN 0..months_ahead LOOP
                month_start := first_month + make_interval(months => i);
                part := 'run_events_' || to_char(month_start, 'YYYY_MM');
                IF to_regclass('public.' || quote_ident(part)) IS NULL THEN
                    EXECUTE format(
                        'CREATE TABLE public.%I PARTITION OF public.run_events FOR VALUES FROM (%L) TO (%L)',
                        part, month_start, month_start + interval '1 month');
                    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', part);
                    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', part);
                    EXECUTE format(
                        'CREATE POLICY tenant_isolation ON public.%I USING (company_id = {tenant}) WITH CHECK (company_id = {tenant})',
                        part);
                    created := created + 1;
                END IF;
            END LOOP;
            RETURN created;
        END
        $$
        """,
        "REVOKE ALL ON FUNCTION rapa_ensure_event_partitions(integer) FROM PUBLIC",
        "SELECT rapa_ensure_event_partitions(2)",
    ]


def _comments() -> list[str]:
    return [
        "COMMENT ON TABLE runs IS 'Execuções; também é a fila do despachante'",
        "COMMENT ON COLUMN runs.attempt IS 'Token de cerca: escritas de tentativas antigas são recusadas'",
        "COMMENT ON TABLE credentials IS 'Referências ao cofre; nunca guarda o valor de um segredo'",
        "COMMENT ON TABLE audit_log IS 'Auditoria encadeada por empresa; só aceita inclusões'",
        "COMMENT ON TABLE run_events IS 'Eventos de execução, particionados por mês'",
        "COMMENT ON TABLE platform_settings IS 'Capacidade da plataforma (linha única)'",
    ]


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def upgrade() -> None:
    statements = [
        *_functions(),
        *_tables(),
        *_row_level_security(),
        *_triggers(),
        *_partitions(),
        "INSERT INTO platform_settings (id, total_browser_slots) VALUES (true, 30)",
        *_comments(),
        *grants_sql(_roles()),
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS audit_log, run_events, idempotency_keys, runs, credentials,"
        " robot_versions, robots, platform_settings, company_quotas, memberships, users,"
        " areas, companies CASCADE",
        "DROP FUNCTION IF EXISTS rapa_ensure_event_partitions(integer), rapa_append_only(),"
        " rapa_audit_chain(), rapa_run_events_unique_seq(), rapa_runs_guard(),"
        " rapa_robot_versions_guard()",
        "DROP FUNCTION IF EXISTS rapa_audit_hash(text, uuid, bigint, timestamptz, text, text,"
        " text, text), rapa_lp(text), rapa_robot_search_document(jsonb), rapa_fold(text)",
    ):
        op.execute(statement)
