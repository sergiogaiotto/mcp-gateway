"""Backends da interface: sobre os contratos e sobre a API REST (com MockTransport)."""

import json
import re
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest

from rapa.core.contracts import (
    Conflict,
    ContractError,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    Role,
    RunState,
    RunTrigger,
    VersionStatus,
)
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform, make_principal
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.ui.auth import TokenSet, UiSession
from rapa.ui.backend import (
    Backend,
    BackendError,
    BackendUnavailable,
    ContractsBackend,
    ContractsBackendFactory,
    NotAuthenticated,
    RestBackend,
    RestBackendFactory,
    coerce_manifest,
    describe_error,
)

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"
API = "http://api.rapa.test"
TOKEN = "token-secreto-de-teste-123"
INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}

type Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]


def _error(
    status: int, code: str, message: str, details: list[str] | None = None
) -> httpx2.Response:
    body = {"erro": code, "mensagem": message, "detalhes": details or []}
    return httpx2.Response(status, json=body)


def _status(exc: ContractError) -> int:
    return {NotFound: 404, Forbidden: 403, Conflict: 409, InvalidInput: 422}.get(type(exc), 400)


class FakeApi:
    """API REST ``/v1`` mínima sobre a plataforma em memória, para testar o RestBackend."""

    def __init__(self, platform: MemoryPlatform, tokens: dict[str, Principal]) -> None:
        self.platform = platform
        self.tokens = tokens
        self.requests: list[httpx2.Request] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        principal = self.tokens.get(auth.removeprefix("Bearer "))
        if principal is None:
            return _error(401, "nao_autenticado", "é preciso se autenticar")
        try:
            return await self._route(request, principal)
        except InvalidInput as exc:
            return _error(422, exc.code, exc.message, exc.errors)
        except ContractError as exc:
            return _error(_status(exc), exc.code, exc.message)

    async def _route(self, request: httpx2.Request, principal: Principal) -> httpx2.Response:
        p = self.platform
        path, method = request.url.path, request.method
        body: Any = json.loads(request.content) if request.content else None

        def ok(data: Any, status: int = 200) -> httpx2.Response:
            if isinstance(data, list):
                return httpx2.Response(status, json=[d.model_dump(mode="json") for d in data])
            return httpx2.Response(status, json=data.model_dump(mode="json"))

        if path == "/v1/catalogo":
            return ok(await p.list_published(principal))
        if path == "/v1/catalogo/busca":
            q, limit = request.url.params["q"], int(request.url.params["limite"])
            return ok(await p.search(principal, q, limit))
        if m := re.fullmatch(r"/v1/catalogo/([a-z0-9_]+)/execucoes", path):
            status = await p.start(
                principal,
                m.group(1),
                body["entradas"],
                RunTrigger.REST,
                request.headers.get("idempotency-key"),
            )
            return ok(status, 202)
        if m := re.fullmatch(r"/v1/catalogo/([a-z0-9_]+)", path):
            summary = await p.get_published(principal, m.group(1))
            if summary is None:
                raise NotFound(f"robô '{m.group(1)}' não encontrado")
            return ok(summary)
        if path == "/v1/execucoes":
            return ok(await p.list_recent(principal, int(request.url.params["limite"])))
        if m := re.fullmatch(r"/v1/robos/([0-9a-f-]+)/versoes", path):
            return ok(await p.list_versions(principal, UUID(m.group(1))))
        if m := re.fullmatch(r"/v1/execucoes/([0-9a-f-]+)", path):
            run = await p.get(principal, UUID(m.group(1)))
            if run is None:
                raise NotFound("execução não encontrada")
            return ok(run)
        if path == "/v1/robos" and method == "GET":
            return ok(await p.list_robots(principal))
        if path == "/v1/robos" and method == "POST":
            manifest = coerce_manifest(body["manifesto"])
            return ok(await p.create_version(principal, UUID(body["area_id"]), manifest), 201)
        if m := re.fullmatch(r"/v1/robos/([0-9a-f-]+)/versoes/([0-9]+)(/validar|/publicar)?", path):
            robot_id, number, action = UUID(m.group(1)), int(m.group(2)), m.group(3)
            if action == "/validar":
                return ok(await p.mark_validated(principal, robot_id, number))
            if action == "/publicar":
                return ok(await p.publish(principal, robot_id, number))
            info = await p.get_version(principal, robot_id, number)
            if info is None:
                raise NotFound("versão não encontrada")
            return ok(info)
        return _error(404, "nao_encontrado", "recurso não encontrado")


