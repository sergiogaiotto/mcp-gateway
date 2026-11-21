"""Rotas ``/v1/sessoes`` e a visão ao vivo pelo websocket, num servidor HTTP de verdade."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from rapa.control.api.sessions import live_origins
from rapa.control.auth import StaticTokenVerifier
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.core.contracts import RecordingSessionStatus
from tests.control.api.support import DictResolver, auth, mcp_client, running, serve
from tests.control.sessions.support import FRAME, SessionsWorld, build_world

URL = "https://portal.exemplo.gov.br/login"
TOKENS = {
    "token-criadora": "criadora",
    "token-colega": "colega",
    "token-leitora": "leitora",
    "token-vizinha": "vizinha",
    "token-outra": "outra",
}


def _app(world: SessionsWorld | None, public_url: str | None = None) -> FastAPI:
    people = world.people if world is not None else build_world().people
    platform = world.platform if world is not None else build_world().platform
    return create_http_app(
        catalog=platform,
        runs=platform,
        admin=platform,
        verifier=StaticTokenVerifier({t: people[who] for t, who in TOKENS.items()}),
        resolver=DictResolver(),
        settings=HttpSettings(
            allow_static_tokens=True, rate_limit_per_s=None, public_url=public_url
        ),
        sessions=world.service if world is not None else None,
    )


@contextmanager
def _served(world: SessionsWorld | None, public_url: str | None = None) -> Iterator[str]:
    with serve(_app(world, public_url)) as base:
        yield base


@pytest.fixture
async def http() -> AsyncIterator[tuple[SessionsWorld, str, httpx2.AsyncClient]]:
    world = build_world()
    with _served(world) as base:
        async with httpx2.AsyncClient(base_url=base, trust_env=False, timeout=20) as client:
            yield world, base, client


async def _create(
    client: httpx2.AsyncClient, who: str = "token-criadora", **body: Any
) -> dict[str, Any]:
    payload = {"area_id": body.pop("area_id"), "endereco": URL, **body}
    response = await client.post("/v1/sessoes", json=payload, headers=auth(who))
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    assert response.headers["location"] == f"/v1/sessoes/{data['session_id']}"
    return data


async def _ticket(client: httpx2.AsyncClient, session_id: str, who: str = "token-criadora") -> str:
    response = await client.post(f"/v1/sessoes/{session_id}/bilhete", headers=auth(who))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    ticket: str = response.json()["ticket"]
    return ticket


async def _viewer(
    base: str, session_id: str, ticket: str, *, origin: str | None = None
) -> ClientConnection:
    """Abre a visão ao vivo como a interface: o bilhete é a primeira mensagem."""
    ws_base = base.replace("http://", "ws://")
    ws = await connect(
        f"{ws_base}/v1/sessoes/{session_id}/ao-vivo",
        origin=origin,  # type: ignore[arg-type]
    )
    await ws.send(json.dumps({"tipo": "bilhete", "bilhete": ticket}))
    return ws


async def _refused(ws: ClientConnection) -> int | None:
    """O servidor fechou sem mandar nada: devolve o código de fechamento."""
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=10)
            raise AssertionError(f"a janela recusada recebeu {message!r}")
    return closed.value.rcvd.code if closed.value.rcvd is not None else None


async def _until(ws: ClientConnection, check: Any, within: float = 10) -> list[Any]:
    seen: list[Any] = []
    async with asyncio.timeout(within):
        while not check(seen):
            message = await ws.recv()
            seen.append(json.loads(message) if isinstance(message, str) else message)
    return seen


async def test_rest_lifecycle_and_access_control(
    http: tuple[SessionsWorld, str, httpx2.AsyncClient],
) -> None:
    world, _base, client = http
    area = str(world.area_a)
    for token, status in (
        ("token-leitora", 403),
        ("token-vizinha", 403),
        ("token-outra", 403),
    ):
        response = await client.post(
            "/v1/sessoes", json={"area_id": area, "endereco": URL}, headers=auth(token)
        )
        assert response.status_code == status, token
        assert response.json()["erro"] == "proibido"
    response = await client.post("/v1/sessoes", json={"area_id": area, "endereco": URL})
    assert response.status_code == 401
    response = await client.post(
        "/v1/sessoes",
        json={"area_id": area, "endereco": "ftp://x.test/"},
        headers=auth("token-criadora"),
    )
    assert response.status_code == 422
    created = await _create(client, area_id=area, titulo="Cadastro")
    sid = created["session_id"]
    assert created["status"] == "active" and created["owner"] == "criadora@a"
    for token in ("token-colega", "token-outra", "token-vizinha"):
        assert (await client.get(f"/v1/sessoes/{sid}", headers=auth(token))).status_code == 404
        for action in ("bilhete", "parar"):
            response = await client.post(f"/v1/sessoes/{sid}/{action}", headers=auth(token))
            assert response.status_code == 404, (token, action)
        response = await client.get(f"/v1/sessoes/{sid}/propostas", headers=auth(token))
        assert response.status_code == 404
        listed = await client.get("/v1/sessoes", headers=auth(token))
        assert listed.json() == []
    listed = await client.get("/v1/sessoes", headers=auth("token-criadora"))
    assert [s["session_id"] for s in listed.json()] == [sid]
    response = await client.get(f"/v1/sessoes/{sid}/propostas", headers=auth("token-criadora"))
    assert response.status_code == 409  # ainda gravando
    stopped = await client.post(f"/v1/sessoes/{sid}/parar", headers=auth("token-criadora"))
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped" and stopped.json()["recording_id"]
    proposals = await client.get(f"/v1/sessoes/{sid}/propostas", headers=auth("token-criadora"))
    assert proposals.status_code == 200
    body = proposals.json()
    assert body["model_available"] is False and body["model_message"]
    assert {p["name"] for p in body["parameters"]} >= {"nome", "cpf"}
    choices = {
        "name": body["name"],
        "title": body["title"],
        "description": body["description"],
        "parameters": [
            {"step_id": p["step_id"], "name": p["name"], "title": p["title"], "type": p["type"]}
            for p in body["parameters"]
        ],
    }
    refused = await client.post(
        f"/v1/sessoes/{sid}/rascunho", json=choices, headers=auth("token-colega")
    )
    assert refused.status_code == 404
    draft = await client.post(
        f"/v1/sessoes/{sid}/rascunho", json=choices, headers=auth("token-criadora")
    )
    assert draft.status_code == 201, draft.text
    version = draft.json()
    assert draft.headers["location"] == f"/v1/robos/{version['robot_id']}/versoes/1"
    assert version["status"] == "draft" and version["created_by"] == "criadora@a"
    assert {f["name"] for f in version["manifest"]["inputs"]} >= {"nome", "cpf"}
    session = await client.get(f"/v1/sessoes/{sid}", headers=auth("token-criadora"))
    assert session.json()["status"] == "drafted"
    bad = await client.post(
        f"/v1/sessoes/{sid}/rascunho",
        json={**choices, "step_names": [{"step_id": "fantasma", "title": "X"}]},
        headers=auth("token-criadora"),
    )
    assert bad.status_code == 422 and bad.json()["detalhes"]


async def test_live_websocket_needs_a_fresh_ticket_and_relays_both_ways(
    http: tuple[SessionsWorld, str, httpx2.AsyncClient],
) -> None:
    world, base, client = http
    sid = (await _create(client, area_id=str(world.area_a)))["session_id"]
    ticket = await _ticket(client, sid)
    for bad in ("", "inventado", ticket + "x", "A" * 43, "A" * 5000):
        assert await _refused(await _viewer(base, sid, bad)) == 1008
    # Os testes acima não gastaram o bilhete certo.
    ws = await _viewer(base, sid, ticket)
    seen = await _until(
        ws, lambda s: FRAME in s and any(isinstance(m, dict) and m["tipo"] == "pagina" for m in s)
    )
    assert seen[0]["tipo"] == "sessao" and seen[0]["restante_s"] > 3500
    await ws.send(json.dumps({"tipo": "mouse", "acao": "mover", "x": 5, "y": 5}))
    await ws.send(json.dumps({"tipo": "parar"}))
    await ws.send(json.dumps({"tipo": "texto", "texto": "Conceição"}))
    host = next(iter(world.launcher.hosts.values()))
    for _ in range(100):
        if len(host.received) >= 2:
            break
        await asyncio.sleep(0.05)
    assert [m["tipo"] for m in host.received] == ["mouse", "texto"]
    assert host.received[1]["texto"] == "Conceição"
    # O mesmo bilhete não abre outra janela.
    assert await _refused(await _viewer(base, sid, ticket)) == 1008
    # Outra janela, com outro bilhete, substitui a primeira.
    second = await _viewer(base, sid, await _ticket(client, sid))
    await _until(second, lambda s: any(isinstance(m, bytes) and m.startswith(FRAME) for m in s))
    replaced = await _until(
        ws, lambda s: any(isinstance(m, dict) and m.get("motivo") == "substituida" for m in s)
    )
    assert replaced
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4001
    stopped = await client.post(f"/v1/sessoes/{sid}/parar", headers=auth("token-criadora"))
    assert stopped.status_code == 200
    end = await _until(second, lambda s: any(isinstance(m, dict) and m["tipo"] == "fim" for m in s))
    assert {"tipo": "fim", "motivo": "parada"} in end
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(second.recv(), timeout=5)
    # Parada, a sessão não dá mais bilhete.
    response = await client.post(f"/v1/sessoes/{sid}/bilhete", headers=auth("token-criadora"))
    assert response.status_code == 409
    record = next(iter(world.store.sessions.values()))
    assert record.status is RecordingSessionStatus.STOPPED


async def test_without_the_session_service_everything_is_503() -> None:
    with _served(None) as base:
        async with httpx2.AsyncClient(base_url=base, trust_env=False) as client:
            response = await client.get("/v1/sessoes", headers=auth("token-criadora"))
            assert response.status_code == 503
            assert response.json()["erro"] == "indisponivel"
            assert response.headers["retry-after"] == "5"
        with pytest.raises(InvalidStatus) as caught:
            await connect(base.replace("http://", "ws://") + f"/v1/sessoes/{'0' * 32}/ao-vivo")
        assert caught.value.response.status_code == 403


async def test_sessions_are_not_mcp_tools() -> None:
    world = build_world()
    app = _app(world)
    async with running(app), mcp_client(app, "token-criadora") as mcp:
        names = {tool.name for tool in (await mcp.list_tools()).tools}
    assert not any("sess" in name or "grav" in name for name in names)


async def test_the_ticket_never_travels_in_the_address(
    http: tuple[SessionsWorld, str, httpx2.AsyncClient],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, base, client = http
    ws_base = base.replace("http://", "ws://")
    sid = (await _create(client, area_id=str(world.area_a)))["session_id"]
    other = await _create(client, who="token-outra", area_id=str(world.area_b))
    # O log do servidor HTTP, com as linhas de acesso dos websockets.
    for name in ("", "uvicorn.error", "uvicorn.access"):
        caplog.set_level(logging.DEBUG, logger=name)
    # Um bilhete no endereço é recusado antes de abrir, mesmo válido (e não é gasto).
    in_address = await _ticket(client, sid)
    with pytest.raises(InvalidStatus) as caught:
        await connect(f"{ws_base}/v1/sessoes/{sid}/ao-vivo?bilhete={in_address}")
    assert caught.value.response.status_code == 403
    ticket = await _ticket(client, sid)
    # Sem mensagem nenhuma, ou com outra coisa no lugar do bilhete, a conexão fecha.
    monkeypatch.setattr("rapa.control.api.sessions.TICKET_TIMEOUT_S", 0.3)
    silent = await connect(f"{ws_base}/v1/sessoes/{sid}/ao-vivo")
    assert await _refused(silent) == 1008
    wrong = await connect(f"{ws_base}/v1/sessoes/{sid}/ao-vivo")
    await wrong.send(json.dumps({"tipo": "mouse", "acao": "mover", "x": 1, "y": 1}))
    assert await _refused(wrong) == 1008
    # O bilhete de outra empresa não abre esta sessão.
    foreign = await _ticket(client, other["session_id"], who="token-outra")
    assert await _refused(await _viewer(base, sid, foreign)) == 1008
    # Uma colega da mesma área não recebe bilhete da sessão alheia.
    colleague = await client.post(f"/v1/sessoes/{sid}/bilhete", headers=auth("token-colega"))
    assert colleague.status_code == 404
    # O bilhete que ficou no endereço recusado ainda vale (nunca foi lido), mas a interface
    # nunca o põe lá: ela manda o bilhete como a primeira mensagem.
    ws = await _viewer(base, sid, ticket)
    seen = await _until(ws, lambda s: any(isinstance(m, dict) and m["tipo"] == "pagina" for m in s))
    assert seen[0]["tipo"] == "sessao"
    # "parar" vindo da janela nunca chega ao hospedeiro; uma mensagem enorme não derruba.
    await ws.send(json.dumps({"tipo": "parar"}))
    await ws.send("x" * (1024 * 1024))
    await ws.send(json.dumps({"tipo": "mouse", "acao": "mover", "x": 1, "y": 1}))
    host = world.launcher.hosts[UUID(sid)]
    for _ in range(100):
        if host.received:
            break
        await asyncio.sleep(0.05)
    assert [m["tipo"] for m in host.received] == ["mouse"]
    await ws.close()
    # Nenhum bilhete mandado como mensagem aparece em log nenhum (nem no de acesso).
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ao-vivo" in logged
    for value in (ticket, foreign):
        assert value not in logged


async def test_with_a_public_url_only_the_interface_origin_opens_the_live_view() -> None:
    world = build_world()
    # Os hospedeiros falsos vivem no laço do servidor e fecham com ele.
    with _served(world, public_url="https://rapa.exemplo.gov.br/api") as base:
        async with httpx2.AsyncClient(base_url=base, trust_env=False, timeout=20) as client:
            sid = (await _create(client, area_id=str(world.area_a)))["session_id"]
            ticket = await _ticket(client, sid)
            ws_base = base.replace("http://", "ws://")
            for evil in ("https://evil.example", "https://rapa.exemplo.gov.br.evil.example"):
                with pytest.raises(InvalidStatus) as caught:
                    await connect(
                        f"{ws_base}/v1/sessoes/{sid}/ao-vivo",
                        origin=evil,  # type: ignore[arg-type]
                    )
                assert caught.value.response.status_code == 403
            # A origem da interface (a da URL pública) abre; o bilhete não foi gasto acima.
            ws = await _viewer(base, sid, ticket, origin="https://RAPA.exemplo.gov.br")
            seen = await _until(ws, lambda s: any(isinstance(m, dict) for m in s))
            assert seen[0]["tipo"] == "sessao"
            await ws.close()
            # Fora do navegador (sem Origin), só o bilhete vale.
            ws = await _viewer(base, sid, await _ticket(client, sid))
            assert (await _until(ws, lambda s: bool(s)))[0]["tipo"] == "sessao"
            await ws.close()


def test_live_origins_come_from_the_public_url_and_the_extra_list() -> None:
    assert live_origins(None) == frozenset()
    assert live_origins("https://RaPA.exemplo.gov.br:8443/base/") == {
        "https://rapa.exemplo.gov.br:8443"
    }
    assert live_origins("https://api.exemplo", ["https://ui.exemplo/", " "]) == {
        "https://api.exemplo",
        "https://ui.exemplo",
    }
