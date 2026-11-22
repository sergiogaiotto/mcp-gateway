"""Publicar: agendas, webhooks e lotes por planilha.

Revisão: 0004
Anterior: 0003

Seis tabelas, todas com a empresa liderando as chaves e isolamento por linha
(ENABLE e FORCE) com a política de sempre:

- ``schedules``: um robô, uma expressão cron num fuso e as entradas fixas. A
  agenda roda a versão publicada no momento de cada ocorrência. ``next_run_at``
  só existe com a agenda ligada; remover só arquiva (``archived_at``).
- ``schedule_occurrences``: cada ocorrência que o agendador tratou, única por
  agenda e horário (a chave primária): enfileirada, com a execução, ou ignorada,
  com o motivo. Só aceita inclusões.
- ``webhooks``: um robô, o sal da chave e o limite por minuto. A chave de
  assinatura nunca vem para cá: ela é derivada, a cada uso, da chave do servidor
  (``RAPA_CHAVE_WEBHOOKS``) com o sal aleatório de cada webhook. Trocar a chave é
  trocar o sal.
- ``webhook_deliveries``: cada entrega aceita, única por webhook e identificador
  da entrega (a proteção contra repetição; ASCII visível sem o ponto, que separa
  os campos da mensagem assinada), com o resumo do corpo e a execução. Só aceita
  inclusões.
- ``batches`` e ``batch_rows``: o lote (o robô e a versão, o arquivo pelo resumo,
  nunca o conteúdo) e uma linha por execução. Só aceitam inclusões.

A entrega de um webhook chega sem token, então não se sabe a empresa antes de
ler o webhook. A política de ``webhooks`` tem por isso uma segunda condição:
numa transação **sem empresa**, enxerga-se só o webhook cujo identificador está
em ``rapa.webhook_id`` (o do caminho da entrega), para ler a empresa e o sal e
conferir a assinatura. Nada se grava assim (``WITH CHECK`` só pela empresa), e
com uma empresa fixada a segunda condição não vale.

O despachante só lê a fila das agendas (``company_id``, ``id``, ``enabled``,
``next_run_at`` e ``archived_at``): as ocorrências viram execuções numa
transação fixada na empresa da agenda, pelo papel da aplicação.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, publishing_grants_sql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"
_WEBHOOK = "nullif(current_setting('rapa.webhook_id', true), '')::uuid"
_TABLES = (
    "schedules",
    "schedule_occurrences",
    "webhooks",
    "webhook_deliveries",
    "batches",
    "batch_rows",
)
_APPEND_ONLY = ("schedule_occurrences", "webhook_deliveries", "batches", "batch_rows")


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _tables() -> list[str]:
    return [
        """
        CREATE TABLE schedules (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            robot_id uuid NOT NULL,
            title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
            cron text NOT NULL CHECK (length(cron) BETWEEN 1 AND 200),
            timezone text NOT NULL DEFAULT 'America/Sao_Paulo'
                CHECK (length(timezone) BETWEEN 1 AND 64),
            inputs jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(inputs) = 'object'),
            enabled boolean NOT NULL DEFAULT true,
            next_run_at timestamptz,
            last_occurrence_at timestamptz,
            created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
            updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            PRIMARY KEY (company_id, id),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            CHECK (enabled OR next_run_at IS NULL),
            CHECK (archived_at IS NULL OR NOT enabled)
        )
        """,
        """
        CREATE INDEX schedules_due ON schedules (next_run_at)
            WHERE enabled AND archived_at IS NULL
        """,
        "CREATE INDEX schedules_robot ON schedules (company_id, robot_id)",
        """
        CREATE TABLE schedule_occurrences (
            company_id uuid NOT NULL REFERENCES companies (id),
            schedule_id uuid NOT NULL,
            scheduled_for timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('enqueued', 'skipped')),
            reason text CHECK (reason IS NULL OR reason ~ '^[a-z_]{1,40}$'),
            run_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, schedule_id, scheduled_for),
            FOREIGN KEY (company_id, schedule_id) REFERENCES schedules (company_id, id),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id),
            UNIQUE (company_id, run_id),
            CHECK ((status = 'enqueued') = (run_id IS NOT NULL)),
            CHECK (status = 'enqueued' OR reason IS NOT NULL)
        )
        """,
        """
        CREATE TABLE webhooks (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            robot_id uuid NOT NULL,
            title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
            key_salt bytea NOT NULL CHECK (length(key_salt) = 32),
            key_version integer NOT NULL DEFAULT 1 CHECK (key_version >= 1),
            enabled boolean NOT NULL DEFAULT true,
            rate_per_minute integer NOT NULL DEFAULT 60
                CHECK (rate_per_minute BETWEEN 1 AND 6000),
            created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
            updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            rotated_at timestamptz,
            archived_at timestamptz,
            PRIMARY KEY (company_id, id),
            UNIQUE (id),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            CHECK (archived_at IS NULL OR NOT enabled)
        )
        """,
        "CREATE INDEX webhooks_robot ON webhooks (company_id, robot_id)",
        """
        CREATE TABLE webhook_deliveries (
            company_id uuid NOT NULL REFERENCES companies (id),
            webhook_id uuid NOT NULL,
            -- ASCII visível sem o ponto, que separa os campos da mensagem assinada.
            delivery_id text NOT NULL
                CHECK (delivery_id ~ '^[!-~]{1,200}$' AND strpos(delivery_id, '.') = 0),
            body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
            run_id uuid NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, webhook_id, delivery_id),
            FOREIGN KEY (company_id, webhook_id) REFERENCES webhooks (company_id, id),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id),
            UNIQUE (company_id, run_id)
        )
        """,
        """
        CREATE INDEX webhook_deliveries_recent
            ON webhook_deliveries (company_id, webhook_id, received_at DESC)
        """,
        """
        CREATE TABLE batches (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            robot_id uuid NOT NULL,
            version_id uuid NOT NULL,
            file_name text NOT NULL CHECK (length(file_name) BETWEEN 1 AND 200),
            file_format text NOT NULL CHECK (file_format IN ('csv', 'xlsx')),
            file_sha256 text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
            total_rows integer NOT NULL CHECK (total_rows >= 1),
            columns jsonb NOT NULL CHECK (jsonb_typeof(columns) = 'object'),
            request_key text CHECK (request_key IS NULL OR length(request_key) BETWEEN 1 AND 255),
            created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, id),
            UNIQUE (company_id, request_key),
            FOREIGN KEY (company_id, robot_id, version_id)
                REFERENCES robot_versions (company_id, robot_id, id)
        )
        """,
        "CREATE INDEX batches_company_created ON batches (company_id, created_at DESC)",
        """
        CREATE TABLE batch_rows (
            company_id uuid NOT NULL REFERENCES companies (id),
            batch_id uuid NOT NULL,
            row_no integer NOT NULL CHECK (row_no >= 1),
            line_no integer NOT NULL CHECK (line_no >= 2),
            run_id uuid NOT NULL,
            PRIMARY KEY (company_id, batch_id, row_no),
            UNIQUE (company_id, run_id),
            FOREIGN KEY (company_id, batch_id) REFERENCES batches (company_id, id),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id)
        )
        """,
    ]


def _row_level_security() -> list[str]:
    statements: list[str] = []
    for table in _TABLES:
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        ]
        if table == "webhooks":
            # Sem empresa, só o webhook do caminho da entrega (para conferir a assinatura).
            statements.append(
                f"""
                CREATE POLICY tenant_isolation ON webhooks
                    USING (
                        company_id = {_TENANT}
                        OR ({_TENANT} IS NULL AND id = {_WEBHOOK})
                    )
                    WITH CHECK (company_id = {_TENANT})
                """
            )
        else:
            statements.append(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                    USING (company_id = {_TENANT}) WITH CHECK (company_id = {_TENANT})
                """
            )
    return statements


