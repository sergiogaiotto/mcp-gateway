"""Partes do gravador que não precisam de navegador."""

from datetime import UTC, datetime
from importlib import resources

import pytest

from rapa.recorder import (
    REDACTED,
    ClickEvent,
    ElementDescriptor,
    Heading,
    InputEvent,
    NavigationCause,
    NavigationEvent,
    PageTextEvent,
    Recorder,
    RecorderError,
    Recording,
)
from rapa.recorder.recorder import redact_url
from rapa.recorder.sensitivity import is_password_like_text, password_by_name, username_by_signal


def test_redact_url_masks_sensitive_params_and_drops_fragment() -> None:
    url = "https://portal.gov.br/entrar?usuario=ana&senha=segredo&token=abc&pagina=2#topo"
    redacted = redact_url(url, {"usuario"})
    assert "segredo" not in redacted and "abc" not in redacted and "ana" not in redacted
    assert redacted.count(REDACTED) == 3
    assert "pagina=2" in redacted
    assert "#" not in redacted
    assert redact_url("https://portal.gov.br/notas?pagina=2", set()) == (
        "https://portal.gov.br/notas?pagina=2"
    )


def test_messages_without_the_recorder_signature_are_dropped() -> None:
    recorder = Recorder()
    recorder._on_binding({}, {"kind": "click", "nonce": "falso"})
    recorder._on_binding({}, ["não", "é", "objeto"])
    assert recorder._dropped == 2
    assert recorder._events == []


async def test_stop_before_start_fails() -> None:
    with pytest.raises(RecorderError, match="não foi iniciado"):
        await Recorder().stop()


def test_capture_script_is_packaged_and_never_sends_sensitive_values() -> None:
    script = resources.files("rapa.recorder").joinpath("capture.js").read_text(encoding="utf-8")
    assert "__RAPA_CONFIG__" in script
    # O valor só sai quando o campo não é sensível.
    assert "value: sensitive ? null" in script
    assert "isTrusted" in script


class _FakePage:
    def on(self, event: str, handler: object) -> None:
        pass


class _FakeFrame:
    parent_frame = None

    def __init__(self, page: _FakePage, url: str) -> None:
        self.page = page
        self.url = url


def _send_input(recorder: Recorder, frame: _FakeFrame, value: str, **element: object) -> None:
    """Uma mensagem do script como ele mandaria para um campo que não marcou como sensível."""
    sensitive = bool(element.get("sensitive"))
    recorder._on_binding(
        {"page": frame.page, "frame": frame},
        {
            "nonce": recorder._nonce,
            "kind": "input",
            "element": {"tag": "input", "input_type": "text", "frame_path": [], **element},
            "value": None if sensitive else value,
            "secret": value if sensitive else None,
            "typed": None if sensitive else value,
            "script_modified": False,
        },
    )


def _send_click(recorder: Recorder, frame: _FakeFrame, name: str, **element: object) -> None:
    """Uma mensagem do script para o clique num botão."""
    recorder._on_binding(
        {"page": frame.page, "frame": frame},
        {
            "nonce": recorder._nonce,
            "kind": "click",
            "element": {
                "tag": "button",
                "role": "button",
                "name": name,
                "frame_path": [],
                **element,
            },
            "detail": 1,
        },
    )


def test_text_field_that_looks_like_a_password_is_hidden_by_the_recorder_too() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    frame = _FakeFrame(_FakePage(), "https://banco.exemplo.com.br/login")
    _send_input(recorder, frame, "S3nh@Forte!", label="Senha", name="Senha", css="#s", id="s")
    _send_input(recorder, frame, "PIN-0042", placeholder="Seu PIN", css="#p", name_attr="cod")
    first, second = recorder._events
    for event in (first, second):
        assert isinstance(event, InputEvent)
        assert event.value is None and event.typed is None
        assert event.element.is_password and event.element.sensitive
    assert {"S3nh@Forte!", "PIN-0042"} <= recorder._secret_values
    assert "cod" in recorder._pages[frame.page].sensitive_names
    assert recorder._dropped == 0


