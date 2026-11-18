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
    DateTime,
    Integer,
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
    "areas",
    "audit_log",
    "companies",
    "company_quotas",
    "credentials",
    "idempotency_keys",
    "memberships",
    "metadata",
    "platform_settings",
    "robot_versions",
    "robots",
    "run_events",
    "runs",
    "users",
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
)
