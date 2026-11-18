from typing import Any

import pytest

from rapa.llm.errors import MessageShapeError
from rapa.llm.messages import validate_messages
from tests.llm.fakehub import FakeHub

SYSTEM = {"role": "system", "content": "Você é objetivo."}
USER = {"role": "user", "content": "Olá"}


def test_valid_conversation_is_copied() -> None:
    messages: list[dict[str, Any]] = [
        SYSTEM,
        USER,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "42"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "e esta imagem?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]
    checked = validate_messages(messages)
    assert checked == messages
    assert checked[0] is not messages[0]


def test_system_is_optional_but_user_is_required() -> None:
    assert validate_messages([USER]) == [USER]
    with pytest.raises(MessageShapeError, match="ao menos uma mensagem 'user'"):
        validate_messages([SYSTEM])


@pytest.mark.parametrize(
    ("messages", "fragment"),
    [
        ([], "vazia"),
        ("texto", "lista"),
        ([{"role": "developer", "content": "x"}, USER], "developer"),
        ([SYSTEM, SYSTEM, USER], "só pode haver uma mensagem de sistema"),
        ([USER, SYSTEM], "deve ser a primeira"),
        ([{"role": "bot", "content": "x"}, USER], "papel inválido"),
        ([{"role": "user", "content": "x", "temperature": 1}], "campos não aceitos"),
        ([USER, {"role": "tool", "content": "42"}], "tool_call_id"),
        ([USER, {"role": "assistant", "content": ""}], "nem chamadas"),
        ([{"role": "user", "content": "   "}], "vazia"),
        ([{"role": "user", "content": 42}], "formato não aceito"),
        ([{"role": "user", "content": [{"type": "audio"}]}], "tipo não aceito"),
        ([{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}], "sem 'url'"),
        ([{"role": "user", "content": "x", "tool_calls": []}], "só cabe em mensagens"),
        ([{"role": "system", "content": [{"type": "text", "text": "x"}]}, USER], "formato"),
    ],
)
def test_invalid_shapes(messages: Any, fragment: str) -> None:
    with pytest.raises(MessageShapeError, match=fragment):
        validate_messages(messages)


def test_error_never_repeats_the_content() -> None:
    secret_prompt = "conteúdo confidencial do prompt"
    with pytest.raises(MessageShapeError) as info:
        validate_messages([SYSTEM, {"role": "system", "content": secret_prompt}, USER])
    assert secret_prompt not in str(info.value)


async def test_client_checks_shape_before_sending() -> None:
    hub = FakeHub()
    async with hub.client() as client:
        with pytest.raises(MessageShapeError):
            await client.chat([{"role": "developer", "content": "x"}, USER])
        with pytest.raises(MessageShapeError):
            await client.chat([USER, SYSTEM])
    assert hub.requests == []
