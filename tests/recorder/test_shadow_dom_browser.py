"""Componentes com shadow root gravados num Chromium de verdade, e compilados (rodadas 7 a 10).

Duas instâncias do mesmo componente (``<x-field>``, ``<x-check>``, ``<x-select>``)
têm o mesmo ``<input id="input">`` por dentro, como nas bibliotecas de
componentes. Cada campo leva o caminho do hospedeiro e um seletor que atravessa o
shadow root; edições, marcações e escolhas de campos diferentes nunca se juntam; o
alvo e o valor de cada passo vêm do mesmo campo (conferido resolvendo os
localizadores do robô na página, como o executor faz); um campo de componente nunca
é credencial de login; e o gravador não embrulha nenhum método de shadow DOM nem de
eventos.

Rodada 8: o valor de um campo de texto de shadow root sai no change do próprio
shadow root, ou antes da próxima ação noutro elemento (salvamento automático,
ordem dos campos, input que some no blur, usuário que vira "chip", componente que
para o change), também em shadow roots declarativos e nos que já existiam antes
do gravador; e dois campos que coincidem em tudo o que se descreve deles (cartões
fundos, o mesmo rótulo e caminho) viram dois passos.

Rodada 9: o valor também sai no blur na fase de captura (o usuário que vira "chip"
quando a pessoa sai para um iframe); o campo que o componente tira da página antes
do gravador sai com o descritor e o valor guardados (com a marca de sigilo).

Rodada 10: o descritor e o valor guardados são os de depois de cada tecla, tirados
juntos (a linha certa, a máscara e o rótulo de código que aparecem depois do primeiro
foco, a tecla que a máscara põe por script), e as marcas de sigilo só sobem; no blur
e antes da próxima ação o gravador decide depois dos ouvintes de blur do componente
(o campo que vira senha no blur só sai como segredo); o caminho do blur da janela
vale só para campos de shadow roots; e o segundo clique de um clique duplo só vira
outro passo com prova de outro alvo (os dois botões de duas linhas sem nada que os
separe, o "Pagar" e o "Entrar" que a página recria, ficam um passo só).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from playwright.async_api import Browser, Page, Route, async_playwright
from playwright.async_api import Locator as PwLocator

from rapa.compiler import CompileResult
from rapa.core.robot import (
    CheckStep,
    ClickStep,
    Effect,
    FillStep,
    Locator,
    LocatorBy,
    SecretValue,
    SelectStep,
    TextValue,
)
from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.recorder import (
    CheckEvent,
    ClickEvent,
    InputEvent,
    Recorder,
    Recording,
    RequestEvent,
    SelectEvent,
)
from tests.recorder.test_login_rule_browser import (
    PASSWORD,
    SITE,
    USER,
    Scenario,
    an_input,
    click_effect,
    compile_,
    dumps,
    page,
    record_site,
    usernames_in,
)
from tests.recorder.texts import has_text

pytestmark = pytest.mark.browser

#: Campo de texto num componente: o rótulo e o <input id="input"> no shadow root aberto.
XFIELD = """<script>
customElements.define('x-field', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.attachShadow({mode: 'open'}).innerHTML = '<label for="input">' +
    this.getAttribute('label') + '</label><input id="input" type="' +
    (this.getAttribute('type') || 'text') + '">'; } });
</script>"""
#: Caixa de seleção num componente, numa célula da tabela.
XCHECK = """<script>
customElements.define('x-check', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.attachShadow({mode: 'open'}).innerHTML = '<input id="input" type="checkbox"' +
    (this.hasAttribute('checked') ? ' checked' : '') + ' aria-label="Selecionar ' +
    this.getAttribute('row') + '">'; } });
</script>"""
#: Lista num componente.
XSELECT = """<script>
customElements.define('x-select', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.attachShadow({mode: 'open'}).innerHTML = '<label for="input">' +
    this.getAttribute('label') + '</label><select id="input"><option>Escolha</option>' +
    '<option>Corrente 1</option><option>Poupança 2</option></select>'; } });
</script>"""
PIX_JS = """<script>
document.getElementById('b').onclick = async () => {
  await fetch('/api/pix', {method: 'POST', body: '{}'});
  document.body.insertAdjacentHTML('beforeend', '<p>Pix enviado</p>');
};
</script>"""


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


def locate(page: Page | PwLocator, locator: Locator) -> PwLocator:
    """O localizador do robô na página, como o executor o monta."""
    match locator.by:
        case LocatorBy.ROLE:
            role: Any = locator.value
            if locator.name is None:
                return page.get_by_role(role)
            return page.get_by_role(role, name=locator.name, exact=locator.exact)
        case LocatorBy.LABEL:
            return page.get_by_label(locator.value, exact=locator.exact)
        case LocatorBy.PLACEHOLDER:
            return page.get_by_placeholder(locator.value, exact=locator.exact)
        case LocatorBy.TEXT:
            return page.get_by_text(locator.value, exact=locator.exact)
        case LocatorBy.TEST_ID:
            return page.get_by_test_id(locator.value)
        case _:
            return page.locator(f"css={locator.value}")


async def hosts_of(
    browser: Browser, html: str, steps: list[FillStep] | list[CheckStep] | list[SelectStep]
) -> list[str]:
    """O hospedeiro do campo que cada passo acha, por todos os localizadores dele.

    Abre a página de novo e resolve cada localizador do alvo: cada um tem de achar um
    elemento só, e todos o mesmo; devolve o id do hospedeiro (ou do próprio campo, fora
    de shadow root).
    """
    context = await browser.new_context()
    try:
        p = await context.new_page()
        await p.set_content(html)
        found: list[str] = []
        for s in steps:
            ids = set()
            for locator in s.target.locators:
                located = locate(p, locator)
                assert await located.count() == 1, (s.id, locator)
                ids.add(
                    await located.evaluate(
                        "(el) => { const r = el.getRootNode(); return r.host ? r.host.id : el.id; }"
                    )
                )
            assert len(ids) == 1, (s.id, ids)
            found.append(ids.pop())
        return found
    finally:
        await context.close()


def fill_steps(result: CompileResult) -> list[FillStep]:
    return [s for s in result.manifest.steps if isinstance(s, FillStep)]


def inputs_of(recording: Recording) -> list[InputEvent]:
    return [e for e in recording.events if isinstance(e, InputEvent)]


# -- C1: login com usuário e senha em dois componentes iguais -------------------------

LOGIN = page(
    "Login",
    XFIELD
    + """<h1>Login</h1><x-field id="fu" label="Usuário"></x-field>
<x-field id="fp" label="Senha" type="password"></x-field><button id="b">Entrar</button>
<script>
document.getElementById('b').onclick = async () => {
  const u = document.getElementById('fu').shadowRoot.getElementById('input').value;
  await fetch('/api/login', {method: 'POST', body: '{}'});
  document.body.innerHTML = '<h1>Início</h1><p>Olá, ' + u + '</p><button>Sair</button>';
};
</script>""",
)


async def test_a_login_in_two_components_of_the_same_kind_never_mixes_the_fields(
    browser: Browser,
) -> None:
    async def scenario(p: Page) -> None:
        await p.locator("#fu").locator("input").fill(USER)
        await p.locator("#fp").locator("input").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()
        await p.get_by_role("button", name="Sair").click()

    recording = await record_site(browser, {"/login": LOGIN}, "/login", scenario)
    assert not has_text(recording, USER, ignore_case=True)
    assert not has_text(recording, PASSWORD)
    usuario, senha = inputs_of(recording)
    assert (usuario.element.css, usuario.element.shadow_path) == ("#fu > #input", ["#fu"])
    assert (senha.element.css, senha.element.shadow_path) == ("#fp > #input", ["#fp"])
    result = compile_(recording)
    found = fill_steps(result)
    # Dois passos, cada um no seu campo: o cofre nunca digita a senha na caixa do
    # usuário, e o usuário (campo de componente) nunca é credencial: vira uma entrada.
    assert [s.id for s in found] == ["digitar_usuario", "digitar_senha"]
    assert found[0].value == an_input("usuario")
    assert found[1].value == SecretValue(credential="banco", field="password")
    assert await hosts_of(browser, LOGIN, found) == ["fu", "fp"]
    assert usernames_in(result) == []
    assert click_effect(result, "clicar_entrar") is Effect.WRITE
    # Nenhum aviso fala de uma entrada que não existe (o campo juntado a outro).
    named = {spec.name for spec in result.manifest.inputs}
    assert named == {"usuario"}
    assert all(f"entrada “{n}”" not in w for w in result.warnings for n in ("senha", "usuario_2"))
    manifest, prompt = dumps(result)
    assert USER not in manifest.lower() and USER not in prompt.lower()


# -- C7 e C8: Pix com campos de componentes e a senha ---------------------------------

PIX_C7 = page(
    "Pix",
    XFIELD
    + """<h1>Pix por CPF</h1><x-field id="fc" label="CPF"></x-field>
