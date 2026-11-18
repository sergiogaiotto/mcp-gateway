"""Compilação determinística da gravação num robô.

``lower`` transforma uma ``rapa.recorder.Recording`` num ``Manifest`` que passa
no portão de política, sem modelo de linguagem. ``generalize_inputs`` lista os
valores literais que podem virar entradas do robô (para
``rapa.core.edits.Parametrize``).
"""

from rapa.compiler.generalize import ParameterCandidate, generalize_inputs, suggest_type
from rapa.compiler.lowering import CompileError, CompileResult, lower

__all__ = [
    "CompileError",
    "CompileResult",
    "ParameterCandidate",
    "generalize_inputs",
    "lower",
    "suggest_type",
]
