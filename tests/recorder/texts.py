"""Procura de valores sigilosos só nos textos de uma gravação, de um robô ou de um resultado.

Um segredo curto só de dígitos ("4829", um código de seis dígitos) aparece por
acaso nos números da gravação: o ``t_ms`` e o ``editing_since_ms`` têm muitas casas
decimais e o ``finished_at`` tem os microssegundos ("…37.864829Z"). Sob carga, a
busca no JSON inteiro acusava esses números (rodada 7, H3: em 900 gravações sob
carga, 7 tinham "4829", todas num ``t_ms`` ou no ``finished_at``, nenhuma num
texto). Aqui a busca olha só os campos de texto: o modelo em modo Python, em que
datas e números não viram texto.
"""

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

__all__ = ["has_text", "texts_of"]


def texts_of(value: Any) -> Iterator[str]:
    """Todos os textos de um modelo (ou de listas, conjuntos e dicionários), chaves incluídas."""
    if isinstance(value, BaseModel):
        yield from texts_of(value.model_dump(mode="python"))
    elif isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from texts_of(key)
            yield from texts_of(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from texts_of(item)


def has_text(value: Any, secret: str, *, ignore_case: bool = False) -> bool:
    """O segredo aparece em algum texto do valor (nunca num número ou numa data)."""
    if ignore_case:
        secret = secret.lower()
        return any(secret in text.lower() for text in texts_of(value))
    return any(secret in text for text in texts_of(value))