<x-field id="fv" label="Valor"></x-field>
<label for="s">Senha</label><input id="s" type="password"><button id="b">Continuar</button>"""
    + PIX_JS,
)
PIX_C8 = page(
    "Pix",
    XFIELD
    + """<h1>Pix por CPF</h1><label for="c">CPF</label><input id="c">
<x-field id="fs" label="Senha" type="password"></x-field>
<x-field id="fv" label="Valor"></x-field><button id="b">Continuar</button>"""
    + PIX_JS,
)


@pytest.mark.parametrize("variant", ["C7", "C8"])
async def test_a_pix_with_fields_of_components_is_write_and_never_mixes_the_fields(
    browser: Browser, variant: str
) -> None:
    html = PIX_C7 if variant == "C7" else PIX_C8

    async def scenario(p: Page) -> None:
        if variant == "C7":
            await p.locator("#fc").locator("input").fill("11122233344")
            await p.locator("#fv").locator("input").fill("1250,00")
            await p.locator("#s").fill("1234")
        else:
            await p.locator("#c").fill("11122233344")
            await p.locator("#fs").locator("input").fill("1234")
            await p.locator("#fv").locator("input").fill("1250,00")
        await p.get_by_role("button", name="Continuar").click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pix": html}, "/pix", scenario)
    for secret in ("11122233344", "1250,00", "1234"):
        assert not has_text(recording, secret)
    result = compile_(recording)
    # O "Continuar" que faz o Pix nunca é autenticação.
    assert click_effect(result, "clicar_continuar") is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    assert usernames_in(result) == []
    found = {s.id: s for s in fill_steps(result)}
    vault = SecretValue(credential="banco", field="password")
    if variant == "C7":
        assert [s.value for s in found.values()] == [an_input("cpf"), an_input("valor"), vault]
        expected = ["fc", "fv", "s"]
    else:
        assert [s.value for s in found.values()] == [an_input("cpf"), vault, an_input("valor")]
        expected = ["c", "fs", "fv"]
    # O valor digitado em cada passo vai para o campo de onde ele veio.
    assert await hosts_of(browser, html, list(found.values())) == expected
    manifest, prompt = dumps(result)
    for secret in ("11122233344", "1250,00"):
        assert secret not in manifest and secret not in prompt


# -- C2 e C9: dados em dois componentes iguais (sem senha na página) -----------------


@pytest.mark.parametrize(
    ("labels", "values", "button"),
    [
        (("Chave Pix", "Valor"), ("11122233344", "1250,00"), "Continuar"),
        (("Quantidade", "Preço unitário"), ("10", "25"), "Salvar pedido"),
    ],
    ids=["C2", "C9"],
)
async def test_values_of_two_components_of_the_same_kind_go_to_their_own_fields(
    browser: Browser, labels: tuple[str, str], values: tuple[str, str], button: str
) -> None:
    html = page(
        "Pedido",
        XFIELD
        + f"""<h1>Pedido</h1><x-field id="fa" label="{labels[0]}"></x-field>
<x-field id="fb" label="{labels[1]}"></x-field><button id="b">{button}</button>"""
        + PIX_JS,
    )

    async def scenario(p: Page) -> None:
        await p.locator("#fa").locator("input").fill(values[0])
        await p.locator("#fb").locator("input").fill(values[1])
        await p.get_by_role("button", name=button).click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/pedido": html}, "/pedido", scenario)
    result = compile_(recording)
    found = fill_steps(result)
    assert [s.value for s in found] == [TextValue(template=v) for v in values]
    assert await hosts_of(browser, html, found) == ["fa", "fb"]
    clicks = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [s.effect for s in clicks] == [Effect.WRITE]


# -- C10 e C11: caixas de seleção de componentes nas linhas de uma tabela -----------

ROWS = """<h1>Boletos</h1><table>
<tr><td><x-check id="k1" row="Boleto 1001"{c1}></x-check></td><td>Boleto 1001</td></tr>
<tr><td><x-check id="k2" row="Boleto 1002"></x-check></td><td>Boleto 1002</td></tr>
<tr><td><x-check id="k3" row="Boleto 1003"></x-check></td><td>Boleto 1003</td></tr>
</table>
<button id="x" onclick="fetch('/api/excluir', {method: 'POST', body: '{}'})
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Removidos</p>'))">
Remover selecionados</button>"""


@pytest.mark.parametrize("variant", ["C10", "C11"])
async def test_checks_of_components_in_rows_act_on_their_own_rows(
    browser: Browser, variant: str
) -> None:
    html = page("Boletos", XCHECK + ROWS.replace("{c1}", " checked" if variant == "C11" else ""))

    async def scenario(p: Page) -> None:
        if variant == "C10":
            await p.get_by_role("checkbox", name="Selecionar Boleto 1002").check()
            await p.get_by_role("checkbox", name="Selecionar Boleto 1003").check()
        else:
            await p.get_by_role("checkbox", name="Selecionar Boleto 1003").check()
            await p.get_by_role("checkbox", name="Selecionar Boleto 1001").uncheck()
        await p.get_by_role("button", name="Remover selecionados").click()
        await p.get_by_text("Removidos").wait_for()

    recording = await record_site(browser, {"/boletos": html}, "/boletos", scenario)
    events = [e for e in recording.events if isinstance(e, CheckEvent)]
    assert len({e.element.css for e in events}) == 2
    assert all(e.element.shadow_path for e in events)
    result = compile_(recording)
    checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    if variant == "C10":
        expected = [
            ("Marcar “Selecionar Boleto 1002”", True),
            ("Marcar “Selecionar Boleto 1003”", True),
        ]
        hosts = ["k2", "k3"]
    else:
        expected = [
            ("Marcar “Selecionar Boleto 1003”", True),
            ("Desmarcar “Selecionar Boleto 1001”", False),
        ]
        hosts = ["k3", "k1"]
    assert [(s.title, s.checked) for s in checks] == expected
    assert await hosts_of(browser, html, checks) == hosts
    assert click_effect(result, "clicar_remover_selecionados") is Effect.WRITE


# -- listas em componentes e shadow DOM declarativo ----------------------------------


async def test_selects_and_checks_inside_shadow_roots_are_captured_without_wrappers(
    browser: Browser,
) -> None:
    # Duas listas do mesmo componente e uma caixa de seleção num shadow root
    # declarativo (sem attachShadow): o change não sai do shadow root, o input sai.
    html = page(
        "Transferir",
        XSELECT
        + """<h1>Transferir</h1><x-select id="so" label="Conta de origem"></x-select>
<x-select id="sd" label="Conta de destino"></x-select>
<div id="dk"><template shadowrootmode="open"><input id="input" type="checkbox"
  aria-label="Agendar"></template></div>
<button id="b" onclick="fetch('/api/transferir', {method: 'POST', body: '{}'})
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Feito</p>'))">Transferir</button>""",
    )

    async def scenario(p: Page) -> None:
        # Nenhum método nativo é trocado pelo gravador (scripts antifraude olham isso).
        assert await p.evaluate(
            """() => [Element.prototype.attachShadow, HTMLElement.prototype.attachShadow]
                .every((f) => Function.prototype.toString.call(f).includes('[native code]'))"""
        )
        assert await p.evaluate(
            "() => !Object.prototype.hasOwnProperty.call(HTMLElement.prototype, 'attachShadow')"
        )
        await p.locator("#so").locator("select").select_option("Corrente 1")
        await p.locator("#sd").locator("select").select_option("Poupança 2")
        await p.get_by_role("checkbox", name="Agendar").check()
        await p.get_by_role("button", name="Transferir").click()
        await p.get_by_text("Feito").wait_for()

    recording = await record_site(browser, {"/transferir": html}, "/transferir", scenario)
    selects = [e for e in recording.events if isinstance(e, SelectEvent)]
    assert [(e.element.css, e.label) for e in selects] == [
        ("#so > #input", "Corrente 1"),
        ("#sd > #input", "Poupança 2"),
    ]
    (agendar,) = [e for e in recording.events if isinstance(e, CheckEvent)]
    assert agendar.element.css == "#dk > #input" and agendar.checked
    result = compile_(recording)
    steps = [s for s in result.manifest.steps if isinstance(s, SelectStep)]
    assert [s.option for s in steps] == ["Corrente 1", "Poupança 2"]
    assert await hosts_of(browser, html, steps) == ["so", "sd"]
    (check,) = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    assert await hosts_of(browser, html, [check]) == ["dk"]


# -- H2: só o código depois do login de uma SPA, sem <form> ---------------------------


