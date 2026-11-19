"""Regra conservadora do login, sobre gravações montadas à mão (sem navegador).

Na dúvida o passo grava: um login tratado como gravação é aceitável, uma
transação tratada como login (leitura, com o usuário do cofre digitado num campo
de dado) é um defeito.
"""

import pytest

from rapa.compiler import CompileResult, lower
from rapa.core import policy
from rapa.core.robot import ClickStep, Effect, FillStep, PressStep, SecretValue, Step, TextValue
from rapa.recorder.models import (
    REDACTED,
    ElementDescriptor,
    FormInfo,
    InputEvent,
    NavigationCause,
    Recording,
)
from rapa.recorder.sensitivity import login_inputs, username_inputs
from tests.compiler.builders import (
    BASE,
    LOGIN_FORM,
    OTP_FORM,
    RecordingBuilder,
    button,
    checkbox,
    element,
    otp,
    password,
    select_box,
    textbox,
)

USERNAME = SecretValue(credential="portal", field="username")
PASSWORD = SecretValue(credential="portal", field="password")
PIX_FORM = FormInfo(action=f"{BASE}/pix", method="post", has_password=True, text_fields=1)
MODAL_FORM = FormInfo(action=f"{BASE}/painel", method="post", has_password=True, text_fields=0)


def compile_(recording: Recording) -> CompileResult:
    result = lower(recording, name="robo_teste", title="Robô de teste", description="Teste.")
    assert policy.check(result.manifest) == []
    return result


def step(result: CompileResult, step_id: str) -> Step:
    found = result.manifest.step_by_id(step_id)
    assert found is not None, [s.id for s in result.manifest.steps]
    return found


def fill_value(result: CompileResult, step_id: str) -> object:
    found = step(result, step_id)
    assert isinstance(found, FillStep)
    return found.value


def usernames_in(result: CompileResult) -> list[str]:
    return [
        s.id
        for s in result.manifest.steps
        if isinstance(s, FillStep)
        and isinstance(s.value, SecretValue)
        and s.value.field == "username"
    ]


def script_username(label: str, **kw: object) -> ElementDescriptor:
    """Campo que o script da página marcou como usuário (valor fora da gravação)."""
    return textbox(label, is_username=True, sensitive=True, **kw)  # type: ignore[arg-type]


# -- botão de login: o texto inteiro, de um vocabulário curto ---------------------


def _transaction(field: ElementDescriptor, button_label: str) -> CompileResult:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.page_text("Transferência Pix")
    rec.fill(field, "12345678909")
    rec.fill(password(id="senha", form=PIX_FORM), None)
    click = rec.click(button(button_label, id="ok", form=PIX_FORM))
    rec.request("POST", "/pix")
    rec.navigate("/pix", cause=NavigationCause.CLICK, trigger=click)
    rec.page_text("Comprovante")
    return compile_(rec.build())


TRANSACTIONS = [
    ("Chave Pix", "Confirmar"),
    ("Valor", "Confirmar transação"),
    ("Conta destino", "Confirmar compra"),
    ("Valor", "OK"),
    ("Valor", "Continuar para o pagamento"),
]


@pytest.mark.parametrize(("field_label", "button_label"), TRANSACTIONS)
@pytest.mark.parametrize("marked_by_script", [False, True], ids=["literal", "marcado"])
def test_a_transaction_with_a_password_is_never_a_login(
    field_label: str, button_label: str, marked_by_script: bool
) -> None:
    make = script_username if marked_by_script else textbox
    field = make(field_label, id="campo", form=PIX_FORM)
    result = _transaction(field, button_label)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    # O usuário do cofre nunca é digitado no campo de dado da operação.
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    if marked_by_script:
        # O valor já saiu da gravação: vira uma entrada obrigatória, com aviso.
        assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
        assert [(f.required) for f in result.manifest.inputs] == [True]
        assert any(field_label in w and "entrada" in w for w in result.warnings)
    else:
        assert value == TextValue(template="12345678909")
    recording_rule = login_inputs(_rebuilt(field, button_label).events)
    assert recording_rule.usernames == [] and recording_rule.hidden == []


def _rebuilt(field: ElementDescriptor, button_label: str) -> Recording:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(field, "12345678909")
    rec.fill(password(id="senha", form=PIX_FORM), None)
    rec.click(button(button_label, id="ok", form=PIX_FORM))
    rec.request("POST", "/pix")
    return rec.build()


def _login(button_label: str, *, form: FormInfo = LOGIN_FORM) -> CompileResult:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=form), "ana.souza")
    rec.fill(password(id="senha", form=form), None)
    click = rec.click(button(button_label, id="entrar", form=form))
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    return compile_(rec.build())


