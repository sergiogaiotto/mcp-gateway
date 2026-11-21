"""Revisão da gravação: repetições, propostas (regras e modelo) e escolhas confirmadas."""

from typing import Any
from uuid import uuid4

import pytest

from rapa.compiler import generalize_inputs
from rapa.compiler.lowering import CompileResult
from rapa.compiler.repetition import apply_repetition, find_repetitions
from rapa.compiler.review import apply_choices, build_proposals
from rapa.core import policy
from rapa.core.contracts import (
    DraftChoices,
    EffectChoice,
    InvalidInput,
    ParameterChoice,
    ProposalSource,
    RepetitionChoice,
    RepetitionFieldChoice,
    StepNameChoice,
)
from rapa.core.edits import (
    AddTextPostcondition,
    Parametrize,
    RaiseEffect,
    RenameStep,
    SetIntent,
    SetRobotInfo,
)
from rapa.core.robot import Effect, FieldType, ForEachStep, Manifest

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


def _target(label: str) -> dict[str, Any]:
    return {"intent": f"campo {label}", "locators": [{"by": "label", "value": label}]}


def _fill(step_id: str, label: str, value: str) -> dict[str, Any]:
    return {
        "kind": "fill",
        "id": step_id,
        "title": f"Digitar {label}",
        "target": _target(label),
        "value": {"source": "text", "template": value},
    }


def _cadastro(people: list[tuple[str, str]]) -> Manifest:
    steps: list[dict[str, Any]] = [
        {"kind": "navigate", "id": "abrir", "title": "Abrir o cadastro", "url": f"{BASE}/cadastro"}
    ]
    for n, (nome, cpf) in enumerate(people, start=1):
        suffix = "" if n == 1 else f"_{n}"
        steps += [
            _fill(f"digitar_nome{suffix}", "Nome", nome),
            _fill(f"digitar_cpf{suffix}", "CPF", cpf),
            {
                "kind": "select",
                "id": f"escolher_uf{suffix}",
                "title": "Escolher a UF",
                "target": {"intent": "lista UF", "locators": [{"by": "label", "value": "UF"}]},
                "option": "SP",
            },
            {
                "kind": "click",
                "id": f"clicar_salvar{suffix}",
                "title": "Clicar em Salvar",
                "effect": "write",
                "target": {
                    "intent": "botão Salvar",
                    "locators": [{"by": "role", "value": "button", "name": "Salvar"}],
                },
            },
        ]
    return Manifest.model_validate(
        {
            "name": "cadastrar_pessoas",
            "title": "Robô gravado",
            "description": "Robô gravado na demonstração.",
            "allowed_domains": [HOST],
            "steps": steps,
        }
    )


def _compiled(manifest: Manifest, texts: set[str] | None = None) -> CompileResult:
    return CompileResult(
        manifest=manifest,
        provenance={},
        parameter_candidates=generalize_inputs(manifest),
        observed_texts=texts or {"Cadastro salvo com sucesso"},
        warnings=["aviso do compilador"],
    )


PEOPLE = [("Maria", "529.982.247-25"), ("João", "111.444.777-35"), ("Zé", "390.533.447-05")]


def test_finds_the_repeated_block_with_the_values_that_change() -> None:
    manifest = _cadastro(PEOPLE)
    [candidate] = find_repetitions(manifest)
    assert candidate.start == 1 and candidate.period == 4 and candidate.times == 3
    assert candidate.body_step_ids == [
        "digitar_nome",
        "digitar_cpf",
        "escolher_uf",
        "clicar_salvar",
    ]
    assert len(candidate.step_ids) == 12
    # A UF é a mesma nas três voltas: fica fixa.
    assert [(s.step_id, s.values) for s in candidate.slots] == [
        ("digitar_nome", ["Maria", "João", "Zé"]),
        ("digitar_cpf", [p[1] for p in PEOPLE]),
    ]
    assert [s.suggested_type for s in candidate.slots] == ["string", "cpf"]


