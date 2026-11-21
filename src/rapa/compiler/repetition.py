"""Repetições na demonstração: passos seguidos que a pessoa fez de novo com outros valores.

Regras determinísticas, sem modelo de linguagem:

- só os passos do primeiro nível contam (nada dentro de uma repetição ou de uma
  condição que já exista);
- uma volta é um trecho de 1 a ``max_period`` passos; ela se repete quando as
  voltas seguintes, em sequência e pelo menos duas vezes no total, são iguais à
  primeira em tudo (tipo, alvo, efeito, tecla, endereço, credencial), menos o id,
  o título e o valor literal digitado ou escolhido (``fill`` de texto e
  ``select``);
- pelo menos um desses valores muda entre as voltas (repetir a mesma coisa não
  vira lista de entrada); os que não mudam ficam como estão;
- entre as candidatas que começam no mesmo passo, ganha a que cobre mais passos
  (e, no empate, a de volta mais curta); depois da escolhida, a procura segue no
  primeiro passo depois dela.

:func:`apply_repetition` troca as voltas por um ``for_each`` sobre uma entrada
de lista nova, com um campo por valor que muda (``{{ item.<campo> }}``). O efeito
do ``for_each`` fica ``read``: cada passo de dentro se anuncia com o próprio.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rapa.compiler.generalize import SimpleType, generalize_inputs, suggest_type
from rapa.compiler.naming import IdAllocator, clip_label, name_from
from rapa.core import templates
from rapa.core.robot import FillStep, Manifest, SelectStep, Step, TextValue

__all__ = [
    "MAX_PERIOD",
    "RepetitionCandidate",
    "RepetitionSlot",
    "apply_repetition",
    "find_repetitions",
]

#: Maior número de passos de uma volta.
MAX_PERIOD = 20
_PREVIEW = 60
_SLOT = "\x00valor"


class RepetitionSlot(BaseModel):
    """Um valor literal que muda de uma volta para outra."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: O passo da primeira volta que tem o valor.
    step_id: str
    #: A posição do passo dentro da volta.
    position: int = Field(ge=0)
    #: O valor de cada volta, na ordem.
    values: list[str]
    suggested_name: str
    suggested_title: str
    suggested_type: SimpleType = "string"


