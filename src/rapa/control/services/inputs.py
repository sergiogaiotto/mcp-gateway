"""Validação das entradas de uma execução pelo JSON Schema do manifesto.

Usa o jsonschema-rs no rascunho 2020-12, com formatos conferidos (por exemplo,
``date``) e sem buscar nada na rede (``offline``): o esquema vem inteiro do
manifesto, sem ``$ref`` remoto. As mensagens saem em pt-BR e nunca repetem o
valor recebido.
"""

import json
from functools import lru_cache
from typing import Any

import jsonschema_rs
from jsonschema_rs import ValidationErrorKind as Kind

from rapa.core.robot import Manifest

__all__ = ["validate_inputs"]

_TYPE_NAMES = {
    "string": "texto",
    "integer": "número inteiro",
    "number": "número",
    "boolean": "verdadeiro ou falso",
    "array": "lista",
    "object": "objeto",
    "null": "nulo",
}

_MAX_ERRORS = 50


@lru_cache(maxsize=512)
def _validator(schema_json: str) -> jsonschema_rs.Validator:
    return jsonschema_rs.Draft202012Validator(
        json.loads(schema_json), validate_formats=True, offline=True
    )


def _path(parts: list[str | int]) -> str:
    return "/".join(str(p) for p in parts)


def _messages(error: jsonschema_rs.ValidationError) -> list[str]:
    path = _path(error.instance_path)
    where = f"{path}: " if path else ""
    kind = error.kind
    if isinstance(kind, Kind.Required):
        if not path:
            return [f"entrada obrigatória ausente: {kind.property}"]
        return [f"{where}campo obrigatório ausente: {kind.property}"]
    if isinstance(kind, Kind.AdditionalProperties):
        if not path:
            return [f"entrada não declarada: {name}" for name in kind.unexpected]
        return [f"{where}campo não declarado: {name}" for name in kind.unexpected]
    if isinstance(kind, Kind.Type):
        expected = " ou ".join(_TYPE_NAMES.get(t, t) for t in kind.types)
        return [f"{where}tipo inválido (esperado {expected})"]
    if isinstance(kind, Kind.Pattern):
        return [f"{where}formato inválido"]
    if isinstance(kind, Kind.Format):
        return [f"{where}formato inválido ({kind.format})"]
    if isinstance(kind, Kind.MaxLength):
        return [f"{where}texto com mais de {kind.limit} caracteres"]
    if isinstance(kind, Kind.MinLength):
        return [f"{where}texto com menos de {kind.limit} caracteres"]
    if isinstance(kind, Kind.MaxItems):
        return [f"{where}lista com mais de {kind.limit} itens"]
    if isinstance(kind, Kind.MinItems):
        return [f"{where}lista com menos de {kind.limit} itens"]
    if isinstance(kind, Kind.Enum):
        return [f"{where}valor fora das opções permitidas"]
    return [f"{where or 'entradas: '}valor inválido ({kind.name})"]


def validate_inputs(manifest: Manifest, inputs: dict[str, Any]) -> list[str]:
    """Erros de validação das entradas, em pt-BR; lista vazia se estão corretas."""
    schema = manifest.input_json_schema()
    validator = _validator(json.dumps(schema, sort_keys=True))
    errors: list[str] = []
    for error in validator.iter_errors(inputs):
        errors.extend(_messages(error))
        if len(errors) >= _MAX_ERRORS:
            break
    return errors[:_MAX_ERRORS]