def test_no_repetition_without_changing_values_or_with_a_single_pass() -> None:
    assert find_repetitions(_cadastro(PEOPLE[:1])) == []
    assert find_repetitions(_cadastro([PEOPLE[0], PEOPLE[0]])) == []
    data = _cadastro(PEOPLE[:2]).model_dump(mode="json")
    data["steps"][5]["target"]["locators"][0]["value"] = "Outro campo"
    assert find_repetitions(Manifest.model_validate(data)) == []


def test_apply_repetition_builds_a_loop_over_a_list_input() -> None:
    manifest = _cadastro(PEOPLE)
    [candidate] = find_repetitions(manifest)
    looped = apply_repetition(
        manifest,
        candidate,
        list_name="pessoas",
        list_title="Pessoas",
        fields={
            "digitar_nome": ("nome", "Nome", "string"),
            "digitar_cpf": ("cpf", "CPF", "cpf"),
        },
    )
    assert policy.check(looped) == []
    assert looped.steps[0].id == "abrir"
    loop = looped.steps[1]
    assert isinstance(loop, ForEachStep) and loop.items == "inputs.pessoas"
    assert loop.effect is Effect.READ  # cada passo de dentro se anuncia com o próprio efeito
    assert [s.id for s in loop.steps] == candidate.body_step_ids
    data = loop.model_dump(mode="json")
    assert data["steps"][0]["value"]["template"] == "{{ item.nome }}"
    assert data["steps"][2]["option"] == "SP"
    [lista] = looped.inputs
    assert lista.type is FieldType.LIST
    assert lista.item_fields is not None
    assert [(f.name, f.type) for f in lista.item_fields] == [
        ("nome", FieldType.STRING),
        ("cpf", FieldType.CPF),
    ]
    with pytest.raises(ValueError, match="falta o campo"):
        apply_repetition(
            manifest,
            candidate,
            list_name="pessoas",
            list_title="Pessoas",
            fields={"digitar_nome": ("nome", "Nome", "string")},
        )
    with pytest.raises(ValueError, match="não existe mais"):
        apply_repetition(looped, candidate, list_name="x", list_title="X", fields={})


def test_proposals_without_the_model_keep_the_rule_suggestions() -> None:
    compiled = _compiled(_cadastro(PEOPLE))
    proposals = build_proposals(compiled, session_id=uuid4(), recording_id=uuid4(), model_ops=None)
    assert not proposals.model_available
    assert proposals.model_message is not None and "indisponíveis" in proposals.model_message
    assert [p.step_id for p in proposals.parameters][:2] == ["digitar_nome", "digitar_cpf"]
    assert all(p.source is ProposalSource.RULES for p in proposals.parameters)
    assert proposals.step_names == [] and proposals.effects == [] and proposals.checks == []
    assert len(proposals.repetitions) == 1
    assert proposals.warnings == ["aviso do compilador"]
    assert proposals.steps[0].number == "1" and proposals.steps[0].step_id == "abrir"


def test_model_proposals_are_checked_by_the_code() -> None:
    compiled = _compiled(_cadastro(PEOPLE[:1]))
    ops = [
        Parametrize(step_id="digitar_nome", input_name="nome_pessoa", title="Nome", type="string"),
        RenameStep(step_id="clicar_salvar", title="Salvar o cadastro"),
        RenameStep(step_id="nao_existe", title="Fantasma"),
        RaiseEffect(step_id="clicar_salvar", effect="irreversible"),
        RaiseEffect(step_id="abrir", effect="write"),
        AddTextPostcondition(text="Cadastro salvo com sucesso"),
        AddTextPostcondition(text="Texto inventado pelo modelo"),
        SetRobotInfo(title="Cadastrar pessoa", description="Cadastra uma pessoa no portal."),
        SetIntent(step_id="clicar_salvar", intent="botão Salvar do cadastro"),
    ]
    proposals = build_proposals(compiled, session_id=uuid4(), recording_id=uuid4(), model_ops=ops)
    assert proposals.model_available and proposals.model_message is None
    nome = next(p for p in proposals.parameters if p.step_id == "digitar_nome")
    assert (nome.name, nome.source, nome.value_preview) == (
        "nome_pessoa",
        ProposalSource.MODEL,
        "Maria",
    )
    assert [(p.step_id, p.title) for p in proposals.step_names] == [
        ("clicar_salvar", "Salvar o cadastro")
    ]
    assert {(p.step_id, p.proposed) for p in proposals.effects} == {
        ("clicar_salvar", "irreversible"),
        ("abrir", "write"),
    }
    assert [c.text for c in proposals.checks] == ["Cadastro salvo com sucesso"]
    assert proposals.title == "Cadastrar pessoa"
    assert len(proposals.model_rejected) == 2
    assert any("nao_existe" in reason for reason in proposals.model_rejected)
    assert any("não apareceu" in reason for reason in proposals.model_rejected)


