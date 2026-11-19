"""Identidade dos campos e campos de shadow roots, sobre gravações montadas à mão.

Rodada 7 do gravador: dois componentes iguais (o mesmo ``<input id="input">``
dentro de cada shadow root) nunca viram o mesmo campo, edições, marcações e
escolhas de campos diferentes nunca se juntam num passo (o alvo e o valor sempre
vêm do mesmo campo), um campo de shadow root nunca é credencial de login, e só
códigos só autenticam com o botão num ``<form>`` só de códigos.
"""

import pytest

from rapa.compiler import CompileResult, lower
from rapa.core import policy
from rapa.core.robot import (
    CheckStep,
    ClickStep,
    Effect,
    FillStep,
    SecretValue,
    SelectStep,
    Step,
    TextValue,
)
from rapa.recorder.models import ElementDescriptor, FormInfo, NavigationCause, Recording, RowInfo
from rapa.recorder.sensitivity import (
    analyze_logins,
    credential_kind,
    declared_username,
    field_key,
    in_shadow_root,
    secret_page_inputs,
)
from tests.compiler.builders import (
    BASE,
    LOGIN_FORM,
    OTP_FORM,
    RecordingBuilder,
    button,
    checkbox,
    otp,
    password,
    select_box,
    textbox,
)

PASSWORD = SecretValue(credential="portal", field="password")
TOTP = SecretValue(credential="portal_otp", field="totp")
OTHER_OTP_FORM = FormInfo(action=f"{BASE}/outro", method="post", has_one_time_code=True)
TOKEN_AND_TEXT = FormInfo(
    action=f"{BASE}/token", method="post", has_one_time_code=True, text_fields=1
)


def compile_(recording: Recording) -> CompileResult:
    result = lower(recording, name="robo_teste", title="Robô de teste", description="Teste.")
    assert policy.check(result.manifest) == []
    return result


def step(result: CompileResult, step_id: str) -> Step:
    found = result.manifest.step_by_id(step_id)
    assert found is not None, [s.id for s in result.manifest.steps]
    return found


def fills(result: CompileResult) -> list[FillStep]:
    return [s for s in result.manifest.steps if isinstance(s, FillStep)]


def usernames_in(result: CompileResult) -> list[str]:
    return [
        s.id
        for s in fills(result)
        if isinstance(s.value, SecretValue) and s.value.field == "username"
    ]


def an_input(name: str) -> TextValue:
    return TextValue(template=f"{{{{ inputs.{name} }}}}")


def first_name(s: FillStep | CheckStep | SelectStep) -> str | None:
    return s.target.locators[0].name or s.target.locators[0].value


def in_component(el: ElementDescriptor, host: str, **update: object) -> ElementDescriptor:
    """O campo dentro do shadow root do componente ``host``: o que o script manda."""
    inner = el.css
    return el.model_copy(
        update={
            "shadow_path": [host],
            "css": f"{host} > {inner}",
            "id": "input",
            "name_attr": None,
            **update,
        }
    )


def round6(el: ElementDescriptor) -> ElementDescriptor:
    """O mesmo campo como o script da rodada 6 o mandava (sem hospedeiros)."""
    return el.model_copy(update={"css": "#input", "id": "input", "name_attr": None})


# -- identidade: nunca juntar campos diferentes --------------------------------------


def test_two_components_with_the_same_inner_id_and_label_are_two_fields() -> None:
    # Só o hospedeiro distingue: o mesmo rótulo, o mesmo id e o mesmo seletor interno.
    a = textbox("Valor", id="input", css="#input").model_copy(update={"shadow_path": ["#fa"]})
    b = textbox("Valor", id="input", css="#input").model_copy(update={"shadow_path": ["#fb"]})
    assert field_key(0, a) != field_key(0, b)
    rec = RecordingBuilder()
    rec.navigate("/pedido")
    rec.fill(a, "10")
    rec.fill(b, "25")
    rec.click(button("Salvar pedido", id="salvar"))
    rec.request("POST", "/api/pedido", "fetch")
    result = compile_(rec.build())
    values = [s.value for s in fills(result)]
    assert values == [TextValue(template="10"), TextValue(template="25")]
    assert step(result, "clicar_salvar_pedido").effect is Effect.WRITE


