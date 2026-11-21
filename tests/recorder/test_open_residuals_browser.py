"""Resíduos abertos do gravador (verificação da rodada 11), gravados num Chromium de verdade.

R1: a senha que a pessoa mostra com o "olho" antes de pôr o foco no campo, a que a
página mostra no próprio ``focus`` do campo (antes do ``focusin`` do gravador, também
com o foco pelo teclado), a que o "olho" mostra tirando a máscara por CSS e a do
"olho" que troca o ``<input>`` de senha por outro, de texto, só saem como segredo e
viram uma entrada do robô.
R2: com o foco automático na senha, a senha mostrada e digitada e o usuário digitado
depois, o usuário segue as mesmas regras do usuário digitado antes da senha.
R3: "Código de segurança do pedido" e ``name="codigo"`` continuam sigilosos, mas nunca
viram o código do cofre sem um login confirmado: o robô pede o valor numa entrada.
R4: o campo com ``autocomplete=username`` só vira o usuário do cofre num login
confirmado; sem ele, vira uma entrada.
R5: o componente que troca o rótulo no próprio blur (ou no ``pointerdown`` do botão)
e tira o campo da página não leva o valor como texto: o gravador relê, na hora de
decidir, o rótulo que o campo tinha.

Verificação da rodada G1: a senha mostrada pelo input que entra antes de a senha sair,
pelo invólucro recriado, pelo gêmeo de texto, numa página com mais de 6000 elementos,
pelo "olho" que redesenha o formulário e com o ``observe`` trocado pela página; o
``aria-labelledby`` que o componente aponta para outro elemento; os códigos de rótulo ou
nome genérico; o código num ``type=password``; o código ambíguo só depois de um login
com senha; e a SPA que troca o login por uma busca no mesmo contêiner.

Verificação da rodada G2: o "olho" que redesenha um formulário com mais de 64 campos ou
um formulário pequeno sem id nem name estáveis, a SPA que troca o login por outra tela de
dois campos (perda de disponibilidade aceita), a senha nova e o código sem rótulo.
"""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Browser, Page, async_playwright

from rapa.compiler import CompileResult
from rapa.core.robot import FillStep, SecretValue, TextValue
from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.recorder import InputEvent, Recording
from tests.recorder.test_login_rule_browser import (
    PASSWORD,
    USER,
    an_input,
    compile_,
    dumps,
    fill_value,
    input_of,
    page,
    record_site,
    usernames_in,
)
from tests.recorder.texts import has_text

pytestmark = pytest.mark.browser

OK = ".then(() => document.body.insertAdjacentHTML('beforeend', '<p>Ok</p>'))"


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


def inputs_of(recording: Recording) -> list[InputEvent]:
    return [e for e in recording.events if isinstance(e, InputEvent)]


def fill_steps(result: CompileResult) -> list[FillStep]:
    return [s for s in result.manifest.steps if isinstance(s, FillStep)]


# -- R1: a senha mostrada antes do foco, no foco ou trocada por outro input ------------

#: O "olho" que alterna o type do campo.
EYE_TYPE = (
    "<button id='olho' type='button' onclick=\"const p = document.getElementById('p');"
    " p.type = p.type === 'password' ? 'text' : 'password'\">Mostrar</button>"
)
#: O "olho" que tira a máscara por CSS de um campo de texto.
EYE_MASK = (
    "<button id='olho' type='button' onclick=\"document.getElementById('p')"
    ".classList.toggle('m')\">Mostrar</button>"
)
#: O "olho" que troca o input de senha por um novo, de texto, com o mesmo id e valor
#: (como um v-if/v-else).
EYE_SWAP = """<button id='olho' type='button'>Mostrar</button><script>
document.getElementById('olho').addEventListener('click', () => {
  const old = document.getElementById('p');
  const n = document.createElement('input');
  n.id = 'p'; n.type = old.type === 'password' ? 'text' : 'password'; n.value = old.value;
  old.replaceWith(n);
});
</script>"""
#: A página mostra a senha no próprio focus do campo e a esconde no blur.
SHOW_ON_FOCUS = """<script>
const p = document.getElementById('p');
p.addEventListener('focus', () => { p.type = 'text'; });
p.addEventListener('blur', () => { p.type = 'password'; });
</script>"""


def reveal_page(reveal: str) -> str:
    """Senha "Palavra-passe" (o rótulo não fala em senha) mostrada como ``reveal`` diz."""
    field = "<input id='p' type='password'>"
    extra = ""
    if reveal == "olho_antes_do_foco":
        extra = EYE_TYPE
    elif reveal == "mascara_por_css":
        field = "<style>.m { -webkit-text-security: disc; }</style><input id='p' class='m'>"
        extra = EYE_MASK
    elif reveal == "troca_o_input":
        field = "<span id='slot'><input id='p' type='password'></span>"
        extra = EYE_SWAP
    return page(
        "Acesso",
        f"<h1>Acesso</h1><label for='p'>Palavra-passe</label>{field}"
        "<button id='ok' onclick=\"fetch('/api/confirmar', {method: 'POST', body: '{}'})"
        + OK
        + '">Confirmar</button>'
        + (SHOW_ON_FOCUS if reveal.startswith("no_foco") else extra),
    )


@pytest.mark.parametrize(
    "reveal",
    [
        "olho_antes_do_foco",
        "mascara_por_css",
        "no_foco",
        "no_foco_pelo_teclado",
        "troca_o_input",
    ],
)
async def test_a_password_revealed_before_the_focus_never_leaves_as_text(
    browser: Browser, reveal: str
) -> None:
    # R1: a pessoa mostra a senha com o "olho" antes de pôr o foco no campo (o type
    # muda, ou a máscara por CSS sai), a página a mostra no próprio focus (antes do
    # focusin do gravador, pelo mouse ou pelo Tab), ou o "olho" troca o input de senha
    # por outro, de texto. No foco e no envio o campo é um texto com o rótulo
    # "Palavra-passe": antes, o valor saía como texto. O campo que já foi senha nesta
    # página só sai como segredo e vira uma entrada (nunca a senha do cofre).
    secret = "Qw7#zz19"

    async def scenario(p: Page) -> None:
        if reveal in ("olho_antes_do_foco", "mascara_por_css", "troca_o_input"):
            await p.get_by_role("button", name="Mostrar").click()
        if reveal == "no_foco_pelo_teclado":
            await p.keyboard.press("Tab")
        else:
            await p.locator("#p").click()
        await p.keyboard.type(secret, delay=30)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/acesso": reveal_page(reveal)}, "/acesso", scenario)
    assert not has_text(recording, secret)
    (field,) = inputs_of(recording)
    assert field.element.sensitive and field.value is None
    result = compile_(recording)
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_palavra_passe": an_input("palavra_passe")
    }
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