@pytest.mark.parametrize(
    "label",
    [
        "Entrar",
        "ENTRAR",
        "  Acessar ",
        "Login",
        "Log in",
        "Fazer login",
        "Sign in",
        "Logar",
        "Iniciar sessão",
        "Continuar",
        "Avançar",
        "Próximo",
        "Seguinte",
    ],
)
def test_a_login_button_is_one_of_a_short_vocabulary(label: str) -> None:
    result = _login(label)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.READ
    assert fill_value(result, "digitar_usuario") == USERNAME
    assert "ana.souza" not in result.manifest.model_dump_json()


@pytest.mark.parametrize(
    "label",
    [
        "OK",
        "Confirmar",
        "Validar",
        "Prosseguir",
        "Entrar no sistema",
        "Acessar conta",
        "Continuar para o pagamento",
        "Confirmar acesso",
    ],
)
def test_any_other_button_that_sends_a_post_is_write(label: str) -> None:
    result = _login(label)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    # Sem login confirmado, o campo antes da senha não vira o usuário do cofre.
    assert usernames_in(result) == []


def test_confirm_only_counts_for_a_one_time_code_form() -> None:
    rec = RecordingBuilder()
    rec.navigate("/codigo")
    rec.fill(otp(id="codigo"), None)
    rec.click(button("Confirmar", id="ok", form=OTP_FORM))
    rec.request("POST", "/codigo")
    result = compile_(rec.build())
    assert step(result, "clicar_confirmar").effect is Effect.READ
    # O mesmo botão num formulário com usuário e senha não é login.
    assert step(_login("Confirmar"), "clicar_confirmar").effect is Effect.WRITE


def test_a_login_after_a_write_keeps_the_username_out_of_the_robot() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.click(button("Gravar", id="gravar"))
    rec.request("POST", "/cadastro")
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(id="senha"), None)
    rec.click(button("Entrar", id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/login")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    # O gravador já teria tirado o valor (cara de login); o robô pede o usuário.
    assert fill_value(result, "digitar_usuario") == TextValue(template="{{ inputs.usuario }}")
    assert "ana.souza" not in result.manifest.model_dump_json()


def test_a_username_marked_by_the_script_without_a_login_becomes_an_input() -> None:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(script_username("Valor", id="valor"), None)
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Pagar agora", id="ok"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_valor") == TextValue(template="{{ inputs.valor }}")
    assert step(result, "clicar_pagar_agora").effect is Effect.IRREVERSIBLE


def test_a_page_declared_username_stays_the_username() -> None:
    usuario = script_username("Usuário", id="usuario", autocomplete="username", form=LOGIN_FORM)
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, None)
    rec.fill(password(id="senha"), None)
    rec.click(button("OK", id="ok", form=LOGIN_FORM))
    rec.request("POST", "/login")
    result = compile_(rec.build())
    assert step(result, "clicar_ok").effect is Effect.WRITE
    assert fill_value(result, "digitar_usuario") == USERNAME


def test_an_enter_in_the_password_without_a_button_is_a_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    senha = password(id="senha", form=None)
    rec.fill(senha, None)
    enter = rec.key("Enter", senha)
    rec.request("POST", "/api/login", "fetch")
    rec.navigate("/inicio", cause=NavigationCause.KEY, trigger=enter)
    result = compile_(rec.build())
    press = next(s for s in result.manifest.steps if isinstance(s, PressStep))
    assert press.effect is Effect.READ
    assert fill_value(result, "digitar_usuario") == USERNAME


