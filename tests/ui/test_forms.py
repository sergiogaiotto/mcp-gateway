"""Formulários gerados do esquema: leitura, conversão, conferência e campos no NiceGUI."""

from typing import Any

import pytest
from fastapi import FastAPI
from nicegui import ui

from rapa.core.robot import FieldType, Manifest
from rapa.fixtures.robots import emitir_protocolo
from rapa.ui.forms import (
    FormValidationError,
    SchemaForm,
    cnpj_valido,
    convert_values,
    cpf_valido,
    field_error,
    fields_from_schema,
    split_server_errors,
)
from tests.ui.conftest import BASE, HOST, simulate

pytestmark = pytest.mark.usefixtures("no_error_logs")


def _manifest() -> Manifest:
    """Um robô com uma entrada de cada tipo."""
    return Manifest.model_validate(
        {
            "name": "todos_os_tipos",
            "title": "Todos os tipos",
            "description": "Robô de teste com uma entrada de cada tipo.",
            "allowed_domains": [HOST],
            "inputs": [
                {"name": "nome", "title": "Nome", "type": "string"},
                {"name": "quantidade", "title": "Quantidade", "type": "integer"},
                {"name": "taxa", "title": "Taxa", "type": "number", "required": False},
                {"name": "urgente", "title": "Urgente", "type": "boolean"},
                {"name": "vencimento", "title": "Vencimento", "type": "date"},
                {"name": "cpf", "title": "CPF", "type": "cpf"},
                {"name": "cnpj", "title": "CNPJ", "type": "cnpj", "required": False},
                {"name": "valor", "title": "Valor", "type": "money"},
                {"name": "anexo", "title": "Anexo", "type": "file", "required": False},
                {
                    "name": "itens",
                    "title": "Itens",
                    "type": "list",
                    "item_fields": [
                        {"name": "codigo", "title": "Código", "type": "string"},
                        {"name": "preco", "title": "Preço", "type": "money"},
                        {"name": "qtd", "title": "Qtd", "type": "integer", "required": False},
                    ],
                },
            ],
            "steps": [{"kind": "navigate", "id": "abrir", "title": "Abrir", "url": f"{BASE}/"}],
        }
    )


SCHEMA = _manifest().input_json_schema()

VALID: dict[str, Any] = {
    "nome": "  Ana Souza ",
    "quantidade": 3.0,
    "taxa": "1,5",
    "urgente": True,
    "vencimento": "31/12/2026",
    "cpf": "52998224725",
    "cnpj": "",
    "valor": "R$ 1234,5",
    "anexo": "",
    "itens": [
        {"codigo": "A1", "preco": "10", "qtd": 2.0},
        {"codigo": "", "preco": "", "qtd": None},
        {"codigo": "B2", "preco": "1.000,00", "qtd": None},
    ],
}

EXPECTED: dict[str, Any] = {
    "nome": "Ana Souza",
    "quantidade": 3,
    "taxa": 1.5,
    "urgente": True,
    "vencimento": "2026-12-31",
    "cpf": "529.982.247-25",
    "valor": "1.234,50",
    "itens": [
        {"codigo": "A1", "preco": "10,00", "qtd": 2},
        {"codigo": "B2", "preco": "1.000,00"},
    ],
}


def test_fields_from_schema_types() -> None:
    fields = fields_from_schema(SCHEMA)
    assert [(f.name, f.type, f.required) for f in fields] == [
        ("nome", FieldType.STRING, True),
        ("quantidade", FieldType.INTEGER, True),
        ("taxa", FieldType.NUMBER, False),
        ("urgente", FieldType.BOOLEAN, True),
        ("vencimento", FieldType.DATE, True),
        ("cpf", FieldType.CPF, True),
        ("cnpj", FieldType.CNPJ, False),
        ("valor", FieldType.MONEY, True),
        ("anexo", FieldType.FILE, False),
        ("itens", FieldType.LIST, True),
    ]
    itens = fields[-1]
    assert [(f.name, f.type, f.required) for f in itens.item_fields] == [
        ("codigo", FieldType.STRING, True),
        ("preco", FieldType.MONEY, True),
        ("qtd", FieldType.INTEGER, False),
    ]
    assert itens.max_items == 10_000
    assert fields[0].max_length == 4000


def test_convert_values_matches_schema() -> None:
    converted = convert_values(fields_from_schema(SCHEMA), VALID)
    assert converted == EXPECTED
    jsonschema_rs = pytest.importorskip("jsonschema_rs")
    validator = jsonschema_rs.Draft202012Validator(SCHEMA, validate_formats=True)
    assert validator.is_valid(converted)