@pytest.mark.parametrize("button", ["Continuar", "Avançar", "Entrar"])
async def test_a_token_after_the_login_of_a_spa_without_a_form_is_write(
    browser: Browser, button: str
) -> None:
    # G2a: login na SPA; no mesmo documento, "Token" e o botão (sem <form>) que faz o Pix.
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
    '<button id="c">BOTAO</button>';
  document.getElementById('c').onclick = async () => {
    await fetch('/api/pix', {method: 'POST', body: '{}'});
    document.getElementById('ok').textContent = 'Pix enviado';
  };
};
</script>""".replace("BOTAO", button),
    )

    async def scenario(p: Page) -> None:
        await p.get_by_label("Usuário").fill(USER)
        await p.get_by_label("Senha").fill(PASSWORD)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_label("Token").wait_for()
        # A edição do token começa bem depois do POST do login (que, na folga do
        # começo da edição, faria o campo gravar e esconderia a regra).
        await p.wait_for_timeout(600)
        await p.get_by_label("Token").fill("481516")
        await p.get_by_role("button", name=button).click()
        await p.get_by_text("Pix enviado").wait_for()

    recording = await record_site(browser, {"/app": app}, "/app", scenario)
    assert not has_text(recording, "481516")
    result = compile_(recording)
    effects = {s.id: s.effect for s in result.manifest.steps}
    assert effects["digitar_token"] is Effect.READ
    assert effects["clicar_entrar"] is Effect.READ
    clicks = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [s.effect for s in clicks] == [Effect.READ, Effect.WRITE]
    assert result.manifest.effect() is Effect.WRITE


# -- rodada 8: o change de campos de texto em shadow roots e a identidade do elemento -

#: Marca do campo que um elemento é (nele, num ancestral ou num hospedeiro).
MARK_JS = """(el) => {
  let cur = el;
  while (cur) {
    if (cur.getAttribute && cur.getAttribute('data-gt')) return cur.getAttribute('data-gt');
    const root = cur.getRootNode();
    cur = cur.parentElement || (root && root.host) || null;
  }
  return null;
}"""


async def marks_of(
    browser: Browser, html: str, steps: list[FillStep] | list[CheckStep] | list[SelectStep]
) -> list[str]:
    """A marca ``data-gt`` do campo que cada passo acha, por todos os localizadores dele.

    Como ``hosts_of``, mas pela marca mais próxima do elemento: os campos destes
    testes têm o mesmo id (e o mesmo rótulo) nos dois lugares.
    """
    context = await browser.new_context()
    try:
        p = await context.new_page()
        await p.set_content(html)
        found: list[str] = []
        for s in steps:
            marks = set()
            for locator in s.target.locators:
                located = locate(p, locator)
                assert await located.count() == 1, (s.id, locator)
                marks.add(await located.evaluate(MARK_JS))
            assert len(marks) == 1, (s.id, marks)
            found.append(marks.pop())
        return found
    finally:
        await context.close()


async def record_open_page(browser: Browser, html: str, scenario: Scenario) -> Recording:
    """Grava numa aba que já estava aberta: os shadow roots existem antes do script."""
    context = await browser.new_context()

    async def handle(route: Route) -> None:
        if "/api/" in route.request.url:
            await route.fulfill(status=200, content_type="application/json", body="{}")
            return
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    await context.route("**/*", handle)
    try:
        p = await context.new_page()
        await p.goto(f"{SITE}/editor")
        await p.locator("x-form").locator("#b").wait_for()
        recorder = Recorder()
        await recorder.start(context)
        await scenario(p)
        return await recorder.stop()
    finally:
        await context.close()


#: "Rascunho" salva sozinho (POST) no change; "Título" fica no mesmo shadow root.
XFORM = """<script>
customElements.define('x-form', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="a">Rascunho</label><input id="a" data-gt="ra">' +
    '<label for="b">Título</label><input id="b" data-gt="tb">';
  r.getElementById('a').addEventListener('change',
    () => fetch('/api/rascunho', {method: 'POST', body: '{}'}));
} });
</script><x-form></x-form>"""
#: O mesmo, num shadow root declarativo (sem attachShadow).
XFORM_DECLARATIVE = """<x-form><template shadowrootmode="open">
<label for="a">Rascunho</label><input id="a" data-gt="ra">
<label for="b">Título</label><input id="b" data-gt="tb"></template></x-form>
<script>
document.querySelector('x-form').shadowRoot.getElementById('a')
  .addEventListener('change', () => fetch('/api/rascunho', {method: 'POST', body: '{}'}));
</script>"""
PREVIEW = """<button id="pv" onclick="fetch('/api/previa?x=1')
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Prévia</p>'))">
Pré-visualizar</button>"""


@pytest.mark.parametrize("variant", ["componente", "declarativo", "antes_do_gravador"])
async def test_an_autosave_in_a_shadow_field_before_another_field_of_the_root_is_write(
    browser: Browser, variant: str
) -> None:
    # R1: o change não sai do shadow root, e o focusout entre dois campos do mesmo
    # shadow root não chega à janela. Sem o change do shadow root, o valor do
    # "Rascunho" saía depois do POST (e depois do "Título") e o robô ficava de leitura.
    form = XFORM_DECLARATIVE if variant == "declarativo" else XFORM
    html = page("Editor", "<h1>Editor</h1>" + form + PREVIEW)

    async def scenario(p: Page) -> None:
        # Nenhum método nativo é trocado pelo gravador.
        assert await p.evaluate(
            """() => [Element.prototype.attachShadow, EventTarget.prototype.addEventListener,
                      Event.prototype.composedPath, HTMLElement.prototype.focus]
                .every((f) => Function.prototype.toString.call(f).includes('[native code]'))"""
        )
        host = p.locator("x-form")
        await host.locator("#a").click()
        await p.keyboard.type("texto do rascunho")
        await host.locator("#b").click()
        await p.wait_for_timeout(900)
        await p.keyboard.type("meu título")
        await p.get_by_role("button", name="Pré-visualizar").click()
        await p.get_by_text("Prévia").wait_for()

    if variant == "antes_do_gravador":
        recording = await record_open_page(browser, html, scenario)
    else:
        recording = await record_site(browser, {"/editor": html}, "/editor", scenario)
    assert [(e.element.label, e.value) for e in inputs_of(recording)] == [
        ("Rascunho", "texto do rascunho"),
        ("Título", "meu título"),
    ]
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value, s.effect) for s in found] == [
        ("digitar_rascunho", TextValue(template="texto do rascunho"), Effect.WRITE),
        ("digitar_titulo", TextValue(template="meu título"), Effect.READ),
    ]
    assert result.manifest.effect() is Effect.WRITE
    assert await marks_of(browser, html, found) == ["ra", "tb"]


async def test_fields_of_one_shadow_root_keep_the_order_the_person_typed(
    browser: Browser,
) -> None:
    # R2: o change do CEP preenche o logradouro e a pessoa corrige o logradouro. O
    # robô digita o CEP antes do logradouro, como a pessoa (senão o CEP desfaria a
    # correção).
    html = page(
        "Endereço",
        """<script>
customElements.define('x-end', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="c">CEP</label><input id="c" data-gt="cep">' +
    '<label for="l">Logradouro</label><input id="l" data-gt="log">';
  r.getElementById('c').addEventListener('change',
    () => { r.getElementById('l').value = 'Rua do CEP'; });
} });
</script><h1>Endereço</h1><x-end></x-end>
<button id="s" onclick="fetch('/api/salvar', {method: 'POST', body: '{}'})
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Salvo</p>'))">Salvar</button>""",
    )

    async def scenario(p: Page) -> None:
        host = p.locator("x-end")
        await host.locator("#c").click()
        await p.keyboard.type("01001000")
        await host.locator("#l").click()
        await p.keyboard.press("Control+A")
        await p.keyboard.type("Praça da Sé")
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_text("Salvo").wait_for()

    recording = await record_site(browser, {"/endereco": html}, "/endereco", scenario)
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value) for s in found] == [
        ("digitar_cep", TextValue(template="01001000")),
        ("digitar_logradouro", TextValue(template="Praça da Sé")),
    ]
    assert await marks_of(browser, html, found) == ["cep", "log"]


