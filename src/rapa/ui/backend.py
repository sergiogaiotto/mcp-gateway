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
    Conflict,
    ContractError,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RobotAdmin,
    RobotCatalog,
    RobotInfo,
    RobotSummary,
    RobotVersionInfo,
    RunService,
    RunStatus,
    RunTrigger,
)
from rapa.core.robot import SLUG_PATTERN, Manifest

if TYPE_CHECKING:
    from rapa.ui.auth import UiSession

__all__ = [
    "MAX_VERSIONS",
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

#: Quantas versões de um robô a interface lista no máximo.
MAX_VERSIONS = 100

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
    ) -> None:
        self._catalog = catalog
        self._runs = runs
        self._admin = admin
        self._principal = principal

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
        versions: list[RobotVersionInfo] = []
        for number in range(1, MAX_VERSIONS + 1):
            info = await self._admin.get_version(self._principal, robot_id, number)
            if info is None:
                break
            versions.append(info)
        return versions

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


class _Platform(RobotCatalog, RunService, RobotAdmin, Protocol):
    """Um objeto que segue os três contratos (como a plataforma em memória)."""


class ContractsBackendFactory:
    """Cria um :class:`ContractsBackend` para o principal de cada sessão."""

    def __init__(self, catalog: RobotCatalog, runs: RunService, admin: RobotAdmin) -> None:
        self._catalog = catalog
        self._runs = runs
        self._admin = admin

    @classmethod
    def from_platform(cls, platform: _Platform) -> ContractsBackendFactory:
        return cls(platform, platform, platform)

    def __call__(self, session: UiSession) -> ContractsBackend:
        return ContractsBackend(self._catalog, self._runs, self._admin, session.principal)


# ---------------------------------------------------------------------------
# Implementação sobre a API REST
# ---------------------------------------------------------------------------

_SUMMARIES: TypeAdapter[list[RobotSummary]] = TypeAdapter(list[RobotSummary])
_SUMMARY: TypeAdapter[RobotSummary] = TypeAdapter(RobotSummary)
_ROBOTS: TypeAdapter[list[RobotInfo]] = TypeAdapter(list[RobotInfo])
_VERSION: TypeAdapter[RobotVersionInfo] = TypeAdapter(RobotVersionInfo)
_RUN: TypeAdapter[RunStatus] = TypeAdapter(RunStatus)

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
        versions: list[RobotVersionInfo] = []
        for number in range(1, MAX_VERSIONS + 1):
            info = await self.get_version(robot_id, number)
            if info is None:
                break
            versions.append(info)
        return versions

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
