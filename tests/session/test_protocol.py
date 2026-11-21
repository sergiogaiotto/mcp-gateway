"""Mensagens do canal ao vivo, tradução para o CDP e envio de quadros sem acúmulo."""

import asyncio
import json

import pytest

from rapa.session.keys import (
    CTRL,
    SHIFT,
    MouseState,
    key_commands,
    text_commands,
    wheel_commands,
)
from rapa.session.protocol import (
    MAX_CLIENT_MESSAGE,
    VIEWER_MESSAGES,
    FrameSender,
    KeyMessage,
    MouseMessage,
    NavigateMessage,
    ProtocolError,
    ResizeMessage,
    StopMessage,
    TextMessage,
    WheelMessage,
    parse_client_message,
    parse_host_event,
)


def _msg(**data: object) -> str:
    return json.dumps(data, ensure_ascii=False)


def test_parses_every_input_message() -> None:
    assert isinstance(
        parse_client_message(_msg(tipo="mouse", acao="apertar", x=10, y=20, botao="esquerdo")),
        MouseMessage,
    )
    assert isinstance(parse_client_message(_msg(tipo="roda", x=1, y=2, dy=120)), WheelMessage)
    assert isinstance(
        parse_client_message(_msg(tipo="tecla", acao="descer", tecla="Enter")), KeyMessage
    )
    assert isinstance(parse_client_message(_msg(tipo="texto", texto="ção")), TextMessage)
    assert isinstance(
        parse_client_message(_msg(tipo="tamanho", largura=800, altura=600)), ResizeMessage
    )
    assert isinstance(
        parse_client_message(_msg(tipo="navegar", url="https://portal.rapa.test/a")),
        NavigateMessage,
    )
    assert isinstance(parse_client_message(_msg(tipo="parar")), StopMessage)
    assert "parar" not in VIEWER_MESSAGES


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00binario",
        "não é json",
        _msg(tipo="desconhecido"),
        _msg(tipo="mouse", acao="voar", x=1, y=1),
        _msg(tipo="mouse", acao="mover", x=-5, y=1),
        _msg(tipo="mouse", acao="mover", x=1, y=1, extra="x"),
        _msg(tipo="texto", texto=""),
        _msg(tipo="tamanho", largura=10, altura=10),
        _msg(tipo="navegar", url="javascript:alert(1)"),
        _msg(tipo="navegar", url="file:///etc/passwd"),
        _msg(tipo="navegar", url="https://ana:segredo@portal.rapa.test/"),
        _msg(tipo="tecla", acao="descer", tecla="x" * 40),
        "x" * (MAX_CLIENT_MESSAGE + 1),
    ],
)
def test_rejects_malformed_messages_without_echoing_them(raw: str | bytes) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_client_message(raw)
    assert "segredo" not in str(caught.value)
    assert "passwd" not in str(caught.value)


def test_resize_is_clamped_to_browser_limits() -> None:
    message = ResizeMessage(largura=5000, altura=3000)
    assert message.clamped() == (1920, 1200)


def test_host_events_need_a_type() -> None:
    assert parse_host_event('{"tipo": "pronta"}') == {"tipo": "pronta"}
    with pytest.raises(ProtocolError):
        parse_host_event("[1]")
    with pytest.raises(ProtocolError):
        parse_host_event("x")


def test_accented_text_goes_through_insert_text() -> None:
    for text in ("ã", "ç", "João da Conceição", "é", "€", "olá mundo"):
        assert text_commands(TextMessage(texto=text)) == [("Input.insertText", {"text": text})]


def test_us_characters_become_key_presses_with_text() -> None:
    commands = text_commands(TextMessage(texto="a"))
    assert [c[1]["type"] for c in commands] == ["keyDown", "keyUp"]
    down = commands[0][1]
    assert down["key"] == "a" and down["code"] == "KeyA" and down["text"] == "a"
    assert down["windowsVirtualKeyCode"] == 65 and down["modifiers"] == 0
    upper = text_commands(TextMessage(texto="A"))[0][1]
    assert upper["modifiers"] == SHIFT and upper["text"] == "A"
    at = text_commands(TextMessage(texto="@"))[0][1]
    assert at["code"] == "Digit2" and at["modifiers"] == SHIFT
    space = text_commands(TextMessage(texto=" "))[0][1]
    assert space["code"] == "Space" and space["text"] == " "
    assert "text" not in text_commands(TextMessage(texto="1"))[1][1]  # keyUp sem texto


