"""Regras de compilação sobre gravações montadas à mão (sem navegador)."""

import json
import re

import pytest

from rapa.compiler import CompileError, CompileResult, lower
from rapa.core import policy
from rapa.core.robot import (
    STEP_ID_PATTERN,
    AssertStep,
    CheckStep,
    ClickStep,
    CredentialKind,
    DownloadStep,
    Effect,
    FieldType,
    FillStep,
    LocatorBy,
    Manifest,
    NavigateStep,
    PressStep,
    SecretValue,
    SelectStep,
    Step,
    TextValue,
    UploadStep,
)
from rapa.recorder.models import (
    ElementDescriptor,
    FormInfo,
    FrameInfo,
    InputEvent,
    NavigationCause,
    Recording,
    RowInfo,
)
from rapa.recorder.sensitivity import field_key, secret_page_inputs
from tests.compiler.builders import (
    BASE,
    LOGIN_FORM,
    RecordingBuilder,
    button,
    checkbox,
    element,
    file_input,
    link,
    otp,
    password,
    select_box,
    textbox,
)


def compile_(recording: Recording) -> CompileResult:
    result = lower(recording, name="robo_teste", title="Robô de teste", description="Teste.")
    assert policy.check(result.manifest) == []
    Manifest.model_validate(result.manifest.model_dump(mode="json"))
    return result


def steps(result: CompileResult) -> list[Step]:
    return list(result.manifest.steps)


def step(result: CompileResult, step_id: str) -> Step:
    found = result.manifest.step_by_id(step_id)
    assert found is not None, [s.id for s in result.manifest.steps]
    return found


# -- navegação -------------------------------------------------------------------


def test_first_navigation_becomes_navigate_with_escaped_url() -> None:
    rec = RecordingBuilder()
    rec.navigate("/busca?q={{x}}")
    result = compile_(rec.build())
    first = steps(result)[0]
    assert isinstance(first, NavigateStep)
    assert first.url == BASE + "/busca?q={{{{x}}"
    assert first.id == "abrir_busca"
    assert first.title == "Abrir a página de busca"
    assert result.manifest.allowed_domains == ["portal.exemplo.gov.br"]


def test_click_navigation_sets_expect_navigation_and_typed_becomes_navigate() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.click_to(link("Notas fiscais"), "/notas")
    rec.navigate("/relatorio", cause=NavigationCause.TYPED)
    rec.navigate("/relatorio/pronto", cause=NavigationCause.SCRIPT)
    result = compile_(rec.build())
    kinds = [(s.kind, s.id) for s in steps(result)]
    assert kinds == [
        ("navigate", "abrir_inicio"),
        ("click", "clicar_notas_fiscais"),
        ("navigate", "abrir_relatorio"),
    ]
    click = step(result, "clicar_notas_fiscais")
    assert isinstance(click, ClickStep) and click.expect_navigation
    assert click.title == "Clicar em “Notas fiscais”"
    assert step(result, "abrir_inicio").title == "Abrir a página inicial"


def test_same_document_navigation_does_not_expect_navigation() -> None:
    rec = RecordingBuilder()
    rec.navigate("/app")
    click = rec.click(link("Relatórios"))
    rec.navigate("/app#relatorios", cause=NavigationCause.CLICK, trigger=click, same_document=True)
    result = compile_(rec.build())
    clicked = step(result, "clicar_relatorios")
    assert isinstance(clicked, ClickStep) and not clicked.expect_navigation


def test_iframe_hosts_are_allowed_domains() -> None:
    rec = RecordingBuilder()
    rec.navigate("/quadro")
    rec.navigate("https://consulta.exemplo.gov.br/x", main_frame=False)
    frame = FrameInfo(id="quadro-consulta", title="Consulta")
    rec.click(button("Confirmar consulta", frames=[frame]))
    result = compile_(rec.build())
    assert result.manifest.allowed_domains == ["portal.exemplo.gov.br", "consulta.exemplo.gov.br"]
    clicked = step(result, "clicar_confirmar_consulta")
    assert isinstance(clicked, ClickStep)
    assert clicked.target.frames == ["#quadro-consulta"]


def test_frame_selector_falls_back_when_id_looks_generated() -> None:
    rec = RecordingBuilder()
    rec.navigate("/quadro")
    rec.click(button("Ok", frames=[FrameInfo(id="frame-8f3a9c2d11", title="Consulta")]))
    clicked = steps(compile_(rec.build()))[1]
    assert isinstance(clicked, ClickStep)
    assert clicked.target.frames == ['iframe[title="Consulta"]']


def test_popup_becomes_navigate_with_warning() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    click = rec.click(link("Abrir relatório"))
    rec.navigate("/relatorio", cause=NavigationCause.POPUP, trigger=click, page_id=1)
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate", "navigate"]
    assert any("nova aba" in w for w in result.warnings)


def test_ip_address_host_is_rejected() -> None:
    rec = RecordingBuilder(base="https://10.0.0.5")
    rec.navigate("/")
    with pytest.raises(CompileError, match="inválido"):
        lower(rec.build(), name="robo_teste", title="T", description="D")


def test_recording_without_pages_is_rejected() -> None:
    with pytest.raises(CompileError, match="nenhuma página"):
        lower(RecordingBuilder().build(), name="robo_teste", title="T", description="D")


# -- campos ----------------------------------------------------------------------


def test_successive_inputs_on_same_field_merge_into_one_fill() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    nome = textbox("Nome completo", id="nome")
    rec.fill(nome, "Maria")
    rec.fill(nome, "Maria da Silva")
    rec.fill(textbox("Cidade", id="cidade"), "Curitiba")
    rec.fill(nome, "Maria da Silva Souza")
    result = compile_(rec.build())
    fills = [s for s in steps(result) if isinstance(s, FillStep)]
    assert [s.id for s in fills] == [
        "digitar_nome_completo",
        "digitar_cidade",
        "digitar_nome_completo_2",
    ]
    first = fills[0]
    assert isinstance(first.value, TextValue) and first.value.template == "Maria da Silva"
    assert first.title == "Digitar o nome completo"
    assert result.provenance["digitar_nome_completo"] == 2
    assert fills[1].title == "Digitar a cidade"


