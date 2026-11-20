"""Criação de ponta a ponta: demonstrar, compilar, generalizar, publicar e executar.

Uma pessoa (simulada com ações reais no Chromium) demonstra a emissão de um
protocolo. O gravador captura a demonstração sem guardar senha nem código, o
compilador gera o robô, o hub de LLM (simulado, compatível com OpenAI) propõe
edições do conjunto fechado, o código as aplica, e o robô gravado é publicado
pelo REST e chamado pelo MCP com outra pessoa como entrada.
"""

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from playwright.async_api import Browser, Page, async_playwright

from rapa.compiler import lower
from rapa.core import policy
from rapa.core.edits import apply_edits
from rapa.core.robot import Effect, FieldType
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_USER, current_totp
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.llm.generalize import propose_edits
from rapa.recorder import Recorder, Recording
from tests.control.api.support import asgi_client, auth, mcp_client, running
from tests.control.db.support import Tenant
from tests.e2e.support import TOKENS, publish, wait_finished
from tests.llm.fakehub import FakeHub, completion_message, make_matrix

pytestmark = [pytest.mark.database, pytest.mark.browser]

DEMONSTRATED = {"nome": "Maria da Silva", "cpf": "52998224725"}
NEW_PERSON = {"nome": "João Pereira", "cpf": "111.444.777-35"}


@pytest.fixture
async def browser(chromium_path: str) -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
        )
        try:
            yield browser
        finally:
            await browser.close()


async def _demonstrate(browser: Browser, portal: RunningPortal) -> Recording:
    context = await browser.new_context()
    try:
        recorder = Recorder()
        await recorder.start(context)
        page: Page = await context.new_page()
        await page.goto(f"{portal.base_url}/login")
        await page.get_by_label("Usuário").fill(DEFAULT_USER)
        await page.get_by_label("Senha").fill(DEFAULT_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()
        await page.wait_for_url("**/login/codigo")
        await page.get_by_label("Código de verificação").fill(current_totp())
        await page.get_by_role("button", name="Confirmar").click()
        await page.wait_for_url("**/inicio")
        await page.get_by_role("link", name="Emitir protocolo").click()
        await page.get_by_label("Nome completo").fill(DEMONSTRATED["nome"])
        await page.get_by_label("CPF").press_sequentially(DEMONSTRATED["cpf"])
        await page.get_by_label("UF").select_option("SP")
        await page.get_by_label("Declaro que as informações são verdadeiras").check()
        await page.get_by_role("button", name="Emitir").click()
        await page.get_by_test_id("protocolo").wait_for()
        return await recorder.stop()
    finally:
        await context.close()


def _model_answer() -> str:
    """O que o modelo responderia: nomes amigáveis e o nome e o CPF como entradas."""
    return json.dumps(
        {
            "edits": [
                {
                    "op": "set_robot_info",
                    "title": "Emitir protocolo (gravado)",
                    "description": "Emite um protocolo no portal para a pessoa informada.",
                },
                {
                    "op": "parametrize",
                    "step_id": "digitar_nome_completo",
                    "input_name": "nome",
                    "title": "Nome completo",
                    "type": "string",
                },
                {
                    "op": "parametrize",
                    "step_id": "digitar_cpf",
                    "input_name": "cpf",
                    "title": "CPF",
                    "type": "cpf",
                },
                {"op": "rename_step", "step_id": "clicar_emitir", "title": "Emitir o protocolo"},
                # Tentativa de baixar o efeito: fora do conjunto fechado, o código recusa.
                {"op": "raise_effect", "step_id": "clicar_emitir", "effect": "write"},
                {"op": "add_text_postcondition", "text": "Protocolo emitido"},
            ]
        },
        ensure_ascii=False,
    )


async def test_demonstrate_compile_generalize_publish_and_run(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    browser: Browser,
) -> None:
    app, tenant, _other = platform_app

    # 1. Demonstração e compilação, sem segredos na gravação nem no robô.
    recording = await _demonstrate(browser, portal_v1)
    assert len(portal_v1.state.protocolos) == 1
    compiled = lower(
        recording,
        name="emitir_protocolo_gravado",
        title="Robô gravado",
        description="Robô gravado na demonstração.",
    )
    assert DEFAULT_PASSWORD not in recording.model_dump_json()
    assert DEFAULT_PASSWORD not in compiled.manifest.model_dump_json()
    assert policy.check(compiled.manifest) == []

    # 2. Generalização assistida: o modelo propõe, o código decide.
    hub = FakeHub(script=[completion_message(_model_answer())])
    async with hub.client() as client:
        ops = await propose_edits(
            client,
            compiled.manifest,
            capabilities=make_matrix(),
            observed_texts=compiled.observed_texts,
        )
    edited = apply_edits(compiled.manifest, ops, observed_texts=compiled.observed_texts)
    sent = json.dumps(hub.chat_requests, ensure_ascii=False)
    assert DEFAULT_PASSWORD not in sent and DEFAULT_USER not in sent
    manifest = edited.manifest
    assert {f.name: f.type for f in manifest.inputs} == {
        "nome": FieldType.STRING,
        "cpf": FieldType.CPF,
    }
    emitir = manifest.step_by_id("clicar_emitir")
    assert emitir is not None and emitir.effect is Effect.IRREVERSIBLE
    assert policy.check(manifest) == []

    # 3. Publicação pelo REST e chamada pelo MCP com outra pessoa.
    async with running(app), asgi_client(app) as http:
        await publish(http, tenant, manifest.model_dump(mode="json"))
        async with mcp_client(app, TOKENS["operator"]) as mcp:
            tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
            tool = tools["emitir_protocolo_gravado"]
            assert tool.annotations is not None and tool.annotations.destructive_hint is True
            assert set(tool.input_schema["required"]) == {"nome", "cpf"}
            started = await mcp.call_tool("emitir_protocolo_gravado", NEW_PERSON)
            assert not started.is_error, started.content
            assert started.structured_content is not None
            run_id = started.structured_content["run_id"]
        done = await wait_finished(http, run_id, TOKENS["operator"])
        assert done["state"] == "succeeded", done
        listed = await http.get("/v1/execucoes", headers=auth(TOKENS["operator"]))
        assert listed.json()[0]["robot_slug"] == "emitir_protocolo_gravado"

    # O robô gravado emitiu um protocolo novo, para a pessoa nova.
    assert len(portal_v1.state.protocolos) == 2
    issued = portal_v1.state.protocolos[-1]
    assert issued["nome"] == NEW_PERSON["nome"]
    assert issued["cpf"] == NEW_PERSON["cpf"]
    assert issued["uf"] == "SP"
