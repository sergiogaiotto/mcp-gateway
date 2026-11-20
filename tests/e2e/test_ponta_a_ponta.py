"""Ponta a ponta: o RaPA inteiro num processo, sobre PostgreSQL, Chromium e o portal de teste.

O robô é criado e publicado pela API REST (quem cria não publica), chamado como
tool MCP pelo cliente oficial do SDK e pelo REST com chave de idempotência, e
executado de verdade pelo despachante: o lançador local roda o worker, que abre
o Chromium, entra no portal com usuário, senha e TOTP e emite o protocolo.
"""

import json
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from rapa.control.db import Database
from rapa.control.services import verify_chain
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.fixtures.server import RunningPortal
from tests.control.api.support import asgi_client, auth, mcp_client, running
from tests.control.db.support import Tenant
from tests.e2e.support import INPUTS, TOKENS, publish, wait_finished

pytestmark = [pytest.mark.database, pytest.mark.browser]


async def test_publish_by_rest_call_by_mcp_and_rest_and_run_on_the_portal(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    app_db: Database,
    superuser_engine: Any,
) -> None:
    app, tenant, _other = platform_app
    base, host = portal_v1.base_url, portal_v1.host

    async with running(app), asgi_client(app) as client:
        protocolo = await publish(
            client, tenant, emitir_protocolo(base, host).model_dump(mode="json")
        )
        await publish(client, tenant, baixar_notas(base, host).model_dump(mode="json"))

        # -- MCP: cada robô publicado vira uma tool, só para a empresa dele ------
        async with mcp_client(app, TOKENS["operator"]) as mcp:
            tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
            assert {"emitir_protocolo", "baixar_notas", "executar_robo"} <= set(tools)
            assert tools["emitir_protocolo"].annotations is not None
            assert tools["emitir_protocolo"].annotations.destructive_hint is True
            assert tools["baixar_notas"].annotations is not None
            assert tools["baixar_notas"].annotations.read_only_hint is True
            started = await mcp.call_tool("emitir_protocolo", INPUTS)
            assert not started.is_error, started.content
            mcp_run = started.structured_content
            assert mcp_run is not None and mcp_run["state"] == "queued"
            assert mcp_run["trigger"] == "mcp"
        async with mcp_client(app, TOKENS["other"]) as mcp:
            names = {tool.name for tool in (await mcp.list_tools()).tools}
            assert "emitir_protocolo" not in names
            peek = await mcp.call_tool("consultar_execucao", {"execucao_id": mcp_run["run_id"]})
            assert peek.is_error

        done = await wait_finished(client, mcp_run["run_id"], TOKENS["operator"])
        assert done["state"] == "succeeded", done
        assert len(portal_v1.state.protocolos) == 1
        issued = portal_v1.state.protocolos[0]
        assert done["outputs"] == {"protocolo": issued["numero"]}
        assert issued["cpf"] == INPUTS["cpf"] and issued["nome"] == INPUTS["nome"]

        # -- REST com chave de idempotência: repetir não duplica ------------------
        headers = {**auth(TOKENS["operator"]), "Idempotency-Key": "lote-notas-1"}
        first = await client.post(
            "/v1/catalogo/baixar_notas/execucoes", json={"entradas": {}}, headers=headers
        )
        again = await client.post(
            "/v1/catalogo/baixar_notas/execucoes", json={"entradas": {}}, headers=headers
        )
        assert first.status_code == 202, first.text
        assert again.json()["run_id"] == first.json()["run_id"]
        notas = await wait_finished(client, first.json()["run_id"], TOKENS["operator"])
        assert notas["state"] == "succeeded", notas
        assert len(notas["outputs"]["notas"]) == 10

        # -- quem vê o quê --------------------------------------------------------
        listed = await client.get("/v1/execucoes", headers=auth(TOKENS["operator"]))
        assert [(r["robot_slug"], r["trigger"]) for r in listed.json()] == [
            ("baixar_notas", "rest"),
            ("emitir_protocolo", "mcp"),
        ]
        for token in (TOKENS["outsider"], TOKENS["other"]):
            assert (await client.get("/v1/execucoes", headers=auth(token))).json() == []
            hidden = await client.get(f"/v1/execucoes/{mcp_run['run_id']}", headers=auth(token))
            assert hidden.status_code == 404
        me = await client.get("/v1/eu", headers=auth(TOKENS["approver"]))
        assert me.json()["subject"] == tenant.approver.subject

    # -- o que ficou gravado ------------------------------------------------------
    async with superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT kind, step_id, data FROM run_events WHERE run_id = :run ORDER BY seq"),
                {"run": mcp_run["run_id"]},
            )
        ).all()
        marked = await conn.scalar(
            text("SELECT irreversible_started_at IS NOT NULL FROM runs WHERE id = :run"),
            {"run": mcp_run["run_id"]},
        )
    kinds = [row.kind for row in rows]
    assert kinds[0] == "run_started" and kinds[-1] == "run_succeeded"
    assert any(row.step_id == "emitir" and row.data.get("irreversivel") for row in rows)
    assert marked is True  # o aviso do passo irreversível chegou ao banco antes do clique
    dump = json.dumps([list(row) for row in rows], default=str)
    for secret in (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert secret not in dump

    async with app_db.tenant_session(tenant.company_id) as session:
        chain = await verify_chain(session, tenant.company_id)
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY seq"))).all()
    assert chain.ok, chain.problems
    assert actions.count("robo.versao_publicada") == 2
    assert protocolo["version_no"] == 1
