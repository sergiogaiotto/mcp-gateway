from rapa.core.edits import (
    AddTextPostcondition,
    Parametrize,
    RaiseEffect,
    RenameStep,
    SetRobotInfo,
    apply_edits,
    edit_ops_adapter,
)
from rapa.core.robot import Effect, FillStep, Manifest, TextValue
from rapa.fixtures.robots import emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


def literal_manifest() -> Manifest:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["inputs"] = [f for f in data["inputs"] if f["name"] != "nome"]
    for step in data["steps"]:
        if step["id"] == "digitar_nome":
            step["value"]["template"] = "Maria da Silva"
    return Manifest.model_validate(data)


def test_parametrize_literal() -> None:
    result = apply_edits(
        literal_manifest(),
        [Parametrize(step_id="digitar_nome", input_name="nome", title="Nome completo")],
    )
    assert result.rejected == []
    step = result.manifest.step_by_id("digitar_nome")
    assert isinstance(step, FillStep)
    assert isinstance(step.value, TextValue)
    assert step.value.template == "{{ inputs.nome }}"
    assert [f.name for f in result.manifest.inputs][-1] == "nome"


def test_cannot_parametrize_secret_or_existing_template() -> None:
    result = apply_edits(
        literal_manifest(),
        [
            Parametrize(step_id="digitar_senha", input_name="senha", title="Senha"),
            Parametrize(step_id="digitar_cpf", input_name="cpf2", title="CPF"),
        ],
    )
    assert result.applied == []
    reasons = [reason for _, reason in result.rejected]
    assert "não tem valor digitado" in reasons[0]
    assert "já usa uma entrada" in reasons[1]


def test_effect_only_goes_up() -> None:
    manifest = emitir_protocolo(BASE, HOST)
    result = apply_edits(manifest, [RaiseEffect(step_id="entrar", effect="write")])
    step = result.manifest.step_by_id("entrar")
    assert step is not None and step.effect is Effect.WRITE
    again = apply_edits(result.manifest, [RaiseEffect(step_id="emitir", effect="write")])
    emitir = again.manifest.step_by_id("emitir")
    assert emitir is not None and emitir.effect is Effect.IRREVERSIBLE


def test_postcondition_needs_observed_text() -> None:
    manifest = emitir_protocolo(BASE, HOST)
    result = apply_edits(
        manifest,
        [
            AddTextPostcondition(text="Protocolo emitido"),
            AddTextPostcondition(text="Pagamento aprovado"),
        ],
        observed_texts={"Protocolo nº PRT-2026-000001 emitido. Protocolo emitido"},
    )
    assert len(result.applied) == 1
    assert "não apareceu" in result.rejected[0][1]


def test_invalid_result_is_rejected_and_previous_kept() -> None:
    manifest = emitir_protocolo(BASE, HOST)
    result = apply_edits(
        manifest,
        [
            RenameStep(step_id="nao_existe", title="X"),
            SetRobotInfo(title="Emitir protocolo no portal"),
        ],
    )
    assert result.manifest.title == "Emitir protocolo no portal"
    assert len(result.rejected) == 1


def test_adapter_parses_model_output() -> None:
    ops = edit_ops_adapter.validate_json(
        '[{"op":"rename_step","step_id":"emitir","title":"Emitir o protocolo"},'
        '{"op":"raise_effect","step_id":"entrar","effect":"write"}]'
    )
    assert [op.op for op in ops] == ["rename_step", "raise_effect"]
