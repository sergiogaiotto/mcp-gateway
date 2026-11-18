"""Partes do gravador que não precisam de navegador."""

from datetime import UTC, datetime
from importlib import resources

import pytest

from rapa.recorder import (
    REDACTED,
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
    after = "https://portal.exemplo.com.br/inicio?usuario=ana+souza%40exemplo.com.br&q=ana%20souza%40exemplo.com.br&pagina=2"
    recorder._append(NavigationEvent, page_id=0, url=after, cause=NavigationCause.CLICK)
    recorder._append(PageTextEvent, page_id=0, url=after, text=f"Início\nOlá, {user}")

    recorder._hide_usernames()
    events = [recorder._scrub(event) for event in recorder._events]
    dumped = Recording(started_at=datetime.now(UTC), events=events).model_dump_json()
    for leaked in (user, "ana+souza", "ana%20souza", "souza%40exemplo"):
        assert leaked not in dumped
    usuario = events[1]
    assert isinstance(usuario, InputEvent) and usuario.value is None and usuario.typed is None
    assert usuario.element.is_username and usuario.element.sensitive
    navigation = events[3]
    assert isinstance(navigation, NavigationEvent)
    assert f"usuario={REDACTED}" in navigation.url and "pagina=2" in navigation.url
    page_text = events[4]
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
