"""O modelo de linguagem na revisão: propõe edições; o código decide.

:class:`GeneralizationAssistant` é o que o serviço das sessões usa para pedir as
propostas do modelo. :class:`HubAssistant` fala com o hub interno
(:func:`rapa.llm.generalize.propose_edits`, com o raciocínio desligado, saída JSON
validada e a matriz de capacidades da sonda). Qualquer falha (hub fora do ar,
matriz ausente ou de outro modelo, resposta inválida) vira
:class:`AssistantUnavailable`: a revisão segue só com as sugestões das regras.

O prompt só tem a visão compacta do robô compilado (referências ao cofre, nunca
valores) e os textos vistos na página, já limpos pelo gravador.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from rapa.core.edits import EditOp
from rapa.core.robot import Manifest
from rapa.llm.capabilities import CapabilityMatrix
from rapa.llm.client import HubClient
from rapa.llm.errors import LLMError
from rapa.llm.generalize import propose_edits

__all__ = ["AssistantUnavailable", "GeneralizationAssistant", "HubAssistant"]

logger = logging.getLogger("rapa.control.sessions")


class AssistantUnavailable(RuntimeError):
    """O modelo não pôde propor agora (a mensagem é fixa, em pt-BR)."""


class GeneralizationAssistant(Protocol):
    async def propose(self, manifest: Manifest, observed_texts: set[str]) -> list[EditOp]: ...


class HubAssistant:
    """Propostas do hub de LLM, com a matriz de capacidades medida pela sonda."""

    def __init__(
        self,
        client_factory: Callable[[], HubClient],
        capabilities_path: str | None,
    ) -> None:
        self._client_factory = client_factory
        self._capabilities_path = capabilities_path

    async def propose(self, manifest: Manifest, observed_texts: set[str]) -> list[EditOp]:
        if self._capabilities_path is None:
            raise AssistantUnavailable("a matriz de capacidades do hub não foi configurada")
        try:
            capabilities = CapabilityMatrix.load(self._capabilities_path)
        except OSError, ValueError:
            raise AssistantUnavailable("a matriz de capacidades do hub não pôde ser lida") from None
        try:
            async with self._client_factory() as client:
                return await propose_edits(
                    client, manifest, capabilities=capabilities, observed_texts=observed_texts
                )
        except LLMError as exc:
            # Só o tipo: a mensagem pode trazer trechos da resposta do hub.
            logger.warning("o modelo não propôs edições: %s", type(exc).__name__)
            raise AssistantUnavailable("o hub do modelo de linguagem não respondeu") from None