def test_literal_values_are_escaped() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.fill(textbox("Observação"), "use {{ inputs.x }}")
    fill = steps(compile_(rec.build()))[1]
    assert isinstance(fill, FillStep) and isinstance(fill.value, TextValue)
    assert fill.value.template == "use {{{{ inputs.x }}"


def test_masked_field_is_sequential() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.fill(textbox("CPF", id="cpf"), "529.982.247-25", typed="52998224725")
    rec.fill(textbox("Telefone", id="tel"), "(41) 99999-0000", script_modified=True)
    rec.fill(textbox("Nome", id="nome"), "Ana")
    result = compile_(rec.build())
    cpf, tel, nome = (s for s in steps(result) if isinstance(s, FillStep))
    assert cpf.sequential and tel.sequential and not nome.sequential
    assert cpf.title == "Digitar o CPF"


def test_password_username_and_totp_become_secrets() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(), None)
    rec.click_to(button("Entrar", form=LOGIN_FORM), "/codigo", method="POST")
    rec.fill(otp(), None)
    rec.click_to(button("Confirmar", form=otp().form), "/inicio", method="POST")
    result = compile_(rec.build())
    usuario, senha, codigo = (s for s in steps(result) if isinstance(s, FillStep))
    assert usuario.value == SecretValue(credential="portal", field="username")
    assert senha.value == SecretValue(credential="portal", field="password")
    assert codigo.value == SecretValue(credential="portal_otp", field="totp")
    creds = {c.name: c for c in result.manifest.credentials}
    assert creds["portal"].kind is CredentialKind.PASSWORD
    assert creds["portal_otp"].kind is CredentialKind.TOTP
    assert creds["portal"].origin == BASE
    assert "ana.souza" not in result.manifest.model_dump_json()
    assert result.parameter_candidates == []
    # Login fica como leitura, mesmo com POST.
    for step_id in ("clicar_entrar", "clicar_confirmar"):
        assert step(result, step_id).effect is Effect.READ


def test_credentials_are_per_origin() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(password(), None)
    rec.navigate("https://outro.exemplo.gov.br:8443/entrar")
    rec.fill(password(), None)
    result = compile_(rec.build())
    origins = {c.origin for c in result.manifest.credentials}
    assert origins == {BASE, "https://outro.exemplo.gov.br:8443"}
    assert len({c.name for c in result.manifest.credentials}) == 2


def test_username_rule_only_for_text_field_on_same_page() -> None:
    rec = RecordingBuilder()
    rec.navigate("/busca")
    rec.fill(textbox("Busca", id="q"), "notas")
    rec.navigate("/login")
    rec.fill(password(), None)
    result = compile_(rec.build())
    busca = step(result, "digitar_busca")
    assert isinstance(busca, FillStep) and isinstance(busca.value, TextValue)


def test_password_without_tls_is_rejected() -> None:
    rec = RecordingBuilder(base="http://portal.exemplo.gov.br")
    rec.navigate("/login")
    rec.fill(password(), None)
    with pytest.raises(CompileError, match="https"):
        lower(rec.build(), name="robo_teste", title="T", description="D")


def test_select_check_upload_and_press() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    uf = select_box("UF", id="uf")
    rec.click(uf)  # clique só para abrir a lista: ignorado
    rec.select(uf, "PR")
    rec.select(uf, "SP")
    termo = checkbox("Aceito os termos")
    rec.click(termo)
    rec.check(termo)
    rec.file(file_input("Comprovante", id="comprovante"), "comprovante.pdf")
    busca = textbox("Pesquisar", id="busca")
    rec.fill(busca, "nota 10")
    rec.key("Tab", busca)
    rec.key("Enter", busca)
    rec.key("Escape")
    result = compile_(rec.build())
    kinds = [s.kind for s in steps(result)]
    assert kinds == ["navigate", "select", "check", "upload", "fill", "press", "press"]
    select = steps(result)[1]
    assert isinstance(select, SelectStep) and select.option == "SP"
    assert select.title == "Escolher a UF"
    check = steps(result)[2]
    assert isinstance(check, CheckStep) and check.checked
    assert check.title == "Marcar “Aceito os termos”"
    upload = steps(result)[3]
    assert isinstance(upload, UploadStep)
    assert upload.target.locators[0].by is LocatorBy.LABEL
    file_field = next(f for f in result.manifest.inputs if f.name == upload.file_input)
    assert file_field.type is FieldType.FILE
    enter, escape = steps(result)[5:]
    assert isinstance(enter, PressStep) and enter.key == "Enter" and enter.target is not None
    assert isinstance(escape, PressStep) and escape.key == "Escape" and escape.target is None
    assert [c.step_id for c in result.parameter_candidates] == [select.id, steps(result)[4].id]


def test_enter_followed_by_implicit_submit_keeps_only_the_click() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(password(), None)
    rec.key("Enter", password())
    entrar = button("Entrar", form=LOGIN_FORM)
    click = rec.click(entrar, detail=0)
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate", "fill", "click"]
    clicked = steps(result)[2]
    assert isinstance(clicked, ClickStep) and clicked.expect_navigation


def test_click_on_file_chooser_button_is_dropped() -> None:
    rec = RecordingBuilder()
    rec.navigate("/envio")
    rec.click(button("Escolher arquivo"))
    rec.file(file_input("Arquivo", id="arquivo"), "a.txt")
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate", "upload"]


def test_download_after_click() -> None:
    rec = RecordingBuilder()
    rec.navigate("/notas")
    row = RowInfo(
        text="001003 Clínica 278,50 Baixar",
        cells=["001003"],
        key="001003",
        role_name_count=1,
        text_count=1,
    )
    baixar = link("Baixar", row=row, counts={"role_name": 10, "text": 10})
    click = rec.click(baixar)
    rec.request("GET", "/notas/001003/download")
    rec.download("nota-001003.pdf", click)
    result = compile_(rec.build())
    download = steps(result)[1]
    assert isinstance(download, DownloadStep)
    assert download.save_as == "nota-001003.pdf"
    assert download.target.within is not None and download.target.within.value == "row"
    assert download.target.within_has_text == "001003"
    assert download.target.intent == "link “Baixar” da linha “001003”"
    assert [loc.by for loc in download.target.locators] == [LocatorBy.ROLE, LocatorBy.TEXT]
    assert download.effect is Effect.READ
    output = result.manifest.outputs[0]
    assert output.type is FieldType.FILE and output.source == f"vars.{download.into}"


