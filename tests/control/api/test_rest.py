"""API REST (/v1) com TestClient e a plataforma em memória."""

import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from rapa.control.auth import StaticTokenVerifier
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.validation import InputValidator
from rapa.core.contracts import Principal, QuotaExceeded, RunStatus, RunTrigger
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform
from tests.control.api.support import (
    INPUTS,
    TOKEN_A,
    TOKEN_A_CREATOR,
    TOKEN_A_VIEWER,
    TOKEN_B,
    BrokenCatalog,
    DictResolver,
    SpyRuns,
    World,
    auth,
    make_app,
    simple_robot,
)


def _error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"erro", "mensagem", "detalhes"}
    assert body["erro"] == code
    assert body["mensagem"]
    return body


# -- saúde e autenticação -----------------------------------------------------


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/saude")
    assert response.status_code == 200
    assert response.json()["estado"] == "ok"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpzZW5oYQ=="}, {"Authorization": "Bearer"}, auth("x y")],
)
def test_missing_or_malformed_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/v1/catalogo", headers=headers)
    _error(response, 401, "nao_autenticado")
    assert response.headers["www-authenticate"].startswith('Bearer realm="rapa"')


def test_unknown_token(client: TestClient) -> None:
    response = client.get("/v1/catalogo", headers=auth("token-que-nao-existe"))
    _error(response, 401, "nao_autenticado")
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_static_tokens_require_explicit_opt_in(world: World) -> None:
    with pytest.raises(ValueError, match="allow_static_tokens"):
        create_http_app(
            catalog=world.platform,
            runs=world.platform,
            admin=world.platform,
            verifier=StaticTokenVerifier(world.tokens),
            resolver=DictResolver(),
            settings=HttpSettings(),
        )


# -- catálogo -----------------------------------------------------------------


def test_catalog_is_per_company(client: TestClient) -> None:
    a = client.get("/v1/catalogo", headers=auth(TOKEN_A))
    b = client.get("/v1/catalogo", headers=auth(TOKEN_B))
    assert a.status_code == b.status_code == 200
    assert [r["slug"] for r in a.json()] == [
        "atualizar_cadastro",
        "baixar_notas",
        "buscar_robo",
        "emitir_protocolo",
    ]
    assert [r["slug"] for r in b.json()] == ["consultar_saldo", "emitir_protocolo"]
    emitir_b = next(r for r in b.json() if r["slug"] == "emitir_protocolo")
    assert emitir_b["title"] == "Emitir protocolo (B)"
    assert all(r["company_id"] != emitir_b["company_id"] for r in a.json())


def test_catalog_robot_and_search(client: TestClient, world: World) -> None:
    robot = client.get("/v1/catalogo/emitir_protocolo", headers=auth(TOKEN_A))
    assert robot.status_code == 200
    assert UUID(robot.json()["robot_id"]) == world.robot_ids["a", "emitir_protocolo"]
    assert robot.json()["effect"] == "irreversible"

    found = client.get(
        "/v1/catalogo/busca", params={"q": "emissão protocolo"}, headers=auth(TOKEN_A)
    )
    assert found.status_code == 200
    assert found.json()[0]["slug"] == "emitir_protocolo"
    limited = client.get("/v1/catalogo/busca", params={"limite": 1}, headers=auth(TOKEN_A))
    assert len(limited.json()) == 1
    for bad in ({"limite": 0}, {"limite": 51}, {"q": "x" * 201}):
        response = client.get("/v1/catalogo/busca", params=bad, headers=auth(TOKEN_A))
        _error(response, 422, "entrada_invalida")


def test_other_company_robot_is_not_found(client: TestClient) -> None:
    for path in ("/v1/catalogo/baixar_notas", "/v1/catalogo/NAO-E-SLUG", "/v1/catalogo/xyz"):
        _error(client.get(path, headers=auth(TOKEN_B)), 404, "nao_encontrado")


# -- execuções ----------------------------------------------------------------


