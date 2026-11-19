"""Regra do login gravada num Chromium de verdade, e compilada.

As páginas são servidas pela rota do contexto: transações com senha que não são
login (inclusive com o Enter, um "Continuar" de SPA, um código de transação, o
"Confirmar" do formulário do código ou do token depois do login, o CPF do
favorecido com o código do SMS e o cadastro com senha nova), logins em duas
etapas de SPA e em duas páginas, o botão de mostrar a senha, campos em shadow
roots, a limpeza dos valores sigilosos (também no texto e no nome dos elementos)
e o campo que só o ``aria-label`` ou o ``aria-labelledby`` diz ser senha.
"""

import html
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright

from rapa.compiler import CompileResult, lower
from rapa.core import policy
from rapa.core.robot import (
    ClickStep,
    Effect,
    FillStep,
    Manifest,
    PressStep,
    SecretValue,
    TextValue,
)
from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.llm.generalize import build_user_prompt
from rapa.recorder import REDACTED, InputEvent, Recorder, Recording
from tests.recorder.texts import has_text

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


def an_input(name: str) -> TextValue:
    return TextValue(template=f"{{{{ inputs.{name} }}}}")


def input_of(recording: Recording, name_attr: str) -> InputEvent:
    return next(
        e
        for e in recording.events
        if isinstance(e, InputEvent) and e.element.name_attr == name_attr
    )


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
    assert not has_text(recording, "12345678909")
    assert not has_text(result, "12345678909")


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
    valor_event = input_of(recording, "valor")
    assert valor_event.element.sensitive and not valor_event.element.is_username
    assert "1250,00" not in recording.model_dump_json()
    result = compile_(recording)
    assert usernames_in(result) == []
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    # O valor estava numa página com senha (o script tira no formulário; o gravador,
    # ao fechar, quando a senha só aparece depois): vira uma entrada.
    assert fill_value(result, "digitar_valor") == an_input("valor")


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
    busca = input_of(recording, "q")
    assert busca.element.sensitive and not busca.element.is_username and busca.value is None
    result = compile_(recording)
    # Na mesma página da senha: a busca vira uma entrada, nunca o usuário do cofre.
    assert fill_value(result, "digitar_buscar_nota") == an_input("buscar_nota")
    assert usernames_in(result) == []
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


# -- limpeza dos valores sigilosos e senha pelo nome acessível -----------------------