def test_download_without_click_is_warned() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.download("auto.pdf", None)
    result = compile_(rec.build())
    assert any("auto.pdf" in w for w in result.warnings)


# -- localizadores ---------------------------------------------------------------


def test_locator_preference_order() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    el = element(
        "input",
        role="textbox",
        name="E-mail",
        label="E-mail",
        placeholder="voce@exemplo.com",
        test_id="campo-email",
        id="email",
        name_attr="email",
        input_type="email",
    )
    rec.fill(el, "a@b.com")
    fill = steps(compile_(rec.build()))[1]
    assert isinstance(fill, FillStep)
    assert [(loc.by.value, loc.value) for loc in fill.target.locators] == [
        ("role", "textbox"),
        ("label", "E-mail"),
        ("placeholder", "voce@exemplo.com"),
        ("test_id", "campo-email"),
        ("css", "#email"),
        ("css", 'input[name="email"]'),
    ]


@pytest.mark.parametrize(
    "generated",
    [
        "ember123",
        "j_idt45",
        ":r1:",
        "input-8f3a9c2d",
        "mat-input-0",
        "react-select-2-input",
        "x12345",
    ],
)
def test_generated_ids_are_not_used(generated: str) -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.click(element("div", id=generated, css="body > div:nth-of-type(2)"))
    result = compile_(rec.build())
    clicked = steps(result)[1]
    assert isinstance(clicked, ClickStep)
    assert all(generated not in loc.value for loc in clicked.target.locators)
    assert any("caminho CSS" in w for w in result.warnings)


def test_ambiguous_locators_are_dropped() -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.click(button("Salvar", id="salvar", counts={"role_name": 2, "text": 2}))
    clicked = steps(compile_(rec.build()))[1]
    assert isinstance(clicked, ClickStep)
    assert [loc.value for loc in clicked.target.locators] == ["#salvar"]


def test_probe_from_playwright_overrides_and_demotes() -> None:
    from rapa.recorder.models import LocatorProbe

    rec = RecordingBuilder()
    rec.navigate("/form")
    el = button("Emitir")
    el.probe = LocatorProbe(found=True, role_name=0, text=1)
    rec.click(el)
    clicked = steps(compile_(rec.build()))[1]
    assert isinstance(clicked, ClickStep)
    assert [loc.by for loc in clicked.target.locators] == [LocatorBy.TEXT, LocatorBy.ROLE]


# -- efeitos ---------------------------------------------------------------------


def test_click_with_post_xhr_is_write() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(textbox("Nome"), "Ana")
    rec.click(button("Salvar"))
    rec.request("POST", "/api/salvar", "fetch")
    rec.request("GET", "/api/lista", "xhr")
    result = compile_(rec.build())
    assert step(result, "clicar_salvar").effect is Effect.WRITE
    assert step(result, "digitar_nome").effect is Effect.READ


def test_get_only_click_is_read() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.click(button("Atualizar"))
    rec.request("GET", "/api/lista", "xhr")
    assert step(compile_(rec.build()), "clicar_atualizar").effect is Effect.READ


@pytest.mark.parametrize(
    "label",
    [
        "Emitir",
        "Enviar declaração",
        "Pagar",
        "Confirmar pagamento",
        "Excluir",
        "Assinar",
        "Concluir",
        "Transmitir",
        "Cancelar",
    ],
)
def test_irreversible_verbs(label: str) -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.click(button(label))
    result = compile_(rec.build())
    clicked = steps(result)[1]
    assert clicked.effect is Effect.IRREVERSIBLE


def test_link_with_verb_that_opens_a_form_is_read() -> None:
    rec = RecordingBuilder()
    rec.navigate("/inicio")
    rec.click_to(link("Emitir protocolo"), "/protocolo")
    rec.fill(textbox("Nome"), "Ana")
    result = compile_(rec.build())
    assert step(result, "clicar_emitir_protocolo").effect is Effect.READ
    assert any("só abriu um formulário" in w for w in result.warnings)


def test_link_with_verb_that_does_not_open_a_form_is_irreversible() -> None:
    rec = RecordingBuilder()
    rec.navigate("/itens")
    rec.click_to(link("Excluir"), "/itens/1/excluir")
    rec.click(link("Voltar"))
    assert step(compile_(rec.build()), "clicar_excluir").effect is Effect.IRREVERSIBLE


def test_otp_after_a_write_is_not_authentication() -> None:
    rec = RecordingBuilder()
    rec.navigate("/transferencia")
    rec.fill(textbox("Valor"), "100,00")
    rec.click(button("Continuar"))
    rec.request("POST", "/transferencia")
    rec.navigate("/transferencia/token", cause=NavigationCause.CLICK, trigger=rec.events[-3])
    rec.fill(otp(), None)
    rec.click(button("OK", form=otp().form))
    rec.request("POST", "/transferencia/token")
    result = compile_(rec.build())
    assert step(result, "clicar_continuar").effect is Effect.WRITE
    assert step(result, "clicar_ok").effect is Effect.WRITE


def test_password_change_is_not_authentication() -> None:
    form = FormInfo(method="post", has_password=True, text_fields=0)
    rec = RecordingBuilder()
    rec.navigate("/minha-conta/senha")
    rec.fill(password("Senha atual", id="atual", form=form), None)
    rec.fill(password("Nova senha", id="nova", form=form), None)
    rec.click(button("Salvar", form=form))
    rec.request("POST", "/minha-conta/senha")
    assert step(compile_(rec.build()), "clicar_salvar").effect is Effect.WRITE


def test_login_with_extra_data_field_is_not_authentication() -> None:
    rec = RecordingBuilder()
    rec.navigate("/assinar")
    rec.fill(textbox("Motivo"), "Contrato")
    rec.fill(password(form=FormInfo(method="post", has_password=True, text_fields=2)), None)
    rec.click(button("Gravar", form=FormInfo(method="post", has_password=True, text_fields=2)))
    rec.request("POST", "/assinar")
    assert step(compile_(rec.build()), "clicar_gravar").effect is Effect.WRITE


