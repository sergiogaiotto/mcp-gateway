"""Validação das entradas dos robôs (único uso de jsonschema_rs no RaPA)."""

import re
from pathlib import Path
from typing import Any

import pytest

from rapa.control.validation import (
    InputValidator,
    InvalidSchemaError,
    schema_fingerprint,
    validate_inputs,
)
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from tests.control.api.support import BASE, HOST, INPUTS

SRC = Path(__file__).resolve().parents[3] / "src" / "rapa"


def test_robot_schemas_validate() -> None:
    schema = emitir_protocolo(BASE, HOST).input_json_schema()
    assert validate_inputs(schema, INPUTS) == []
    assert validate_inputs(baixar_notas(BASE, HOST).input_json_schema(), {}) == []


def test_messages_in_portuguese_without_values() -> None:
    schema = emitir_protocolo(BASE, HOST).input_json_schema()
    secret = "valor-que-nao-pode-aparecer"
    errors = validate_inputs(schema, {"nome": 42, "cpf": secret, "extra": secret})
    assert errors == [
        "campo obrigatório ausente: uf",
        "nome: tipo inválido; esperado texto",
        "cpf: valor fora do formato esperado",
        "campo não declarado: extra",
    ]
    assert all(secret not in error for error in errors)
    assert validate_inputs(schema, []) == ["entradas: tipo inválido; esperado objeto"]


def test_nested_paths_formats_and_limits() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {"type": "string", "format": "date"},
            "itens": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {"numero": {"type": "integer", "minimum": 1}},
                    "required": ["numero"],
                },
            },
            "nome": {"type": "string", "maxLength": 3},
            "uf": {"enum": ["SP", "RJ"]},
        },
    }
    errors = validate_inputs(
        schema,
        {
            "data": "2026-02-30",
            "itens": [{"numero": 0}, {}, {"numero": "um"}],
            "nome": "abcd",
            "uf": "MG",
        },
    )
    assert "data: formato inválido; esperado data no formato AAAA-MM-DD" in errors
    assert "itens: itens demais (máximo de 2)" in errors
    assert "itens[0].numero: valor abaixo do mínimo (1)" in errors
    assert "campo obrigatório ausente: itens[1].numero" in errors
    assert "itens[2].numero: tipo inválido; esperado número inteiro" in errors
    assert "nome: texto longo demais (máximo de 3 caracteres)" in errors
    assert "uf: valor fora das opções permitidas" in errors


def test_error_list_is_bounded() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    validator = InputValidator(max_errors=5)
    errors = validator.errors(schema, {f"campo_{i}": "x" for i in range(50)})
    assert len(errors) == 6
    assert errors[-1].startswith("há mais erros")


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.com/esquema.json"},
        {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/latest"}}},
        {"$ref": "file:///etc/passwd"},
        {"anyOf": [{"type": "string"}, {"$ref": "outro.json"}]},
        {"$id": "https://rapa.test/raiz.json", "$ref": "vizinho.json"},
    ],
)
def test_remote_and_file_refs_are_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(InvalidSchemaError, match="referência externa"):
        InputValidator().compiled(schema)


def test_local_refs_and_invalid_schemas() -> None:
    local = {
        "$defs": {"uf": {"type": "string", "enum": ["SP", "RJ"]}},
        "type": "object",
        "properties": {"uf": {"$ref": "#/$defs/uf"}},
    }
    assert validate_inputs(local, {"uf": "SP"}) == []
    assert validate_inputs(local, {"uf": "MG"}) == ["uf: valor fora das opções permitidas"]
    with pytest.raises(InvalidSchemaError):
        InputValidator().compiled({"type": "texto"})
    with pytest.raises(InvalidSchemaError):
        InputValidator().compiled(["não", "é", "objeto"])  # type: ignore[arg-type]


def test_draft_2020_12_is_forced() -> None:
    # prefixItems só existe no 2020-12; mesmo declarando o draft 7, vale o 2020-12.
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "array",
        "prefixItems": [{"type": "integer"}],
    }
    assert validate_inputs(schema, ["x"]) == ["[0]: tipo inválido; esperado número inteiro"]


def test_cache_is_lru_and_keyed_by_fingerprint() -> None:
    validator = InputValidator(cache_size=2)
    first = {"type": "object", "properties": {"a": {"type": "string"}}}
    same = {"properties": {"a": {"type": "string"}}, "type": "object"}
    assert schema_fingerprint(first) == schema_fingerprint(same)
    compiled = validator.compiled(first)
    assert validator.compiled(same) is compiled
    validator.compiled({"type": "object"})
    validator.compiled({"type": "array"})
    assert len(validator) == 2
    assert validator.compiled(first) is not compiled
    with pytest.raises(ValueError):
        InputValidator(cache_size=0)


def test_jsonschema_rs_only_used_by_the_wrapper() -> None:
    pattern = re.compile(r"^\s*(import|from)\s+jsonschema_rs\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
        and path != SRC / "control" / "validation.py"
    ]
    assert offenders == []
