"""Operações de edição que o modelo de linguagem pode propor sobre um robô.

O modelo nunca reescreve o manifesto inteiro: ele propõe operações deste
conjunto fechado, e o código as aplica de forma determinística. Regras:

- só se parametriza um valor literal que veio da gravação;
- o efeito de um passo só pode subir (leitura → gravação → irreversível);
- uma pós-condição de texto só é aceita se o texto foi visto na gravação.

Operações recusadas voltam com o motivo em pt-BR, para a pessoa ver o que foi
proposto e não entrou.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from rapa.core import templates
from rapa.core.robot import (
    NAME_PATTERN,
    SLUG_PATTERN,
    Effect,
    FieldType,
    Manifest,
    max_effect,
)

__all__ = [
    "AddTextPostcondition",
    "EditOp",
    "EditResult",
    "Parametrize",
    "RaiseEffect",
    "RenameStep",
    "SetIntent",
    "SetRobotInfo",
    "apply_edits",
    "edit_ops_adapter",
]

_SIMPLE_TYPES = Literal["string", "integer", "number", "boolean", "date", "cpf", "cnpj", "money"]


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SetRobotInfo(_Op):
    op: Literal["set_robot_info"] = "set_robot_info"
    name: str | None = Field(default=None, pattern=SLUG_PATTERN)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=2000)


class RenameStep(_Op):
    op: Literal["rename_step"] = "rename_step"
    step_id: str
    title: str = Field(min_length=1, max_length=200)


class SetIntent(_Op):
    op: Literal["set_intent"] = "set_intent"
    step_id: str
    intent: str = Field(min_length=1, max_length=300)


class Parametrize(_Op):
    """Troca o valor literal de um passo por uma entrada do robô."""

    op: Literal["parametrize"] = "parametrize"
    step_id: str
    input_name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    type: _SIMPLE_TYPES = "string"
    description: str = Field(default="", max_length=1000)


class RaiseEffect(_Op):
    op: Literal["raise_effect"] = "raise_effect"
    step_id: str
    effect: Literal["write", "irreversible"]


class AddTextPostcondition(_Op):
    op: Literal["add_text_postcondition"] = "add_text_postcondition"
    text: str = Field(min_length=3, max_length=200)


type EditOp = Annotated[
    SetRobotInfo | RenameStep | SetIntent | Parametrize | RaiseEffect | AddTextPostcondition,
    Field(discriminator="op"),
]

edit_ops_adapter: TypeAdapter[list[EditOp]] = TypeAdapter(list[EditOp])


@dataclass(frozen=True)
class EditResult:
    manifest: Manifest
    applied: list[EditOp]
    rejected: list[tuple[EditOp, str]]


def _iter_step_dicts(steps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for step in steps:
        yield step
        for key in ("steps", "then", "otherwise"):
            child = step.get(key)
            if isinstance(child, list):
                yield from _iter_step_dicts(child)


def _find(data: dict[str, Any], step_id: str) -> dict[str, Any] | None:
    return next((s for s in _iter_step_dicts(data["steps"]) if s.get("id") == step_id), None)


def _literal_slot(step: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Onde fica o valor literal parametrizável do passo, se houver."""
    if step.get("kind") == "fill":
        value = step.get("value", {})
        if value.get("source") == "text":
            return value, "template"
        return None
    if step.get("kind") == "select":
        return step, "option"
    return None


def _apply_one(data: dict[str, Any], op: EditOp, observed_texts: set[str] | None) -> str | None:
    """Aplica a operação em ``data``. Devolve o motivo da recusa ou None."""
    match op:
        case SetRobotInfo():
            for key in ("name", "title", "description"):
                value = getattr(op, key)
                if value is not None:
                    data[key] = value
            return None
        case RenameStep(step_id=step_id, title=title):
            step = _find(data, step_id)
            if step is None:
                return f"o passo '{step_id}' não existe"
            step["title"] = title
            return None
        case SetIntent(step_id=step_id, intent=intent):
            step = _find(data, step_id)
            if step is None:
                return f"o passo '{step_id}' não existe"
            target = step.get("target")
            if not isinstance(target, dict):
                return f"o passo '{step_id}' não tem alvo"
            target["intent"] = intent
            return None
        case Parametrize():
            step = _find(data, op.step_id)
            if step is None:
                return f"o passo '{op.step_id}' não existe"
            slot = _literal_slot(step)
            if slot is None:
                return f"o passo '{op.step_id}' não tem valor digitado que possa virar entrada"
            holder, key = slot
            current = holder[key]
            if templates.has_references(current):
                return f"o valor do passo '{op.step_id}' já usa uma entrada ou variável"
            existing = next((f for f in data["inputs"] if f["name"] == op.input_name), None)
            if existing is not None and existing["type"] != op.type:
                return f"a entrada '{op.input_name}' já existe com outro tipo"
            if existing is None:
                data["inputs"].append(
                    {
                        "name": op.input_name,
                        "title": op.title,
                        "description": op.description,
                        "type": FieldType(op.type).value,
                        "required": True,
                    }
                )
            holder[key] = f"{{{{ inputs.{op.input_name} }}}}"
            return None
        case RaiseEffect(step_id=step_id, effect=effect):
            step = _find(data, step_id)
            if step is None:
                return f"o passo '{step_id}' não existe"
            current_effect = Effect(step.get("effect", "read"))
            step["effect"] = max_effect(current_effect, Effect(effect)).value
            return None
        case AddTextPostcondition(text=text):
            if observed_texts is not None and not any(text in seen for seen in observed_texts):
                return f"o texto '{text}' não apareceu na gravação"
            data["postconditions"].append(
                {"op": "text_contains", "text": templates.escape(text), "negate": False}
            )
            return None
    return "operação desconhecida"


def apply_edits(
    manifest: Manifest,
    edits: Iterable[EditOp],
    *,
    observed_texts: set[str] | None = None,
) -> EditResult:
    """Aplica as operações válidas, uma a uma, e devolve as recusadas com o motivo.

    Cada operação é aplicada sobre uma cópia; se o manifesto resultante não
    for válido, a operação é recusada e o manifesto anterior é mantido.
    """
    current = manifest
    applied: list[EditOp] = []
    rejected: list[tuple[EditOp, str]] = []
    for op in edits:
        data = current.model_dump(mode="json")
        reason = _apply_one(data, op, observed_texts)
        if reason is not None:
            rejected.append((op, reason))
            continue
        try:
            current = Manifest.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            rejected.append((op, f"resultado inválido: {first['msg']}"))
            continue
        applied.append(op)
    return EditResult(manifest=current, applied=applied, rejected=rejected)
