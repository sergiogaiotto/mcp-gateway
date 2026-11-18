"""Gravação de demonstrações reais no Chromium contra o portal de teste, e compilação.

A "pessoa" é simulada com ações do Playwright que disparam eventos reais do DOM
(fill, click, select_option, check, set_input_files, press).
"""

import re
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from playwright.async_api import Browser, Page, async_playwright

from rapa.compiler import CompileResult, lower
from rapa.core import policy
from rapa.core.edits import AddTextPostcondition, Parametrize, apply_edits
from rapa.core.robot import (
    AssertStep,
    ClickStep,
    CredentialKind,
    DownloadStep,
    Effect,
    FieldType,
    FillStep,
    LocatorBy,
    Manifest,
    NavigateStep,
    SecretValue,
    SelectStep,
    TextValue,
    UploadStep,
)
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_USER, current_totp
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.recorder import (
    REDACTED,
    ClickEvent,
    InputEvent,
    NavigationCause,
    NavigationEvent,
    Recorder,
    RecorderError,
    Recording,
)

pytestmark = pytest.mark.browser

type Scenario = Callable[[Page], Awaitable[None]]


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


async def record(browser: Browser, scenario: Scenario) -> Recording:
    context = await browser.new_context()
    try:
        recorder = Recorder()
        await recorder.start(context)
        page = await context.new_page()
        await scenario(page)
        return await recorder.stop()
    finally:
        await context.close()


async def login(
    page: Page, portal: RunningPortal, codes: list[str], *, enter: bool = False
) -> None:
    await page.goto(f"{portal.base_url}/login")
    await page.get_by_label("Usuário").fill(DEFAULT_USER)
    await page.get_by_label("Senha").fill(DEFAULT_PASSWORD)
    if enter:
        await page.get_by_label("Senha").press("Enter")
    else:
        await page.get_by_role("button", name="Entrar").click()
    await page.wait_for_url("**/login/codigo")
    code = current_totp()
    codes.append(code)
    await page.get_by_label("Código de verificação").fill(code)
    await page.get_by_role("button", name="Confirmar").click()
    await page.wait_for_url("**/inicio")


def compile_(recording: Recording, name: str) -> CompileResult:
    result = lower(recording, name=name, title="Robô gravado", description="Robô gravado no teste.")
    manifest = Manifest.model_validate(result.manifest.model_dump(mode="json"))
    assert policy.check(manifest) == []
    return result


def assert_no_secrets(text: str, codes: list[str], *, username: bool = True) -> None:
    assert DEFAULT_PASSWORD not in text
    for code in codes:
        assert not re.search(rf"(?<!\d){code}(?!\d)", text), "código TOTP vazou"
    if username:
        assert DEFAULT_USER not in text


def assert_login_steps(result: CompileResult) -> None:
    manifest = result.manifest
    fills = [s for s in manifest.steps if isinstance(s, FillStep)]
    usuario, senha, codigo = fills[:3]
    assert isinstance(usuario.value, SecretValue) and usuario.value.field == "username"
    assert isinstance(senha.value, SecretValue) and senha.value.field == "password"
    assert isinstance(codigo.value, SecretValue) and codigo.value.field == "totp"
    kinds = {c.name: c.kind for c in manifest.credentials}
    assert kinds[usuario.value.credential] is CredentialKind.PASSWORD
    assert usuario.value.credential == senha.value.credential
    assert kinds[codigo.value.credential] is CredentialKind.TOTP
    entrar = manifest.step_by_id("clicar_entrar")
    confirmar = manifest.step_by_id("clicar_confirmar")
    for click in (entrar, confirmar):
        assert isinstance(click, ClickStep)
        assert click.effect is Effect.READ
        assert click.expect_navigation
    first = manifest.steps[0]
    assert isinstance(first, NavigateStep) and first.url.endswith("/login")