def test_username_typed_right_before_the_password_leaves_the_recording() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    page = _FakePage()
    frame = _FakeFrame(page, "https://portal.exemplo.com.br/login")
    recorder._register_page(page)
    recorder._append(NavigationEvent, page_id=0, url=frame.url, cause=NavigationCause.TYPED)
    user = "ana souza@exemplo.com.br"
    _send_input(
        recorder, frame, user, label="Usuário", name="Usuário", css="#u", name_attr="usuario"
    )
    _send_input(
        recorder,
        frame,
        "S3nh@Forte!",
        input_type="password",
        css="#p",
        is_password=True,
        sensitive=True,
    )
    # O envio do login: só com ele (botão de login) o campo vira o usuário.
    _send_click(recorder, frame, "Entrar", css="#entrar")
    after = "https://portal.exemplo.com.br/inicio?usuario=ana+souza%40exemplo.com.br&q=ana%20souza%40exemplo.com.br&pagina=2"
    recorder._append(NavigationEvent, page_id=0, url=after, cause=NavigationCause.CLICK)
    recorder._append(PageTextEvent, page_id=0, url=after, text=f"Início\nOlá, {user}")

    recorder._hide_secret_page_fields()
    events = [recorder._scrub(event) for event in recorder._events]
    dumped = Recording(started_at=datetime.now(UTC), events=events).model_dump_json()
    for leaked in (user, "ana+souza", "ana%20souza", "souza%40exemplo"):
        assert leaked not in dumped
    usuario = events[1]
    assert isinstance(usuario, InputEvent) and usuario.value is None and usuario.typed is None
    assert usuario.element.is_username and usuario.element.sensitive
    assert isinstance(events[3], ClickEvent)
    navigation = events[4]
    assert isinstance(navigation, NavigationEvent)
    assert f"usuario={REDACTED}" in navigation.url and "pagina=2" in navigation.url
    page_text = events[5]
    assert isinstance(page_text, PageTextEvent) and page_text.text == f"Início\nOlá, {REDACTED}"


def test_observed_texts_split_lines_and_headings() -> None:
    recording = Recording(
        started_at=datetime.now(UTC),
        events=[
            PageTextEvent(
                index=0,
                t_ms=0,
                page_id=0,
                url="https://portal.gov.br/",
                title="Início · Portal",
                headings=[Heading(level=1, text="Início")],
                text="Início\n  Olá,   Ana.  \n\nNotas fiscais",
                frame_texts=["Consulta confirmada"],
            )
        ],
    )
    assert recording.observed_texts() == {
        "Início",
        "Início · Portal",
        "Olá, Ana.",
        "Notas fiscais",
        "Consulta confirmada",
    }


def test_secret_values_are_removed_in_any_letter_case() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    recorder._register_page(_FakePage())
    recorder._remember_secret("ana.souza")
    recorder._remember_secret("joão.weiß")
    url = "https://portal.exemplo.com.br/inicio"
    # O cabeçalho com text-transform: uppercase chega pelo innerText em maiúsculas.
    text = "ANA.SOUZA\nOlá, Ana.Souza\nJOÃO.WEISS e joão.weiss\nana.souzas"
    recorder._append(PageTextEvent, page_id=0, url=url, text=text, title="Ana.SOUZA")
    event = recorder._scrub(recorder._events[0])
    assert isinstance(event, PageTextEvent)
    assert event.title == REDACTED
    assert event.text == f"{REDACTED}\nOlá, {REDACTED}\n{REDACTED} e {REDACTED}\n{REDACTED}s"


def test_recorder_never_turns_a_username_of_the_script_into_a_password() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    frame = _FakeFrame(_FakePage(), "https://portal.exemplo.com.br/login")
    # O nome do campo fala em senha, mas o script já o marcou como usuário.
    _send_input(
        recorder,
        frame,
        "12345678900",
        label="CPF",
        name="Entre com seu CPF e senha",
        name_attr="cpf_senha",
        css="#cpf",
        is_username=True,
        sensitive=True,
    )
    (event,) = recorder._events
    assert isinstance(event, InputEvent)
    assert event.element.is_username and not event.element.is_password
    assert event.value is None and "12345678900" in recorder._secret_values