# -- verificações ------------------------------------------------------------------


def test_assert_and_postcondition_after_irreversible_step() -> None:
    rec = RecordingBuilder()
    form_nav = rec.navigate("/protocolo")
    rec.page_text("Emitir protocolo", navigation=form_nav)
    rec.fill(textbox("Nome"), "Ana")
    click = rec.click(button("Emitir"))
    rec.request("POST", "/protocolo")
    result_nav = rec.navigate("/protocolo", cause=NavigationCause.CLICK, trigger=click)
    rec.page_text("Protocolo emitido", navigation=result_nav, text="Protocolo nº PRT-1")
    result = compile_(rec.build())
    last = steps(result)[-1]
    assert isinstance(last, AssertStep)
    assert last.id == "conferir_protocolo_emitido"
    assert last.condition.op == "text_contains"
    assert result.provenance[last.id] == rec.events[-1].index
    assert [c.model_dump()["text"] for c in result.manifest.postconditions] == ["Protocolo emitido"]
    assert "Protocolo nº PRT-1" in result.observed_texts


def test_no_assert_when_heading_did_not_change() -> None:
    rec = RecordingBuilder()
    nav = rec.navigate("/protocolo")
    rec.page_text("Emitir protocolo", navigation=nav)
    click = rec.click(button("Emitir"))
    rec.request("POST", "/protocolo")
    again = rec.navigate("/protocolo", cause=NavigationCause.CLICK, trigger=click)
    rec.page_text("Emitir protocolo", navigation=again)
    result = compile_(rec.build())
    assert not any(isinstance(s, AssertStep) for s in steps(result))
    assert result.manifest.postconditions == []


# -- ids e serialização -------------------------------------------------------------


def test_step_ids_are_unique_and_valid() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    for _ in range(3):
        rec.click(button("Próxima página de resultados da consulta pública"))
    ids = [s.id for s in steps(compile_(rec.build()))]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(STEP_ID_PATTERN, i) for i in ids)
    assert ids[1:] == [
        "clicar_proxima_pagina_de_resultados",
        "clicar_proxima_pagina_de_resultados_2",
        "clicar_proxima_pagina_de_resultados_3",
    ]


def test_recording_round_trips_through_json() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.click(button("Ok"))
    recording = rec.build()
    again = Recording.model_validate_json(recording.model_dump_json())
    assert again == recording
    assert json.loads(compile_(again).manifest.model_dump_json())["name"] == "robo_teste"


def test_session_id_in_path_is_dropped_and_redacted_urls_are_warned() -> None:
    rec = RecordingBuilder()
    rec.navigate("/consulta;jsessionid=ABC123?token=REMOVIDO")
    result = compile_(rec.build())
    first = steps(result)[0]
    assert isinstance(first, NavigateStep)
    assert first.url == BASE + "/consulta?token=REMOVIDO"
    assert any("valor sensível" in w for w in result.warnings)


def test_unknown_frame_path_is_warned() -> None:
    rec = RecordingBuilder()
    rec.navigate("/quadro")
    el = button("Ok")
    el.frame_path = None
    rec.click(el)
    assert any("iframe" in w for w in compile_(rec.build()).warnings)


def test_actions_before_first_page_are_dropped() -> None:
    rec = RecordingBuilder()
    rec.click(button("Ok"))
    rec.navigate("/")
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate"]
    assert any("antes de abrir" in w for w in result.warnings)


def test_events_out_of_order_are_rejected() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.click(button("Ok"))
    recording = rec.build()
    recording.events.reverse()
    with pytest.raises(CompileError, match="fora de ordem"):
        lower(recording, name="robo_teste", title="T", description="D")


def test_double_click_becomes_one_click() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    ok = button("Ok")
    rec.click(ok)
    rec.click(ok, detail=2)
    assert [s.kind for s in steps(compile_(rec.build()))] == ["navigate", "click"]


def test_unknown_counts_are_kept_after_checked_locators() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.click(button("Ok", id="ok", counts={"role_name": None, "text": None}))
    clicked = steps(compile_(rec.build()))[1]
    assert isinstance(clicked, ClickStep)
    assert [loc.by for loc in clicked.target.locators] == [
        LocatorBy.CSS,
        LocatorBy.ROLE,
        LocatorBy.TEXT,
    ]


def test_locators_that_do_not_fit_are_skipped() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    rec.fill(textbox("{{" * 150, id="campo"), "x")
    fill = steps(compile_(rec.build()))[1]
    assert isinstance(fill, FillStep)
    assert [loc.value for loc in fill.target.locators] == ["#campo"]


# -- senha em campo de texto e literais com cara de segredo ---------------------


def test_password_typed_in_a_text_field_becomes_a_secret() -> None:
    """Senha num campo type=text (mascarado por CSS) que chegou com o valor gravado."""
    rec = RecordingBuilder()
    rec.navigate("/login")
    form = FormInfo(action=f"{BASE}/login", method="post", text_fields=2)
    rec.fill(textbox("Conta", id="conta", form=form), "12345-6")
    rec.fill(textbox("Senha", id="senha", name_attr="senha", form=form), "S3nh@Forte!")
    rec.click_to(button("Entrar", form=form), "/inicio", method="POST")
    result = compile_(rec.build())
    conta, senha = (s for s in steps(result) if isinstance(s, FillStep))
    # "Conta" não é usuário pelo próprio campo e a senha não é type=password: não é
    # login; a conta, numa página com senha, vira uma entrada.
    assert conta.value == TextValue(template="{{ inputs.conta }}")
    assert senha.value == SecretValue(credential="portal", field="password")
    assert step(result, "clicar_entrar").effect is Effect.WRITE
    assert [c.kind for c in result.manifest.credentials] == [CredentialKind.PASSWORD]
    dumped = result.model_dump_json()
    assert "S3nh@Forte!" not in dumped and "12345-6" not in dumped
    assert result.parameter_candidates == []
    assert any("parece de senha" in w for w in result.warnings)