async def test_login_totp_and_issue_protocol(browser: Browser, portal_v1: RunningPortal) -> None:
    codes: list[str] = []

    async def scenario(page: Page) -> None:
        await login(page, portal_v1, codes)
        await page.get_by_role("link", name="Emitir protocolo").click()
        await page.get_by_label("Nome completo").fill("Maria da Silva")
        # Digitado tecla a tecla: a máscara do portal põe pontos e traço.
        await page.get_by_label("CPF").press_sequentially("52998224725")
        await page.get_by_label("UF").select_option("SP")
        await page.get_by_label("Declaro que as informações são verdadeiras").check()
        await page.get_by_role("button", name="Emitir").click()
        await page.get_by_test_id("protocolo").wait_for()

    recording = await record(browser, scenario)
    assert portal_v1.state.protocolos[0]["cpf"] == "529.982.247-25"
    # Nem o nome de usuário que o portal mostra depois do login ("Olá, ...") fica na gravação.
    assert_no_secrets(recording.model_dump_json(), codes)
    assert any(f"Olá, {REDACTED}" in text for text in recording.observed_texts())
    sensitive = [e for e in recording.events if isinstance(e, InputEvent) and e.element.sensitive]
    assert len(sensitive) == 3 and all(e.value is None for e in sensitive)

    result = compile_(recording, "emitir_protocolo_gravado")
    manifest = result.manifest
    assert_no_secrets(manifest.model_dump_json(), codes)
    assert_no_secrets(result.model_dump_json(), codes)
    assert_login_steps(result)

    emitir = manifest.step_by_id("clicar_emitir")
    assert isinstance(emitir, ClickStep)
    assert emitir.effect is Effect.IRREVERSIBLE and emitir.expect_navigation
    assert emitir.target.locators[0].by is LocatorBy.ROLE
    link = manifest.step_by_id("clicar_emitir_protocolo")
    assert isinstance(link, ClickStep) and link.effect is Effect.READ
    assert policy.first_write_step(manifest) == "clicar_emitir"

    cpf = manifest.step_by_id("digitar_cpf")
    assert isinstance(cpf, FillStep) and cpf.sequential
    assert isinstance(cpf.value, TextValue) and cpf.value.template == "529.982.247-25"
    uf = manifest.step_by_id("escolher_uf")
    assert isinstance(uf, SelectStep) and uf.option == "SP"
    assert manifest.step_by_id("marcar_declaro_que_as_informacoes") is not None
    conferir = manifest.steps[-1]
    assert isinstance(conferir, AssertStep)
    assert conferir.condition.model_dump()["text"] == "Protocolo emitido"
    assert [c.model_dump()["text"] for c in manifest.postconditions] == ["Protocolo emitido"]
    assert manifest.allowed_domains == [portal_v1.host]

    # Generalização: nome e CPF viram entradas, sem tocar no resto.
    by_step = {c.step_id: c for c in result.parameter_candidates}
    assert by_step["digitar_nome_completo"].value_preview == "Maria da Silva"
    assert by_step["digitar_cpf"].suggested_type == "cpf"
    edited = apply_edits(
        manifest,
        [
            Parametrize(step_id="digitar_nome_completo", input_name="nome", title="Nome completo"),
            Parametrize(step_id="digitar_cpf", input_name="cpf", title="CPF", type="cpf"),
            AddTextPostcondition(text="Protocolo nº"),
        ],
        observed_texts=result.observed_texts,
    )
    assert edited.rejected == []
    assert {f.name: f.type for f in edited.manifest.inputs} == {
        "nome": FieldType.STRING,
        "cpf": FieldType.CPF,
    }
    assert policy.check(edited.manifest) == []
    nome = edited.manifest.step_by_id("digitar_nome_completo")
    assert isinstance(nome, FillStep) and isinstance(nome.value, TextValue)
    assert nome.value.template == "{{ inputs.nome }}"


