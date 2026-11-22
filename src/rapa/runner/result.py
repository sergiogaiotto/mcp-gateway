"""Resultado de uma execução, como o executor entrega ao plano de controle.

É o conteúdo do RESULT.json escrito pelo worker. Não depende do Playwright, para
que o plano de controle possa ler resultados sem instalar o navegador.
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rapa.core.contracts import RunState
from rapa.core.snapshot import PageSnapshot

__all__ = ["ArtifactKind", "ArtifactRef", "RunResult"]


class ArtifactKind(StrEnum):
    """Tipos de artefato que o executor produz."""

    SCREENSHOT = "screenshot"
    DOWNLOAD = "download"


class ArtifactRef(BaseModel):
    """Arquivo guardado pela execução (tela ou download)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    #: Nome do arquivo, já saneado.
    name: str = Field(min_length=1, max_length=255)
    #: Caminho onde o arquivo foi guardado.
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunResult(BaseModel):
    """Desfecho de uma tentativa de execução."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    attempt: int = Field(ge=1)
    state: RunState
    outputs: dict[str, Any] | None = None
    error_code: str | None = None
    #: Mensagem em pt-BR, sem segredos.
    error_message: str | None = None
    failed_step_id: str | None = None
    #: Algum passo irreversível começou: uma falha depois disso vira "indeterminado".
    irreversible_started: bool = False
    steps_done: int = Field(default=0, ge=0)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    #: Retrato da página no passo que falhou por causa do alvo (para o conserto
    #: assistido), sem valores de campos nem segredos; só em execuções que não deram certo.
    failure_snapshot: PageSnapshot | None = None
