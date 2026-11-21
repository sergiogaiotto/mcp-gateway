"""Revisão da gravação compilada: propostas para a pessoa e o robô com o que ela confirmou.

:func:`build_proposals` junta as sugestões determinísticas (valores que podem virar
entradas, de :func:`rapa.compiler.generalize_inputs`, e repetições, de
:func:`rapa.compiler.repetition.find_repetitions`) com as operações que o modelo de
linguagem propôs (:func:`rapa.llm.generalize.propose_edits`). Cada operação do
modelo é conferida pelo código (:func:`rapa.core.edits.apply_edits`, uma a uma,
sobre o robô compilado); as recusadas aparecem com o motivo e nunca viram proposta.
Nenhuma proposta entra no robô sozinha: o título e a descrição do formulário são os
da compilação, e os do modelo aparecem à parte (``robot_info``).

:func:`apply_choices` aplica só o que a pessoa confirmou: primeiro as repetições
(que mudam a estrutura), depois as entradas, os nomes dos passos, os efeitos (que
só sobem) e as conferências de texto, sempre pelas operações fechadas de
:mod:`rapa.core.edits`. Uma escolha que o código não consegue aplicar vira erro
(``InvalidInput``) com o motivo: nada confirmado some em silêncio.
"""

from collections.abc import Sequence
from uuid import UUID

from rapa.compiler.lowering import CompileResult
from rapa.compiler.naming import clip_label
from rapa.compiler.repetition import RepetitionCandidate, apply_repetition, find_repetitions
from rapa.core.contracts import (
    CheckProposal,
    DraftChoices,
    EffectProposal,
    GeneralizationProposals,
    InvalidInput,
    ParameterProposal,
    ProposalSource,
    RepetitionChoice,
    RepetitionField,
    RepetitionProposal,
    RobotInfoProposal,
    StepNameProposal,
    StepOverview,
)
from rapa.core.edits import (
    AddTextPostcondition,
    EditOp,
    Parametrize,
    RaiseEffect,
    RenameStep,
    SetIntent,
    SetRobotInfo,
    apply_edits,
)
from rapa.core.robot import Effect, Manifest, Step, child_blocks, max_effect

__all__ = ["apply_choices", "build_proposals", "describe_op", "overview"]

_MAX_REJECTED = 50


def overview(manifest: Manifest) -> list[StepOverview]:
    """Os passos numerados (1, 2, 2.1...), com profundidade, para a revisão."""
    out: list[StepOverview] = []

    def walk(steps: Sequence[Step], prefix: str, depth: int) -> None:
        for n, step in enumerate(steps, start=1):
            number = f"{prefix}{n}"
            out.append(
                StepOverview(
                    step_id=step.id,
                    number=number,
                    title=step.title,
                    kind=step.kind,
                    effect=step.effect,
                    depth=depth,
                )
            )
            for block in child_blocks(step):
                walk(block, f"{number}.", depth + 1)

    walk(manifest.steps, "", 0)
    return out


def describe_op(op: EditOp) -> str:
    """Resumo da operação em pt-BR, sem valores digitados."""
    match op:
        case Parametrize():
            return f"transformar o valor do passo '{op.step_id}' na entrada '{op.input_name}'"
        case RenameStep():
            return f"renomear o passo '{op.step_id}'"
        case RaiseEffect():
            return f"marcar o passo '{op.step_id}' como {op.effect}"
        case AddTextPostcondition():
            return "conferir um texto no fim"
        case SetIntent():
            return f"descrever o alvo do passo '{op.step_id}'"
        case SetRobotInfo():
            return "mudar o nome, o título ou a descrição do robô"
    return "operação"


def _accepted(compiled: CompileResult, op: EditOp) -> str | None:
    """Motivo da recusa da operação sobre o robô compilado, ou None se ela entra."""
    result = apply_edits(compiled.manifest, [op], observed_texts=compiled.observed_texts)
    if result.rejected:
        return result.rejected[0][1]
    return None


def _repetition(candidate: RepetitionCandidate) -> RepetitionProposal:
    return RepetitionProposal(
        proposal_id=candidate.proposal_id,
        first_step_id=candidate.body_step_ids[0],
        body_step_ids=candidate.body_step_ids,
        step_ids=candidate.step_ids,
        times=candidate.times,
        list_name=candidate.list_name,
        list_title=candidate.list_title,
        fields=[
            RepetitionField(
                step_id=slot.step_id,
                name=slot.suggested_name,
                title=slot.suggested_title,
                type=slot.suggested_type,
                values=slot.values,
            )
            for slot in candidate.slots
        ],
    )


