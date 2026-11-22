"""Conserto assistido do passo que quebrou, sem modelo de linguagem.

``enumerate_candidates`` lista, só a partir do retrato da página que o executor tirou
na falha (``rapa.core.snapshot.PageSnapshot``), os elementos que podem ser o alvo do
passo, cada um com o alvo montado pelas regras do compilador e uma nota
determinística. ``apply_candidate`` troca o alvo daquele passo, e só dele, num
manifesto novo. O modelo de linguagem (``rapa.llm.repair``) só escolhe entre esses
candidatos; quem decide é uma pessoa, e outra publica.
"""

from rapa.repair.candidates import (
    DEFAULT_CANDIDATES,
    NotRepairable,
    apply_candidate,
    enumerate_candidates,
    repair_target,
    step_info,
)

__all__ = [
    "DEFAULT_CANDIDATES",
    "NotRepairable",
    "apply_candidate",
    "enumerate_candidates",
    "repair_target",
    "step_info",
]
