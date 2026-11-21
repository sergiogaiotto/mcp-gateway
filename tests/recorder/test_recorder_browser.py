"""Gravação de demonstrações reais no Chromium contra o portal de teste, e compilação.

A "pessoa" é simulada com ações do Playwright que disparam eventos reais do DOM
(fill, click, select_option, check, set_input_files, press).
"""

import html
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qsl, quote, urlsplit

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright

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
from rapa.llm.generalize import build_user_prompt
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
from tests.recorder.texts import has_text, texts_of

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


def assert_no_secrets(value: object, codes: list[str], *, username: bool = True) -> None:
    """Nenhum segredo nos textos de ``value`` (os números e as datas não contam: um código
    de seis dígitos pode aparecer por acaso num ``t_ms`` ou no ``finished_at``)."""
    for text in texts_of(value):
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
    assert_no_secrets(recording, codes)
    assert any(f"Olá, {REDACTED}" in text for text in recording.observed_texts())
    sensitive = [e for e in recording.events if isinstance(e, InputEvent) and e.element.sensitive]
    assert len(sensitive) == 3 and all(e.value is None for e in sensitive)

    result = compile_(recording, "emitir_protocolo_gravado")
    manifest = result.manifest
    assert_no_secrets(manifest, codes)
    assert_no_secrets(result, codes)
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
    assert_no_secrets(manifest, codes)
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
    assert_no_secrets(recording, codes)
    result = compile_(recording, "baixar_nota_gravada")
    manifest = result.manifest
    assert_no_secrets(manifest, codes)
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
    assert_no_secrets(manifest, codes)
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
    assert_no_secrets(manifest, codes)
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


# -- logins que o portal de teste não tem (páginas servidas pela rota do contexto) --

ROUTED = "https://portal.exemplo.com.br"
SPA_USER = "ana.souza@exemplo.com.br"
SPA_PASSWORD = "S3nh@Forte!"
#: Quatro campos de texto soltos antes do login, que a pessoa não preenche: o
#: usuário continua sendo o usuário (pelo próprio campo).
MANY_FIELDS = (
    '<div><input aria-label="Busca"><input aria-label="Filtro 1">'
    '<input aria-label="Filtro 2"><input aria-label="Filtro 3"></div>'
)


def spa_login(extra: str = "", *, split: bool = False) -> str:
    """Login de SPA: campos soltos numa <div>, sem <form> e sem autocomplete.

    ``split``: a senha fica num ``<form>`` só dela (o script não a vê no grupo do
    usuário, que são os campos soltos).
    """
    secret = '<label for="p">Senha</label><input id="p" name="senha" type="password">'
    if split:
        secret = f'<form onsubmit="return false">{secret}</form>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Entrar</title></head>
<body><h1>Portal</h1>{extra}
<div class="login">
  <label for="u">Usuário</label><input id="u" name="usuario">
  {secret}
  <button id="entrar" type="button">Entrar</button>
</div>
<script>
document.getElementById('entrar').addEventListener('click', () => {{
  const u = encodeURIComponent(document.getElementById('u').value);
  location.href = '/inicio?usuario=' + u + '&quem=' + u;
}});
</script></body></html>"""


def form_login(label: str, name: str, style: str) -> str:
    """Login de banco: a senha é um campo type=text (mascarado por CSS ou não)."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Entrar</title></head>
<body><h1>Banco</h1>
<form action="/entrar" method="post">
  <label for="c">Conta</label><input id="c" name="conta">
  <label for="s">{label}</label>
  <input id="s" name="{name}" type="text" inputmode="numeric" style="{style}">
  <button>Entrar</button>
