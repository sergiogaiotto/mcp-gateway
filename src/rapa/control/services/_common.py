"""Auxiliares compartilhados pelos serviços do PostgreSQL."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import RowMapping

from rapa.control.db.session import Database
from rapa.core.contracts import (
    InvalidInput,
    Principal,
    RobotSummary,
    RobotVersionInfo,
    RunState,
    RunStatus,
    RunTrigger,
    VersionStatus,
)
from rapa.core.robot import Manifest

__all__ = [
    "INTERACTIVE_PRIORITY",
    "PRIORITY_BY_TRIGGER",
    "PgService",
    "clean_title",
    "now_utc",
    "run_status",
    "summary",
    "version_info",
    "visible_areas",
]

#: Menor número = maior prioridade: interativo, depois MCP, API e interface,
#: depois agenda e webhook, depois lote. Só o interativo (a validação; as sessões
#: de gravação também ocupam a reserva, contadas à parte) usa a reserva e fica fora da cota da
#: empresa; o botão "Executar" da interface roda em produção e conta na cota,
#: senão uma empresa poderia tomar todas as vagas das outras.
PRIORITY_BY_TRIGGER: dict[RunTrigger, int] = {
    RunTrigger.VALIDATION: 0,
    RunTrigger.MANUAL: 1,
    RunTrigger.MCP: 1,
    RunTrigger.REST: 1,
    RunTrigger.SCHEDULE: 2,
    RunTrigger.WEBHOOK: 2,
    RunTrigger.BATCH: 3,
}
INTERACTIVE_PRIORITY = 0


def now_utc() -> datetime:
    return datetime.now(UTC)


def clean_title(text: str) -> str:
    """O título com os brancos normalizados; um título só de brancos é recusado."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise InvalidInput("título inválido", ["o título não pode ficar em branco"])
    return cleaned


class PgService:
    """Base dos serviços: guarda o banco."""

    def __init__(self, db: Database) -> None:
        self.db = db


def visible_areas(principal: Principal) -> list[UUID] | None:
    """Áreas que o principal enxerga; ``None`` quer dizer todas as da empresa.

    Mesma regra da plataforma em memória: um robô é visível quando o principal
    tem algum papel na área dele (papéis da empresa valem em todas as áreas).
    """
    if principal.company_roles:
        return None
    return sorted(area for area, roles in principal.area_roles.items() if roles)


def version_info(robot_id: UUID, row: RowMapping) -> RobotVersionInfo:
    return RobotVersionInfo(
        robot_id=robot_id,
        version_id=row["id"],
        version_no=row["version_no"],
        status=VersionStatus(row["status"]),
        manifest=Manifest.model_validate(row["manifest"]),
        fingerprint=row["fingerprint"],
        created_by=row["created_by"],
        approved_by=row["approved_by"],
        created_at=row["created_at"],
        violations=list(row["violations"]),
    )


def summary(row: RowMapping) -> RobotSummary:
    manifest = Manifest.model_validate(row["manifest"])
    return RobotSummary(
        robot_id=row["robot_id"],
        company_id=row["company_id"],
        area_id=row["area_id"],
        slug=row["slug"],
        title=manifest.title,
        description=manifest.description,
        version_id=row["version_id"],
        version_no=row["version_no"],
        effect=manifest.effect(),
        input_schema=manifest.input_json_schema(),
        output_schema=manifest.output_json_schema(),
        fingerprint=row["fingerprint"],
    )


def run_status(row: RowMapping, queue_position: int | None = None) -> RunStatus:
    return RunStatus(
        run_id=row["id"],
        robot_slug=row["slug"],
        version_no=row["version_no"],
        state=RunState(row["state"]),
        trigger=RunTrigger(row["trigger"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outputs=row["outputs"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        steps_done=row["steps_done"],
        queue_position=queue_position,
    )
