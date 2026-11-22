"""Serviços do plano de controle sobre o PostgreSQL.

Implementam os protocolos de :mod:`rapa.core.contracts` com as mesmas regras da
plataforma em memória (:mod:`rapa.fixtures.memory`):

- :class:`PgRobotAdmin`, :class:`PgRobotCatalog` e :class:`PgRunService`, ou
  :class:`PgPlatform`, que junta os três num objeto só;
- :class:`PgPrincipalResolver` (protocolo ``PrincipalResolver`` da camada HTTP);
- :class:`PgProvisioning`, cadastro de empresas, áreas, usuários e vínculos;
- :mod:`~rapa.control.services.queue` e :class:`Dispatcher`, fila e despacho;
- publicar: :class:`PgSchedules` e :class:`Scheduler` (agendas), :class:`PgWebhooks`
  (webhooks) e :class:`PgBatches` (lote por planilha);
- consertar: :class:`PgRepairs` (o passo que quebrou, com os candidatos do retrato e
  a sugestão do modelo);
- credenciais e as regras do certificado A1: :class:`PgCredentials` (registro,
  custodiante, autorizações por robô e aprovações) e :class:`CredentialAlerts` (os
  avisos de vencimento, uma etapa do despachante);
- operar por empresa: :class:`PgCompanyAdmin` (usuários, papéis, áreas e consumo, pela
  própria empresa), :class:`PgOrganizationDirectory` (a empresa de uma organização do
  provedor) e :mod:`~rapa.control.services.lifecycle` (entrada, congelar, exportar e
  apagar, pela plataforma);
- evidências: :class:`PgArtifacts` (os artefatos de uma execução) e
  :class:`Retention` (a retenção, uma etapa do laço do despachante).
"""

from typing import TYPE_CHECKING

from rapa.control.artifacts import LocalArtifactStore
from rapa.control.db.session import Database
from rapa.control.services.artifacts import PgArtifacts
from rapa.control.services.audit import ChainReport, verify_chain
from rapa.control.services.batches import PgBatches
from rapa.control.services.company import PgCompanyAdmin
from rapa.control.services.credentials import CredentialAlerts, PgCredentials
from rapa.control.services.dispatcher import (
    Dispatcher,
    Launcher,
    RetentionTicker,
    Ticker,
    WorkerNotStarted,
    failure_result,
)
from rapa.control.services.principals import PgOrganizationDirectory, PgPrincipalResolver
from rapa.control.services.provisioning import PgProvisioning, set_platform_capacity
from rapa.control.services.queue import (
    EventConflict,
    PgRunQueue,
    ReapReport,
    RunnerOutcome,
    StaleAttempt,
)
from rapa.control.services.repairs import HubRepairAssistant, PgRepairs
from rapa.control.services.retention import Retention, RetentionPolicy, RetentionReport
from rapa.control.services.robots import PgRobotAdmin, PgRobotCatalog
from rapa.control.services.runs import PgRunService
from rapa.control.services.schedules import PgSchedules, Scheduler
from rapa.control.services.webhooks import PgWebhooks, WebhookKeys
from rapa.core.contracts import (
    ArtifactService,
    BatchService,
    CompanyAdminService,
    CredentialService,
    RepairService,
    RobotAdmin,
    RobotCatalog,
    RunService,
    ScheduleService,
    WebhookReceiver,
    WebhookService,
)

__all__ = [
    "ChainReport",
    "CredentialAlerts",
    "Dispatcher",
    "EventConflict",
    "HubRepairAssistant",
    "Launcher",
    "PgArtifacts",
    "PgBatches",
    "PgCompanyAdmin",
    "PgCredentials",
    "PgOrganizationDirectory",
    "PgPlatform",
    "PgPrincipalResolver",
    "PgProvisioning",
    "PgRepairs",
    "PgRobotAdmin",
    "PgRobotCatalog",
    "PgRunQueue",
    "PgRunService",
    "PgSchedules",
    "PgWebhooks",
    "ReapReport",
    "Retention",
    "RetentionPolicy",
    "RetentionReport",
    "RetentionTicker",
    "RunnerOutcome",
    "Scheduler",
    "StaleAttempt",
    "Ticker",
    "WebhookKeys",
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

    def _publishing_conforms(
        db: Database, keys: WebhookKeys
    ) -> tuple[ScheduleService, WebhookService, WebhookReceiver, BatchService, Ticker]:
        webhooks = PgWebhooks(db, keys)
        return PgSchedules(db), webhooks, webhooks, PgBatches(db), Scheduler(db, db)

    def _repair_conforms(db: Database) -> RepairService:
        return PgRepairs(db)

    def _credentials_conforms(db: Database) -> CredentialService:
        return PgCredentials(db)

    def _company_conforms(db: Database) -> CompanyAdminService:
        return PgCompanyAdmin(db)

    def _evidence_conforms(db: Database) -> tuple[ArtifactService, RetentionTicker]:
        store = LocalArtifactStore("artefatos")
        return PgArtifacts(db, store), Retention(db, store)