</form></body></html>"""


async def record_routed(browser: Browser, login_html: str, scenario: Scenario) -> Recording:
    context = await browser.new_context()

    async def handle(route: Route) -> None:
        parts = urlsplit(route.request.url)
        if parts.path == "/login":
            body = login_html
        else:
            quem = html.escape(dict(parse_qsl(parts.query)).get("quem", ""))
            # O cabeçalho em maiúsculas por CSS chega assim pelo innerText.
            body = (
                '<!doctype html><html><head><meta charset="utf-8"><title>Início</title></head>'
                f'<body><header style="text-transform: uppercase">{quem}</header>'
                f"<h1>Início</h1><p>Olá, {quem}</p></body></html>"
            )
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    await context.route("**/*", handle)
    try:
        recorder = Recorder()
        await recorder.start(context)
        page = await context.new_page()
        await recorder.goto(page, f"{ROUTED}/login")
        await scenario(page)
        await page.get_by_role("heading", name="Início").wait_for()
        return await recorder.stop()
    finally:
        await context.close()


def assert_spa_username_hidden(recording: Recording, *, login: bool = True) -> None:
    dumped = recording.model_dump_json()
    for leaked in (SPA_USER, SPA_USER.upper(), quote(SPA_USER, safe=""), SPA_PASSWORD):
        assert leaked not in dumped
    usuario = next(
        e
        for e in recording.events
        if isinstance(e, InputEvent) and e.element.name_attr == "usuario"
    )
    assert usuario.element.is_username and usuario.element.sensitive
    assert usuario.value is None and usuario.typed is None
    assert any(f"Olá, {REDACTED}" in text for text in recording.observed_texts())
    inicio = [e for e in recording.events if isinstance(e, NavigationEvent) and "/inicio" in e.url]
    assert inicio and all(f"usuario={REDACTED}" in e.url for e in inicio)

    result = compile_(recording, "entrar_no_portal")
    fills = [s for s in result.manifest.steps if isinstance(s, FillStep)]
    secrets_used = sorted(s.value.field for s in fills if isinstance(s.value, SecretValue))
    if login:
        assert all(isinstance(s.value, SecretValue) for s in fills)
        assert secrets_used == ["password", "username"]
    else:
        # Fora da regra do login o usuário do cofre não é digitado: o robô pede o valor.
        assert secrets_used == ["password"]
        assert fills[0].value == TextValue(template="{{ inputs.usuario }}")
    assert SPA_USER not in result.model_dump_json()
    assert not any(SPA_USER in text for text in result.observed_texts)
    prompt = build_user_prompt(result.manifest, result.observed_texts)
    assert SPA_USER not in prompt and SPA_USER.upper() not in prompt


async def test_username_of_a_login_without_form_never_reaches_the_recording(
    browser: Browser,
) -> None:
    async def scenario(page: Page) -> None:
        await page.get_by_label("Usuário").fill(SPA_USER)
        await page.get_by_label("Senha").fill(SPA_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()

    assert_spa_username_hidden(await record_routed(browser, spa_login(), scenario))


async def test_page_script_finds_the_username_of_a_login_without_form(browser: Browser) -> None:
    # A senha antes do usuário: o usuário é reconhecido pelo próprio campo, e o
    # login vale em qualquer ordem.
    async def scenario(page: Page) -> None:
        await page.get_by_label("Senha").fill(SPA_PASSWORD)
        await page.get_by_label("Usuário").fill(SPA_USER)
        await page.get_by_role("button", name="Entrar").click()

    assert_spa_username_hidden(await record_routed(browser, spa_login(), scenario))


@pytest.mark.parametrize("variant", ["muitos_campos", "senha_em_outro_form"])
async def test_recorder_hides_the_username_the_page_script_missed(
    browser: Browser, variant: str
) -> None:
    async def scenario(page: Page) -> None:
        await page.get_by_label("Usuário").fill(SPA_USER)
        await page.get_by_label("Senha").fill(SPA_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()

    login_html = spa_login(MANY_FIELDS) if variant == "muitos_campos" else spa_login(split=True)
    assert_spa_username_hidden(await record_routed(browser, login_html, scenario))


async def test_hidden_password_does_not_turn_a_field_without_form_into_username(
    browser: Browser,
) -> None:
    page_html = """<!doctype html><html><head><meta charset="utf-8"><title>Cadastro</title></head>
