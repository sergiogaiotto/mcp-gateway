"""Serviços do plano de controle sobre o PostgreSQL.

Implementam os protocolos de :mod:`rapa.core.contracts` com as mesmas regras da
plataforma em memória (:mod:`rapa.fixtures.memory`):

- :class:`PgRobotAdmin`, :class:`PgRobotCatalog` e :class:`PgRunService`, ou
  :class:`PgPlatform`, que junta os três num objeto só;
- :class:`PgPrincipalResolver` (protocolo ``PrincipalResolver`` da camada HTTP);
- :class:`PgProvisioning`, cadastro de empresas, áreas, usuários e vínculos;
- :mod:`~rapa.control.services.queue` e :class:`Dispatcher`, fila e despacho.
"""

from typing import TYPE_CHECKING

from rapa.control.db.session import Database
from rapa.control.services.audit import ChainReport, verify_chain
from rapa.control.services.dispatcher import (
    Dispatcher,
    Launcher,
    WorkerNotStarted,
    failure_result,
)
from rapa.control.services.principals import PgPrincipalResolver
from rapa.control.services.provisioning import PgProvisioning, set_platform_capacity
from rapa.control.services.queue import (
    EventConflict,
    PgRunQueue,
    ReapReport,
    RunnerOutcome,
    StaleAttempt,
)
from rapa.control.services.robots import PgRobotAdmin, PgRobotCatalog
from rapa.control.services.runs import PgRunService
from rapa.core.contracts import RobotAdmin, RobotCatalog, RunService

__all__ = [
    "ChainReport",
    "Dispatcher",
    "EventConflict",
    "Launcher",
    "PgPlatform",
    "PgPrincipalResolver",
    "PgProvisioning",
    "PgRobotAdmin",
    "PgRobotCatalog",
    "PgRunQueue",
    "PgRunService",
    "ReapReport",
    "RunnerOutcome",
    "StaleAttempt",
    "WorkerNotStarted",
    "failure_result",
    "set_platform_capacity",
    "verify_chain",
]


class PgPlatform(PgRobotAdmin, PgRobotCatalog, PgRunService):
    """Administração, catálogo e execuções num objeto só (como ``MemoryPlatform``)."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)


if TYPE_CHECKING:
    # Conferência estática: o mypy acusa se as classes deixarem de seguir os protocolos.
    def _conforms(db: Database) -> tuple[RobotAdmin, RobotCatalog, RunService, PgPlatform]:
        return PgRobotAdmin(db), PgRobotCatalog(db), PgRunService(db), PgPlatform(db)

    def _platform_conforms(platform: PgPlatform) -> tuple[RobotAdmin, RobotCatalog, RunService]:
        return platform, platform, platform