def test_start_run_and_follow_location(client: TestClient, world: World) -> None:
    response = client.post(
        "/v1/catalogo/emitir_protocolo/execucoes",
        json={"entradas": INPUTS},
        headers=auth(TOKEN_A),
    )
    assert response.status_code == 202, response.text
    status = RunStatus.model_validate(response.json())
    assert status.trigger is RunTrigger.REST
    assert status.state.value == "queued"
    assert response.headers["location"] == f"/v1/execucoes/{status.run_id}"
    assert world.platform.runs[status.run_id].company_id == world.company_a

    follow = client.get(response.headers["location"], headers=auth(TOKEN_A))
    assert follow.status_code == 200
    assert follow.json()["run_id"] == str(status.run_id)
    # A empresa B não enxerga a execução da empresa A.
    _error(client.get(response.headers["location"], headers=auth(TOKEN_B)), 404, "nao_encontrado")
    _error(client.get(f"/v1/execucoes/{new_id()}", headers=auth(TOKEN_A)), 404, "nao_encontrado")
    _error(client.get("/v1/execucoes/nao-e-uuid", headers=auth(TOKEN_A)), 422, "entrada_invalida")


def test_whoami_and_recent_runs(client: TestClient, world: World) -> None:
    me = client.get("/v1/eu", headers=auth(TOKEN_A))
    assert me.status_code == 200, me.text
    assert me.json()["subject"] == world.operator_a.subject
    assert me.json()["company_id"] == str(world.company_a)
    assert sorted(me.json()["company_roles"]) == ["approver", "operator"]

    started = [
        client.post(
            "/v1/catalogo/emitir_protocolo/execucoes",
            json={"entradas": INPUTS},
            headers=auth(TOKEN_A),
        ).json()["run_id"]
        for _ in range(3)
    ]
    listed = client.get("/v1/execucoes", params={"limite": 2}, headers=auth(TOKEN_A))
    assert listed.status_code == 200, listed.text
    assert [r["run_id"] for r in listed.json()] == started[::-1][:2]
    assert client.get("/v1/execucoes", headers=auth(TOKEN_B)).json() == []
    _error(
        client.get("/v1/execucoes", params={"limite": 0}, headers=auth(TOKEN_A)),
        422,
        "entrada_invalida",
    )
    _error(client.get("/v1/execucoes"), 401, "nao_autenticado")


def test_company_comes_from_token_not_from_body(client: TestClient, world: World) -> None:
    response = client.post(
        "/v1/catalogo/emitir_protocolo/execucoes",
        json={"entradas": INPUTS, "company_id": str(world.company_a)},
        headers=auth(TOKEN_B),
    )
    body = _error(response, 422, "entrada_invalida")
    assert any("company_id" in d for d in body["detalhes"])
    own = client.post(
        "/v1/catalogo/emitir_protocolo/execucoes", json={"entradas": INPUTS}, headers=auth(TOKEN_B)
    )
    assert own.status_code == 202
    run = world.platform.runs[UUID(own.json()["run_id"])]
    assert run.company_id == world.company_b


def test_invalid_inputs_are_rejected_before_starting(world: World) -> None:
    spy = SpyRuns(world.platform)
    with TestClient(make_app(world, runs=spy)) as client:
        url = "/v1/catalogo/emitir_protocolo/execucoes"
        missing = client.post(url, json={"entradas": {"nome": "Maria"}}, headers=auth(TOKEN_A))
        bad_cpf = client.post(
            url, json={"entradas": {**INPUTS, "cpf": "12"}}, headers=auth(TOKEN_A)
        )
        no_body = client.post(url, headers=auth(TOKEN_A))
        not_object = client.post(url, json={"entradas": [1, 2]}, headers=auth(TOKEN_A))
        not_json = client.post(
            url,
            content=b"{entradas",
            headers={**auth(TOKEN_A), "Content-Type": "application/json"},
        )
    body = _error(missing, 422, "entrada_invalida")
    assert "campo obrigatório ausente: cpf" in body["detalhes"]
    assert "campo obrigatório ausente: uf" in body["detalhes"]
    assert (
        "cpf: valor fora do formato esperado"
        in _error(bad_cpf, 422, "entrada_invalida")["detalhes"]
    )
    assert "12" not in json.dumps(bad_cpf.json()["detalhes"])
    _error(no_body, 422, "entrada_invalida")
    _error(not_object, 422, "entrada_invalida")
    _error(not_json, 422, "entrada_invalida")
    assert spy.started == []


