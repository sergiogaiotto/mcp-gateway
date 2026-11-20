"""Identidade dos campos e campos de shadow roots, sobre gravações montadas à mão.

Rodada 7 do gravador: dois componentes iguais (o mesmo ``<input id="input">``
dentro de cada shadow root) nunca viram o mesmo campo, edições, marcações e
escolhas de campos diferentes nunca se juntam num passo (o alvo e o valor sempre
vêm do mesmo campo), um campo de shadow root nunca é credencial de login, e só
códigos só autenticam com o botão num ``<form>`` só de códigos.

Rodada 8: o ``data-testid`` entra na identidade do campo, e só edições do mesmo
elemento (o número que o script dá a cada um) se juntam num passo.

Rodada 9: o segundo clique de um clique duplo só some no primeiro passo quando é no
mesmo elemento ou quando um dos dois cliques não gravou nada.

Rodada 10: noutro elemento, ele também some sem prova de outro alvo (outra chave da
linha, texto, nome, rótulo ou ``data-testid``): o número do elemento sozinho não
separa os cliques.
"""

from collections.abc import Callable
from typing import Any

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
    other_target,
    second_click,
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


# -- rodada 8: o test_id e o número do elemento ---------------------------------------

#: Caminho CSS cortado em seis níveis da luz: o mesmo nos cartões fundos (N10).
DEEP_CSS = "div > div > div > span > label > input"


def deep(make: Callable[..., ElementDescriptor], label: str, **kw: Any) -> ElementDescriptor:
    """Campo de um cartão fundo: o mesmo caminho, rótulo, id e nome nos dois cartões."""
    return make(label, css=DEEP_CSS, id="qtd", name_attr="qtd", **kw)


def test_two_deep_fields_that_differ_only_in_the_test_id_are_two_steps() -> None:
    # N10 numa gravação sem o número do elemento: o test_id, que vira localizador,
    # separa os campos; cada passo leva o localizador e o valor do mesmo cartão.
    repeated = {"role_name": 2, "label": 2, "id": 2, "name_attr": 2}
    um = deep(textbox, "Quantidade", test_id="qtd-1", counts=repeated)
    dois = deep(textbox, "Quantidade", test_id="qtd-2", counts=repeated)
    assert field_key(0, um) != field_key(0, dois)
    rec = RecordingBuilder()
    rec.navigate("/carrinho")
    rec.fill(um, "3")
    rec.fill(dois, "7")
    rec.click(button("Salvar", id="b"))
    rec.request("POST", "/api/salvar", "fetch")
    result = compile_(rec.build())
    found = fills(result)
    assert [(s.target.locators[0].value, s.value) for s in found] == [
        ("qtd-1", TextValue(template="3")),
        ("qtd-2", TextValue(template="7")),
    ]


@pytest.mark.parametrize("kind", ["fill", "check", "select"])
def test_two_elements_with_the_same_description_never_merge(kind: str) -> None:
    # O mesmo rótulo, caminho, id, nome e test_id: só o número que o script dá a cada
    # elemento separa. Antes, um passo só (alvo de um, valor do outro).
    make = {"fill": textbox, "check": checkbox, "select": select_box}[kind]
    um = deep(make, "Quantidade", serial=11)
    dois = deep(make, "Quantidade", serial=12)
    assert field_key(0, um) == field_key(0, dois)
    rec = RecordingBuilder()
    rec.navigate("/carrinho")
    if kind == "fill":
        rec.fill(um, "3")
        rec.fill(dois, "7")
    elif kind == "check":
        rec.check(um, True)
        rec.check(dois, False)
    else:
        rec.select(um, "Pagar")
        rec.select(dois, "Cancelar")
    rec.click(button("Salvar", id="b"))
    rec.request("POST", "/api/salvar", "fetch")
    result = compile_(rec.build())
    if kind == "fill":
        assert [s.value for s in fills(result)] == [
            TextValue(template="3"),
            TextValue(template="7"),
        ]
    elif kind == "check":
        checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
        assert [s.checked for s in checks] == [True, False]
    else:
        selects = [s for s in result.manifest.steps if isinstance(s, SelectStep)]
        assert [s.option for s in selects] == ["Pagar", "Cancelar"]
    assert step(result, "clicar_salvar").effect is Effect.WRITE


