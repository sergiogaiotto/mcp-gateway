"""Mapeamento de erros restantes e o balde de fichas por empresa."""

import logging
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from rapa.control.api.errors import status_for
from rapa.control.api.ratelimit import RateLimiter
from rapa.control.auth import AuthUnavailable, VerifiedToken
from rapa.core.contracts import (
    Conflict,
    ContractError,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    QuotaExceeded,
    RobotSummary,
)
from rapa.core.ids import new_id
from tests.control.api.support import (
    TOKEN_A,
    DictResolver,
    World,
    asgi_client,
    auth,
    make_app,
    running,
)


def test_status_for_every_contract_error() -> None:
    assert status_for(NotFound("x")) == 404
    assert status_for(Forbidden("x")) == 403
    assert status_for(Conflict("x")) == 409
    assert status_for(InvalidInput("x", ["a"])) == 422
    assert status_for(QuotaExceeded("x", 3)) == 429
    assert status_for(ContractError("x")) == 400


class _GenericErrorCatalog:
    async def list_published(self, principal: Principal) -> list[RobotSummary]:
        raise ContractError("operação não suportada")

    async def get_published(self, principal: Principal, slug: str) -> RobotSummary | None:
        return None

    async def search(self, principal: Principal, query: str, limit: int = 10) -> list[RobotSummary]:
        return []


def test_generic_contract_error_is_400(world: World) -> None:
    with TestClient(make_app(world, catalog=_GenericErrorCatalog())) as client:
        response = client.get("/v1/catalogo", headers=auth(TOKEN_A))
    assert response.status_code == 400
    assert response.json() == {"erro": "erro", "mensagem": "operação não suportada", "detalhes": []}


class _DownVerifier:
    async def verify(self, token: str) -> VerifiedToken:
        raise AuthUnavailable("não foi possível verificar o token agora")


def test_identity_provider_outage_is_503(world: World) -> None:
    with TestClient(make_app(world, verifier=_DownVerifier())) as client:
        response = client.get("/v1/catalogo", headers=auth("qualquer"))
    assert response.status_code == 503
    assert response.json()["erro"] == "autenticacao_indisponivel"
    assert response.headers["retry-after"] == "5"


class _CrashingResolver(DictResolver):
    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        raise RuntimeError("conexão recusada: senha=nao-deveria-aparecer")


class _PassThroughVerifier:
    async def verify(self, token: str) -> VerifiedToken:
        return VerifiedToken(subject="alguem", company_id=new_id())


async def test_resolver_crash_is_500_without_details(
    async_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    app = make_app(async_world, verifier=_PassThroughVerifier(), resolver=_CrashingResolver())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with running(app), asgi_client(app, "qualquer") as http:
        rest = await http.get("/v1/catalogo")
        mcp = await http.post(
            "/mcp", json=body, headers={"Accept": "application/json, text/event-stream"}
        )
    for response in (rest, mcp):
        assert response.status_code == 500
        assert response.json()["erro"] == "erro_interno"
        assert "nao-deveria-aparecer" not in response.text
    # O registro tem o tipo e a pilha, nunca a mensagem.
    assert "RuntimeError" in caplog.text
    assert "nao-deveria-aparecer" not in caplog.text


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_token_bucket_refills_per_company() -> None:
    clock = _Clock()
    limiter = RateLimiter(rate_per_s=2.0, burst=3, clock=clock, max_keys=2)
    a, b, c = new_id(), new_id(), new_id()
    assert [limiter.try_acquire(a) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.try_acquire(a) == pytest.approx(0.5)
    assert limiter.try_acquire(b) == 0.0
    clock.now += 0.5
    assert limiter.try_acquire(a) == 0.0
    clock.now += 100
    assert [limiter.try_acquire(a) for _ in range(3)] == [0.0, 0.0, 0.0]
    with pytest.raises(QuotaExceeded) as error:
        limiter.check(a)
    assert error.value.retry_after_s == 1
    # Só guarda as empresas mais recentes; a mais antiga recomeça com o balde cheio.
    limiter.try_acquire(c)
    assert limiter.try_acquire(b) == 0.0
    with pytest.raises(ValueError):
        RateLimiter(rate_per_s=0, burst=1)


def test_error_body_shape_is_stable(world: World) -> None:
    with TestClient(make_app(world)) as client:
        response = client.get("/v1/catalogo")
    body: dict[str, Any] = response.json()
    assert list(body) == ["erro", "mensagem", "detalhes"]
