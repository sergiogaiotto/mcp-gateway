"""Gateway MCP do RaPA sobre o SDK oficial (``mcp``), em Streamable HTTP sem estado.

Um endpoint só para todas as empresas. Cada requisição HTTP é autenticada
antes de chegar ao SDK; o principal vai no escopo ASGI e os tratadores o leem
de ``ctx.request``. A lista de ferramentas é calculada a cada requisição para
quem chama, então cada empresa só vê os próprios robôs.

O servidor de baixo nível do SDK não valida argumentos: toda chamada passa por
:mod:`rapa.control.validation` antes de iniciar uma execução. Erros voltam como
resultados com ``isError`` e texto em pt-BR, nunca com rastro de pilha.
"""

import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_REQUEST
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

from rapa.control.api.errors import (
    auth_error_response,
    contract_error_response,
    error_response,
    log_unexpected,
)
from rapa.control.api.ratelimit import RateLimiter
from rapa.control.api.routes import check_idempotency_key, robot_not_found
from rapa.control.auth import Authenticator, AuthError
from rapa.control.mcp.tools import (
    BUSCAR_ROBO,
    CONSULTAR_EXECUCAO,
    EXECUTAR_ROBO,
    IDEMPOTENCY_META_KEY,
    META_INPUT_SCHEMAS,
    META_TOOLS,
    RESERVED_NAMES,
    describe_run,
    robot_tool,
    search_item,
)
from rapa.control.validation import InputValidator, InvalidSchemaError
from rapa.core.contracts import (
    ContractError,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RobotCatalog,
    RunService,
    RunStatus,
    RunTrigger,
)

__all__ = ["PRINCIPAL_SCOPE_KEY", "McpGateway", "McpHandlers", "McpSettings"]

logger = logging.getLogger(__name__)

#: Chave do escopo ASGI onde o gateway guarda o principal autenticado.
PRINCIPAL_SCOPE_KEY = "rapa.principal"
_MAX_REPORTED = 10_000

_INSTRUCTIONS = (
    "Robôs RPA publicados para a sua empresa. Cada robô é uma ferramenta. A execução é "
    "assíncrona: a ferramenta devolve o identificador e o estado da execução, e "
    f"{CONSULTAR_EXECUCAO} mostra o andamento e as saídas. Use {BUSCAR_ROBO} para achar um "
    f"robô pelo que ele faz e {EXECUTAR_ROBO} para rodar um robô pelo nome."
)


