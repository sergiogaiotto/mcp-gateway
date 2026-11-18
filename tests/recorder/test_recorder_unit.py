"""Partes do gravador que não precisam de navegador."""

from datetime import UTC, datetime
from importlib import resources

import pytest

from rapa.recorder import REDACTED, Heading, PageTextEvent, Recorder, RecorderError, Recording
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
