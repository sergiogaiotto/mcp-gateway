"""Validação das entradas de uma execução pelo JSON Schema do manifesto.

Só adapta o manifesto ao validador único do RaPA (:mod:`rapa.control.validation`),
que roda no rascunho 2020-12, sem rede, com cache e mensagens em pt-BR que nunca
repetem o valor recebido.
"""

from typing import Any

from rapa.control import validation
from rapa.core.robot import Manifest

__all__ = ["validate_inputs"]


def validate_inputs(manifest: Manifest, inputs: dict[str, Any]) -> list[str]:
    """Erros de validação das entradas, em pt-BR; lista vazia se estão corretas."""
    return validation.validate_inputs(manifest.input_json_schema(), inputs)
