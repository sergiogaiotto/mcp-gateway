"""Mundo de teste da camada HTTP: duas empresas, robôs publicados e tokens fixos."""

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx2
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from rapa.control.auth import BearerVerifier, StaticTokenVerifier
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.core.contracts import Principal, RobotSummary, Role, RunStatus, RunTrigger
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.memory import MemoryPlatform, make_principal
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"
INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}

TOKEN_A = "token-empresa-a-operadora"
TOKEN_A_CREATOR = "token-empresa-a-criadora"
TOKEN_A_VIEWER = "token-empresa-a-leitora"
TOKEN_B = "token-empresa-b-operador"

MCP_URL = "http://testserver/mcp"


def simple_robot(name: str, effect: str, *, title: str, description: str) -> Manifest:
    """Robô pequeno que passa pelo portão de política, com o efeito pedido."""
    return Manifest.model_validate(
        {
            "name": name,
            "title": title,
            "description": description,
            "allowed_domains": [HOST],
            "inputs": [{"name": "codigo", "title": "Código", "type": "string"}],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir",
                    "title": "Abrir o portal",
                    "url": f"{BASE}/inicio",
                },
                {
                    "kind": "fill",
                    "id": "digitar_codigo",
                    "title": "Digitar o código",
                    "target": {
                        "intent": "campo Código",
                        "locators": [{"by": "label", "value": "Código"}],
                    },
                    "value": {"source": "text", "template": "{{ inputs.codigo }}"},
                },
                {
                    "kind": "click",
                    "id": "confirmar",
                    "title": "Confirmar",
                    "effect": effect,
                    "target": {
                        "intent": "botão Confirmar",
                        "locators": [{"by": "role", "value": "button", "name": "Confirmar"}],
                    },
                },
            ],
        }
    )


class DictResolver:
    """PrincipalResolver de teste: (sujeito, empresa) → principal."""

    def __init__(self, principals: Mapping[tuple[str, UUID], Principal] | None = None) -> None:
        self.principals = dict(principals or {})
        self.calls: list[tuple[str, UUID]] = []

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        self.calls.append((subject, company_id))
        return self.principals.get((subject, company_id))


@dataclass
class World:
    platform: MemoryPlatform
    company_a: UUID
    area_a: UUID
    company_b: UUID
    area_b: UUID
    operator_a: Principal
    creator_a: Principal
    viewer_a: Principal
    operator_b: Principal
    robot_ids: dict[tuple[str, str], UUID] = field(default_factory=dict)

    @property
    def tokens(self) -> dict[str, Principal]:
        return {
            TOKEN_A: self.operator_a,
            TOKEN_A_CREATOR: self.creator_a,
            TOKEN_A_VIEWER: self.viewer_a,
            TOKEN_B: self.operator_b,
        }


async def _publish(
    platform: MemoryPlatform,
    creator: Principal,
    approver: Principal,
    area: UUID,
    manifest: Manifest,
) -> UUID:
    version = await platform.create_version(creator, area, manifest)
    await platform.mark_validated(creator, version.robot_id, version.version_no)
    await platform.publish(approver, version.robot_id, version.version_no)
    return version.robot_id


