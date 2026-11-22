"""Contratos compartilhados entre os módulos do RaPA.

A camada HTTP (REST e MCP), a interface e o despachante falam com a
persistência só por estes protocolos. Assim cada parte pode ser testada com
implementações em memória.
"""

import builtins
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rapa.core.cron import DEFAULT_TIMEZONE
from rapa.core.robot import (
    NAME_PATTERN,
    SLUG_PATTERN,
    CredentialKind,
    Effect,
    Locator,
    Manifest,
    Target,
)

__all__ = [
    "A1_APPROVALS",
    "AUTOMATION_ROLES",
    "MAX_BATCHES_LISTED",
    "MAX_OCCURRENCES_LISTED",
    "MAX_RUNS_LISTED",
    "MAX_SESSIONS_LISTED",
    "A1AuthorizationInfo",
    "ApprovalInfo",
    "BatchColumn",
    "BatchFile",
    "BatchFormat",
    "BatchInfo",
    "BatchPreview",
    "BatchRow",
    "BatchRowError",
    "BatchService",
    "CertificateBlocked",
    "CheckProposal",
    "Conflict",
    "ContractError",
    "CredentialInfo",
    "CredentialService",
    "CredentialStatus",
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
    "RepairCandidate",
    "RepairProposalInfo",
    "RepairService",
    "RepairStepInfo",
    "RepairSuggestion",
    "RepairSuggestionStatus",
    "RepairView",
    "RepetitionChoice",
    "RepetitionField",
    "RepetitionFieldChoice",
    "RepetitionProposal",
    "RobotAdmin",
    "RobotCatalog",
    "RobotInfo",
    "RobotInfoProposal",
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
    "ScheduleChanges",
    "ScheduleInfo",
    "ScheduleOccurrence",
    "ScheduleOccurrenceStatus",
    "ScheduleService",
    "ScheduleSpec",
    "ServiceUnavailable",
    "SignatureInvalid",
    "StepNameChoice",
    "StepNameProposal",
    "StepOverview",
    "VersionApprovals",
    "VersionStatus",
    "ViewerTicket",
    "WebhookChanges",
    "WebhookDelivery",
    "WebhookInfo",
    "WebhookReceiver",
    "WebhookSecret",
    "WebhookService",
    "WebhookSpec",
    "can_automate",
    "required_approvals",
    "uses_a1",
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


#: Quem cria e muda agendas, webhooks e lotes de uma área (o administrador da empresa também).
AUTOMATION_ROLES = frozenset({Role.OPERATOR, Role.AREA_ADMIN})


def can_automate(principal: Principal, area_id: UUID | None) -> bool:
    """Se o principal pode criar e mudar agendas, webhooks e lotes dos robôs da área.

    Passa só por :meth:`Principal.has_role`, a função única das permissões. Sem
    área, valem só os papéis da empresa.
    """
    return any(principal.has_role(role, area_id) for role in AUTOMATION_ROLES)


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


#: Aprovações exigidas para publicar uma versão que declara um certificado digital A1.
A1_APPROVALS = 2


def uses_a1(manifest: Manifest) -> bool:
    """Se o manifesto declara alguma credencial do tipo certificado digital A1."""
    return any(c.kind is CredentialKind.PFX_A1 for c in manifest.credentials)


def required_approvals(manifest: Manifest) -> int:
    """Quantas pessoas diferentes aprovam a versão antes de ela ser publicada.

    Duas para um robô com certificado A1 (a mesma conta do gatilho do banco, que
    olha ``manifest->'credentials'``), uma para os outros.
    """
    return A1_APPROVALS if uses_a1(manifest) else 1


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
    #: Quem já aprovou a versão, na ordem (o último publica).
    approvals: list[str] = Field(default_factory=list)
    #: Quantas aprovações a versão precisa para ser publicada (2 com certificado A1).
    approvals_required: int = Field(default=1, ge=1)


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


class CertificateBlocked(InvalidInput):
    """O certificado A1 de um robô não pode ser usado agora; ``errors`` diz o que falta.

    Registro na área, revogação, validade e a autorização do custodiante para o robô
    (com a impressão atual do certificado). Nada sigiloso entra na lista.
    """

    code = "certificado_a1_bloqueado"


class SignatureInvalid(ContractError):
    """Pedido sem token cuja assinatura não confere (a entrega de um webhook).

    A mensagem nunca diz se o webhook existe: webhook desconhecido, assinatura
    errada e horário fora da tolerância têm a mesma resposta (HTTP 401).
    """

    code = "assinatura_invalida"


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

    Vale por pouco tempo e só uma vez; quem o recebe abre o websocket em ``path``
    (sem nada no endereço) e manda, como primeira mensagem,
    ``{"tipo": "bilhete", "bilhete": "<ticket>"}``. Nunca vai para o banco, para o
    endereço nem para o log.
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


class RobotInfoProposal(_Model):
    """O título e a descrição que o modelo sugere para o robô.

    Fica fora do formulário: o título e a descrição de :class:`GeneralizationProposals`
    são os da compilação, e o texto do modelo (que pode ter vindo de uma página
    demonstrada, escrito para quem aprova) só entra se a pessoa aceitar.
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    source: ProposalSource = ProposalSource.MODEL


class GeneralizationProposals(_Model):
    """Tudo o que a revisão mostra: nada disso entra no robô sem a confirmação da pessoa."""

    session_id: UUID
    recording_id: UUID
    #: Nome técnico, título e descrição da compilação (os valores iniciais do formulário).
    name: str
    title: str
    description: str
    steps: list[StepOverview]
    #: A sugestão do modelo para o título e a descrição, à parte (começa em "Rejeitar").
    robot_info: RobotInfoProposal | None = None
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


# ---------------------------------------------------------------------------
# Publicar: agenda, webhook e lote por planilha
# ---------------------------------------------------------------------------

#: Quantas ocorrências de uma agenda, no máximo, uma listagem devolve.
MAX_OCCURRENCES_LISTED = 200
#: Quantos lotes, no máximo, uma listagem devolve.
MAX_BATCHES_LISTED = 100


class ScheduleOccurrenceStatus(StrEnum):
    """O que aconteceu com uma ocorrência de uma agenda."""

    #: Virou uma execução na fila.
    ENQUEUED = "enqueued"
    #: Não virou execução; o motivo fica em ``reason``.
    SKIPPED = "skipped"


class ScheduleSpec(_Model):
    """Uma agenda nova: o robô roda na versão publicada no momento de cada ocorrência."""

    robot_id: UUID
    title: str = Field(min_length=1, max_length=120)
    cron: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default=DEFAULT_TIMEZONE, min_length=1, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ScheduleChanges(_Model):
    """O que muda numa agenda; o que fica ``None`` continua igual."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduleInfo(_Model):
    schedule_id: UUID
    company_id: UUID
    area_id: UUID
    robot_id: UUID
    robot_slug: str
    title: str
    cron: str
    timezone: str
    inputs: dict[str, Any]
    enabled: bool
    #: Próxima ocorrência (UTC); ``None`` com a agenda desligada.
    next_run_at: datetime | None
    last_occurrence_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class ScheduleOccurrence(_Model):
    """Uma ocorrência de uma agenda: única por agenda e horário."""

    scheduled_for: datetime
    status: ScheduleOccurrenceStatus
    #: Por que não virou execução (``perdida``, ``substituida``, ``fila_cheia``...).
    reason: str | None = None
    run_id: UUID | None = None
    created_at: datetime


class ScheduleService(Protocol):
    """Agendas: um robô publicado rodando por uma expressão cron, num fuso.

    Qualquer papel na área vê as agendas dos robôs dela; só quem opera (ou
    administra) a área cria, muda e remove (:func:`can_automate`).
    """

    async def list_schedules(self, principal: Principal) -> list[ScheduleInfo]: ...

    async def get_schedule(
        self, principal: Principal, schedule_id: UUID
    ) -> ScheduleInfo | None: ...

    async def create_schedule(self, principal: Principal, spec: ScheduleSpec) -> ScheduleInfo: ...

    async def update_schedule(
        self, principal: Principal, schedule_id: UUID, changes: ScheduleChanges
    ) -> ScheduleInfo: ...

    async def delete_schedule(self, principal: Principal, schedule_id: UUID) -> None: ...

    async def list_occurrences(
        self, principal: Principal, schedule_id: UUID, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        """Ocorrências da agenda, das mais novas às mais antigas."""
        ...


class WebhookSpec(_Model):
    title: str = Field(min_length=1, max_length=120)
    #: Entregas aceitas por minuto, contadas por webhook.
    rate_per_minute: int = Field(default=60, ge=1, le=6000)
    enabled: bool = True


class WebhookChanges(_Model):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    rate_per_minute: int | None = Field(default=None, ge=1, le=6000)
    enabled: bool | None = None


class WebhookInfo(_Model):
    """Um webhook de um robô publicado. A chave de assinatura nunca aparece aqui."""

    webhook_id: UUID
    company_id: UUID
    area_id: UUID
    robot_id: UUID
    robot_slug: str
    title: str
    enabled: bool
    rate_per_minute: int
    #: Caminho da entrega (``POST``, sem token, assinado com a chave).
    path: str
    #: Quantas vezes a chave já foi gerada (1 na criação, mais 1 a cada troca).
    key_version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    rotated_at: datetime | None = None
    last_delivery_at: datetime | None = None


class WebhookSecret(_Model):
    """A chave de assinatura de um webhook: só aparece na criação e na troca, uma vez."""

    webhook: WebhookInfo
    secret: str = Field(min_length=32, max_length=200, repr=False)


class WebhookDelivery(_Model):
    """O que a entrega de um webhook criou (ou já tinha criado, com o mesmo ``delivery_id``)."""

    webhook_id: UUID
    delivery_id: str
    run_id: UUID
    state: RunState
    #: A entrega repetida devolve a mesma execução, sem criar outra.
    duplicate: bool = False


class WebhookService(Protocol):
    """Webhooks dos robôs publicados: criar, listar, mudar, trocar a chave e remover."""

    async def list_webhooks(self, principal: Principal, robot_id: UUID) -> list[WebhookInfo]: ...

    async def get_webhook(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID
    ) -> WebhookInfo | None: ...

    async def create_webhook(
        self, principal: Principal, robot_id: UUID, spec: WebhookSpec
    ) -> WebhookSecret: ...

    async def update_webhook(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID, changes: WebhookChanges
    ) -> WebhookInfo: ...

    async def rotate_secret(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID
    ) -> WebhookSecret: ...

    async def delete_webhook(
        self, principal: Principal, robot_id: UUID, webhook_id: UUID
    ) -> None: ...


class WebhookReceiver(Protocol):
    """A entrega de um webhook, sem token: autenticada só pela assinatura.

    ``client`` é o endereço de origem da requisição, para os limites de quem erra.
    """

    async def deliver(
        self,
        webhook_id: UUID,
        *,
        timestamp: str | None,
        delivery_id: str | None,
        signature: str | None,
        body: bytes,
        client: str | None = None,
    ) -> WebhookDelivery: ...


class BatchFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"


class BatchColumn(_Model):
    """Uma coluna da planilha e a entrada do robô que ela preenche (``None``: ignorada)."""

    header: str
    input: str | None = None


class BatchRowError(_Model):
    """Os problemas de uma linha da planilha (a linha 1 é a dos títulos)."""

    line: int = Field(ge=2)
    errors: list[str]


class BatchPreview(_Model):
    """O que um lote criaria, conferido linha a linha, sem criar nada."""

    robot_id: UUID
    robot_slug: str
    version_no: int
    file_name: str
    format: BatchFormat
    file_sha256: str
    rows: int
    columns: list[BatchColumn]
    #: Problemas da planilha inteira (por exemplo, a coluna de uma entrada obrigatória).
    file_errors: list[str] = Field(default_factory=list)
    #: Problemas por linha (as primeiras; ``error_rows`` conta todas).
    errors: list[BatchRowError] = Field(default_factory=list)
    error_rows: int = 0
    #: As primeiras linhas, já convertidas para as entradas do robô.
    sample: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.file_errors and not self.error_rows


class BatchInfo(_Model):
    """Um lote: uma execução por linha, com o andamento somado."""

    batch_id: UUID
    company_id: UUID
    area_id: UUID
    robot_id: UUID
    robot_slug: str
    version_no: int
    file_name: str
    format: BatchFormat
    total: int
    #: Execuções do lote por situação (só as que têm alguma).
    counts: dict[RunState, int] = Field(default_factory=dict)
    created_by: str
    created_at: datetime

    @property
    def done(self) -> int:
        return sum(n for state, n in self.counts.items() if state.finished)

    @property
    def finished(self) -> bool:
        return self.done >= self.total


class BatchRow(_Model):
    """Uma linha do lote e a execução dela."""

    row_no: int
    line: int
    run_id: UUID
    state: RunState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Só as saídas que o robô declara.
    outputs: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class BatchFile(_Model):
    """O resultado de um lote como arquivo (CSV ou xlsx)."""

    file_name: str
    media_type: str
    content: bytes = Field(repr=False)


class BatchService(Protocol):
    """Lote por planilha: conferir, criar (tudo ou nada), acompanhar e baixar o resultado."""

    async def preview_batch(
        self,
        principal: Principal,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
    ) -> BatchPreview: ...

    async def create_batch(
        self,
        principal: Principal,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> BatchInfo: ...

    async def list_batches(self, principal: Principal, limit: int = 50) -> list[BatchInfo]: ...

    async def get_batch(self, principal: Principal, batch_id: UUID) -> BatchInfo | None: ...

    async def list_batch_rows(self, principal: Principal, batch_id: UUID) -> list[BatchRow]: ...

    async def batch_results(
        self, principal: Principal, batch_id: UUID, format: BatchFormat
    ) -> BatchFile: ...


# ---------------------------------------------------------------------------
# Consertar (o passo que quebrou)
# ---------------------------------------------------------------------------


class RepairStepInfo(_Model):
    """O passo que quebrou, como está na versão da execução que falhou."""

    step_id: str
    title: str
    kind: str
    effect: Effect
    intent: str
    locators: list[Locator]
    frames: list[str] = Field(default_factory=list)
    within: Locator | None = None
    within_has_text: str | None = None


class RepairCandidate(_Model):
    """Um elemento do retrato que pode ser o alvo do passo, com o alvo já montado.

    Os candidatos saem só do retrato da página (nada é executado de novo para
    montá-los) e vêm do mais provável para o menos provável, pela nota
    determinística ``score`` (0 a 100). ``target`` é o alvo que o passo ganha se a
    pessoa escolher este candidato: os localizadores montados pelas regras do
    compilador, o iframe e, num passo dentro de uma linha, a mesma linha de antes.
    """

    candidate_id: str = Field(pattern=r"^c[0-9]{1,3}$")
    description: str
    role: str | None = None
    tag: str
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    test_id: str | None = None
    row_text: str | None = None
    frames: list[str] = Field(default_factory=list)
    sensitive: bool = False
    enabled: bool = True
    score: int = Field(ge=0, le=100)
    #: Por que o candidato ganhou a nota, em pt-BR.
    reasons: list[str] = Field(default_factory=list)
    target: Target


class RepairSuggestionStatus(StrEnum):
    """O que aconteceu com o pedido ao modelo de linguagem."""

    #: O modelo escolheu um dos candidatos.
    CHOSEN = "escolhido"
    #: O modelo disse que nenhum candidato serve.
    NONE = "nenhum"
    #: O hub não respondeu, ou a resposta não passou no esquema.
    UNAVAILABLE = "indisponivel"
    #: O modelo escolheu um candidato que não estava na lista: o código recusou.
    REFUSED = "recusada"
    #: Não há hub configurado, ou a sugestão não foi pedida.
    OFF = "desligado"


class RepairSuggestion(_Model):
    """A sugestão do modelo: só um dos candidatos, ou nenhum. Quem decide é a pessoa."""

    status: RepairSuggestionStatus
    candidate_id: str | None = None
    #: O motivo dado pelo modelo (texto dele, já sem marcadores de controle).
    reason: str | None = None
    #: O que a tela mostra, em pt-BR.
    message: str


class RepairProposalInfo(_Model):
    """Um conserto pedido: a versão nova (rascunho) criada com o candidato escolhido."""

    proposal_id: UUID
    run_id: UUID
    robot_id: UUID
    step_id: str
    #: A versão da execução que falhou, sobre a qual o conserto foi feito.
    base_version_no: int
    #: O rascunho criado com o passo consertado; uma outra pessoa, que aprova, publica.
    version_no: int
    candidate_id: str
    #: O que o modelo sugeriu quando a pessoa escolheu (``None``: não sugeriu).
    model_candidate_id: str | None = None
    requested_by: str
    created_at: datetime


class RepairView(_Model):
    """O conserto de uma execução que falhou: o passo, os candidatos e a sugestão do modelo."""

    run_id: UUID
    robot_id: UUID
    robot_slug: str
    area_id: UUID
    version_no: int
    error_code: str
    error_message: str | None = None
    page_url: str
    page_title: str
    step: RepairStepInfo
    candidates: list[RepairCandidate]
    suggestion: RepairSuggestion
    #: O retrato não guardou todos os elementos da página (página grande demais).
    truncated: bool = False
    #: O conserto já pedido para esta execução (só um por execução).
    proposal: RepairProposalInfo | None = None
    #: Por que um conserto novo não pode ser pedido a partir desta execução (já pedido,
    #: versão antiga), em pt-BR; ``None`` quando pode.
    blocked_reason: str | None = None


class RepairService(Protocol):
    """Consertar o passo que quebrou: candidatos enumerados, a escolha de uma pessoa e
    um rascunho novo, que outra pessoa (com o papel de aprovação) publica."""

    async def get_repair(
        self, principal: Principal, run_id: UUID, *, suggest: bool = True
    ) -> RepairView: ...

    async def create_repair(
        self, principal: Principal, run_id: UUID, candidate_id: str
    ) -> RepairProposalInfo: ...


# ---------------------------------------------------------------------------
# Credenciais do cofre e as regras do certificado A1
# ---------------------------------------------------------------------------


class CredentialStatus(StrEnum):
    """Situação de uma credencial registrada."""

    ACTIVE = "ativa"
    #: Certificado A1 que vence dentro do prazo do aviso.
    EXPIRING = "vencendo"
    #: Certificado A1 vencido (ou sem a validade registrada: conta como vencido).
    EXPIRED = "vencida"
    REVOKED = "revogada"


class CredentialInfo(_Model):
    """Uma credencial registrada: só a referência ao cofre e os metadados, nunca o valor."""

    credential_id: UUID
    area_id: UUID
    name: str
    kind: CredentialKind
    origin: str
    custodian_subject: str | None = None
    custodian_name: str | None = None
    #: Validade do certificado (só A1).
    not_after: datetime | None = None
    #: Impressão SHA-256 do certificado (64 hexadecimais minúsculos).
    fingerprint_sha256: str | None = None
    status: CredentialStatus
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    #: Caminho no cofre; só para quem administra a área e para o custodiante.
    vault_path: str | None = None


class A1AuthorizationInfo(_Model):
    """A autorização do custodiante para um robô usar um certificado A1 num portal."""

    authorization_id: UUID
    robot_id: UUID
    area_id: UUID
    credential_id: UUID
    credential_name: str
    origin: str
    #: A impressão do certificado no momento da autorização.
    fingerprint_sha256: str | None = None
    authorized_by: str
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    #: Ativa e com a impressão atual do certificado (a que vale para rodar).
    current: bool = False


class ApprovalInfo(_Model):
    approved_by: str
    approved_at: datetime


class VersionApprovals(_Model):
    """As aprovações de uma versão: quem aprovou, quantas faltam e o que impede."""

    robot_id: UUID
    version_no: int
    status: VersionStatus
    required: int
    approvals: list[ApprovalInfo] = Field(default_factory=list)
    missing: int = 0
    #: O que falta no certificado A1 para contar a próxima aprovação (sem segredos).
    problems: list[str] = Field(default_factory=list)


class CredentialService(Protocol):
    """Credenciais registradas (referências ao cofre) e as regras do certificado A1."""

    async def register(
        self,
        principal: Principal,
        area_id: UUID,
        *,
        name: str,
        kind: CredentialKind,
        origin: str,
        vault_path: str,
        custodian_subject: str | None = None,
        not_after: datetime | None = None,
        fingerprint: str | None = None,
    ) -> CredentialInfo: ...

    async def renew(
        self,
        principal: Principal,
        credential_id: UUID,
        *,
        not_after: datetime,
        fingerprint: str | None = None,
    ) -> CredentialInfo: ...

    async def revoke(self, principal: Principal, credential_id: UUID) -> CredentialInfo: ...

    async def list(
        self, principal: Principal, area_id: UUID | None = None
    ) -> builtins.list[CredentialInfo]: ...

    async def expiring(
        self, principal: Principal, within_days: int | None = None
    ) -> builtins.list[CredentialInfo]: ...

    async def authorize(
        self, principal: Principal, robot_id: UUID, credential_name: str
    ) -> A1AuthorizationInfo: ...

    async def revoke_authorization(
        self, principal: Principal, authorization_id: UUID, *, robot_id: UUID | None = None
    ) -> A1AuthorizationInfo: ...

    async def list_authorizations(
        self, principal: Principal, robot_id: UUID
    ) -> builtins.list[A1AuthorizationInfo]: ...

    async def approvals(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> VersionApprovals: ...