async def test_issue_protocol_on_the_changed_layout(
    browser: Browser, portal_v2: RunningPortal
) -> None:
    codes: list[str] = []

    async def scenario(page: Page) -> None:
        await page.goto(f"{portal_v2.base_url}/login")
        await page.get_by_label("Login").fill(DEFAULT_USER)
        await page.get_by_label("Senha").fill(DEFAULT_PASSWORD)
        await page.get_by_role("button", name="Acessar").click()
        code = current_totp()
        codes.append(code)
        await page.get_by_label("Código de verificação").fill(code)
        await page.get_by_label("Código de verificação").press("Enter")
        await page.wait_for_url("**/inicio")
        await page.get_by_role("link", name="Emitir documento").click()
        await page.get_by_label("Nome completo").fill("João Pereira")
        await page.get_by_label("CPF do titular").fill("111.444.777-35")
        await page.get_by_label("UF").select_option("RS")
        await page.get_by_text("Declaro que as informações são verdadeiras").click()
        await page.get_by_role("button", name="Emitir documento").click()
        await page.get_by_test_id("protocolo").wait_for()

    recording = await record(browser, scenario)
    assert portal_v2.state.protocolos[0]["uf"] == "RS"
    result = compile_(recording, "emitir_documento_gravado")
    manifest = result.manifest
    assert_no_secrets(manifest.model_dump_json(), codes)
    secrets_used = [
        s.value.field
        for s in manifest.steps
        if isinstance(s, FillStep) and isinstance(s.value, SecretValue)
    ]
    assert secrets_used == ["username", "password", "totp"]
    effects = {s.id: s.effect for s in manifest.steps}
    assert effects["clicar_acessar"] is Effect.READ
    assert effects["clicar_emitir_documento"] is Effect.READ  # link do menu
    assert effects["clicar_emitir_documento_2"] is Effect.IRREVERSIBLE
    # Clique no texto do rótulo: vira a caixa de seleção, não um clique solto.
    assert [s.kind for s in manifest.steps].count("check") == 1
    # Enter no código: o navegador clica no botão padrão, e é esse clique que fica.
    assert not any(s.kind == "press" for s in manifest.steps)
    confirmar = manifest.step_by_id("clicar_confirmar")
    assert isinstance(confirmar, ClickStep) and confirmar.expect_navigation
    assert confirmar.effect is Effect.READ


async def test_read_notes_and_download_one_file(browser: Browser, portal_v1: RunningPortal) -> None:
    codes: list[str] = []

    async def scenario(page: Page) -> None:
        await login(page, portal_v1, codes, enter=True)
        await page.get_by_role("link", name="Notas fiscais").click()
        await page.get_by_test_id("pagina-atual").wait_for()
        async with page.expect_download() as info:
            row = page.get_by_role("row").filter(has_text="001003")
            await row.get_by_role("link", name="Baixar").click()
        download = await info.value
        assert download.suggested_filename == "nota-001003.pdf"

    recording = await record(browser, scenario)
    assert portal_v1.state.downloads == ["001003"]
    assert_no_secrets(recording.model_dump_json(), codes)
    result = compile_(recording, "baixar_nota_gravada")
    manifest = result.manifest
    assert_no_secrets(manifest.model_dump_json(), codes)
    # Enter no campo de senha: fica só o clique implícito em "Entrar".
    assert_login_steps(result)
    assert not any(s.kind == "press" for s in manifest.steps)

    download = next(s for s in manifest.steps if isinstance(s, DownloadStep))
    assert download.save_as == "nota-001003.pdf"
    assert download.target.within is not None and download.target.within.value == "row"
    assert download.target.within_has_text == "001003"
    assert download.target.locators[0].by is LocatorBy.ROLE
    assert download.target.locators[0].name == "Baixar"
    assert manifest.effect() is Effect.READ
    assert [(o.type, o.source) for o in manifest.outputs] == [
        (FieldType.FILE, f"vars.{download.into}")
    ]
    assert "Notas fiscais emitidas" in result.observed_texts


async def test_upload_a_file(browser: Browser, portal_v1: RunningPortal) -> None:
    codes: list[str] = []

    async def scenario(page: Page) -> None:
        await login(page, portal_v1, codes)
        await page.get_by_role("link", name="Enviar arquivo").click()
        await page.get_by_label("Arquivo").set_input_files(
            files=[{"name": "comprovante.txt", "mimeType": "text/plain", "buffer": b"conteudo"}]
        )
        await page.get_by_role("button", name="Enviar").click()
        await page.get_by_test_id("arquivo-recebido").wait_for()

    recording = await record(browser, scenario)
    assert portal_v1.state.uploads == [{"nome": "comprovante.txt", "tamanho": 8}]
    result = compile_(recording, "enviar_arquivo_gravado")
    manifest = result.manifest
    assert_no_secrets(manifest.model_dump_json(), codes)
    assert_login_steps(result)
    upload = next(s for s in manifest.steps if isinstance(s, UploadStep))
    field = next(f for f in manifest.inputs if f.name == upload.file_input)
    assert field.type is FieldType.FILE
    assert upload.target.locators[0].by is LocatorBy.LABEL
    enviar = manifest.step_by_id("clicar_enviar")
    assert isinstance(enviar, ClickStep) and enviar.effect is Effect.IRREVERSIBLE
    assert isinstance(manifest.steps[-1], AssertStep)