def build_proposals(
    compiled: CompileResult,
    *,
    session_id: UUID,
    recording_id: UUID,
    model_ops: Sequence[EditOp] | None,
    model_message: str | None = None,
) -> GeneralizationProposals:
    """Propostas da revisão; ``model_ops`` é None quando o modelo não respondeu."""
    manifest = compiled.manifest
    titles = {step.id: step.title for step, _ in manifest.iter_steps()}
    parameters = {
        c.step_id: ParameterProposal(
            step_id=c.step_id,
            step_title=titles.get(c.step_id, c.intent),
            field_label=c.field_label,
            value_preview=c.value_preview,
            name=c.suggested_name,
            title=c.suggested_title,
            type=c.suggested_type,
        )
        for c in compiled.parameter_candidates
    }
    step_names: dict[str, StepNameProposal] = {}
    effects: dict[str, EffectProposal] = {}
    checks: dict[str, CheckProposal] = {}
    rejected: list[str] = []
    robot_info: RobotInfoProposal | None = None
    for op in model_ops or ():
        reason = _accepted(compiled, op)
        if reason is not None:
            rejected.append(f"{describe_op(op)}: {reason}")
            continue
        match op:
            case Parametrize():
                base = parameters.get(op.step_id)
                parameters[op.step_id] = ParameterProposal(
                    step_id=op.step_id,
                    step_title=titles.get(op.step_id, op.step_id),
                    field_label=base.field_label if base else None,
                    value_preview=base.value_preview if base else "",
                    name=op.input_name,
                    title=op.title,
                    type=op.type,
                    source=ProposalSource.MODEL,
                )
            case RenameStep():
                if op.title != titles.get(op.step_id):
                    step_names[op.step_id] = StepNameProposal(
                        step_id=op.step_id,
                        current_title=titles.get(op.step_id, ""),
                        title=op.title,
                    )
            case RaiseEffect():
                step = manifest.step_by_id(op.step_id)
                raised = Effect(op.effect)
                if step is not None and max_effect(step.effect, raised) is not step.effect:
                    effects[op.step_id] = EffectProposal(
                        step_id=op.step_id,
                        step_title=step.title,
                        current=step.effect,
                        proposed=op.effect,
                    )
            case AddTextPostcondition():
                checks.setdefault(op.text, CheckProposal(text=op.text))
            case SetRobotInfo():
                # Uma proposta à parte: o formulário fica com o título e a descrição compilados.
                title = clip_label(op.title, 120) if op.title else None
                if title == manifest.title:
                    title = None
                description = op.description if op.description != manifest.description else None
                if title or description:
                    robot_info = RobotInfoProposal(title=title, description=description)
            case _:
                pass  # o alvo de um passo não muda pela revisão
    order = {step.id: n for n, (step, _) in enumerate(manifest.iter_steps())}
    message = model_message
    if model_ops is None and message is None:
        message = "As propostas do modelo estão indisponíveis agora."
    return GeneralizationProposals(
        session_id=session_id,
        recording_id=recording_id,
        name=manifest.name,
        title=clip_label(manifest.title, 120),
        description=manifest.description,
        steps=overview(manifest),
        robot_info=robot_info,
        parameters=sorted(parameters.values(), key=lambda p: order.get(p.step_id, 0)),
        step_names=sorted(step_names.values(), key=lambda p: order.get(p.step_id, 0)),
        repetitions=[_repetition(c) for c in find_repetitions(manifest)],
        effects=sorted(effects.values(), key=lambda p: order.get(p.step_id, 0)),
        checks=list(checks.values()),
        warnings=list(compiled.warnings),
        model_available=model_ops is not None,
        model_message=message,
        model_rejected=rejected[:_MAX_REJECTED],
    )


def apply_choices(compiled: CompileResult, choices: DraftChoices) -> Manifest:
    """O robô com as escolhas confirmadas; ``InvalidInput`` com os motivos do que não entra."""
    manifest = compiled.manifest
    errors: list[str] = []
    candidates = {c.proposal_id: c for c in find_repetitions(manifest)}
    chosen: list[tuple[RepetitionCandidate, RepetitionChoice]] = []
    seen: set[str] = set()
    for repetition in choices.repetitions:
        candidate = candidates.get(repetition.proposal_id)
        if candidate is None or repetition.proposal_id in seen:
            errors.append(f"a repetição '{repetition.proposal_id}' não existe nesta gravação")
            continue
        seen.add(repetition.proposal_id)
        chosen.append((candidate, repetition))
    # De trás para a frente: as posições das repetições anteriores não mudam.
    for candidate, repetition in sorted(chosen, key=lambda pair: -pair[0].start):
        fields = {f.step_id: (f.name, f.title, f.type) for f in repetition.fields}
        try:
            manifest = apply_repetition(
                manifest,
                candidate,
                list_name=repetition.list_name,
                list_title=repetition.list_title,
                fields=fields,
            )
        except ValueError as exc:
            errors.append(str(exc).split("\n", 1)[0])
    ops: list[EditOp] = [
        SetRobotInfo(name=choices.name, title=choices.title, description=choices.description)
    ]
    ops += [
        Parametrize(
            step_id=p.step_id,
            input_name=p.name,
            title=p.title,
            type=p.type,
            description=p.description,
        )
        for p in choices.parameters
    ]
    ops += [RenameStep(step_id=s.step_id, title=s.title) for s in choices.step_names]
    ops += [RaiseEffect(step_id=e.step_id, effect=e.effect) for e in choices.effects]
    ops += [AddTextPostcondition(text=text) for text in choices.checks]
    result = apply_edits(manifest, ops, observed_texts=compiled.observed_texts)
    errors += [f"{describe_op(op)}: {reason}" for op, reason in result.rejected]
    if errors:
        raise InvalidInput("algumas escolhas da revisão não puderam ser aplicadas", errors)
    return result.manifest