def test_convert_values_reports_each_field_in_portuguese() -> None:
    raw = {
        **VALID,
        "nome": "",
        "quantidade": 2.5,
        "vencimento": "31/02/2026",
        "cpf": "529.982.247-00",
        "cnpj": "11.222.333/0001-00",
        "valor": "1,234",
        "itens": [{"codigo": "X", "preco": "abc", "qtd": None}],
    }
    with pytest.raises(FormValidationError) as caught:
        convert_values(fields_from_schema(SCHEMA), raw)
    assert caught.value.errors == {
        "nome": "Campo obrigatório.",
        "quantidade": "Informe um número inteiro.",
        "vencimento": "Data inválida; use o formato dd/mm/aaaa.",
        "cpf": "CPF inválido: confira os dígitos.",
        "cnpj": "CNPJ inválido: confira os dígitos verificadores.",
        "valor": "Valor inválido; use o formato 1.234,56.",
        "itens[1].preco": "Linha 1, “Preço”: Valor inválido; use o formato 1.234,56.",
    }


def test_required_list_and_boolean_defaults() -> None:
    fields = fields_from_schema(SCHEMA)
    raw = {**VALID, "urgente": None, "itens": [{"codigo": "", "preco": "", "qtd": None}]}
    with pytest.raises(FormValidationError) as caught:
        convert_values(fields, raw)
    assert caught.value.errors == {"itens": "Preencha ao menos uma linha."}
    raw["itens"] = VALID["itens"]
    assert convert_values(fields, raw)["urgente"] is False


def test_documents_and_money() -> None:
    assert cpf_valido("529.982.247-25")
    assert not cpf_valido("111.111.111-11")
    assert cnpj_valido("11.222.333/0001-81")
    # Exemplo de CNPJ alfanumérico divulgado pela Receita Federal.
    assert cnpj_valido("12.ABC.345/01DE-35")
    assert not cnpj_valido("12.ABC.345/01DE-36")
    fields = {f.name: f for f in fields_from_schema(SCHEMA)}
    assert convert_values([fields["cnpj"]], {"cnpj": "12abc34501de35"}) == {
        "cnpj": "12.ABC.345/01DE-35"
    }
    money = fields["valor"]
    for raw, expected in (
        ("10", "10,00"),
        ("-3,5", "-3,50"),
        ("1.234.567,89", "1.234.567,89"),
        (12.5, "12,50"),
    ):
        assert convert_values([money], {"valor": raw}) == {"valor": expected}
    assert field_error(money, "1,999") == "Valor inválido; use o formato 1.234,56."
    assert field_error(money, 1.234) == "Use no máximo dois dígitos de centavos."
    assert field_error(fields["vencimento"], "2026-02-28") is None
    quantidade, taxa = fields["quantidade"], fields["taxa"]
    assert convert_values([quantidade], {"quantidade": "1.000"}) == {"quantidade": 1000}
    assert field_error(quantidade, "1.5") == "Informe um número inteiro."
    assert convert_values([taxa], {"taxa": "1.000"}) == {"taxa": 1000}
    assert convert_values([taxa], {"taxa": "1.000,25"}) == {"taxa": 1000.25}
    assert convert_values([taxa], {"taxa": "2.5"}) == {"taxa": 2.5}
    assert field_error(taxa, "1e5") == "Informe um número."
    assert field_error(taxa, "nan") == "Informe um número."
    assert field_error(fields["nome"], "x" * 4001) == "Use no máximo 4000 caracteres."


def test_huge_money_values_get_the_field_message() -> None:
    fields = {f.name: f for f in fields_from_schema(SCHEMA)}
    money = fields["valor"]
    # O maior valor que cabe no esquema (20 caracteres antes da vírgula, com os pontos).
    assert convert_values([money], {"valor": "9" * 15 + ",99"}) == {
        "valor": "999.999.999.999.999,99"
    }
    # Com 27 dígitos ou mais, o arredondamento para centavos passava da precisão do
    # Decimal e escapava como decimal.InvalidOperation em vez da mensagem do campo.
    for raw in ("1" * 16, "1" * 27, "1" * 60, "-" + "9" * 40 + ",99", 1e30, -1e300, 10**40):
        assert field_error(money, raw) == "Valor grande demais.", raw
        with pytest.raises(FormValidationError) as caught:
            convert_values([money], {"valor": raw})
        assert caught.value.errors == {"valor": "Valor grande demais."}
    # O mesmo dentro de uma lista (coluna de valor em reais).
    itens = fields["itens"]
    row = {**VALID["itens"][0], "preco": "1" * 30}
    with pytest.raises(FormValidationError) as caught:
        convert_values([itens], {"itens": [row]})
    assert "Valor grande demais." in " ".join(caught.value.errors.values())


