import math
from decimal import Decimal

import pytest

from rapa.core.templates import (
    Ref,
    Scope,
    TemplateError,
    escape,
    format_value,
    parse,
    references,
    render,
)


def test_parse_literal_and_refs() -> None:
    parts = parse("CPF {{ inputs.cpf }} na linha {{item.numero}}")
    assert parts == ["CPF ", Ref(Scope.INPUTS, "cpf"), " na linha ", Ref(Scope.ITEM, "numero")]


def test_render_strict_missing_value() -> None:
    with pytest.raises(TemplateError, match=r"sem valor para inputs\.cpf"):
        render("{{ inputs.cpf }}", {Scope.INPUTS: {}})


def test_render_values() -> None:
    context = {Scope.INPUTS: {"n": 3, "ok": True}, Scope.VARS: {"x": "abc"}}
    assert render("{{inputs.n}}-{{inputs.ok}}-{{vars.x}}", context) == "3-true-abc"


@pytest.mark.parametrize(
    "template",
    [
        "{{ inputs.cpf.__class__ }}",
        "{{ inputs['cpf'] }}",
        "{{ os.system }}",
        "{{ inputs.cpf | upper }}",
        "{{ inputs.cpf",
    ],
)
def test_rejects_anything_but_plain_refs(template: str) -> None:
    with pytest.raises(TemplateError):
        parse(template)


def test_escape_round_trip() -> None:
    text = "valor com {{ chaves }} literais"
    assert render(escape(text), {}) == text
    assert references(escape(text)) == []


def test_render_rejects_non_scalar() -> None:
    with pytest.raises(TemplateError, match="não é um valor simples"):
        render("{{ vars.tabela }}", {Scope.VARS: {"tabela": [1, 2]}})


# -- números e codificação por valor --------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.0, "5"),
        (-0.0, "0"),
        (12345678901234567.0, "12345678901234568"),
        (0.00001, "0.00001"),
        (1.5, "1.5"),
        (-2.25, "-2.25"),
        (0.1 + 0.2, "0.30000000000000004"),
        (Decimal("1E+2"), "100"),
        (Decimal("10.50"), "10.50"),
    ],
)
def test_render_numbers_without_exponent(value: object, expected: str) -> None:
    assert render("{{ inputs.v }}", {Scope.INPUTS: {"v": value}}) == expected


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf, 1e18, -1e20, 1e-19, Decimal("NaN"), Decimal("1E+30")],
)
def test_render_refuses_numbers_that_cannot_be_typed(value: object) -> None:
    with pytest.raises(TemplateError, match=r"inputs\.v"):
        render("{{ inputs.v }}", {Scope.INPUTS: {"v": value}})


def test_format_value_matches_render() -> None:
    assert format_value(True) == "true"
    assert format_value(7) == "7"
    assert format_value(7.0) == "7"
    with pytest.raises(TemplateError, match="não é um valor simples"):
        format_value({"a": 1})


def test_render_encodes_only_the_values() -> None:
    context = {Scope.INPUTS: {"n": "12/3"}}
    rendered = render("/notas/{{ inputs.n }}/ver?{{{{", context, encode=lambda text: f"<{text}>")
    assert rendered == "/notas/<12/3>/ver?{{"


def test_render_encode_errors_name_the_reference() -> None:
    def refuse(text: str) -> str:
        raise TemplateError("valor recusado")

    with pytest.raises(TemplateError, match=r"inputs\.n: valor recusado"):
        render("{{ inputs.n }}", {Scope.INPUTS: {"n": ".."}}, encode=refuse)