# -- R2: o usuário digitado depois da senha mostrada -----------------------------------

LOGIN_AUTOFOCUS = page(
    "Entrar",
    "<h1>Entrar</h1><form><label for='u'>Usuário</label><input id='u' name='usuario'>"
    "<label for='p'>Palavra-passe</label><input id='p' type='password' autofocus>"
    + EYE_TYPE
    + "<button id='en' onclick=\"event.preventDefault(); "
    "fetch('/api/login', {method: 'POST', body: '{}'})" + OK + '">Entrar</button></form>',
)


@pytest.mark.parametrize("order", ["usuario_antes", "usuario_depois"])
async def test_a_username_typed_after_a_revealed_password_follows_the_username_rules(
    browser: Browser, order: str
) -> None:
    # R2: foco automático na senha; a pessoa a mostra com o "olho", digita a senha e só
    # depois o usuário. Quando o usuário sai, a senha já é um campo de texto
    # ("Palavra-passe"): antes, o grupo não tinha senha e o usuário saía como texto.
    # O campo que já foi senha conta como a senha do grupo: o usuário sai como o
    # usuário digitado antes da senha (só como segredo, e vira uma entrada).
    async def type_user(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=30)

    async def scenario(p: Page) -> None:
        await p.wait_for_timeout(200)
        if order == "usuario_antes":
            await type_user(p)
        await p.get_by_role("button", name="Mostrar").click()
        await p.locator("#p").click()
        await p.keyboard.type(PASSWORD, delay=30)
        if order == "usuario_depois":
            await type_user(p)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/entrar": LOGIN_AUTOFOCUS}, "/entrar", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    usuario = input_of(recording, "usuario")
    assert usuario.element.is_username and usuario.element.sensitive and usuario.value is None
    result = compile_(recording)
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_usuario": an_input("usuario"),
        "digitar_palavra_passe": an_input("palavra_passe"),
    }
    assert usernames_in(result) == []
    manifest, prompt = dumps(result)
    for secret in (USER, PASSWORD):
        assert secret not in manifest and secret not in prompt


# -- R3: códigos ambíguos nunca viram o código do cofre sem um login confirmado -------


@pytest.mark.parametrize(
    "field",
    [
        "<label for='c'>Código de segurança do pedido</label><input id='c' name='pedido'>",
        "<label for='c'>Código do pedido</label><input id='c' name='codigo'>",
    ],
    ids=["codigo_de_seguranca", "name_codigo"],
)
async def test_an_ambiguous_code_is_never_the_vault_code_without_a_confirmed_login(
    browser: Browser, field: str
) -> None:
    # R3: "Código de segurança do pedido" e name="codigo" contavam como código de uso
    # único: o robô digitaria o código do cofre no lugar do que a pessoa digitou. Na
    # dúvida, o valor continua sigiloso (só sai como segredo), mas o campo só vira o
    # código do cofre num login confirmado; aqui (uma busca), vira uma entrada.
    value = "PED-4821"
    html = page(
        "Pedidos",
        f"<h1>Pedidos</h1><form onsubmit='return false'>{field}"
        "<button id='b' onclick=\"fetch('/api/pedidos')" + OK + '">Buscar</button></form>',
    )

    async def scenario(p: Page) -> None:
        await p.locator("#c").click()
        await p.keyboard.type(value, delay=30)
        await p.get_by_role("button", name="Buscar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/pedidos": html}, "/pedidos", scenario)
    assert not has_text(recording, value)
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert value not in manifest and value not in prompt


# -- R4: autocomplete=username só vira o usuário do cofre num login confirmado --------


@pytest.mark.parametrize("variant", ["sem_senha", "botao_ok", "login"])
async def test_a_declared_username_is_the_vault_username_only_in_a_confirmed_login(
    browser: Browser, variant: str
) -> None:
    # R4: a página declara o campo como usuário (autocomplete=username). Antes, ele
    # virava o usuário do cofre mesmo sem um login confirmado (a página de recuperar o
    # acesso, sem senha; o envio "OK", que não é de login). Agora só o login
    # confirmado ("Entrar") o liga ao cofre; fora dele, o valor (que não sai da
    # gravação) vira uma entrada.
    user = "<label for='u'>Usuário</label><input id='u' name='usuario' autocomplete='username'>"
    secret = (
        ""
        if variant == "sem_senha"
        else "<label for='p'>Senha</label><input id='p' type='password'>"
    )
    button = {"sem_senha": "Enviar link", "botao_ok": "OK", "login": "Entrar"}[variant]
    html = page(
        "Acesso",
        f"<h1>Acesso</h1><form>{user}{secret}<button id='b' onclick=\"event.preventDefault(); "
        "fetch('/api/acesso', {method: 'POST', body: '{}'})" + OK + f'">{button}</button></form>',
    )

    async def scenario(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=30)
        if variant != "sem_senha":
            await p.locator("#p").click()
            await p.keyboard.type(PASSWORD, delay=30)
        await p.get_by_role("button", name=button).click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/acesso": html}, "/acesso", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    result = compile_(recording)
    if variant == "login":
        assert usernames_in(result) == ["digitar_usuario"]
        assert fill_value(result, "digitar_usuario") == SecretValue(
            credential="banco", field="username"
        )
    else:
        assert usernames_in(result) == []
        assert fill_value(result, "digitar_usuario") == an_input("usuario")
    manifest, prompt = dumps(result)
    assert USER not in manifest and USER not in prompt


# -- R5: o rótulo que o componente troca no próprio blur ------------------------------


def relabel_component(label: str, on_blur: str) -> str:
    """Componente que para o change no shadow root e, no blur do campo, roda ``on_blur``."""
    return (
        """<script>
customElements.define('x-val', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.addEventListener('change', (e) => e.stopImmediatePropagation(), true);
  r.innerHTML = '<label id="lb" for="v">"""
        + label
        + """</label><input id="v">';
  const inp = r.getElementById('v');
  const lb = r.getElementById('lb');
  inp.addEventListener('blur', () => { """
        + on_blur
        + """ });
} });
</script>"""
    )


#: O componente tira o campo da página no blur (vira um texto).
GONE = "const c = document.createElement('span'); c.textContent = inp.value; inp.replaceWith(c);"