def test_password_rule_ignores_the_accessible_name_like_the_page_script() -> None:
    def field(**kw: object) -> ElementDescriptor:
        return ElementDescriptor.model_validate(
            {"tag": "input", "input_type": "text", "css": "#c", **kw}
        )

    # aria-labelledby (nome acessível) fala em senha e o <label> é "CPF": o script
    # olha o rótulo, e o gravador e o compilador também.
    assert not is_password_like_text(field(label="CPF", name="Entre com seu CPF e senha"))
    assert is_password_like_text(field(label="Senha", name="CPF"))
    assert is_password_like_text(field(placeholder="Sua senha", name="Acesso"))


def test_a_field_beside_a_password_leaves_the_recording_without_becoming_the_username() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    page = _FakePage()
    frame = _FakeFrame(page, "https://banco.exemplo.com.br/pix")
    recorder._register_page(page)
    recorder._append(NavigationEvent, page_id=0, url=frame.url, cause=NavigationCause.TYPED)
    _send_input(
        recorder, frame, "1250,00", label="Valor", name="Valor", css="#v", id="v", name_attr="v"
    )
    _send_input(
        recorder, frame, "s3nh4", input_type="password", css="#s", is_password=True, sensitive=True
    )
    # "Confirmar" depois de um valor e uma senha não é login, mas o valor estava numa
    # página com senha: sai da gravação, sem a marca de usuário.
    _send_click(recorder, frame, "Confirmar", css="#ok")
    recorder._hide_secret_page_fields()
    valor = recorder._events[1]
    assert isinstance(valor, InputEvent)
    assert valor.value is None and valor.typed is None
    assert valor.element.sensitive and not valor.element.is_username
    assert "1250,00" in recorder._secret_values
    assert "v" in recorder._pages[page].sensitive_names


def test_scrub_ignores_letter_case_only_in_texts_from_the_inner_text() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    page = _FakePage()
    frame = _FakeFrame(page, "https://erp.exemplo.com.br/inicio")
    recorder._register_page(page)
    recorder._remember_secret("financeiro")
    url = "https://erp.exemplo.com.br/inicio"
    recorder._append(
        PageTextEvent,
        page_id=0,
        url=url,
        title="Painel de FINANCEIRO",
        headings=[Heading(level=1, text="Olá, Financeiro")],
        text="Usuário: financeiro\nMenu Financeiro",
        frame_texts=["FINANCEIRO"],
    )
    _send_input(recorder, frame, "Financeiro Corporativo", label="Departamento", css="#d")
    _send_input(recorder, frame, "equipe financeiro", label="Equipe", css="#e")
    _send_click(recorder, frame, "Financeiro", css="#menu", text="Financeiro")
    text, departamento, equipe, menu = (recorder._scrub(e) for e in recorder._events)
    assert isinstance(text, PageTextEvent)
    assert text.title == f"Painel de {REDACTED}"
    assert text.headings[0].text == f"Olá, {REDACTED}"
    assert text.text == f"Usuário: {REDACTED}\nMenu {REDACTED}"
    assert text.frame_texts == [REDACTED]
    # Valores digitados e nomes de elementos: só o valor exato.
    assert isinstance(departamento, InputEvent) and departamento.value == "Financeiro Corporativo"
    assert isinstance(equipe, InputEvent) and equipe.value == f"equipe {REDACTED}"
    assert isinstance(menu, ClickEvent) and menu.element.name == "Financeiro"
    # O texto do elemento vem do innerText (passou pelo text-transform): qualquer caixa.
    assert menu.element.text == REDACTED