@pytest.mark.parametrize(
    "field",
    [
        textbox("PIN", input_type="tel", id="pin"),
        textbox("Acesso", placeholder="Digite sua senha", id="acesso"),
        textbox("Acesso", name_attr="novaSenha", id="acesso"),
        textbox("Acesso", id="user_password"),
        textbox("Chave", id="chave", autocomplete="current-password"),
        textbox("Confirmar senha nova", id="confirmar"),
    ],
    ids=["pin", "placeholder", "nome", "id", "autocomplete", "confirmar"],
)
def test_text_fields_that_look_like_passwords(field: ElementDescriptor) -> None:
    rec = RecordingBuilder()
    rec.navigate("/acesso")
    rec.fill(field, "1234-segredo")
    result = compile_(rec.build())
    fill = steps(result)[1]
    assert isinstance(fill, FillStep)
    assert fill.value == SecretValue(credential="portal", field="password")
    assert "1234-segredo" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        (textbox("Resenha", id="resenha"), False),
        (textbox("Opinião", id="opiniao"), True),
        (textbox("E-mail para recuperar a senha", input_type="email", id="email"), False),
        (textbox("Usuário ou senha", id="login", autocomplete="username"), False),
    ],
    ids=["resenha", "opiniao", "email", "autocomplete-usuario"],
)
def test_text_fields_that_only_mention_passwords_are_not_passwords(
    field: ElementDescriptor, literal: bool
) -> None:
    rec = RecordingBuilder()
    rec.navigate("/form")
    rec.fill(field, "um texto qualquer")
    result = compile_(rec.build())
    fill = steps(result)[1]
    assert isinstance(fill, FillStep) and isinstance(fill.value, TextValue)
    assert result.manifest.credentials == []
    # Os que ainda têm cara de segredo viram entrada, sem o valor digitado.
    assert (fill.value.template == "um texto qualquer") is literal
    assert ("um texto qualquer" in result.manifest.model_dump_json()) is literal


def test_literal_that_looks_secret_becomes_an_input_without_the_value() -> None:
    rec = RecordingBuilder()
    rec.navigate("/pagamento")
    cartao = textbox("Número do cartão", id="cartao")
    rec.fill(cartao, "4111 1111")
    rec.fill(cartao, "4111 1111 1111 1111")
    rec.fill(textbox("Nome impresso", id="nome"), "ANA SOUZA")
    rec.fill(textbox("CVV", id="cvv"), "987")
    result = compile_(rec.build())
    cartao_step, nome, cvv = (s for s in steps(result) if isinstance(s, FillStep))
    assert isinstance(cartao_step.value, TextValue)
    assert cartao_step.value.template == "{{ inputs.numero_do_cartao }}"
    assert isinstance(cvv.value, TextValue) and cvv.value.template == "{{ inputs.cvv }}"
    assert isinstance(nome.value, TextValue) and nome.value.template == "ANA SOUZA"
    assert [(f.name, f.type, f.required) for f in result.manifest.inputs] == [
        ("numero_do_cartao", FieldType.STRING, True),
        ("cvv", FieldType.STRING, True),
    ]
    dumped = result.model_dump_json()
    assert "4111" not in dumped and "987" not in dumped
    assert [c.step_id for c in result.parameter_candidates] == [nome.id]
    assert any("Número do cartão" in w and "entrada" in w for w in result.warnings)


def test_a_field_that_looks_secret_beside_a_password_is_an_input() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Token de acesso", id="token"), "abc-123")
    rec.fill(password(), None)
    # O campo não é usuário pelo próprio campo: nunca vira o usuário do cofre.
    rec.click(button("Entrar", id="entrar"))
    result = compile_(rec.build())
    token = steps(result)[1]
    assert isinstance(token, FillStep)
    assert token.value == TextValue(template="{{ inputs.token_de_acesso }}")
    assert [(f.name, f.required) for f in result.manifest.inputs] == [("token_de_acesso", True)]
    assert "abc-123" not in result.model_dump_json()


# -- usuário do login: a mesma regra no gravador e no compilador ------------------