@pytest.mark.parametrize("serial", [7, None], ids=["com_numero", "gravacao_antiga"])
def test_edits_of_the_same_element_still_merge(serial: int | None) -> None:
    # O mesmo número (ou nenhum, nas gravações antigas): um passo só, com o valor final.
    numbers: list[dict[str, Any]] = (
        [{}, {}]
        if serial is None
        else [
            {"serial": serial},
            {"serial": serial + 1},
        ]
    )
    campo = deep(textbox, "Quantidade", **numbers[0])
    caixa = deep(checkbox, "Incluir", **numbers[1])
    rec = RecordingBuilder()
    rec.navigate("/carrinho")
    rec.fill(campo, "3")
    rec.fill(campo, "30")
    rec.check(caixa, True)
    rec.check(caixa, False)
    rec.click(button("Salvar", id="b"))
    rec.request("POST", "/api/salvar", "fetch")
    result = compile_(rec.build())
    assert [s.value for s in fills(result)] == [TextValue(template="30")]
    checks = [s for s in result.manifest.steps if isinstance(s, CheckStep)]
    assert [s.checked for s in checks] == [False]


def test_the_login_rule_sees_two_elements_of_the_same_kind_as_two_steps() -> None:
    # O gravador (analyze_logins) junta edições como o compilador: com números
    # diferentes, dois passos; o usuário e a senha continuam um login.
    um = textbox("Usuário", id="u", name_attr="usuario", form=LOGIN_FORM, serial=1)
    outro = um.model_copy(update={"serial": 2})
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(um, "ana")
    rec.fill(outro, "ana.souza")
    rec.fill(password(id="p", serial=3), "segredo-123")
    rec.click(button("Entrar", id="b", form=LOGIN_FORM))
    rec.request("POST", "/login", "document")
    rec.navigate("/inicio", cause=NavigationCause.CLICK)
    recording = rec.build()
    (login,) = analyze_logins(recording.events).logins
    assert [e.value for e in login.username] == ["ana", "ana.souza"]
    result = compile_(recording)
    assert usernames_in(result) == ["digitar_usuario", "digitar_usuario_2"]


# -- rodada 9: o segundo clique de um clique duplo noutro elemento -------------------

#: O botão de uma linha funda: o caminho CSS cortado é o mesmo em todas as linhas.
ROW_BUTTON_CSS = "article > div > div > div > div > button"


def row_button(name: str, serial: int | None, **kw: Any) -> ElementDescriptor:
    extra: dict[str, Any] = {} if serial is None else {"serial": serial}
    return button(name, css=ROW_BUTTON_CSS, counts={"role_name": 3, "text": 3}, **extra, **kw)


@pytest.mark.parametrize(
    ("serials", "keys", "writes", "steps"),
    [
        ((1, 2), (None, None), (True, True), 1),
        ((1, 2), ("Boleto 1", "Boleto 2"), (True, True), 2),
        ((1, 1), (None, None), (True, True), 1),
        ((None, None), (None, None), (True, True), 1),
        ((1, 2), ("Boleto 1", "Boleto 2"), (True, False), 1),
        ((1, 2), ("Boleto 1", "Boleto 2"), (False, True), 1),
    ],
    ids=[
        "outra_linha_que_gravou",
        "outra_linha_com_outra_chave",
        "o_mesmo_botao",
        "gravacao_antiga",
        "segundo_nao_gravou",
        "primeiro_nao_gravou",
    ],
)
def test_a_second_click_on_another_element_is_a_step_only_with_another_target_and_both_writes(
    serials: tuple[int | None, int | None],
    keys: tuple[str | None, str | None],
    writes: tuple[bool, bool],
    steps: int,
) -> None:
    # D1: o "Excluir" do boleto 2 sobe para o lugar do 1, que a pessoa excluiu, e o
    # segundo clique vem com detail 2 e o mesmo caminho CSS. Só é outro passo com
    # prova de outro alvo (aqui, a chave da linha) e quando cada clique gravou algo.
    # Só o número do elemento não basta: um botão sem trava que a página recria
    # entre os dois cliques de um clique duplo também tem outro número, e o robô
    # pagaria duas vezes (W11). Sem a chave da linha, o D1 continua um passo só (o
    # robô age menos que a pessoa, nunca mais).
    def row(key: str | None) -> dict[str, Any]:
        return {} if key is None else {"row": RowInfo(text=f"{key} Excluir", key=key)}

    rec = RecordingBuilder()
    rec.navigate("/boletos")
    first = rec.click(row_button("Excluir", serials[0], **row(keys[0])))
    after_first = [rec.request("POST", "/api/excluir?i=1", "fetch")] if writes[0] else []
    second = rec.click(row_button("Excluir", serials[1], **row(keys[1])), detail=2)
    after_second = [rec.request("POST", "/api/excluir?i=2", "fetch")] if writes[1] else []
    result = compile_(rec.build())
    clicks = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert len(clicks) == steps
    assert all(s.effect is Effect.IRREVERSIBLE for s in clicks)
    assert result.manifest.effect() is Effect.IRREVERSIBLE
    # O gravador (analyze_logins) decide pela mesma regra.
    absorbed = second_click(first.element, after_first, second.element, after_second)
    assert absorbed is (steps == 1)