class McpSettings(BaseModel):
    """Configuração do gateway MCP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Respostas em JSON (``True``) ou em SSE (``False``).
    json_response: bool = True
    #: Origens aceitas no cabeçalho Origin. Vazio: recusa toda requisição com Origin
    #: (clientes MCP fora do navegador não mandam Origin).
    allowed_origins: frozenset[str] = frozenset()
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024)
    #: URL do documento de metadados do recurso protegido (RFC 9728), citada no 401.
    resource_metadata_url: str | None = None


class _ToolFailure(Exception):
    """Falha de uma chamada de ferramenta, devolvida como resultado com ``isError``."""

    def __init__(self, code: str, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


def _error_result(
    code: str, message: str, details: list[str] | None = None
) -> types.CallToolResult:
    lines = [f"Erro ({code}): {message}"]
    lines += [f"- {detail}" for detail in (details or [])]
    return types.CallToolResult(content=[types.TextContent(text="\n".join(lines))], is_error=True)


def _run_result(status: RunStatus, *, started: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=describe_run(status, started=started))],
        structured_content=status.model_dump(mode="json"),
    )


def principal_from(request: Any) -> Principal:
    """Principal que o gateway pôs no escopo ASGI da requisição HTTP."""
    scope = getattr(request, "scope", None)
    principal = scope.get(PRINCIPAL_SCOPE_KEY) if isinstance(scope, Mapping) else None
    if not isinstance(principal, Principal):
        raise _ToolFailure("nao_autenticado", "requisição sem autenticação")
    return principal


class McpHandlers:
    """Tratadores de ``tools/list`` e ``tools/call``."""

    def __init__(
        self, catalog: RobotCatalog, runs: RunService, validator: InputValidator | None = None
    ) -> None:
        self.catalog = catalog
        self.runs = runs
        self.validator = validator or InputValidator()
        self._reported: set[tuple[UUID, str]] = set()

    def _report_reserved(self, company_id: UUID, slug: str) -> None:
        """Registra uma vez por empresa e nome que um robô ficou fora do MCP."""
        if (company_id, slug) in self._reported:
            return
        if len(self._reported) >= _MAX_REPORTED:
            self._reported.clear()
        self._reported.add((company_id, slug))
        logger.warning(
            "robô com nome reservado fica fora do MCP: empresa=%s robô=%s", company_id, slug
        )

    # -- tools/list -----------------------------------------------------------

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """Ferramentas fixas mais os robôs publicados visíveis para quem chama."""
        try:
            principal = principal_from(ctx.request)
            robots = await self.catalog.list_published(principal)
        except _ToolFailure as exc:
            raise MCPError(code=INVALID_REQUEST, message=exc.message) from None
        except ContractError as exc:
            raise MCPError(code=INVALID_REQUEST, message=exc.message) from None
        except Exception as exc:
            log_unexpected(logger, "erro inesperado ao listar ferramentas", exc)
            raise MCPError(
                code=INTERNAL_ERROR, message="erro interno; tente de novo mais tarde"
            ) from None
        tools = list(META_TOOLS)
        for robot in robots:
            if robot.slug in RESERVED_NAMES:
                self._report_reserved(principal.company_id, robot.slug)
                continue
            tools.append(robot_tool(robot))
        # A lista depende de quem chama: nunca pode ir para um cache compartilhado.
        return types.ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    # -- tools/call -----------------------------------------------------------

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        meta_tools = {
            BUSCAR_ROBO: self._search,
            EXECUTAR_ROBO: self._execute,
            CONSULTAR_EXECUCAO: self._status,
        }
        try:
            principal = principal_from(ctx.request)
            arguments = params.arguments or {}
            meta_tool = meta_tools.get(params.name)
            if meta_tool is not None:
                return await meta_tool(principal, arguments)
            key = (params.meta or {}).get(IDEMPOTENCY_META_KEY)
            if key is not None and not isinstance(key, str):
                raise InvalidInput("chave de idempotência inválida", ["use um texto"])
            return await self._start(principal, params.name, arguments, key)
        except _ToolFailure as exc:
            return _error_result(exc.code, exc.message, exc.details)
        except ContractError as exc:
            details = exc.errors if isinstance(exc, InvalidInput) else []
            if isinstance(exc, QuotaExceeded):
                details = [f"tente de novo em {exc.retry_after_s} s"]
            return _error_result(exc.code, exc.message, details)
        except Exception as exc:
            log_unexpected(logger, "erro inesperado no gateway MCP", exc)
            return _error_result("erro_interno", "erro interno; tente de novo mais tarde")

    def _check(self, schema: Mapping[str, Any], arguments: Any, message: str) -> None:
        try:
            errors = self.validator.errors(schema, arguments)
        except InvalidSchemaError:
            logger.error("esquema de entradas inválido numa ferramenta MCP")
            raise _ToolFailure(
                "esquema_invalido",
                "o esquema de entradas do robô é inválido; avise quem mantém o robô",
            ) from None
        if errors:
            raise InvalidInput(message, errors)

    async def _start(
        self,
        principal: Principal,
        slug: str,
        inputs: dict[str, Any],
        idempotency_key: str | None,
    ) -> types.CallToolResult:
        if slug in RESERVED_NAMES:  # pragma: no cover - tratados antes
            raise robot_not_found(slug)
        robot = await self.catalog.get_published(principal, slug)
        if robot is None:
            raise robot_not_found(slug)
        self._check(robot.input_schema, inputs, "entradas inválidas")
        key = check_idempotency_key(idempotency_key)
        status = await self.runs.start(principal, robot.slug, inputs, RunTrigger.MCP, key)
        return _run_result(status, started=True)

    async def _search(
        self, principal: Principal, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        self._check(META_INPUT_SCHEMAS[BUSCAR_ROBO], arguments, "argumentos inválidos")
        query = str(arguments["consulta"])
        limit = int(arguments.get("limite", 10))
        robots = [
            robot
            for robot in await self.catalog.search(principal, query, limit)
            if robot.slug not in RESERVED_NAMES
        ]
        if robots:
            lines = [f"{len(robots)} robô(s) encontrado(s):"]
            lines += [f"- {robot.slug}: {robot.title}" for robot in robots]
        else:
            lines = ["Nenhum robô encontrado para essa consulta."]
        return types.CallToolResult(
            content=[types.TextContent(text="\n".join(lines))],
            structured_content={"robos": [search_item(robot) for robot in robots]},
        )

    async def _execute(
        self, principal: Principal, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        self._check(META_INPUT_SCHEMAS[EXECUTAR_ROBO], arguments, "argumentos inválidos")
        slug = str(arguments["robo"])
        if slug in RESERVED_NAMES:
            raise robot_not_found(slug)
        inputs = arguments.get("entradas", {})
        return await self._start(principal, slug, inputs, arguments.get("chave_idempotencia"))

    async def _status(
        self, principal: Principal, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        self._check(META_INPUT_SCHEMAS[CONSULTAR_EXECUCAO], arguments, "argumentos inválidos")
        try:
            run_id = UUID(str(arguments["execucao_id"]))
        except ValueError:
            raise InvalidInput(
                "argumentos inválidos", ["execucao_id: identificador UUID inválido"]
            ) from None
        status = await self.runs.get(principal, run_id)
        if status is None:
            raise NotFound("execução não encontrada")
        return _run_result(status, started=False)


def _no_param_headers(name: str) -> None:
    return None


class McpGateway:
    """Aplicação ASGI do endpoint ``/mcp``: autenticação, limite e o SDK oficial."""

    def __init__(
        self,
        *,
        catalog: RobotCatalog,
        runs: RunService,
        authenticator: Authenticator,
        limiter: RateLimiter | None = None,
        validator: InputValidator | None = None,
        settings: McpSettings | None = None,
        version: str = "",
    ) -> None:
        self.settings = settings or McpSettings()
        self.authenticator = authenticator
        self.limiter = limiter
        self.handlers = McpHandlers(catalog, runs, validator)
        self.server: Server[Any] = Server(
            "rapa",
            version=version,
            title="RaPA",
            instructions=_INSTRUCTIONS,
            # Nenhum esquema de robô declara cabeçalhos Mcp-Param; sem isto, o SDK
            # listaria as ferramentas de novo a cada chamada só para conferi-los.
            get_tool_input_schema=_no_param_headers,
            on_list_tools=self.handlers.list_tools,
            on_call_tool=self.handlers.call_tool,
        )
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server,
            stateless=True,
            json_response=self.settings.json_response,
            max_request_body_size=self.settings.max_body_bytes,
        )

    def run(self) -> AbstractAsyncContextManager[None]:
        """Contexto de vida do gerenciador do SDK; entre nele no ``lifespan`` da aplicação."""
        return self.session_manager.run()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - só HTTP chega aqui
            return
        if scope.get("method") != "POST":
            # Sem estado não há mensagens do servidor para um fluxo SSE (GET) nem
            # sessão para encerrar (DELETE); um GET aberto só prenderia a conexão.
            response = error_response(
                405,
                "metodo_nao_permitido",
                "o endpoint MCP só aceita POST",
                headers={"Allow": "POST"},
            )
            await response(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None and origin not in self.settings.allowed_origins:
            response = error_response(403, "origem_nao_permitida", "origem não permitida")
            await response(scope, receive, send)
            return
        try:
            principal = await self.authenticator.authenticate(headers.get("authorization"))
            if self.limiter is not None:
                self.limiter.check(principal.company_id)
        except AuthError as exc:
            await auth_error_response(exc, self.settings.resource_metadata_url)(
                scope, receive, send
            )
            return
        except QuotaExceeded as exc:
            await contract_error_response(exc)(scope, receive, send)
            return
        scoped = dict(scope)
        scoped[PRINCIPAL_SCOPE_KEY] = principal
        await self.session_manager.handle_request(scoped, receive, send)
