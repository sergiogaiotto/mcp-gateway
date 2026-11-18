"""Templates de valores dos passos.

Sintaxe fechada e sem lógica: ``{{ inputs.nome }}``, ``{{ vars.nome }}`` e
``{{ item.nome }}``. Não há filtros, expressões nem acesso a atributos, para que
um template escrito a partir de conteúdo não confiável nunca execute código.
Para escrever ``{{`` literal, use ``{{{{``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "Ref",
    "Scope",
    "TemplateError",
    "escape",
    "has_references",
    "parse",
    "references",
    "render",
]


class Scope(StrEnum):
    INPUTS = "inputs"
    VARS = "vars"
    ITEM = "item"


class TemplateError(ValueError):
    """Template mal formado ou referência sem valor."""


@dataclass(frozen=True, slots=True)
class Ref:
    scope: Scope
    name: str

    def __str__(self) -> str:
        return f"{self.scope.value}.{self.name}"


_REF = re.compile(r"\s*(inputs|vars|item)\.([a-z_][a-z0-9_]{0,63})\s*")
_MAX_TEMPLATE_LENGTH = 4000

type Part = str | Ref


def parse(template: str) -> list[Part]:
    """Divide o template em trechos literais e referências."""
    if len(template) > _MAX_TEMPLATE_LENGTH:
        raise TemplateError(f"template maior que {_MAX_TEMPLATE_LENGTH} caracteres")
    parts: list[Part] = []
    literal: list[str] = []
    i = 0
    while i < len(template):
        if template.startswith("{{{{", i):
            literal.append("{{")
            i += 4
            continue
        if template.startswith("{{", i):
            end = template.find("}}", i + 2)
            if end == -1:
                raise TemplateError("'{{' sem '}}' correspondente")
            match = _REF.fullmatch(template, i + 2, end)
            if match is None:
                raise TemplateError(
                    f"referência inválida: {template[i : end + 2]!r}; "
                    "use inputs.<nome>, vars.<nome> ou item.<nome>"
                )
            if literal:
                parts.append("".join(literal))
                literal = []
            parts.append(Ref(Scope(match.group(1)), match.group(2)))
            i = end + 2
            continue
        literal.append(template[i])
        i += 1
    if literal:
        parts.append("".join(literal))
    return parts


def references(template: str) -> list[Ref]:
    """Lista as referências do template, na ordem em que aparecem."""
    return [part for part in parse(template) if isinstance(part, Ref)]


def has_references(template: str) -> bool:
    return bool(references(template))


def escape(text: str) -> str:
    """Converte um texto literal num template que o reproduz exatamente."""
    return text.replace("{{", "{{{{")


def _format(value: object, ref: Ref) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TemplateError(f"{ref} não é um valor simples (tipo {type(value).__name__})")


def render(template: str, context: Mapping[Scope, Mapping[str, object]]) -> str:
    """Substitui as referências pelos valores do contexto.

    Falha se alguma referência não tiver valor: um robô nunca digita um campo
    vazio por engano.
    """
    out: list[str] = []
    for part in parse(template):
        if isinstance(part, str):
            out.append(part)
            continue
        scope_values = context.get(part.scope)
        if scope_values is None or part.name not in scope_values:
            raise TemplateError(f"sem valor para {part}")
        out.append(_format(scope_values[part.name], part))
    return "".join(out)
