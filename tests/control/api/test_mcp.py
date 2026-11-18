"""Gateway MCP de ponta a ponta com o cliente oficial do SDK, em processo."""

import logging
from typing import Any
from uuid import UUID

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from rapa.control.auth import StaticTokenVerifier
from rapa.control.mcp import IDEMPOTENCY_META_KEY, RESERVED_NAMES
from rapa.core.contracts import RunState, RunTrigger
from rapa.core.ids import new_id
from tests.control.api.support import (
    INPUTS,
    TOKEN_A,
    TOKEN_A_VIEWER,
    TOKEN_B,
    BrokenCatalog,
    SpyRuns,
    World,
    asgi_client,
    auth,
    make_app,
    mcp_client,
    running,
    serve,
)

META = sorted(RESERVED_NAMES)
MODES = ["legacy", "auto"]


def _text(result: Any) -> str:
    return "\n".join(block.text for block in result.content)


@pytest.mark.parametrize("mode", MODES)
async def test_initialize_and_list_tools_per_company(
    async_world: World, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    app = make_app(async_world)
    caplog.set_level(logging.WARNING, logger="rapa.control.mcp.server")
    async with running(app):
        async with mcp_client(app, TOKEN_A, mode=mode) as client:
            assert client.server_info is not None and client.server_info.name == "rapa"
            expected = "2026-07-28" if mode == "auto" else "2025-11-25"
            assert client.protocol_version == expected
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        async with mcp_client(app, TOKEN_B, mode=mode) as client:
            tools_b = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert sorted(tools) == sorted(
        [*META, "atualizar_cadastro", "baixar_notas", "emitir_protocolo"]
    )
    # O robô chamado "buscar_robo" fica fora: a ferramenta com esse nome é a fixa.
    assert tools["buscar_robo"].title == "Buscar robô"
    assert "nome reservado" in caplog.text
    for tool in tools.values():
        assert tool.title
        assert tool.annotations is not None and tool.annotations.title

    emitir = tools["emitir_protocolo"]
    assert emitir.annotations is not None
    assert emitir.annotations.destructive_hint is True
    assert emitir.annotations.read_only_hint is False
    assert emitir.input_schema["required"] == ["nome", "cpf", "uf"]
    notas = tools["baixar_notas"].annotations
    assert notas is not None and notas.read_only_hint is True
    assert notas.destructive_hint is False
    cadastro = tools["atualizar_cadastro"].annotations
    assert cadastro is not None
    assert cadastro.read_only_hint is False and cadastro.destructive_hint is False

    assert sorted(tools_b) == sorted([*META, "consultar_saldo", "emitir_protocolo"])
    assert tools_b["emitir_protocolo"].title == "Emitir protocolo (B)"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("json_response", [True, False])
async def test_call_robot_tool_starts_run(
    async_world: World, mode: str, json_response: bool
) -> None:
    spy = SpyRuns(async_world.platform)
    app = make_app(async_world, runs=spy, mcp_json_response=json_response)
    async with running(app), mcp_client(app, TOKEN_A, mode=mode) as client:
        result = await client.call_tool("emitir_protocolo", INPUTS)
    assert not result.is_error, _text(result)
    status = result.structured_content
    assert status["robot_slug"] == "emitir_protocolo"
    assert status["state"] == "queued"
    assert status["trigger"] == "mcp"
    assert "consultar_execucao" in _text(result)
    assert spy.started == [("emitir_protocolo", INPUTS, RunTrigger.MCP, None)]
    run = async_world.platform.runs[new_uuid(status["run_id"])]
    assert run.company_id == async_world.company_a


def new_uuid(value: str) -> UUID:
    return UUID(value)


async def test_invalid_arguments_never_reach_run_service(async_world: World) -> None:
    spy = SpyRuns(async_world.platform)
    app = make_app(async_world, runs=spy)
    async with running(app), mcp_client(app, TOKEN_A) as client:
        missing = await client.call_tool("emitir_protocolo", {"nome": "Maria"})
        bad_cpf = await client.call_tool("emitir_protocolo", {**INPUTS, "cpf": "123"})
        extra = await client.call_tool("emitir_protocolo", {**INPUTS, "senha": "x"})
        via_meta = await client.call_tool(
            "executar_robo", {"robo": "emitir_protocolo", "entradas": {"nome": 1}}
        )
    for result in (missing, bad_cpf, extra, via_meta):
        assert result.is_error
        assert "entrada_invalida" in _text(result)
        assert "Traceback" not in _text(result)
    assert "campo obrigatório ausente: cpf" in _text(missing)
    assert "cpf: valor fora do formato esperado" in _text(bad_cpf)
    assert "campo não declarado: senha" in _text(extra)
    assert "nome: tipo inválido" in _text(via_meta)
    assert spy.started == []


async def test_meta_tools_execute_and_status(async_world: World) -> None:
    app = make_app(async_world)
    args = {"robo": "emitir_protocolo", "entradas": INPUTS, "chave_idempotencia": "pedido-42"}
    async with running(app), mcp_client(app, TOKEN_A) as client:
        first = await client.call_tool("executar_robo", args)
        again = await client.call_tool("executar_robo", args)
        conflict = await client.call_tool(
            "executar_robo", {**args, "entradas": {**INPUTS, "uf": "RJ"}}
        )
        run_id = first.structured_content["run_id"]
        queued = await client.call_tool("consultar_execucao", {"execucao_id": run_id})
        async_world.platform.set_run_state(
            new_uuid(run_id), RunState.SUCCEEDED, outputs={"protocolo": "PRT-2026-0001"}
        )
        done = await client.call_tool("consultar_execucao", {"execucao_id": run_id})
        unknown = await client.call_tool("consultar_execucao", {"execucao_id": str(new_id())})
        not_uuid = await client.call_tool("consultar_execucao", {"execucao_id": "abc"})
    assert not first.is_error and not again.is_error
    assert again.structured_content["run_id"] == run_id
    assert conflict.is_error and "conflito" in _text(conflict)
    assert queued.structured_content["state"] == "queued"
    assert done.structured_content["state"] == "succeeded"
    assert done.structured_content["outputs"] == {"protocolo": "PRT-2026-0001"}
    assert "concluída com sucesso" in _text(done) and "PRT-2026-0001" in _text(done)
    assert unknown.is_error and "nao_encontrado" in _text(unknown)
    assert not_uuid.is_error and "entrada_invalida" in _text(not_uuid)


async def test_robot_tool_idempotency_through_meta(async_world: World) -> None:
    app = make_app(async_world)
    meta: Any = {IDEMPOTENCY_META_KEY: "chave-meta-1"}
    async with running(app), mcp_client(app, TOKEN_A) as client:
        first = await client.call_tool("emitir_protocolo", INPUTS, meta=meta)
        second = await client.call_tool("emitir_protocolo", INPUTS, meta=meta)
        other = await client.call_tool("emitir_protocolo", INPUTS)
    assert first.structured_content["run_id"] == second.structured_content["run_id"]
    assert other.structured_content["run_id"] != first.structured_content["run_id"]


async def test_search_tool(async_world: World) -> None:
    app = make_app(async_world)
    async with running(app), mcp_client(app, TOKEN_A) as client:
        found = await client.call_tool("buscar_robo", {"consulta": "emitir protocolo"})
        everything = await client.call_tool("buscar_robo", {"consulta": "", "limite": 50})
        bad = await client.call_tool("buscar_robo", {"consulta": "x", "limite": 0})
    assert not found.is_error
    assert found.structured_content["robos"][0]["slug"] == "emitir_protocolo"
    assert "input_schema" in found.structured_content["robos"][0]
    slugs = [robot["slug"] for robot in everything.structured_content["robos"]]
    assert sorted(slugs) == ["atualizar_cadastro", "baixar_notas", "emitir_protocolo"]
    assert bad.is_error and "limite" in _text(bad)


async def test_cross_company_isolation(async_world: World) -> None:
    app = make_app(async_world)
    async with running(app):
        async with mcp_client(app, TOKEN_A) as client_a:
            run_a = await client_a.call_tool("emitir_protocolo", INPUTS)
        run_id = run_a.structured_content["run_id"]
        async with mcp_client(app, TOKEN_B) as client_b:
            notas = await client_b.call_tool("baixar_notas", {})
            via_meta = await client_b.call_tool("executar_robo", {"robo": "baixar_notas"})
            peek = await client_b.call_tool("consultar_execucao", {"execucao_id": run_id})
            search = await client_b.call_tool("buscar_robo", {"consulta": "notas"})
            own = await client_b.call_tool("emitir_protocolo", INPUTS)
    assert notas.is_error and "nao_encontrado" in _text(notas)
    assert via_meta.is_error and "nao_encontrado" in _text(via_meta)
    assert peek.is_error and "nao_encontrado" in _text(peek)
    assert all(r["slug"] != "baixar_notas" for r in search.structured_content["robos"])
    # Mesmo nome de robô, empresa diferente: a execução é da empresa B.
    run_b = async_world.platform.runs[new_uuid(own.structured_content["run_id"])]
    assert run_b.company_id == async_world.company_b


async def test_reserved_name_cannot_be_executed(async_world: World) -> None:
    app = make_app(async_world)
    async with running(app), mcp_client(app, TOKEN_A) as client:
        result = await client.call_tool("executar_robo", {"robo": "buscar_robo"})
    assert result.is_error and "nao_encontrado" in _text(result)


async def test_viewer_cannot_run(async_world: World) -> None:
    app = make_app(async_world)
    async with running(app), mcp_client(app, TOKEN_A_VIEWER) as client:
        result = await client.call_tool("baixar_notas", {})
    assert result.is_error and "proibido" in _text(result)


async def test_internal_errors_do_not_leak(async_world: World) -> None:
    app = make_app(async_world, catalog=BrokenCatalog())
    async with running(app), mcp_client(app, TOKEN_A) as client:
        result = await client.call_tool("emitir_protocolo", INPUTS)
        with pytest.raises(Exception) as listing:
            await client.list_tools()
    assert result.is_error
    assert "erro_interno" in _text(result)
    assert BrokenCatalog.SECRET not in _text(result)
    assert BrokenCatalog.SECRET not in str(listing.value)


async def test_http_auth_origin_and_rate_limit(async_world: World) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    app = make_app(async_world, rate_limit_per_s=0.01, rate_limit_burst=1)
    async with running(app):
        async with asgi_client(app) as anonymous:
            missing = await anonymous.post("/mcp", json=body, headers=headers)
            wrong = await anonymous.post(
                "/mcp", json=body, headers={**headers, "Authorization": "Bearer nao-existe"}
            )
        async with asgi_client(app, TOKEN_A) as http:
            origin = await http.post(
                "/mcp", json=body, headers={**headers, "Origin": "https://malicioso.example"}
            )
            first = await http.post("/mcp", json=body, headers=headers)
            limited = await http.post("/mcp", json=body, headers=headers)
        async with asgi_client(app, TOKEN_B) as http_b:
            other_company = await http_b.post("/mcp", json=body, headers=headers)
    assert missing.status_code == 401
    assert missing.json()["erro"] == "nao_autenticado"
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert wrong.status_code == 401
    assert 'error="invalid_token"' in wrong.headers["www-authenticate"]
    assert origin.status_code == 403
    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["erro"] == "limite_excedido"
    assert int(limited.headers["retry-after"]) >= 1
    assert other_company.status_code == 200


async def test_static_tokens_resolve_only_configured_principals(async_world: World) -> None:
    verifier = StaticTokenVerifier({TOKEN_B: async_world.operator_b})
    app = make_app(async_world, verifier=verifier)
    async with running(app), asgi_client(app, TOKEN_A) as http:
        response = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401


@pytest.mark.parametrize("mode", MODES)
async def test_over_real_http_with_sse(async_world: World, mode: str) -> None:
    """Uvicorn numa porta livre, respostas em SSE, cliente oficial por HTTP de verdade."""
    app = make_app(async_world, mcp_json_response=False)
    with serve(app) as base_url:
        async with (
            httpx2.AsyncClient(headers=auth(TOKEN_A), trust_env=False, timeout=10) as http,
            Client(
                streamable_http_client(f"{base_url}/mcp", http_client=http), mode=mode
            ) as client,
        ):
            names = {tool.name for tool in (await client.list_tools()).tools}
            started = await client.call_tool("baixar_notas", {})
            status = await client.call_tool(
                "consultar_execucao", {"execucao_id": started.structured_content["run_id"]}
            )
    assert "baixar_notas" in names
    assert not started.is_error and status.structured_content["state"] == "queued"


async def test_only_post_is_served(async_world: World) -> None:
    """Sem estado não há fluxo SSE do servidor: GET e DELETE recebem 405 na hora."""
    app = make_app(async_world)
    async with running(app), asgi_client(app, TOKEN_A) as http:
        get = await http.get("/mcp", headers={"Accept": "text/event-stream"})
        delete = await http.delete("/mcp")
    for response in (get, delete):
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.json()["erro"] == "metodo_nao_permitido"