@pytest.mark.parametrize(
    ("label", "on_blur", "on_pointerdown"),
    [
        ("Valor", "lb.textContent = 'Assinatura eletrônica'; " + GONE, ""),
        ("Valor", GONE, "lb.textContent = 'Assinatura eletrônica';"),
        ("Valor", "lb.textContent = 'Código de verificação'; " + GONE, ""),
        ("Valor", "lb.textContent = 'Assinatura eletrônica';", ""),
        ("Senha", "lb.textContent = 'Valor'; " + GONE, ""),
    ],
    ids=[
        "rotulo_no_blur",
        "rotulo_no_pointerdown",
        "codigo_no_blur",
        "rotulo_no_blur_sem_tirar",
        "rotulo_que_deixa_de_ser_senha",
    ],
)
async def test_a_label_the_component_swaps_on_blur_never_leaves_the_value_as_text(
    browser: Browser, label: str, on_blur: str, on_pointerdown: str
) -> None:
    # R5: o componente troca o rótulo do campo no próprio blur (ou no pointerdown do
    # botão, antes do blur) e tira o campo da página. O retrato que o gravador tirou
    # (depois da última tecla e ao guardar o campo) tem o rótulo de antes, e o campo
    # solto não tem mais rótulo: antes, o valor saía como texto. Na hora de decidir, o
    # gravador relê o rótulo que o campo tinha (o mesmo <label>): se agora fala em
    # senha, código ou assinatura, o valor só sai como segredo e vira uma entrada. As
    # marcas só sobem: o rótulo "Senha" que vira "Valor" continua senha.
    typed = "7319"
    pointerdown = (
        "<script>document.getElementById('b').addEventListener('pointerdown', () => {"
        " const lb = document.querySelector('x-val').shadowRoot.getElementById('lb'); "
        + on_pointerdown
        + " });</script>"
        if on_pointerdown
        else ""
    )
    html = page(
        "Pagamento",
        relabel_component(label, on_blur)
        + "<h1>Pagamento</h1><div style='height:40px'><x-val></x-val></div>"
        "<button id='b' onclick=\"fetch('/api/confirmar', {method: 'POST', body: '{}'})"
        + OK
        + '">Confirmar</button>'
        + pointerdown,
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-val").locator("#v").click()
        await p.keyboard.type(typed, delay=60)
        await p.wait_for_timeout(200)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/pagar": html}, "/pagar", scenario)
    assert not has_text(recording, typed)
    (field,) = inputs_of(recording)
    assert field.element.sensitive and field.value is None
    result = compile_(recording)
    (step,) = fill_steps(result)
    if label == "Senha":
        assert step.value == SecretValue(credential="banco", field="password")
    else:
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
        assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert typed not in manifest and typed not in prompt


# -- verificação da rodada G1: outros caminhos da senha mostrada ----------------------

#: O botão que confirma (fetch POST) e escreve "Ok" na página.
CONFIRM = (
    "<button id='ok' onclick=\"fetch('/api/confirmar', {method: 'POST', body: '{}'})"
    + OK
    + '">Confirmar</button>'
)


def eye_page(field: str, script: str = "", before: str = "") -> str:
    """Página com ``field``, o "olho" (``script`` o liga) e o botão que confirma."""
    return page(
        "Acesso",
        f"{before}<h1>Acesso</h1>{field}<button id='olho' type='button'>Mostrar</button>"
        f"{CONFIRM}<script>{script}</script>",
    )


#: Como um <transition> do Vue: o input de texto entra antes, e a senha sai depois, em
#: outra tarefa (o registro da saída não tem a entrada).
EYE_ARRIVES_FIRST = eye_page(
    "<label for='p'>Palavra-passe</label><span id='slot'><input id='p' type='password'></span>",
    """document.getElementById('olho').addEventListener('click', () => {
  const old = document.getElementById('p');
  const n = document.createElement('input'); n.type = 'text'; n.value = old.value;
  old.removeAttribute('id'); n.id = 'p';
  old.parentNode.insertBefore(n, old);
  setTimeout(() => old.remove(), 150);
});""",
)
#: O "olho" recria o invólucro do input (sem id nem name): o pai da senha que saiu é o
#: invólucro velho, fora da página.
EYE_NEW_WRAPPER = eye_page(
    "<label>Palavra-passe</label><div id='box'><div class='w'>"
    "<input class='pw' type='password'></div></div>",
    """document.getElementById('olho').addEventListener('click', () => {
  const box = document.getElementById('box');
  const t = box.querySelector('input').type === 'password' ? 'text' : 'password';
  box.innerHTML = "<div class='w'><input class='pw' type='" + t + "'></div>";
});""",
)
#: O plugin antigo de "mostrar senha": um gêmeo de texto escondido aparece, a senha some,
#: e o plugin copia o valor de um para o outro.
EYE_TWIN = eye_page(
    "<label for='pw'>Palavra-passe</label><input id='pw' type='password'>"
    "<input id='p' type='text' style='display:none'>",
    """document.getElementById('olho').addEventListener('click', () => {
  const a = document.getElementById('pw'), b = document.getElementById('p');
  b.value = a.value; a.style.display = 'none'; b.style.display = '';
});
document.getElementById('p').addEventListener('input', (e) => {
  document.getElementById('pw').value = e.target.value;
});""",
)
#: Página com mais de 6000 elementos e a máscara por CSS que o "olho" tira.
EYE_BIG_PAGE = eye_page(
    "<style>.m { -webkit-text-security: disc; }</style>"
    "<label for='p'>Palavra-passe</label><input id='p' class='m'>",
    "document.getElementById('olho').addEventListener('click', () => "
    "document.getElementById('p').classList.toggle('m'));",
    before="<div style='display:none'>" + "<span>x</span>" * 6500 + "</div>",
)


@pytest.mark.parametrize(
    ("html", "selector"),
    [
        (EYE_ARRIVES_FIRST, "#p"),
        (EYE_NEW_WRAPPER, ".pw"),
        (EYE_TWIN, "#p"),
        (EYE_BIG_PAGE, "#p"),
    ],
    ids=["texto_entra_antes", "involucro_novo", "gemeo_de_texto", "pagina_grande"],
)
async def test_a_password_the_eye_shows_in_other_ways_never_leaves_as_text(
    browser: Browser, html: str, selector: str
) -> None:
    # Verificação da rodada G1: a senha mostrada ainda saía como texto quando o input de
    # texto entrava antes de a senha sair (numa tarefa depois, como um <transition>),
    # quando o "olho" recriava o invólucro do input sem id nem name, quando um plugin
    # mostrava um gêmeo de texto e escondia o type=password, e numa página com mais de
    # 6000 elementos (a máscara por CSS nunca era vista). Agora o campo leva a marca de
    # que já foi senha: só sai como segredo e vira uma entrada.
    secret = "Qw7#zz19"

    async def scenario(p: Page) -> None:
        await p.get_by_role("button", name="Mostrar").click()
        # A senha velha já saiu quando a pessoa digita (o campo novo está sozinho).
        await p.wait_for_timeout(600)
        await p.locator(selector).click()
        await p.keyboard.type(secret, delay=30)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/acesso": html}, "/acesso", scenario)
    assert not has_text(recording, secret)
    (field,) = inputs_of(recording)
    assert field.element.sensitive and field.element.was_password and field.value is None
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


#: O "olho" redesenha o formulário pequeno inteiro (o usuário e a senha, com id e name
#: que não falam em senha): o registro tira os dois campos de uma vez.
EYE_REDRAWS_THE_FORM = page(
    "Entrar",
    "<h1>Entrar</h1><div id='f'><label>Usuário</label><input id='u' name='usuario'>"
    "<label>Palavra-passe</label><input id='k' name='chave' type='password'></div>"
    "<button id='olho' type='button'>Mostrar</button>"
    "<button id='en' onclick=\"fetch('/api/login', {method: 'POST', body: '{}'})"
    + OK
    + """">Entrar</button><script>
document.getElementById('olho').addEventListener('click', () => {
  const f = document.getElementById('f');
  const user = f.querySelector('#u').value;
  f.innerHTML = "<label>Usuário</label><input id='u' name='usuario'>" +
    "<label>Palavra-passe</label><input id='k' name='chave' type='text'>";
  f.querySelector('#u').value = user;
});
</script>""",
)


async def test_an_eye_that_redraws_the_whole_form_never_leaves_the_password_as_text(
    browser: Browser,
) -> None:
    # Verificação da rodada G1: a regra da troca só conta pelo mesmo pai o registro que
    # tira um campo só (a SPA que troca o login inteiro por uma busca no mesmo contêiner
    # não marca a busca). O "olho" que redesenha o formulário pequeno inteiro tira os
    # dois campos de uma vez: o input novo ainda conta como a senha pelo mesmo id ou
    # name. Sem id nem name, ele não é visto (resíduo documentado).
    async def scenario(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=30)
        await p.get_by_role("button", name="Mostrar").click()
        await p.wait_for_timeout(300)
        await p.locator("#k").click()
        await p.keyboard.type(PASSWORD, delay=30)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/entrar": EYE_REDRAWS_THE_FORM}, "/entrar", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    chave = input_of(recording, "chave")
    assert chave.element.was_password and chave.element.sensitive and chave.value is None
    result = compile_(recording)
    assert fill_value(result, "digitar_chave") == an_input("chave")
    manifest, prompt = dumps(result)
    for secret in (USER, PASSWORD):
        assert secret not in manifest and secret not in prompt


#: Componente com a senha e o "olho" que troca o input (o mesmo id) no shadow root.
SHADOW_EYE = """<script>
customElements.define('x-pw', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = "<label for='p'>Palavra-passe</label><span id='slot'>" +
    "<input id='p' type='password'></span><button id='olho' type='button'>Mostrar</button>";
  r.getElementById('olho').addEventListener('click', () => {
    const old = r.getElementById('p');
    const n = document.createElement('input');
    n.id = 'p'; n.type = old.type === 'password' ? 'text' : 'password'; n.value = old.value;
    old.replaceWith(n);
  });
} });
</script>"""


@pytest.mark.parametrize("tampered", [False, True], ids=["observe_nativo", "observe_trocado"])
async def test_a_page_that_replaces_observe_never_hides_a_swap_in_a_shadow_root(
    browser: Browser, tampered: bool
) -> None:
    # Verificação da rodada G1: o gravador procurava o observe do MutationObserver na
    # hora de observar um shadow root; a página que troca o método do protótipo
    # (MutationObserver.prototype.observe = function () {}) escondia a troca do input de
    # senha no componente, e a senha mostrada saía como texto. Agora o método é guardado
    # antes dos scripts da página.
    secret = "S3nh@Forte"
    tamper = "<script>MutationObserver.prototype.observe = function () {};</script>"
    html = page(
        "Acesso",
        (tamper if tampered else "") + SHADOW_EYE + "<h1>Acesso</h1><x-pw></x-pw>" + CONFIRM,
    )

    async def scenario(p: Page) -> None:
        # O clique no "olho" passa pelo shadow root (o gravador passa a observá-lo) e o
        # mostra; a pessoa digita no input novo.
        await p.locator("x-pw").locator("#olho").click()
        await p.locator("x-pw").locator("#p").click()
        await p.keyboard.type(secret, delay=30)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/acesso": html}, "/acesso", scenario)
    assert not has_text(recording, secret)
    (field,) = inputs_of(recording)
    assert field.element.sensitive and field.value is None
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


LOGIN_NEW_WRAPPER = page(
    "Entrar",
    "<h1>Entrar</h1><div><label for='u'>Usuário</label><input id='u' name='usuario'></div>"
    "<label>Palavra-passe</label><div id='box'><div class='w'>"
    "<input class='pw' type='password' autofocus></div></div>"
    "<button id='olho' type='button'>Mostrar</button>"
    "<button id='en' onclick=\"fetch('/api/login', {method: 'POST', body: '{}'})"
    + OK
    + """">Entrar</button><script>
document.getElementById('olho').addEventListener('click', () => {
  const box = document.getElementById('box');
  const t = box.querySelector('input').type === 'password' ? 'text' : 'password';
  box.innerHTML = "<div class='w'><input class='pw' type='" + t + "'></div>";
});
</script>""",
)


async def test_a_username_typed_after_a_password_in_a_new_wrapper_never_leaves_as_text(
    browser: Browser,
) -> None:
    # Verificação da rodada G1 (R2 depois de R1): o "olho" recria o invólucro da senha
    # (sem id nem name), a pessoa digita a senha e só depois o usuário. Antes, a senha e
    # o usuário saíam como texto; agora a senha leva a marca de que já foi senha e conta
    # como a senha do grupo: os dois só saem como segredo e viram entradas.
    async def scenario(p: Page) -> None:
        await p.get_by_role("button", name="Mostrar").click()
        await p.wait_for_timeout(200)
        await p.locator(".pw").click()
        await p.keyboard.type(PASSWORD, delay=30)
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=30)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/entrar": LOGIN_NEW_WRAPPER}, "/entrar", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    usuario = input_of(recording, "usuario")
    assert usuario.element.is_username and usuario.element.sensitive and usuario.value is None
    result = compile_(recording)
    for step in fill_steps(result):
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert usernames_in(result) == []
    manifest, prompt = dumps(result)
    for secret in (USER, PASSWORD):
        assert secret not in manifest and secret not in prompt


