"""Aplicação HTTP do plano de controle: REST em ``/v1``, MCP em ``/mcp`` e ``/saude``.

Monta tudo sobre os protocolos de :mod:`rapa.core.contracts`; quem chama
escolhe as implementações (PostgreSQL em produção, memória nos testes).
"""

import importlib.metadata
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rapa.control.api.errors import error_response, install_error_handlers, log_unexpected
from rapa.control.api.publishing import PublishingServices
from rapa.control.api.ratelimit import RateLimiter
from rapa.control.api.routes import ApiServices, create_api_router
from rapa.control.api.sessions import live_origins
from rapa.control.auth import (
    Authenticator,
    BearerVerifier,
    PrincipalResolver,
    StaticTokenVerifier,
    UserProvisioner,
)
from rapa.control.mcp.server import McpGateway, McpSettings
from rapa.control.validation import InputValidator
from rapa.core.contracts import (
    CompanyAdminService,
    CredentialService,
    RepairService,
    RobotAdmin,
    RobotCatalog,
    RunService,
)

if TYPE_CHECKING:
    from rapa.control.sessions.service import RecordingSessions

__all__ = [
    "UI_UPLOAD_PATH",
    "BodySizeLimitMiddleware",
    "CatchAllMiddleware",
    "HttpSettings",
    "create_http_app",
]

logger = logging.getLogger(__name__)

_METADATA_PATH = "/.well-known/oauth-protected-resource"
#: Folga do limite do corpo para os campos e separadores do multipart de um lote.
_MULTIPART_SLACK = 64 * 1024
#: Envio de arquivo da interface (o ``ui.upload`` do NiceGUI, montado na mesma
#: aplicação): é por ele que a tela Lotes manda a planilha.
UI_UPLOAD_PATH = r"/_nicegui/client/[^/]+/upload/[0-9]+"


def _version() -> str:
    try:
        return importlib.metadata.version("rapa")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - sempre instalado
        return "0"


class HttpSettings(BaseModel):
    """Configuração da aplicação HTTP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Tamanho máximo do corpo de uma requisição (REST e MCP).
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    #: Fichas por segundo do limite por empresa; ``None`` desliga o limite.
    rate_limit_per_s: float | None = Field(default=20.0, gt=0)
    rate_limit_burst: int = Field(default=60, ge=1)
    #: Origens com CORS liberado na API REST. Vazio (padrão): CORS desligado.
    cors_allowed_origins: tuple[str, ...] = ()
    #: Origens aceitas no endpoint MCP (cabeçalho Origin).
    mcp_allowed_origins: frozenset[str] = frozenset()
    mcp_json_response: bool = True
    #: Liga o modo de desenvolvimento com tokens fixos (:class:`StaticTokenVerifier`).
    allow_static_tokens: bool = False
    #: URL pública do serviço (sem barra no fim), usada no OpenAPI dos robôs e nos
    #: metadados do recurso protegido.
    public_url: str | None = None
    #: Origens da interface aceitas no websocket da visão ao vivo, além da de ``public_url``
    #: (a interface servida noutro endereço).
    live_allowed_origins: tuple[str, ...] = ()
    #: Emissores (Keycloak) anunciados nos metadados do recurso protegido (RFC 9728).
    authorization_servers: tuple[str, ...] = ()
    #: Expõe ``/v1/docs`` e ``/v1/openapi.json`` da própria API.
    api_docs: bool = False
    validator_cache_size: int = Field(default=256, ge=1)


class BodySizeLimitMiddleware:
    """Recusa com 413 corpos maiores que o limite, antes de qualquer leitura pela aplicação.

    ``path_limits`` dá um limite próprio a caminhos exatos (o envio da planilha de um
    lote, por exemplo, que passa do limite comum); ``path_patterns``, a caminhos que
    casam por inteiro com uma expressão regular (o envio de arquivo da interface,
    que tem os identificadores da página e do elemento no caminho).
    """

    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int,
        path_limits: Mapping[str, int] | None = None,
        path_patterns: Sequence[tuple[str, int]] = (),
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.path_limits = dict(path_limits or {})
        self.path_patterns = [(re.compile(pattern), limit) for pattern, limit in path_patterns]

    def _limit(self, path: str) -> int:
        if (limit := self.path_limits.get(path)) is not None:
            return limit
        for pattern, pattern_limit in self.path_patterns:
            if pattern.fullmatch(path):
                return pattern_limit
        return self.max_body_bytes

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            413, "corpo_muito_grande", "o corpo da requisição é grande demais"
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(str(scope.get("path", "")))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False
            if too_large:
                await self._too_large(scope, receive, send)
                return
        if scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        pending: deque[Message] = deque()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                pending.append(message)
                break
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        pending.appendleft({"type": "http.request", "body": bytes(body), "more_body": False})

        async def replay() -> Message:
            if pending:
                return pending.popleft()
            return await receive()

        await self.app(scope, replay, send)


class CatchAllMiddleware:
    """Último recurso: responde 500 no formato de erro e registra sem a mensagem.

    O ``ServerErrorMiddleware`` do Starlette sempre relança a exceção, e o
    servidor então registraria a mensagem dela; aqui ela para.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            log_unexpected(logger, "erro inesperado na aplicação HTTP", exc)
            if not started:
                response = error_response(
                    500, "erro_interno", "erro interno; tente de novo mais tarde"
                )
                await response(scope, receive, send)


