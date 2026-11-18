"""Ponta a ponta: o RaPA inteiro num processo, sobre PostgreSQL, Chromium e o portal de teste.

O robô é criado e publicado pela API REST (quem cria não publica), chamado como
tool MCP pelo cliente oficial do SDK e pelo REST com chave de idempotência, e
executado de verdade pelo despachante: o lançador local roda o worker, que abre
o Chromium, entra no portal com usuário, senha e TOTP e emite o protocolo.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from rapa.control.db import Database
from rapa.control.services import verify_chain
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.server import ServerSettings, create_server_app
from tests.control.api.support import asgi_client, auth, mcp_client, running
from tests.control.db.conftest import app_db, pg_database, superuser_engine  # noqa: F401
from tests.control.db.support import PgDatabase, Tenant, make_tenant, reset_queue

pytestmark = [pytest.mark.database, pytest.mark.browser]

INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
SECRETS = {
    "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
    "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
}
TOKENS = {
    "creator": "token-criadora",
    "approver": "token-aprovador",
    "operator": "token-operadora",
    "outsider": "token-vizinho",
    "other": "token-outra-empresa",
}


async def _wait_finished(client: httpx2.AsyncClient, run_id: str, token: str) -> dict[str, Any]:
    for _ in range(600):
        response = await client.get(f"/v1/execucoes/{run_id}", headers=auth(token))
        assert response.status_code == 200, response.text
        status: dict[str, Any] = response.json()
        if status["finished_at"] is not None:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"a execução {run_id} não terminou em 60 s")


async def _publish(
    client: httpx2.AsyncClient, tenant: Tenant, manifest: dict[str, Any]
) -> dict[str, Any]:
    body = {"area_id": str(tenant.area_id), "manifesto": manifest}
    created = await client.post("/v1/robos", json=body, headers=auth(TOKENS["creator"]))
    assert created.status_code == 201, created.text
    base = created.headers["location"]
    validated = await client.post(f"{base}/validar", headers=auth(TOKENS["creator"]))
    assert validated.status_code == 200, validated.text
    # Quem criou não publica; quem aprova, sim.
    refused = await client.post(f"{base}/publicar", headers=auth(TOKENS["creator"]))
    assert refused.status_code == 403
    published = await client.post(f"{base}/publicar", headers=auth(TOKENS["approver"]))
    assert published.status_code == 200, published.text
    info: dict[str, Any] = published.json()
    assert info["status"] == "published"
    return info


@pytest.fixture
async def platform_app(
    app_db: Database,  # noqa: F811
    pg_database: PgDatabase,  # noqa: F811
    superuser_engine: Any,  # noqa: F811
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> AsyncIterator[tuple[FastAPI, Tenant, Tenant]]:
    await reset_queue(superuser_engine, total_slots=4)
    tenant = await make_tenant(app_db, "Ponta a ponta")
    other = await make_tenant(app_db, "Outra empresa")
    tokens = {
        TOKENS["creator"]: tenant.creator,
        TOKENS["approver"]: tenant.approver,
        TOKENS["operator"]: tenant.operator,
        TOKENS["outsider"]: tenant.outsider,
        TOKENS["other"]: other.admin,
    }
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(
        json.dumps(
            {t: {"sujeito": p.subject, "empresa": str(p.company_id)} for t, p in tokens.items()}
        )
    )
    secrets_file = tmp_path / "segredos.json"
    secrets_file.write_text(json.dumps({str(tenant.company_id): SECRETS}))
    settings = ServerSettings(
        database_url=pg_database.app_url,
        dispatcher_database_url=pg_database.dispatcher_url,
        ui=False,
        dev=True,
        dev_tokens_file=tokens_file,
        dev_secrets_file=secrets_file,
        rate_limit_per_s=None,
        poll_interval_s=0.2,
        work_dir=tmp_path / "trabalho",
        artifacts_dir=tmp_path / "artefatos",
        chromium=chromium_path,
        browser_args=(HOST_RESOLVER_ARG,),
    )
    # O lifespan (gateway MCP e despachante) roda dentro do teste: o SDK do MCP
    # exige abrir e fechar o grupo de tarefas na mesma tarefa.
    yield create_server_app(settings), tenant, other


async def test_publish_by_rest_call_by_mcp_and_rest_and_run_on_the_portal(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    app_db: Database,  # noqa: F811
    superuser_engine: Any,  # noqa: F811
) -> None:
    app, tenant, _other = platform_app
    base, host = portal_v1.base_url, portal_v1.host

    async with running(app), asgi_client(app) as client:
        protocolo = await _publish(
            client, tenant, emitir_protocolo(base, host).model_dump(mode="json")
        )
        await _publish(client, tenant, baixar_notas(base, host).model_dump(mode="json"))

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

        done = await _wait_finished(client, mcp_run["run_id"], TOKENS["operator"])
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
        notas = await _wait_finished(client, first.json()["run_id"], TOKENS["operator"])
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