def test_one_time_code_on_the_login_page_after_the_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.fill(otp(id="codigo"), None)
    rec.click(button("Confirmar", id="confirmar", form=OTP_FORM))
    rec.request("POST", "/api/codigo", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert step(result, "clicar_confirmar").effect is Effect.READ
    assert result.manifest.effect() is Effect.READ


# -- o botão de mostrar a senha não é o envio ---------------------------------------


@pytest.mark.parametrize("marked_by_script", [False, True], ids=["literal", "escondido"])
def test_the_show_password_button_is_not_the_submit(marked_by_script: bool) -> None:
    pay = FormInfo(action=f"{BASE}/pagar", method="post", has_password=True, text_fields=1)
    flags = {"sensitive": True} if marked_by_script else {}
    rec = RecordingBuilder()
    rec.navigate("/pagar")
    rec.fill(textbox("Valor", id="valor", form=pay, **flags), "1250,00")
    rec.fill(password(id="senha", form=pay), None)
    rec.click(button("Mostrar senha", id="olho", form=pay))
    click = rec.click(button("Confirmar pagamento", id="ok", form=pay))
    rec.request("POST", "/pagar")
    rec.navigate("/pagar", cause=NavigationCause.CLICK, trigger=click)
    recording = rec.build()
    result = compile_(recording)
    assert fill_value(result, "digitar_valor") == TextValue(template="{{ inputs.valor }}")
    assert [f.name for f in result.manifest.inputs] == ["valor"]
    assert usernames_in(result) == []
    assert step(result, "clicar_confirmar_pagamento").effect is Effect.IRREVERSIBLE
    assert result.manifest.effect() is Effect.IRREVERSIBLE
    assert "1250,00" not in result.manifest.model_dump_json()
    assert login_inputs(recording.events).usernames == []


def test_the_show_password_button_does_not_break_a_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(id="senha"), None)
    rec.click(button("Mostrar senha", id="olho", form=LOGIN_FORM))
    rec.click(button("Entrar", id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/login")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert fill_value(result, "digitar_usuario") == USERNAME


# -- login em duas etapas: só com a forma exata --------------------------------------


def _two_step(variant: str) -> Recording:
    usuario = textbox("Usuário", id="usuario", name_attr="usuario")
    rec = RecordingBuilder()
    rec.navigate("/login")
    if variant == "radio":
        rec.check(element_radio("Pessoa física"))
    rec.fill(usuario, "ana.souza")
    if variant == "lembrar":
        rec.check(checkbox("Lembrar meu usuário", id="lembrar"))
    continuar = rec.click(button("Continuar", id="continuar"))
    if variant == "pushstate":
        rec.navigate(
            "/login/senha", cause=NavigationCause.CLICK, trigger=continuar, same_document=True
        )
    if variant == "hash":
        rec.navigate(
            "/login#senha", cause=NavigationCause.CLICK, trigger=continuar, same_document=True
        )
    rec.fill(password(id="senha", form=None), None)
    click = rec.click(button("Entrar", id="entrar"))
    rec.request("GET", "/inicio")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    return rec.build()


def element_radio(label: str) -> ElementDescriptor:
    return element("input", role="radio", name=label, label=label, input_type="radio", id="tipo")


@pytest.mark.parametrize("variant", ["simples", "pushstate", "hash", "radio", "lembrar"])
def test_two_step_login_variants_hide_the_username(variant: str) -> None:
    recording = _two_step(variant)
    found = username_inputs(recording.events)
    assert [e.value for e in found] == ["ana.souza"]
    result = compile_(recording)
    assert fill_value(result, "digitar_usuario") == USERNAME
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert "ana.souza" not in result.model_dump_json()


def test_the_bridge_of_a_two_step_login_may_check_the_username() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("E-mail", input_type="email", id="email"), "ana@exemplo.com.br")
    rec.click(button("Avançar", id="avancar"))
    rec.request("POST", "/api/identificar", "fetch")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_avancar").effect is Effect.READ
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert fill_value(result, "digitar_e_mail") == USERNAME


@pytest.mark.parametrize(
    "variant",
    ["ponte_com_texto_a_mais", "lista_antes", "outro_campo_antes", "senha_em_campo_de_texto"],
)
def test_two_step_login_needs_the_exact_shape(variant: str) -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    if variant == "lista_antes":
        rec.select(select_box("Perfil", id="perfil"), "Contador")
    if variant == "outro_campo_antes":
        rec.fill(textbox("Empresa", id="empresa"), "ACME")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    bridge = "Continuar para a senha" if variant == "ponte_com_texto_a_mais" else "Continuar"
    rec.click(button(bridge, id="continuar"))
    if variant == "senha_em_campo_de_texto":
        rec.fill(textbox("Senha", id="senha_texto"), "S3nh@Forte!")
    else:
        rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    # Sem a forma exata o campo fica literal e o envio grava.
    assert fill_value(result, "digitar_usuario") == TextValue(template="ana.souza")
    assert step(result, "clicar_entrar").effect is Effect.WRITE


@pytest.mark.parametrize("final", ["Confirmar", "Confirmar transação", "Concluir", "OK"])
@pytest.mark.parametrize("with_form", [True, False], ids=["formulario", "sem_formulario"])
def test_a_payment_wizard_is_not_a_two_step_login(final: str, with_form: bool) -> None:
    form = PIX_FORM if with_form else None
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.page_text("Pix")
    rec.fill(textbox("Valor", id="valor", form=form), "1250,00")
    rec.click(button("Continuar", id="continuar", form=form))
    rec.fill(password(id="senha", form=form or MODAL_FORM), None)
    rec.click(button(final, id="ok", form=form or MODAL_FORM))
    rec.request("POST", "/api/pix", "fetch")
    recording = rec.build()
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    assert usernames_in(result) == []
    assert result.manifest.effect() in (Effect.WRITE, Effect.IRREVERSIBLE)
    valor = fill_value(result, "digitar_valor")
    if final == "Concluir":
        # Verbo irreversível: o campo antes da senha sai do robô, como antes.
        assert valor == TextValue(template="{{ inputs.valor }}")
    else:
        assert valor == TextValue(template="1250,00")


def test_a_search_before_a_password_modal_is_not_the_username() -> None:
    rec = RecordingBuilder()
    rec.navigate("/painel")
    rec.page_text("Painel")
    rec.fill(textbox("Buscar nota", id="q"), "Nota 123")
    rec.click(button("Buscar", id="buscar"))
    rec.request("GET", "/api/notas", "fetch")
    rec.click(button("Editar", id="editar"))
    rec.fill(password("Senha", id="senha", form=MODAL_FORM), None)
    rec.click(button("Confirmar", id="confirmar", form=MODAL_FORM))
    rec.request("POST", "/api/notas/123/liberar", "fetch")
    recording = rec.build()
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    assert fill_value(result, "digitar_buscar_nota") == TextValue(template="Nota 123")
    assert step(result, "clicar_confirmar").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


def test_the_compiler_never_turns_a_hidden_field_into_the_username() -> None:
    # O script marcou o campo como "escondido" (antes de uma senha de transação);
    # mesmo com um botão de login depois, ele não vira o usuário do cofre.
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor", sensitive=True), None)
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_valor") == TextValue(template="{{ inputs.valor }}")
    assert step(result, "clicar_entrar").effect is Effect.WRITE


# -- valores apagados pela limpeza e senha pelo nome acessível ----------------------


def test_a_fill_with_a_removed_value_becomes_an_input() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(textbox("Departamento", id="dep"), f"{REDACTED} Corporativo")
    rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/salvar")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_departamento") == TextValue(
        template="{{ inputs.departamento }}"
    )
    assert REDACTED not in result.manifest.model_dump_json()
    assert any("Departamento" in w and "sensível" in w for w in result.warnings)