def test_newline_is_enter() -> None:
    commands = text_commands(TextMessage(texto="\n"))
    assert commands[0][1]["key"] == "Enter" and commands[0][1]["text"] == "\r"


def test_special_keys_and_shortcuts() -> None:
    enter = key_commands(KeyMessage(acao="descer", tecla="Enter"))
    assert enter[0][1]["type"] == "keyDown" and enter[0][1]["text"] == "\r"
    tab = key_commands(KeyMessage(acao="descer", tecla="Tab", modificadores=SHIFT))
    assert tab[0][1]["type"] == "rawKeyDown" and tab[0][1]["modifiers"] == SHIFT
    assert "text" not in tab[0][1]
    assert key_commands(KeyMessage(acao="subir", tecla="ArrowLeft"))[0][1]["type"] == "keyUp"
    select_all = key_commands(KeyMessage(acao="descer", tecla="a", modificadores=CTRL))
    assert select_all[0][1]["code"] == "KeyA" and "text" not in select_all[0][1]
    # Caractere sem atalho chega como texto; tecla desconhecida não vira nada.
    assert key_commands(KeyMessage(acao="descer", tecla="a")) == []
    assert key_commands(KeyMessage(acao="descer", tecla="Dead")) == []
    assert key_commands(KeyMessage(acao="descer", tecla="ç", modificadores=CTRL)) == []


def test_mouse_tracks_buttons_and_clamps_to_viewport() -> None:
    state = MouseState()
    down = state.commands(
        MouseMessage(acao="apertar", x=5000, y=10, botao="esquerdo", cliques=1),
        width=800,
        height=600,
    )[0][1]
    assert down["type"] == "mousePressed" and down["buttons"] == 1 and down["x"] == 799
    move = state.commands(MouseMessage(acao="mover", x=10, y=10), width=800, height=600)[0][1]
    assert move["button"] == "left" and move["buttons"] == 1
    up = state.commands(
        MouseMessage(acao="soltar", x=10, y=10, botao="esquerdo", cliques=1),
        width=800,
        height=600,
    )[0][1]
    assert up["type"] == "mouseReleased" and up["buttons"] == 0 and up["clickCount"] == 1
    assert state.commands(MouseMessage(acao="apertar", x=1, y=1), width=800, height=600) == []
    wheel = wheel_commands(WheelMessage(x=10, y=10, dy=240), width=800, height=600)[0][1]
    assert wheel["type"] == "mouseWheel" and wheel["deltaY"] == 240


async def test_frame_sender_drops_intermediate_frames_and_keeps_the_last() -> None:
    gate = asyncio.Event()
    delivered: list[bytes] = []

    async def slow_send(frame: bytes) -> None:
        await gate.wait()
        delivered.append(frame)

    sender = FrameSender(slow_send)
    for n in range(10):
        sender.offer(bytes([n]))
    assert sender.busy
    gate.set()
    for _ in range(50):
        if not sender.busy:
            break
        await asyncio.sleep(0.01)
    # O primeiro já estava saindo; dos que esperaram, só o último chega.
    assert delivered == [b"\x00", b"\x09"]
    assert sender.dropped == 8
    assert sender.sent == 2
    await sender.close()
    sender.offer(b"depois")
    assert delivered == [b"\x00", b"\x09"]


async def test_frame_sender_stops_when_the_connection_breaks() -> None:
    async def broken(frame: bytes) -> None:
        raise ConnectionError

    sender = FrameSender(broken)
    sender.offer(b"a")
    await asyncio.sleep(0.01)
    sender.offer(b"b")
    assert not sender.busy and sender.sent == 0
    await sender.close()