def _triggers() -> list[str]:
    statements = [
        """
        CREATE FUNCTION rapa_schedules_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'agendas não podem ser apagadas; remova-as (arquivar)';
            END IF;
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a agenda não pode trocar de empresa, robô ou autor';
            END IF;
            IF OLD.archived_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma agenda removida não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER schedules_guard
            BEFORE UPDATE OR DELETE ON schedules
            FOR EACH ROW EXECUTE FUNCTION rapa_schedules_guard()
        """,
        """
        CREATE FUNCTION rapa_webhooks_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'webhooks não podem ser apagados; remova-os (arquivar)';
            END IF;
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o webhook não pode trocar de empresa, robô ou autor';
            END IF;
            IF NEW.key_version < OLD.key_version
                OR (NEW.key_salt IS DISTINCT FROM OLD.key_salt
                    AND NEW.key_version <= OLD.key_version)
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a chave do webhook só muda para uma versão nova';
            END IF;
            IF OLD.archived_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'um webhook removido não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER webhooks_guard
            BEFORE UPDATE OR DELETE ON webhooks
            FOR EACH ROW EXECUTE FUNCTION rapa_webhooks_guard()
        """,
    ]
    for table in _APPEND_ONLY:
        statements.append(
            f"""
            CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
            """
        )
    return statements


def _comments() -> list[str]:
    return [
        "COMMENT ON TABLE schedules IS"
        " 'Agendas: robô, expressão cron, fuso e entradas fixas; remover só arquiva'",
        "COMMENT ON TABLE schedule_occurrences IS"
        " 'Ocorrências das agendas, únicas por agenda e horário; só aceitam inclusões'",
        "COMMENT ON TABLE webhooks IS"
        " 'Webhooks dos robôs; guarda só o sal da chave, nunca a chave'",
        "COMMENT ON TABLE webhook_deliveries IS"
        " 'Entregas aceitas, únicas por webhook e identificador; só aceitam inclusões'",
        "COMMENT ON TABLE batches IS"
        " 'Lotes por planilha (o arquivo só pelo resumo); só aceitam inclusões'",
        "COMMENT ON TABLE batch_rows IS 'Uma linha de lote por execução; só aceitam inclusões'",
    ]


def upgrade() -> None:
    for statement in (
        *_tables(),
        *_row_level_security(),
        *_triggers(),
        *_comments(),
        *publishing_grants_sql(_roles()),
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS batch_rows, batches, webhook_deliveries, webhooks,"
        " schedule_occurrences, schedules CASCADE",
        "DROP FUNCTION IF EXISTS rapa_schedules_guard(), rapa_webhooks_guard()",
    ):
        op.execute(statement)