async def test_an_inline_editor_that_replaces_the_input_on_blur_keeps_both_values(
    browser: Browser,
) -> None:
    # R3: o input da célula some no blur (que grava a célula). O valor sai no change,
    # antes do blur, enquanto o campo ainda existe.
    html = page(
        "Grade",
        """<script>
customElements.define('x-grid', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<div class="c" data-k="a">10</div><div class="c" data-k="b">20</div>';
  for (const cell of r.querySelectorAll('.c')) cell.addEventListener('click', () => {
    if (cell.querySelector('input')) return;
    const old = cell.textContent; cell.textContent = '';
    const inp = document.createElement('input');
    inp.setAttribute('aria-label', 'Valor ' + cell.dataset.k); inp.value = old;
    cell.appendChild(inp); inp.focus(); inp.select();
    inp.addEventListener('blur', () => {
      const v = inp.value; cell.textContent = v;
      fetch('/api/celula?k=' + cell.dataset.k, {method: 'POST', body: v});
    });
  });
} });
</script><h1>Grade</h1><x-grid></x-grid>
<button id="pv" onclick="fetch('/api/ver')
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Visto</p>'))">Ver</button>""",
    )

    async def scenario(p: Page) -> None:
        grid = p.locator("x-grid")
        await grid.locator(".c >> nth=0").click()
        await p.keyboard.type("42")
        await grid.locator(".c >> nth=1").click()
        await p.wait_for_timeout(700)
        await p.keyboard.type("43")
        await p.get_by_role("button", name="Ver").click()
        await p.get_by_text("Visto").wait_for()

    recording = await record_site(browser, {"/grade": html}, "/grade", scenario)
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value) for s in found] == [
        ("digitar_valor_a", TextValue(template="42")),
        ("digitar_valor_b", TextValue(template="43")),
    ]
    # O POST do blur de cada célula grava no passo do campo ou no clique seguinte (o
    # que tira o foco, como no robô): a requisição pode chegar antes ou depois do
    # clique, conforme o navegador. Nunca fica sem um passo que grava.
    effects = {s.id: s.effect for s in result.manifest.steps}
    assert Effect.WRITE in (effects["digitar_valor_a"], effects["clicar_20"])
    assert Effect.WRITE in (effects["digitar_valor_b"], effects["clicar_ver"])
    assert result.manifest.effect() is Effect.WRITE


async def test_a_value_leaves_before_the_next_action_even_if_the_component_stops_the_change(
    browser: Browser,
) -> None:
    # O componente para o change no próprio shadow root (antes do ouvinte do gravador)
    # e troca o input por texto no blur: o valor sai no pointerdown do botão, antes
    # do blur, enquanto o campo ainda existe. (O botão fica acima do componente, que
    # muda de tamanho no blur.)
    html = page(
        "Etiquetas",
        """<script>
customElements.define('x-tag', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.addEventListener('change', (e) => e.stopImmediatePropagation(), true);
  r.innerHTML = '<label for="t">Etiqueta</label><input id="t">';
  const inp = r.getElementById('t');
  inp.addEventListener('blur', () => {
    const s = document.createElement('span'); s.textContent = inp.value; inp.replaceWith(s);
  });
} });
</script><h1>Etiquetas</h1>
<p><button id="b" onclick="fetch('/api/etiqueta', {method: 'POST', body: '{}'})
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Salvo</p>'))">Salvar</button></p>
<div><x-tag></x-tag></div>""",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-tag").locator("#t").click()
        await p.keyboard.type("urgente")
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_text("Salvo").wait_for()

    recording = await record_site(browser, {"/etiquetas": html}, "/etiquetas", scenario)
    result = compile_(recording)
    assert [(s.id, s.value) for s in fill_steps(result)] == [
        ("digitar_etiqueta", TextValue(template="urgente"))
    ]
    assert click_effect(result, "clicar_salvar") is Effect.WRITE


async def test_a_username_that_becomes_a_chip_on_blur_never_reaches_the_robot(
    browser: Browser,
) -> None:
    # R4b: o e-mail do componente vira um "chip" no blur, e a página ecoa "Entrando
    # como <e-mail>" na luz. O valor sai (só como segredo) antes do blur, e a limpeza
    # tira o eco da gravação.
    email = "ana.chip@exemplo.com"
    html = page(
        "Login",
        """<script>
customElements.define('x-user', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="e">E-mail</label>' +
    '<input id="e" type="email" autocomplete="username">';
  const inp = r.getElementById('e');
  inp.addEventListener('blur', () => {
    if (!inp.value) return;
    const chip = document.createElement('span'); chip.textContent = inp.value;
    inp.replaceWith(chip);
    document.getElementById('quem').textContent = 'Entrando como ' + inp.value;
  });
} });
</script><h1>Entrar no banco</h1><p id="quem"></p><x-user></x-user>
<label>Senha <input id="p" type="password"></label><button id="b">Entrar</button>
<script>document.getElementById('b').onclick = async () => {
  await fetch('/api/login', {method: 'POST', body: '{}'});
  await new Promise((r) => setTimeout(r, 300)); location.href = '/inicio';
};</script>""",
    )
    inicio = page("Início", "<h1>Início</h1><p>Sua conta</p><button>Sair</button>")

    async def scenario(p: Page) -> None:
        await p.locator("x-user").locator("#e").click()
        await p.keyboard.type(email)
        await p.locator("#p").click()
        await p.keyboard.type(PASSWORD)
        await p.wait_for_timeout(300)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()

    recording = await record_site(browser, {"/login": html, "/inicio": inicio}, "/login", scenario)
    assert not has_text(recording, email, ignore_case=True)
    assert not has_text(recording, PASSWORD)
    usuario = next(e for e in inputs_of(recording) if e.element.label == "E-mail")
    assert usuario.element.sensitive and usuario.value is None
    result = compile_(recording)
    # Campo de componente: nunca credencial; o valor vira uma entrada.
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_e_mail": an_input("e_mail"),
        "digitar_senha": SecretValue(credential="banco", field="password"),
    }
    assert usernames_in(result) == []
    manifest, prompt = dumps(result)
    assert email not in manifest.lower() and email not in prompt.lower()


#: Componentes dos cartões fundos: o rótulo, o id e o nome iguais; o data-testid muda.
XCARDS = """<script>
customElements.define('x-qty', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.attachShadow({mode: 'open'}).innerHTML = '<label for="input">Quantidade</label>' +
    '<input id="input" name="qtd" data-testid="' + this.getAttribute('tid') + '">'; } });
customElements.define('x-inc', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  this.attachShadow({mode: 'open'}).innerHTML = '<label for="input">Incluir</label>' +
    '<input id="input" name="inc" type="checkbox" data-testid="' + this.getAttribute('tid') +
    '"' + (this.hasAttribute('checked') ? ' checked' : '') + '>'; } });
</script>"""
SAVE = """<button id="b" onclick="fetch('/api/salvar', {method: 'POST', body: '{}'})
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Salvo</p>'))">Salvar</button>"""


def deep_cards(light: bool, check: bool) -> str:
    """Dois cartões com mais de seis níveis da luz até o campo (ou o hospedeiro).

    O caminho CSS (e o do hospedeiro) para em seis níveis, então é o mesmo nos dois;
    o rótulo, o id e o nome também. Só o ``data-testid`` muda.
    """
    cards = []
    for k in (1, 2):
        if check:
            checked = " checked" if k == 2 else ""
            inner = (
                f'<label>Incluir <input type="checkbox" id="inc" name="inc" '
                f'data-testid="inc-{k}"{checked}></label>'
                if light
                else f'<x-inc tid="inc-{k}"{checked}></x-inc>'
            )
        else:
            inner = (
                f'<label>Quantidade <input id="qtd" name="qtd" data-testid="qtd-{k}"></label>'
                if light
                else f'<x-qty tid="qtd-{k}"></x-qty>'
            )
        cards.append(
            f'<li data-gt="q{k}"><article><div><div><div><div><span>{inner}</span></div>'
            "</div></div></div></article></li>"
        )
    return XCARDS + "<h1>Carrinho</h1><ul>" + "".join(cards) + "</ul>" + SAVE


@pytest.mark.parametrize("light", [True, False], ids=["luz", "shadow"])
async def test_two_deep_fields_with_the_same_label_and_path_are_two_steps(
    browser: Browser, light: bool
) -> None:
    # N10: antes, um passo só, com o localizador do cartão 1 (test_id "qtd-1") e o
    # valor digitado no cartão 2: o robô digitava 7 no cartão errado.
    html = page("Carrinho", deep_cards(light, check=False))

    async def scenario(p: Page) -> None:
        await p.locator("[data-gt=q1]").locator("input").fill("3")
        await p.locator("[data-gt=q2]").locator("input").fill("7")
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_text("Salvo").wait_for()

    recording = await record_site(browser, {"/carrinho": html}, "/carrinho", scenario)
    first, second = inputs_of(recording)
    assert first.element.css == second.element.css
    result = compile_(recording)
    found = fill_steps(result)
    assert [s.value for s in found] == [TextValue(template="3"), TextValue(template="7")]
    assert await marks_of(browser, html, found) == ["q1", "q2"]
    # Cada elemento tem o seu número na gravação.
    serials = {first.element.serial, second.element.serial}
    assert None not in serials and len(serials) == 2