def test_whole_numbers_are_sent_as_int() -> None:
    fields = {f.name: f for f in fields_from_schema(SCHEMA)}
    quantidade, taxa, itens = fields["quantidade"], fields["taxa"], fields["itens"]
    # O campo numérico do navegador sempre entrega float (5.0); no JSON vai 5.
    for spec in (quantidade, taxa):
        converted = convert_values([spec], {spec.name: 5.0})[spec.name]
        assert converted == 5 and type(converted) is int, spec.name
    assert type(convert_values([taxa], {"taxa": "7"})["taxa"]) is int
    assert convert_values([taxa], {"taxa": 2.5}) == {"taxa": 2.5}
    row = {**VALID["itens"][0], "qtd": 4.0}
    (item,) = convert_values([itens], {"itens": [row]})["itens"]
    assert item["qtd"] == 4 and type(item["qtd"]) is int
    # Um texto só com dígitos não passa por float, que perderia o último dígito.
    assert convert_values([taxa], {"taxa": "12345678901234567"}) == {"taxa": 12345678901234567}
    # Acima de 2**53 o float do navegador já trocou dígitos (12345678901234567 chega
    # como 12345678901234568): melhor recusar que mandar outro número.
    assert field_error(quantidade, 12345678901234567.0) == "Número grande demais."
    assert convert_values([quantidade], {"quantidade": float(2**53)}) == {"quantidade": 2**53}
    assert field_error(quantidade, 10**18) == "Número grande demais."
    assert field_error(quantidade, "1" * 19) == "Número grande demais."
    assert field_error(taxa, "1" * 19) == "Número grande demais."
    # Número que o executor não conseguiria digitar: recusado no campo, não na execução.
    for raw in (1e30, "1" * 30 + ",5", 1e-30, "0,0000000000000000000001"):
        assert field_error(taxa, raw) == "Número grande ou pequeno demais.", raw


def test_split_server_errors() -> None:
    fields = fields_from_schema(SCHEMA)
    by_field, general = split_server_errors(
        fields,
        [
            "cpf: valor fora do formato esperado",
            "campo obrigatório ausente: vencimento",
            "itens[0].preco: valor fora do formato esperado",
            "entrada obrigatória ausente: nome",
            "campo não declarado: extra",
            "entradas inválidas",
        ],
    )
    assert set(by_field) == {"cpf", "vencimento", "itens", "nome"}
    assert general == ["campo não declarado: extra", "entradas inválidas"]


async def test_schema_form_in_nicegui() -> None:
    results: list[dict[str, Any] | None] = []
    protocolo = emitir_protocolo(BASE, HOST).input_json_schema()

    def build(app: FastAPI) -> None:
        @ui.page("/formulario")
        def page() -> None:
            form = SchemaForm(SCHEMA)
            form.build()
            ui.button("Enviar", on_click=lambda: results.append(form.submit())).mark("enviar")
            ui.button(
                "Erros do servidor",
                on_click=lambda: form.show_server_errors(["cpf: recusado", "erro geral"]),
            ).mark("erros-servidor")

        @ui.page("/vazio")
        def empty() -> None:
            SchemaForm({"type": "object", "properties": {}}).build()

        @ui.page("/protocolo")
        def protocol() -> None:
            SchemaForm(protocolo).build()

        ui.run_with(app, show_welcome_message=False)

    async with simulate(build) as user:
        await user.open("/formulario")
        cpf = user.find(marker="campo-cpf")
        assert [e.props.get("mask") for e in cpf.elements] == ["###.###.###-##"]
        user.find(marker="campo-nome").type("Ana")
        user.find(marker="campo-quantidade").type("2")
        user.find(marker="campo-urgente").click()
        user.find(marker="campo-vencimento").type("01/03/2027")
        cpf.type("529.982.247-25")
        user.find(marker="campo-valor").type("99,90")
        user.find(marker="campo-itens-0-codigo").type("A1")
        user.find(marker="campo-itens-0-preco").type("5")
        user.find(marker="campo-itens-adicionar").click()
        user.find(marker="campo-itens-1-codigo").type("B2")
        user.find(marker="campo-itens-1-preco").type("abc")
        user.find(marker="enviar").click()
        assert results == [None]
        (preco,) = user.find(marker="campo-itens-1-preco").elements
        assert getattr(preco, "error", None) == "Valor inválido; use o formato 1.234,56."

        user.find(marker="campo-itens-1-remover").click()
        user.find(marker="enviar").click()
        assert results[-1] == {
            "nome": "Ana",
            "quantidade": 2,
            "urgente": True,
            "vencimento": "2027-03-01",
            "cpf": "529.982.247-25",
            "valor": "99,90",
            "itens": [{"codigo": "A1", "preco": "5,00"}],
        }

        user.find(marker="erros-servidor").click()
        (cpf_element,) = cpf.elements
        assert getattr(cpf_element, "error", None) == "cpf: recusado"
        await user.should_see("erro geral")

        await user.open("/vazio")
        await user.should_see("Este robô não tem entradas.")
        await user.open("/protocolo")
        await user.should_see(marker="campo-uf")
