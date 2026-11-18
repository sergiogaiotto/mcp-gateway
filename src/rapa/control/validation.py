"""Validação das entradas de um robô contra o JSON Schema publicado.

Único ponto do RaPA que usa ``jsonschema_rs``: o restante do código chama
:func:`validate_inputs` ou um :class:`InputValidator`. Regras:

- sempre o rascunho 2020-12, qualquer que seja o ``$schema`` declarado;
- nada de ``$ref`` remoto ou de arquivo: a validação roda desligada da rede
  (``offline``), e um esquema que aponte para fora é recusado ao compilar;
- os validadores compilados ficam num cache LRU limitado, indexado pela
  impressão digital (SHA-256) da forma canônica do esquema;
- as mensagens de erro saem em pt-BR e nunca repetem o valor recebido, só o
  caminho do campo e o motivo.
"""

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema_rs

__all__ = [
    "DEFAULT_CACHE_SIZE",
    "MAX_ERRORS",
    "InputValidator",
    "InvalidSchemaError",
    "schema_fingerprint",
    "validate_inputs",
]

DEFAULT_CACHE_SIZE = 256
#: Quantos erros, no máximo, voltam numa resposta.
MAX_ERRORS = 20

_TYPE_NAMES = {
    "string": "texto",
    "integer": "número inteiro",
    "number": "número",
    "boolean": "verdadeiro ou falso",
    "object": "objeto",
    "array": "lista",
    "null": "nulo",
}

_FORMAT_NAMES = {
    "date": "data no formato AAAA-MM-DD",
    "date-time": "data e hora no formato ISO 8601",
    "time": "hora no formato ISO 8601",
    "email": "endereço de e-mail",
    "uuid": "identificador UUID",
    "uri": "endereço (URI)",
}


class InvalidSchemaError(ValueError):
    """O esquema do robô não compila (inválido ou com referência externa)."""