@pytest.mark.parametrize("light", [True, False], ids=["luz", "shadow"])
async def test_a_check_and_an_uncheck_in_two_deep_cards_are_two_steps(
    browser: Browser, light: bool
) -> None:
    html = page("Carrinho", deep_cards(light, check=True))

    async def scenario(p: Page) -> None:
        await p.locator("[data-gt=q1]").locator("input").check()
        await p.locator("[data-gt=q2]").locator("input").uncheck()
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_text("Salvo").wait_for()

    recording = await record_site(browser, {"/carrinho": html}, "/carrinho", scenario)
    events = [e for e in recording.events if isinstance(e, CheckEvent)]
    assert [e.checked for e in events] == [True, False]
    assert events[0].element.css == events[1].element.css
    result = compile_(recording)
    checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    assert [s.checked for s in checks] == [True, False]
    assert await marks_of(browser, html, checks) == ["q1", "q2"]


# -- rodada 9: o blur na captura, o valor guardado do campo que saiu e o clique duplo --

#: O e-mail do componente vira um "chip" no blur, e o componente para o change no
#: próprio shadow root (antes do ouvinte do gravador). A página ecoa "Entrando como
#: <e-mail>" na luz; a senha e o botão ficam num iframe do mesmo site.
XUSER_STOP = """<script>
customElements.define('x-user', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.addEventListener('change', (e) => e.stopImmediatePropagation(), true);
  r.innerHTML = '<label for="e">E-mail</label>' +
    '<input id="e" type="email" autocomplete="username">';
  const inp = r.getElementById('e');
  inp.addEventListener('blur', () => {
    if (!inp.value) return;
    const chip = document.createElement('span'); chip.textContent = inp.value;
    inp.replaceWith(chip);
    document.getElementById('quem').textContent = 'Entrando como ' + inp.value;
  });
} });
</script>"""
SENHA_QUADRO = page(
    "Senha",
    """<label>Senha <input id="p" type="password"></label><button id="b">Entrar</button>
<script>document.getElementById('b').onclick = async () => {
  await fetch('/api/login', {method: 'POST', body: '{}'});
  await new Promise((r) => setTimeout(r, 300)); top.location.href = '/inicio';
};</script>""",
)


async def test_a_username_that_becomes_a_chip_while_leaving_to_an_iframe_never_reaches_the_robot(
    browser: Browser,
) -> None:
    # E10b: ao clicar no iframe da senha não há pointerdown nem keydown no documento do
    # e-mail, o change para no shadow root e o focusout chega com o input já trocado
    # pelo chip. O blur na fase de captura (da janela) chega antes do blur do campo:
    # o valor sai (só como segredo) enquanto o input existe, e a limpeza tira o eco.
    email = "ana.quadro@exemplo.com"
    html = page(
        "Login",
        XUSER_STOP + "<h1>Entrar no banco</h1>"
        "<iframe src='/senha' title='Senha' style='width:500px;height:150px'></iframe>"
        "<p id='quem' style='height:2em'></p><div style='height:3em'><x-user></x-user></div>",
    )
    inicio = page("Início", "<h1>Início</h1><p>Sua conta</p><button>Sair</button>")

    async def scenario(p: Page) -> None:
        # Nenhum método nativo é trocado pelo gravador (o blur também não).
        assert await p.evaluate(
            """() => [Element.prototype.attachShadow, EventTarget.prototype.addEventListener,
                      Event.prototype.composedPath, HTMLElement.prototype.focus,
                      HTMLElement.prototype.blur]
                .every((f) => Function.prototype.toString.call(f).includes('[native code]'))"""
        )
        await p.locator("x-user").locator("#e").click()
        await p.keyboard.type(email)
        frame = p.frame_locator("iframe")
        await frame.locator("#p").click()
        await p.keyboard.type(PASSWORD)
        await p.wait_for_timeout(300)
        await frame.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()

    recording = await record_site(
        browser, {"/login": html, "/senha": SENHA_QUADRO, "/inicio": inicio}, "/login", scenario
    )
    assert not has_text(recording, email, ignore_case=True)
    assert not has_text(recording, PASSWORD)
    usuario = next(e for e in inputs_of(recording) if e.element.label == "E-mail")
    assert usuario.element.sensitive and usuario.value is None
    result = compile_(recording)
    # Campo de componente: nunca credencial; o valor vira uma entrada.
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_e_mail": an_input("e_mail"),
        "digitar_senha": SecretValue(credential="banco", field="password"),
    }
    assert usernames_in(result) == []
    manifest, prompt = dumps(result)
    assert email not in manifest.lower() and email not in prompt.lower()


def xcell(stop: bool) -> str:
    """Célula de componente que troca o input por texto (e grava) no próprio change.

    O ouvinte de change do componente fica no shadow root, na fase de captura, e é
    posto antes do do gravador (que só acha o shadow root no primeiro evento dele).
    ``stop``: ele também para o change, e o gravador não o vê.
    """
    halt = "e.stopImmediatePropagation();" if stop else ""
    return (
        """<script>
customElements.define('x-cell', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="t">Valor</label><input id="t" data-gt="c">';
  r.addEventListener('change', (e) => { """
        + halt
        + """
    const inp = e.composedPath()[0];
    if (!inp || inp.tagName !== 'INPUT') return;
    const s = document.createElement('span'); s.textContent = inp.value; inp.replaceWith(s);
    fetch('/api/celula', {method: 'POST', body: inp.value});
  }, true);
} });
</script>"""
    )


QUADRO_VER = page(
    "Quadro",
    """<button id="ver" onclick="fetch('/api/ver')
  .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Visto</p>'))">Ver</button>""",
)


@pytest.mark.parametrize("stop", [False, True], ids=["troca", "para_e_troca"])
async def test_a_value_the_component_takes_away_before_the_recorder_sees_it_is_kept(
    browser: Browser, stop: bool
) -> None:
    # E3b/E3d: a pessoa sai da célula clicando num iframe (sem pointerdown nem keydown
    # neste documento) e o change do componente troca o input antes do ouvinte do
    # gravador (ou também para o change). O gravador acha o campo fora da página (no
    # change do shadow root ou no blur da janela) e manda o descritor e o valor
    # guardados no último input da pessoa.
    html = page(
        "Grade",
        xcell(stop) + "<h1>Grade</h1>"
        "<iframe src='/quadro' title='Ações' style='width:400px;height:120px'></iframe>"
        "<div style='height:40px'><x-cell></x-cell></div>",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-cell").locator("#t").click()
        await p.keyboard.type("V-42")
        frame = p.frame_locator("iframe")
        await frame.get_by_role("button", name="Ver").click()
        await frame.get_by_text("Visto").wait_for()

    recording = await record_site(
        browser, {"/grade": html, "/quadro": QUADRO_VER}, "/grade", scenario
    )
    assert [(e.element.label, e.value) for e in inputs_of(recording)] == [("Valor", "V-42")]
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value) for s in found] == [("digitar_valor", TextValue(template="V-42"))]
    assert await marks_of(browser, html, found) == ["c"]
    # O POST do change grava no passo do campo (ou no clique no iframe, que tira o foco
    # no robô, se a requisição chegar depois dele). Nunca fica sem um passo que grava.
    effects = {s.id: s.effect for s in result.manifest.steps}
    assert Effect.WRITE in (effects["digitar_valor"], effects["clicar_ver"])
    assert result.manifest.effect() is Effect.WRITE


@pytest.mark.parametrize("stop", [False, True], ids=["troca", "para_e_troca"])
async def test_a_username_the_component_takes_away_before_the_recorder_sees_it_stays_secret(
    browser: Browser, stop: bool
) -> None:
    # O mesmo caminho com o usuário (autocomplete=username): o descritor guardado leva a
    # marca de sigilo, o valor só sai como segredo e a limpeza tira o eco.
    email = "ana.celula@exemplo.com"
    component = (
        xcell(stop)
        .replace('<label for="t">Valor</label>', '<label for="t">E-mail</label>')
        .replace(
            '<input id="t" data-gt="c">',
            '<input id="t" data-gt="c" type="email" autocomplete="username">',
        )
        .replace(
            "inp.replaceWith(s);",
            "inp.replaceWith(s); document.getElementById('quem').textContent = "
            "'Entrando como ' + inp.value;",
        )
    )
    html = page(
        "Login",
        component + "<h1>Entrar no banco</h1>"
        "<iframe src='/senha' title='Senha' style='width:500px;height:150px'></iframe>"
        "<p id='quem' style='height:2em'></p><div style='height:3em'><x-cell></x-cell></div>",
    )
    inicio = page("Início", "<h1>Início</h1><p>Sua conta</p><button>Sair</button>")

    async def scenario(p: Page) -> None:
        await p.locator("x-cell").locator("#t").click()
        await p.keyboard.type(email)
        frame = p.frame_locator("iframe")
        await frame.locator("#p").click()
        await p.get_by_text("Entrando como").wait_for()
        await p.keyboard.type(PASSWORD)
        await p.wait_for_timeout(300)
        await frame.get_by_role("button", name="Entrar").click()
        await p.get_by_role("heading", name="Início").wait_for()

    recording = await record_site(
        browser, {"/login": html, "/senha": SENHA_QUADRO, "/inicio": inicio}, "/login", scenario
    )
    assert not has_text(recording, email, ignore_case=True)
    assert not has_text(recording, PASSWORD)
    usuario = next(e for e in inputs_of(recording) if e.element.label == "E-mail")
    assert usuario.element.sensitive and usuario.value is None
    result = compile_(recording)
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_e_mail": an_input("e_mail"),
        "digitar_senha": SecretValue(credential="banco", field="password"),
    }
    manifest, prompt = dumps(result)
    assert email not in manifest.lower() and email not in prompt.lower()