def test_idempotency_key(client: TestClient) -> None:
    url = "/v1/catalogo/emitir_protocolo/execucoes"
    headers = {**auth(TOKEN_A), "Idempotency-Key": "pedido-123"}
    first = client.post(url, json={"entradas": INPUTS}, headers=headers)
    second = client.post(url, json={"entradas": INPUTS}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    conflict = client.post(url, json={"entradas": {**INPUTS, "uf": "RJ"}}, headers=headers)
    _error(conflict, 409, "conflito")
    other_company = client.post(
        url, json={"entradas": INPUTS}, headers={**auth(TOKEN_B), "Idempotency-Key": "pedido-123"}
    )
    assert other_company.status_code == 202
    assert other_company.json()["run_id"] != first.json()["run_id"]
    for key in ("x" * 201, "com espaço", "acentuação"):
        response = client.post(
            url,
            json={"entradas": INPUTS},
            headers={**auth(TOKEN_A), "Idempotency-Key": key.encode()},
        )
        _error(response, 422, "entrada_invalida")
    longest = client.post(
        url, json={"entradas": INPUTS}, headers={**auth(TOKEN_A), "Idempotency-Key": "k" * 200}
    )
    assert longest.status_code == 202


def test_viewer_cannot_start_runs(client: TestClient) -> None:
    response = client.post(
        "/v1/catalogo/baixar_notas/execucoes", json={"entradas": {}}, headers=auth(TOKEN_A_VIEWER)
    )
    _error(response, 403, "proibido")


def test_other_company_cannot_start_runs(client: TestClient) -> None:
    response = client.post(
        "/v1/catalogo/baixar_notas/execucoes", json={"entradas": {}}, headers=auth(TOKEN_B)
    )
    _error(response, 404, "nao_encontrado")


class _QuotaRuns:
    async def start(self, *args: Any, **kwargs: Any) -> RunStatus:
        raise QuotaExceeded("limite de execuções simultâneas da área atingido", retry_after_s=7)

    async def get(self, principal: Principal, run_id: UUID) -> RunStatus | None:
        return None


def test_quota_from_run_service_maps_to_429(world: World) -> None:
    with TestClient(make_app(world, runs=_QuotaRuns())) as client:
        response = client.post(
            "/v1/catalogo/emitir_protocolo/execucoes",
            json={"entradas": INPUTS},
            headers=auth(TOKEN_A),
        )
    _error(response, 429, "limite_excedido")
    assert response.headers["retry-after"] == "7"


# -- administração de robôs ---------------------------------------------------


def test_robot_lifecycle_over_rest(client: TestClient, world: World) -> None:
    manifest = simple_robot(
        "novo_robo", "write", title="Novo robô", description="Robô criado pela API."
    )
    body = {"area_id": str(world.area_a), "manifesto": manifest.model_dump(mode="json")}
    created = client.post("/v1/robos", json=body, headers=auth(TOKEN_A_CREATOR))
    assert created.status_code == 201, created.text
    info = created.json()
    assert info["status"] == "draft" and info["version_no"] == 1
    assert info["violations"] == []
    base = f"/v1/robos/{info['robot_id']}/versoes/1"
    assert created.headers["location"] == base

    assert client.get(base, headers=auth(TOKEN_A)).json()["status"] == "draft"
    listed = client.get(f"/v1/robos/{info['robot_id']}/versoes", headers=auth(TOKEN_A))
    assert [v["version_no"] for v in listed.json()] == [1]
    _error(
        client.get(f"/v1/robos/{info['robot_id']}/versoes", headers=auth(TOKEN_B)),
        404,
        "nao_encontrado",
    )
    _error(client.get(base, headers=auth(TOKEN_B)), 404, "nao_encontrado")
    _error(
        client.get(f"/v1/robos/{info['robot_id']}/versoes/9", headers=auth(TOKEN_A)),
        404,
        "nao_encontrado",
    )
    _error(
        client.get(f"/v1/robos/{info['robot_id']}/versoes/0", headers=auth(TOKEN_A)),
        422,
        "entrada_invalida",
    )

    _error(client.post(f"{base}/publicar", headers=auth(TOKEN_A)), 409, "conflito")
    validated = client.post(f"{base}/validar", headers=auth(TOKEN_A_CREATOR))
    assert validated.status_code == 200 and validated.json()["status"] == "validated"
    _error(client.post(f"{base}/validar", headers=auth(TOKEN_A_CREATOR)), 409, "conflito")
    _error(client.post(f"{base}/publicar", headers=auth(TOKEN_A_CREATOR)), 403, "proibido")
    _error(client.post(f"{base}/publicar", headers=auth(TOKEN_B)), 404, "nao_encontrado")
    published = client.post(f"{base}/publicar", headers=auth(TOKEN_A))
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert published.json()["approved_by"] == world.operator_a.subject

    catalog = client.get("/v1/catalogo", headers=auth(TOKEN_A)).json()
    assert "novo_robo" in [r["slug"] for r in catalog]
    robots = client.get("/v1/robos", headers=auth(TOKEN_A)).json()
    assert "novo_robo" in [r["slug"] for r in robots]
    assert "novo_robo" not in [
        r["slug"] for r in client.get("/v1/robos", headers=auth(TOKEN_B)).json()
    ]


def test_create_robot_errors(client: TestClient, world: World) -> None:
    manifest = simple_robot("outro_robo", "read", title="Outro", description="Outro robô.")
    payload = manifest.model_dump(mode="json")
    forbidden = client.post(
        "/v1/robos",
        json={"area_id": str(world.area_a), "manifesto": payload},
        headers=auth(TOKEN_A_VIEWER),
    )
    _error(forbidden, 403, "proibido")
    broken = {**payload, "allowed_domains": ["10.0.0.1"], "steps": []}
    invalid = client.post(
        "/v1/robos",
        json={"area_id": str(world.area_a), "manifesto": broken},
        headers=auth(TOKEN_A_CREATOR),
    )
    body = _error(invalid, 422, "entrada_invalida")
    assert any("manifesto.allowed_domains" in d and "endereço IP" in d for d in body["detalhes"])
    assert any(d.startswith("manifesto.steps") for d in body["detalhes"])

    # Passa na estrutura, mas o portão de política acha violações: validar devolve 422.
    risky = simple_robot("robo_arriscado", "read", title="Arriscado", description="Domínio errado.")
    risky_payload = risky.model_dump(mode="json")
    risky_payload["steps"][0]["url"] = "http://outro.rapa.test/inicio"
    created = client.post(
        "/v1/robos",
        json={"area_id": str(world.area_a), "manifesto": risky_payload},
        headers=auth(TOKEN_A_CREATOR),
    )
    assert created.status_code == 201
    assert created.json()["violations"]
    url = f"/v1/robos/{created.json()['robot_id']}/versoes/1/validar"
    rejected = _error(client.post(url, headers=auth(TOKEN_A_CREATOR)), 422, "entrada_invalida")
    assert rejected["detalhes"] == created.json()["violations"]


# -- OpenAPI dos robôs --------------------------------------------------------


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for value in node for ref in _refs(value)]
    return []