@pytest.mark.parametrize("field", ["text", "name", "label", "test_id"])
def test_another_text_name_label_or_test_id_is_another_target(field: str) -> None:
    # Prova de um segundo alvo além da chave da linha; o número e o seletor não contam.
    base = row_button("Excluir", 1, label="Excluir", test_id="excluir")
    other = base.model_copy(update={"serial": 2, field: "Outro"})
    assert other_target(base, other)
    assert not other_target(base, base.model_copy(update={"serial": 2}))
    assert not other_target(base, base.model_copy(update={"serial": 2, "css": "#outro"}))


@pytest.mark.parametrize(
    ("names", "first_kw", "second_kw"),
    [
        (
            ("Pagar", "Pagar"),
            {"row": RowInfo(text="Boleto 1 Pendente Pagar", key="Pendente")},
            {"row": RowInfo(text="Boleto 1 Processando Pagar", key="Processando")},
        ),
        (("Pagar", "Pagando…"), {}, {}),
    ],
    ids=["a_linha_mudou", "o_botao_mudou"],
)
def test_a_double_click_that_repeats_the_same_write_is_one_click(
    names: tuple[str, str], first_kw: dict[str, Any], second_kw: dict[str, Any]
) -> None:
    # Y5: um botão sem trava que o primeiro clique recria com outro texto ("Pagando…") ou
    # numa linha que mudou ("Processando"); os dois cliques gravam no mesmo endereço. O
    # texto e a chave diferentes vieram do próprio primeiro clique: o robô paga uma vez.
    rec = RecordingBuilder()
    rec.navigate("/boletos")
    first = rec.click(row_button(names[0], 1, **first_kw))
    after_first = [rec.request("POST", "/api/pagar", "fetch")]
    second = rec.click(row_button(names[1], 2, **second_kw), detail=2)
    after_second = [rec.request("POST", "/api/pagar", "fetch")]
    result = compile_(rec.build())
    clicks = [s for s in result.manifest.steps if isinstance(s, ClickStep)]
    assert [s.effect for s in clicks] == [Effect.IRREVERSIBLE]
    assert other_target(first.element, second.element)
    assert second_click(first.element, after_first, second.element, after_second)


def test_a_second_click_that_lands_on_another_button_after_a_login_is_its_own_step() -> None:
    # Login de SPA com clique duplo no "Entrar": o primeiro clique entra (POST), a página
    # troca de tela e o segundo cai no "Transferir" do mesmo lugar (o mesmo caminho
    # CSS), que também grava. Antes, a transferência ficava dentro do passo do login
    # (leitura) e o robô não a refazia; agora é o seu passo, irreversível. O login
    # continua o mesmo na conta do gravador.
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="u", name_attr="usuario", form=LOGIN_FORM, serial=1), "ana")
    rec.fill(password(id="p", serial=2), "segredo-123")
    entrar = rec.click(row_button("Entrar", 3, form=LOGIN_FORM))
    rec.request("POST", "/api/login", "xhr")
    rec.click(row_button("Transferir", 4), detail=2)
    rec.request("POST", "/api/transferir", "xhr")
    recording = rec.build()
    (login,) = analyze_logins(recording.events).logins
    assert login.submit == entrar
    result = compile_(recording)
    assert [(s.id, s.effect) for s in result.manifest.steps if isinstance(s, ClickStep)] == [
        ("clicar_entrar", Effect.READ),
        ("clicar_transferir", Effect.IRREVERSIBLE),
    ]
    assert usernames_in(result) == ["digitar_usuario"]
    assert result.manifest.effect() is Effect.IRREVERSIBLE


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