async def test_two_fast_clicks_on_the_buttons_of_two_rows_without_another_target_are_one_step(
    browser: Browser,
) -> None:
    # D1: a pessoa exclui o boleto 1, a linha some e o "Excluir" do boleto 2 sobe para
    # o mesmo lugar; o segundo clique vem com detail 2 e o mesmo caminho CSS (as
    # linhas são fundas demais). São dois elementos e cada clique gravou, mas nada
    # mais os separa (o mesmo texto, o mesmo nome e nenhuma linha de tabela): para o
    # gravador, é o mesmo que um clique duplo num botão sem trava que a página recria
    # (W11). Um passo só (resíduo: o robô exclui um boleto onde a pessoa excluiu dois;
    # age menos, nunca mais).
    rows = "".join(
        f'<li data-gt="i{k}"><article><div><div><div><div><span>Boleto {k}</span>'
        f"<button onclick=\"fetch('/api/excluir?i={k}', {{method: 'POST', body: '{{}}'}}); "
        "this.closest('li').remove()\">Excluir</button></div></div></div></div></article></li>"
        for k in (1, 2, 3)
    )
    html = page("Boletos", f"<h1>Boletos</h1><ul>{rows}</ul>")

    async def scenario(p: Page) -> None:
        box = await p.locator("[data-gt=i1] button").bounding_box()
        assert box is not None
        await p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # O segundo clique só depois da requisição do primeiro (o gravador já a viu).
        async with p.expect_request("**/api/excluir?i=1"):
            await p.mouse.down(click_count=1)
            await p.mouse.up(click_count=1)
        await p.locator("[data-gt=i1]").wait_for(state="detached")
        async with p.expect_request("**/api/excluir?i=2"):
            await p.mouse.down(click_count=2)
            await p.mouse.up(click_count=2)
        await p.locator("[data-gt=i2]").wait_for(state="detached")

    recording = await record_site(browser, {"/boletos": html}, "/boletos", scenario)
    clicks = [e for e in recording.events if isinstance(e, ClickEvent)]
    assert [c.detail for c in clicks] == [1, 2]
    assert clicks[0].element.css == clicks[1].element.css
    assert clicks[0].element.serial != clicks[1].element.serial
    result = compile_(recording)
    steps = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [(s.id, s.effect) for s in steps] == [("clicar_excluir", Effect.IRREVERSIBLE)]


async def test_a_double_click_on_a_button_the_page_recreates_is_still_one_click(
    browser: Browser,
) -> None:
    # O primeiro clique paga e a página recria o botão; o segundo cai no botão novo (outro
    # elemento, o mesmo caminho) e não faz nada (a página ignora o clique repetido). Um
    # passo só: o robô nunca paga duas vezes onde a pessoa pagou uma.
    html = page(
        "Boleto",
        """<h1>Boleto</h1><div id="box"></div><script>
let busy = false;
const render = () => {
  document.getElementById('box').innerHTML = '<div><button id="pg">Pagar</button></div>';
  document.getElementById('pg').onclick = () => {
    if (busy) return;
    busy = true;
    fetch('/api/pagar', {method: 'POST', body: '{}'})
      .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Pago</p>'));
    render();
  };
};
render();
</script>""",
    )

    async def scenario(p: Page) -> None:
        await p.locator("#pg").dblclick()
        await p.get_by_text("Pago").wait_for()

    recording = await record_site(browser, {"/boleto": html}, "/boleto", scenario)
    clicks = [e for e in recording.events if isinstance(e, ClickEvent)]
    assert [c.detail for c in clicks] == [1, 2]
    assert clicks[0].element.serial != clicks[1].element.serial
    result = compile_(recording)
    steps = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [(s.id, s.effect) for s in steps] == [("clicar_pagar", Effect.IRREVERSIBLE)]


# -- rodada 10: o descritor e o valor guardados depois de cada tecla, as decisões ------
# -- depois dos ouvintes do componente e o clique duplo sem prova de outro alvo --------

QUADRO_IFRAME = "<iframe src='/quadro' title='Ações' style='width:400px;height:120px'></iframe>"


async def leave_to_the_iframe(p: Page) -> None:
    """A pessoa sai do campo clicando num iframe (sem pointerdown nem keydown neste documento)."""
    frame = p.frame_locator("iframe")
    await frame.get_by_role("button", name="Ver").click()
    await frame.get_by_text("Visto").wait_for()


async def marks_after(
    browser: Browser, html: str, before: Scenario, steps: list[FillStep]
) -> list[str]:
    """Como ``marks_of``, depois de ``before`` (o que o robô faz antes) e dentro da linha do passo.

    A linha é escolhida como o executor escolhe: o contêiner com um elemento cujo
    texto inteiro é ``within_has_text``.
    """
    context = await browser.new_context()
    try:
        p = await context.new_page()
        await p.set_content(html)
        await before(p)
        found: list[str] = []
        for s in steps:
            root: Page | PwLocator = p
            if s.target.within is not None:
                within = locate(p, s.target.within)
                if s.target.within_has_text is not None:
                    within = within.filter(has=p.get_by_text(s.target.within_has_text, exact=True))
                assert await within.count() == 1, (s.id, s.target.within)
                root = within
            marks = set()
            for locator in s.target.locators:
                located = locate(root, locator)
                assert await located.count() == 1, (s.id, locator)
                marks.add(await located.evaluate(MARK_JS))
            assert len(marks) == 1, (s.id, marks)
            found.append(marks.pop())
        return found
    finally:
        await context.close()


#: Linhas que a lista reaproveita no "Ordenar" (como uma lista sem chave): o mesmo
#: input passa a ser o da outra linha. Cada célula é um componente que troca o input
#: por texto (e grava) no próprio change, antes do gravador.
ROWS_REUSED = (
    """<script>
customElements.define('x-cell', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<input aria-label="Valor">';
  r.addEventListener('change', (e) => {
    const inp = e.composedPath()[0];
    if (!inp || inp.tagName !== 'INPUT') return;
    const s = document.createElement('span'); s.textContent = inp.value; inp.replaceWith(s);
    fetch('/api/celula?id=' + this.closest('tr').dataset.gt, {method: 'POST', body: inp.value});
  }, true);
} });
</script><h1>Boletos</h1><button id="ord" onclick="sortRows()">Ordenar</button>
"""
    + QUADRO_IFRAME
    + """
<table><tbody id="tb">
<tr data-gt="rA"><td class="n">Boleto A</td><td><x-cell></x-cell></td></tr>
<tr data-gt="rB"><td class="n">Boleto B</td><td><x-cell></x-cell></td></tr>
</tbody></table>
<script>
let data = [{id: 'A', n: 'Boleto A'}, {id: 'B', n: 'Boleto B'}];
function sortRows() { data.reverse(); document.querySelectorAll('#tb tr').forEach((tr, i) => {
  tr.dataset.gt = 'r' + data[i].id; tr.querySelector('.n').textContent = data[i].n; }); }
</script>"""
)


async def test_a_reused_row_input_keeps_the_row_the_person_typed_in(browser: Browser) -> None:
    # W1s: a pessoa foca o campo da linha A, clica em "Ordenar" (o mesmo input passa a
    # ser o da linha B), digita 99 e sai para o iframe; o componente tira o input da
    # página no próprio change. Antes, o descritor guardado no primeiro foco (linha A)
    # saía com o valor, e o robô digitava 99 na linha A.
    html = page("Boletos", ROWS_REUSED)

    async def scenario(p: Page) -> None:
        cell = p.locator("tr").nth(0).locator("x-cell").locator("input")
        await cell.click()
        await p.get_by_role("button", name="Ordenar").click()
        await cell.click()
        await p.keyboard.type("99")
        await leave_to_the_iframe(p)

    recording = await record_site(
        browser, {"/boletos": html, "/quadro": QUADRO_VER}, "/boletos", scenario
    )
    (valor,) = inputs_of(recording)
    assert valor.value == "99"
    assert valor.element.row is not None and valor.element.row.key == "Boleto B"
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value, s.target.within_has_text) for s in found] == [
        ("digitar_valor", TextValue(template="99"), "Boleto B")
    ]

    async def ordenar(p: Page) -> None:
        await p.get_by_role("button", name="Ordenar").click()

    assert await marks_after(browser, html, ordenar, found) == ["rB"]