def _username_scenarios() -> dict[str, Recording]:
    usuario = textbox("Usuário", id="usuario", name_attr="usuario")
    scenarios: dict[str, Recording] = {}

    def sent(rec: RecordingBuilder) -> Recording:
        """A pessoa termina clicando em "Entrar": o envio que a regra do login confere."""
        rec.click(button("Entrar", id="entrar"))
        return rec.build()

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.fill(password(), None)
    scenarios["direto"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana")
    rec.fill(usuario, "ana.souza")
    rec.key("Tab", usuario)
    rec.click(password())  # clique que só foca a senha
    rec.fill(password(), None)
    scenarios["editado_com_tab"] = sent(rec)

    # Login em duas etapas na mesma página (SPA): a senha só aparece depois do
    # "Continuar" ou do Enter no campo do usuário.
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.click(button("Continuar"))
    rec.fill(password(), None)
    scenarios["clique_entre"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana")
    rec.key("Enter", usuario)
    rec.fill(usuario, "ana.souza")
    rec.key("Enter", usuario)
    rec.fill(password(), None)
    scenarios["enter_entre"] = sent(rec)

    # Na segunda etapa, só vale o primeiro campo preenchido na página.
    rec = RecordingBuilder()
    rec.navigate("/transferencia")
    rec.fill(textbox("Chave Pix", id="chave"), "ana@exemplo.com.br")
    rec.fill(textbox("Descrição", id="descricao"), "aluguel")
    rec.click(button("Continuar"))
    rec.fill(password(), None)
    scenarios["clique_depois_de_outro_campo"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.click(button("Enviar código"))
    rec.fill(password(), None)
    scenarios["verbo_entre"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor"), "1250,00")
    rec.fill(textbox("Senha de transação", id="senha_tx"), "4829")
    scenarios["senha_de_transacao"] = rec.build()

    # O Enter enviou o login; o "Excluir" bem depois, na mesma página, não é o envio.
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.fill(password(), None)
    rec.key("Enter", password())
    rec.click(button("Excluir rascunho"))
    scenarios["enter_envia_o_login"] = rec.build()

    rec = RecordingBuilder()
    rec.navigate("/busca")
    rec.fill(textbox("Busca", id="q"), "notas fiscais")
    rec.navigate("/login")
    rec.fill(password(), None)
    scenarios["outra_pagina"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(password(), None)
    rec.fill(usuario, "ana.souza")
    scenarios["senha_antes"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(element("textarea", role="textbox", name="Obs", label="Obs", id="obs"), "obs")
    rec.fill(password(), None)
    scenarios["textarea"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.select(select_box("Perfil", id="perfil"), "Contador")
    rec.fill(password(), None)
    scenarios["lista_entre"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(usuario, "ana.souza")
    rec.fill(textbox("Senha", id="senha_texto"), "S3nh@Forte!")
    scenarios["senha_em_campo_de_texto"] = sent(rec)

    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(otp(), None)
    rec.fill(password(), None)
    scenarios["codigo_antes"] = sent(rec)
    return scenarios


@pytest.mark.parametrize("name", list(_username_scenarios()))
def test_recorder_hides_every_value_the_compiler_does_not_keep_literal(name: str) -> None:
    recording = _username_scenarios()[name]
    result = compile_(recording)
    by_index = {e.index: e for e in recording.events}
    found = secret_page_inputs(recording.events)
    hidden_users = {e.index for e in found.usernames}
    hidden = hidden_users | {e.index for e in found.others}
    compiled: set[tuple[object, ...]] = set()
    for fill in steps(result):
        if not isinstance(fill, FillStep):
            continue
        event = by_index[result.provenance[fill.id]]
        assert isinstance(event, InputEvent)
        if event.element.sensitive:
            continue
        if fill.value == SecretValue(credential="portal", field="username"):
            # Só vira o usuário do cofre o que o gravador tira da gravação como usuário.
            assert event.index in hidden_users
            compiled.add(field_key(event.page_id, event.element))
        elif isinstance(fill.value, TextValue) and not fill.value.template.startswith("{{"):
            # O que fica literal no robô o gravador deixa na gravação.
            assert event.index not in hidden
        else:
            # Uma entrada, ou a senha do cofre (senha em campo de texto, com o valor).
            assert isinstance(fill.value, TextValue) or fill.value.field == "password"
    # Usuário do cofre só num login que passa na regra: o campo de usuário, a senha
    # de verdade e o botão de login, nessa forma (ou em duas etapas, pelo clique).
    expected = {"direto", "editado_com_tab", "clique_entre", "senha_antes"}
    assert bool(compiled) is (name in expected)
    if name == "editado_com_tab":
        assert [e.value for e in found.usernames] == ["ana", "ana.souza"]
    # Numa página com senha, os outros campos saem da gravação sem a marca de usuário.
    others = {
        "clique_depois_de_outro_campo": ["chave", "descricao"],
        "senha_de_transacao": ["valor"],
        "textarea": ["obs"],
    }
    assert [e.element.id for e in found.others] == others.get(name, [])
    users = {
        "outra_pagina": [],
        "senha_de_transacao": [],
        "textarea": [],
        "clique_depois_de_outro_campo": [],
        "codigo_antes": [],
    }
    assert bool(found.usernames) is bool(users.get(name, True))


# -- efeito de campos editados mais de uma vez ou deixados com Tab --------------


def test_autosave_after_editing_a_field_twice_is_write() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    observacao = textbox("Observação", id="obs")
    rec.fill(observacao, "a")
    rec.fill(observacao, "abc")
    rec.request("POST", "/api/salvar-rascunho", "fetch")
    rec.click(button("Voltar"))
    result = compile_(rec.build())
    assert step(result, "digitar_observacao").effect is Effect.WRITE
    assert step(result, "clicar_voltar").effect is Effect.READ
    assert result.manifest.effect() is Effect.WRITE


def test_autosave_after_leaving_a_field_with_tab_is_write() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    observacao = textbox("Observação", id="obs")
    rec.fill(observacao, "abc")
    rec.key("Tab", observacao)
    rec.request("POST", "/api/salvar-rascunho", "fetch")
    rec.click(button("Voltar"))
    result = compile_(rec.build())
    assert step(result, "digitar_observacao").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE
    assert policy.first_write_step(result.manifest) == "digitar_observacao"


def test_autosave_after_changing_a_list_or_box_twice_is_write() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    uf = select_box("UF", id="uf")
    rec.select(uf, "PR")
    rec.select(uf, "SP")
    rec.request("POST", "/api/uf", "xhr")
    aviso = checkbox("Receber avisos", id="aviso")
    rec.check(aviso, True)
    rec.check(aviso, False)
    rec.key("Tab", aviso)
    rec.request("PUT", "/api/aviso", "fetch")
    result = compile_(rec.build())
    assert step(result, "escolher_uf").effect is Effect.WRITE
    aviso_step = step(result, "marcar_receber_avisos")
    assert isinstance(aviso_step, CheckStep) and not aviso_step.checked
    assert aviso_step.effect is Effect.WRITE


def test_tab_requests_go_only_to_the_field_being_left() -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.fill(textbox("Nome", id="nome"), "Ana")
    rec.key("Tab", textbox("Cidade", id="cidade"))  # Tab num campo que não foi editado
    rec.request("POST", "/api/rascunho", "fetch")
    buscar = button("Buscar")
    rec.click(buscar)
    rec.key("Tab", buscar)  # Tab depois de um clique: nunca vai para o clique
    rec.request("POST", "/api/rascunho", "fetch")
    result = compile_(rec.build())
    assert step(result, "digitar_nome").effect is Effect.READ
    assert step(result, "clicar_buscar").effect is Effect.READ


def test_write_after_the_second_click_of_a_double_click_counts() -> None:
    rec = RecordingBuilder()
    rec.navigate("/")
    ok = button("Ok")
    rec.click(ok)
    rec.click(ok, detail=2)
    rec.request("POST", "/api/ok", "fetch")
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate", "click"]
    assert step(result, "clicar_ok").effect is Effect.WRITE


# -- confirmação de transação nunca é login ---------------------------------------

PAY_FORM = FormInfo(action=f"{BASE}/pagar", method="post", has_password=True, text_fields=1)


def _confirmation(secret: ElementDescriptor, button_label: str) -> CompileResult:
    rec = RecordingBuilder()
    rec.navigate("/boletos")
    rec.page_text("Pagamento de boleto")
    rec.fill(secret, "482913")
    click = rec.click(button(button_label, id="ok", form=PAY_FORM))
    rec.request("POST", "/pagar")
    rec.navigate("/comprovante", cause=NavigationCause.CLICK, trigger=click)
    rec.page_text("Comprovante")
    return compile_(rec.build())


@pytest.mark.parametrize(
    "button_label", ["Confirmar pagamento", "Autorizar", "Assinar", "Emitir", "Transferir"]
)
@pytest.mark.parametrize(
    "secret",
    [
        textbox("Senha de transação", id="segredo", form=PAY_FORM),
        textbox("PIN", input_type="tel", id="segredo", form=PAY_FORM),
        # Como o script marca um campo de texto mascarado por CSS.
        textbox(
            "Assinatura eletrônica", id="segredo", form=PAY_FORM, is_password=True, sensitive=True
        ),
        password("Senha", id="segredo", form=PAY_FORM),
    ],
    ids=["senha-de-transacao", "pin", "assinatura-mascarada", "senha"],
)
def test_irreversible_confirmation_is_never_authentication(
    secret: ElementDescriptor, button_label: str
) -> None:
    result = _confirmation(secret, button_label)
    click = next(s for s in steps(result) if isinstance(s, ClickStep))
    assert click.effect is Effect.IRREVERSIBLE
    assert result.manifest.effect() is Effect.IRREVERSIBLE
    assert "482913" not in result.model_dump_json()


def test_username_and_password_before_an_irreversible_button_is_not_login() -> None:
    rec = RecordingBuilder()
    rec.navigate("/assinar")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(), None)
    rec.click(button("Assinar contrato", form=LOGIN_FORM))
    rec.request("POST", "/assinar")
    result = compile_(rec.build())
    assert step(result, "clicar_assinar_contrato").effect is Effect.IRREVERSIBLE
    assert "ana.souza" not in result.model_dump_json()


def test_field_before_a_transaction_pin_is_not_the_username() -> None:
    rec = RecordingBuilder()
    rec.navigate("/pix")
    rec.fill(textbox("Valor", id="valor", form=PAY_FORM), "1250,00")
    rec.fill(textbox("PIN", input_type="tel", id="pin", form=PAY_FORM), "4829")
    rec.click(button("OK", id="ok", form=PAY_FORM))
    rec.request("POST", "/pix")
    recording = rec.build()
    result = compile_(recording)
    valor = step(result, "digitar_valor")
    assert isinstance(valor, FillStep)
    # Nem o usuário do cofre (que seria digitado no valor) nem o valor gravado: uma entrada.
    assert valor.value == TextValue(template="{{ inputs.valor }}")
    assert [(f.name, f.required) for f in result.manifest.inputs] == [("valor", True)]
    pin = step(result, "digitar_pin")
    assert isinstance(pin, FillStep) and isinstance(pin.value, SecretValue)
    # Senha de transação não é login: o clique que grava continua gravando.
    assert step(result, "clicar_ok").effect is Effect.WRITE
    dumped = result.model_dump_json()
    assert "4829" not in dumped and "1250,00" not in dumped
    # O gravador esconde o mesmo campo, sem chamá-lo de usuário.
    found = secret_page_inputs(recording.events)
    assert found.usernames == []
    assert [e.element.id for e in found.others] == ["valor"]


def test_field_before_a_password_and_an_irreversible_button_is_not_the_username() -> None:
    rec = RecordingBuilder()
    rec.navigate("/transferencia")
    rec.fill(textbox("Valor", id="valor", form=PAY_FORM), "1250,00")
    senha = password("Senha", id="senha", form=PAY_FORM)
    rec.fill(senha, None)
    rec.key("Tab", senha)
    rec.click(button("Transferir", id="ok", form=PAY_FORM))
    rec.request("POST", "/transferencia")
    recording = rec.build()
    result = compile_(recording)
    valor = step(result, "digitar_valor")
    assert isinstance(valor, FillStep)
    assert valor.value == TextValue(template="{{ inputs.valor }}")
    assert step(result, "clicar_transferir").effect is Effect.IRREVERSIBLE
    found = secret_page_inputs(recording.events)
    assert found.usernames == []
    assert [e.element.id for e in found.others] == ["valor"]


def _login(
    button_label: str = "Entrar", *, username: bool = True, wrote_before: bool = False
) -> CompileResult:
    rec = RecordingBuilder()
    if wrote_before:
        rec.navigate("/cadastro")
        rec.click(button("Gravar"))
        rec.request("POST", "/cadastro")
    rec.navigate("/login")
    if username:
        rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(), None)
    click = rec.click(button(button_label, id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.CLICK, trigger=click)
    return compile_(rec.build())


@pytest.mark.parametrize("label", ["Entrar", "Acessar", "Login", "Continuar"])
def test_login_with_username_password_and_a_login_button_is_read(label: str) -> None:
    result = _login(label)
    click = steps(result)[-1]
    assert isinstance(click, ClickStep) and click.effect is Effect.READ


def test_login_needs_username_password_and_a_login_button() -> None:
    # Sem usuário, com um botão que não é de login ou depois de gravar: não é login.
    assert step(_login(username=False), "clicar_entrar").effect is Effect.WRITE
    assert step(_login("Salvar"), "clicar_salvar").effect is Effect.WRITE
    # "OK" não está no vocabulário curto de login: o POST grava.
    assert step(_login("OK"), "clicar_ok").effect is Effect.WRITE
    assert step(_login(wrote_before=True), "clicar_entrar").effect is Effect.WRITE


@pytest.mark.parametrize("implicit_click", [True, False], ids=["clique_implicito", "so_o_enter"])
def test_enter_in_the_password_field_is_a_login_only_through_the_button(
    implicit_click: bool,
) -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    senha = password(id="senha")
    rec.fill(senha, None)
    enter = rec.key("Enter", senha)
    trigger = enter
    if implicit_click:
        # O navegador clica no botão de envio do mesmo formulário.
        trigger = rec.click(button("Entrar", id="entrar", form=LOGIN_FORM), detail=0)
    rec.request("POST", "/login")
    rec.navigate("/inicio", cause=NavigationCause.KEY, trigger=trigger)
    result = compile_(rec.build())
    if implicit_click:
        assert step(result, "clicar_entrar").effect is Effect.READ
    else:
        # Sem o clique do formulário, o Enter nunca é login: o POST grava.
        assert step(result, "pressionar_enter").effect is Effect.WRITE


# -- requisição feita enquanto a pessoa edita o campo (salvamento com atraso) -----


def test_write_while_the_field_is_being_edited_counts_for_the_field() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(), None)
    click = rec.click(button("Entrar", form=LOGIN_FORM))
    rec.request("POST", "/login")
    rec.navigate("/cadastro", cause=NavigationCause.CLICK, trigger=click)
    started = rec.page_text("Cadastro")  # a pessoa começa a digitar depois disto
    rec.request("POST", "/api/rascunho", "fetch")  # salvamento com atraso, antes do valor
    observacao = textbox("Observação", id="obs")
    rec.fill(observacao, "abc", editing_since=started)
    rec.key("Tab", observacao)
    rec.click(button("Voltar"))
    result = compile_(rec.build())
    assert step(result, "clicar_entrar").effect is Effect.READ
    assert step(result, "digitar_observacao").effect is Effect.WRITE
    assert result.manifest.effect() is Effect.WRITE


def test_write_on_the_first_page_while_editing_counts_for_the_field() -> None:
    rec = RecordingBuilder()
    started = rec.navigate("/cadastro")
    rec.request("POST", "/api/rascunho", "fetch")
    rec.fill(textbox("Observação", id="obs"), "abc", editing_since=started)
    result = compile_(rec.build())
    assert step(result, "digitar_observacao").effect is Effect.WRITE


def test_requests_before_the_edit_or_the_page_do_not_count_for_the_field() -> None:
    rec = RecordingBuilder()
    first = rec.navigate("/inicio")
    click = rec.click(button("Novo cadastro"))
    rec.request("POST", "/api/cadastro")  # do clique, antes da página nova
    rec.navigate("/cadastro", cause=NavigationCause.CLICK, trigger=click)
    rec.request("POST", "/api/visita", "fetch")  # bem antes de a pessoa começar a digitar
    rec.wait(1000)
    started = rec.page_text("Cadastro")
    rec.fill(textbox("Nome", id="nome"), "Ana", editing_since=started)
    # Começo informado antes da ação anterior: só vale o que veio depois dela.
    rec.fill(textbox("Cidade", id="cidade"), "Recife", editing_since=first)
    result = compile_(rec.build())
    assert step(result, "clicar_novo_cadastro").effect is Effect.WRITE
    assert step(result, "digitar_nome").effect is Effect.READ
    assert step(result, "digitar_cidade").effect is Effect.READ


def test_requests_of_the_navigation_to_the_page_do_not_count_for_the_field() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.fill(textbox("Usuário", id="usuario", form=LOGIN_FORM), "ana.souza")
    rec.fill(password(), None)
    click = rec.click(button("Entrar", form=LOGIN_FORM))
    started = rec.request("POST", "/login")
    rec.navigate("/codigo", cause=NavigationCause.CLICK, trigger=click)
    # O começo da edição chegou antes da navegação: o POST do login não é do código.
    rec.fill(otp(), None, editing_since=started)
    rec.click(button("Confirmar", form=otp().form))
    result = compile_(rec.build())
    assert step(result, "digitar_codigo_de_verificacao").effect is Effect.READ
    assert step(result, "clicar_confirmar").effect is Effect.READ
    assert result.manifest.effect() is Effect.READ


# -- verificação só com o que a própria ação mostrou -------------------------------


@pytest.mark.parametrize("variant", ["single", "double", "tab"])
def test_assert_after_a_field_ignores_headings_of_the_next_action(variant: str) -> None:
    rec = RecordingBuilder()
    rec.navigate("/cadastro")
    rec.page_text("Cadastro")
    observacao = textbox("Observação", id="obs")
    if variant == "double":
        rec.fill(observacao, "a")
    rec.fill(observacao, "abc")
    if variant == "tab":
        rec.key("Tab", observacao)
    rec.request("POST", "/api/rascunho", "fetch")
    click = rec.click(button("Salvar", id="salvar"))
    rec.request("POST", "/api/salvar", "fetch")
    rec.page_text("Cadastro", "Registro salvo", after=click)
    result = compile_(rec.build())
    assert [(s.id, s.kind) for s in steps(result)] == [
        ("abrir_cadastro", "navigate"),
        ("digitar_observacao", "fill"),
        ("clicar_salvar", "click"),
        ("conferir_registro_salvo", "assert"),
    ]
    assert step(result, "digitar_observacao").effect is Effect.WRITE


def test_assert_after_a_login_field_does_not_jump_over_the_password() -> None:
    rec = RecordingBuilder()
    rec.navigate("/login")
    rec.page_text("Portal")
    cpf = textbox("CPF", id="cpf", form=LOGIN_FORM)
    rec.fill(cpf, "12345678900")
    rec.key("Tab", cpf)
    rec.request("POST", "/api/cpf/verificar", "xhr")
    rec.fill(password(id="senha"), None)
    click = rec.click(button("Entrar", id="entrar", form=LOGIN_FORM))
    rec.request("POST", "/api/login", "xhr")
    rec.page_text("Portal", "Início", after=click)
    result = compile_(rec.build())
    assert [s.kind for s in steps(result)] == ["navigate", "fill", "fill", "click", "assert"]
    assert "12345678900" not in result.model_dump_json()


def test_assert_after_a_navigation_ignores_headings_of_the_next_action() -> None:
    rec = RecordingBuilder()
    rec.navigate("/protocolo")
    rec.page_text("Emitir protocolo")
    click = rec.click(button("Emitir"))
    rec.request("POST", "/protocolo")
    result_nav = rec.navigate("/resultado", cause=NavigationCause.CLICK, trigger=click)
    rec.page_text("Emitir protocolo", navigation=result_nav)
    detalhar = rec.click(button("Detalhar"))
    rec.page_text(
        "Emitir protocolo", "Detalhes do protocolo", navigation=result_nav, after=detalhar
    )
    result = compile_(rec.build())
    assert not any(isinstance(s, AssertStep) for s in steps(result))
