"""Candidatos a parâmetro: valores literais digitados ou escolhidos na gravação.

É só dado, para o assistente de generalização (o modelo de linguagem propõe
quais viram entradas do robô; ``rapa.core.edits.Parametrize`` aplica).
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rapa.compiler.naming import clip_label, name_from
from rapa.core import templates
from rapa.core.edits import Parametrize
from rapa.core.robot import (
    NAME_PATTERN,
    FillStep,
    LocatorBy,
    Manifest,
    SelectStep,
    Target,
    TextValue,
)

__all__ = ["ParameterCandidate", "generalize_inputs", "suggest_type"]

type SimpleType = Literal["string", "integer", "number", "boolean", "date", "cpf", "cnpj", "money"]

_CPF = re.compile(r"^\d{3}\.\d{3}\.\d{3}-\d{2}$")
_CPF_DIGITS = re.compile(r"^\d{11}$")
_CNPJ = re.compile(r"^[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-\d{2}$")
_CNPJ_DIGITS = re.compile(r"^[0-9A-Z]{12}\d{2}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d{2}$")
_PREVIEW = 80


class ParameterCandidate(BaseModel):
    """Um valor literal que pode virar entrada do robô."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    step_kind: Literal["fill", "select"]
    intent: str
    #: Rótulo do campo, quando conhecido.
    field_label: str | None = None
    #: Valor gravado, encurtado para exibição.
    value_preview: str
    suggested_name: str = Field(pattern=NAME_PATTERN)
    suggested_title: str = Field(min_length=1, max_length=200)
    suggested_type: SimpleType = "string"

    def to_edit(self, input_name: str | None = None, title: str | None = None) -> Parametrize:
        """A operação ``Parametrize`` correspondente (nome e título podem ser trocados)."""
        return Parametrize(
            step_id=self.step_id,
            input_name=input_name or self.suggested_name,
            title=title or self.suggested_title,
            type=self.suggested_type,
        )


def suggest_type(value: str, label: str | None) -> SimpleType:
    """Tipo provável do valor (nunca ``integer`` para não perder zeros à esquerda)."""
    text = (label or "").lower()
    stripped = value.strip()
    if _CPF.fullmatch(stripped) or ("cpf" in text and _CPF_DIGITS.fullmatch(stripped)):
        return "cpf"
    if _CNPJ.fullmatch(stripped) or ("cnpj" in text and _CNPJ_DIGITS.fullmatch(stripped)):
        return "cnpj"
    if _ISO_DATE.fullmatch(stripped):
        return "date"
    if _MONEY.fullmatch(stripped):
        return "money"
    return "string"


def _field_label(target: Target) -> str | None:
    for by in (LocatorBy.LABEL, LocatorBy.ROLE, LocatorBy.PLACEHOLDER):
        for locator in target.locators:
            if locator.by is by:
                value = locator.name if by is LocatorBy.ROLE else locator.value
                if value and not templates.has_references(value):
                    return templates.render(value, {})
    return None


def _literal(template: str) -> str | None:
    try:
        if templates.has_references(template):
            return None
        return templates.render(template, {})
    except templates.TemplateError:
        return None


def generalize_inputs(manifest: Manifest) -> list[ParameterCandidate]:
    """Lista os valores literais de ``fill`` (texto) e ``select`` que podem virar entradas."""
    taken = {f.name for f in manifest.inputs}
    candidates: list[ParameterCandidate] = []
    for step, _ in manifest.iter_steps():
        if isinstance(step, FillStep) and isinstance(step.value, TextValue):
            kind: Literal["fill", "select"] = "fill"
            value = _literal(step.value.template)
        elif isinstance(step, SelectStep):
            kind = "select"
            value = _literal(step.option)
        else:
            continue
        if value is None:
            continue
        label = _field_label(step.target)
        base = name_from(label or step.target.intent, "valor")
        name = base
        n = 2
        while name in taken:
            name = f"{base}_{n}"
            n += 1
        taken.add(name)
        candidates.append(
            ParameterCandidate(
                step_id=step.id,
                step_kind=kind,
                intent=step.target.intent,
                field_label=label,
                value_preview=clip_label(value, _PREVIEW),
                suggested_name=name,
                suggested_title=clip_label(label or step.target.intent, 200),
                suggested_type=suggest_type(value, label),
            )
        )
    return candidates
