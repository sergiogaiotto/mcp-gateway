"""Rotas REST da API (``/v1``).

Os serviços chegam por injeção (:class:`ApiServices`) e só são usados pelos
protocolos de :mod:`rapa.core.contracts`. A empresa vem sempre do principal
autenticado; nada no corpo, na consulta ou no caminho escolhe a empresa.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from rapa.control.api.artifacts import add_artifact_routes
from rapa.control.api.company import add_company_routes
from rapa.control.api.credentials import add_credential_routes
from rapa.control.api.errors import error_response
from rapa.control.api.openapi_robots import build_robots_openapi
from rapa.control.api.publishing import PublishingServices, add_publishing_routes
from rapa.control.api.ratelimit import RateLimiter
from rapa.control.api.repairs import add_repair_routes
from rapa.control.api.sessions import add_session_routes
from rapa.control.auth import Authenticator
from rapa.control.sessions.service import RecordingSessions
from rapa.control.validation import InputValidator, InvalidSchemaError
from rapa.core.contracts import (
    MAX_RUNS_LISTED,
    ArtifactService,
    CompanyAdminService,
    CredentialService,
    InvalidInput,
    NotFound,
    Principal,
    RepairService,
    RobotAdmin,
    RobotCatalog,
    RobotInfo,
    RobotSummary,
    RobotVersionInfo,
    RunService,
    RunStatus,
    RunTrigger,
    VisibleNames,
    WhoAmI,
)
from rapa.core.robot import SLUG_PATTERN, Manifest

__all__ = [
    "BEARER_SCHEME",
    "MAX_IDEMPOTENCY_KEY",
    "ApiServices",
    "CreateRobotBody",
    "StartRunBody",
    "check_idempotency_key",
    "create_api_router",
    "robot_not_found",
]

logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_KEY = 200
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]+$")
_SLUG = re.compile(SLUG_PATTERN)
#: O token das rotas de ``/v1`` no OpenAPI (o mesmo ``bearer`` do OpenAPI dos robôs): é
#: por ele que ``/v1/docs`` mostra o botão "Authorize" e manda o token no "Try it out".
#: Só documenta; quem confere o cabeçalho continua sendo o ``Authenticator``, com as
#: mesmas respostas (``auto_error=False``: este esquema nunca recusa nada).
BEARER_SCHEME = HTTPBearer(
    scheme_name="bearer",
    description="O token de acesso do RaPA, sem a palavra Bearer (a página a acrescenta).",
    auto_error=False,
)


class StartRunBody(BaseModel):
    """Corpo de ``POST /v1/catalogo/{slug}/execucoes``."""

    model_config = ConfigDict(extra="forbid")

    entradas: dict[str, Any] = Field(default_factory=dict)


class CreateRobotBody(BaseModel):
    """Corpo de ``POST /v1/robos``."""

    model_config = ConfigDict(extra="forbid")

    area_id: UUID
    manifesto: Manifest


@dataclass(frozen=True)
class ApiServices:
    """Dependências da API REST."""

    catalog: RobotCatalog
    runs: RunService
    admin: RobotAdmin
    authenticator: Authenticator
    limiter: RateLimiter | None = None
    validator: InputValidator = field(default_factory=InputValidator)
    #: URL pública da API, citada no documento OpenAPI dos robôs.
    public_url: str | None = None
    #: Sessões de gravação (Demonstrar e Generalizar); sem elas, ``/v1/sessoes`` responde 503.
    sessions: RecordingSessions | None = None
    #: Origens da interface aceitas no websocket da visão ao vivo (vazio: não confere).
    live_origins: frozenset[str] = frozenset()
    #: Agendas, webhooks e lotes; o que faltar responde 503.
    publishing: PublishingServices = field(default_factory=PublishingServices)
    #: O conserto assistido; sem ele, ``/v1/execucoes/{id}/conserto`` responde 503.
    repairs: RepairService | None = None
    #: Credenciais e as regras do certificado A1; sem elas, ``/v1/credenciais`` responde 503.
    credentials: CredentialService | None = None
    #: A administração da empresa por ela mesma; sem ela, ``/v1/empresa`` responde 503.
    company: CompanyAdminService | None = None
    #: As evidências (artefatos) das execuções; sem elas, ``/v1/execucoes/{id}/artefatos``
    #: responde 503.
    artifacts: ArtifactService | None = None


def check_idempotency_key(key: str | None) -> str | None:
    """Confere a chave de idempotência (até 200 caracteres ASCII visíveis)."""
    if key is None:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY:
        raise InvalidInput(
            "chave de idempotência inválida",
            [f"a chave pode ter no máximo {MAX_IDEMPOTENCY_KEY} caracteres"],
        )
    if not _IDEMPOTENCY_KEY.fullmatch(key):
        raise InvalidInput(
            "chave de idempotência inválida",
            ["use só letras, números e símbolos ASCII visíveis, sem espaços"],
        )
    return key


def robot_not_found(slug: str) -> NotFound:
    """``NotFound`` do robô; só repete o nome quando ele tem o formato de um nome de robô."""
    if _SLUG.fullmatch(slug):
        return NotFound(f"robô '{slug}' não encontrado")
    return NotFound("robô não encontrado")


def create_api_router(services: ApiServices) -> APIRouter:
    """Monta as rotas de ``/v1`` sobre os serviços informados."""
    router = APIRouter(prefix="/v1")

    async def current_principal(
        request: Request,
        _bearer: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER_SCHEME)],
    ) -> Principal:
        principal = await services.authenticator.authenticate(request.headers.get("authorization"))
        if services.limiter is not None:
            services.limiter.check(principal.company_id)
        return principal

    caller_dep = Depends(current_principal)

    async def published(principal: Principal, slug: str) -> RobotSummary:
        if not _SLUG.fullmatch(slug):
            raise robot_not_found(slug)
        summary = await services.catalog.get_published(principal, slug)
        if summary is None:
            raise robot_not_found(slug)
        return summary

    # -- catálogo -----------------------------------------------------------

    @router.get("/catalogo", response_model=list[RobotSummary])
    async def list_catalog(principal: Annotated[Principal, caller_dep]) -> list[RobotSummary]:
        return await services.catalog.list_published(principal)

    @router.get("/catalogo/busca", response_model=list[RobotSummary])
    async def search_catalog(
        principal: Annotated[Principal, caller_dep],
        q: Annotated[str, Query(max_length=200)] = "",
        limite: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> list[RobotSummary]:
        return await services.catalog.search(principal, q, limite)

    @router.get("/catalogo/{slug}", response_model=RobotSummary)
    async def get_catalog_robot(
        slug: str, principal: Annotated[Principal, caller_dep]
    ) -> RobotSummary:
        return await published(principal, slug)

    @router.post(
        "/catalogo/{slug}/execucoes",
        status_code=202,
        response_model=RunStatus,
        responses={202: {"headers": {"Location": {"schema": {"type": "string"}}}}},
    )
    async def start_run(
        slug: str,
        body: StartRunBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> RunStatus | JSONResponse:
        key = check_idempotency_key(idempotency_key)
        summary = await published(principal, slug)
        try:
            errors = services.validator.errors(summary.input_schema, body.entradas)
        except InvalidSchemaError:
            logger.error("esquema de entradas inválido no robô publicado %s", summary.slug)
            return error_response(
                500,
                "esquema_invalido",
                "o esquema de entradas do robô é inválido; avise quem mantém o robô",
            )
        if errors:
            raise InvalidInput("entradas inválidas", errors)
        status = await services.runs.start(
            principal, summary.slug, body.entradas, RunTrigger.REST, key
        )
        response.headers["Location"] = f"/v1/execucoes/{status.run_id}"
        return status

    # -- quem está chamando ----------------------------------------------------

    @router.get("/eu", response_model=WhoAmI)
    async def whoami(principal: Annotated[Principal, caller_dep]) -> WhoAmI:
        """O principal autenticado, com os papéis na empresa e em cada área.

        Também os nomes que ele pode ver: o da empresa (``company_name``) e os das áreas
        em que tem algum papel, ou de todas com um papel na empresa (``area_names``).
        Sem a administração da empresa no servidor, os nomes vêm vazios.
        """
        names = (
            await services.company.visible_names(principal)
            if services.company is not None
            else VisibleNames()
        )
        return WhoAmI.model_validate(
            {**principal.model_dump(), **names.model_dump()},
        )

    # -- execuções ----------------------------------------------------------

    @router.get("/execucoes", response_model=list[RunStatus])
    async def list_runs(
        principal: Annotated[Principal, caller_dep],
        limite: Annotated[int, Query(ge=1, le=MAX_RUNS_LISTED)] = 50,
    ) -> list[RunStatus]:
        return await services.runs.list_recent(principal, limite)

    @router.get("/execucoes/{run_id}", response_model=RunStatus)
    async def get_run(run_id: UUID, principal: Annotated[Principal, caller_dep]) -> RunStatus:
        status = await services.runs.get(principal, run_id)
        if status is None:
            raise NotFound("execução não encontrada")
        return status

    # -- evidências ----------------------------------------------------------------

    add_artifact_routes(router, services.artifacts, current_principal)

    # -- consertar ----------------------------------------------------------------

    add_repair_routes(router, services.repairs, current_principal)

    # -- administração de robôs ---------------------------------------------

    @router.get("/robos", response_model=list[RobotInfo])
    async def list_robots(principal: Annotated[Principal, caller_dep]) -> list[RobotInfo]:
        return await services.admin.list_robots(principal)

    @router.post("/robos", status_code=201, response_model=RobotVersionInfo)
    async def create_robot_version(
        body: CreateRobotBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> RobotVersionInfo:
        info = await services.admin.create_version(principal, body.area_id, body.manifesto)
        response.headers["Location"] = f"/v1/robos/{info.robot_id}/versoes/{info.version_no}"
        return info

    @router.get("/robos/{robot_id}/versoes", response_model=list[RobotVersionInfo])
    async def list_robot_versions(
        robot_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> list[RobotVersionInfo]:
        return await services.admin.list_versions(principal, robot_id)

    @router.get("/robos/{robot_id}/versoes/{n}", response_model=RobotVersionInfo)
    async def get_robot_version(
        robot_id: UUID,
        n: Annotated[int, Path(ge=1)],
        principal: Annotated[Principal, caller_dep],
    ) -> RobotVersionInfo:
        info = await services.admin.get_version(principal, robot_id, n)
        if info is None:
            raise NotFound("versão não encontrada")
        return info

    @router.post("/robos/{robot_id}/versoes/{n}/validar", response_model=RobotVersionInfo)
    async def validate_robot_version(
        robot_id: UUID, n: Annotated[int, Path(ge=1)], principal: Annotated[Principal, caller_dep]
    ) -> RobotVersionInfo:
        return await services.admin.mark_validated(principal, robot_id, n)

    @router.post("/robos/{robot_id}/versoes/{n}/publicar", response_model=RobotVersionInfo)
    async def publish_robot_version(
        robot_id: UUID, n: Annotated[int, Path(ge=1)], principal: Annotated[Principal, caller_dep]
    ) -> RobotVersionInfo:
        """Registra a aprovação de quem chama; a última aprovação exigida publica.

        Um robô com certificado A1 precisa de duas aprovações: depois da primeira, a
        versão continua validada (``approvals`` e ``approvals_required`` dizem quantas
        faltam).
        """
        return await services.admin.publish(principal, robot_id, n)

    # -- sessões de gravação ------------------------------------------------

    add_session_routes(
        router, services.sessions, current_principal, allowed_origins=services.live_origins
    )

    # -- publicar: agendas, webhooks e lotes ----------------------------------------

    add_publishing_routes(router, services.publishing, current_principal, check_idempotency_key)

    # -- credenciais e o certificado A1 ----------------------------------------------

    add_credential_routes(router, services.credentials, current_principal)
    # -- a empresa: usuários e papéis, áreas e consumo ------------------------------

    add_company_routes(router, services.company, current_principal)

    # -- OpenAPI dos robôs --------------------------------------------------

    @router.get("/openapi-robos.json")
    async def robots_openapi(principal: Annotated[Principal, caller_dep]) -> JSONResponse:
        robots = await services.catalog.list_published(principal)
        return JSONResponse(build_robots_openapi(robots, server_url=services.public_url))

    return router
