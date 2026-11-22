"""Acesso da interface aos dados do RaPA.

A interface é fina: toda regra fica no plano de controle. As telas só usam o
protocolo :class:`Backend`, com duas implementações:

- :class:`ContractsBackend`, que chama diretamente objetos que seguem os
  contratos de :mod:`rapa.core.contracts` (a plataforma em memória nos testes e
  no modo de desenvolvimento num processo só);
- :class:`RestBackend`, que fala com a API REST ``/v1`` usando o token de
  acesso da sessão, guardado no servidor da interface.

Os erros de negócio chegam como as exceções de :mod:`rapa.core.contracts`
(``NotFound``, ``Forbidden``, ``Conflict``, ``InvalidInput``,
``QuotaExceeded``), com a mensagem em pt-BR; problemas de sessão e de rede
viram :class:`NotAuthenticated` e :class:`BackendUnavailable`.
"""

import json
import logging
import math
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote
from uuid import UUID

import httpx2
from pydantic import TypeAdapter, ValidationError

from rapa.core.contracts import (
    BatchFile,
    BatchFormat,
    BatchInfo,
    BatchPreview,
    BatchRow,
    BatchService,
    Conflict,
    ContractError,
    DraftChoices,
    Forbidden,
    GeneralizationProposals,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RecordingService,
    RecordingSessionInfo,
    RepairProposalInfo,
    RepairService,
    RepairView,
    RobotAdmin,
    RobotCatalog,
    RobotInfo,
    RobotSummary,
    RobotVersionInfo,
    RunService,
    RunStatus,
    RunTrigger,
    ScheduleChanges,
    ScheduleInfo,
    ScheduleOccurrence,
    ScheduleService,
    ScheduleSpec,
    ViewerTicket,
    WebhookChanges,
    WebhookInfo,
    WebhookSecret,
    WebhookService,
    WebhookSpec,
)
from rapa.core.robot import SLUG_PATTERN, Manifest

if TYPE_CHECKING:
    from rapa.ui.auth import UiSession

__all__ = [
    "BATCHES_OFF",
    "REPAIRS_OFF",
    "SCHEDULES_OFF",
    "SESSIONS_OFF",
    "WEBHOOKS_OFF",
    "Backend",
    "BackendError",
    "BackendUnavailable",
    "ContractsBackend",
    "ContractsBackendFactory",
    "ManifestLike",
    "NotAuthenticated",
    "RestBackend",
    "RestBackendFactory",
    "coerce_manifest",
    "describe_error",
]

logger = logging.getLogger(__name__)

type ManifestLike = Manifest | Mapping[str, Any] | str

_SLUG = re.compile(SLUG_PATTERN)


