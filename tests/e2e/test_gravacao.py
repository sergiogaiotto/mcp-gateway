"""Demonstrar e Generalizar de ponta a ponta, pelo serviço inteiro e por mensagens de entrada.

O RaPA sobe num processo (``create_server_app``, sobre PostgreSQL) e serve por HTTP de
verdade. A criadora abre uma sessão de gravação pela API (o lançador local põe de pé o
hospedeiro, com Chromium e o gravador), pede um bilhete e conecta o websocket da visão ao
vivo. Pelo websocket ela recebe a tela e faz a tarefa só com mensagens de entrada, como a
interface faria: clica, digita o usuário, a senha e o código, e um nome com acentos.
Depois para pela API, recebe as propostas (o hub de LLM não está configurado: só as
sugestões das regras), confirma as entradas e cria o rascunho. O rascunho é validado,
publicado por outra pessoa e executado pelo despachante com outra pessoa como entrada.

No fim, nenhum segredo da demonstração aparece no banco (sessões, gravações, auditoria,
versões, eventos), nos registros do log nem no manifesto.
"""

import asyncio
import json
import logging
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from playwright.async_api import async_playwright
from sqlalchemy import text
from websockets.asyncio.client import connect

from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER, current_totp
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from tests.control.api.support import auth, serve
from tests.control.db.support import Tenant
from tests.e2e.support import TOKENS, wait_finished
from tests.session.support import LiveClient

pytestmark = [pytest.mark.database, pytest.mark.browser]

NAME = "João da Conceição"
CPF = "52998224725"
NEW_PERSON = {"nome": "Ana Lúcia Brandão", "cpf": "111.444.777-35"}