class RepetitionCandidate(BaseModel):
    """Voltas seguidas dos mesmos passos, a partir de ``start`` no primeiro nível."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    start: int = Field(ge=0)
    period: int = Field(ge=1)
    times: int = Field(ge=2)
    body_step_ids: list[str]
    step_ids: list[str]
    slots: list[RepetitionSlot] = Field(min_length=1)
    list_name: str
    list_title: str


def _literal(step: Step) -> str | None:
    """O valor literal do passo, se ele tiver um (texto de ``fill`` ou opção de ``select``)."""
    if isinstance(step, FillStep) and isinstance(step.value, TextValue):
        template = step.value.template
    elif isinstance(step, SelectStep):
        template = step.option
    else:
        return None
    try:
        if templates.has_references(template):
            return None
        return templates.render(template, {})
    except templates.TemplateError:
        return None


def _signature(step: Step) -> str:
    data: dict[str, Any] = step.model_dump(mode="json")
    data.pop("id", None)
    data.pop("title", None)
    if _literal(step) is not None:
        if isinstance(step, FillStep):
            data["value"]["template"] = _SLOT
        else:
            data["option"] = _SLOT
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _times(signatures: Sequence[str], start: int, period: int) -> int:
    block = signatures[start : start + period]
    times = 1
    while signatures[start + times * period : start + (times + 1) * period] == block:
        times += 1
    return times


def _unique(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    taken.add(name)
    return name


def _slots(
    steps: Sequence[Step], start: int, period: int, times: int, manifest: Manifest
) -> list[RepetitionSlot]:
    suggestions = {c.step_id: c for c in generalize_inputs(manifest)}
    taken: set[str] = set()
    slots: list[RepetitionSlot] = []
    for position in range(period):
        first = steps[start + position]
        values = [_literal(steps[start + n * period + position]) for n in range(times)]
        if values[0] is None or len(set(values)) == 1:
            continue
        literal_values = [v or "" for v in values]
        candidate = suggestions.get(first.id)
        label = candidate.suggested_title if candidate else first.title
        name = _unique(candidate.suggested_name if candidate else name_from(label, "valor"), taken)
        types = {suggest_type(v, label) for v in literal_values}
        slots.append(
            RepetitionSlot(
                step_id=first.id,
                position=position,
                values=[clip_label(v, _PREVIEW) for v in literal_values],
                suggested_name=name,
                suggested_title=clip_label(label, 200),
                suggested_type=types.pop() if len(types) == 1 else "string",
            )
        )
    return slots


def find_repetitions(
    manifest: Manifest, *, max_period: int = MAX_PERIOD
) -> list[RepetitionCandidate]:
    """As repetições do primeiro nível do robô, da primeira à última, sem sobreposição."""
    steps = list(manifest.steps)
    signatures = [_signature(step) for step in steps]
    taken_inputs = {f.name for f in manifest.inputs}
    found: list[RepetitionCandidate] = []
    i = 0
    while i < len(steps):
        best: tuple[int, int, int, list[RepetitionSlot]] | None = None
        for period in range(1, min(max_period, (len(steps) - i) // 2) + 1):
            times = _times(signatures, i, period)
            if times < 2:
                continue
            covered = times * period
            if best is not None and covered <= best[0] * best[1]:
                continue
            slots = _slots(steps, i, period, times, manifest)
            if slots:
                best = (period, times, covered, slots)
        if best is None:
            i += 1
            continue
        period, times, covered, slots = best
        list_name = _unique("itens", taken_inputs)
        found.append(
            RepetitionCandidate(
                proposal_id=f"repeticao_{steps[i].id}",
                start=i,
                period=period,
                times=times,
                body_step_ids=[s.id for s in steps[i : i + period]],
                step_ids=[s.id for s in steps[i : i + covered]],
                slots=slots,
                list_name=list_name,
                list_title=f"Itens ({times} na demonstração)",
            )
        )
        i += covered
    return found


def apply_repetition(
    manifest: Manifest,
    candidate: RepetitionCandidate,
    *,
    list_name: str,
    list_title: str,
    fields: Mapping[str, tuple[str, str, str]],
) -> Manifest:
    """Troca as voltas por um ``for_each`` sobre a entrada de lista ``list_name``.

    ``fields`` diz, para cada passo com valor que muda (``step_id`` da primeira
    volta), o nome, o título e o tipo do campo do item. Levanta ``ValueError`` (em
    pt-BR) se o robô não tiver mais essas voltas ou se faltar um campo.
    """
    current = find_repetitions(manifest)
    match = next((c for c in current if c.proposal_id == candidate.proposal_id), None)
    if match is None or match != candidate:
        raise ValueError(f"a repetição '{candidate.proposal_id}' não existe mais no robô")
    missing = [slot.step_id for slot in candidate.slots if slot.step_id not in fields]
    if missing:
        raise ValueError(f"falta o campo do item para o passo '{missing[0]}'")
    unknown = sorted(set(fields) - {slot.step_id for slot in candidate.slots})
    if unknown:
        raise ValueError(f"o passo '{unknown[0]}' não muda entre as voltas da repetição")
    names = [fields[slot.step_id][0] for slot in candidate.slots]
    if len(set(names)) != len(names):
        raise ValueError("os campos do item da repetição precisam ter nomes diferentes")
    if any(f.name == list_name for f in manifest.inputs):
        raise ValueError(f"já existe uma entrada chamada '{list_name}'")
    data = manifest.model_dump(mode="json")
    steps: list[dict[str, Any]] = data["steps"]
    start, period, times = candidate.start, candidate.period, candidate.times
    body = steps[start : start + period]
    for slot in candidate.slots:
        step = body[slot.position]
        reference = f"{{{{ item.{fields[slot.step_id][0]} }}}}"
        if step["kind"] == "fill":
            step["value"]["template"] = reference
        else:
            step["option"] = reference
    ids = IdAllocator({step.id for step, _ in manifest.iter_steps()})
    loop = {
        "kind": "for_each",
        "id": ids.allocate(f"repetir {body[0]['id']}"),
        "title": clip_label(f"Repetir para cada item de {list_title}", 200),
        "items": f"inputs.{list_name}",
        "steps": body,
        "max_items": 1000,
    }
    data["steps"] = [*steps[:start], loop, *steps[start + period * times :]]
    data["inputs"].append(
        {
            "name": list_name,
            "title": list_title,
            "description": "",
            "type": "list",
            "required": True,
            "item_fields": [
                {
                    "name": fields[slot.step_id][0],
                    "title": fields[slot.step_id][1],
                    "description": "",
                    "type": fields[slot.step_id][2],
                    "required": True,
                }
                for slot in candidate.slots
            ],
        }
    )
    return Manifest.model_validate(data)