def masked_signature(mask: str) -> str:
    """Assinatura eletrônica de componente cuja máscara por CSS só aparece depois da primeira tecla.

    ``classe``: o componente põe a classe da máscara no input; ``placeholder``: o CSS
    mascara o input sem o placeholder à mostra. O componente troca o input no próprio
    change, antes do gravador.
    """
    style = {
        "classe": "<style>.m { -webkit-text-security: disc; }</style>",
        "placeholder": (
            "<style>input:not(:placeholder-shown) { -webkit-text-security: disc; }</style>"
        ),
    }[mask]
    on_input = (
        "inp.addEventListener('input', () => inp.classList.add('m'));" if mask == "classe" else ""
    )
    return (
        """<script>
customElements.define('x-sig', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '"""
        + style
        + """<label for="s">Assinatura eletrônica</label><input id="s" placeholder="Digite">';
  const inp = r.getElementById('s'); """
        + on_input
        + """
  r.addEventListener('change', (e) => {
    const t = e.composedPath()[0];
    if (!t || t.tagName !== 'INPUT') return;
    const s = document.createElement('span'); s.textContent = '••••'; t.replaceWith(s);
    fetch('/api/assinar', {method: 'POST', body: t.value});
  }, true);
} });
</script>"""
    )


@pytest.mark.parametrize("mask", ["classe", "placeholder"])
async def test_a_mask_that_appears_after_the_first_key_keeps_the_signature_secret(
    browser: Browser, mask: str
) -> None:
    # W2a/W2b: a máscara por CSS aparece depois da primeira tecla; o componente tira o
    # input da página no change e a pessoa sai para o iframe. O descritor guardado é o
    # de depois da última tecla (com a máscara), e as marcas de sigilo só sobem: a
    # assinatura só sai como segredo. Antes, saía como texto (o descritor do primeiro
    # foco, sem a máscara).
    secret = "8642"
    html = page(
        "Transferência",
        masked_signature(mask)
        + "<h1>Confirmar transferência</h1>"
        + QUADRO_IFRAME
        + "<div style='height:40px'><x-sig></x-sig></div>",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-sig").locator("#s").click()
        await p.keyboard.type(secret)
        await leave_to_the_iframe(p)

    recording = await record_site(
        browser, {"/transferencia": html, "/quadro": QUADRO_VER}, "/transferencia", scenario
    )
    assert not has_text(recording, secret)
    (assinatura,) = inputs_of(recording)
    assert assinatura.element.sensitive and assinatura.element.is_password
    assert assinatura.value is None
    result = compile_(recording)
    assert [(s.id, s.value) for s in fill_steps(result)] == [
        ("digitar_assinatura_eletronica", SecretValue(credential="banco", field="password"))
    ]
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


#: O mesmo input do componente serve ao nome e, depois do "Continuar", ao código de
#: verificação (só o rótulo muda); no código, o componente troca o input no change.
XWIZARD = """<script>
customElements.define('x-wiz', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="u" id="lb">Nome completo</label><input id="u">';
  const inp = r.getElementById('u');
  this.step2 = () => {
    r.getElementById('lb').textContent = 'Código de verificação'; inp.value = '';
  };
  r.addEventListener('change', (e) => {
    const t = e.composedPath()[0];
    if (!this.done2 || !t || t.tagName !== 'INPUT') return;
    const s = document.createElement('span'); s.textContent = 'Verificando...'; t.replaceWith(s);
    fetch('/api/verificar', {method: 'POST', body: t.value});
  }, true);
} });
</script>"""


async def test_a_component_input_reused_for_a_code_keeps_the_code_secret(
    browser: Browser,
) -> None:
    # W3: o input do "Nome completo" vira o do "Código de verificação"; o componente o
    # tira da página no change e a pessoa sai para o iframe. Antes, o código saía como
    # texto, com o descritor do primeiro foco (o rótulo do nome).
    code = "482913"
    html = page(
        "Cadastro",
        XWIZARD
        + "<h1>Cadastro</h1>"
        + QUADRO_IFRAME
        + "<div style='height:40px'><x-wiz></x-wiz></div><button id='c' onclick=\""
        "const w = document.querySelector('x-wiz'); w.step2(); w.done2 = true;\">"
        "Continuar</button>",
    )

    async def scenario(p: Page) -> None:
        field = p.locator("x-wiz").locator("#u")
        await field.click()
        await p.keyboard.type("Ana Souza")
        await p.get_by_role("button", name="Continuar").click()
        await field.click()
        await p.keyboard.type(code)
        await leave_to_the_iframe(p)

    recording = await record_site(
        browser, {"/cadastro": html, "/quadro": QUADRO_VER}, "/cadastro", scenario
    )
    assert not has_text(recording, code)
    codes = [e for e in inputs_of(recording) if e.element.label == "Código de verificação"]
    assert codes and all(e.element.is_one_time_code and e.value is None for e in codes)
    result = compile_(recording)
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_nome_completo": an_input("nome_completo"),
        "digitar_codigo_de_verificacao": SecretValue(credential="banco_otp", field="totp"),
    }
    manifest, prompt = dumps(result)
    assert code not in manifest and code not in prompt


#: Máscara de componente que cancela o beforeinput de uma tecla sim, outra não, e põe
#: o caractere por script (sem evento input); troca o input no change.
XAMOUNT = """<script>
customElements.define('x-amt', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<label for="t">Valor</label><input id="t" data-gt="c">';
  const inp = r.getElementById('t');
  let n = 0;
  inp.addEventListener('beforeinput', (e) => {
    if (e.inputType === 'insertText' && (n++ % 2 === 1)) {
      e.preventDefault(); inp.value = inp.value + e.data;
    }
  });
  r.addEventListener('change', (e) => {
    const t = e.composedPath()[0];
    if (!t || t.tagName !== 'INPUT') return;
    const s = document.createElement('span'); s.textContent = t.value; t.replaceWith(s);
    fetch('/api/valor', {method: 'POST', body: t.value});
  }, true);
} });
</script>"""