# -- verificação da rodada G1: o aria-labelledby que o componente aponta para outro ----


def labelledby_component(inner: str, on_blur: str) -> str:
    """Componente que para o change no shadow root e, no blur do campo, roda ``on_blur``."""
    return (
        """<script>
customElements.define('x-val', class extends HTMLElement { connectedCallback() {
  if (this.shadowRoot) return;
  const r = this.attachShadow({mode: 'open'});
  r.addEventListener('change', (e) => e.stopImmediatePropagation(), true);
  r.innerHTML = `"""
        + inner
        + """`;
  const inp = r.getElementById('v');
  inp.addEventListener('blur', () => { """
        + on_blur
        + """ });
} });
</script>"""
    )


@pytest.mark.parametrize(
    ("inner", "on_blur"),
    [
        (
            "<span id='l1'>Valor</span><span id='l2'></span><input id='v' aria-labelledby='l1'>",
            "r.getElementById('l2').textContent = 'Assinatura eletrônica'; "
            "inp.setAttribute('aria-labelledby', 'l2'); " + GONE,
        ),
        (
            "<span id='l1'>Valor</span><input id='v' aria-labelledby='l1'>",
            "const n = document.createElement('span'); n.id = 'l1'; "
            "n.textContent = 'Assinatura eletrônica'; r.getElementById('l1').replaceWith(n); "
            + GONE,
        ),
    ],
    ids=["aponta_para_outro", "troca_o_alvo"],
)
async def test_an_aria_labelledby_the_component_repoints_on_blur_never_leaves_the_value_as_text(
    browser: Browser, inner: str, on_blur: str
) -> None:
    # Verificação da rodada G1 (R5): no próprio blur, o componente aponta o
    # aria-labelledby do campo para outro elemento ("Assinatura eletrônica"), ou troca o
    # elemento do rótulo por outro com o mesmo id, e tira o campo da página. O gravador
    # só relia o elemento guardado no retrato: o valor saía como texto. Agora ele guarda
    # os ids do aria-labelledby e os procura de novo (também o aria-labelledby de agora
    # do campo solto) na raiz do campo.
    typed = "7319"
    html = page(
        "Pagamento",
        labelledby_component(inner, on_blur)
        + "<h1>Pagamento</h1><div style='height:40px'><x-val></x-val></div>"
        "<button id='b' onclick=\"fetch('/api/confirmar', {method: 'POST', body: '{}'})"
        + OK
        + '">Confirmar</button>',
    )

    async def scenario(p: Page) -> None:
        await p.locator("x-val").locator("#v").click()
        await p.keyboard.type(typed, delay=60)
        await p.wait_for_timeout(200)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/pagar": html}, "/pagar", scenario)
    assert not has_text(recording, typed)
    (field,) = inputs_of(recording)
    assert field.element.sensitive and field.value is None
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert typed not in manifest and typed not in prompt