class BackendError(Exception):
    """Erro da interface ao falar com o plano de controle, com mensagem em pt-BR."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAuthenticated(BackendError):
    """A sessão acabou ou o token foi recusado: é preciso entrar de novo."""


class BackendUnavailable(BackendError):
    """O plano de controle não respondeu ou respondeu algo inesperado."""


class Backend(Protocol):
    """O que as telas precisam do plano de controle, já no nome de quem está logado."""

    async def list_catalog(self) -> list[RobotSummary]: ...

    async def search_catalog(self, query: str, limit: int = 10) -> list[RobotSummary]: ...

    async def get_catalog_robot(self, slug: str) -> RobotSummary | None: ...

    async def list_robots(self) -> list[RobotInfo]: ...

    async def list_versions(self, robot_id: UUID) -> list[RobotVersionInfo]: ...

    async def get_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo | None: ...

    async def create_version(self, area_id: UUID, manifest: ManifestLike) -> RobotVersionInfo: ...

    async def validate_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo: ...

    async def publish_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo: ...

    async def start_run(
        self, slug: str, inputs: Mapping[str, Any], idempotency_key: str | None = None
    ) -> RunStatus: ...

    async def get_run(self, run_id: UUID) -> RunStatus | None: ...

    async def list_runs(self, limit: int = 50) -> list[RunStatus]: ...

    # -- gravação (Demonstrar e Generalizar) ------------------------------------

    async def list_sessions(self, limit: int = 20) -> list[RecordingSessionInfo]: ...

    async def get_session(self, session_id: UUID) -> RecordingSessionInfo | None: ...

    async def create_session(
        self, area_id: UUID, start_url: str, title: str = ""
    ) -> RecordingSessionInfo: ...

    async def issue_ticket(self, session_id: UUID) -> ViewerTicket: ...

    async def stop_session(self, session_id: UUID) -> RecordingSessionInfo: ...

    async def get_proposals(self, session_id: UUID) -> GeneralizationProposals: ...

    async def create_draft(self, session_id: UUID, choices: DraftChoices) -> RobotVersionInfo: ...

    # -- publicar: agendas, webhooks e lotes ----------------------------------------

    async def list_schedules(self) -> list[ScheduleInfo]: ...

    async def get_schedule(self, schedule_id: UUID) -> ScheduleInfo | None: ...

    async def create_schedule(self, spec: ScheduleSpec) -> ScheduleInfo: ...

    async def update_schedule(
        self, schedule_id: UUID, changes: ScheduleChanges
    ) -> ScheduleInfo: ...

    async def delete_schedule(self, schedule_id: UUID) -> None: ...

    async def list_occurrences(
        self, schedule_id: UUID, limit: int = 50
    ) -> list[ScheduleOccurrence]: ...

    async def list_webhooks(self, robot_id: UUID) -> list[WebhookInfo]: ...

    async def create_webhook(self, robot_id: UUID, spec: WebhookSpec) -> WebhookSecret: ...

    async def update_webhook(
        self, robot_id: UUID, webhook_id: UUID, changes: WebhookChanges
    ) -> WebhookInfo: ...

    async def rotate_webhook(self, robot_id: UUID, webhook_id: UUID) -> WebhookSecret: ...

    async def delete_webhook(self, robot_id: UUID, webhook_id: UUID) -> None: ...

    async def preview_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
    ) -> BatchPreview: ...

    async def create_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> BatchInfo: ...

    async def list_batches(self, limit: int = 50) -> list[BatchInfo]: ...

    async def get_batch(self, batch_id: UUID) -> BatchInfo | None: ...

    async def list_batch_rows(self, batch_id: UUID) -> list[BatchRow]: ...

    async def batch_results(self, batch_id: UUID, format: BatchFormat) -> BatchFile: ...

    # -- consertar ---------------------------------------------------------------------

    async def get_repair(self, run_id: UUID, *, suggest: bool = True) -> RepairView: ...

    async def create_repair(self, run_id: UUID, candidate_id: str) -> RepairProposalInfo: ...


#: Mensagem quando este servidor não oferece a gravação de demonstrações.
SESSIONS_OFF = "a gravação de demonstrações não está disponível neste servidor"
#: Mensagens quando este servidor não oferece agendas, webhooks ou lotes.
SCHEDULES_OFF = "as agendas não estão disponíveis neste servidor"
WEBHOOKS_OFF = "os webhooks não estão disponíveis neste servidor"
BATCHES_OFF = "os lotes por planilha não estão disponíveis neste servidor"
#: Mensagem quando este servidor não oferece o conserto assistido.
REPAIRS_OFF = "o conserto assistido não está disponível neste servidor"


# ---------------------------------------------------------------------------
# Manifesto vindo de JSON
# ---------------------------------------------------------------------------

_PYDANTIC_MESSAGES = {
    "missing": "campo obrigatório ausente",
    "extra_forbidden": "campo não permitido",
    "string_type": "esperado texto",
    "int_type": "esperado número inteiro",
    "int_parsing": "esperado número inteiro",
    "bool_type": "esperado verdadeiro ou falso",
    "dict_type": "esperado objeto",
    "list_type": "esperada lista",
    "string_too_short": "texto curto demais",
    "string_too_long": "texto longo demais",
    "string_pattern_mismatch": "formato inválido",
    "too_short": "itens de menos",
    "too_long": "itens demais",
    "greater_than_equal": "valor abaixo do mínimo",
    "less_than_equal": "valor acima do máximo",
    "literal_error": "valor fora das opções permitidas",
    "enum": "valor fora das opções permitidas",
    "union_tag_invalid": "tipo de passo ou de valor desconhecido",
    "union_tag_not_found": "falta o campo que identifica o tipo",
    "model_type": "esperado objeto",
    "model_attributes_type": "esperado objeto",
}


def _location(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "manifesto"


def _pydantic_errors(exc: ValidationError) -> list[str]:
    """Erros do pydantic em pt-BR, sem repetir o valor recebido."""
    out: list[str] = []
    for error in exc.errors()[:30]:
        kind = error.get("type", "")
        if kind == "value_error":
            cause = (error.get("ctx") or {}).get("error")
            message = str(cause) if cause is not None else "valor inválido"
        else:
            message = _PYDANTIC_MESSAGES.get(kind, "valor inválido")
        # Nas uniões discriminadas o pydantic põe o tipo no caminho; ele não ajuda quem lê.
        loc = tuple(p for p in error.get("loc", ()) if not (isinstance(p, str) and "-" in p))
        out.append(f"{_location(loc)}: {message}")
    return out


def coerce_manifest(value: ManifestLike) -> Manifest:
    """Manifesto validado a partir de um modelo, de um dicionário ou de um texto JSON."""
    if isinstance(value, Manifest):
        return value
    data: Any = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvalidInput(
                "o manifesto não é um JSON válido",
                [f"linha {exc.lineno}, coluna {exc.colno}: JSON mal formado"],
            ) from None
    if not isinstance(data, Mapping):
        raise InvalidInput("o manifesto precisa ser um objeto JSON", [])
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise InvalidInput("o manifesto é inválido", _pydantic_errors(exc)) from None


# ---------------------------------------------------------------------------
# Implementação sobre os contratos (num processo só)
# ---------------------------------------------------------------------------


class ContractsBackend:
    """Usa diretamente os contratos do núcleo, sempre com o principal da sessão."""

    def __init__(
        self,
        catalog: RobotCatalog,
        runs: RunService,
        admin: RobotAdmin,
        principal: Principal,
        *,
        sessions: RecordingService | None = None,
        schedules: ScheduleService | None = None,
        webhooks: WebhookService | None = None,
        batches: BatchService | None = None,
        repairs: RepairService | None = None,
    ) -> None:
        self._catalog = catalog
        self._runs = runs
        self._admin = admin
        self._principal = principal
        self._sessions = sessions
        self._schedules = schedules
        self._webhooks = webhooks
        self._batches = batches
        self._repairs = repairs

    def __repr__(self) -> str:
        return f"ContractsBackend(subject={self._principal.subject!r})"

    async def list_catalog(self) -> list[RobotSummary]:
        return await self._catalog.list_published(self._principal)

    async def search_catalog(self, query: str, limit: int = 10) -> list[RobotSummary]:
        return await self._catalog.search(self._principal, query, limit)

    async def get_catalog_robot(self, slug: str) -> RobotSummary | None:
        if not _SLUG.fullmatch(slug):
            return None
        return await self._catalog.get_published(self._principal, slug)

    async def list_robots(self) -> list[RobotInfo]:
        return await self._admin.list_robots(self._principal)

    async def list_versions(self, robot_id: UUID) -> list[RobotVersionInfo]:
        try:
            return await self._admin.list_versions(self._principal, robot_id)
        except NotFound:
            return []

    async def get_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo | None:
        if version_no < 1:
            return None
        return await self._admin.get_version(self._principal, robot_id, version_no)

    async def create_version(self, area_id: UUID, manifest: ManifestLike) -> RobotVersionInfo:
        return await self._admin.create_version(self._principal, area_id, coerce_manifest(manifest))

    async def validate_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo:
        return await self._admin.mark_validated(self._principal, robot_id, version_no)

    async def publish_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo:
        return await self._admin.publish(self._principal, robot_id, version_no)

    async def start_run(
        self, slug: str, inputs: Mapping[str, Any], idempotency_key: str | None = None
    ) -> RunStatus:
        if not _SLUG.fullmatch(slug):
            raise NotFound("robô não encontrado")
        return await self._runs.start(
            self._principal, slug, dict(inputs), RunTrigger.MANUAL, idempotency_key
        )

    async def get_run(self, run_id: UUID) -> RunStatus | None:
        return await self._runs.get(self._principal, run_id)

    async def list_runs(self, limit: int = 50) -> list[RunStatus]:
        return await self._runs.list_recent(self._principal, limit)

    def _recording(self) -> RecordingService:
        if self._sessions is None:
            raise BackendUnavailable(SESSIONS_OFF)
        return self._sessions

    async def list_sessions(self, limit: int = 20) -> list[RecordingSessionInfo]:
        return await self._recording().list_sessions(self._principal, limit)

    async def get_session(self, session_id: UUID) -> RecordingSessionInfo | None:
        return await self._recording().get_session(self._principal, session_id)

    async def create_session(
        self, area_id: UUID, start_url: str, title: str = ""
    ) -> RecordingSessionInfo:
        return await self._recording().create_session(self._principal, area_id, start_url, title)

    async def issue_ticket(self, session_id: UUID) -> ViewerTicket:
        return await self._recording().issue_ticket(self._principal, session_id)

    async def stop_session(self, session_id: UUID) -> RecordingSessionInfo:
        return await self._recording().stop_session(self._principal, session_id)

    async def get_proposals(self, session_id: UUID) -> GeneralizationProposals:
        return await self._recording().get_proposals(self._principal, session_id)

    async def create_draft(self, session_id: UUID, choices: DraftChoices) -> RobotVersionInfo:
        return await self._recording().create_draft(self._principal, session_id, choices)

    # -- publicar --------------------------------------------------------------------

    def _schedule_service(self) -> ScheduleService:
        if self._schedules is None:
            raise BackendUnavailable(SCHEDULES_OFF)
        return self._schedules

    def _webhook_service(self) -> WebhookService:
        if self._webhooks is None:
            raise BackendUnavailable(WEBHOOKS_OFF)
        return self._webhooks

    def _batch_service(self) -> BatchService:
        if self._batches is None:
            raise BackendUnavailable(BATCHES_OFF)
        return self._batches

    async def list_schedules(self) -> list[ScheduleInfo]:
        return await self._schedule_service().list_schedules(self._principal)

    async def get_schedule(self, schedule_id: UUID) -> ScheduleInfo | None:
        return await self._schedule_service().get_schedule(self._principal, schedule_id)

    async def create_schedule(self, spec: ScheduleSpec) -> ScheduleInfo:
        return await self._schedule_service().create_schedule(self._principal, spec)

    async def update_schedule(self, schedule_id: UUID, changes: ScheduleChanges) -> ScheduleInfo:
        return await self._schedule_service().update_schedule(self._principal, schedule_id, changes)

    async def delete_schedule(self, schedule_id: UUID) -> None:
        await self._schedule_service().delete_schedule(self._principal, schedule_id)

    async def list_occurrences(
        self, schedule_id: UUID, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        return await self._schedule_service().list_occurrences(self._principal, schedule_id, limit)

    async def list_webhooks(self, robot_id: UUID) -> list[WebhookInfo]:
        return await self._webhook_service().list_webhooks(self._principal, robot_id)

    async def create_webhook(self, robot_id: UUID, spec: WebhookSpec) -> WebhookSecret:
        return await self._webhook_service().create_webhook(self._principal, robot_id, spec)

    async def update_webhook(
        self, robot_id: UUID, webhook_id: UUID, changes: WebhookChanges
    ) -> WebhookInfo:
        return await self._webhook_service().update_webhook(
            self._principal, robot_id, webhook_id, changes
        )

    async def rotate_webhook(self, robot_id: UUID, webhook_id: UUID) -> WebhookSecret:
        return await self._webhook_service().rotate_secret(self._principal, robot_id, webhook_id)

    async def delete_webhook(self, robot_id: UUID, webhook_id: UUID) -> None:
        await self._webhook_service().delete_webhook(self._principal, robot_id, webhook_id)

    async def preview_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
    ) -> BatchPreview:
        return await self._batch_service().preview_batch(
            self._principal, robot_id, file_name, data, columns
        )

    async def create_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> BatchInfo:
        return await self._batch_service().create_batch(
            self._principal, robot_id, file_name, data, columns, idempotency_key
        )

    async def list_batches(self, limit: int = 50) -> list[BatchInfo]:
        return await self._batch_service().list_batches(self._principal, limit)

    async def get_batch(self, batch_id: UUID) -> BatchInfo | None:
        return await self._batch_service().get_batch(self._principal, batch_id)

    async def list_batch_rows(self, batch_id: UUID) -> list[BatchRow]:
        return await self._batch_service().list_batch_rows(self._principal, batch_id)

    async def batch_results(self, batch_id: UUID, format: BatchFormat) -> BatchFile:
        return await self._batch_service().batch_results(self._principal, batch_id, format)

    # -- consertar ---------------------------------------------------------------------

    def _repair_service(self) -> RepairService:
        if self._repairs is None:
            raise BackendUnavailable(REPAIRS_OFF)
        return self._repairs

    async def get_repair(self, run_id: UUID, *, suggest: bool = True) -> RepairView:
        return await self._repair_service().get_repair(self._principal, run_id, suggest=suggest)

    async def create_repair(self, run_id: UUID, candidate_id: str) -> RepairProposalInfo:
        return await self._repair_service().create_repair(self._principal, run_id, candidate_id)


class _Platform(RobotCatalog, RunService, RobotAdmin, Protocol):
    """Um objeto que segue os três contratos (como a plataforma em memória)."""


class ContractsBackendFactory:
    """Cria um :class:`ContractsBackend` para o principal de cada sessão."""

    def __init__(
        self,
        catalog: RobotCatalog,
        runs: RunService,
        admin: RobotAdmin,
        *,
        sessions: RecordingService | None = None,
        schedules: ScheduleService | None = None,
        webhooks: WebhookService | None = None,
        batches: BatchService | None = None,
        repairs: RepairService | None = None,
    ) -> None:
        self._catalog = catalog
        self._runs = runs
        self._admin = admin
        self._sessions = sessions
        self._schedules = schedules
        self._webhooks = webhooks
        self._batches = batches
        self._repairs = repairs

    @classmethod
    def from_platform(
        cls,
        platform: _Platform,
        *,
        sessions: RecordingService | None = None,
        schedules: ScheduleService | None = None,
        webhooks: WebhookService | None = None,
        batches: BatchService | None = None,
        repairs: RepairService | None = None,
    ) -> ContractsBackendFactory:
        return cls(
            platform,
            platform,
            platform,
            sessions=sessions,
            schedules=schedules,
            webhooks=webhooks,
            batches=batches,
            repairs=repairs,
        )

    def __call__(self, session: UiSession) -> ContractsBackend:
        return ContractsBackend(
            self._catalog,
            self._runs,
            self._admin,
            session.principal,
            sessions=self._sessions,
            schedules=self._schedules,
            webhooks=self._webhooks,
            batches=self._batches,
            repairs=self._repairs,
        )


# ---------------------------------------------------------------------------
# Implementação sobre a API REST
# ---------------------------------------------------------------------------

_SUMMARIES: TypeAdapter[list[RobotSummary]] = TypeAdapter(list[RobotSummary])
_SUMMARY: TypeAdapter[RobotSummary] = TypeAdapter(RobotSummary)
_ROBOTS: TypeAdapter[list[RobotInfo]] = TypeAdapter(list[RobotInfo])
_VERSION: TypeAdapter[RobotVersionInfo] = TypeAdapter(RobotVersionInfo)
_VERSIONS: TypeAdapter[list[RobotVersionInfo]] = TypeAdapter(list[RobotVersionInfo])
_RUN: TypeAdapter[RunStatus] = TypeAdapter(RunStatus)
_RUNS: TypeAdapter[list[RunStatus]] = TypeAdapter(list[RunStatus])
_SESSION: TypeAdapter[RecordingSessionInfo] = TypeAdapter(RecordingSessionInfo)
_SESSIONS: TypeAdapter[list[RecordingSessionInfo]] = TypeAdapter(list[RecordingSessionInfo])
_TICKET: TypeAdapter[ViewerTicket] = TypeAdapter(ViewerTicket)
_PROPOSALS: TypeAdapter[GeneralizationProposals] = TypeAdapter(GeneralizationProposals)
_SCHEDULE: TypeAdapter[ScheduleInfo] = TypeAdapter(ScheduleInfo)
_SCHEDULES: TypeAdapter[list[ScheduleInfo]] = TypeAdapter(list[ScheduleInfo])
_OCCURRENCES: TypeAdapter[list[ScheduleOccurrence]] = TypeAdapter(list[ScheduleOccurrence])
_WEBHOOK: TypeAdapter[WebhookInfo] = TypeAdapter(WebhookInfo)
_WEBHOOKS: TypeAdapter[list[WebhookInfo]] = TypeAdapter(list[WebhookInfo])
_WEBHOOK_SECRET: TypeAdapter[WebhookSecret] = TypeAdapter(WebhookSecret)
_PREVIEW: TypeAdapter[BatchPreview] = TypeAdapter(BatchPreview)
_BATCH: TypeAdapter[BatchInfo] = TypeAdapter(BatchInfo)
_BATCHES: TypeAdapter[list[BatchInfo]] = TypeAdapter(list[BatchInfo])
_BATCH_ROWS: TypeAdapter[list[BatchRow]] = TypeAdapter(list[BatchRow])
_REPAIR: TypeAdapter[RepairView] = TypeAdapter(RepairView)
_REPAIR_PROPOSAL: TypeAdapter[RepairProposalInfo] = TypeAdapter(RepairProposalInfo)

_MAX_MESSAGE = 500
_MAX_DETAILS = 50
_UNAVAILABLE = "o serviço do RaPA não respondeu; tente de novo em instantes"
_UNEXPECTED = "o serviço do RaPA respondeu algo inesperado; tente de novo mais tarde"


def _clean(text: Any, default: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return default
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= _MAX_MESSAGE else cleaned[: _MAX_MESSAGE - 1] + "…"


def _retry_after(value: str | None) -> int:
    try:
        seconds = float(value) if value is not None else 1.0
    except ValueError:
        return 1
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else 1


def _api_error(response: httpx2.Response) -> Exception:
    """Converte uma resposta de erro ``{erro, mensagem, detalhes}`` na exceção certa."""
    try:
        body = response.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    message = body.get("mensagem")
    raw_details = body.get("detalhes")
    details = (
        [_clean(d, "") for d in raw_details[:_MAX_DETAILS] if isinstance(d, str)]
        if isinstance(raw_details, list)
        else []
    )
    status = response.status_code
    match status:
        case 401:
            return NotAuthenticated(_clean(message, "sua sessão expirou; entre de novo"))
        case 403:
            return Forbidden(_clean(message, "acesso negado"))
        case 404:
            return NotFound(_clean(message, "não encontrado"))
        case 409:
            return Conflict(_clean(message, "a operação conflita com o estado atual"))
        case 422:
            return InvalidInput(_clean(message, "dados inválidos"), [d for d in details if d])
        case 429:
            return QuotaExceeded(
                _clean(message, "limite de uso atingido; tente de novo mais tarde"),
                _retry_after(response.headers.get("Retry-After")),
            )
    if status == 503:
        return BackendUnavailable(_clean(message, _UNAVAILABLE))
    if status >= 500:
        return BackendUnavailable(_UNAVAILABLE)
    return BackendError(_clean(message, f"o serviço do RaPA recusou o pedido (HTTP {status})"))


class RestBackend:
    """Fala com a API REST ``/v1`` com o token de acesso da sessão.

    O token só vai no cabeçalho ``Authorization`` e nunca aparece em mensagens
    de erro ou na representação do objeto.
    """

    def __init__(self, client: httpx2.AsyncClient, token: str) -> None:
        if not token:
            raise NotAuthenticated("a sessão não tem token de acesso; entre de novo")
        self._client = client
        self._token = token

    def __repr__(self) -> str:
        return f"RestBackend(base_url={str(self._client.base_url)!r})"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        not_found_is_none: bool = False,
    ) -> Any:
        request_headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        request_headers.update(headers or {})
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                params=dict(params) if params else None,
                headers=request_headers,
            )
        except httpx2.TimeoutException:
            raise BackendUnavailable(_UNAVAILABLE) from None
        except httpx2.HTTPError as exc:
            logger.warning("falha ao chamar a API do RaPA: %s", type(exc).__name__)
            raise BackendUnavailable(_UNAVAILABLE) from None
        if response.is_success:
            try:
                return response.json()
            except ValueError:
                raise BackendUnavailable(_UNEXPECTED) from None
        if response.status_code == 404 and not_found_is_none:
            return None
        raise _api_error(response)

    @staticmethod
    def _parse[T](adapter: TypeAdapter[T], data: Any) -> T:
        try:
            return adapter.validate_python(data)
        except ValidationError:
            logger.warning("resposta da API fora do contrato (%s)", adapter)
            raise BackendUnavailable(_UNEXPECTED) from None

    async def list_catalog(self) -> list[RobotSummary]:
        return self._parse(_SUMMARIES, await self._send("GET", "/v1/catalogo"))

    async def search_catalog(self, query: str, limit: int = 10) -> list[RobotSummary]:
        limit = min(max(limit, 1), 50)
        data = await self._send(
            "GET", "/v1/catalogo/busca", params={"q": query[:200], "limite": limit}
        )
        return self._parse(_SUMMARIES, data)

    async def get_catalog_robot(self, slug: str) -> RobotSummary | None:
        if not _SLUG.fullmatch(slug):
            return None
        data = await self._send(
            "GET", f"/v1/catalogo/{quote(slug, safe='')}", not_found_is_none=True
        )
        return None if data is None else self._parse(_SUMMARY, data)

    async def list_robots(self) -> list[RobotInfo]:
        return self._parse(_ROBOTS, await self._send("GET", "/v1/robos"))

    async def list_versions(self, robot_id: UUID) -> list[RobotVersionInfo]:
        data = await self._send("GET", f"/v1/robos/{robot_id}/versoes", not_found_is_none=True)
        return [] if data is None else self._parse(_VERSIONS, data)

    async def get_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo | None:
        if version_no < 1:
            return None
        data = await self._send(
            "GET", f"/v1/robos/{robot_id}/versoes/{version_no}", not_found_is_none=True
        )
        return None if data is None else self._parse(_VERSION, data)

    async def create_version(self, area_id: UUID, manifest: ManifestLike) -> RobotVersionInfo:
        body = {
            "area_id": str(area_id),
            "manifesto": coerce_manifest(manifest).model_dump(mode="json"),
        }
        return self._parse(_VERSION, await self._send("POST", "/v1/robos", json_body=body))

    async def validate_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo:
        data = await self._send("POST", f"/v1/robos/{robot_id}/versoes/{version_no}/validar")
        return self._parse(_VERSION, data)

    async def publish_version(self, robot_id: UUID, version_no: int) -> RobotVersionInfo:
        data = await self._send("POST", f"/v1/robos/{robot_id}/versoes/{version_no}/publicar")
        return self._parse(_VERSION, data)

    async def start_run(
        self, slug: str, inputs: Mapping[str, Any], idempotency_key: str | None = None
    ) -> RunStatus:
        if not _SLUG.fullmatch(slug):
            raise NotFound("robô não encontrado")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data = await self._send(
            "POST",
            f"/v1/catalogo/{quote(slug, safe='')}/execucoes",
            json_body={"entradas": dict(inputs)},
            headers=headers,
        )
        return self._parse(_RUN, data)

    async def get_run(self, run_id: UUID) -> RunStatus | None:
        data = await self._send("GET", f"/v1/execucoes/{run_id}", not_found_is_none=True)
        return None if data is None else self._parse(_RUN, data)

    async def list_runs(self, limit: int = 50) -> list[RunStatus]:
        data = await self._send("GET", "/v1/execucoes", params={"limite": str(limit)})
        return self._parse(_RUNS, data)

    async def list_sessions(self, limit: int = 20) -> list[RecordingSessionInfo]:
        limit = min(max(limit, 1), 100)
        data = await self._send("GET", "/v1/sessoes", params={"limite": str(limit)})
        return self._parse(_SESSIONS, data)

    async def get_session(self, session_id: UUID) -> RecordingSessionInfo | None:
        data = await self._send("GET", f"/v1/sessoes/{session_id}", not_found_is_none=True)
        return None if data is None else self._parse(_SESSION, data)

    async def create_session(
        self, area_id: UUID, start_url: str, title: str = ""
    ) -> RecordingSessionInfo:
        body = {"area_id": str(area_id), "endereco": start_url, "titulo": title}
        return self._parse(_SESSION, await self._send("POST", "/v1/sessoes", json_body=body))

    async def issue_ticket(self, session_id: UUID) -> ViewerTicket:
        data = await self._send("POST", f"/v1/sessoes/{session_id}/bilhete")
        return self._parse(_TICKET, data)

    async def stop_session(self, session_id: UUID) -> RecordingSessionInfo:
        data = await self._send("POST", f"/v1/sessoes/{session_id}/parar")
        return self._parse(_SESSION, data)

    async def get_proposals(self, session_id: UUID) -> GeneralizationProposals:
        data = await self._send("GET", f"/v1/sessoes/{session_id}/propostas")
        return self._parse(_PROPOSALS, data)

    async def create_draft(self, session_id: UUID, choices: DraftChoices) -> RobotVersionInfo:
        data = await self._send(
            "POST",
            f"/v1/sessoes/{session_id}/rascunho",
            json_body=choices.model_dump(mode="json"),
        )
        return self._parse(_VERSION, data)

    # -- publicar --------------------------------------------------------------------

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """Pedido que envia um formulário multipart ou recebe um arquivo."""
        request_headers = {"Authorization": f"Bearer {self._token}"}
        request_headers.update(headers or {})
        try:
            response = await self._client.request(
                method,
                path,
                data=dict(data) if data else None,
                files=dict(files) if files else None,
                params=dict(params) if params else None,
                headers=request_headers,
            )
        except httpx2.TimeoutException:
            raise BackendUnavailable(_UNAVAILABLE) from None
        except httpx2.HTTPError as exc:
            logger.warning("falha ao chamar a API do RaPA: %s", type(exc).__name__)
            raise BackendUnavailable(_UNAVAILABLE) from None
        if not response.is_success:
            raise _api_error(response)
        return response

    async def list_schedules(self) -> list[ScheduleInfo]:
        return self._parse(_SCHEDULES, await self._send("GET", "/v1/agendas"))

    async def get_schedule(self, schedule_id: UUID) -> ScheduleInfo | None:
        data = await self._send("GET", f"/v1/agendas/{schedule_id}", not_found_is_none=True)
        return None if data is None else self._parse(_SCHEDULE, data)

    async def create_schedule(self, spec: ScheduleSpec) -> ScheduleInfo:
        body = {
            "robo_id": str(spec.robot_id),
            "titulo": spec.title,
            "cron": spec.cron,
            "fuso": spec.timezone,
            "entradas": spec.inputs,
            "ativa": spec.enabled,
        }
        return self._parse(_SCHEDULE, await self._send("POST", "/v1/agendas", json_body=body))

    async def update_schedule(self, schedule_id: UUID, changes: ScheduleChanges) -> ScheduleInfo:
        names = {
            "title": "titulo",
            "cron": "cron",
            "timezone": "fuso",
            "inputs": "entradas",
            "enabled": "ativa",
        }
        body = {
            names[key]: value
            for key, value in changes.model_dump(mode="json").items()
            if value is not None
        }
        data = await self._send("PATCH", f"/v1/agendas/{schedule_id}", json_body=body)
        return self._parse(_SCHEDULE, data)

    async def delete_schedule(self, schedule_id: UUID) -> None:
        await self._raw("DELETE", f"/v1/agendas/{schedule_id}")

    async def list_occurrences(
        self, schedule_id: UUID, limit: int = 50
    ) -> list[ScheduleOccurrence]:
        data = await self._send(
            "GET",
            f"/v1/agendas/{schedule_id}/ocorrencias",
            params={"limite": str(min(max(limit, 1), 200))},
        )
        return self._parse(_OCCURRENCES, data)

    async def list_webhooks(self, robot_id: UUID) -> list[WebhookInfo]:
        data = await self._send("GET", f"/v1/robos/{robot_id}/webhooks", not_found_is_none=True)
        return [] if data is None else self._parse(_WEBHOOKS, data)

    async def create_webhook(self, robot_id: UUID, spec: WebhookSpec) -> WebhookSecret:
        body = {"titulo": spec.title, "por_minuto": spec.rate_per_minute, "ativo": spec.enabled}
        data = await self._send("POST", f"/v1/robos/{robot_id}/webhooks", json_body=body)
        return self._parse(_WEBHOOK_SECRET, data)

    async def update_webhook(
        self, robot_id: UUID, webhook_id: UUID, changes: WebhookChanges
    ) -> WebhookInfo:
        names = {"title": "titulo", "rate_per_minute": "por_minuto", "enabled": "ativo"}
        body = {
            names[key]: value
            for key, value in changes.model_dump(mode="json").items()
            if value is not None
        }
        data = await self._send(
            "PATCH", f"/v1/robos/{robot_id}/webhooks/{webhook_id}", json_body=body
        )
        return self._parse(_WEBHOOK, data)

    async def rotate_webhook(self, robot_id: UUID, webhook_id: UUID) -> WebhookSecret:
        data = await self._send("POST", f"/v1/robos/{robot_id}/webhooks/{webhook_id}/trocar-chave")
        return self._parse(_WEBHOOK_SECRET, data)

    async def delete_webhook(self, robot_id: UUID, webhook_id: UUID) -> None:
        await self._raw("DELETE", f"/v1/robos/{robot_id}/webhooks/{webhook_id}")

    @staticmethod
    def _batch_form(robot_id: UUID, columns: dict[str, str] | None) -> dict[str, str]:
        form = {"robo_id": str(robot_id)}
        if columns:
            form["colunas"] = json.dumps(columns, ensure_ascii=False)
        return form

    async def preview_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
    ) -> BatchPreview:
        response = await self._raw(
            "POST",
            "/v1/lotes/previa",
            data=self._batch_form(robot_id, columns),
            files={"arquivo": (file_name, data, "application/octet-stream")},
        )
        return self._parse(_PREVIEW, self._json(response))

    async def create_batch(
        self,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> BatchInfo:
        response = await self._raw(
            "POST",
            "/v1/lotes",
            data=self._batch_form(robot_id, columns),
            files={"arquivo": (file_name, data, "application/octet-stream")},
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return self._parse(_BATCH, self._json(response))

    async def list_batches(self, limit: int = 50) -> list[BatchInfo]:
        data = await self._send("GET", "/v1/lotes", params={"limite": str(min(max(limit, 1), 100))})
        return self._parse(_BATCHES, data)

    async def get_batch(self, batch_id: UUID) -> BatchInfo | None:
        data = await self._send("GET", f"/v1/lotes/{batch_id}", not_found_is_none=True)
        return None if data is None else self._parse(_BATCH, data)

    async def list_batch_rows(self, batch_id: UUID) -> list[BatchRow]:
        return self._parse(_BATCH_ROWS, await self._send("GET", f"/v1/lotes/{batch_id}/linhas"))

    async def batch_results(self, batch_id: UUID, format: BatchFormat) -> BatchFile:
        response = await self._raw(
            "GET", f"/v1/lotes/{batch_id}/resultado", params={"formato": format.value}
        )
        extension = "xlsx" if format is BatchFormat.XLSX else "csv"
        return BatchFile(
            file_name=f"lote-{str(batch_id)[:8]}-resultado.{extension}",
            media_type=response.headers.get("content-type", "application/octet-stream"),
            content=response.content,
        )

    # -- consertar ---------------------------------------------------------------------

    async def get_repair(self, run_id: UUID, *, suggest: bool = True) -> RepairView:
        data = await self._send(
            "GET",
            f"/v1/execucoes/{run_id}/conserto",
            params={"sugestao": "true" if suggest else "false"},
        )
        return self._parse(_REPAIR, data)

    async def create_repair(self, run_id: UUID, candidate_id: str) -> RepairProposalInfo:
        data = await self._send(
            "POST", f"/v1/execucoes/{run_id}/conserto", json_body={"candidato": candidate_id}
        )
        return self._parse(_REPAIR_PROPOSAL, data)

    @staticmethod
    def _json(response: httpx2.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise BackendUnavailable(_UNEXPECTED) from None


class RestBackendFactory:
    """Cria um :class:`RestBackend` com o token da sessão sobre um cliente HTTP compartilhado."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.client = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout_s,
            follow_redirects=False,
        )

    def __call__(self, session: UiSession) -> RestBackend:
        token = session.access_token
        if token is None:
            raise NotAuthenticated("a sessão não tem token de acesso; entre de novo")
        return RestBackend(self.client, token)

    async def aclose(self) -> None:
        await self.client.aclose()


def describe_error(exc: ContractError | BackendError) -> tuple[str, list[str]]:
    """Mensagem principal e detalhes de um erro, prontos para mostrar."""
    details = list(exc.errors) if isinstance(exc, InvalidInput) else []
    message = exc.message
    if isinstance(exc, QuotaExceeded):
        message = f"{message} (tente de novo em {exc.retry_after_s} s)"
    return message[:1].upper() + message[1:], details
