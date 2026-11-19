"""Templates de valores dos passos.

Sintaxe fechada e sem lógica: ``{{ inputs.nome }}``, ``{{ vars.nome }}`` e
``{{ item.nome }}``. Não há filtros, expressões nem acesso a atributos, para que
um template escrito a partir de conteúdo não confiável nunca execute código.
Para escrever ``{{`` literal, use ``{{{{``.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "Ref",
    "Scope",
    "TemplateError",
    "escape",
    "format_value",
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
# Números digitáveis: de 1e-18 a 1e18 (exclusive) em valor absoluto, além do zero.
_MIN_EXPONENT = -18
_MAX_EXPONENT = 17

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


def _format_decimal(value: Decimal) -> str:
    # Sem expoente ("1e+16" ou "1E+2" seriam digitados como texto) e com limite de
    # tamanho, para um número enorme não virar centenas de dígitos.
    if not value.is_finite():
        raise TemplateError("não é um número finito")
    if value and not _MIN_EXPONENT <= value.adjusted() <= _MAX_EXPONENT:
        raise TemplateError("é um número grande ou pequeno demais para ser digitado")
    return format(value, "f")


def format_value(value: object) -> str:
    """Texto que o robô digita para um valor simples.

    Números inteiros que chegam como ``float`` (``5.0``, comum em JSON e em
    formulários) viram ``"5"``; os demais números nunca usam expoente.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TemplateError("não é um número finito")
        if value.is_integer() and abs(value) < 10 ** (_MAX_EXPONENT + 1):
            return str(int(value))
        # repr() dá os dígitos mais curtos que voltam ao mesmo float.
        return _format_decimal(Decimal(repr(value)))
    if isinstance(value, Decimal):
        return _format_decimal(value)
    raise TemplateError(f"não é um valor simples (tipo {type(value).__name__})")


def render(
    template: str,
    context: Mapping[Scope, Mapping[str, object]],
    *,
    encode: Callable[[int, str], str] | None = None,
) -> str:
    """Substitui as referências pelos valores do contexto.

    Falha se alguma referência não tiver valor: um robô nunca digita um campo
    vazio por engano. ``encode``, quando dado, transforma o texto de cada valor
    (nunca os trechos literais), por exemplo para codificá-lo num endereço. Ele
    recebe a posição da referência na lista de :func:`parse` (o trecho do
    endereço onde ela cai pode mudar a regra) e o texto, e pode recusar o valor
    levantando :class:`TemplateError`.
    """
    out: list[str] = []
    for index, part in enumerate(parse(template)):
        if isinstance(part, str):
            out.append(part)
            continue
        scope_values = context.get(part.scope)
        if scope_values is None or part.name not in scope_values:
            raise TemplateError(f"sem valor para {part}")
        try:
            text = format_value(scope_values[part.name])
            out.append(encode(index, text) if encode is not None else text)
        except TemplateError as exc:
            raise TemplateError(f"{part}: {exc}") from None
    return "".join(out)