async def test_click_inside_iframe(browser: Browser, portal_v1: RunningPortal) -> None:
    codes: list[str] = []

    async def scenario(page: Page) -> None:
        await login(page, portal_v1, codes)
        await page.goto(f"{portal_v1.base_url}/quadro")
        frame = page.frame_locator("#quadro-consulta")
        await frame.get_by_role("button", name="Confirmar consulta").click()
        await frame.get_by_text("Consulta confirmada").wait_for()

    recording = await record(browser, scenario)
    result = compile_(recording, "consulta_em_quadro")
    manifest = result.manifest
    assert_no_secrets(manifest.model_dump_json(), codes)
    typed = [s for s in manifest.steps if isinstance(s, NavigateStep)]
    assert [s.url.rsplit("/", 1)[-1] for s in typed] == ["login", "quadro"]
    click = manifest.steps[-1]
    assert isinstance(click, ClickStep)
    assert click.target.frames == ["#quadro-consulta"]
    assert click.target.locators[0].by is LocatorBy.ROLE
    assert click.effect is Effect.READ
    assert "Consulta confirmada" in result.observed_texts
    navigations = [e for e in recording.events if isinstance(e, NavigationEvent)]
    assert any(not n.main_frame for n in navigations)
    assert [n.cause for n in navigations if n.main_frame][-1] is NavigationCause.TYPED


async def test_binding_is_hidden_and_recorder_lifecycle(
    browser: Browser, portal_v1: RunningPortal
) -> None:
    context = await browser.new_context()
    try:
        recorder = Recorder()
        await recorder.start(context)
        with pytest.raises(RecorderError):
            await recorder.start(context)
        page = await context.new_page()
        await recorder.goto(page, f"{portal_v1.base_url}/")
        exposed = await page.evaluate(
            "Object.getOwnPropertyNames(window).filter((n) => n.includes('rapa'))"
        )
        assert exposed == []
        await page.get_by_role("link", name="Entrar").click()
        await page.wait_for_url("**/login")
        first = await recorder.stop()
        assert await recorder.stop() is first
        clicks = [e for e in first.events if isinstance(e, ClickEvent)]
        assert [c.element.name for c in clicks] == ["Entrar"]
        assert first.dropped_messages == 0
        # Depois de parar, nada mais entra na gravação.
        await page.get_by_label("Usuário").fill("outra pessoa")
        await page.get_by_role("button", name="Entrar").click()
        assert len((await recorder.stop()).events) == len(first.events)
    finally:
        await context.close()


async def test_recording_starts_on_an_open_page(browser: Browser, portal_v1: RunningPortal) -> None:
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(f"{portal_v1.base_url}/login")
        recorder = Recorder()
        await recorder.start(context)
        await page.get_by_label("Usuário").fill(DEFAULT_USER)
        await page.get_by_label("Senha").fill("senha errada")
        await page.get_by_role("button", name="Entrar").click()
        await page.get_by_role("alert").wait_for()
        recording = await recorder.stop()
    finally:
        await context.close()
    assert "senha errada" not in recording.model_dump_json()
    navigations = [e for e in recording.events if isinstance(e, NavigationEvent)]
    assert [n.cause for n in navigations] == [NavigationCause.INITIAL, NavigationCause.CLICK]
    result = compile_(recording, "tentar_entrar")
    assert [s.kind for s in result.manifest.steps] == ["navigate", "fill", "fill", "click"]
    assert "Usuário ou senha incorretos." in result.observed_texts