async def test_scrub_drops_names_in_any_letter_case_but_keeps_typed_values(
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
    # O nome do menu ("Financeiro") é o usuário em outra caixa: sai da gravação e não
    # vira localizador; o clique fica com o caminho CSS gravado.
    menu = next(
        s
        for s in result.manifest.steps
        if isinstance(s, ClickStep) and s.target.locators[0].by.value == "css"
    )
    assert [loc.name for loc in menu.target.locators if loc.name] == []
    assert all("REMOVIDO" not in loc.value for loc in menu.target.locators)
    assert "Financeiro" not in recording.model_dump_json().replace("Financeiro Corporativo", "")
    # Valores digitados só perdem o valor exato.
    assert fill_value(result, "digitar_departamento") == TextValue(
        template="Financeiro Corporativo"
    )
    # O valor digitado tinha o usuário exato: vira entrada, nunca "REMOVIDO" no sistema.
    assert fill_value(result, "digitar_equipe") == TextValue(template="{{ inputs.equipe }}")
    assert any("Equipe" in w and "sensível" in w for w in result.warnings)
    assert click_effect(result, "clicar_salvar") is Effect.WRITE
    assert (
        "financeiro"
        not in result.manifest.model_dump_json().replace("Financeiro Corporativo", "").lower()
    )


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
    assert not has_text(recording, "918273") and not has_text(recording, "12345678909")
    inputs = {e.element.name_attr: e for e in recording.events if isinstance(e, InputEvent)}
    assert inputs["cod"].element.sensitive and not inputs["cod"].element.is_password
    assert inputs["cpf"].element.is_username
    result = compile_(recording)
    # Não é uma senha type=password: não é login; os dois valores viram entradas.
    cpf, cod = (s for s in result.manifest.steps if isinstance(s, FillStep))
    assert cpf.value == an_input("cpf")
    assert isinstance(cod.value, TextValue) and cod.value.template.startswith("{{ inputs.")
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    assert result.manifest.credentials == []
    assert not has_text(result, "918273")


async def test_an_aria_label_of_a_password_hides_the_field_beside_aria_labelledby(
    browser: Browser,
) -> None:
    entrar = page(
        "Entrar",
        """<h1>Portal</h1><form action="/entrar" method="post">
<label for="cpf">CPF</label><input id="cpf" name="cpf">
<span id="t">Código pessoal</span>
<input id="k" name="cod" aria-labelledby="t" aria-label="Senha de 6 dígitos">
<button>Entrar</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.locator("#cpf").fill("12345678909")
        await p.locator("#k").fill("918273")
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/entrar": entrar}, "/entrar", scenario)
    assert not has_text(recording, "918273") and not has_text(recording, "12345678909")
    cod = input_of(recording, "cod")
    assert cod.element.aria_mentions_password and cod.element.name == "Código pessoal"
    assert cod.element.sensitive and not cod.element.is_password
    result = compile_(recording)
    assert fill_value(result, "digitar_cpf") == an_input("cpf")
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    prompt = build_user_prompt(result.manifest, result.observed_texts)
    assert not has_text(result, "918273") and "918273" not in prompt
    assert "12345678909" not in prompt


# -- o Enter, um "Continuar" de SPA e códigos de transação não são login -----------

#: A página paga (fetch POST) e mostra "Pix enviado".
_PAY = (
    "fetch('/api/pix', {method: 'POST', body: 'x'}).then(() => {"
    "document.getElementById('ok').textContent = 'Pix enviado'; })"
)


@pytest.mark.parametrize("with_form", [False, True], ids=["sem_formulario", "formulario"])
async def test_an_enter_in_the_password_of_a_transaction_is_write(
    browser: Browser, with_form: bool
) -> None:
    fields = f"""<label for="v">Valor</label><input id="v" name="valor">
<label for="s">Senha</label><input id="s" name="senha" type="password"
  onkeydown="if (event.key === 'Enter') {{ event.preventDefault(); {_PAY} }}">
<button type="button" onclick="{_PAY}">Pagar</button><p id="ok"></p>"""
    body = f'<form onsubmit="return false">{fields}</form>' if with_form else f"<div>{fields}</div>"
    pix = page("Pix", f"<h1>Pix</h1>{body}")

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_label("Senha").press("Enter")
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    assert "1250,00" not in recording.model_dump_json()
    result = compile_(recording)
    press = next(s for s in result.manifest.steps if isinstance(s, PressStep))
    assert press.effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_valor") == an_input("valor")


async def test_a_continue_button_of_a_spa_after_a_value_and_a_password_is_write(
    browser: Browser,
) -> None:
    pix = page(
        "Pix",
        f"""<h1>Pix</h1><div>
<label for="v">Valor</label><input id="v" name="valor">
<label for="s">Senha</label><input id="s" name="senha" type="password">
<button type="button" onclick="{_PAY}">Continuar</button><p id="ok"></p></div>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    result = compile_(recording)
    assert click_effect(result, "clicar_continuar") is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert any("página que pede senha ou código" in w for w in result.warnings)


@pytest.mark.parametrize("button", ["Continuar", "Entrar"])
async def test_a_transaction_token_never_makes_a_login(browser: Browser, button: str) -> None:
    pix = page(
        "Pix",
        f"""<h1>Pix</h1><form action="/pix" method="post">
<label for="v">Valor</label><input id="v" name="valor">
<label for="s">Senha</label><input id="s" name="senha" type="password">
<label for="t">Token da transação</label><input id="t" name="token" autocomplete="one-time-code">
<button>{button}</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_label("Token da transação").fill("123456")
        await p.get_by_role("button", name=button).click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    result = compile_(recording)
    assert click_effect(result, f"clicar_{button.lower()}") is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_valor") == an_input("valor")


async def test_confirm_in_the_form_of_the_code_after_a_value_and_a_password_is_write(
    browser: Browser,
) -> None:
    pix = page(
        "Pix",
        f"""<h1>Pix</h1>
<form id="a" onsubmit="return false"><label for="v">Valor</label><input id="v" name="valor">
<label for="s">Senha</label><input id="s" name="senha" type="password"></form>
<form id="b" onsubmit="{_PAY}; return false"><label for="c">Código de verificação</label>
<input id="c" name="codigo" autocomplete="one-time-code"><button>Confirmar</button></form>
<p id="ok"></p>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Valor").fill("1250,00")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_label("Código de verificação").fill("123456")
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    result = compile_(recording)
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_valor") == an_input("valor")


async def test_a_security_code_and_confirm_is_write(browser: Browser) -> None:
    pagar = page(
        "Pagamento",
        f"""<h1>Pagar com o cartão final 4242</h1><p>Total: R$ 1.250,00</p>
<form onsubmit="{_PAY}; return false"><label for="cvv">Código de segurança</label>
<input id="cvv" name="cvv" inputmode="numeric"><button>Confirmar</button></form>
<p id="ok"></p>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Código de segurança").fill("737")
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pagar": pagar}, "/pagar", scenario)
    result = compile_(recording)
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


async def test_an_enter_never_bridges_a_two_step_login(browser: Browser) -> None:
    show = "document.getElementById('s2').style.display = 'block'"
    pix = page(
        "Pix",
        f"""<h1>Pix</h1><div>
<label for="c">Chave Pix</label><input id="c" name="chave"
  onkeydown="if (event.key === 'Enter') {{ {show} }}"></div>
<div id="s2" style="display: none"><label for="s">Senha</label>
<input id="s" name="senha" type="password">
<button type="button" onclick="{_PAY}">Entrar</button><p id="ok"></p></div>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Chave Pix").fill("ana@exemplo.com.br")
        await p.get_by_label("Chave Pix").press("Enter")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pix": pix}, "/pix", scenario)
    chave = input_of(recording, "chave")
    assert chave.element.sensitive and not chave.element.is_username and chave.value is None
    result = compile_(recording)
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_chave_pix") == an_input("chave_pix")
    assert "ana@exemplo.com.br" not in result.model_dump_json()


async def test_an_enter_in_the_password_of_a_login_form_clicks_the_login_button(
    browser: Browser,
) -> None:
    login = page(
        "Entrar",
        """<h1>Portal</h1><form action="/login" method="post">
<label for="u">Usuário</label><input id="u" name="usuario">
<label for="p">Senha</label><input id="p" name="senha" type="password">
<button>Entrar</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_label("Senha").press("Enter")
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(
        browser, {"/login": login}, "/login", scenario, redirects={"/login": "/inicio"}
    )
    assert USER not in recording.model_dump_json()
    result = compile_(recording)
    assert not any(isinstance(s, PressStep) for s in result.manifest.steps)
    assert click_effect(result, "clicar_entrar") is Effect.READ
    assert usernames_in(result) == ["digitar_usuario"]


async def test_scrub_ignores_letter_case_in_the_text_of_elements_and_rows(
    browser: Browser,
) -> None:
    login = page(
        "Entrar",
        """<h1>Portal</h1><form action="/login" method="post">
<label for="u">Usuário</label><input id="u" name="usuario">
<label for="p">Senha</label><input id="p" name="senha" type="password">
<button>Entrar</button></form>""",
    )
    inicio = page(
        "Início",
        """<header><a href="/perfil" style="text-transform: uppercase">ana.souza</a></header>
<h1>Início</h1>
<table><tr><th>Responsável</th><th>Documento</th><th></th></tr>
<tr><td style="text-transform: uppercase">ana.souza</td><td>NF 10</td>
<td><button type="button" onclick="this.textContent = 'Aberto'">Abrir</button></td></tr>
<tr><td style="text-transform: uppercase">bruno.lima</td><td>NF 11</td>
<td><button type="button">Abrir</button></td></tr></table>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()
        await p.get_by_role("row").nth(1).get_by_role("button", name="Abrir").click()
        await p.get_by_role("button", name="Aberto").wait_for()
        await p.get_by_role("link").first.click()
        await p.get_by_role("heading", name="Perfil").wait_for()

    recording = await record_site(
        browser,
        {"/login": login, "/inicio": inicio, "/perfil": page("Perfil", "<h1>Perfil</h1>")},
        "/login",
        scenario,
        redirects={"/login": "/inicio"},
    )
    dumped = recording.model_dump_json()
    assert USER not in dumped and USER.upper() not in dumped
    result = compile_(recording)
    manifest = result.manifest.model_dump_json()
    assert USER not in manifest and USER.upper() not in manifest
    abrir = result.manifest.step_by_id("clicar_abrir")
    assert isinstance(abrir, ClickStep) and abrir.target.snapshot is not None
    assert "NF 10" in abrir.target.snapshot and USER.upper() not in abrir.target.snapshot
    prompt = build_user_prompt(result.manifest, result.observed_texts)
    assert USER not in prompt and USER.upper() not in prompt


# -- rodada 6: campo ambíguo, shadow roots, nomes, token, favorecido, cadastro -------


def dumps(result: CompileResult) -> tuple[str, str]:
    """O manifesto e o prompt do modelo de linguagem, como texto."""
    return result.manifest.model_dump_json(), build_user_prompt(
        result.manifest, result.observed_texts
    )


@pytest.mark.parametrize("variant", ["cpf_aria_labelledby", "usuario_aria_label"])
async def test_a_username_named_like_a_password_never_reaches_the_recording(
    browser: Browser, variant: str
) -> None:
    # F1: rótulo "CPF" e aria-labelledby "Digite a senha numérica", sozinho num form
    # com "Entrar". F2: rótulo "Usuário" e aria-label "Senha do usuário", sem form.
    if variant == "cpf_aria_labelledby":
        body = """<h1>Acesso</h1><span id="h">Digite a senha numérica</span>
<form action="/login" method="post"><label for="c">CPF</label>
<input id="c" name="cpf" aria-labelledby="h"><button>Entrar</button></form>"""
        value = "918273"
    else:
        body = """<h1>Acesso</h1>
<label for="c">Usuário</label><input id="c" name="usuario" aria-label="Senha do usuário">
<button onclick="fetch('/api/login', {method: 'POST', body: '{}'}).then(() => {
  document.body.insertAdjacentHTML('beforeend', '<h2>Pronto</h2>'); })">Entrar</button>"""
        value = "S3nhaForte"

    async def scenario(p: Page) -> None:
        await p.locator("#c").fill(value)
        await p.get_by_role("button", name="Entrar").click()
        if variant == "cpf_aria_labelledby":
            await p.get_by_role("heading", name="Comprovante").wait_for()
        else:
            await p.get_by_role("heading", name="Pronto").wait_for()

    recording = await record_site(browser, {"/login": page("Acesso", body)}, "/login", scenario)
    assert value not in recording.model_dump_json()
    field = next(e for e in recording.events if isinstance(e, InputEvent))
    assert field.element.sensitive and field.element.aria_mentions_password
    assert not field.element.is_username and not field.element.is_password
    result = compile_(recording)
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    assert usernames_in(result) == [] and result.manifest.credentials == []
    (fill,) = (s for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(fill.value, TextValue) and fill.value.template.startswith("{{ inputs.")
    assert any("fala em senha" in w for w in result.warnings)
    manifest, prompt = dumps(result)
    assert value not in manifest and value not in prompt


SHADOW_LOGIN = page(
    "Login",
    """<h1>Login</h1>{user}<x-pass></x-pass>
<button id="b">Entrar</button>
<script>
customElements.define('x-user', class extends HTMLElement { constructor() { super();
  this.attachShadow({mode: 'open'}).innerHTML =
    '<label for="u">Usuário</label><input id="u" name="usuario">'; } });
customElements.define('x-pass', class extends HTMLElement { constructor() { super();
  this.attachShadow({mode: 'open'}).innerHTML =
    '<label for="p">Senha</label><input id="p" name="senha" type="password">'; } });
document.getElementById('b').onclick = async () => {
  const host = document.querySelector('x-user');
  const u = (host ? host.shadowRoot : document).getElementById('u').value;
  await fetch('/api/login', {method: 'POST', body: '{}'});
  document.body.innerHTML = '<h1>Início</h1><p>Olá, ' + u + '</p><button>Sair</button>';
};
</script>""",
)


@pytest.mark.parametrize("variant", ["dois_shadow_roots", "usuario_fora"])
async def test_fields_inside_shadow_roots_are_captured_and_never_recorded(
    browser: Browser, variant: str
) -> None:
    # S1: cada campo no seu shadow root, sem form; depois do login a página mostra o
    # usuário ("Olá, ana.souza").
    user = (
        "<x-user></x-user>"
        if variant == "dois_shadow_roots"
        else '<label for="u">Usuário</label><input id="u" name="usuario">'
    )

    async def scenario(p: Page) -> None:
        await p.locator("#u").fill(USER)
        await p.locator("#p").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()
        await p.get_by_role("button", name="Sair").click()

    login = SHADOW_LOGIN.replace("{user}", user)
    recording = await record_site(browser, {"/login": login}, "/login", scenario)
    dumped = recording.model_dump_json()
    assert USER not in dumped.lower() and PASSWORD not in dumped
    inputs = {e.element.name_attr: e for e in recording.events if isinstance(e, InputEvent)}
    assert inputs["usuario"].element.is_username and inputs["usuario"].value is None
    assert inputs["senha"].element.is_password
    assert any(f"Olá, {REDACTED}" in text for text in recording.observed_texts())
    assert inputs["senha"].element.shadow_path == ["body > x-pass"]
    assert inputs["senha"].element.css == "body > x-pass > #p"
    result = compile_(recording)
    # Campo de shadow root nunca é credencial de login: o envio grava (POST) e o
    # usuário vira uma entrada; a senha (type=password) continua a do cofre.
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")
    assert fill_value(result, "digitar_senha") == SecretValue(credential="banco", field="password")
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    manifest, prompt = dumps(result)
    assert USER not in manifest.lower() and USER not in prompt.lower()


async def test_a_username_in_another_case_in_the_names_of_the_page_never_reaches_the_robot(
    browser: Browser,
) -> None:
    # S3: o aria-label ("Menu de ANA.SOUZA") e o título ("Perfil de Ana.Souza") têm o
    # usuário em outra caixa, no DOM (sem CSS).
    login = page(
        "Entrar",
        """<h1>Portal</h1><form action="/login" method="post">
<label for="u">Usuário</label><input id="u" name="usuario">
<label for="p">Senha</label><input id="p" name="senha" type="password">
<button>Entrar</button></form>""",
    )
    inicio = page(
        "Início",
        """<h1>Início</h1>
<button aria-label="Menu de ANA.SOUZA" onclick="this.textContent = 'aberto'">Menu</button>
<a href="/perfil" title="Perfil de Ana.Souza">Perfil</a>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()
        await p.get_by_role("button").first.click()
        await p.get_by_role("link", name="Perfil").click()
        await p.get_by_role("heading", name="Perfil").wait_for()

    recording = await record_site(
        browser,
        {"/login": login, "/inicio": inicio, "/perfil": page("Perfil", "<h1>Perfil</h1>")},
        "/login",
        scenario,
        redirects={"/login": "/inicio"},
    )
    assert USER not in recording.model_dump_json().lower()
    result = compile_(recording)
    manifest, prompt = dumps(result)
    assert USER not in manifest.lower() and USER not in prompt.lower()
    perfil = result.manifest.step_by_id("clicar_perfil")
    assert isinstance(perfil, ClickStep)
    assert [loc.name for loc in perfil.target.locators if loc.name] == ["Perfil"]


async def test_confirm_with_a_token_after_a_login_of_a_spa_is_write(browser: Browser) -> None:
    # S7: login na SPA e, no mesmo documento, "Token" e "Confirmar" sem form (Pix).
    app = page(
        "Banco",
        """<h1>Banco</h1><div id="login">
<label for="u">Usuário</label><input id="u">
<label for="p">Senha</label><input id="p" type="password">
<button id="b">Entrar</button></div><p id="ok"></p>
<script>
document.getElementById('b').onclick = async () => {
  await fetch('/api/login', {method: 'POST', body: '{}'});
  history.pushState({}, '', '/app/pix/confirmar');
  document.getElementById('login').outerHTML = '<h2>Confirme o Pix de R$ 900,00</h2>' +
    '<label for="t">Token</label><input id="t" autocomplete="one-time-code">' +
    '<button id="c">Confirmar</button>';
  document.getElementById('c').onclick = async () => {
    await fetch('/api/pix', {method: 'POST', body: '{}'});
    document.getElementById('ok').textContent = 'Pix enviado';
  };
};
</script>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_label("Token").fill("481516")
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/app": app}, "/app", scenario)
    assert not has_text(recording, "481516")
    result = compile_(recording)
    assert click_effect(result, "clicar_entrar") is Effect.READ
    assert click_effect(result, "clicar_confirmar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


async def test_a_payee_and_a_code_without_a_password_is_write(browser: Browser) -> None:
    # S8: "CPF do favorecido" + código do SMS + "Continuar" (POST), sem senha.
    ted = page(
        "Transferência",
        """<h1>Transferência</h1><form action="/ted" method="post">
<label for="c">CPF do favorecido</label><input id="c" name="cpf_favorecido">
<label for="o">Código enviado por SMS</label><input id="o" autocomplete="one-time-code">
<button>Continuar</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("CPF do favorecido").fill("11122233344")
        await p.get_by_label("Código enviado por SMS").fill("481516")
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_role("heading", name="Agendada").wait_for()

    recording = await record_site(
        browser,
        {"/ted": ted, "/ok": page("OK", "<h1>Agendada</h1>")},
        "/ted",
        scenario,
        redirects={"/ted": "/ok"},
    )
    assert not has_text(recording, "11122233344") and not has_text(recording, "481516")
    favorecido = input_of(recording, "cpf_favorecido")
    assert favorecido.element.sensitive and not favorecido.element.is_username
    result = compile_(recording)
    assert click_effect(result, "clicar_continuar") is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_cpf_do_favorecido") == an_input("cpf_do_favorecido")
    _, prompt = dumps(result)
    assert not has_text(result.manifest, "11122233344") and "11122233344" not in prompt


async def test_a_sign_up_with_a_new_password_is_write(browser: Browser) -> None:
    # S9: e-mail + senha nova (autocomplete=new-password) + "Continuar" (POST).
    cadastro = page(
        "Cadastro",
        """<h1>Crie sua conta</h1><form action="/cadastro" method="post">
<label for="e">E-mail</label><input id="e" name="email" type="email" autocomplete="email">
<label for="s">Crie uma senha</label>
<input id="s" name="senha" type="password" autocomplete="new-password">
<button>Continuar</button></form>""",
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("E-mail").fill("novo@exemplo.com")
        await p.get_by_label("Crie uma senha").fill(PASSWORD)
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_role("heading", name="Conta criada").wait_for()

    recording = await record_site(
        browser,
        {"/cadastro": cadastro, "/ok": page("OK", "<h1>Conta criada</h1>")},
        "/cadastro",
        scenario,
        redirects={"/cadastro": "/ok"},
    )
    assert "novo@exemplo.com" not in recording.model_dump_json()
    result = compile_(recording)
    assert click_effect(result, "clicar_continuar") is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_e_mail") == an_input("e_mail")
    manifest, prompt = dumps(result)
    assert "novo@exemplo.com" not in manifest and "novo@exemplo.com" not in prompt


def two_pages(extra: str = "") -> dict[str, str]:
    """Login em duas páginas: o usuário e "Continuar" (GET), depois a senha e "Entrar"."""
    return {
        "/login": page(
            "Entrar",
            """<h1>Portal</h1><form action="/login/senha" method="get">
<label for="u">Usuário</label><input id="u" name="usuario">
<button>Continuar</button></form>""",
        ),
        "/login/senha": page(
            "Senha",
            f"""<h1>Digite a senha</h1><form action="/login/senha" method="post">{extra}
<label for="p">Senha</label><input id="p" name="senha" type="password">
<button>Entrar</button></form>""",
        ),
    }


@pytest.mark.parametrize("variant", ["forma_exata", "outro_campo"])
async def test_the_username_of_a_login_over_two_pages_never_reaches_the_recording(
    browser: Browser, variant: str
) -> None:
    extra = '<label for="e">Empresa</label><input id="e" name="empresa">'
    pages = two_pages(extra if variant == "outro_campo" else "")

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_role("heading", name="Digite a senha").wait_for()
        if variant == "outro_campo":
            await p.get_by_label("Empresa").fill("ACME")
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Comprovante").wait_for()

    recording = await record_site(
        browser,
        pages,
        "/login",
        scenario,
        redirects={"/login/senha": f"/inicio?quem={USER}"},
    )
    dumped = recording.model_dump_json()
    assert USER not in dumped.lower() and PASSWORD not in dumped
    usuario = input_of(recording, "usuario")
    assert usuario.element.is_username and usuario.element.sensitive and usuario.value is None
    assert any(f"Olá, {REDACTED}" in text for text in recording.observed_texts())
    result = compile_(recording)
    if variant == "forma_exata":
        assert usernames_in(result) == ["digitar_usuario"]
    else:
        assert usernames_in(result) == []
        assert fill_value(result, "digitar_usuario") == an_input("usuario")
    # A ponte (GET) e o envio (POST) ficam com o efeito das requisições deles.
    assert click_effect(result, "clicar_continuar") is Effect.READ
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    manifest, prompt = dumps(result)
    assert USER not in manifest.lower() and USER not in prompt.lower()