def create_http_app(
    *,
    catalog: RobotCatalog,
    runs: RunService,
    admin: RobotAdmin,
    verifier: BearerVerifier,
    resolver: PrincipalResolver,
    settings: HttpSettings | None = None,
    sessions: RecordingSessions | None = None,
    publishing: PublishingServices | None = None,
    repairs: RepairService | None = None,
    credentials: CredentialService | None = None,
    company: CompanyAdminService | None = None,
    provisioner: UserProvisioner | None = None,
) -> FastAPI:
    """Monta a aplicação FastAPI com REST, MCP, saúde e as proteções comuns.

    ``sessions`` liga as sessões de gravação (``/v1/sessoes`` e a visão ao vivo);
    elas não entram no MCP, que só executa robôs publicados. ``publishing`` liga
    as agendas, os webhooks e os lotes por planilha (também fora do MCP),
    ``repairs``, o conserto assistido (``/v1/execucoes/{id}/conserto``),
    ``credentials``, as credenciais e as regras do certificado A1 (``/v1/credenciais``),
    e ``company``, a administração da empresa (``/v1/empresa``). ``provisioner`` cria,
    no primeiro login, o usuário (sem papéis) de uma organização ligada a uma empresa.
    """
    settings = settings or HttpSettings()
    publishing = publishing or PublishingServices()
    if isinstance(verifier, StaticTokenVerifier) and not settings.allow_static_tokens:
        raise ValueError(
            "tokens fixos só valem em desenvolvimento; ligue allow_static_tokens explicitamente"
        )
    version = _version()
    authenticator = Authenticator(verifier, resolver, provisioner=provisioner)
    limiter = (
        RateLimiter(settings.rate_limit_per_s, settings.rate_limit_burst)
        if settings.rate_limit_per_s is not None
        else None
    )
    validator = InputValidator(cache_size=settings.validator_cache_size)
    public_url = settings.public_url.rstrip("/") if settings.public_url else None
    publish_metadata = public_url is not None and bool(settings.authorization_servers)
    metadata_url = f"{public_url}{_METADATA_PATH}/mcp" if publish_metadata else None

    gateway = McpGateway(
        catalog=catalog,
        runs=runs,
        authenticator=authenticator,
        limiter=limiter,
        validator=validator,
        settings=McpSettings(
            json_response=settings.mcp_json_response,
            allowed_origins=settings.mcp_allowed_origins,
            max_body_bytes=settings.max_body_bytes,
            resource_metadata_url=metadata_url,
        ),
        version=version,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with gateway.run():
            yield

    app = FastAPI(
        title="RaPA",
        version=version,
        lifespan=lifespan,
        docs_url="/v1/docs" if settings.api_docs else None,
        redoc_url=None,
        openapi_url="/v1/openapi.json" if settings.api_docs else None,
    )
    install_error_handlers(app, resource_metadata_url=metadata_url)
    services = ApiServices(
        catalog=catalog,
        runs=runs,
        admin=admin,
        authenticator=authenticator,
        limiter=limiter,
        validator=validator,
        public_url=public_url,
        sessions=sessions,
        live_origins=live_origins(public_url, settings.live_allowed_origins),
        publishing=publishing,
        repairs=repairs,
        credentials=credentials,
        company=company,
    )
    app.include_router(create_api_router(services))
    app.router.routes.append(Route("/mcp", endpoint=gateway, include_in_schema=False))

    @app.get("/saude", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"estado": "ok", "versao": version})

    if publish_metadata:
        metadata: dict[str, Any] = {
            "resource": f"{public_url}/mcp",
            "authorization_servers": list(settings.authorization_servers),
            "bearer_methods_supported": ["header"],
            "resource_name": "RaPA",
        }

        async def protected_resource() -> JSONResponse:
            return JSONResponse(metadata)

        app.add_api_route(_METADATA_PATH, protected_resource, include_in_schema=False)
        app.add_api_route(f"{_METADATA_PATH}/mcp", protected_resource, include_in_schema=False)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
            expose_headers=["Location", "Retry-After"],
        )
    # A planilha de um lote pode passar do limite comum (com folga para o multipart),
    # pelo REST e pelo envio de arquivo da interface (a tela Lotes).
    batch_limit = max(settings.max_body_bytes, publishing.batch_max_bytes + _MULTIPART_SLACK)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        path_limits={"/v1/lotes": batch_limit, "/v1/lotes/previa": batch_limit},
        path_patterns=[(UI_UPLOAD_PATH, batch_limit)],
    )
    app.add_middleware(CatchAllMiddleware)
    return app
