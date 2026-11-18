"""Regras de formato das mensagens aceitas pelo hub.

O template do Qwen exige exatamente uma mensagem de sistema, sempre a
primeira, não conhece o papel ``developer`` e falha se não houver nenhuma
mensagem de usuário. As regras são conferidas antes de enviar, para que o erro
apareça aqui, em pt-BR, e não como um 500 do servidor. As mensagens de erro
nunca repetem o conteúdo das mensagens.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from rapa.llm.errors import MessageShapeError

__all__ = ["ALLOWED_ROLES", "Message", "validate_messages"]

type Message = Mapping[str, Any]

ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_ALLOWED_KEYS = frozenset({"role", "content", "name", "tool_calls", "tool_call_id"})
_PART_TYPES = frozenset({"text", "image_url"})


def _check_parts(position: int, parts: list[Any]) -> None:
    if not parts:
        raise MessageShapeError(f"a mensagem {position} tem uma lista de partes vazia")
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") not in _PART_TYPES:
            raise MessageShapeError(
                f"a mensagem {position} tem uma parte de tipo não aceito; use 'text' ou 'image_url'"
            )
        if part["type"] == "text" and not isinstance(part.get("text"), str):
            raise MessageShapeError(f"a mensagem {position} tem uma parte 'text' sem texto")
        if part["type"] == "image_url":
            image = part.get("image_url")
            if not isinstance(image, Mapping) or not isinstance(image.get("url"), str):
                raise MessageShapeError(
                    f"a mensagem {position} tem uma parte 'image_url' sem 'url'"
                )


def _check_content(position: int, role: str, message: Message) -> None:
    content = message.get("content")
    if role == "assistant":
        if content is None or content == "":
            if not message.get("tool_calls"):
                raise MessageShapeError(
                    f"a mensagem {position} (assistant) não tem conteúdo nem chamadas de ferramenta"
                )
            return
    if isinstance(content, str):
        if role in {"system", "user"} and not content.strip():
            raise MessageShapeError(f"a mensagem {position} ({role}) está vazia")
        return
    if isinstance(content, list) and role in {"user", "assistant"}:
        _check_parts(position, content)
        if role == "assistant" and any(p.get("type") == "image_url" for p in content):
            raise MessageShapeError(f"a mensagem {position} (assistant) não pode trazer imagem")
        return
    raise MessageShapeError(f"a mensagem {position} ({role}) tem conteúdo em formato não aceito")


def validate_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Confere as regras e devolve uma cópia das mensagens pronta para enviar.

    Regras: no máximo uma mensagem ``system`` e, se houver, a primeira; papéis
    só ``system``, ``user``, ``assistant`` e ``tool``; ao menos uma mensagem
    ``user``; mensagens ``tool`` com ``tool_call_id``.
    """
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise MessageShapeError("as mensagens devem ser uma lista")
    if not messages:
        raise MessageShapeError("a lista de mensagens está vazia")
    checked: list[dict[str, Any]] = []
    has_user = False
    for index, message in enumerate(messages):
        position = index + 1
        if not isinstance(message, Mapping):
            raise MessageShapeError(f"a mensagem {position} não é um objeto")
        role = message.get("role")
        if role == "developer":
            raise MessageShapeError(
                f"a mensagem {position} usa o papel 'developer', que o hub não aceita; "
                "use uma única mensagem 'system' no início"
            )
        if role not in ALLOWED_ROLES:
            raise MessageShapeError(
                f"a mensagem {position} tem papel inválido; use system, user, assistant ou tool"
            )
        extra = set(message) - _ALLOWED_KEYS
        if extra:
            names = ", ".join(sorted(str(key) for key in extra))
            raise MessageShapeError(f"a mensagem {position} tem campos não aceitos: {names}")
        if role == "system" and index != 0:
            raise MessageShapeError(
                f"a mensagem {position} é 'system', mas só pode haver uma mensagem de sistema, "
                "e ela deve ser a primeira"
            )
        if role == "tool" and not isinstance(message.get("tool_call_id"), str):
            raise MessageShapeError(f"a mensagem {position} (tool) precisa de 'tool_call_id'")
        if "tool_calls" in message and role != "assistant":
            raise MessageShapeError(
                f"a mensagem {position} traz 'tool_calls', que só cabe em mensagens 'assistant'"
            )
        _check_content(position, str(role), message)
        has_user = has_user or role == "user"
        checked.append(dict(message))
    if not has_user:
        raise MessageShapeError("é preciso ao menos uma mensagem 'user'")
    return checked