# -- verificação da rodada G1: códigos ---------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "<label for='c'>Código recebido por SMS</label>"
        "<input id='c' name='code' inputmode='numeric' maxlength='6'>",
        "<label for='c'>Digite o código enviado</label>"
        "<input id='c' name='otp_input' inputmode='numeric'>",
    ],
    ids=["recebido_por_sms", "name_otp_input"],
)
async def test_a_code_with_a_generic_label_or_name_never_leaves_as_text(
    browser: Browser, field: str
) -> None:
    # Verificação da rodada G1: "Código recebido por SMS" (name="code") e "Digite o código
    # enviado" (name="otp_input") não eram códigos de uso único, e o código saía como
    # texto. Agora o rótulo que fala do código enviado ou recebido (por SMS, e-mail,
    # celular ou WhatsApp) e o nome ou id com otp, totp, mfa ou 2fa como palavra os fazem
    # códigos: o valor só sai como segredo e, sem um sinal claro e fora de um login,
    # vira uma entrada (nunca o código do cofre).
    code = "482913"
    html = page(
        "Verificação",
        f"<h1>Verificação em duas etapas</h1><div>{field}"
        "<button id='b' onclick=\"fetch('/api/verificar', {method: 'POST', body: '{}'})"
        + OK
        + '">Verificar</button></div>',
    )

    async def scenario(p: Page) -> None:
        await p.locator("#c").click()
        await p.keyboard.type(code, delay=30)
        await p.get_by_role("button", name="Verificar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/verificar": html}, "/verificar", scenario)
    assert not has_text(recording, code)
    (event,) = inputs_of(recording)
    assert event.element.is_one_time_code and event.element.sensitive and event.value is None
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert code not in manifest and code not in prompt


@pytest.mark.parametrize(
    ("label", "name_attr", "expected"),
    [
        ("Token", "token", "entrada"),
        ("Código de verificação", "cv", "codigo"),
        ("Token da transação", "tk", "entrada"),
        ("Senha", "token", "senha"),
    ],
    ids=["token", "codigo_de_verificacao", "token_da_transacao", "rotulo_de_senha"],
)
async def test_a_code_typed_in_a_password_field_is_never_the_vault_password(
    browser: Browser, label: str, name_attr: str, expected: str
) -> None:
    # Verificação da rodada G1: o código num campo type=password ("Token", "Código de
    # verificação", "Token da transação") virava a senha do cofre (a marca de senha vinha
    # primeiro): o robô digitaria a senha do login no campo do código. Agora ele segue as
    # regras do código; o campo cujo rótulo fala em senha continua a senha.
    code = "482913"
    html = page(
        "Pix",
        "<h1>Pix</h1><label for='v'>Valor</label><input id='v' name='valor'>"
        f"<label for='c'>{label}</label><input id='c' name='{name_attr}' type='password'>"
        "<button id='b' onclick=\"fetch('/api/pix', {method: 'POST', body: '{}'})"
        + OK
        + '">Confirmar</button>',
    )

    async def scenario(p: Page) -> None:
        await p.locator("#v").click()
        await p.keyboard.type("10,00", delay=20)
        await p.locator("#c").click()
        await p.keyboard.type(code, delay=20)
        await p.get_by_role("button", name="Confirmar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/pix": html}, "/pix", scenario)
    assert not has_text(recording, code)
    event = input_of(recording, name_attr)
    assert event.element.is_password and event.element.is_one_time_code
    result = compile_(recording)
    (step,) = (s for s in fill_steps(result) if s.id != "digitar_valor")
    if expected == "senha":
        assert step.value == SecretValue(credential="banco", field="password")
    elif expected == "codigo":
        assert step.value == SecretValue(credential="banco_otp", field="totp")
    else:
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
        assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert code not in manifest and code not in prompt


def code_page(field: str, button: str) -> str:
    """Um <form> só com o código e o botão (fetch POST, sem navegar)."""
    return page(
        "Código",
        f'<h1>Verificação</h1><form>{field}<button onclick="event.preventDefault(); '
        "fetch('/api/codigo', {method: 'POST', body: '{}'})" + OK + f'">{button}</button></form>',
    )