<body><h1>Cadastro</h1>
<div><label for="n">Nome</label><input id="n" name="nome"></div>
<div style="display:none"><label for="p">Senha</label><input id="p" type="password"></div>
<button type="button" onclick="location.href='/inicio'">Continuar</button>
</body></html>"""

    async def scenario(page: Page) -> None:
        await page.get_by_label("Nome").fill("Ana Lima")
        await page.get_by_role("button", name="Continuar").click()

    recording = await record_routed(browser, page_html, scenario)
    nome = next(e for e in recording.events if isinstance(e, InputEvent))
    assert not nome.element.sensitive and nome.value == "Ana Lima"


@pytest.mark.parametrize(
    ("label", "name", "style"),
    [
        ("Chave pessoal", "chave", "-webkit-text-security: disc"),
        ("Senha", "senha", ""),
        # Assinatura eletrônica é segredo de transação: some da gravação.
        ("Assinatura eletrônica", "assinatura", "-webkit-text-security: disc"),
    ],
    ids=["mascara-css", "rotulo", "assinatura-mascarada"],
)
async def test_password_in_a_text_field_is_never_recorded(
    browser: Browser, label: str, name: str, style: str
) -> None:
    async def scenario(page: Page) -> None:
        await page.get_by_label("Conta").fill("12345-6")
        await page.get_by_label(label).fill(SPA_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()

    recording = await record_routed(browser, form_login(label, name, style), scenario)
    dumped = recording.model_dump_json()
    assert SPA_PASSWORD not in dumped and "12345-6" not in dumped
    inputs = {e.element.name_attr: e for e in recording.events if isinstance(e, InputEvent)}
    assert inputs[name].element.is_password and inputs[name].value is None
    assert inputs["conta"].element.sensitive and inputs["conta"].value is None
    # "Conta" não é usuário pelo próprio campo: sai da gravação sem a marca.
    assert not inputs["conta"].element.is_username

    result = compile_(recording, "entrar_no_banco")
    manifest = result.manifest
    conta, senha = (s for s in manifest.steps if isinstance(s, FillStep))
    # A conta não vira o usuário do cofre: o robô pede o valor numa entrada.
    assert conta.value == TextValue(template="{{ inputs.conta }}")
    assert senha.value == SecretValue(credential="portal", field="password")
    assert [c.kind for c in manifest.credentials] == [CredentialKind.PASSWORD]
    entrar = manifest.step_by_id("clicar_entrar")
    assert isinstance(entrar, ClickStep)
    # Sem um usuário pelo próprio campo e uma senha type=password, o POST grava.
    assert entrar.effect is Effect.WRITE
    assert SPA_PASSWORD not in result.model_dump_json()


_GO_HOME = (
    "const u = encodeURIComponent(document.getElementById('u').value);"
    "location.href = '/inicio?usuario=' + u + '&quem=' + u;"
)


def two_step_login(reveal: str) -> str:
    """Login de SPA em duas etapas: a senha só aparece depois do usuário."""
    show = "document.getElementById('s2').style.display = 'block';"
    if reveal == "continuar":
        user_step = (
            '<label for="u">Usuário</label><input id="u" name="usuario">'
            f'<button type="button" onclick="{show}">Continuar</button>'
        )
    else:
        user_step = (
            '<label for="u">Usuário</label><input id="u" name="usuario" onkeydown="'
            f"if (event.key === 'Enter') {{ {show} document.getElementById('p').focus(); }}\">"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Entrar</title></head>
<body><h1>Portal</h1>
<div>{user_step}</div>
<div id="s2" style="display: none">
  <label for="p">Senha</label><input id="p" name="senha" type="password">
  <button id="entrar" type="button">Entrar</button>
</div>
<script>
document.getElementById('entrar').addEventListener('click', () => {{ {_GO_HOME} }});
</script></body></html>"""


