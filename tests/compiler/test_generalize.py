"""Candidatos a parâmetro e nomes em pt-BR."""

import pytest

from rapa.compiler import ParameterCandidate, generalize_inputs, suggest_type
from rapa.compiler.naming import IdAllocator, article, lower_first, page_phrase, slugify
from rapa.core import policy
from rapa.core.edits import apply_edits
from rapa.core.robot import FieldType, Manifest
from rapa.fixtures.robots import emitir_protocolo

BASE = "https://portal.exemplo.gov.br"
HOST = "portal.exemplo.gov.br"


def literal_manifest() -> Manifest:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["inputs"] = []
    literals = {"digitar_nome": "Maria da Silva", "digitar_cpf": "529.982.247-25"}
    for step in data["steps"]:
        if step["id"] in literals:
            step["value"]["template"] = literals[step["id"]]
        if step["id"] == "escolher_uf":
            step["option"] = "SP"
    return Manifest.model_validate(data)


def test_lists_fill_and_select_literals_only() -> None:
    candidates = generalize_inputs(literal_manifest())
    assert [(c.step_id, c.step_kind, c.value_preview) for c in candidates] == [
        ("digitar_nome", "fill", "Maria da Silva"),
        ("digitar_cpf", "fill", "529.982.247-25"),
        ("escolher_uf", "select", "SP"),
    ]
    nome, cpf, uf = candidates
    assert nome.field_label == "Nome completo"
    assert nome.suggested_name == "nome_completo"
    assert cpf.suggested_type == "cpf"
    assert uf.suggested_type == "string"


def test_secrets_and_templates_are_not_candidates() -> None:
    candidates = generalize_inputs(emitir_protocolo(BASE, HOST))
    assert candidates == []


def test_candidates_apply_as_parametrize() -> None:
    manifest = literal_manifest()
    candidates = generalize_inputs(manifest)
    edits = [c.to_edit() for c in candidates]
    result = apply_edits(manifest, edits)
    assert result.rejected == []
    assert {f.name: f.type for f in result.manifest.inputs} == {
        "nome_completo": FieldType.STRING,
        "cpf": FieldType.CPF,
        "uf": FieldType.STRING,
    }
    assert policy.check(result.manifest) == []
    assert generalize_inputs(result.manifest) == []


def test_candidate_is_plain_data() -> None:
    candidate = generalize_inputs(literal_manifest())[0]
    again = ParameterCandidate.model_validate_json(candidate.model_dump_json())
    assert again == candidate
    edit = candidate.to_edit("nome", "Nome do titular")
    assert (edit.input_name, edit.title) == ("nome", "Nome do titular")


def test_suggested_names_do_not_clash_with_inputs() -> None:
    data = literal_manifest().model_dump(mode="json")
    data["inputs"] = [{"name": "cpf", "title": "CPF", "type": "cpf"}]
    candidates = generalize_inputs(Manifest.model_validate(data))
    assert [c.suggested_name for c in candidates] == ["nome_completo", "cpf_2", "uf"]


@pytest.mark.parametrize(
    ("value", "label", "expected"),
    [
        ("529.982.247-25", None, "cpf"),
        ("52998224725", "CPF do titular", "cpf"),
        ("52998224725", "Telefone", "string"),
        ("12.ABC.345/01DE-35", None, "cnpj"),
        ("2026-11-18", None, "date"),
        ("18/11/2026", None, "string"),
        ("1.234,56", None, "money"),
        ("001003", None, "string"),
        ("Maria", None, "string"),
    ],
)
def test_suggest_type(value: str, label: str | None, expected: str) -> None:
    assert suggest_type(value, label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("CPF", "o CPF"),
        ("UF", "a UF"),
        ("Senha", "a senha"),
        ("Usuário", "o usuário"),
        ("Nome completo", "o nome completo"),
        ("Data de nascimento", "a data de nascimento"),
        ("Inscrição estadual", "a inscrição estadual"),
        ("Código de verificação", "o código de verificação"),
    ],
)
def test_article_and_lower_first(label: str, expected: str) -> None:
    assert f"{article(label)} {lower_first(label)}" == expected


def test_slugify_and_ids() -> None:
    assert slugify("Código de verificação") == "codigo_de_verificacao"
    assert slugify("  ** ") == ""
    allocator = IdAllocator()
    assert allocator.allocate("clicar_Emitir") == "clicar_emitir"
    assert allocator.allocate("clicar_Emitir") == "clicar_emitir_2"
    assert allocator.allocate("123") == "passo_123"
    assert allocator.allocate("") == "passo"


@pytest.mark.parametrize(
    ("url", "phrase", "slug"),
    [
        ("https://x.gov.br/", ("a página inicial"), "inicio"),
        ("https://x.gov.br/login", "a página de login", "login"),
        ("https://x.gov.br/notas/001003", "a página de notas", "notas"),
        ("https://x.gov.br/area-restrita/index", "a página inicial", "inicio"),
        ("https://x.gov.br/123/456", "a página", "pagina"),
    ],
)
def test_page_phrase(url: str, phrase: str, slug: str) -> None:
    assert page_phrase(url) == (phrase, slug)