@pytest.mark.parametrize("variant", ["pedido_continuar", "pedido_entrar", "segundo_fator"])
async def test_an_ambiguous_code_is_the_vault_code_only_after_a_password_login(
    browser: Browser, variant: str
) -> None:
    # Verificação da rodada G1 (R3): o <form> só com um código ambíguo (name="codigo") e um
    # botão de login conta como login confirmado; sozinho ("Código do pedido" com
    # "Continuar" ou "Entrar"), o robô digitava o código do cofre no lugar do que a
    # pessoa digitou. Agora o código ambíguo só é o do cofre num login com senha ou
    # depois de um: a página do segundo fator, depois do login, continua com ele.
    code = "482913"
    if variant == "segundo_fator":
        pages = {
            "/entrar": page(
                "Entrar",
                "<h1>Portal</h1><form action='/entrar' method='post'>"
                "<label for='u'>Usuário</label><input id='u' name='usuario'>"
                "<label for='p'>Senha</label><input id='p' name='senha' type='password'>"
                "<button>Entrar</button></form>",
            ),
            "/codigo": code_page(
                "<label for='c'>Código</label><input id='c' name='codigo' inputmode='numeric'>",
                "Confirmar",
            ),
        }
        button = "Confirmar"
    else:
        button = "Continuar" if variant == "pedido_continuar" else "Entrar"
        pages = {
            "/entrar": code_page(
                "<label for='c'>Código do pedido</label><input id='c' name='codigo'>", button
            )
        }

    async def scenario(p: Page) -> None:
        if variant == "segundo_fator":
            await p.locator("#u").click()
            await p.keyboard.type(USER, delay=20)
            await p.locator("#p").click()
            await p.keyboard.type(PASSWORD, delay=20)
            await p.get_by_role("button", name="Entrar").click()
            await p.get_by_role("heading", name="Verificação").wait_for()
        await p.locator("#c").click()
        await p.keyboard.type(code, delay=20)
        await p.get_by_role("button", name=button).click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(
        browser, pages, "/entrar", scenario, redirects={"/entrar": "/codigo"}
    )
    assert not has_text(recording, code)
    result = compile_(recording)
    (step,) = (s for s in fill_steps(result) if s.id.startswith("digitar_codigo"))
    if variant == "segundo_fator":
        assert step.value == SecretValue(credential="banco_otp", field="totp")
        assert fill_value(result, "digitar_senha") == SecretValue(
            credential="banco", field="password"
        )
    else:
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
        assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert code not in manifest and code not in prompt


# -- verificação da rodada G1: a SPA que troca o login por outra tela -----------------

SPA_LOGIN_THEN_SEARCH = page(
    "App",
    "<h1>App</h1><div id='app'><input id='u' name='usuario' placeholder='Usuário'>"
    "<input id='p' type='password' placeholder='Palavra-passe'><button id='en'>Entrar</button>"
    """</div><script>
document.getElementById('en').addEventListener('click', () => {
  fetch('/api/login', {method: 'POST', body: '{}'}).then(() => {
    document.getElementById('app').innerHTML = "<input id='q' placeholder='Buscar cliente'>" +
      "<button id='bq'>Buscar</button>";
    document.getElementById('bq').onclick = () => fetch('/api/clientes')"""
    + OK
    + """;
  });
});
</script>""",
)


async def test_a_spa_that_swaps_its_login_for_a_search_keeps_the_typed_search(
    browser: Browser,
) -> None:
    # Verificação da rodada G1 (perda de disponibilidade): a SPA troca o login (o usuário
    # e a senha, filhos diretos do contêiner) por uma busca no mesmo contêiner, em menos
    # de 1 s. A regra da troca do "olho" (o input que entra no mesmo pai de uma senha que
    # saiu) marcava a busca como senha, e o texto buscado virava uma entrada. Agora só
    # conta o registro que tira um input só (o "olho" tira uma senha; a SPA, o login
    # inteiro): a busca fica com o texto digitado.
    async def scenario(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=20)
        await p.locator("#p").click()
        await p.keyboard.type(PASSWORD, delay=20)
        await p.get_by_role("button", name="Entrar").click()
        await p.locator("#q").click()
        await p.keyboard.type("Maria", delay=20)
        await p.get_by_role("button", name="Buscar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/app": SPA_LOGIN_THEN_SEARCH}, "/app", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    result = compile_(recording)
    assert {s.id: s.value for s in fill_steps(result)} == {
        "digitar_usuario": SecretValue(credential="banco", field="username"),
        "digitar_palavra_passe": SecretValue(credential="banco", field="password"),
        "digitar_buscar_cliente": TextValue(template="Maria"),
    }


# -- verificação da rodada G2: o "olho" que redesenha o formulário ---------------------

#: O botão de login (fetch POST) que escreve "Ok" na página.
ENTRAR = (
    "<button id='en' onclick=\"fetch('/api/login', {method: 'POST', body: '{}'})"
    + OK
    + '">Entrar</button>'
)


def big_form(fields: int) -> str:
    """Um render() sem framework redesenha o formulário inteiro (a senha e ``fields`` campos)."""
    return page(
        "Cadastro",
        "<h1>Cadastro</h1><div id='app'></div><button id='olho' type='button'>Mostrar</button>"
        + ENTRAR
        + """<script>
let show = false;
function render() {
  const app = document.getElementById('app');
  const vals = {};
  app.querySelectorAll('input').forEach((i) => { vals[i.id] = i.value; });
  let h = "<label for='pw'>Palavra-passe</label><input id='pw' name='pw' type='" +
    (show ? 'text' : 'password') + "'>";
  for (let i = 0; i < """
        + str(fields)
        + """; i++) {
    h += "<label for='f" + i + "'>Campo " + i + "</label><input id='f" + i + "' name='f" + i + "'>";
  }
  app.innerHTML = h;
  app.querySelectorAll('input').forEach((i) => { if (vals[i.id] !== undefined) i.value = vals[i.id]; });
}
render();
document.getElementById('olho').addEventListener('click', () => { show = !show; render(); });
</script>""",
    )


async def test_an_eye_that_redraws_a_large_form_never_leaves_the_password_as_text(
    browser: Browser,
) -> None:
    # Verificação da rodada G2: o gravador guarda no máximo 64 campos que entraram na
    # página, e cortava a lista ANTES de procurar a senha que saiu. O "olho" que redesenha
    # um formulário de 71 campos (a senha primeiro, todos com id e name) punha o input de
    # texto da senha fora da lista: a senha digitada depois saía como texto, no robô e no
    # prompt. Agora a procura vem antes do corte.
    secret = "Qw7#zz19"

    async def scenario(p: Page) -> None:
        await p.get_by_role("button", name="Mostrar").click()
        await p.wait_for_timeout(300)
        await p.locator("#pw").click()
        await p.keyboard.type(secret, delay=20)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/cadastro": big_form(70)}, "/cadastro", scenario)
    assert not has_text(recording, secret)
    pw = input_of(recording, "pw")
    assert pw.element.was_password and pw.element.sensitive and pw.value is None
    result = compile_(recording)
    assert fill_value(result, "digitar_palavra_passe") == an_input("palavra_passe")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    assert secret not in manifest and secret not in prompt


