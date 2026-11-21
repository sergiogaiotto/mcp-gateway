"""Contratos compartilhados entre os módulos do RaPA.

A camada HTTP (REST e MCP), a interface e o despachante falam com a
persistência só por estes protocolos. Assim cada parte pode ser testada com
implementações em memória.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rapa.core.robot import NAME_PATTERN, SLUG_PATTERN, Effect, Manifest

__all__ = [
    "MAX_RUNS_LISTED",
    "MAX_SESSIONS_LISTED",
    "CheckProposal",
    "Conflict",
    "ContractError",
    "DraftChoices",
    "EffectChoice",
    "EffectProposal",
    "Forbidden",
    "GeneralizationProposals",
    "InvalidInput",
    "NotFound",
    "ParameterChoice",
    "ParameterProposal",
    "ParameterType",
    "Principal",
    "ProposalSource",
    "QuotaExceeded",
    "RecordingService",
    "RecordingSessionInfo",
    "RecordingSessionStatus",
    "RepetitionChoice",
    "RepetitionField",
    "RepetitionFieldChoice",
    "RepetitionProposal",
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
    "ServiceUnavailable",
    "StepNameChoice",
    "StepNameProposal",
    "StepOverview",
    "VersionStatus",
    "ViewerTicket",
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
    first_seq: int = Field(
        default=0,
        ge=0,
        description=(
            "Número do primeiro evento desta tentativa: a numeração continua a das "
            "tentativas anteriores, para que (run_id, seq) seja único na execução"
        ),
    )


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


class ServiceUnavailable(ContractError):
    """Um recurso da plataforma não está disponível agora (por exemplo, o navegador da gravação)."""

    code = "indisponivel"


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


# ---------------------------------------------------------------------------
# Demonstração e generalização (sessões de gravação)
# ---------------------------------------------------------------------------

#: Quantas sessões de gravação, no máximo, uma listagem devolve.
MAX_SESSIONS_LISTED = 100

#: Tipos simples que uma entrada criada na revisão pode ter (os de ``rapa.core.edits``).
type ParameterType = Literal[
    "string", "integer", "number", "boolean", "date", "cpf", "cnpj", "money"
]


class RecordingSessionStatus(StrEnum):
    """Situação de uma sessão de gravação (o navegador na nuvem de uma demonstração)."""

    STARTING = "starting"
    ACTIVE = "active"
    #: A pessoa parou: a gravação está guardada e pronta para a revisão.
    STOPPED = "stopped"
    #: A revisão virou um rascunho de robô.
    DRAFTED = "drafted"
    #: Terminou sozinha (sem atividade, prazo máximo, navegador perdido): nada foi guardado.
    EXPIRED = "expired"
    FAILED = "failed"

    @property
    def live(self) -> bool:
        """O navegador da sessão ainda está (ou deveria estar) aberto."""
        return self in {RecordingSessionStatus.STARTING, RecordingSessionStatus.ACTIVE}

    @property
    def reviewable(self) -> bool:
        """Há uma gravação guardada para revisar."""
        return self in {RecordingSessionStatus.STOPPED, RecordingSessionStatus.DRAFTED}


class RecordingSessionInfo(_Model):
    """Uma sessão de gravação, como a dona dela a vê."""

    session_id: UUID
    company_id: UUID
    area_id: UUID
    owner: str
    title: str
    start_url: str
    status: RecordingSessionStatus
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    #: Código de por que terminou sem gravação (``ociosa``, ``tempo_esgotado``, ``perdida``...).
    end_reason: str | None = None
    recording_id: UUID | None = None
    robot_id: UUID | None = None
    version_no: int | None = None


class ViewerTicket(_Model):
    """Bilhete de uso único para abrir a visão ao vivo de uma sessão.

    Vale por pouco tempo e só uma vez; quem o recebe abre o websocket em
    ``path`` com ``?bilhete=<ticket>``. Nunca vai para o banco nem para o log.
    """

    session_id: UUID
    ticket: str = Field(min_length=32, max_length=200, repr=False)
    expires_at: datetime
    path: str


class ProposalSource(StrEnum):
    """De onde veio uma proposta da revisão."""

    #: Regras determinísticas do compilador.
    RULES = "regras"
    #: O modelo de linguagem (validado pelo código antes de aparecer).
    MODEL = "modelo"


class StepOverview(_Model):
    """Um passo do robô compilado, para a pessoa se localizar na revisão."""

    step_id: str
    number: str
    title: str
    kind: str
    effect: Effect
    depth: int = 0


class ParameterProposal(_Model):
    """Um valor digitado ou escolhido que pode virar entrada do robô."""

    step_id: str
    step_title: str
    field_label: str | None = None
    #: Valor gravado, encurtado para exibição.
    value_preview: str
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    type: ParameterType = "string"
    source: ProposalSource = ProposalSource.RULES


class StepNameProposal(_Model):
    step_id: str
    current_title: str
    title: str = Field(min_length=1, max_length=200)
    source: ProposalSource = ProposalSource.MODEL


class RepetitionField(_Model):
    """Um valor que muda a cada volta da repetição e vira um campo do item."""

    step_id: str
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    type: ParameterType = "string"
    #: Os valores de cada volta, encurtados para exibição.
    values: list[str] = Field(default_factory=list)


class RepetitionProposal(_Model):
    """Passos que a pessoa repetiu, em sequência, com valores diferentes."""

    proposal_id: str
    first_step_id: str
    #: Os passos da primeira volta (viram o corpo da repetição).
    body_step_ids: list[str]
    #: Todos os passos cobertos (as voltas seguintes saem do robô).
    step_ids: list[str]
    times: int = Field(ge=2)
    list_name: str = Field(pattern=NAME_PATTERN)
    list_title: str = Field(min_length=1, max_length=200)
    fields: list[RepetitionField] = Field(min_length=1)
    source: ProposalSource = ProposalSource.RULES


class EffectProposal(_Model):
    """O modelo acha que o passo grava mais do que o compilador concluiu (o efeito só sobe)."""

    step_id: str
    step_title: str
    current: Effect
    proposed: Literal["write", "irreversible"]
    source: ProposalSource = ProposalSource.MODEL


class CheckProposal(_Model):
    """Texto visto na gravação que confirma o sucesso, para conferir no fim de cada execução."""

    text: str = Field(min_length=3, max_length=200)
    source: ProposalSource = ProposalSource.MODEL


class GeneralizationProposals(_Model):
    """Tudo o que a revisão mostra: nada disso entra no robô sem a confirmação da pessoa."""

    session_id: UUID
    recording_id: UUID
    name: str
    title: str
    description: str
    steps: list[StepOverview]
    parameters: list[ParameterProposal] = Field(default_factory=list)
    step_names: list[StepNameProposal] = Field(default_factory=list)
    repetitions: list[RepetitionProposal] = Field(default_factory=list)
    effects: list[EffectProposal] = Field(default_factory=list)
    checks: list[CheckProposal] = Field(default_factory=list)
    #: Avisos do compilador em pt-BR.
    warnings: list[str] = Field(default_factory=list)
    #: O modelo respondeu agora; sem ele, só as sugestões das regras aparecem.
    model_available: bool = False
    model_message: str | None = None
    #: Propostas do modelo que o código recusou, com o motivo.
    model_rejected: list[str] = Field(default_factory=list)


class ParameterChoice(_Model):
    step_id: str = Field(min_length=1, max_length=40)
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    type: ParameterType = "string"
    description: str = Field(default="", max_length=1000)


class StepNameChoice(_Model):
    step_id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)


class RepetitionFieldChoice(_Model):
    step_id: str = Field(min_length=1, max_length=40)
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    type: ParameterType = "string"


class RepetitionChoice(_Model):
    proposal_id: str = Field(min_length=1, max_length=80)
    list_name: str = Field(pattern=NAME_PATTERN)
    list_title: str = Field(min_length=1, max_length=200)
    fields: list[RepetitionFieldChoice] = Field(min_length=1, max_length=50)


class EffectChoice(_Model):
    step_id: str = Field(min_length=1, max_length=40)
    effect: Literal["write", "irreversible"]


class DraftChoices(_Model):
    """O que a pessoa confirmou na revisão; o código aplica e o portão de política confere."""

    name: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    parameters: list[ParameterChoice] = Field(default_factory=list, max_length=50)
    step_names: list[StepNameChoice] = Field(default_factory=list, max_length=500)
    repetitions: list[RepetitionChoice] = Field(default_factory=list, max_length=20)
    effects: list[EffectChoice] = Field(default_factory=list, max_length=500)
    checks: list[str] = Field(default_factory=list, max_length=20)


class RecordingService(Protocol):
    """Demonstrar e generalizar: sessões de gravação, a revisão e o rascunho.

    Só quem tem o papel de criação na área abre uma sessão, e só a dona dela a vê,
    controla, para e revisa.
    """

    async def create_session(
        self, principal: Principal, area_id: UUID, start_url: str, title: str = ""
    ) -> RecordingSessionInfo: ...

    async def get_session(
        self, principal: Principal, session_id: UUID
    ) -> RecordingSessionInfo | None: ...

    async def list_sessions(
        self, principal: Principal, limit: int = 20
    ) -> list[RecordingSessionInfo]:
        """Sessões da própria pessoa, das mais novas às mais antigas."""
        ...

    async def issue_ticket(self, principal: Principal, session_id: UUID) -> ViewerTicket: ...

    async def stop_session(
        self, principal: Principal, session_id: UUID
    ) -> RecordingSessionInfo: ...

    async def get_proposals(
        self, principal: Principal, session_id: UUID
    ) -> GeneralizationProposals: ...

    async def create_draft(
        self, principal: Principal, session_id: UUID, choices: DraftChoices
    ) -> RobotVersionInfo: ...