def schema_fingerprint(schema: Mapping[str, Any] | bool) -> str:
    """SHA-256 da forma canônica (chaves ordenadas, sem espaços) do esquema."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _path(parts: Sequence[str | int]) -> str:
    if not parts:
        return "entradas"
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _child(parts: Sequence[str | int], name: str) -> str:
    return _path([*parts, name])


def _types(types: Sequence[str]) -> str:
    names = [_TYPE_NAMES.get(t, t) for t in types]
    return " ou ".join(names)


def _message(error: jsonschema_rs.ValidationError) -> str:
    """Mensagem em pt-BR sem o valor recebido (que pode ser sensível)."""
    kind = error.kind
    where = error.instance_path
    path = _path(where)
    match kind:
        case jsonschema_rs.ValidationErrorKind.Required(prop):
            return f"campo obrigatório ausente: {_child(where, prop)}"
        case jsonschema_rs.ValidationErrorKind.AdditionalProperties(unexpected):
            names = ", ".join(_child(where, name) for name in sorted(unexpected))
            return f"campo não declarado: {names}"
        case jsonschema_rs.ValidationErrorKind.UnevaluatedProperties(unexpected):
            names = ", ".join(_child(where, name) for name in sorted(unexpected))
            return f"campo não declarado: {names}"
        case jsonschema_rs.ValidationErrorKind.Type(types):
            return f"{path}: tipo inválido; esperado {_types(types)}"
        case jsonschema_rs.ValidationErrorKind.Format(fmt):
            return f"{path}: formato inválido; esperado {_FORMAT_NAMES.get(fmt, fmt)}"
        case jsonschema_rs.ValidationErrorKind.Pattern():
            return f"{path}: valor fora do formato esperado"
        case jsonschema_rs.ValidationErrorKind.MaxLength(limit):
            return f"{path}: texto longo demais (máximo de {limit} caracteres)"
        case jsonschema_rs.ValidationErrorKind.MinLength(limit):
            return f"{path}: texto curto demais (mínimo de {limit} caracteres)"
        case jsonschema_rs.ValidationErrorKind.MaxItems(limit):
            return f"{path}: itens demais (máximo de {limit})"
        case jsonschema_rs.ValidationErrorKind.MinItems(limit):
            return f"{path}: itens de menos (mínimo de {limit})"
        case jsonschema_rs.ValidationErrorKind.Maximum(limit):
            return f"{path}: valor acima do máximo ({limit})"
        case jsonschema_rs.ValidationErrorKind.ExclusiveMaximum(limit):
            return f"{path}: valor deve ser menor que {limit}"
        case jsonschema_rs.ValidationErrorKind.Minimum(limit):
            return f"{path}: valor abaixo do mínimo ({limit})"
        case jsonschema_rs.ValidationErrorKind.ExclusiveMinimum(limit):
            return f"{path}: valor deve ser maior que {limit}"
        case jsonschema_rs.ValidationErrorKind.Enum():
            return f"{path}: valor fora das opções permitidas"
        case jsonschema_rs.ValidationErrorKind.Constant():
            return f"{path}: valor diferente do permitido"
        case jsonschema_rs.ValidationErrorKind.UniqueItems():
            return f"{path}: a lista tem itens repetidos"
        case jsonschema_rs.ValidationErrorKind.MaxProperties(limit):
            return f"{path}: campos demais (máximo de {limit})"
        case jsonschema_rs.ValidationErrorKind.MinProperties(limit):
            return f"{path}: campos de menos (mínimo de {limit})"
        case jsonschema_rs.ValidationErrorKind.MultipleOf(multiple):
            return f"{path}: o valor deve ser múltiplo de {multiple}"
        case jsonschema_rs.ValidationErrorKind.FalseSchema():
            return f"{path}: campo não permitido"
        case jsonschema_rs.ValidationErrorKind.Referencing():
            return f"{path}: o esquema tem uma referência que não pode ser resolvida"
        case _:
            return f"{path}: valor inválido ({kind.name})"


@dataclass(frozen=True, slots=True)
class _Compiled:
    validator: jsonschema_rs.Validator


class InputValidator:
    """Valida entradas contra esquemas de robôs, com cache LRU de validadores."""

    def __init__(self, cache_size: int = DEFAULT_CACHE_SIZE, max_errors: int = MAX_ERRORS) -> None:
        if cache_size < 1:
            raise ValueError("o cache precisa de pelo menos uma posição")
        if max_errors < 1:
            raise ValueError("é preciso devolver pelo menos um erro")
        self._cache_size = cache_size
        self._max_errors = max_errors
        self._cache: OrderedDict[str, _Compiled] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def compiled(self, schema: Mapping[str, Any]) -> jsonschema_rs.Validator:
        """Validador compilado do esquema, do cache quando possível.

        Levanta :class:`InvalidSchemaError` se o esquema for inválido ou se
        referir a um recurso externo (rede ou arquivo).
        """
        key = schema_fingerprint(schema)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.validator
        validator = self._compile(schema)
        with self._lock:
            self._cache[key] = _Compiled(validator)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return validator

    @staticmethod
    def _compile(schema: Mapping[str, Any]) -> jsonschema_rs.Validator:
        if not isinstance(schema, Mapping):
            raise InvalidSchemaError("o esquema de entradas precisa ser um objeto JSON")
        try:
            return jsonschema_rs.Draft202012Validator(
                dict(schema),
                validate_formats=True,
                ignore_unknown_formats=True,
                offline=True,
            )
        except jsonschema_rs.ValidationError as exc:
            if isinstance(exc.kind, jsonschema_rs.ValidationErrorKind.Referencing):
                raise InvalidSchemaError(
                    "o esquema de entradas aponta para uma referência externa, o que não é permitido"
                ) from None
            raise InvalidSchemaError("o esquema de entradas é inválido") from None
        except ValueError, TypeError, jsonschema_rs.ReferencingError:
            raise InvalidSchemaError("o esquema de entradas é inválido") from None

    def errors(self, schema: Mapping[str, Any], instance: Any) -> list[str]:
        """Lista de erros em pt-BR (vazia quando as entradas são válidas)."""
        validator = self.compiled(schema)
        messages: list[str] = []
        try:
            for error in validator.iter_errors(instance):
                message = _message(error)
                if message not in messages:
                    messages.append(message)
                if len(messages) >= self._max_errors:
                    messages.append("há mais erros; corrija estes e tente de novo")
                    break
        except jsonschema_rs.ValidationError:
            # Erro de referência só aparece na validação: o esquema é que está quebrado.
            raise InvalidSchemaError("o esquema de entradas não pôde ser aplicado") from None
        return messages


_default = InputValidator()


def validate_inputs(schema: Mapping[str, Any], instance: Any) -> list[str]:
    """Valida ``instance`` contra ``schema`` com o validador compartilhado do processo."""
    return _default.errors(schema, instance)
