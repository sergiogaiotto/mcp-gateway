"""Tabelas do plano de controle, como o SQLAlchemy Core as enxerga.

A fonte da verdade do esquema é a migração do Alembic, escrita à mão em SQL
(políticas de isolamento por linha, gatilhos e partições não cabem no
autogenerate). Estas definições servem só para montar consultas; um teste
confere que elas batem com o banco migrado.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

__all__ = [
    "TENANT_TABLES",
    "a1_authorizations",
    "areas",
    "audit_log",
    "batch_rows",
    "batches",
    "companies",
    "company_quotas",
    "credential_alerts",
    "credentials",
    "idempotency_keys",
    "memberships",
    "metadata",
    "platform_events",
    "platform_settings",
    "recording_sessions",
    "recordings",
    "repair_proposals",
    "robot_versions",
    "robots",
    "run_events",
    "run_snapshots",
    "runs",
    "schedule_occurrences",
    "schedules",
    "users",
    "version_approvals",
    "webhook_deliveries",
    "webhooks",
]

metadata = MetaData()


def _ts(name: str, *, nullable: bool = True, server_default: bool = False) -> Column[object]:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=text("now()") if server_default else None,
    )


companies = Table(
    "companies",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
    #: Situação (revisão 0007): ``active`` ou ``frozen`` (lê tudo, nada novo começa).
    Column("status", Text, nullable=False, server_default="active"),
    Column("status_reason", Text, nullable=True),
    _ts("status_changed_at"),
    #: Id ou apelido da organização no provedor de identidade (único).
    Column("idp_organization", Text, nullable=True),
)

areas = Table(
    "areas",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("name", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("subject", Text, nullable=False),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("active", Boolean, nullable=False, server_default="true"),
    _ts("created_at", nullable=False, server_default=True),
)

memberships = Table(
    "memberships",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("area_id", Uuid, nullable=True),
    Column("user_id", Uuid, nullable=False),
    Column("role", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

company_quotas = Table(
    "company_quotas",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("guaranteed_slots", Integer, nullable=False),
    Column("max_borrow_factor", Numeric(5, 2), nullable=False),
    Column("max_queued", Integer, nullable=False),
    _ts("updated_at", nullable=False, server_default=True),
)

platform_settings = Table(
    "platform_settings",
    metadata,
    Column("id", Boolean, primary_key=True),
    Column("total_browser_slots", Integer, nullable=False),
    Column("interactive_reserve_ratio", Numeric(4, 3), nullable=False),
    _ts("updated_at", nullable=False, server_default=True),
)

robots = Table(
    "robots",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("area_id", Uuid, nullable=False),
    Column("slug", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("current_version_no", Integer, nullable=True),
    _ts("created_at", nullable=False, server_default=True),
    _ts("updated_at", nullable=False, server_default=True),
)

robot_versions = Table(
    "robot_versions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("robot_id", Uuid, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("violations", JSONB, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("approved_by", Text, nullable=True),
    _ts("created_at", nullable=False, server_default=True),
    Column(
        "search_document",
        TSVECTOR,
        Computed("rapa_robot_search_document(manifest)", persisted=True),
    ),
)

credentials = Table(
    "credentials",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("area_id", Uuid, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("origin", Text, nullable=False),
    Column("vault_path", Text, nullable=False),
    Column("custodian_user_id", Uuid, nullable=True),
    _ts("created_at", nullable=False, server_default=True),
    #: Revisão 0006: validade e impressão do certificado A1, revogação.
    _ts("not_after"),
    Column("fingerprint_sha256", Text, nullable=True),
    _ts("revoked_at"),
    Column("revoked_by", Text, nullable=True),
    _ts("updated_at", nullable=False, server_default=True),
)

runs = Table(
    "runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("company_id", Uuid, nullable=False),
    Column("area_id", Uuid, nullable=False),
    Column("robot_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("priority", SmallInteger, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("inputs", JSONB, nullable=False),
    Column("outputs", JSONB(none_as_null=True), nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("failed_step_id", Text, nullable=True),
    Column("steps_done", Integer, nullable=False),
    Column("artifacts", JSONB, nullable=False),
    Column("idempotency_key", Text, nullable=True),
    _ts("irreversible_started_at"),
    #: Início do primeiro passo que grava (revisão 0002): a execução não volta para a fila.
    _ts("write_started_at"),
    _ts("lease_expires_at"),
    _ts("deadline_at"),
    _ts("queued_at", nullable=False, server_default=True),
    _ts("started_at"),
    _ts("finished_at"),
    _ts("created_at", nullable=False, server_default=True),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("scope", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("run_id", Uuid, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
    _ts("expires_at", nullable=False),
)

run_events = Table(
    "run_events",
    metadata,
    Column("company_id", Uuid, nullable=False),
    Column("run_id", Uuid, primary_key=True),
    Column("attempt", Integer, nullable=False),
    Column("seq", Integer, primary_key=True),
    Column("at", DateTime(timezone=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("step_id", Text, nullable=True),
    Column("data", JSONB, nullable=False),
    _ts("ingested_at", nullable=False, server_default=True),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    # Preenchidos pelo gatilho de encadeamento; os valores padrão só existem
    # para que a inclusão não precise informá-los.
    Column("seq", BigInteger, primary_key=True, server_default="0"),
    _ts("at", nullable=False, server_default=True),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("payload_digest", Text, nullable=False),
    Column("prev_hash", Text, nullable=False, server_default=""),
    Column("row_hash", Text, nullable=False, server_default=""),
)

recording_sessions = Table(
    "recording_sessions",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("area_id", Uuid, nullable=False),
    Column("owner", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("start_url", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("end_reason", Text, nullable=True),
    Column("robot_id", Uuid, nullable=True),
    Column("version_no", Integer, nullable=True),
    Column("instance_id", Text, nullable=False, server_default=""),
    _ts("created_at", nullable=False, server_default=True),
    _ts("started_at"),
    _ts("stopped_at"),
    _ts("expires_at", nullable=False),
    _ts("seen_at", nullable=False, server_default=True),
)

recordings = Table(
    "recordings",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("session_id", Uuid, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("recording", JSONB, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

schedules = Table(
    "schedules",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("robot_id", Uuid, nullable=False),
    Column("title", Text, nullable=False),
    Column("cron", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("inputs", JSONB, nullable=False),
    Column("enabled", Boolean, nullable=False),
    _ts("next_run_at"),
    _ts("last_occurrence_at"),
    Column("created_by", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
    _ts("updated_at", nullable=False, server_default=True),
    _ts("archived_at"),
)

schedule_occurrences = Table(
    "schedule_occurrences",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("schedule_id", Uuid, primary_key=True),
    Column("scheduled_for", DateTime(timezone=True), primary_key=True),
    Column("status", Text, nullable=False),
    Column("reason", Text, nullable=True),
    Column("run_id", Uuid, nullable=True),
    _ts("created_at", nullable=False, server_default=True),
)

webhooks = Table(
    "webhooks",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("robot_id", Uuid, nullable=False),
    Column("title", Text, nullable=False),
    #: Sal da chave de assinatura; a chave é derivada dele com a chave do servidor.
    Column("key_salt", LargeBinary, nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("rate_per_minute", Integer, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
    _ts("updated_at", nullable=False, server_default=True),
    _ts("rotated_at"),
    _ts("archived_at"),
)

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("webhook_id", Uuid, primary_key=True),
    Column("delivery_id", Text, primary_key=True),
    Column("body_sha256", Text, nullable=False),
    Column("run_id", Uuid, nullable=False),
    _ts("received_at", nullable=False, server_default=True),
)

batches = Table(
    "batches",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("robot_id", Uuid, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("file_name", Text, nullable=False),
    Column("file_format", Text, nullable=False),
    Column("file_sha256", Text, nullable=False),
    Column("total_rows", Integer, nullable=False),
    Column("columns", JSONB, nullable=False),
    Column("request_key", Text, nullable=True),
    Column("created_by", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

batch_rows = Table(
    "batch_rows",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("batch_id", Uuid, primary_key=True),
    Column("row_no", Integer, primary_key=True),
    Column("line_no", Integer, nullable=False),
    Column("run_id", Uuid, nullable=False),
)

run_snapshots = Table(
    "run_snapshots",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("run_id", Uuid, primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("step_id", Text, nullable=False),
    Column("error_code", Text, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("snapshot_sha256", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

repair_proposals = Table(
    "repair_proposals",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("run_id", Uuid, nullable=False),
    Column("robot_id", Uuid, nullable=False),
    Column("step_id", Text, nullable=False),
    Column("base_version_id", Uuid, nullable=False),
    Column("base_version_no", Integer, nullable=False),
    Column("version_id", Uuid, nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("candidate_id", Text, nullable=False),
    Column("candidate", JSONB, nullable=False),
    Column("model_candidate_id", Text, nullable=True),
    Column("snapshot_sha256", Text, nullable=False),
    Column("requested_by", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
)

a1_authorizations = Table(
    "a1_authorizations",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("area_id", Uuid, nullable=False),
    Column("robot_id", Uuid, nullable=False),
    Column("credential_id", Uuid, nullable=False),
    Column("origin", Text, nullable=False),
    Column("fingerprint_sha256", Text, nullable=True),
    Column("authorized_by", Text, nullable=False),
    _ts("created_at", nullable=False, server_default=True),
    _ts("revoked_at"),
    Column("revoked_by", Text, nullable=True),
)

version_approvals = Table(
    "version_approvals",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("robot_id", Uuid, primary_key=True),
    Column("version_no", Integer, primary_key=True),
    Column("approved_by", Text, primary_key=True),
    _ts("approved_at", nullable=False, server_default=True),
)

credential_alerts = Table(
    "credential_alerts",
    metadata,
    Column("company_id", Uuid, primary_key=True),
    Column("credential_id", Uuid, primary_key=True),
    Column("day", Date, primary_key=True),
    Column("kind", Text, primary_key=True),
    _ts("not_after"),
    _ts("created_at", nullable=False, server_default=True),
)

#: Registros da plataforma sem empresa (revisão 0007): só o dono lê e escreve.
platform_events = Table(
    "platform_events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    _ts("at", nullable=False, server_default=True),
    Column("action", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("data", JSONB, nullable=False),
)

#: Tabelas com dados de empresa (todas com isolamento por linha).
TENANT_TABLES: tuple[Table, ...] = (
    companies,
    areas,
    users,
    memberships,
    company_quotas,
    robots,
    robot_versions,
    credentials,
    runs,
    idempotency_keys,
    run_events,
    audit_log,
    recording_sessions,
    recordings,
    schedules,
    schedule_occurrences,
    webhooks,
    webhook_deliveries,
    batches,
    batch_rows,
    run_snapshots,
    repair_proposals,
    a1_authorizations,
    version_approvals,
    credential_alerts,
)
