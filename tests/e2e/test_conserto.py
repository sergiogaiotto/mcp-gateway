"""Ponta a ponta do conserto: o robô do portal v1 quebra no v2 e é consertado pela API.

O robô (com o layout do v1) é publicado e chamado pelo REST; o executor de verdade
falha no botão de login (que no v2 se chama "Acessar") e guarda o retrato da página.
Sem o hub de LLM, a pessoa escolhe o candidato sozinha; o conserto cria o rascunho da
versão nova, que quem pediu valida mas não publica, e outra pessoa (com o papel de
aprovação) publica. A segunda falha (o botão irreversível "Emitir documento") é
consertada do mesmo jeito, antes de agir; a terceira execução emite o protocolo no v2.
"""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from rapa.control.db import Database
from rapa.control.services import verify_chain
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import RunningPortal
from tests.control.api.support import asgi_client, auth, running
from tests.control.db.support import PgDatabase, Tenant
from tests.e2e.conftest import _platform
from tests.e2e.support import INPUTS, TOKENS, publish, wait_finished
from tests.runner.support import set_step

pytestmark = [pytest.mark.database, pytest.mark.browser]


@pytest.fixture
async def repair_app(
    app_db: Database,
    pg_database: PgDatabase,
    superuser_engine: Any,
    portal_v2: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> tuple[FastAPI, Tenant, Tenant]:
    return await _platform(app_db, pg_database, superuser_engine, chromium_path, tmp_path)


async def _run(client: httpx2.AsyncClient) -> dict[str, Any]:
    started = await client.post(
        "/v1/catalogo/emitir_protocolo/execucoes",
        json={"entradas": INPUTS},
        headers=auth(TOKENS["operator"]),
    )
    assert started.status_code == 202, started.text
    return await wait_finished(client, started.json()["run_id"], TOKENS["operator"])


async def _repair(
    client: httpx2.AsyncClient, run: dict[str, Any], step_id: str, name: str
) -> dict[str, Any]:
    path = f"/v1/execucoes/{run['run_id']}/conserto"
    view = await client.get(path, headers=auth(TOKENS["creator"]))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["step"]["step_id"] == step_id
    # Sem hub: a sugestão vem desligada, e a pessoa escolhe sozinha.
    assert body["suggestion"]["status"] == "desligado"
    best = body["candidates"][0]
    assert (best["role"], best["name"]) == ("button", name)
    created = await client.post(
        path, json={"candidato": best["candidate_id"]}, headers=auth(TOKENS["creator"])
    )
    assert created.status_code == 201, created.text
    location = created.headers["location"]
    validated = await client.post(f"{location}/validar", headers=auth(TOKENS["creator"]))
    assert validated.status_code == 200, validated.text
    # Quem pediu o conserto não publica a versão nova; outra pessoa, que aprova, sim.
    own = await client.post(f"{location}/publicar", headers=auth(TOKENS["creator"]))
    assert own.status_code == 403
    published = await client.post(f"{location}/publicar", headers=auth(TOKENS["approver"]))
    assert published.status_code == 200, published.text
    info: dict[str, Any] = published.json()
    return info


async def test_fail_on_v2_repair_publish_by_someone_else_and_run_to_the_end(
    repair_app: tuple[FastAPI, Tenant, Tenant],
    portal_v2: RunningPortal,
    app_db: Database,
    superuser_engine: Any,
) -> None:
    app, tenant, _other = repair_app
    manifest = emitir_protocolo(portal_v2.base_url, portal_v2.host)
    for step_id in ("entrar", "emitir"):
        manifest = set_step(manifest, step_id, timeout_ms=2_000)

    async with running(app), asgi_client(app) as client:
        first = await publish(client, tenant, manifest.model_dump(mode="json"))
        assert first["version_no"] == 1

        # 1. Falha no botão de login e conserto.
        failed = await _run(client)
        assert failed["state"] == "failed", failed
        assert failed["error_code"] == "elemento_nao_encontrado"
        # Outra empresa não vê o conserto.
        hidden = await client.get(
            f"/v1/execucoes/{failed['run_id']}/conserto", headers=auth(TOKENS["other"])
        )
        assert hidden.status_code == 404
        second = await _repair(client, failed, "entrar", "Acessar")
        assert second["version_no"] == 2
        entrar = next(s for s in second["manifest"]["steps"] if s["id"] == "entrar")
        assert entrar["target"]["locators"][0]["name"] == "Acessar"

        # 2. Falha no botão irreversível, antes de agir, e conserto.
        failed_again = await _run(client)
        assert failed_again["state"] == "failed", failed_again
        assert portal_v2.state.protocolos == []
        third = await _repair(client, failed_again, "emitir", "Emitir documento")
        emitir = next(s for s in third["manifest"]["steps"] if s["id"] == "emitir")
        assert emitir["effect"] == "irreversible"

        # 3. A versão consertada roda até o fim no v2.
        done = await _run(client)
        assert done["state"] == "succeeded", done
        assert done["version_no"] == 3
        assert len(portal_v2.state.protocolos) == 1
        assert done["outputs"] == {"protocolo": portal_v2.state.protocolos[0]["numero"]}

    async with superuser_engine.connect() as conn:
        snapshots = (
            await conn.execute(
                text(
                    "SELECT step_id, snapshot FROM run_snapshots WHERE company_id = :c "
                    "ORDER BY created_at"
                ),
                {"c": tenant.company_id},
            )
        ).all()
        proposals = (
            await conn.execute(
                text(
                    "SELECT step_id, base_version_no, version_no, requested_by "
                    "FROM repair_proposals WHERE company_id = :c ORDER BY version_no"
                ),
                {"c": tenant.company_id},
            )
        ).all()
    assert [row.step_id for row in snapshots] == ["entrar", "emitir"]
    dump = json.dumps([row.snapshot for row in snapshots], ensure_ascii=False)
    for secret in (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER, INPUTS["nome"]):
        assert secret not in dump
    assert [tuple(row) for row in proposals] == [
        ("entrar", 1, 2, tenant.creator.subject),
        ("emitir", 2, 3, tenant.creator.subject),
    ]
    async with app_db.tenant_session(tenant.company_id) as session:
        chain = await verify_chain(session, tenant.company_id)
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY seq"))).all()
    assert chain.ok, chain.problems
    assert actions.count("conserto.pedido") == 2
    assert actions.count("robo.versao_publicada") == 3