async def build_world() -> World:
    """Empresa A com 4 robôs (um com nome reservado) e empresa B com 2."""
    platform = MemoryPlatform()
    company_a, area_a, company_b, area_b = new_id(), new_id(), new_id(), new_id()
    creator_a = make_principal(company_a, Role.CREATOR, subject="criadora@a")
    operator_a = make_principal(company_a, Role.APPROVER, Role.OPERATOR, subject="operadora@a")
    viewer_a = make_principal(company_a, Role.VIEWER, subject="leitora@a")
    creator_b = make_principal(company_b, Role.CREATOR, subject="criador@b")
    operator_b = make_principal(company_b, Role.APPROVER, Role.OPERATOR, subject="operador@b")
    world = World(
        platform=platform,
        company_a=company_a,
        area_a=area_a,
        company_b=company_b,
        area_b=area_b,
        operator_a=operator_a,
        creator_a=creator_a,
        viewer_a=viewer_a,
        operator_b=operator_b,
    )
    robots_a = [
        emitir_protocolo(BASE, HOST),
        baixar_notas(BASE, HOST),
        simple_robot(
            "atualizar_cadastro",
            "write",
            title="Atualizar cadastro",
            description="Atualiza o cadastro de um cliente no portal.",
        ),
        simple_robot(
            "buscar_robo",
            "read",
            title="Robô com nome reservado",
            description="Não pode aparecer no MCP.",
        ),
    ]
    for manifest in robots_a:
        robot_id = await _publish(platform, creator_a, operator_a, area_a, manifest)
        world.robot_ids["a", manifest.name] = robot_id
    robots_b = [
        emitir_protocolo(BASE, HOST).model_copy(update={"title": "Emitir protocolo (B)"}),
        simple_robot(
            "consultar_saldo",
            "read",
            title="Consultar saldo",
            description="Consulta o saldo de uma conta.",
        ),
    ]
    for manifest in robots_b:
        robot_id = await _publish(platform, creator_b, operator_b, area_b, manifest)
        world.robot_ids["b", manifest.name] = robot_id
    return world


def make_app(
    world: World,
    *,
    verifier: BearerVerifier | None = None,
    resolver: DictResolver | None = None,
    catalog: Any = None,
    runs: Any = None,
    **settings: Any,
) -> FastAPI:
    options: dict[str, Any] = {"allow_static_tokens": True, "rate_limit_per_s": None}
    options.update(settings)
    return create_http_app(
        catalog=catalog or world.platform,
        runs=runs or world.platform,
        admin=world.platform,
        verifier=verifier or StaticTokenVerifier(world.tokens),
        resolver=resolver or DictResolver(),
        settings=HttpSettings(**options),
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class SpyRuns:
    """RunService que conta as chamadas de start antes de delegar."""

    def __init__(self, inner: MemoryPlatform) -> None:
        self.inner = inner
        self.started: list[tuple[str, dict[str, Any], RunTrigger, str | None]] = []

    async def start(
        self,
        principal: Principal,
        slug: str,
        inputs: dict[str, Any],
        trigger: RunTrigger,
        idempotency_key: str | None = None,
    ) -> RunStatus:
        self.started.append((slug, dict(inputs), trigger, idempotency_key))
        return await self.inner.start(principal, slug, inputs, trigger, idempotency_key)

    async def get(self, principal: Principal, run_id: UUID) -> RunStatus | None:
        return await self.inner.get(principal, run_id)


class BrokenCatalog:
    """Catálogo que falha com uma mensagem interna que não pode vazar."""

    SECRET = "detalhe-interno-que-nao-pode-vazar"

    async def list_published(self, principal: Principal) -> list[RobotSummary]:
        raise RuntimeError(self.SECRET)

    async def get_published(self, principal: Principal, slug: str) -> RobotSummary | None:
        raise RuntimeError(self.SECRET)

    async def search(self, principal: Principal, query: str, limit: int = 10) -> list[RobotSummary]:
        raise RuntimeError(self.SECRET)


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """Executa o ``lifespan`` da aplicação (o gerenciador do SDK MCP)."""
    async with app.router.lifespan_context(app):
        yield


def asgi_client(app: FastAPI, token: str | None = None) -> httpx2.AsyncClient:
    headers = auth(token) if token else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
        trust_env=False,
    )


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str, *, mode: str = "legacy") -> AsyncIterator[Client]:
    """Cliente MCP oficial falando com a aplicação em processo (já em execução)."""
    async with (
        asgi_client(app, token) as http,
        Client(streamable_http_client(MCP_URL, http_client=http), mode=mode) as client,
    ):
        yield client


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Sobe a aplicação com uvicorn numa porta livre (com lifespan); devolve a URL base."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="rapa-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("a aplicação HTTP não subiu em 10 s")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