class Setup:
    def __init__(self) -> None:
        self.platform = MemoryPlatform()
        self.company, self.area = new_id(), new_id()
        self.creator = make_principal(self.company, Role.CREATOR, subject="criadora")
        self.approver = make_principal(
            self.company, Role.APPROVER, Role.OPERATOR, subject="aprovador"
        )
        self.outsider = make_principal(new_id(), Role.COMPANY_ADMIN, subject="intrusa")
        self.api = FakeApi(
            self.platform,
            {TOKEN: self.approver, "token-criadora": self.creator, "token-intrusa": self.outsider},
        )

    def client(self, handler: Handler | None = None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(base_url=API, transport=httpx2.MockTransport(handler or self.api))

    def rest(self, token: str = TOKEN, handler: Handler | None = None) -> RestBackend:
        return RestBackend(self.client(handler), token)

    def contracts(self, principal: Principal) -> ContractsBackend:
        return ContractsBackend(self.platform, self.platform, self.platform, principal)

    async def publish(self) -> UUID:
        info = await self.platform.create_version(
            self.creator, self.area, emitir_protocolo(BASE, HOST)
        )
        await self.platform.mark_validated(self.creator, info.robot_id, 1)
        await self.platform.publish(self.approver, info.robot_id, 1)
        return info.robot_id


@pytest.fixture
async def setup() -> Setup:
    return Setup()


@pytest.mark.parametrize("kind", ["rest", "contracts"])
async def test_lifecycle_through_backend(setup: Setup, kind: str) -> None:
    creator: Backend
    approver: Backend
    if kind == "rest":
        creator, approver = setup.rest("token-criadora"), setup.rest()
    else:
        creator, approver = setup.contracts(setup.creator), setup.contracts(setup.approver)
    manifest_json = emitir_protocolo(BASE, HOST).model_dump_json()
    info = await creator.create_version(setup.area, manifest_json)
    assert info.version_no == 1 and info.status is VersionStatus.DRAFT
    assert info.created_by == "criadora"
    validated = await creator.validate_version(info.robot_id, 1)
    assert validated.status is VersionStatus.VALIDATED
    with pytest.raises(Forbidden):
        await creator.publish_version(info.robot_id, 1)
    published = await approver.publish_version(info.robot_id, 1)
    assert published.status is VersionStatus.PUBLISHED
    assert published.approved_by == "aprovador"

    assert [r.slug for r in await approver.list_robots()] == ["emitir_protocolo"]
    versions = await approver.list_versions(info.robot_id)
    assert [v.version_no for v in versions] == [1]
    assert await approver.get_version(info.robot_id, 2) is None
    assert await approver.get_version(info.robot_id, 0) is None
    catalog = await approver.list_catalog()
    assert [s.slug for s in catalog] == ["emitir_protocolo"]
    assert catalog[0].input_schema["required"] == ["nome", "cpf", "uf"]
    found = await approver.search_catalog("protocolo", limit=5)
    assert [s.slug for s in found] == ["emitir_protocolo"]
    assert (await approver.get_catalog_robot("emitir_protocolo")) is not None
    assert await approver.get_catalog_robot("baixar_notas") is None
    assert await approver.get_catalog_robot("../robos") is None

    run = await approver.start_run("emitir_protocolo", INPUTS, "chave-1")
    again = await approver.start_run("emitir_protocolo", INPUTS, "chave-1")
    assert run.run_id == again.run_id and run.state is RunState.QUEUED
    with pytest.raises(Conflict):
        await approver.start_run("emitir_protocolo", {**INPUTS, "uf": "RJ"}, "chave-1")
    with pytest.raises(InvalidInput) as caught:
        await approver.start_run("emitir_protocolo", {"nome": "Maria"})
    assert "entrada obrigatória ausente: cpf" in caught.value.errors
    with pytest.raises(NotFound):
        await approver.start_run("../../v1/robos", INPUTS)
    setup.platform.set_run_state(run.run_id, RunState.SUCCEEDED, outputs={"protocolo": "P-1"})
    status = await approver.get_run(run.run_id)
    assert status is not None and status.outputs == {"protocolo": "P-1"}
    assert await approver.get_run(new_id()) is None


async def test_rest_requests_carry_token_and_contract_shapes(setup: Setup) -> None:
    await setup.publish()
    backend = setup.rest()
    await backend.search_catalog("emitir", limit=99)
    await backend.start_run("emitir_protocolo", INPUTS, "chave-x")
    search, start = setup.api.requests
    assert search.headers["authorization"] == f"Bearer {TOKEN}"
    assert search.url.path == "/v1/catalogo/busca"
    assert dict(search.url.params) == {"q": "emitir", "limite": "50"}
    assert start.method == "POST"
    assert start.url.path == "/v1/catalogo/emitir_protocolo/execucoes"
    assert start.headers["idempotency-key"] == "chave-x"
    assert json.loads(start.content) == {"entradas": INPUTS}
    assert TOKEN not in repr(backend)


async def test_rest_other_company_sees_nothing(setup: Setup) -> None:
    robot_id = await setup.publish()
    run = await setup.rest().start_run("emitir_protocolo", INPUTS)
    outsider = setup.rest("token-intrusa")
    assert await outsider.list_catalog() == []
    assert await outsider.list_robots() == []
    assert await outsider.get_version(robot_id, 1) is None
    assert await outsider.get_run(run.run_id) is None
    with pytest.raises(NotFound):
        await outsider.start_run("emitir_protocolo", INPUTS)


async def test_rest_error_mapping(setup: Setup) -> None:
    responses = {
        "/v1/robos": _error(403, "proibido", "só quem cria robôs pode criar versões"),
        "/v1/catalogo": httpx2.Response(
            429,
            json={"erro": "limite_excedido", "mensagem": "limite de uso atingido", "detalhes": []},
            headers={"Retry-After": "7"},
        ),
        "/v1/catalogo/busca": httpx2.Response(500, text="Internal Server Error"),
        "/v1/execucoes/00000000-0000-0000-0000-000000000000": httpx2.Response(
            400, json={"erro": "requisicao_invalida", "mensagem": "requisição inválida"}
        ),
    }

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return responses.get(request.url.path, httpx2.Response(200, json={"inesperado": True}))

    backend = setup.rest(handler=handler)
    with pytest.raises(Forbidden, match="só quem cria"):
        await backend.list_robots()
    with pytest.raises(QuotaExceeded) as quota:
        await backend.list_catalog()
    assert quota.value.retry_after_s == 7
    assert describe_error(quota.value)[0] == "Limite de uso atingido (tente de novo em 7 s)"
    with pytest.raises(BackendUnavailable):
        await backend.search_catalog("x")
    with pytest.raises(BackendError, match="requisição inválida"):
        await backend.get_run(UUID(int=0))
    with pytest.raises(BackendUnavailable):  # resposta fora do contrato
        await backend.get_catalog_robot("emitir_protocolo")


async def test_rest_unauthenticated_and_network_errors(setup: Setup) -> None:
    with pytest.raises(NotAuthenticated):
        await setup.rest("token-desconhecido").list_catalog()

    async def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("conexão recusada", request=request)

    with pytest.raises(BackendUnavailable) as caught:
        await setup.rest(handler=broken).list_catalog()
    assert TOKEN not in str(caught.value)

    async def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("demorou", request=request)

    with pytest.raises(BackendUnavailable):
        await setup.rest(handler=timeout).list_robots()

    async def not_json(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>")

    with pytest.raises(BackendUnavailable):
        await setup.rest(handler=not_json).list_robots()
    with pytest.raises(NotAuthenticated):
        RestBackend(setup.client(), "")


async def test_coerce_manifest_messages() -> None:
    with pytest.raises(InvalidInput) as caught:
        coerce_manifest("{nao é json")
    assert caught.value.message == "o manifesto não é um JSON válido"
    assert caught.value.errors == ["linha 1, coluna 2: JSON mal formado"]
    with pytest.raises(InvalidInput, match="objeto JSON"):
        coerce_manifest("[1, 2]")
    data = baixar_notas(BASE, HOST).model_dump(mode="json")
    data["name"] = "X"
    data["steps"][0]["url"] = ""
    with pytest.raises(InvalidInput) as caught:
        coerce_manifest(data)
    assert caught.value.message == "o manifesto é inválido"
    assert "name: formato inválido" in caught.value.errors
    assert any(e.startswith("steps[0]") and "texto curto demais" in e for e in caught.value.errors)


def _session(principal: Principal, token: str | None) -> UiSession:
    now = datetime.now(UTC)
    return UiSession(
        key="k",
        principal=principal,
        display_name="Pessoa",
        method="oidc",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen=now,
        tokens=TokenSet(token) if token else None,
    )


async def test_factories(setup: Setup) -> None:
    await setup.publish()
    contracts = ContractsBackendFactory.from_platform(setup.platform)(
        _session(setup.approver, None)
    )
    assert [s.slug for s in await contracts.list_catalog()] == ["emitir_protocolo"]
    assert "aprovador" in repr(contracts)

    factory = RestBackendFactory(API, transport=httpx2.MockTransport(setup.api))
    try:
        rest = factory(_session(setup.approver, TOKEN))
        assert [s.slug for s in await rest.list_catalog()] == ["emitir_protocolo"]
        with pytest.raises(NotAuthenticated):
            factory(_session(setup.approver, None))
    finally:
        await factory.aclose()