#: O "olho" redesenha o formulário pequeno (o usuário e a senha) sem id nem name.
REDRAW_WITHOUT_IDS = page(
    "Entrar",
    "<h1>Entrar</h1><div id='f'><label>Usuário</label><input class='u'>"
    "<label>Palavra-passe</label><input class='p' type='password'></div>"
    "<button id='olho' type='button'>Mostrar</button>"
    + ENTRAR
    + """<script>
document.getElementById('olho').addEventListener('click', () => {
  const f = document.getElementById('f');
  const user = f.querySelector('.u').value;
  const t = f.querySelector('.p').type === 'password' ? 'text' : 'password';
  f.innerHTML = "<label>Usuário</label><input class='u'>" +
    "<label>Palavra-passe</label><input class='p' type='" + t + "'>";
  f.querySelector('.u').value = user;
});
</script>""",
)
#: O "olho" redesenha o formulário pequeno com ids novos a cada vez (in-1, in-2 viram
#: in-3, in-4), sem name.
REDRAW_WITH_NEW_IDS = page(
    "Entrar",
    "<h1>Entrar</h1><div id='f'></div><button id='olho' type='button'>Mostrar</button>"
    + ENTRAR
    + """<script>
let n = 0, show = false;
function render() {
  const f = document.getElementById('f');
  const old = f.querySelectorAll('input');
  const uv = old[0] ? old[0].value : '', pv = old[1] ? old[1].value : '';
  const a = 'in-' + (++n), b = 'in-' + (++n);
  f.innerHTML = "<label for='" + a + "'>Usuário</label><input id='" + a + "' class='u'>" +
    "<label for='" + b + "'>Palavra-passe</label><input id='" + b + "' class='p' type='" +
    (show ? 'text' : 'password') + "'>";
  const now = f.querySelectorAll('input'); now[0].value = uv; now[1].value = pv;
}
render();
document.getElementById('olho').addEventListener('click', () => { show = !show; render(); });
</script>""",
)
#: O "olho" esvazia o contêiner (textContent = '') e devolve o mesmo nó do usuário com
#: um input novo no lugar da senha: o registro da saída conta o usuário, que já voltou.
REDRAW_KEEPING_THE_USER = page(
    "Entrar",
    "<h1>Entrar</h1><div id='f'><label>Usuário</label><input class='u'>"
    "<label>Palavra-passe</label><input class='p' type='password'></div>"
    "<button id='olho' type='button'>Mostrar</button>"
    + ENTRAR
    + """<script>
document.getElementById('olho').addEventListener('click', () => {
  const f = document.getElementById('f');
  const [lu, u, lp, p] = f.children;
  const n = document.createElement('input');
  n.className = 'p'; n.type = p.type === 'password' ? 'text' : 'password'; n.value = p.value;
  f.textContent = '';
  f.append(lu, u, lp, n);
});
</script>""",
)


@pytest.mark.parametrize(
    "html",
    [REDRAW_WITHOUT_IDS, REDRAW_WITH_NEW_IDS, REDRAW_KEEPING_THE_USER],
    ids=["sem_id_nem_name", "ids_novos", "esvazia_e_devolve"],
)
async def test_an_eye_that_redraws_a_small_form_without_stable_ids_never_leaves_the_password_as_text(
    browser: Browser, html: str
) -> None:
    # Verificação da rodada G2: o registro que tira mais de um campo de uma vez só contava
    # pelo mesmo id ou name. O "olho" que redesenha o formulário pequeno (o usuário e a
    # senha) sem id nem name, com ids novos a cada vez, ou que esvazia o contêiner e
    # devolve o mesmo nó do usuário com um input novo, deixava a senha digitada depois
    # sair como texto. Agora vale também a posição: a senha que saiu na posição k de um
    # registro com N campos é o campo da posição k de um registro que pôs N campos no
    # mesmo lugar, em menos de 1 s.
    async def scenario(p: Page) -> None:
        await p.locator(".u").click()
        await p.keyboard.type(USER, delay=20)
        await p.get_by_role("button", name="Mostrar").click()
        await p.wait_for_timeout(300)
        await p.locator(".p").click()
        await p.keyboard.type(PASSWORD, delay=20)
        await p.get_by_role("button", name="Entrar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/entrar": html}, "/entrar", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    *_, shown = inputs_of(recording)
    assert shown.element.was_password and shown.element.sensitive and shown.value is None
    result = compile_(recording)
    for step in fill_steps(result):
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
    assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    for secret in (USER, PASSWORD):
        assert secret not in manifest and secret not in prompt


SPA_LOGIN_THEN_TWO_FIELDS = page(
    "App",
    "<h1>App</h1><div id='app'><input id='u' name='usuario' placeholder='Usuário'>"
    "<input id='p' type='password' placeholder='Palavra-passe'><button id='en'>Entrar</button>"
    """</div><script>
document.getElementById('en').addEventListener('click', () => {
  fetch('/api/login', {method: 'POST', body: '{}'}).then(() => {
    document.getElementById('app').innerHTML = "<input id='q' placeholder='Nome do cliente'>" +
      "<input id='c' placeholder='Cidade'><button id='bq'>Buscar</button>";
    document.getElementById('bq').onclick = () => fetch('/api/clientes')"""
    + OK
    + """;
  });
});
</script>""",
)


async def test_a_spa_that_swaps_its_login_for_a_two_field_view_treats_the_password_slot_as_secret(
    browser: Browser,
) -> None:
    # Verificação da rodada G2 (perda de disponibilidade aceita, falha fechada): a SPA
    # troca o login (o usuário e a senha) por outra tela de dois campos no mesmo
    # contêiner, em menos de 1 s. Pela regra da posição, o campo que entra no lugar da
    # senha pode ser a senha que o "olho" mostrou: ele leva a marca de que já foi senha,
    # e os dois campos da tela nova viram entradas (nunca um literal, nunca credencial do
    # cofre). O login continua com o usuário e a senha do cofre.
    async def scenario(p: Page) -> None:
        await p.locator("#u").click()
        await p.keyboard.type(USER, delay=20)
        await p.locator("#p").click()
        await p.keyboard.type(PASSWORD, delay=20)
        await p.get_by_role("button", name="Entrar").click()
        await p.locator("#q").click()
        await p.keyboard.type("Maria", delay=20)
        await p.locator("#c").click()
        await p.keyboard.type("Campinas", delay=20)
        await p.get_by_role("button", name="Buscar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/app": SPA_LOGIN_THEN_TWO_FIELDS}, "/app", scenario)
    assert not has_text(recording, USER) and not has_text(recording, PASSWORD)
    assert not has_text(recording, "Campinas")
    cidade = next(e for e in inputs_of(recording) if e.element.id == "c")
    assert cidade.element.was_password and cidade.element.sensitive and cidade.value is None
    result = compile_(recording)
    values = {s.id: s.value for s in fill_steps(result)}
    assert values["digitar_usuario"] == SecretValue(credential="banco", field="username")
    assert values["digitar_palavra_passe"] == SecretValue(credential="banco", field="password")
    assert values["digitar_cidade"] == an_input("cidade")
    assert values["digitar_nome_do_cliente"] == an_input("nome_do_cliente")
    assert [c.name for c in result.manifest.credentials] == ["banco"]


