"""Sessões de gravação no plano de controle: Demonstrar e Generalizar.

- :mod:`~rapa.control.sessions.service`: as regras (:class:`RecordingSessions`);
- :mod:`~rapa.control.sessions.store`: o armazenamento (protocolo e memória; o do
  PostgreSQL fica em :mod:`rapa.control.services.sessions`);
- :mod:`~rapa.control.sessions.launcher`: quem põe de pé o hospedeiro de cada sessão
  (um processo local ou um pod do Kubernetes);
- :mod:`~rapa.control.sessions.relay`: o repasse da visão ao vivo;
- :mod:`~rapa.control.sessions.tickets`: os bilhetes de uso único do websocket;
- :mod:`~rapa.control.sessions.assistant`: as propostas do modelo de linguagem.
"""

from rapa.control.sessions.assistant import (
    AssistantUnavailable,
    GeneralizationAssistant,
    HubAssistant,
)
from rapa.control.sessions.launcher import (
    KubernetesSessionLauncher,
    KubernetesSessionSettings,
    LocalSessionLauncher,
    LocalSessionSettings,
    SessionEndpoint,
    SessionJob,
    SessionLauncher,
    SessionNotStarted,
)
from rapa.control.sessions.relay import LiveRelay, SessionGone, StopFailed, Viewer
from rapa.control.sessions.service import RecordingSessions, SessionLimits
from rapa.control.sessions.store import MemorySessionStore, SessionRecord, SessionStore
from rapa.control.sessions.tickets import TicketBook
from rapa.core.contracts import RecordingService

__all__ = [
    "AssistantUnavailable",
    "GeneralizationAssistant",
    "HubAssistant",
    "KubernetesSessionLauncher",
    "KubernetesSessionSettings",
    "LiveRelay",
    "LocalSessionLauncher",
    "LocalSessionSettings",
    "MemorySessionStore",
    "RecordingSessions",
    "SessionEndpoint",
    "SessionGone",
    "SessionJob",
    "SessionLauncher",
    "SessionLimits",
    "SessionNotStarted",
    "SessionRecord",
    "SessionStore",
    "StopFailed",
    "TicketBook",
    "Viewer",
]


def _conforms(service: RecordingSessions) -> RecordingService:
    # Conferência estática: o mypy acusa se o serviço deixar de seguir o protocolo.
    return service