async def test_a_component_mask_that_types_by_script_keeps_the_last_key(browser: Browser) -> None:
    # W9s: a pessoa digita 1234 no ritmo de uma pessoa (120 ms por tecla) e sai para o
    # iframe; o componente tira o input da página no change. O valor guardado é o de
    # depois de cada tecla (no beforeinput ele fica desconhecido). Antes, saía 123.
    html = page(
        "Grade",
        XAMOUNT
        + "<h1>Grade</h1>"
        + QUADRO_IFRAME
        + "<div style='height:40px'><x-amt></x-amt></div>",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-amt").locator("#t").click()
        await p.keyboard.type("1234", delay=120)
        await leave_to_the_iframe(p)

    recording = await record_site(
        browser, {"/grade": html, "/quadro": QUADRO_VER}, "/grade", scenario
    )
    assert [(e.element.label, e.value) for e in inputs_of(recording)] == [("Valor", "1234")]
    result = compile_(recording)
    found = fill_steps(result)
    assert [(s.id, s.value) for s in found] == [("digitar_valor", TextValue(template="1234"))]
    assert await marks_of(browser, html, found) == ["c"]


#: Editor rico da luz: cancela o beforeinput e põe o texto por script (sem input); no
#: blur, grava e troca o editor por um novo.
LIGHT_EDITOR = (
    "<h1>Notas</h1>"
    + QUADRO_IFRAME
    + """
<div id="wrap"><div id="ed" contenteditable="true" aria-label="Nota"
  style="min-height:30px;border:1px solid"></div></div>
<script>
function wire(ed) {
  ed.addEventListener('beforeinput', (e) => {
    if (e.inputType === 'insertText') {
      e.preventDefault(); ed.textContent = ed.textContent + e.data;
      const r = document.createRange(); r.selectNodeContents(ed); r.collapse(false);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }
  });
  ed.addEventListener('blur', () => {
    fetch('/api/nota', {method: 'POST', body: ed.textContent});
    const fresh = document.createElement('div'); fresh.id = 'ed'; fresh.contentEditable = 'true';
    fresh.setAttribute('aria-label', 'Nota'); fresh.style.cssText = ed.style.cssText;
    fresh.textContent = ed.textContent; ed.replaceWith(fresh); wire(fresh);
  });
}
wire(document.getElementById('ed'));
</script>"""
)


async def test_a_light_editor_replaced_on_blur_never_gets_a_value_one_key_behind(
    browser: Browser,
) -> None:
    # W9i: o editor da luz some no próprio blur quando a pessoa sai para o iframe (sem
    # change nem focusout). O caminho do blur da janela vale só para campos de shadow
    # roots: o valor deste se perde (falha fechada, como antes da rodada 9), nunca sai
    # uma tecla atrás ("Pagar 123", como na rodada 9).
    html = page("Notas", LIGHT_EDITOR)

    async def scenario(p: Page) -> None:
        await p.locator("#ed").click()
        await p.keyboard.type("Pagar 1234")
        await leave_to_the_iframe(p)

    recording = await record_site(
        browser, {"/notas": html, "/quadro": QUADRO_VER}, "/notas", scenario
    )
    # O texto da página (o editor mostra "Pagar 1234") vai na gravação; o valor, não.
    assert inputs_of(recording) == []
    assert fill_steps(compile_(recording)) == []


#: Assinatura de componente que mostra o texto enquanto a pessoa digita e vira
#: type=password no próprio blur; o componente para o change no shadow root.
XPIN = """<script>
customElements.define('x-pin', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.addEventListener('change', (e) => e.stopImmediatePropagation(), true);
  r.innerHTML = '<label for="s">Assinatura eletrônica</label><input id="s">';
  const inp = r.getElementById('s');
  inp.addEventListener('blur', () => { inp.type = 'password'; });
  inp.addEventListener('focus', () => { inp.type = 'text'; });
} });
</script>"""


@pytest.mark.parametrize("leave", ["iframe", "botao"])
async def test_a_signature_that_becomes_a_password_on_blur_stays_secret(
    browser: Browser, leave: str
) -> None:
    # W10a (sai para o iframe) e W10b (clica no botão): no blur e no pointerdown o
    # gravador só guarda o campo e decide num setTimeout, depois dos ouvintes de blur
    # do componente. O campo que virou type=password só sai como segredo. Antes, o
    # valor saía como texto no blur na captura (W10a) ou no pointerdown (W10b).
    secret = "7531"
    html = page(
        "Transferência",
        XPIN
        + "<h1>Confirmar transferência</h1>"
        + QUADRO_IFRAME
        + "<div style='height:40px'><x-pin></x-pin></div>"
        "<button id='b' onclick=\"fetch('/api/transferir', {method: 'POST', body: '{}'})"
        ".then(() => document.body.insertAdjacentHTML('beforeend', '<p>Ok</p>'))\">"
        "Transferir</button>",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-pin").locator("#s").click()
        await p.keyboard.type(secret)
        if leave == "iframe":
            await leave_to_the_iframe(p)
        await p.get_by_role("button", name="Transferir").click()
        await p.get_by_text("Ok").wait_for()

    recording = await record_site(
        browser, {"/transferencia": html, "/quadro": QUADRO_VER}, "/transferencia", scenario
    )
    assert not has_text(recording, secret)
    (assinatura,) = inputs_of(recording)
    assert assinatura.element.sensitive and assinatura.element.is_password
    assert assinatura.value is None
    result = compile_(recording)
    assert [(s.id, s.value) for s in fill_steps(result)] == [
        ("digitar_assinatura_eletronica", SecretValue(credential="banco", field="password"))
    ]
    assert click_effect(result, "clicar_transferir") is Effect.IRREVERSIBLE
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


#: O contrário: type=password enquanto a pessoa digita, texto no próprio blur.
XPIN_SHOWN = XPIN.replace(
    "inp.type = 'password'; });\n  inp.addEventListener('focus', () => { inp.type = 'text'; });",
    "inp.type = 'text'; });\n  inp.addEventListener('focus', () => { inp.type = 'password'; });",
)


@pytest.mark.parametrize("leave", ["iframe", "botao"])
async def test_a_signature_that_shows_the_text_on_blur_stays_secret(
    browser: Browser, leave: str
) -> None:
    # As marcas de sigilo só sobem: o retrato de depois de cada tecla (type=password)
    # conta quando o gravador decide depois do blur do componente, que já mostrou o
    # texto. O valor só sai como segredo.
    assert XPIN_SHOWN != XPIN
    secret = "9753"
    html = page(
        "Transferência",
        XPIN_SHOWN
        + "<h1>Confirmar transferência</h1>"
        + QUADRO_IFRAME
        + "<div style='height:40px'><x-pin></x-pin></div>"
        "<button id='b' onclick=\"fetch('/api/transferir', {method: 'POST', body: '{}'})"
        ".then(() => document.body.insertAdjacentHTML('beforeend', '<p>Ok</p>'))\">"
        "Transferir</button>",
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-pin").locator("#s").click()
        await p.keyboard.type(secret)
        if leave == "iframe":
            await leave_to_the_iframe(p)
        await p.get_by_role("button", name="Transferir").click()
        await p.get_by_text("Ok").wait_for()

    recording = await record_site(
        browser, {"/transferencia": html, "/quadro": QUADRO_VER}, "/transferencia", scenario
    )
    assert not has_text(recording, secret)
    (assinatura,) = inputs_of(recording)
    assert assinatura.element.sensitive and assinatura.element.is_password
    assert assinatura.value is None
    manifest, prompt = dumps(compile_(recording))
    assert secret not in manifest and secret not in prompt


#: "Pagar" que a página recria no próprio clique, sem trava: cada clique grava.
PAY_UNGUARDED = """<h1>Boleto</h1><div id="box"></div><script>
const render = () => {
  document.getElementById('box').innerHTML =
    '<div><div><div><div><div><button id="pg">Pagar</button></div></div></div></div></div>';
  document.getElementById('pg').onclick = () => {
    fetch('/api/pagar', {method: 'POST', body: '{}'})
      .then(() => document.body.insertAdjacentHTML('beforeend', '<p>Pago</p>'));
    render();
  };
};
render();
</script>"""


async def double_click(p: Page, css: str) -> None:
    """Clique duplo de uma pessoa: dois cliques no mesmo ponto, o segundo com detail 2."""
    box = await p.locator(css).bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await p.mouse.click(x, y, click_count=2, delay=20)


async def test_a_double_click_on_an_unguarded_button_the_page_recreates_is_one_click(
    browser: Browser,
) -> None:
    # W11: a pessoa clica duas vezes no "Pagar" por hábito; a página recria o botão no
    # clique, sem trava, e cada clique paga. Os dois botões só diferem no número do
    # elemento: um passo só (na rodada 9, dois passos irreversíveis, e o robô pagava
    # duas vezes em cada execução).
    html = page("Boleto", PAY_UNGUARDED)

    async def scenario(p: Page) -> None:
        await double_click(p, "#pg")
        await p.get_by_text("Pago").first.wait_for()
        await p.wait_for_timeout(400)

    recording = await record_site(browser, {"/boleto": html}, "/boleto", scenario)
    clicks = [e for e in recording.events if isinstance(e, ClickEvent)]
    assert [c.detail for c in clicks] == [1, 2]
    assert clicks[0].element.serial != clicks[1].element.serial
    paid = [e for e in recording.events if isinstance(e, RequestEvent) and e.method == "POST"]
    assert len(paid) == 2
    result = compile_(recording)
    steps = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [(s.id, s.effect) for s in steps] == [("clicar_pagar", Effect.IRREVERSIBLE)]


LOGIN_UNGUARDED = """<h1>Entrar</h1>
<label>Usuário <input id="u" name="usuario" autocomplete="username"></label>
<label>Senha <input id="p" type="password"></label><div id="box"></div>
<script>
const render = () => {
  document.getElementById('box').innerHTML =
    '<div><div><div><div><div><button id="en">Entrar</button></div></div></div></div></div>';
  document.getElementById('en').onclick = () => {
    fetch('/api/login', {method: 'POST', body: '{}'})
      .then(() => setTimeout(() => { location.href = '/inicio'; }, 400));
    render();
  };
};
render();
</script>"""


async def test_a_double_click_on_a_login_button_the_page_recreates_is_one_click(
    browser: Browser,
) -> None:
    # W12: o mesmo no "Entrar" de um login: um clique só, o login (leitura). Na rodada
    # 9, um segundo passo clicar_entrar_2 (gravando) clicava de novo depois de entrar.
    html = page("Login", LOGIN_UNGUARDED)
    inicio = page("Início", "<h1>Início</h1><p>Sua conta</p>")

    async def scenario(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER)
        await p.locator("#p").click()
        await p.keyboard.type(PASSWORD)
        await double_click(p, "#en")
        await p.get_by_role("heading", name="Início").wait_for()

    recording = await record_site(browser, {"/login": html, "/inicio": inicio}, "/login", scenario)
    assert not has_text(recording, USER, ignore_case=True)
    assert not has_text(recording, PASSWORD)
    clicks = [e for e in recording.events if isinstance(e, ClickEvent)]
    assert [c.detail for c in clicks] == [1, 2]
    logins = [e for e in recording.events if isinstance(e, RequestEvent) and e.method == "POST"]
    assert len(logins) == 2
    result = compile_(recording)
    steps = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [(s.id, s.effect) for s in steps] == [("clicar_entrar", Effect.READ)]
    assert usernames_in(result) == ["digitar_usuario"]
