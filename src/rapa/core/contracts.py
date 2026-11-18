"""Contratos compartilhados entre os módulos do RaPA.

A camada HTTP (REST e MCP), a interface e o despachante falam com a
persistência só por estes protocolos. Assim cada parte pode ser testada com
implementações em memória.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rapa.core.robot import Effect, Manifest

__all__ = [
    "MAX_RUNS_LISTED",
    "Conflict",
    "ContractError",
    "Forbidden",
    "InvalidInput",
    "NotFound",
    "Principal",
    "QuotaExceeded",
    "RobotAdmin",
    "RobotCatalog",
    "RobotInfo",
    "RobotStatus",
    "RobotSummary",
    "RobotVersionInfo",
    "Role",
    "RunMode",
    "RunService",
    "RunState",
    "RunStatus",
    "RunTrigger",
    "RunnerJob",
    "VersionStatus",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    COMPANY_ADMIN = "company_admin"
    AREA_ADMIN = "area_admin"
    CREATOR = "creator"
    APPROVER = "approver"
    OPERATOR = "operator"
    AUDITOR = "auditor"
    VIEWER = "viewer"
    CREDENTIAL_CUSTODIAN = "credential_custodian"


class Principal(_Model):
    """Quem está chamando, já autenticado. A empresa vem sempre do token."""

    subject: str = Field(min_length=1, max_length=255)
    company_id: UUID
    user_id: UUID | None = None
    client_id: str | None = None
    company_roles: frozenset[Role] = frozenset()
    area_roles: dict[UUID, frozenset[Role]] = Field(default_factory=dict)

    def roles_in(self, area_id: UUID | None) -> frozenset[Role]:
        """Papéis válidos numa área: os da empresa somados aos da área."""
        if area_id is None:
            return self.company_roles
        return self.company_roles | self.area_roles.get(area_id, frozenset())

    def has_role(self, role: Role, area_id: UUID | None = None) -> bool:
        roles = self.roles_in(area_id)
        return role in roles or Role.COMPANY_ADMIN in roles


class RobotStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"
    NEEDS_REPAIR = "needs_repair"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RobotSummary(_Model):
    """Robô publicado, como aparece no catálogo MCP e REST."""

    robot_id: UUID
    company_id: UUID
    area_id: UUID
    slug: str
    title: str
    description: str
    version_id: UUID
    version_no: int
    effect: Effect
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    fingerprint: str


class RobotInfo(_Model):
    robot_id: UUID
    company_id: UUID
    area_id: UUID
    slug: str
    title: str
    status: RobotStatus
    current_version_no: int | None
    created_at: datetime


class RobotVersionInfo(_Model):
    robot_id: UUID
    version_id: UUID
    version_no: int
    status: VersionStatus
    manifest: Manifest
    fingerprint: str
    created_by: str
    approved_by: str | None
    created_at: datetime
    violations: list[str] = Field(default_factory=list)


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: O resultado é desconhecido depois de um passo que grava algo; uma pessoa decide.
    INDETERMINATE = "indeterminate"

    @property
    def finished(self) -> bool:
        return self in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INDETERMINATE,
        }


class RunTrigger(StrEnum):
    MCP = "mcp"
    REST = "rest"
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    BATCH = "batch"
    MANUAL = "manual"
    VALIDATION = "validation"


class RunMode(StrEnum):
    FULL = "full"
    #: Para antes do primeiro passo que grava algo (validação segura).
    UNTIL_FIRST_WRITE = "until_first_write"


class RunStatus(_Model):
    run_id: UUID
    robot_slug: str
    version_no: int
    state: RunState
    trigger: RunTrigger
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outputs: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    steps_done: int = 0
    queue_position: int | None = None


class RunnerJob(_Model):
    """O que o plano de controle entrega a um executor para uma execução."""

    run_id: UUID
    company_id: UUID
    attempt: int = Field(
        ge=1, description="Token de cerca: escritas de tentativas antigas são recusadas"
    )
    manifest: Manifest
    inputs: dict[str, Any]
    mode: RunMode = RunMode.FULL
    #: Prazo da execução, sempre com fuso horário.
    deadline: AwareDatetime


class ContractError(Exception):
    """Erro de negócio com código estável e mensagem em pt-BR."""

    code = "erro"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ContractError):
    code = "nao_encontrado"


class Forbidden(ContractError):
    code = "proibido"


class Conflict(ContractError):
    code = "conflito"


class InvalidInput(ContractError):
    code = "entrada_invalida"

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class QuotaExceeded(ContractError):
    code = "limite_excedido"

    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class RobotCatalog(Protocol):
    """Robôs publicados visíveis para um principal."""

    async def list_published(self, principal: Principal) -> list[RobotSummary]: ...

    async def get_published(self, principal: Principal, slug: str) -> RobotSummary | None: ...

    async def search(
        self, principal: Principal, query: str, limit: int = 10
    ) -> list[RobotSummary]: ...


class RunService(Protocol):
    """Início e consulta de execuções."""

    async def start(
        self,
        principal: Principal,
        slug: str,
        inputs: dict[str, Any],
        trigger: RunTrigger,
        idempotency_key: str | None = None,
    ) -> RunStatus: ...

    async def get(self, principal: Principal, run_id: UUID) -> RunStatus | None: ...

    async def list_recent(self, principal: Principal, limit: int = 50) -> list[RunStatus]:
        """Execuções mais recentes que o principal pode ver (das áreas em que tem papel)."""
        ...


#: Quantas execuções, no máximo, uma listagem devolve.
MAX_RUNS_LISTED = 200


class RobotAdmin(Protocol):
    """Ciclo de vida rascunho → validado → publicado."""

    async def list_robots(self, principal: Principal) -> list[RobotInfo]: ...

    async def create_version(
        self, principal: Principal, area_id: UUID, manifest: Manifest
    ) -> RobotVersionInfo: ...

    async def get_version(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo | None: ...

    async def list_versions(self, principal: Principal, robot_id: UUID) -> list[RobotVersionInfo]:
        """Versões do robô, da primeira à mais nova; ``NotFound`` se o robô não é visível."""
        ...

    async def mark_validated(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo: ...

    async def publish(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo: ...
