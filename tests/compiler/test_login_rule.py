"""Regra do login, sobre gravações montadas à mão (sem navegador).

Na dúvida o passo grava: um login tratado como gravação é aceitável, uma
transação tratada como login (leitura, com o usuário do cofre digitado num campo
de dado) é um defeito. O usuário só é reconhecido pelo próprio campo, e qualquer
outro campo de uma página com senha ou código vira uma entrada obrigatória.
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
from rapa.recorder.sensitivity import (
    analyze_logins,
    password_by_name,
    secret_page_inputs,
    username_by_signal,
)
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


def an_input(name: str) -> TextValue:
    return TextValue(template=f"{{{{ inputs.{name} }}}}")


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
    # Botões do vocabulário de login: o campo de dado não é credencial.
    ("Valor", "Continuar"),
    ("Chave Pix", "Entrar"),
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
    # O usuário do cofre nunca é digitado no campo de dado da operação, e o valor
    # (que o gravador tira da gravação) vira uma entrada obrigatória, com aviso.
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert [f.required for f in result.manifest.inputs] == [True]
    assert any(field_label in w and "entrada" in w for w in result.warnings)
    assert "12345678909" not in result.manifest.model_dump_json()
    found = secret_page_inputs(_rebuilt(field, button_label).events)
    assert found.usernames == []
    assert [e.element.id for e in found.others] == ([] if marked_by_script else ["campo"])


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
    # Sem login confirmado, o campo do usuário não vira o usuário do cofre.
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")


def test_confirm_only_counts_when_only_codes_were_filled() -> None:
    rec = RecordingBuilder()
    rec.navigate("/codigo")
    rec.fill(otp(id="codigo"), None)
    rec.click(button("Confirmar", id="ok", form=OTP_FORM))
    rec.request("POST", "/codigo")
    result = compile_(rec.build())
    assert step(result, "clicar_confirmar").effect is Effect.READ
    # O mesmo botão depois de usuário e senha não é login.
    assert step(_login("Confirmar"), "clicar_confirmar").effect is Effect.WRITE


@pytest.mark.parametrize("label", ["Confirmar", "Continuar", "Próximo", "Avançar", "Entrar"])
@pytest.mark.parametrize("form", [None, OTP_FORM, LOGIN_FORM], ids=["sem_form", "codigo", "login"])
def test_a_code_alone_is_a_login_only_in_the_form_of_the_code(
    label: str, form: FormInfo | None
) -> None:
    # Só o código foi preenchido: qualquer botão de login (e "Confirmar") só vale no
    # <form> que só tem o código; sem <form> ou noutro <form>, o envio grava.
    rec = RecordingBuilder()
    rec.navigate("/codigo")
    rec.fill(otp(id="codigo", form=form), None)
    rec.click(button(label, id="ok", form=form))
    rec.request("POST", "/api/codigo", "fetch")
    result = compile_(rec.build())
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    if form is OTP_FORM:
        assert click.effect is Effect.READ
    else:
        assert click.effect is Effect.WRITE


@pytest.mark.parametrize(
    "form",
    [None, LOGIN_FORM, PIX_FORM, FormInfo(action=f"{BASE}/pix", method="post", text_fields=1)],
    ids=["sem_form", "login", "pix", "com_outro_campo"],
)
def test_confirm_with_a_token_after_a_login_of_a_spa_is_write(form: FormInfo | None) -> None:
    # S7: login na SPA e, no mesmo documento, "Token" e "Confirmar" (paga o Pix).
    rec = RecordingBuilder()
    rec.navigate("/app")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.navigate("/app/pix/confirmar", cause=NavigationCause.SCRIPT, same_document=True)
    token = otp("Token", id="token", form=form, autocomplete="one-time-code")
    rec.fill(token, None)
    rec.click(button("Confirmar", id="confirmar", form=form))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert step(result, "clicar_confirmar").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


def test_confirm_with_a_token_in_a_form_of_only_the_code_stays_a_login() -> None:
    # Resíduo (como em d746ee2): o mesmo "Confirmar" num <form> que só tem o código é
    # login, ainda que a página use o código para confirmar uma operação.
    rec = RecordingBuilder()
    rec.navigate("/app")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.fill(otp("Token", id="token"), None)
    rec.click(button("Confirmar", id="confirmar", form=OTP_FORM))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_confirmar").effect is Effect.READ


def test_confirm_in_the_form_of_the_code_after_a_value_and_a_password_is_write() -> None:
    # O "Confirmar" fica no formulário que só tem o código, mas a pessoa preencheu
    # o valor e a senha no outro formulário: vale o que foi preenchido na página.
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor", form=PIX_FORM), "1250,00")
    rec.fill(password(id="senha", form=PIX_FORM), None)
    rec.fill(otp(id="cod", form=OTP_FORM), None)
    rec.click(button("Confirmar", id="ok", form=OTP_FORM))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_confirmar").effect is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert "1250,00" not in result.manifest.model_dump_json()


@pytest.mark.parametrize(
    "code",
    [
        otp("Token da transação", id="tok", form=PIX_FORM),
        otp("Código de segurança", id="cvv", form=PIX_FORM),
        otp("CVV", id="cvv", form=PIX_FORM),
        otp("Assinatura eletrônica", id="ass", form=PIX_FORM),
    ],
    ids=["token-da-transacao", "codigo-de-seguranca", "cvv", "assinatura"],
)
@pytest.mark.parametrize("button_label", ["Continuar", "Entrar", "Confirmar"])
@pytest.mark.parametrize("with_username", [False, True], ids=["valor", "usuario"])
def test_a_transaction_code_never_makes_a_login(
    code: ElementDescriptor, button_label: str, with_username: bool
) -> None:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    if with_username:
        rec.fill(textbox("Usuário", id="usuario", form=PIX_FORM), "ana.souza")
        rec.fill(password(id="senha", form=PIX_FORM), None)
    rec.fill(code, None)
    rec.click(button(button_label, id="ok", form=PIX_FORM))
    rec.request("POST", "/pix")
    result = compile_(rec.build())
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    assert usernames_in(result) == []


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
    # O gravador tira o valor (página com senha); o robô pede o usuário.
    assert fill_value(result, "digitar_usuario") == an_input("usuario")
    assert "ana.souza" not in result.manifest.model_dump_json()


def test_a_username_marked_by_the_script_without_a_login_becomes_an_input() -> None:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(script_username("Valor", id="valor"), None)
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Pagar agora", id="ok"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_valor") == an_input("valor")
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


# -- o Enter só é login pelo clique implícito no botão do mesmo formulário ---------


@pytest.mark.parametrize("form", [None, LOGIN_FORM], ids=["sem_formulario", "formulario"])
def test_an_enter_without_the_click_of_the_form_is_never_a_login(form: FormInfo | None) -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=form), "ana.souza")
    senha = password(id="senha", form=form)
    rec.fill(senha, None)
    enter = rec.key("Enter", senha)
    rec.request("POST", "/api/login", "fetch")
    rec.navigate("/inicio", cause=NavigationCause.KEY, trigger=enter)
    result = compile_(rec.build())
    press = next(s for s in result.manifest.steps if isinstance(s, PressStep))
    assert press.effect is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")


@pytest.mark.parametrize("form", [None, PIX_FORM], ids=["sem_formulario", "formulario"])
def test_an_enter_in_the_password_of_a_transaction_is_write(form: FormInfo | None) -> None:
    # A página trata o Enter na senha e paga (fetch POST); o botão "Pagar" fica lá.
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor", form=form), "1250,00")
    senha = password(id="senha", form=form)
    rec.fill(senha, None)
    rec.key("Enter", senha)
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "pressionar_enter").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert "1250,00" not in result.manifest.model_dump_json()


@pytest.mark.parametrize("same_form", [True, False], ids=["mesmo_formulario", "outro_formulario"])
def test_an_enter_is_a_login_through_the_implicit_click_of_the_same_form(same_form: bool) -> None:
    other = FormInfo(action=f"{BASE}/busca", method="get", text_fields=1)
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    senha = password(id="senha", form=LOGIN_FORM)
    rec.fill(senha, None)
    rec.key("Enter", senha)
    click = rec.click(
        button("Entrar", id="entrar", form=LOGIN_FORM if same_form else other), detail=0
    )
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    result = compile_(rec.build())
    entrar = step(result, "clicar_entrar")
    assert not any(isinstance(s, PressStep) for s in result.manifest.steps)
    if same_form:
        assert entrar.effect is Effect.READ
        assert fill_value(result, "digitar_usuario") == USERNAME
    else:
        assert entrar.effect is Effect.WRITE
        assert fill_value(result, "digitar_usuario") == an_input("usuario")


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


def test_after_a_login_only_codes_may_follow_on_the_same_page() -> None:
    # Depois do login (SPA), um "Confirme sua senha" com "Continuar" que libera uma
    # operação: a senha de novo, na mesma página, não é login.
    rec = RecordingBuilder()
    rec.navigate("/app")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.fill(password(id="confirma", form=None), None)
    rec.click(button("Continuar", id="continuar"))
    rec.request("POST", "/api/notas/123/liberar", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert step(result, "clicar_continuar").effect is Effect.WRITE
    assert fill_value(result, "digitar_usuario") == USERNAME


@pytest.mark.parametrize("before", ["outra_pagina", "mesma_pagina"])
def test_a_login_shaped_submit_after_a_write_does_not_split_the_page(before: str) -> None:
    # B3: algo gravou antes ("Gerar boleto"); o envio com cara de login não vale e não
    # divide a página: o valor preenchido depois continua numa página com senha.
    rec = RecordingBuilder()
    if before == "outra_pagina":
        rec.navigate("/inicio")
        boleto = rec.click(button("Gerar boleto", id="boleto"))
        rec.request("POST", "/api/boleto", "fetch")
        rec.request("GET", "/app")
        rec.navigate("/app", cause=NavigationCause.CLICK, trigger=boleto)
    else:
        rec.navigate("/app")
        rec.click(button("Gerar boleto", id="boleto"))
        rec.request("POST", "/api/boleto", "fetch")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(id="senha"), None)
    rec.click(button("Entrar", id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/api/login", "fetch")
    rec.fill(textbox("Valor", id="valor"), "1250,00")
    rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/api/salvar", "fetch")
    recording = rec.build()
    analysis = analyze_logins(recording.events)
    assert analysis.logins == ()
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames] == ["ana.souza"]
    assert [e.value for e in found.others] == ["1250,00"]
    result = compile_(recording)
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert fill_value(result, "digitar_usuario") == an_input("usuario")
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert any("Valor" in w and "senha ou código" in w for w in result.warnings)
    dumped = result.manifest.model_dump_json()
    assert "1250,00" not in dumped and "ana.souza" not in dumped


def test_a_write_anywhere_before_keeps_the_recorder_and_the_compiler_together() -> None:
    # O que grava conta do mesmo jeito no gravador e no compilador: o Tab que deixa o
    # campo, a edição com salvamento automático e o botão com verbo irreversível.
    for variant in ("tab", "edicao", "verbo"):
        rec = RecordingBuilder()
        rec.navigate("/app")
        nota = textbox("Observação", id="obs")
        if variant == "edicao":
            started = rec.page_text("App")
            rec.request("POST", "/api/rascunho", "fetch")
            rec.fill(nota, "rascunho", editing_since=started)
        elif variant == "tab":
            rec.fill(nota, "rascunho")
            rec.key("Tab", nota)
            rec.request("POST", "/api/rascunho", "fetch")
        else:
            rec.click(button("Excluir rascunho", id="excluir"))
        rec.navigate("/login")
        rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
        rec.fill(password(id="senha"), None)
        rec.click(button("Entrar", id="entrar", form=LOGIN_FORM))
        rec.request("POST", "/api/login", "fetch")
        recording = rec.build()
        assert analyze_logins(recording.events).logins == (), variant
        result = compile_(recording)
        assert step(result, "clicar_entrar").effect is Effect.WRITE, variant
        assert fill_value(result, "digitar_usuario") == an_input("usuario"), variant


def test_a_field_filled_after_the_login_on_the_same_page_stays_literal() -> None:
    rec = RecordingBuilder()
    rec.navigate("/app")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    rec.fill(textbox("Nome completo", id="nome"), "Maria da Silva")
    rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/api/cadastro", "fetch")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_usuario") == USERNAME
    assert fill_value(result, "digitar_nome_completo") == TextValue(template="Maria da Silva")
    assert step(result, "clicar_salvar").effect is Effect.WRITE


@pytest.mark.parametrize("button_label", ["Continuar", "Avançar", "Entrar", "Confirmar"])
@pytest.mark.parametrize("with_form", [True, False], ids=["formulario", "sem_formulario"])
@pytest.mark.parametrize("field_label", ["CPF do favorecido", "Usuário", "E-mail"])
def test_a_username_and_a_code_without_a_password_is_not_a_login(
    button_label: str, with_form: bool, field_label: str
) -> None:
    # S8 (t2, t2b): um campo com palavra de usuário e o código do SMS, sem senha.
    form = FormInfo(action=f"{BASE}/ted", method="post", has_one_time_code=True, text_fields=1)
    rec = RecordingBuilder()
    rec.navigate("/ted")
    rec.fill(textbox(field_label, id="campo", form=form if with_form else None), "11122233344")
    rec.fill(otp("Código enviado por SMS", id="sms", form=form if with_form else None), None)
    rec.click(button(button_label, id="ok", form=form if with_form else None))
    rec.request("POST", "/ted")
    recording = rec.build()
    assert analyze_logins(recording.events).logins == ()
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames + found.others] == ["11122233344"]
    result = compile_(recording)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert "11122233344" not in result.manifest.model_dump_json()


ANOTHER_PARTY = [
    textbox("CPF do favorecido", id="campo"),
    textbox("Chave Pix (CPF)", id="campo"),
    textbox("E-mail do destinatário", input_type="email", id="campo"),
    textbox("Login do novo usuário", id="campo"),
    textbox("CPF do beneficiário", id="campo"),
    textbox("CPF do pagador", id="campo"),
    textbox("E-mail do recebedor", id="campo"),
    textbox("CPF do titular", id="campo"),
    textbox("E-mail do cliente", id="campo"),
    textbox("E-mail de contato", input_type="email", id="campo"),
    textbox("E-mail do convidado", id="campo"),
    textbox("Usuário", id="campo", name_attr="cadastro_usuario"),
    textbox("Usuário", id="campo", placeholder="Cadastre o seu usuário"),
    textbox("Acesso", id="cpfFavorecido"),
    textbox("Usuário", id="campo", autocomplete="username").model_copy(
        update={"name": "Nova conta"}
    ),
]


@pytest.mark.parametrize("field", ANOTHER_PARTY, ids=lambda f: f.label + "|" + (f.id or ""))
@pytest.mark.parametrize("password_first", [False, True], ids=["usuario_antes", "senha_antes"])
def test_a_field_that_names_another_party_is_never_the_username(
    field: ElementDescriptor, password_first: bool
) -> None:
    # t1, t3, t5, t10: o campo tem palavra de usuário, mas é o dado de outra pessoa
    # (ou de quem está sendo cadastrado): numa página com senha, vira uma entrada.
    assert not username_by_signal(field)
    rec = RecordingBuilder()
    rec.navigate("/pix")
    if password_first:
        rec.fill(password(id="senha", form=None), None)
    rec.fill(field, "12345678909")
    if not password_first:
        rec.fill(password(id="senha", form=None), None)
    rec.click(button("Continuar", id="continuar"))
    rec.request("POST", "/api/pix", "fetch")
    recording = rec.build()
    found = secret_page_inputs(recording.events)
    assert found.usernames == [] and [e.value for e in found.others] == ["12345678909"]
    result = compile_(recording)
    assert step(result, "clicar_continuar").effect is Effect.WRITE
    assert usernames_in(result) == []
    (value,) = (
        s.value
        for s in result.manifest.steps
        if isinstance(s, FillStep) and not isinstance(s.value, SecretValue)
    )
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert "12345678909" not in result.manifest.model_dump_json()


def test_a_declared_username_that_names_another_party_is_an_input() -> None:
    # t9: o script esconde o campo (autocomplete=username), mas ele não é o usuário.
    favorecido = script_username("Favorecido", id="fav", autocomplete="username", form=PIX_FORM)
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(favorecido.model_copy(update={"is_username": False}), None)
    rec.fill(password(id="senha", form=PIX_FORM), None)
    rec.click(button("Continuar", id="ok", form=PIX_FORM))
    rec.request("POST", "/pix")
    result = compile_(rec.build())
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_favorecido") == an_input("favorecido")
    assert step(result, "clicar_continuar").effect is Effect.WRITE


@pytest.mark.parametrize("button_label", ["Continuar", "Próximo", "Entrar"])
@pytest.mark.parametrize(
    "user",
    [
        textbox("E-mail", input_type="email", id="email", autocomplete="email"),
        textbox("Login do novo usuário", id="login"),
        textbox("Usuário", id="usuario"),
    ],
    ids=["e-mail", "novo-usuario", "usuario"],
)
def test_a_new_password_is_never_a_login(button_label: str, user: ElementDescriptor) -> None:
    # S9, t5, t6: cadastro (ou troca de senha) com autocomplete=new-password.
    signup = FormInfo(action=f"{BASE}/cadastro", method="post", has_password=True, text_fields=1)
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(user.model_copy(update={"form": signup}), "novo@exemplo.com")
    rec.fill(password("Crie uma senha", id="nova", form=signup, autocomplete="new-password"), None)
    click = rec.click(button(button_label, id="ok", form=signup))
    rec.request("POST", "/cadastro")
    rec.navigate("/ok", cause=NavigationCause.CLICK, trigger=click)
    recording = rec.build()
    assert analyze_logins(recording.events).logins == ()
    result = compile_(recording)
    assert next(s for s in result.manifest.steps if isinstance(s, ClickStep)).effect is (
        Effect.WRITE
    )
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert "novo@exemplo.com" not in result.manifest.model_dump_json()


def test_a_new_password_never_bridges_a_two_step_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(textbox("E-mail", input_type="email", id="email"), "novo@exemplo.com")
    rec.click(button("Continuar", id="continuar"))
    rec.fill(password("Crie uma senha", id="nova", form=None, autocomplete="new-password"), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/cadastro", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []


def test_a_password_without_a_username_is_not_a_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/pagar")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Continuar", id="continuar"))
    rec.request("POST", "/api/pagar", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_continuar").effect is Effect.WRITE


def test_two_passwords_are_not_one_credential() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.fill(password("Senha atual", id="atual", form=None), None)
    rec.fill(password("Nova senha", id="nova", form=None), None)
    rec.click(button("Continuar", id="continuar"))
    rec.request("POST", "/api/senha", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_continuar").effect is Effect.WRITE
    assert usernames_in(result) == []


# -- usuário só pelo próprio campo ------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        textbox("Usuário", id="campo"),
        textbox("USER", id="campo"),
        textbox("Login", id="campo"),
        textbox("E-mail", id="campo"),
        textbox("Endereço", input_type="email", id="campo"),
        textbox("Acesso", id="campo", autocomplete="username"),
        textbox("Acesso", id="campo", autocomplete="email"),
        textbox("Acesso", id="campo", name_attr="j_username"),
        textbox("Acesso", id="txtCPF"),
        textbox("Acesso", id="campo", placeholder="Seu CNPJ"),
        textbox("Matrícula", id="campo"),
        textbox("Código do usuário", id="campo"),
    ],
    ids=[
        "usuario",
        "user",
        "login",
        "e-mail",
        "type-email",
        "autocomplete-username",
        "autocomplete-email",
        "name",
        "id",
        "placeholder",
        "matricula",
        "codigo-do-usuario",
    ],
)
def test_only_the_field_itself_makes_a_username(field: ElementDescriptor) -> None:
    assert username_by_signal(field)
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(field, "ana.souza")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    assert [e.value for e in secret_page_inputs(recording.events).usernames] == ["ana.souza"]
    result = compile_(recording)
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert len(usernames_in(result)) == 1


@pytest.mark.parametrize(
    "field",
    [
        textbox("Conta", id="campo"),
        textbox("Agência", id="campo"),
        textbox("Chave Pix", id="campo"),
        textbox("Valor", id="campo"),
        textbox("Usuários", id="campo"),
        # O nome acessível (aria-labelledby num título, por exemplo) não conta.
        textbox("Identificação", id="campo").model_copy(update={"name": "Usuário"}),
        textbox("Busca", input_type="search", id="campo", name_attr="usuario"),
    ],
    ids=["conta", "agencia", "chave-pix", "valor", "usuarios", "nome-acessivel", "busca"],
)
def test_any_other_field_beside_a_password_is_an_input(field: ElementDescriptor) -> None:
    assert not username_by_signal(field)
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(field, "12345-6")
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    found = secret_page_inputs(recording.events)
    assert found.usernames == [] and [e.value for e in found.others] == ["12345-6"]
    result = compile_(recording)
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert any("página que pede senha ou código" in w for w in result.warnings)
    assert "12345-6" not in result.manifest.model_dump_json()


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
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert [f.name for f in result.manifest.inputs] == ["valor"]
    assert usernames_in(result) == []
    assert step(result, "clicar_confirmar_pagamento").effect is Effect.IRREVERSIBLE
    assert result.manifest.effect() is Effect.IRREVERSIBLE
    assert "1250,00" not in result.manifest.model_dump_json()
    assert secret_page_inputs(recording.events).usernames == []


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
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames] == ["ana.souza"] and found.others == []
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


def test_a_write_between_the_steps_of_a_two_step_login_drops_it() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario"), "ana.souza")
    rec.click(button("Continuar", id="continuar"))
    senha = password(id="senha", form=None)
    started = rec.page_text("Senha")
    rec.request("POST", "/api/rascunho", "fetch")  # a página grava enquanto a pessoa digita
    rec.fill(senha, None, editing_since=started)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    result = compile_(rec.build())
    assert step(result, "digitar_senha").effect is Effect.WRITE
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")


@pytest.mark.parametrize(
    "variant",
    [
        "ponte_com_texto_a_mais",
        "lista_antes",
        "outro_campo_antes",
        "senha_em_campo_de_texto",
        "enter_como_ponte",
        "outro_clique_antes",
    ],
)
def test_two_step_login_needs_the_exact_shape(variant: str) -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    if variant == "lista_antes":
        rec.select(select_box("Perfil", id="perfil"), "Contador")
    if variant == "outro_campo_antes":
        rec.fill(textbox("Empresa", id="empresa"), "ACME")
    if variant == "outro_clique_antes":
        rec.click(button("Aceitar cookies", id="cookies"))
    usuario = textbox("Usuário", id="usuario")
    rec.fill(usuario, "ana.souza")
    if variant == "enter_como_ponte":
        rec.key("Enter", usuario)
    else:
        bridge = "Continuar para a senha" if variant == "ponte_com_texto_a_mais" else "Continuar"
        rec.click(button(bridge, id="continuar"))
    if variant == "senha_em_campo_de_texto":
        rec.fill(textbox("Senha", id="senha_texto"), "S3nh@Forte!")
    else:
        rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/login", "fetch")
    recording = rec.build()
    result = compile_(recording)
    # Sem a forma exata o envio grava; o usuário (que o gravador tira da gravação,
    # por estar numa página com senha) vira uma entrada obrigatória.
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")
    assert "ana.souza" not in result.manifest.model_dump_json()


# -- login em duas páginas: o usuário sai da gravação; vira o do cofre só na forma exata


def _two_pages(variant: str) -> Recording:
    """Usuário e "Continuar" numa página; a ponte carrega a página da senha e "Entrar"."""
    rec = RecordingBuilder()
    if variant == "gravou_antes":
        rec.navigate("/inicio")
        rec.click(button("Aceitar termos", id="termos"))
        rec.request("POST", "/api/termos", "fetch")
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", name_attr="usuario"), "ana.souza")
    if variant == "lembrar":
        rec.check(checkbox("Lembrar meu usuário", id="lembrar"))
    if variant == "outro_campo":
        rec.fill(textbox("Empresa", id="empresa"), "ACME")
    bridge = "Entrar" if variant == "ponte_entrar" else "Continuar"
    continuar = rec.click(button(bridge, id="continuar"))
    method = "POST" if variant == "ponte_post" else "GET"
    rec.request(method, "/login/senha")
    cause = NavigationCause.TYPED if variant == "digitado" else NavigationCause.CLICK
    rec.navigate(
        "/login/senha", cause=cause, trigger=continuar if cause is NavigationCause.CLICK else None
    )
    if variant == "senha_de_texto":
        rec.fill(textbox("Senha", id="senha", sensitive=True, is_password=True), None)
    else:
        rec.fill(password(id="senha", form=LOGIN_FORM), None)
    if variant == "codigo":
        rec.fill(otp(id="codigo"), None)
    final = "Confirmar" if variant == "confirmar" else "Entrar"
    entrar = rec.click(button(final, id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/login/senha")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=entrar)
    rec.page_text("Início", text="Olá, ana.souza")
    return rec.build()


@pytest.mark.parametrize("variant", ["simples", "lembrar", "ponte_post"])
def test_a_login_over_two_pages_gives_the_vault_username(variant: str) -> None:
    recording = _two_pages(variant)
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames] == ["ana.souza"] and found.others == []
    (login,) = analyze_logins(recording.events).logins
    assert login.cross_document
    result = compile_(recording)
    assert fill_value(result, "digitar_usuario") == USERNAME
    assert fill_value(result, "digitar_senha") == PASSWORD
    # A ponte e o envio ficam com o efeito das requisições deles (na dúvida, gravam).
    expected = Effect.WRITE if variant == "ponte_post" else Effect.READ
    assert step(result, "clicar_continuar").effect is expected
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert "ana.souza" not in result.manifest.model_dump_json()


@pytest.mark.parametrize(
    "variant",
    [
        "gravou_antes",
        "outro_campo",
        "ponte_entrar",
        "senha_de_texto",
        "codigo",
        "confirmar",
    ],
)
def test_a_login_over_two_pages_needs_the_exact_shape(variant: str) -> None:
    recording = _two_pages(variant)
    # O usuário (pelo próprio campo) leva à página da senha: sai da gravação.
    found = secret_page_inputs(recording.events)
    assert [e.value for e in found.usernames] == ["ana.souza"]
    result = compile_(recording)
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_usuario") == an_input("usuario")
    assert any("Usuário" in w and "entrada" in w for w in result.warnings)
    assert "ana.souza" not in result.manifest.model_dump_json()


def test_a_typed_address_between_the_pages_keeps_the_field_literal() -> None:
    recording = _two_pages("digitado")
    assert secret_page_inputs(recording.events).usernames == []
    result = compile_(recording)
    assert fill_value(result, "digitar_usuario") == TextValue(template="ana.souza")
    assert usernames_in(result) == []


def test_a_login_over_two_pages_never_lowers_an_effect() -> None:
    # Mesmo na forma exata, o envio com POST grava: o login em duas páginas só decide
    # o usuário do cofre.
    recording = _two_pages("simples")
    result = compile_(recording)
    effects = {s.id: s.effect for s in result.manifest.steps}
    assert effects["clicar_entrar"] is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


@pytest.mark.parametrize("final", ["Confirmar", "Confirmar transação", "Concluir", "OK", "Entrar"])
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
    assert secret_page_inputs(recording.events).usernames == []
    result = compile_(recording)
    assert usernames_in(result) == []
    assert result.manifest.effect() in (Effect.WRITE, Effect.IRREVERSIBLE)
    # O valor (numa página com senha) vira uma entrada, sem o valor digitado.
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert "1250,00" not in result.manifest.model_dump_json()


@pytest.mark.parametrize("form", [None, PIX_FORM], ids=["sem_formulario", "formulario"])
def test_an_enter_never_bridges_a_two_step_login(form: FormInfo | None) -> None:
    # "Chave Pix" + Enter (a página mostra a senha) + Senha + "Entrar" (paga).
    rec = RecordingBuilder()
    rec.navigate("/pix")
    chave = textbox("Chave Pix", id="chave", form=form)
    rec.fill(chave, "ana@exemplo.com.br")
    rec.key("Enter", chave)
    rec.fill(password(id="senha", form=form), None)
    rec.click(button("Entrar", id="entrar", form=form))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    assert fill_value(result, "digitar_chave_pix") == an_input("chave_pix")


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
    assert secret_page_inputs(recording.events).usernames == []
    result = compile_(recording)
    # Na mesma página da senha: o valor da busca vira uma entrada, nunca o usuário.
    assert fill_value(result, "digitar_buscar_nota") == an_input("buscar_nota")
    assert usernames_in(result) == []
    assert step(result, "clicar_confirmar").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


def test_the_compiler_never_turns_a_hidden_field_into_the_username() -> None:
    # O script escondeu o campo (grupo com senha) sem chamá-lo de usuário; mesmo com
    # um botão de login depois, ele não vira o usuário do cofre.
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor", sensitive=True), None)
    rec.fill(password(id="senha", form=None), None)
    rec.click(button("Entrar", id="entrar"))
    rec.request("POST", "/api/pix", "fetch")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_valor") == an_input("valor")
    assert step(result, "clicar_entrar").effect is Effect.WRITE


# -- valores apagados pela limpeza e senha pelo nome acessível ----------------------


def test_a_fill_with_a_removed_value_becomes_an_input() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(textbox("Departamento", id="dep"), f"{REDACTED} Corporativo")
    rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/salvar")
    result = compile_(rec.build())
    assert fill_value(result, "digitar_departamento") == an_input("departamento")
    assert REDACTED not in result.manifest.model_dump_json()
    assert any("Departamento" in w and "sensível" in w for w in result.warnings)


def _named_password(button_label: str, *, sensitive: bool, aria: bool = False) -> CompileResult:
    form = FormInfo(action=f"{BASE}/entrar", method="post", text_fields=2)
    cod = element_named_password(sensitive=sensitive, form=form, aria=aria)
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


def element_named_password(
    *, sensitive: bool, form: FormInfo, aria: bool = False
) -> ElementDescriptor:
    field = textbox("Código pessoal", id="cod", form=form, sensitive=sensitive)
    if aria:
        # aria-labelledby ("Código pessoal") e aria-label de senha: o script manda o sinal.
        return field.model_copy(update={"aria_mentions_password": True})
    return field.model_copy(update={"name": "Digite a senha de 6 dígitos"})


@pytest.mark.parametrize("button_label", ["Entrar", "Gravar"])
@pytest.mark.parametrize(
    ("sensitive", "aria"),
    [(True, False), (False, False), (False, True)],
    ids=["marcado", "sem_marca", "aria_label"],
)
def test_a_field_named_like_a_password_is_always_an_input(
    button_label: str, sensitive: bool, aria: bool
) -> None:
    result = _named_password(button_label, sensitive=sensitive, aria=aria)
    named = next(
        s for s in result.manifest.steps if isinstance(s, FillStep) and s.id not in ("digitar_cpf",)
    )
    assert isinstance(named.value, TextValue) and named.value.template.startswith("{{ inputs.")
    # Não é uma senha de verdade: o envio grava, e o CPF (página com senha) vira entrada.
    assert fill_value(result, "digitar_cpf") == an_input("cpf")
    assert [f.required for f in result.manifest.inputs] == [True, True]
    assert any("fala em senha" in w for w in result.warnings)
    click = next(s for s in result.manifest.steps if isinstance(s, ClickStep))
    assert click.effect is Effect.WRITE
    dumped = result.model_dump_json()
    assert "918273" not in dumped and "12345678909" not in dumped
    assert result.manifest.credentials == []


@pytest.mark.parametrize("marked", [True, False], ids=["marcado", "sem_marca"])
@pytest.mark.parametrize("with_password", [False, True], ids=["sozinho", "com_senha"])
@pytest.mark.parametrize(
    "field",
    [
        # F1: rótulo "CPF" e aria-labelledby "Digite a senha numérica".
        textbox("CPF", id="campo", name_attr="cpf").model_copy(
            update={"name": "Digite a senha numérica", "aria_mentions_password": True}
        ),
        # F2: rótulo "Usuário" e aria-label "Senha do usuário".
        textbox("Usuário", id="campo").model_copy(
            update={"name": "Senha do usuário", "aria_mentions_password": True}
        ),
        # Só o sinal do script (o nome acessível veio do aria-labelledby).
        textbox("E-mail", input_type="email", id="campo").model_copy(
            update={"aria_mentions_password": True}
        ),
    ],
    ids=["cpf", "usuario", "e-mail"],
)
def test_a_field_named_like_a_password_is_never_a_credential(
    field: ElementDescriptor, with_password: bool, marked: bool
) -> None:
    # O campo diz ser o usuário e o nome diz ser senha: ambíguo, nunca credencial
    # (sem a marca do script, o compilador descarta o valor gravado).
    assert password_by_name(field) and not username_by_signal(field)
    form = FormInfo(action=f"{BASE}/login", method="post", has_password=with_password)
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(field.model_copy(update={"form": form, "sensitive": marked}), "918273")
    if with_password:
        rec.fill(password(id="senha", form=form), None)
    click = rec.click(button("Entrar", id="entrar", form=form))
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    recording = rec.build()
    assert analyze_logins(recording.events).logins == ()
    result = compile_(recording)
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert usernames_in(result) == []
    value = next(s.value for s in result.manifest.steps if isinstance(s, FillStep))
    assert isinstance(value, TextValue) and value.template.startswith("{{ inputs.")
    assert [f.required for f in result.manifest.inputs] == [True]
    assert any("fala em senha" in w for w in result.warnings)
    assert "918273" not in result.model_dump_json()


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
    assert secret_page_inputs(recording.events).usernames == []
    result = compile_(recording)
    assert usernames_in(result) == []
    assert step(result, "clicar_entrar").effect is Effect.WRITE