# -- verificação da rodada G2: a senha nova --------------------------------------------

#: A troca de senha: a senha atual e a senha nova duas vezes (autocomplete=new-password).
CHANGE_PASSWORD = page(
    "Trocar senha",
    "<h1>Trocar senha</h1>"
    "<label for='a'>Senha atual</label>"
    "<input id='a' name='atual' type='password' autocomplete='current-password'>"
    "<label for='n1'>Nova senha</label>"
    "<input id='n1' name='nova' type='password' autocomplete='new-password'>"
    "<label for='n2'>Confirme a nova senha</label>"
    "<input id='n2' name='repita' type='password' autocomplete='new-password'>"
    "<button id='s' onclick=\"fetch('/api/trocar', {method: 'POST', body: '{}'})"
    + OK
    + '">Salvar</button>',
)
#: O "olho" redesenha as duas senhas novas (com o mesmo name), sem a senha atual.
CHANGE_PASSWORD_EYE = page(
    "Trocar senha",
    "<h1>Trocar senha</h1><div id='f'></div><button id='olho' type='button'>Mostrar</button>"
    "<button id='s' onclick=\"fetch('/api/trocar', {method: 'POST', body: '{}'})"
    + OK
    + """">Salvar</button><script>
let show = false;
function render() {
  const f = document.getElementById('f');
  const v = Array.from(f.querySelectorAll('input')).map((i) => i.value);
  const t = show ? 'text' : 'password';
  f.innerHTML = "<label for='n1'>Nova senha</label><input id='n1' name='nova' type='" + t +
    "' autocomplete='new-password'><label for='n2'>Confirme a nova senha</label>" +
    "<input id='n2' name='repita' type='" + t + "' autocomplete='new-password'>";
  f.querySelectorAll('input').forEach((i, k) => { if (v[k] !== undefined) i.value = v[k]; });
}
render();
document.getElementById('olho').addEventListener('click', () => { show = !show; render(); });
</script>""",
)


@pytest.mark.parametrize(
    ("html", "with_current"),
    [(CHANGE_PASSWORD, True), (CHANGE_PASSWORD_EYE, False)],
    ids=["type_password", "olho_redesenha"],
)
async def test_a_new_password_typed_in_the_browser_is_never_the_vault_password(
    browser: Browser, html: str, with_current: bool
) -> None:
    # Verificação da rodada G2: a senha nova de uma troca de senha (autocomplete=
    # new-password), também mostrada pelo "olho" que redesenha os dois campos, virava a
    # senha do cofre: o robô digitaria a senha de agora onde a pessoa digitou outra. Agora
    # ela só sai como segredo e vira uma entrada; a senha atual continua a do cofre.
    current, new = "S3nh@Atual", "N0va#Senha9"

    async def scenario(p: Page) -> None:
        if with_current:
            await p.locator("#a").click()
            await p.keyboard.type(current, delay=20)
        else:
            await p.get_by_role("button", name="Mostrar").click()
            await p.wait_for_timeout(300)
        for selector in ("#n1", "#n2"):
            await p.locator(selector).click()
            await p.keyboard.type(new, delay=20)
        await p.get_by_role("button", name="Salvar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(browser, {"/trocar": html}, "/trocar", scenario)
    assert not has_text(recording, new) and not has_text(recording, current)
    result = compile_(recording)
    values = {s.id: s.value for s in fill_steps(result)}
    assert values["digitar_nova_senha"] == an_input("nova_senha")
    assert values["digitar_confirme_a_nova_senha"] == an_input("confirme_a_nova_senha")
    if with_current:
        assert values["digitar_senha_atual"] == SecretValue(credential="banco", field="password")
    else:
        assert result.manifest.credentials == []
    manifest, prompt = dumps(result)
    for secret in (current, new):
        assert secret not in manifest and secret not in prompt


# -- verificação da rodada G2: o código sem rótulo -------------------------------------


def unlabeled_field(text: str, attrs: str) -> str:
    """O campo sem rótulo, depois de um parágrafo (``text``), e o botão que verifica."""
    return page(
        "Verificação",
        f"<h1>Verificação</h1><p id='h'>{text}</p><input id='c' {attrs}>"
        "<button id='b' onclick=\"fetch('/api/verificar', {method: 'POST', body: '{}'})"
        + OK
        + '">Verificar</button>',
    )


SMS_TEXT = "Digite o código que enviamos por SMS para o seu celular."


@pytest.mark.parametrize(
    ("text", "attrs", "typed", "secret"),
    [
        (SMS_TEXT, "inputmode='numeric' maxlength='6' placeholder='000000'", "482913", True),
        (SMS_TEXT, "aria-describedby='h' name='c'", "482913", True),
        (
            "Digite o CEP do endereço de entrega.",
            "inputmode='numeric' maxlength='8'",
            "01310100",
            False,
        ),
    ],
    ids=["numerico", "aria_describedby", "cep"],
)
async def test_a_code_without_a_label_never_leaves_as_text(
    browser: Browser, text: str, attrs: str, typed: str, secret: bool
) -> None:
    # Verificação da rodada G2: o código de uso único num campo sem rótulo, que só o texto
    # da página diz ser um código ("Digite o código que enviamos por SMS", num parágrafo
    # logo antes do campo com cara de código ou no aria-describedby dele), saía como texto
    # na gravação, no robô e no prompt. Agora o valor só sai como segredo e vira uma
    # entrada, sem a marca de código: nunca o código do cofre, nem outra credencial. O
    # campo de CEP (o texto não fala em código) continua com o valor digitado.
    async def scenario(p: Page) -> None:
        await p.locator("#c").click()
        await p.keyboard.type(typed, delay=20)
        await p.get_by_role("button", name="Verificar").click()
        await p.get_by_text("Ok", exact=True).wait_for()

    recording = await record_site(
        browser, {"/verificar": unlabeled_field(text, attrs)}, "/verificar", scenario
    )
    (event,) = inputs_of(recording)
    assert not event.element.is_one_time_code
    result = compile_(recording)
    (step,) = fill_steps(result)
    assert result.manifest.credentials == []
    if secret:
        assert not has_text(recording, typed)
        assert event.element.sensitive and event.value is None
        assert isinstance(step.value, TextValue) and step.value.template.startswith("{{ inputs.")
        manifest, prompt = dumps(result)
        assert typed not in manifest and typed not in prompt
    else:
        assert event.value == typed
        assert step.value == TextValue(template=typed)