def test_openapi_per_company(client: TestClient) -> None:
    doc_a = client.get("/v1/openapi-robos.json", headers=auth(TOKEN_A)).json()
    doc_b = client.get("/v1/openapi-robos.json", headers=auth(TOKEN_B)).json()
    assert doc_a["openapi"].startswith("3.1")
    paths_a = sorted(p for p in doc_a["paths"] if p.startswith("/v1/catalogo/"))
    assert paths_a == [
        "/v1/catalogo/atualizar_cadastro/execucoes",
        "/v1/catalogo/baixar_notas/execucoes",
        "/v1/catalogo/buscar_robo/execucoes",
        "/v1/catalogo/emitir_protocolo/execucoes",
    ]
    assert "/v1/catalogo/baixar_notas/execucoes" not in doc_b["paths"]
    operation = doc_a["paths"]["/v1/catalogo/emitir_protocolo/execucoes"]["post"]
    assert operation["operationId"] == "emitir_protocolo"
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    entradas = body_schema["properties"]["entradas"]
    assert entradas["required"] == ["nome", "cpf", "uf"]
    assert "$schema" not in entradas
    assert "202" in operation["responses"] and "422" in operation["responses"]
    # Toda referência aponta para um componente existente.
    components = doc_a["components"]["schemas"]
    for ref in _refs(doc_a):
        assert ref.startswith("#/components/schemas/")
        assert ref.removeprefix("#/components/schemas/") in components
    # Os esquemas embutidos compilam no validador do RaPA.
    validator = InputValidator()
    assert validator.errors(entradas, INPUTS) == []
    assert validator.errors(body_schema, {"entradas": INPUTS}) == []


def test_openapi_requires_token(client: TestClient) -> None:
    _error(client.get("/v1/openapi-robos.json"), 401, "nao_autenticado")