def _named_password(button_label: str, *, sensitive: bool) -> CompileResult:
    form = FormInfo(action=f"{BASE}/entrar", method="post", text_fields=2)
    cod = element_named_password(sensitive=sensitive, form=form)
    rec = RecordingBuilder()
    rec.navigate("/entrar")
    rec.fill(textbox("CPF", id="cpf", form=form), "12345678909")
    rec.fill(cod, "918273")
    click = rec.click(button(button_label, id="ok", form=form))
    rec.request("POST", "/entrar")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    return compile_(rec.build())


#: Passo do campo cujo nome acessível (aria-labelledby) fala em senha.
NAMED = "digitar_digite_a_senha_de_6"


def element_named_password(*, sensitive: bool, form: FormInfo) -> ElementDescriptor:
    field = textbox("Código pessoal", id="cod", form=form, sensitive=sensitive)
    return field.model_copy(update={"name": "Digite a senha de 6 dígitos"})


@pytest.mark.parametrize("sensitive", [True, False], ids=["marcado", "sem_marca"])
def test_a_field_named_like_a_password_is_the_password_of_a_login(sensitive: bool) -> None:
    result = _named_password("Entrar", sensitive=sensitive)
    assert fill_value(result, NAMED) == PASSWORD
    assert fill_value(result, "digitar_cpf") == USERNAME
    assert step(result, "clicar_entrar").effect is Effect.READ
    dumped = result.model_dump_json()
    assert "918273" not in dumped and "12345678909" not in dumped


@pytest.mark.parametrize("sensitive", [True, False], ids=["marcado", "sem_marca"])
def test_a_field_named_like_a_password_outside_a_login_becomes_an_input(sensitive: bool) -> None:
    result = _named_password("Gravar", sensitive=sensitive)
    value = fill_value(result, NAMED)
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.digite_a_senha")
    assert [f.required for f in result.manifest.inputs] == [True]
    assert any("fala em senha" in w for w in result.warnings)
    assert step(result, "clicar_gravar").effect is Effect.WRITE
    assert "918273" not in result.model_dump_json()
    assert result.manifest.credentials == []


def test_an_assertion_never_uses_a_heading_with_a_removed_value() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.page_text("Cadastro")
    click = rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/salvar")
    rec.page_text("Cadastro", f"Olá, {REDACTED}", "Registro salvo", after=click)
    result = compile_(rec.build())
    assert step(result, "conferir_registro_salvo").kind == "assert"
    assert REDACTED not in result.manifest.model_dump_json()


def test_a_field_in_another_tab_is_never_the_username() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.navigate("/ajuda", cause=NavigationCause.POPUP, page_id=1)
    # Campo de outra aba (a de ajuda), preenchido logo antes da senha.
    rec._add(
        InputEvent,
        page_id=1,
        element=textbox("Busca", id="busca"),
        value="ana.souza",
        typed="ana.souza",
    )
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    assert username_inputs(recording.events) == []
    result = compile_(recording)
    assert usernames_in(result) == []
    assert step(result, "clicar_entrar").effect is Effect.WRITE
