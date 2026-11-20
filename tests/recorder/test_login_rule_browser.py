"""Regra conservadora do login gravada num Chromium de verdade, e compilada.

As páginas são servidas pela rota do contexto: transações com senha que não são
login, logins em duas etapas de SPA, o botão de mostrar a senha, a limpeza dos
valores sigilosos e o campo que só o nome acessível diz ser senha.
"""

import html
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright

from rapa.compiler import CompileResult, lower
from rapa.core import policy
from rapa.core.robot import ClickStep, Effect, FillStep, Manifest, SecretValue, TextValue
from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.llm.generalize import build_user_prompt
from rapa.recorder import InputEvent, Recorder, Recording
from rapa.recorder.sensitivity import username_inputs

pytestmark = pytest.mark.browser

type Scenario = Callable[[Page], Awaitable[None]]

SITE = "https://banco.exemplo.com.br"
#: Página que o site devolve depois de um envio (POST ou /inicio).
DONE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Pronto</title></head>'
    "<body><h1>Comprovante</h1><p>Olá, {quem}</p></body></html>"
)


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


async def record_site(
    browser: Browser,
    pages: dict[str, str],
    start: str,
    scenario: Scenario,
    *,
    redirects: dict[str, str] | None = None,
) -> Recording:
    """Grava ``scenario`` num site servido pela rota do contexto.

    GET de um caminho de ``pages`` devolve a página; ``/api/...`` devolve JSON
    vazio; POST de um caminho de ``redirects`` redireciona (303); o resto devolve
    ``DONE``, com o parâmetro ``quem`` do endereço.
    """
    context = await browser.new_context()

    async def handle(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if parts.path.startswith("/api/"):
            await route.fulfill(status=200, content_type="application/json", body="{}")
            return
        if request.method == "POST" and parts.path in (redirects or {}):
            await route.fulfill(status=303, headers={"location": (redirects or {})[parts.path]})
            return
        body = pages.get(parts.path) if request.method == "GET" else None
        if body is None:
            body = DONE.format(quem=html.escape(dict(parse_qsl(parts.query)).get("quem", "")))
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    await context.route("**/*", handle)
    try:
        recorder = Recorder()
        await recorder.start(context)
        page = await context.new_page()
        await recorder.goto(page, f"{SITE}{start}")
        await scenario(page)
        return await recorder.stop()
    finally:
        await context.close()


def compile_(recording: Recording) -> CompileResult:
    result = lower(recording, name="robo_gravado", title="Robô gravado", description="Teste.")
    assert policy.check(Manifest.model_validate(result.manifest.model_dump(mode="json"))) == []
    return result


def usernames_in(result: CompileResult) -> list[str]:
    return [
        s.id
        for s in result.manifest.steps
        if isinstance(s, FillStep)
        and isinstance(s.value, SecretValue)
        and s.value.field == "username"
    ]


def fill_value(result: CompileResult, step_id: str) -> object:
    found = result.manifest.step_by_id(step_id)
    assert isinstance(found, FillStep), [s.id for s in result.manifest.steps]
    return found.value


def click_effect(result: CompileResult, step_id: str) -> Effect:
    found = result.manifest.step_by_id(step_id)
    assert isinstance(found, ClickStep), [s.id for s in result.manifest.steps]
    return found.effect


def page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>'
        f"<body>{body}</body></html>"
    )


# -- transação com senha comum e um botão que não é de login ----------------------


