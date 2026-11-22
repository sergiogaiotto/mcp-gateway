"""Auxiliares compartilhados pelos serviços do PostgreSQL."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import RowMapping, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import companies
from rapa.control.db.session import Database
from rapa.core.contracts import (
    CompanyFrozen,
    CompanyStatus,
    InvalidInput,
    NotFound,
    Principal,
    RobotSummary,
    RobotVersionInfo,
    RunState,
    RunStatus,
    RunTrigger,
    VersionStatus,
    required_approvals,
)
from rapa.core.robot import Manifest

__all__ = [
    "COMPANY_STATUS_LOCK_CLASS",
    "INTERACTIVE_PRIORITY",
    "PLATFORM_ACTOR",
    "PRIORITY_BY_TRIGGER",
    "PgService",
    "clean_title",
    "ensure_company_active",
    "lock_company_status",
    "now_utc",
    "platform_actor",
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

#: Classe da trava consultiva da situação da empresa (segundo argumento: a empresa).
#: Toda escrita de uma empresa a toma compartilhada, primeiro na transação; congelar
#: e apagar a empresa a tomam exclusiva, também primeiro. Assim quem congela espera as
#: escritas em andamento terminarem, e as que chegam depois já veem a empresa congelada.
COMPANY_STATUS_LOCK_CLASS = 7130

#: Quem aparece na auditoria das operações da plataforma (seguido de quem pediu).
PLATFORM_ACTOR = "plataforma"


def now_utc() -> datetime:
    return datetime.now(UTC)


def platform_actor(requested_by: str) -> str:
    """O ator da auditoria de uma operação da plataforma: ``plataforma:<quem pediu>``."""
    who = " ".join(requested_by.split()) or "desconhecido"
    return f"{PLATFORM_ACTOR}:{who}"[:255]


def clean_title(text: str) -> str:
    """O título com os brancos normalizados; um título só de brancos é recusado."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise InvalidInput("título inválido", ["o título não pode ficar em branco"])
    return cleaned


async def lock_company_status(session: AsyncSession, company_id: UUID) -> CompanyStatus | None:
    """Trava a situação da empresa (compartilhada) até o fim da transação e a devolve.

    ``None`` quando a empresa não existe (ou não é a da transação). Chame antes de
    qualquer outra trava da transação: congelar e apagar uma empresa tomam esta mesma
    trava, exclusiva, antes de tudo, e a ordem igual evita o impasse.
    """
    await session.execute(
        select(
            func.pg_advisory_xact_lock_shared(
                COMPANY_STATUS_LOCK_CLASS, func.hashtext(str(company_id))
            )
        )
    )
    status = await session.scalar(select(companies.c.status).where(companies.c.id == company_id))
    return None if status is None else CompanyStatus(status)


async def ensure_company_active(session: AsyncSession, company_id: UUID) -> None:
    """Recusa (:class:`CompanyFrozen`) qualquer coisa nova numa empresa congelada.

    É a função única da regra "congelada: lê tudo, mas nada novo acontece", chamada no
    começo de toda escrita: enfileirar execuções (``enqueue_run`` e ``enqueue_many``,
    que cobrem REST, MCP, interface, agenda, webhook e lote), criar, validar e publicar
    versões (cada aprovação, no certificado A1), criar e mudar agendas, webhooks e lotes,
    abrir sessão de gravação, pedir conserto e, no certificado A1, registrar e renovar
    credenciais e autorizar robôs. O que só diminui o acesso (revogar uma credencial ou
    uma autorização, tirar papéis) toma só :func:`lock_company_status`. Sem a empresa,
    ``NotFound`` (falha fechada). No banco, o gatilho ``rapa_company_active_guard`` recusa
    a linha nova dessas tabelas mesmo de um caminho que esqueça esta chamada (migração
    0007).
    """
    status = await lock_company_status(session, company_id)
    if status is None:
        raise NotFound("empresa não encontrada")
    if status is CompanyStatus.FROZEN:
        raise CompanyFrozen()


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


def version_info(
    robot_id: UUID, row: RowMapping, approvals: Sequence[str] = ()
) -> RobotVersionInfo:
    """A versão como os contratos a mostram; ``approvals`` são os aprovadores, em ordem."""
    manifest = Manifest.model_validate(row["manifest"])
    return RobotVersionInfo(
        robot_id=robot_id,
        version_id=row["id"],
        version_no=row["version_no"],
        status=VersionStatus(row["status"]),
        manifest=manifest,
        fingerprint=row["fingerprint"],
        created_by=row["created_by"],
        approved_by=row["approved_by"],
        created_at=row["created_at"],
        violations=list(row["violations"]),
        approvals=list(approvals),
        approvals_required=required_approvals(manifest),
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
