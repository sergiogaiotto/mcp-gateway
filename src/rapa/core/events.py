"""Eventos de execução, enviados pelo executor ao plano de controle.

A tabela de eventos é a fonte da verdade do andamento: o número de sequência é
atribuído pelo executor (um escritor por execução) e o par (run_id, seq) é
único, o que torna a ingestão idempotente e permite retomar o acompanhamento a
partir do último número visto.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["RunEvent", "RunEventKind"]


class RunEventKind(StrEnum):
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    STEP_SUCCEEDED = "step_succeeded"
    STEP_FAILED = "step_failed"
    STEP_HEALED = "step_healed"
    HUMAN_NEEDED = "human_needed"
    ARTIFACT = "artifact"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_STOPPED_BEFORE_WRITE = "run_stopped_before_write"


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    attempt: int = Field(ge=1)
    seq: int = Field(ge=0)
    at: datetime
    kind: RunEventKind
    step_id: str | None = None
    #: Dados sem segredos: o executor mascara valores de credenciais antes de emitir.
    data: dict[str, Any] = Field(default_factory=dict)
