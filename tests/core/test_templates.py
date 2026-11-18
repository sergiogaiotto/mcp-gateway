import pytest

from rapa.core.templates import Ref, Scope, TemplateError, escape, parse, references, render


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