@pytest.mark.parametrize(
    ("field", "button"),
    [
        ("Chave Pix", "Confirmar"),
        ("Valor", "Confirmar transação"),
        ("Conta destino", "Confirmar compra"),
        ("Valor", "OK"),
        ("Valor", "Continuar para o pagamento"),
    ],
)
async def test_a_transaction_with_a_password_is_never_a_login(
    browser: Browser, field: str, button: str
) -> None:
    pix = page(
        "Pix",
        f"""<h1>Transferência Pix</h1>
<form action="/pix" method="post">
  <label for="v">{field}</label><input id="v" name="campo">
  <label for="s">Senha</label><input id="s" name="senha" type="password">
  <button>{button}</button>
</form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label(field).fill("12345678909")
        await p.get_by_label("Senha").fill("s3nh4")
        await p.get_by_role("button", name=button).click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    result = compile_(recording)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    # O usuário do cofre nunca é digitado no campo da operação; o valor, que o
    # script tirou da gravação, vira uma entrada obrigatória.
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert "12345678909" not in recording.model_dump_json()
    assert "12345678909" not in result.model_dump_json()


async def test_the_show_password_button_is_not_the_submit(browser: Browser) -> None:
    pagar = page(
        "Pagamento",
        """<h1>Pagamento</h1>
<form action="/pagar" method="post">
  <label for="v">Valor</label><input id="v" name="valor">
  <label for="s">Senha</label><input id="s" name="senha" type="password">
  <button type="button" onclick="var s = document.getElementById('s');
    s.type = s.type === 'password' ? 'text' : 'password'; s.type = 'password';">Mostrar senha</button>
  <button>Confirmar pagamento</button>
</form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_label("Senha", exact=True).fill("s3nh4")
        await p.get_by_role("button", name="Mostrar senha").click()
        await p.get_by_role("button", name="Confirmar pagamento").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/pagar": pagar}, "/pagar", scenario)
    valor = next(e for e in recording.events if isinstance(e, InputEvent))
    assert valor.element.sensitive and not valor.element.is_username
    result = compile_(recording)
    assert fill_value(result, "digitar_valor") == TextValue(template="{{ inputs.valor }}")
    assert [f.name for f in result.manifest.inputs] == ["valor"]
    assert usernames_in(result) == []
    assert click_effect(result, "clicar_confirmar_pagamento") is Effect.IRREVERSIBLE
    assert result.manifest.effect() is Effect.IRREVERSIBLE


# -- login em duas etapas de SPA ------------------------------------------------------

USER = "ana.souza"
PASSWORD = "S3nh@Forte"


def two_step(variant: str) -> str:
    show = "document.getElementById('s2').style.display = 'block';"
    if variant == "pushstate":
        show += "history.pushState({}, '', '/login/senha');"
    if variant == "hash":
        show += "location.hash = 'senha';"
    before = (
        '<label><input type="radio" name="tipo" value="pf"> Pessoa física</label>'
        '<label><input type="radio" name="tipo" value="pj"> Pessoa jurídica</label>'
        if variant == "radio"
        else ""
    )
    after = (
        '<label><input type="checkbox" name="lembrar"> Lembrar meu usuário</label>'
        if variant == "lembrar"
        else ""
    )
    go = "location.href = '/inicio?quem=' + encodeURIComponent(document.getElementById('u').value)"
    return page(
        "Entrar",
        f"""<h1>Portal</h1>
<div>{before}<label for="u">Usuário</label><input id="u" name="usuario">{after}
<button type="button" onclick="{show}">Continuar</button></div>
<div id="s2" style="display: none">
  <label for="p">Senha</label><input id="p" name="senha" type="password">
  <button type="button" onclick="{go}">Entrar</button>
</div>""",
    )


@pytest.mark.parametrize("variant", ["pushstate", "hash", "radio", "lembrar"])
async def test_two_step_login_variants_hide_the_username(browser: Browser, variant: str) -> None:
    async def scenario(p: Page) -> None:
        if variant == "radio":
            await p.get_by_label("Pessoa física").check()
        await p.get_by_label("Usuário", exact=True).fill(USER)
        if variant == "lembrar":
            await p.get_by_label("Lembrar meu usuário").check()
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/login": two_step(variant)}, "/login", scenario)
    dumped = recording.model_dump_json()
    assert USER not in dumped and USER.upper() not in dumped and PASSWORD not in dumped
    usuario = next(
        e
        for e in recording.events
        if isinstance(e, InputEvent) and e.element.name_attr == "usuario"
    )
    assert usuario.element.is_username and usuario.value is None
    result = compile_(recording)
    assert usernames_in(result) == ["digitar_usuario"]
    assert click_effect(result, "clicar_entrar") is Effect.READ
    assert USER not in result.model_dump_json()
    assert USER not in build_user_prompt(result.manifest, result.observed_texts)


@pytest.mark.parametrize("with_form", [True, False], ids=["formulario", "sem_formulario"])
async def test_a_payment_wizard_is_not_a_two_step_login(browser: Browser, with_form: bool) -> None:
    send = (
        ""
        if with_form
        else """<script>document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault();
  fetch('/api/pix', {method: 'POST', body: document.getElementById('v').value})
    .then(() => document.body.insertAdjacentHTML('beforeend', '<h2>Pix enviado</h2>'));
});</script>"""
    )
    amount = (
        '<div><label for="v">Valor</label><input id="v" name="valor">'
        "<button type=\"button\" onclick=\"document.getElementById('s2').style.display='block'\">"
        "Continuar</button></div>"
    )
    password = (
        '<label for="s">Senha</label><input id="s" name="senha" type="password">'
        "<button>Confirmar</button>"
    )
    if with_form:
        body = (
            f'<form id="f" action="/pix" method="post">{amount}'
            f'<div id="s2" style="display: none">{password}</div></form>'
        )
    else:
        body = f'{amount}<div id="s2" style="display: none"><form id="f">{password}</form></div>'
    pix = page("Pix", f"<h1>Transferência Pix</h1>{body}{send}")

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Confirmar").click()
        done = "Comprovante" if with_form else "Pix enviado"
        await p.get_by_role("heading", name=done).wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    assert usernames_in(result) == []
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    valor = fill_value(result, "digitar_valor")
    if with_form:
        # O script tirou o valor (campo antes da senha no formulário): vira entrada.
        assert valor == TextValue(template="{{ inputs.valor }}")
    else:
        assert valor == TextValue(template="1250,00")


async def test_a_search_before_a_password_modal_is_not_the_username(browser: Browser) -> None:
    notas = page(
        "Notas",
        """<h1>Notas fiscais</h1>
<label for="q">Buscar nota</label><input id="q" name="q">
<button type="button" onclick="fetch('/api/notas?q=' + encodeURIComponent(
  document.getElementById('q').value)).then(() => {
  document.getElementById('r').style.display = 'block'; })">Buscar</button>
<div id="r" style="display: none"><p>Nota 123</p>
<button type="button" onclick="document.getElementById('m').style.display = 'block'">Editar</button>
</div>
<div id="m" style="display: none" role="dialog" aria-label="Confirme sua senha">
<form id="f"><label for="s">Senha</label><input id="s" name="senha" type="password">
<button>Confirmar</button></form></div>
<script>document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault();
  fetch('/api/notas/123/liberar', {method: 'POST'}).then(() => {
    document.body.insertAdjacentHTML('beforeend', '<h2>Nota liberada</h2>'); });
});</script>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Buscar nota").fill("Nota 123")
        await p.get_by_role("button", name="Buscar").click()
        await p.get_by_role("button", name="Editar").click()
        await p.get_by_label("Senha", exact=True).fill(PASSWORD)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_role("heading", name="Nota liberada").wait_for()

    recording = await record_site(browser, {"/notas": notas}, "/notas", scenario)
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    assert fill_value(result, "digitar_buscar_nota") == TextValue(template="Nota 123")
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


# -- limpeza dos valores sigilosos e senha pelo nome acessível -----------------------


async def test_scrub_keeps_names_and_values_that_only_share_letters_with_the_username(
    browser: Browser,
) -> None:
    login = page(
        "Entrar",
        """<h1>ERP</h1><form action="/entrar" method="post">
<label for="u">Usuário</label><input id="u" name="usuario">
<label for="p">Senha</label><input id="p" name="senha" type="password">
<button>Entrar</button></form>""",
    )
    inicio = page(
        "Início",
        '<h1>Painel</h1><nav><a href="/contas">Financeiro</a> <a href="/compras">Compras</a></nav>',
    )
    contas = page(
        "Contas",
        """<h1>Contas a pagar</h1><form action="/salvar" method="post">
<label for="d">Departamento</label><input id="d" name="dep">
<label for="e">Equipe</label><input id="e" name="equipe">
<button>Salvar</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill("financeiro")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Painel").wait_for()
        await p.get_by_role("link", name="Financeiro").click()
        await p.get_by_role("heading", name="Contas a pagar").wait_for()
        await p.get_by_label("Departamento").fill("Financeiro Corporativo")
        await p.get_by_label("Equipe").fill("equipe financeiro")
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(
        browser,
        {"/login": login, "/inicio": inicio, "/contas": contas},
        "/login",
        scenario,
        redirects={"/entrar": "/inicio"},
    )
    result = compile_(recording)
    menu = result.manifest.step_by_id("clicar_financeiro")
    assert isinstance(menu, ClickStep)
    assert [loc.name for loc in menu.target.locators if loc.name] == ["Financeiro"]
    assert fill_value(result, "digitar_departamento") == TextValue(
        template="Financeiro Corporativo"
    )
    # O valor digitado tinha o usuário exato: vira entrada, nunca "REMOVIDO" no sistema.
    assert fill_value(result, "digitar_equipe") == TextValue(template="{{ inputs.equipe }}")
    assert any("Equipe" in w and "sensível" in w for w in result.warnings)
    assert click_effect(result, "clicar_salvar") is Effect.WRITE
    assert "REMOVIDO" not in result.manifest.model_dump_json()


async def test_a_field_named_like_a_password_never_reaches_the_recording(
    browser: Browser,
) -> None:
    entrar = page(
        "Entrar",
        """<h1>Acesso</h1>
<form action="/entrar" method="post">
  <label for="u">CPF</label><input id="u" name="cpf">
  <p id="i">Digite a senha de 6 dígitos</p>
  <label for="k">Código pessoal</label>
  <input id="k" name="cod" inputmode="numeric" aria-labelledby="i">
  <button>Entrar</button>
</form>""",
    )

    async def scenario(p: Page) -> None:
        await p.locator("#u").fill("12345678909")
        await p.locator("#k").fill("918273")
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/entrar": entrar}, "/entrar", scenario)
    dumped = recording.model_dump_json()
    assert "918273" not in dumped and "12345678909" not in dumped
    inputs = {e.element.name_attr: e for e in recording.events if isinstance(e, InputEvent)}
    assert inputs["cod"].element.sensitive and not inputs["cod"].element.is_password
    result = compile_(recording)
    cpf, cod = (s for s in result.manifest.steps if isinstance(s, FillStep))
    assert cpf.value == SecretValue(credential="banco", field="username")
    assert cod.value == SecretValue(credential="banco", field="password")
    assert click_effect(result, "clicar_entrar") is Effect.READ