@pytest.mark.parametrize("reveal", ["continuar", "enter"])
async def test_username_of_a_two_step_login_never_reaches_the_recording(
    browser: Browser, reveal: str
) -> None:
    async def scenario(page: Page) -> None:
        await page.get_by_label("Usuário").fill(SPA_USER)
        if reveal == "continuar":
            await page.get_by_role("button", name="Continuar").click()
        else:
            await page.get_by_label("Usuário").press("Enter")
        await page.get_by_label("Senha").fill(SPA_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()

    recording = await record_routed(browser, two_step_login(reveal), scenario)
    # O Enter nunca é a ponte do login em duas etapas: o usuário sai da gravação do
    # mesmo jeito (página com senha), mas o robô pede o valor numa entrada.
    assert_spa_username_hidden(recording, login=reveal == "continuar")


def payment_page(fields: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Pagamento</title></head>
<body><h1>Pagamento de boleto</h1><p>Valor: R$ 1.250,00</p>
<form action="/pagar" method="post">{fields}<button>Confirmar pagamento</button></form>
</body></html>"""


@pytest.mark.parametrize(
    ("fields", "secret_label"),
    [
        (
            '<label for="a">Assinatura eletrônica</label><input id="a" name="assinatura" '
            'type="text" inputmode="numeric" style="-webkit-text-security: disc">',
            "Assinatura eletrônica",
        ),
        (
            '<label for="v">Valor</label><input id="v" name="valor">'
            '<label for="a">PIN</label><input id="a" name="pin" type="tel" maxlength="4">',
            "PIN",
        ),
    ],
    ids=["mascara-css", "valor-e-pin"],
)
async def test_payment_confirmation_with_a_transaction_password_is_irreversible(
    browser: Browser, fields: str, secret_label: str
) -> None:
    async def scenario(page: Page) -> None:
        if secret_label == "PIN":
            await page.get_by_label("Valor").fill("1250,00")
        await page.get_by_label(secret_label).fill("4829")
        await page.get_by_role("button", name="Confirmar pagamento").click()

    recording = await record_routed(browser, payment_page(fields), scenario)
    dumped = recording.model_dump_json()
    # Só nos textos: "4829" aparece por acaso num t_ms ou no finished_at (rodada 7, H3).
    assert not has_text(recording, "4829")
    result = compile_(recording, "pagar_boleto")
    manifest = result.manifest
    confirmar = manifest.step_by_id("clicar_confirmar_pagamento")
    assert isinstance(confirmar, ClickStep) and confirmar.effect is Effect.IRREVERSIBLE
    assert manifest.effect() is Effect.IRREVERSIBLE
    fills = {s.id: s for s in manifest.steps if isinstance(s, FillStep)}
    if secret_label == "PIN":
        # O valor do pagamento nunca vira o usuário do cofre: vira uma entrada.
        valor = fills["digitar_valor"]
        assert valor.value == TextValue(template="{{ inputs.valor }}")
        assert "1250,00" not in dumped
        usuario = next(e for e in recording.events if isinstance(e, InputEvent))
        assert usuario.element.sensitive and not usuario.element.is_username
    assert not has_text(result, "4829")


async def test_page_script_and_recorder_agree_that_a_username_named_like_a_password_is_ambiguous(
    browser: Browser,
) -> None:
    page_html = """<!doctype html><html><head><meta charset="utf-8"><title>Entrar</title></head>
<body><h1 id="t">Entre com seu CPF e senha</h1>
<form action="/entrar" method="post">
  <label for="cpf">CPF</label><input id="cpf" name="cpf" aria-labelledby="t">
  <label for="s">Senha</label><input id="s" name="senha" type="password">
  <button>Entrar</button>
</form></body></html>"""

    async def scenario(page: Page) -> None:
        await page.locator("#cpf").fill("12345678900")
        await page.locator("#s").fill(SPA_PASSWORD)
        await page.get_by_role("button", name="Entrar").click()

    recording = await record_routed(browser, page_html, scenario)
    inputs = {e.element.name_attr: e for e in recording.events if isinstance(e, InputEvent)}
    # O rótulo diz CPF (usuário) e o aria-labelledby fala em senha: o campo é ambíguo.
    cpf_event = inputs["cpf"]
    assert cpf_event.element.sensitive and cpf_event.element.aria_mentions_password
    assert not cpf_event.element.is_username and not cpf_event.element.is_password
    assert not has_text(recording, "12345678900")
    result = compile_(recording, "entrar_com_cpf")
    cpf, senha = (s for s in result.manifest.steps if isinstance(s, FillStep))
    # Nunca credencial: o login grava e o robô pede o valor numa entrada.
    assert isinstance(cpf.value, TextValue) and cpf.value.template.startswith("{{ inputs.")
    assert senha.value == SecretValue(credential="portal", field="password")
    entrar = result.manifest.step_by_id("clicar_entrar")
    assert isinstance(entrar, ClickStep) and entrar.effect is Effect.WRITE
    assert any("fala em senha" in w for w in result.warnings)
    assert not has_text(result, "12345678900")


@pytest.mark.parametrize("leave", ["tab", "clique"])
async def test_autosave_while_typing_makes_the_field_write(browser: Browser, leave: str) -> None:
    page_html = """<!doctype html><html><head><meta charset="utf-8"><title>Cadastro</title></head>
<body><h1>Cadastro</h1>
<label for="o">Observação</label>
<input id="o" name="obs" oninput="clearTimeout(window.t); window.t = setTimeout(
  () => fetch('/api/rascunho', {method: 'POST', body: this.value}), 100)">
<label for="n">Nome</label><input id="n" name="nome">
<button type="button" onclick="location.href = '/inicio'">Voltar</button>
</body></html>"""

    async def scenario(page: Page) -> None:
        await page.get_by_label("Observação").click()
        await page.keyboard.type("abc")
        await page.wait_for_timeout(500)  # o rascunho é salvo com o campo ainda em foco
        if leave == "tab":
            await page.keyboard.press("Tab")
        else:
            await page.get_by_label("Nome").click()
        await page.get_by_role("button", name="Voltar").click()

    recording = await record_routed(browser, page_html, scenario)
    result = compile_(recording, "cadastrar")
    observacao = result.manifest.step_by_id("digitar_observacao")
    assert observacao is not None and observacao.effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


async def test_row_key_is_the_text_the_runner_compares(browser: Browser) -> None:
    page_html = """<!doctype html><html><head><meta charset="utf-8"><title>Notas</title></head>
<body><h1>Notas</h1>
<table>
  <tr><th>Nota</th><th>Ação</th></tr>
  <tr><td style="text-transform: uppercase">Nota 12</td>
      <td><button type="button" onclick="location.href = '/inicio'">Abrir</button></td></tr>
  <tr><td style="text-transform: uppercase">Nota 120</td>
      <td><button type="button" onclick="location.href = '/inicio'">Abrir</button></td></tr>
</table></body></html>"""

    async def scenario(page: Page) -> None:
        row = page.get_by_role("row").filter(has=page.get_by_text("Nota 12", exact=True))
        await row.get_by_role("button", name="Abrir").click()

    recording = await record_routed(browser, page_html, scenario)
    result = compile_(recording, "abrir_nota")
    abrir = result.manifest.step_by_id("clicar_abrir")
    assert isinstance(abrir, ClickStep)
    assert abrir.target.within is not None and abrir.target.within.value == "row"
    key = abrir.target.within_has_text
    # O texto da célula (textContent), não o que o CSS mostra ("NOTA 12").
    assert key == "Nota 12"
    # O executor compara o texto inteiro do elemento, com maiúsculas: uma linha só.
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.set_content(page_html)
        rows = page.get_by_role("row").filter(has=page.get_by_text(key, exact=True))
        assert await rows.count() == 1
    finally:
        await context.close()