def test_apply_choices_applies_only_what_was_confirmed() -> None:
    compiled = _compiled(_cadastro(PEOPLE))
    choices = DraftChoices(
        name="cadastrar_pessoas",
        title="Cadastrar pessoas",
        description="Cadastra uma lista de pessoas.",
        repetitions=[
            RepetitionChoice(
                proposal_id="repeticao_digitar_nome",
                list_name="pessoas",
                list_title="Pessoas",
                fields=[
                    RepetitionFieldChoice(step_id="digitar_nome", name="nome", title="Nome"),
                    RepetitionFieldChoice(
                        step_id="digitar_cpf", name="cpf", title="CPF", type="cpf"
                    ),
                ],
            )
        ],
        step_names=[StepNameChoice(step_id="clicar_salvar", title="Salvar o cadastro")],
        effects=[EffectChoice(step_id="clicar_salvar", effect="irreversible")],
        checks=["Cadastro salvo com sucesso"],
    )
    manifest = apply_choices(compiled, choices)
    assert policy.check(manifest) == []
    assert manifest.title == "Cadastrar pessoas"
    salvar = manifest.step_by_id("clicar_salvar")
    assert salvar is not None and salvar.title == "Salvar o cadastro"
    assert salvar.effect is Effect.IRREVERSIBLE
    assert manifest.step_by_id("clicar_salvar_2") is None
    assert len(manifest.postconditions) == 1
    assert [f.name for f in manifest.inputs] == ["pessoas"]


def test_choices_the_code_refuses_are_reported_not_dropped() -> None:
    compiled = _compiled(_cadastro(PEOPLE))
    choices = DraftChoices(
        name="cadastrar_pessoas",
        title="Cadastrar pessoas",
        description="Cadastra.",
        repetitions=[
            RepetitionChoice(
                proposal_id="repeticao_digitar_nome",
                list_name="pessoas",
                list_title="Pessoas",
                fields=[
                    RepetitionFieldChoice(step_id="digitar_nome", name="nome", title="Nome"),
                    RepetitionFieldChoice(step_id="digitar_cpf", name="cpf", title="CPF"),
                ],
            ),
            RepetitionChoice(
                proposal_id="repeticao_inventada",
                list_name="outra",
                list_title="Outra",
                fields=[RepetitionFieldChoice(step_id="x", name="x", title="X")],
            ),
        ],
        # O passo da segunda volta saiu do robô com a repetição; o nome já é item.
        parameters=[ParameterChoice(step_id="digitar_nome", name="nome", title="Nome")],
        step_names=[StepNameChoice(step_id="digitar_nome_2", title="Não existe mais")],
        checks=["Texto que ninguém viu"],
    )
    with pytest.raises(InvalidInput) as caught:
        apply_choices(compiled, choices)
    reasons = " | ".join(caught.value.errors)
    assert "repeticao_inventada" in reasons
    assert "digitar_nome_2" in reasons
    assert "já usa uma entrada" in reasons
    assert "não apareceu" in reasons
