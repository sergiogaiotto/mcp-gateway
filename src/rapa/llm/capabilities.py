"""Matriz de capacidades de um apelido de modelo no hub.

A sonda (``rapa.llm.probe``) mede o que o hub realmente faz, julgando pelo
efeito e não pela ausência de erro, e grava esta matriz. O cliente e a saída
estruturada consultam a matriz para decidir como pedir cada coisa, e
``ensure_capabilities`` bloqueia uma operação quando falta algo que ela exige.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rapa.llm.errors import CapabilityMissing

__all__ = [
    "DOCUMENTED_MAX_TOKENS",
    "Capability",
    "CapabilityMatrix",
    "Check",
    "CheckStatus",
    "ReasoningShape",
    "capability_label",
    "ensure_capabilities",
    "matrix_changes",
]

#: Teto de ``max_tokens`` documentado pelo hub.
DOCUMENTED_MAX_TOKENS = 8192


class CheckStatus(StrEnum):
    #: Funciona, verificado pelo efeito.
    YES = "yes"
    #: O hub aceitou e ignorou, ou recusou porque não oferece.
    NO = "no"
    #: Não deu para concluir (rede, tempo esgotado, erro 5xx, resposta ilegível).
    ERROR = "error"
    #: Não verificado porque um pré-requisito falhou.
    SKIPPED = "skipped"


class ReasoningShape(StrEnum):
    """Onde o raciocínio do modelo aparece na resposta."""

    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"
    INLINE_THINK = "inline_think"
    INLINE_HARMONY = "inline_harmony"
    NONE = "none"
    UNKNOWN = "unknown"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(_Model):
    """Resultado de uma verificação da sonda."""

    status: CheckStatus = CheckStatus.SKIPPED
    #: O hub aceitou a requisição (HTTP 2xx)? None quando não houve resposta.
    accepted: bool | None = None
    http_status: int | None = None
    detail: str = Field(default="", max_length=1000)
    elapsed_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is CheckStatus.YES


class Capability(StrEnum):
    """O que uma operação pode exigir do hub."""

    MODEL_LISTED = "model_listed"
    BASELINE = "baseline"
    THINKING_OFF = "thinking_off"
    JSON_SCHEMA = "json_schema"
    TOOLS = "tools"
    TOOL_CHOICE_REQUIRED = "tool_choice_required"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    IMAGE_INPUT = "image_input"
    LARGE_MAX_TOKENS = "large_max_tokens"
    LOGPROBS = "logprobs"


_LABELS: dict[Capability, str] = {
    Capability.MODEL_LISTED: "apelido listado em /models",
    Capability.BASELINE: "resposta básica correta",
    Capability.THINKING_OFF: "desligar o raciocínio por requisição",
    Capability.JSON_SCHEMA: "saída JSON com esquema (response_format json_schema)",
    Capability.TOOLS: "chamadas de ferramenta estruturadas",
    Capability.TOOL_CHOICE_REQUIRED: 'tool_choice "required"',
    Capability.PARALLEL_TOOL_CALLS: "chamadas de ferramenta em paralelo",
    Capability.IMAGE_INPUT: "entrada de imagem",
    Capability.LARGE_MAX_TOKENS: f"max_tokens acima de {DOCUMENTED_MAX_TOKENS}",
    Capability.LOGPROBS: "logprobs",
}


class CapabilityMatrix(_Model):
    """O que um apelido de modelo faz no hub, medido pela sonda."""

    format_version: Literal[1] = 1
    alias: str = Field(min_length=1, max_length=200)
    probed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: Modelos listados em GET /models (None se a listagem falhou).
    models_listed: list[str] | None = None
    alias_listed: Check = Field(default_factory=Check)
    #: Campo "model" da resposta: identifica o modelo real por trás do apelido.
    fingerprint: str | None = None
    baseline: Check = Field(default_factory=Check)
    #: YES quando o hub recusa campos desconhecidos (um 200 então prova que o campo é conhecido).
    rejects_unknown_fields: Check = Field(default_factory=Check)
    thinking_off_template_kwargs: Check = Field(default_factory=Check)
    thinking_off_reasoning_effort: Check = Field(default_factory=Check)
    reasoning_shape: ReasoningShape = ReasoningShape.UNKNOWN
    json_schema_enforced: Check = Field(default_factory=Check)
    tools_structured: Check = Field(default_factory=Check)
    tool_choice_required: Check = Field(default_factory=Check)
    parallel_tool_calls: Check = Field(default_factory=Check)
    image_input: Check = Field(default_factory=Check)
    max_tokens_above_documented: Check = Field(default_factory=Check)
    #: Maior max_tokens aceito comprovadamente.
    max_tokens_ceiling: int = Field(default=DOCUMENTED_MAX_TOKENS, ge=1)
    logprobs: Check = Field(default_factory=Check)
    latency_ms: int | None = None
    tokens_per_second: float | None = None
    #: Erros encontrados durante a sonda, já sem a chave.
    errors: list[str] = Field(default_factory=list)

    def check_for(self, capability: Capability) -> Check:
        match capability:
            case Capability.MODEL_LISTED:
                return self.alias_listed
            case Capability.BASELINE:
                return self.baseline
            case Capability.THINKING_OFF:
                if self.thinking_off_template_kwargs.ok:
                    return self.thinking_off_template_kwargs
                return self.thinking_off_reasoning_effort
            case Capability.JSON_SCHEMA:
                return self.json_schema_enforced
            case Capability.TOOLS:
                return self.tools_structured
            case Capability.TOOL_CHOICE_REQUIRED:
                return self.tool_choice_required
            case Capability.PARALLEL_TOOL_CALLS:
                return self.parallel_tool_calls
            case Capability.IMAGE_INPUT:
                return self.image_input
            case Capability.LARGE_MAX_TOKENS:
                return self.max_tokens_above_documented
            case Capability.LOGPROBS:
                return self.logprobs

    def has(self, capability: Capability) -> bool:
        return self.check_for(capability).ok

    def missing(self, required: Iterable[Capability | str]) -> list[Capability]:
        wanted = [Capability(item) for item in required]
        return [cap for cap in dict.fromkeys(wanted) if not self.has(cap)]

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> CapabilityMatrix:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def capability_label(capability: Capability) -> str:
    """Nome da capacidade em pt-BR."""
    return _LABELS[capability]


def ensure_capabilities(
    matrix: CapabilityMatrix | None,
    required: Iterable[Capability | str],
    *,
    model: str | None = None,
) -> None:
    """Bloqueia a operação se a matriz não comprovar tudo o que ela exige.

    ``model``, quando informado, precisa ser o apelido medido pela matriz.
    """
    wanted = [Capability(item) for item in required]
    if matrix is None:
        raise CapabilityMissing(
            "não há matriz de capacidades do hub; rode a sonda "
            "(python -m rapa.llm.probe) antes de usar o modelo",
            [cap.value for cap in wanted],
        )
    if model is not None and matrix.alias != model:
        raise CapabilityMissing(
            f"a matriz de capacidades é do modelo '{matrix.alias}', não de '{model}'; "
            "rode a sonda para este modelo",
            [cap.value for cap in wanted],
        )
    missing = matrix.missing(wanted)
    if missing:
        names = ", ".join(capability_label(cap) for cap in missing)
        raise CapabilityMissing(
            f"o hub não oferece o que esta operação exige do modelo '{matrix.alias}': {names}. "
            "Rode a sonda de novo ou fale com o time do hub.",
            [cap.value for cap in missing],
        )


def matrix_changes(previous: CapabilityMatrix, current: CapabilityMatrix) -> list[str]:
    """Diferenças relevantes entre duas sondas do mesmo apelido, em pt-BR.

    Serve para bloquear o uso quando o hub muda sem aviso (outro modelo por
    trás do apelido ou uma capacidade que deixou de funcionar).
    """
    changes: list[str] = []
    if previous.alias != current.alias:
        changes.append(f"apelido diferente: '{previous.alias}' → '{current.alias}'")
    if previous.fingerprint != current.fingerprint:
        changes.append(f"modelo mudou: '{previous.fingerprint}' → '{current.fingerprint}'")
    if previous.reasoning_shape is not current.reasoning_shape:
        changes.append(
            "formato do raciocínio mudou: "
            f"{previous.reasoning_shape.value} → {current.reasoning_shape.value}"
        )
    for cap in Capability:
        before, after = previous.has(cap), current.has(cap)
        if before and not after:
            changes.append(f"deixou de funcionar: {capability_label(cap)}")
        elif after and not before:
            changes.append(f"passou a funcionar: {capability_label(cap)}")
    return changes