def test_scrub_ignores_letter_case_in_the_text_of_the_element_and_of_its_row() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    page = _FakePage()
    frame = _FakeFrame(page, "https://portal.exemplo.com.br/inicio")
    recorder._register_page(page)
    recorder._remember_secret("ana.souza")
    # innerText com text-transform: uppercase; a chave da linha vem do texto do DOM.
    row = {
        "text": "ANA.SOUZA NF 10 Abrir",
        "cells": ["ANA.SOUZA", "NF 10", "Abrir"],
        "key": "ana.souza",
    }
    _send_click(recorder, frame, "Abrir", css="#abrir", text="Abrir", row=row)
    _send_click(recorder, frame, "ana.souza", css="#perfil", text="ANA.SOUZA", role="link")
    _send_click(recorder, frame, "Ana.Souza", css="#outro", text="Ana.Souza")
    abrir, perfil, outro = (recorder._scrub(e) for e in recorder._events)
    assert isinstance(abrir, ClickEvent) and abrir.element.row is not None
    assert abrir.element.row.text == f"{REDACTED} NF 10 Abrir"
    assert abrir.element.row.cells == [REDACTED, "NF 10", "Abrir"]
    assert abrir.element.row.key == REDACTED
    assert isinstance(perfil, ClickEvent)
    assert perfil.element.text == REDACTED and perfil.element.name == REDACTED
    # O nome (do texto do DOM, sem CSS) só perde o valor exato.
    assert isinstance(outro, ClickEvent)
    assert outro.element.name == "Ana.Souza" and outro.element.text == REDACTED


def test_scrub_cuts_a_text_that_grew_past_its_limit() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    page = _FakePage()
    frame = _FakeFrame(page, "https://portal.exemplo.com.br/inicio")
    recorder._register_page(page)
    recorder._remember_secret("ana")
    recorder._remember_secret("anas")
    # O script já cortou o texto da linha no limite (500); "anas" vira "REMOVIDO".
    text = ("x " * 248) + "anas"
    _send_click(recorder, frame, "Abrir", css="#abrir", row={"text": text, "cells": []})
    (event,) = (recorder._scrub(e) for e in recorder._events)
    assert isinstance(event, ClickEvent) and event.element.row is not None
    assert len(event.element.row.text) == 500 and "anas" not in event.element.row.text


def test_field_named_like_a_password_is_hidden_without_becoming_a_password() -> None:
    recorder = Recorder(probe_locators=False, capture_text=False)
    frame = _FakeFrame(_FakePage(), "https://banco.exemplo.com.br/entrar")
    # aria-labelledby: "Digite a senha de 6 dígitos"; <label>: "Código pessoal".
    _send_input(
        recorder,
        frame,
        "918273",
        label="Código pessoal",
        name="Digite a senha de 6 dígitos",
        css="#k",
        name_attr="cod",
    )
    (event,) = recorder._events
    assert isinstance(event, InputEvent)
    assert event.value is None and event.typed is None
    assert event.element.sensitive and not event.element.is_password
    assert "918273" in recorder._secret_values
    assert "cod" in recorder._pages[frame.page].sensitive_names
    assert password_by_name(event.element)


def test_an_aria_label_of_a_password_hides_the_field_even_with_another_name() -> None:
    def field(**kw: object) -> ElementDescriptor:
        return ElementDescriptor.model_validate(
            {"tag": "input", "input_type": "text", "css": "#k", **kw}
        )

    # aria-labelledby ("Código pessoal") vira o nome acessível; o aria-label
    # ("Senha de 6 dígitos") chega à parte, no sinal do script.
    named = field(label="Código pessoal", name="Código pessoal", aria_mentions_password=True)
    assert password_by_name(named) and not is_password_like_text(named)
    assert not password_by_name(field(label="Código pessoal", name="Código pessoal"))
    # O sinal nunca desfaz o usuário pelo próprio campo.
    cpf = field(label="CPF", name="CPF", aria_mentions_password=True)
    assert username_by_signal(cpf) and not password_by_name(cpf)
    recorder = Recorder(probe_locators=False, capture_text=False)
    frame = _FakeFrame(_FakePage(), "https://banco.exemplo.com.br/entrar")
    _send_input(
        recorder,
        frame,
        "918273",
        label="Código pessoal",
        name="Código pessoal",
        css="#k",
        aria_mentions_password=True,
    )
    (event,) = recorder._events
    assert isinstance(event, InputEvent)
    assert event.value is None and event.element.sensitive and not event.element.is_password