async def test_record_by_input_messages_generalize_draft_publish_and_run(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    chromium_path: str,
    superuser_engine: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    app, tenant, other = platform_app
    base = portal_v1.base_url
    creator, approver = auth(TOKENS["creator"]), auth(TOKENS["approver"])
    with serve(app) as api:
        async with (
            httpx2.AsyncClient(base_url=api, trust_env=False, timeout=60) as client,
            async_playwright() as playwright,
        ):
            body = {
                "area_id": str(tenant.area_id),
                "endereco": f"{base}/login",
                "titulo": "Emitir protocolo",
            }
            # Quem não cria robôs na área não abre sessão; a área de outra empresa não existe.
            for token, status in (("operator", 403), ("outsider", 403), ("other", 404)):
                refused = await client.post("/v1/sessoes", json=body, headers=auth(TOKENS[token]))
                assert refused.status_code == status, token
            created = await client.post("/v1/sessoes", json=body, headers=creator)
            assert created.status_code == 201, created.text
            session = created.json()
            sid = session["session_id"]
            assert session["status"] == "active"
            ticket = await client.post(f"/v1/sessoes/{sid}/bilhete", headers=creator)
            assert ticket.status_code == 200
            ws_url = api.replace("http://", "ws://") + ticket.json()["path"]
            ws = await connect(ws_url, max_size=None)
            live = LiveClient(ws)
            # O bilhete é a primeira mensagem, nunca vai no endereço.
            await live.send(tipo="bilhete", bilhete=ticket.json()["ticket"])
            # Um navegador do mesmo tamanho só mede onde ficam os campos (sem eventos no outro).
            browser = await playwright.chromium.launch(
                executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
            )
            try:
                ready = await live.event("pronta", within=30)
                measure = await (
                    await browser.new_context(
                        viewport={"width": ready["largura"], "height": ready["altura"]}
                    )
                ).new_page()
                await live.page("/login", within=30)
                await live.wait_for(lambda: live.frames, what="a primeira imagem da tela")
                assert live.frames[0].startswith(b"\xff\xd8")
                await measure.goto(f"{base}/login")
                await live.click_on(measure.get_by_label("Usuário"))
                await live.type(DEFAULT_USER)
                await live.click_on(measure.get_by_label("Senha"))
                await live.type(DEFAULT_PASSWORD)
                await live.click_on(measure.get_by_role("button", name="Entrar"))
                await live.page("/login/codigo")
                # O medidor também entra, para ver as páginas de quem está logado.
                await measure.get_by_label("Usuário").fill(DEFAULT_USER)
                await measure.get_by_label("Senha").fill(DEFAULT_PASSWORD)
                await measure.get_by_role("button", name="Entrar").click()
                await measure.wait_for_url("**/login/codigo")
                await live.click_on(measure.get_by_label("Código de verificação"))
                await live.type(current_totp())
                await live.press("Enter")
                await live.page("/inicio")
                await measure.get_by_label("Código de verificação").fill(current_totp())
                await measure.get_by_role("button", name="Confirmar").click()
                await measure.wait_for_url("**/inicio")
                await live.click_on(measure.get_by_role("link", name="Emitir protocolo"))
                await live.page("/protocolo")
                await measure.goto(f"{base}/protocolo")
                await live.click_on(measure.get_by_label("Nome completo"))
                await live.type(NAME)
                await live.click_on(measure.get_by_label("CPF"))
                await live.type(CPF)
                await live.click_on(
                    measure.get_by_text("Declaro que as informações são verdadeiras")
                )
                await live.click_on(measure.get_by_role("button", name="Emitir"))
                await live.page("/protocolo", within=20)
                await live.event(
                    "pagina",
                    titulo=lambda t: isinstance(t, str) and t.startswith("Protocolo emitido"),
                )
            finally:
                await browser.close()
            assert len(portal_v1.state.protocolos) == 1
            assert portal_v1.state.protocolos[0]["nome"] == NAME

            # Parar devolve a gravação guardada; a janela recebe o fim.
            stopped = await client.post(f"/v1/sessoes/{sid}/parar", headers=creator)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "stopped" and stopped.json()["recording_id"]
            await live.event("fim", motivo="parada")
            await asyncio.wait_for(live.closed.wait(), timeout=10)
            await live.close()

            # Generalizar: sem o hub, as sugestões das regras, com o aviso.
            proposals = (await client.get(f"/v1/sessoes/{sid}/propostas", headers=creator)).json()
            assert proposals["model_available"] is False
            assert "modelo" in proposals["model_message"]
            by_step = {p["step_id"]: p for p in proposals["parameters"]}
            nome = next(p for p in by_step.values() if p["value_preview"] == NAME)
            cpf = next(p for p in by_step.values() if p["type"] == "cpf")
            hidden = await client.get(f"/v1/sessoes/{sid}/propostas", headers=auth(TOKENS["other"]))
            assert hidden.status_code == 404
            choices = {
                "name": "emitir_protocolo_demonstrado",
                "title": "Emitir protocolo (demonstrado)",
                "description": "Emite um protocolo para a pessoa informada.",
                "parameters": [
                    {"step_id": nome["step_id"], "name": "nome", "title": "Nome completo"},
                    {"step_id": cpf["step_id"], "name": "cpf", "title": "CPF", "type": "cpf"},
                ],
            }
            draft = await client.post(f"/v1/sessoes/{sid}/rascunho", json=choices, headers=creator)
            assert draft.status_code == 201, draft.text
            version = draft.json()
            assert version["violations"] == []
            manifest = version["manifest"]
            assert {f["name"]: f["type"] for f in manifest["inputs"]} == {
                "nome": "string",
                "cpf": "cpf",
            }
            assert {c["name"] for c in manifest["credentials"]} >= {"portal_v1"}
            location = draft.headers["location"]

            # O rascunho segue o caminho de sempre: quem criou não publica.
            assert (await client.post(f"{location}/validar", headers=creator)).status_code == 200
            assert (await client.post(f"{location}/publicar", headers=creator)).status_code == 403
            published = await client.post(f"{location}/publicar", headers=approver)
            assert published.status_code == 200, published.text
            run = await client.post(
                "/v1/catalogo/emitir_protocolo_demonstrado/execucoes",
                json={"entradas": NEW_PERSON},
                headers=auth(TOKENS["operator"]),
            )
            assert run.status_code == 202, run.text
            done = await wait_finished(client, run.json()["run_id"], TOKENS["operator"])
            assert done["state"] == "succeeded", done
    assert len(portal_v1.state.protocolos) == 2
    assert portal_v1.state.protocolos[-1]["nome"] == NEW_PERSON["nome"]
    assert portal_v1.state.protocolos[-1]["cpf"] == NEW_PERSON["cpf"]

    # -- nenhum segredo da demonstração em lugar nenhum ---------------------------
    secrets = (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER)
    async with superuser_engine.connect() as conn:
        dumps = []
        for table in (
            "recording_sessions",
            "recordings",
            "audit_log",
            "robot_versions",
            "runs",
            "run_events",
        ):
            rows = await conn.execute(
                text(f"SELECT row_to_json(t)::text FROM {table} t WHERE company_id = :c"),  # noqa: S608
                {"c": tenant.company_id},
            )
            dumps += list(rows.scalars())
        recording = await conn.scalar(
            text("SELECT recording::text FROM recordings WHERE session_id = :s"), {"s": sid}
        )
        statuses = await conn.scalar(
            text("SELECT status FROM recording_sessions WHERE id = :s"), {"s": sid}
        )
    assert recording is not None and NAME in recording
    assert statuses == "drafted"
    everything = "\n".join(dumps) + json.dumps(manifest, ensure_ascii=False)
    everything += "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in everything
    assert other.company_id != tenant.company_id