@pytest.mark.parametrize("with_host", [True, False], ids=["com_hospedeiro", "rodada6"])
def test_fields_that_only_share_the_selector_are_never_merged(with_host: bool) -> None:
    # C2 e C9: o mesmo "#input" em dois componentes. Sem o hospedeiro (gravação da
    # rodada 6), a defesa em profundidade: o que a pessoa vê do campo também conta.
    chave, valor = textbox("Chave Pix", css="#input"), textbox("Valor", css="#input")
    if with_host:
        chave, valor = in_component(chave, "#fk"), in_component(valor, "#fv")
    else:
        chave, valor = round6(chave), round6(valor)
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(chave, "11122233344")
    rec.fill(valor, "1250,00")
    rec.click(button("Continuar", id="b"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    found = fills(result)
    assert [(first_name(s), s.value) for s in found] == [
        ("Chave Pix", TextValue(template="11122233344")),
        ("Valor", TextValue(template="1250,00")),
    ]
    assert step(result, "clicar_continuar").effect is Effect.WRITE


@pytest.mark.parametrize("with_host", [True, False], ids=["com_hospedeiro", "rodada6"])
def test_checks_of_different_components_are_never_merged(with_host: bool) -> None:
    # C10/C11: caixas de seleção de componentes com o mesmo id interno, nas linhas.
    def box(row: str) -> ElementDescriptor:
        el = checkbox(f"Selecionar {row}", css="#input")
        return in_component(el, f"tr:nth-of-type({row[-1]}) x-check") if with_host else round6(el)

    rec = RecordingBuilder()
    rec.navigate("/boletos")
    rec.check(box("Boleto 1003"), True)
    rec.check(box("Boleto 1001"), False)
    rec.click(button("Remover selecionados", id="x"))
    rec.request("POST", "/api/excluir", "fetch")
    result = compile_(rec.build())
    checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    assert [(s.title, first_name(s), s.checked) for s in checks] == [
        ("Marcar “Selecionar Boleto 1003”", "Selecionar Boleto 1003", True),
        ("Desmarcar “Selecionar Boleto 1001”", "Selecionar Boleto 1001", False),
    ]


@pytest.mark.parametrize("with_host", [True, False], ids=["com_hospedeiro", "rodada6"])
def test_selects_of_different_components_are_never_merged(with_host: bool) -> None:
    origem = select_box("Conta de origem", css="#input")
    destino = select_box("Conta de destino", css="#input")
    if with_host:
        origem, destino = in_component(origem, "#fo"), in_component(destino, "#fd")
    else:
        origem, destino = round6(origem), round6(destino)
    rec = RecordingBuilder()
    rec.navigate("/transferir")
    rec.select(origem, "Corrente 1")
    rec.select(destino, "Poupança 2")
    rec.click(button("Transferir", id="b"))
    rec.request("POST", "/api/transferir", "fetch")
    result = compile_(rec.build())
    selects = [s for s in result.manifest.steps if isinstance(s, SelectStep)]
    assert [(first_name(s), s.option) for s in selects] == [
        ("Conta de origem", "Corrente 1"),
        ("Conta de destino", "Poupança 2"),
    ]


def test_fields_of_different_rows_with_the_same_selector_are_never_merged() -> None:
    # Um caminho CSS cortado antes da linha (seis níveis): a chave da linha separa.
    def quantity(key: str) -> ElementDescriptor:
        row = RowInfo(text=f"{key} 1", cells=[key, "1"], key=key)
        return textbox("Quantidade", css="div > div > div > div > div > input", row=row)

    rec = RecordingBuilder()
    rec.navigate("/carrinho")
    rec.fill(quantity("Item 1"), "3")
    rec.fill(quantity("Item 2"), "5")
    rec.click(button("Atualizar carrinho", id="b"))
    rec.request("POST", "/api/carrinho", "fetch")
    result = compile_(rec.build())
    assert [s.value for s in fills(result)] == [TextValue(template="3"), TextValue(template="5")]


def test_the_same_field_edited_or_checked_twice_is_still_one_step() -> None:
    valor = in_component(textbox("Valor", css="#input"), "#fv")
    lembrar = in_component(checkbox("Lembrar", css="#input"), "#fl")
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.fill(valor, "10")
    rec.fill(valor, "100")
    rec.check(lembrar, True)
    rec.check(lembrar, False)
    rec.click(button("Salvar", id="s"))
    rec.request("POST", "/api/form", "fetch")
    result = compile_(rec.build())
    assert [s.value for s in fills(result)] == [TextValue(template="100")]
    checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    assert [(s.title, s.checked) for s in checks] == [("Desmarcar “Lembrar”", False)]


def test_edits_never_merge_across_a_document_load() -> None:
    # O mesmo seletor em outra página (redirecionamento do script entre as edições):
    # dois passos, como na conta do gravador.
    campo = textbox("Busca", id="q")
    rec = RecordingBuilder()
    rec.navigate("/a")
    rec.fill(campo, "um")
    rec.navigate("/b", cause=NavigationCause.SCRIPT)
    rec.fill(campo, "dois")
    rec.key("Enter", campo)
    result = compile_(rec.build())
    assert [s.value for s in fills(result)] == [
        TextValue(template="um"),
        TextValue(template="dois"),
    ]


# -- campo de shadow root nunca é credencial -----------------------------------------


def test_a_field_inside_a_shadow_root_is_never_a_credential() -> None:
    usuario = in_component(textbox("Usuário", css="#input", autocomplete="username"), "#fu")
    senha = in_component(password(css="#input"), "#fp")
    codigo = in_component(otp(css="#input"), "#fc")
    assert all(in_shadow_root(el) for el in (usuario, senha, codigo))
    assert [credential_kind(el) for el in (usuario, senha, codigo)] == [None, None, None]
    assert not declared_username(usuario)
    light = textbox("Usuário", id="u", autocomplete="username")
    assert credential_kind(light) == "username" and declared_username(light)


@pytest.mark.parametrize(
    "variant", ["usuario_no_shadow", "senha_no_shadow", "os_dois", "usuario_declarado"]
)
def test_a_login_with_a_field_of_a_shadow_root_is_write(variant: str) -> None:
    # C1, C7, S1: um campo de componente na página do login: o envio não é
    # autenticação (grava, pelo POST), o usuário vira uma entrada e a senha
    # (type=password) continua a do cofre, no campo dela.
    usuario: ElementDescriptor = textbox("Usuário", id="u", form=None)
    senha: ElementDescriptor = password(id="s", form=None)
    if variant in ("usuario_no_shadow", "os_dois"):
        usuario = in_component(textbox("Usuário", css="#input", form=None), "#fu")
    if variant == "usuario_declarado":
        usuario = in_component(
            textbox("Usuário", css="#input", form=None, autocomplete="username"), "#fu"
        )
    if variant in ("senha_no_shadow", "os_dois"):
        senha = in_component(password(css="#input", form=None), "#fp")
    usuario = usuario.model_copy(update={"sensitive": True, "is_username": True})
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, None)
    rec.fill(senha, None)
    rec.click(button("Entrar", id="b"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    assert analyze_logins(recording.events).logins == ()
    result = compile_(recording)
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    assert [(first_name(s), s.value) for s in fills(result)] == [
        ("Usuário", an_input("usuario")),
        ("Senha", PASSWORD),
    ]


def test_a_value_of_a_shadow_root_on_a_page_with_a_password_becomes_an_input() -> None:
    # C7/C8: o valor de um componente numa página com senha sai da gravação (mesmo que
    # o script não o tenha visto) e vira uma entrada, no campo dele.
    cpf = in_component(textbox("CPF", css="#input", form=None), "#fc")
    valor = in_component(textbox("Valor", css="#input", form=None), "#fv")
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(cpf, "11122233344")
    rec.fill(valor, "1250,00")
    rec.fill(password(id="s", form=None), None)
    rec.click(button("Continuar", id="b"))
    rec.request("POST", "/api/pix", "fetch")
    recording = rec.build()
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames] == ["11122233344"]
    assert [e.value for e in found.others] == ["1250,00"]
    result = compile_(recording)
    assert [(first_name(s), s.value) for s in fills(result)] == [
        ("CPF", an_input("cpf")),
        ("Valor", an_input("valor")),
        ("Senha", PASSWORD),
    ]
    assert step(result, "clicar_continuar").effect is Effect.WRITE
    dumped = result.manifest.model_dump_json()
    assert "11122233344" not in dumped and "1250,00" not in dumped


def test_a_username_of_a_shadow_root_never_bridges_a_two_step_login() -> None:
    usuario = in_component(textbox("Usuário", css="#input", form=None), "#fu")
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.click(button("Continuar", id="c"))
    rec.navigate("/login#senha", cause=NavigationCause.SCRIPT, same_document=True)
    rec.fill(password(id="s", form=None), None)
    rec.click(button("Entrar", id="b"))
    rec.request("POST", "/api/login", "fetch")
    result = compile_(rec.build())
    assert usernames_in(result) == []
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert "ana.souza" not in result.manifest.model_dump_json()


def test_a_username_of_a_shadow_root_never_makes_a_login_over_two_pages() -> None:
    usuario = in_component(textbox("Usuário", css="#input", form=None), "#fu")
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    continuar = rec.click(button("Continuar", id="c"))
    rec.request("GET", "/login/senha")
    rec.navigate("/login/senha", cause=NavigationCause.CLICK, trigger=continuar)
    rec.fill(password(id="s", form=LOGIN_FORM), None)
    entrar = rec.click(button("Entrar", id="b", form=LOGIN_FORM))
    rec.request("POST", "/login/senha")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=entrar)
    recording = rec.build()
    # O usuário da página que leva à senha sai da gravação, mas não vira o do cofre.
    assert [e.value for e in secret_page_inputs(recording.events).usernames] == ["ana.souza"]
    result = compile_(recording)
    assert usernames_in(result) == []
    assert fills(result)[0].value == an_input("usuario")


# -- só códigos: só no <form> só de códigos -------------------------------------------


@pytest.mark.parametrize(
    "label", ["Continuar", "Avançar", "Próximo", "Entrar", "Acessar", "Confirmar", "Seguinte"]
)
def test_a_code_after_the_login_of_a_spa_without_a_form_is_write(label: str) -> None:
    # G2a: login na SPA, depois "Token" e o botão (sem <form>) que faz o Pix.
    rec = RecordingBuilder()
    rec.navigate("/app")
    rec.fill(textbox("Usuário", id="u", form=None), "ana.souza")
    rec.fill(password(id="p", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.navigate("/app/pix/confirmar", cause=NavigationCause.SCRIPT, same_document=True)
    rec.fill(otp("Token", id="t", form=None, autocomplete="one-time-code"), None)
    rec.click(button(label, id="c"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    pix = [s for s in result.manifest.steps if isinstance(s, ClickStep)][-1]
    assert pix.effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


@pytest.mark.parametrize(
    ("code_form", "button_form"),
    [
        (None, None),
        (OTP_FORM, None),
        (None, OTP_FORM),
        (OTP_FORM, OTHER_OTP_FORM),
        (TOKEN_AND_TEXT, TOKEN_AND_TEXT),
        (LOGIN_FORM, LOGIN_FORM),
    ],
    ids=["sem_form", "botao_fora", "codigo_fora", "outro_form", "form_com_texto", "form_login"],
)
@pytest.mark.parametrize("label", ["Continuar", "Entrar", "Confirmar"])
def test_codes_alone_need_the_button_in_the_form_of_only_those_codes(
    code_form: FormInfo | None, button_form: FormInfo | None, label: str
) -> None:
    rec = RecordingBuilder()
    rec.navigate("/codigo")
    rec.fill(otp(id="codigo", form=code_form), None)
    rec.click(button(label, id="ok", form=button_form))
    rec.request("POST", "/api/codigo", "fetch")
    recording = rec.build()
    assert analyze_logins(recording.events).logins == ()
    result = compile_(recording)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE


@pytest.mark.parametrize(
    "label", ["Continuar", "Avançar", "Próximo", "Entrar", "Acessar", "Confirmar"]
)
def test_codes_alone_in_the_form_of_only_the_code_stay_a_login(label: str) -> None:
    # Resíduo documentado (como em d746ee2): a mesma forma dentro de um <form> só com
    # o código continua autenticação, ainda que a página use o código numa operação.
    rec = RecordingBuilder()
    rec.navigate("/login/codigo")
    rec.fill(otp(id="codigo", form=OTP_FORM), None)
    rec.click(button(label, id="ok", form=OTP_FORM))
    rec.request("POST", "/login/codigo")
    result = compile_(rec.build())
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.READ
    assert fills(result)[0].value == TOTP