def test_own_api_docs_off_by_default(client: TestClient, world: World) -> None:
    assert client.get("/v1/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    with TestClient(make_app(world, api_docs=True)) as docs:
        assert docs.get("/v1/openapi.json").status_code == 200


# -- proteções comuns ---------------------------------------------------------


def test_rate_limit_per_company(world: World) -> None:
    app = make_app(world, rate_limit_per_s=0.01, rate_limit_burst=2)
    with TestClient(app) as client:
        ok = [client.get("/v1/catalogo", headers=auth(TOKEN_A)).status_code for _ in range(2)]
        limited = client.get("/v1/catalogo", headers=auth(TOKEN_A))
        other = client.get("/v1/catalogo", headers=auth(TOKEN_B))
    assert ok == [200, 200]
    _error(limited, 429, "limite_excedido")
    assert int(limited.headers["retry-after"]) >= 1
    assert other.status_code == 200


def test_request_body_limit(world: World) -> None:
    app = make_app(world, max_body_bytes=2048)
    with TestClient(app) as client:
        big = {"entradas": {**INPUTS, "nome": "x" * 4096}}
        response = client.post(
            "/v1/catalogo/emitir_protocolo/execucoes", json=big, headers=auth(TOKEN_A)
        )
        chunked = client.post(
            "/v1/catalogo/emitir_protocolo/execucoes",
            content=iter([b'{"entradas": {"nome": "', b"x" * 4096, b'"}}']),
            headers={**auth(TOKEN_A), "Content-Type": "application/json"},
        )
        mcp = client.post("/mcp", content=b"x" * 4096, headers=auth(TOKEN_A))
    _error(response, 413, "corpo_muito_grande")
    _error(chunked, 413, "corpo_muito_grande")
    _error(mcp, 413, "corpo_muito_grande")


def test_cors_off_by_default(world: World) -> None:
    preflight = {
        "Origin": "https://painel.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    }
    with TestClient(make_app(world)) as client:
        response = client.options("/v1/catalogo", headers=preflight)
        assert "access-control-allow-origin" not in response.headers
    with TestClient(make_app(world, cors_allowed_origins=("https://painel.example",))) as client:
        response = client.options("/v1/catalogo", headers=preflight)
        assert response.headers["access-control-allow-origin"] == "https://painel.example"


def test_unknown_route_and_method_use_error_shape(client: TestClient) -> None:
    _error(client.get("/v1/nada", headers=auth(TOKEN_A)), 404, "nao_encontrado")
    _error(client.delete("/v1/catalogo", headers=auth(TOKEN_A)), 405, "metodo_nao_permitido")


def test_unexpected_errors_do_not_leak(world: World) -> None:
    app = make_app(world, catalog=BrokenCatalog())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/catalogo", headers=auth(TOKEN_A))
    _error(response, 500, "erro_interno")
    assert BrokenCatalog.SECRET not in response.text
    assert "Traceback" not in response.text


def test_protected_resource_metadata(world: World) -> None:
    app = make_app(
        world,
        public_url="https://rapa.exemplo.test/",
        authorization_servers=("https://sso.exemplo.test/realms/rapa",),
    )
    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        unauthenticated = client.post("/mcp", json={})
        rest = client.get("/v1/catalogo")
        doc = client.get("/v1/openapi-robos.json", headers=auth(TOKEN_A)).json()
    assert metadata.json()["resource"] == "https://rapa.exemplo.test/mcp"
    assert metadata.json()["authorization_servers"] == ["https://sso.exemplo.test/realms/rapa"]
    expected = (
        'resource_metadata="https://rapa.exemplo.test/.well-known/oauth-protected-resource/mcp"'
    )
    assert expected in unauthenticated.headers["www-authenticate"]
    assert expected in rest.headers["www-authenticate"]
    assert doc["servers"] == [{"url": "https://rapa.exemplo.test"}]


def test_works_with_plain_memory_platform() -> None:
    """Sem robôs publicados, o catálogo vem vazio e o OpenAPI só tem a consulta."""
    platform = MemoryPlatform()
    principal = Principal(subject="ninguem@c", company_id=new_id())
    app = create_http_app(
        catalog=platform,
        runs=platform,
        admin=platform,
        verifier=StaticTokenVerifier({"token-c": principal}),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True),
    )
    with TestClient(app) as client:
        assert client.get("/v1/catalogo", headers=auth("token-c")).json() == []
        doc = client.get("/v1/openapi-robos.json", headers=auth("token-c")).json()
    assert list(doc["paths"]) == ["/v1/execucoes/{run_id}"]
