"""Administração e catálogo de robôs no PostgreSQL.

Mesmas regras de :class:`rapa.fixtures.memory.MemoryPlatform`: tudo é filtrado
pela empresa do principal (e pelo isolamento por linha do banco), um robô só é
visível para quem tem papel na área dele, violações de política impedem a
validação e quem criou uma versão não pode publicá-la.

Publicar é aprovar: cada aprovação fica em ``version_approvals``. Uma versão
precisa de uma aprovação, ou de duas, de pessoas diferentes, quando o manifesto
declara um certificado digital A1 (:func:`rapa.core.contracts.required_approvals`).
Cada aprovador tem o papel de aprovação na área, não criou a versão e não aprovou
antes. Num robô com A1, o aprovador também não é o custodiante de nenhum dos
certificados dele, nem a versão foi criada por um desses custodiantes (separação de
funções: a autorização do custodiante é do robô, e valeria para uma versão que ele
mesmo escreveu); e cada certificado precisa estar pronto para uso
(:func:`rapa.control.services.a1.a1_problems`) antes de a aprovação contar. Enquanto
faltar aprovação, a versão continua validada; a última aprovação publica. O banco
confere de novo (os gatilhos ``rapa_version_approvals_check`` e
``rapa_robot_versions_approvals``).
"""

import re
import unicodedata
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import areas, robot_versions, robots, version_approvals
from rapa.control.services import audit
from rapa.control.services._common import (
    PgService,
    ensure_company_active,
    now_utc,
    summary,
    version_info,
    visible_areas,
)
from rapa.control.services.a1 import BLOCKED_MESSAGE, a1_custodians, a1_problems, a1_refs
from rapa.core import policy
from rapa.core.contracts import (
    CertificateBlocked,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    RobotInfo,
    RobotStatus,
    RobotSummary,
    RobotVersionInfo,
    Role,
    VersionStatus,
    required_approvals,
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = [
    "ALREADY_APPROVED",
    "CUSTODIAN_APPROVER",
    "CUSTODIAN_CREATOR",
    "PgRobotAdmin",
    "PgRobotCatalog",
    "approvers_of",
    "company_published_query",
    "fold_text",
    "insert_version",
    "published_query",
    "published_robot",
]

_VERSION_COLUMNS = (
    robot_versions.c.id,
    robot_versions.c.version_no,
    robot_versions.c.status,
    robot_versions.c.manifest,
    robot_versions.c.fingerprint,
    robot_versions.c.violations,
    robot_versions.c.created_by,
    robot_versions.c.approved_by,
    robot_versions.c.created_at,
)

#: Mesma pessoa aprovando a mesma versão de novo.
ALREADY_APPROVED = "você já aprovou esta versão; falta a aprovação de outra pessoa"
#: O custodiante de um certificado A1 do robô tentando aprovar (separação de funções).
CUSTODIAN_APPROVER = (
    "quem guarda um certificado A1 do robô (o custodiante) não pode aprovar a versão: "
    "a aprovação é de outra pessoa (separação de funções)"
)
#: Uma versão criada pelo custodiante de um certificado A1 do robô (separação de funções).
CUSTODIAN_CREATOR = (
    "esta versão foi criada por quem guarda um certificado A1 do robô (o custodiante) e "
    "não pode ser aprovada: outra pessoa precisa criar a versão (separação de funções)"
)

_MAX_SEARCH_TERMS = 16
_MAX_SEARCH_LIMIT = 100
_WORD = re.compile(r"[a-z0-9]+")


def fold_text(text: str) -> str:
    """Minúsculas sem acentos, como a função SQL ``rapa_fold``."""
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    return stripped.casefold().replace("_", " ")


def published_query(principal: Principal) -> Select[Any]:
    """Robôs publicados visíveis para o principal, com a versão atual."""
    query = company_published_query(principal.company_id)
    allowed = visible_areas(principal)
    if allowed is not None:
        query = query.where(robots.c.area_id.in_(allowed))
    return query


def company_published_query(company_id: UUID) -> Select[Any]:
    """Robôs publicados da empresa, com a versão atual (sem filtrar pelas áreas de alguém).

    Serve às agendas e aos webhooks, que rodam em nome da área do robô.
    """
    return (
        select(
            robots.c.id.label("robot_id"),
            robots.c.company_id,
            robots.c.area_id,
            robots.c.slug,
            robot_versions.c.id.label("version_id"),
            robot_versions.c.version_no,
            robot_versions.c.manifest,
            robot_versions.c.fingerprint,
        )
        .select_from(
            robots.join(
                robot_versions,
                and_(
                    robot_versions.c.robot_id == robots.c.id,
                    robot_versions.c.version_no == robots.c.current_version_no,
                ),
            )
        )
        .where(
            robots.c.company_id == company_id,
            robots.c.status == RobotStatus.PUBLISHED.value,
            robot_versions.c.status == VersionStatus.PUBLISHED.value,
        )
    )


async def published_robot(
    session: AsyncSession, company_id: UUID, robot_id: UUID
) -> RowMapping | None:
    """O robô publicado da empresa, com a versão atual (``manifest`` incluído), ou ``None``."""
    query = company_published_query(company_id).where(robots.c.id == robot_id)
    row: RowMapping | None = (await session.execute(query)).mappings().one_or_none()
    return row


async def approvers_of(
    session: AsyncSession, company_id: UUID, robot_id: UUID, version_no: int | None = None
) -> dict[int, list[tuple[str, Any]]]:
    """Os aprovadores de cada versão do robô (ou só de ``version_no``), na ordem."""
    query = (
        select(
            version_approvals.c.version_no,
            version_approvals.c.approved_by,
            version_approvals.c.approved_at,
        )
        .where(
            version_approvals.c.company_id == company_id,
            version_approvals.c.robot_id == robot_id,
        )
        .order_by(version_approvals.c.approved_at, version_approvals.c.approved_by)
    )
    if version_no is not None:
        query = query.where(version_approvals.c.version_no == version_no)
    out: dict[int, list[tuple[str, Any]]] = {}
    for number, who, when in (await session.execute(query)).all():
        out.setdefault(int(number), []).append((str(who), when))
    return out


async def insert_version(
    session: AsyncSession, principal: Principal, area_id: UUID, manifest: Manifest
) -> RobotVersionInfo:
    """Cria a próxima versão (rascunho) do robô dentro da transação de ``session``.

    O mesmo caminho para quem grava, para a revisão da gravação e para o conserto:
    só quem cria robôs na área cria versões, o portão de política roda e as violações
    ficam na versão (que então não pode ser validada), e a criação entra na auditoria.
    Quem criou a versão nunca a publica. Empresa congelada não ganha versão nova.
    """
    if not principal.has_role(Role.CREATOR, area_id):
        raise Forbidden("só quem cria robôs nesta área pode criar versões")
    await ensure_company_active(session, principal.company_id)
    violations = [v.message for v in policy.check(manifest)]
    fingerprint = manifest.fingerprint()
    company_id = principal.company_id
    area = await session.scalar(
        select(areas.c.id).where(areas.c.id == area_id, areas.c.company_id == company_id)
    )
    if area is None:
        raise NotFound("área não encontrada")
    await session.execute(
        pg_insert(robots)
        .values(
            id=new_id(),
            company_id=company_id,
            area_id=area_id,
            slug=manifest.name,
            title=manifest.title,
            status=RobotStatus.DRAFT.value,
        )
        .on_conflict_do_nothing(index_elements=["company_id", "slug"])
    )
    robot = (
        (
            await session.execute(
                select(robots)
                .where(robots.c.company_id == company_id, robots.c.slug == manifest.name)
                .with_for_update()
            )
        )
        .mappings()
        .one()
    )
    if robot["area_id"] != area_id:
        raise Conflict("já existe um robô com este nome em outra área")
    robot_id: UUID = robot["id"]
    last = await session.scalar(
        select(func.coalesce(func.max(robot_versions.c.version_no), 0)).where(
            robot_versions.c.robot_id == robot_id
        )
    )
    version_no = int(last or 0) + 1
    row = (
        (
            await session.execute(
                insert(robot_versions)
                .values(
                    id=new_id(),
                    company_id=company_id,
                    robot_id=robot_id,
                    version_no=version_no,
                    status=VersionStatus.DRAFT.value,
                    manifest=manifest.model_dump(mode="json"),
                    fingerprint=fingerprint,
                    violations=violations,
                    created_by=principal.subject,
                )
                .returning(*_VERSION_COLUMNS)
            )
        )
        .mappings()
        .one()
    )
    await session.execute(
        update(robots)
        .where(robots.c.id == robot_id)
        .values(title=manifest.title, updated_at=func.now())
    )
    await audit.append(
        session,
        company_id=company_id,
        actor=principal.subject,
        action="robo.versao_criada",
        target=f"robo:{robot_id}/versao:{version_no}",
        payload={"impressao": fingerprint, "violacoes": len(violations)},
    )
    return version_info(robot_id, row)


class PgRobotAdmin(PgService):
    """Ciclo de vida rascunho → validado → publicado (:class:`RobotAdmin`)."""

    async def _robot(
        self, session: AsyncSession, principal: Principal, robot_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        query = select(robots).where(
            robots.c.id == robot_id, robots.c.company_id == principal.company_id
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("robô não encontrado")
        return row

    @staticmethod
    async def _version(
        session: AsyncSession, robot_id: UUID, version_no: int, *, lock: bool = False
    ) -> RowMapping:
        query = select(*_VERSION_COLUMNS).where(
            robot_versions.c.robot_id == robot_id, robot_versions.c.version_no == version_no
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise NotFound("versão não encontrada")
        return row

    async def list_robots(self, principal: Principal) -> list[RobotInfo]:
        query = (
            select(robots)
            .where(robots.c.company_id == principal.company_id)
            .order_by(robots.c.slug)
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(robots.c.area_id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [
            RobotInfo(
                robot_id=row["id"],
                company_id=row["company_id"],
                area_id=row["area_id"],
                slug=row["slug"],
                title=row["title"],
                status=RobotStatus(row["status"]),
                current_version_no=row["current_version_no"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def create_version(
        self, principal: Principal, area_id: UUID, manifest: Manifest
    ) -> RobotVersionInfo:
        async with self.db.tenant_session(principal.company_id) as session:
            return await insert_version(session, principal, area_id, manifest)

    async def get_version(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            try:
                await self._robot(session, principal, robot_id)
                row = await self._version(session, robot_id, version_no)
            except NotFound:
                return None
            approvers = await approvers_of(session, principal.company_id, robot_id, version_no)
        return version_info(robot_id, row, [who for who, _ in approvers.get(version_no, [])])

    async def list_versions(self, principal: Principal, robot_id: UUID) -> list[RobotVersionInfo]:
        async with self.db.tenant_session(principal.company_id) as session:
            await self._robot(session, principal, robot_id)
            rows = (
                (
                    await session.execute(
                        select(*_VERSION_COLUMNS)
                        .where(robot_versions.c.robot_id == robot_id)
                        .order_by(robot_versions.c.version_no)
                    )
                )
                .mappings()
                .all()
            )
            approvers = await approvers_of(session, principal.company_id, robot_id)
        return [
            version_info(
                robot_id, row, [who for who, _ in approvers.get(int(row["version_no"]), [])]
            )
            for row in rows
        ]

    async def mark_validated(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo:
        async with self.db.tenant_session(principal.company_id) as session:
            await ensure_company_active(session, principal.company_id)
            robot = await self._robot(session, principal, robot_id, lock=True)
            if not (
                principal.has_role(Role.CREATOR, robot["area_id"])
                or principal.has_role(Role.APPROVER, robot["area_id"])
            ):
                raise Forbidden("sem permissão para validar")
            version = await self._version(session, robot_id, version_no, lock=True)
            if version["violations"]:
                raise InvalidInput(
                    "a versão tem violações de política", list(version["violations"])
                )
            if version["status"] != VersionStatus.DRAFT.value:
                raise Conflict("só um rascunho pode ser marcado como validado")
            row = (
                (
                    await session.execute(
                        update(robot_versions)
                        .where(robot_versions.c.id == version["id"])
                        .values(status=VersionStatus.VALIDATED.value)
                        .returning(*_VERSION_COLUMNS)
                    )
                )
                .mappings()
                .one()
            )
            await audit.append(
                session,
                company_id=principal.company_id,
                actor=principal.subject,
                action="robo.versao_validada",
                target=f"robo:{robot_id}/versao:{version_no}",
                payload={"impressao": version["fingerprint"]},
            )
        return version_info(robot_id, row)

    async def publish(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo:
        """Registra a aprovação de quem chama; a última aprovação exigida publica."""
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            robot = await self._robot(session, principal, robot_id, lock=True)
            area_id: UUID = robot["area_id"]
            if not principal.has_role(Role.APPROVER, area_id):
                raise Forbidden("só quem aprova nesta área pode publicar")
            version = await self._version(session, robot_id, version_no, lock=True)
            if version["created_by"] == principal.subject:
                raise Forbidden("quem criou a versão não pode aprová-la")
            if version["status"] != VersionStatus.VALIDATED.value:
                raise Conflict("a versão precisa estar validada antes de publicar")
            manifest = Manifest.model_validate(version["manifest"])
            required = required_approvals(manifest)
            earlier = [
                who
                for who, _ in (await approvers_of(session, company_id, robot_id, version_no)).get(
                    version_no, []
                )
            ]
            if principal.subject in earlier:
                raise Conflict(ALREADY_APPROVED)
            refs = a1_refs(manifest)
            if refs:
                custodians = await a1_custodians(
                    session, company_id=company_id, area_id=area_id, names=[r.name for r in refs]
                )
                if principal.subject in custodians:
                    raise Forbidden(CUSTODIAN_APPROVER)
                # A autorização do custodiante é do robô, não da versão: uma versão que ele
                # mesmo escreveu rodaria com a autorização dele. Ela não é aprovada.
                if version["created_by"] in custodians:
                    raise Forbidden(CUSTODIAN_CREATOR)
                problems = await a1_problems(
                    session,
                    company_id=company_id,
                    area_id=area_id,
                    robot_id=robot_id,
                    manifest=manifest,
                    now=now_utc(),
                )
                if problems:
                    raise CertificateBlocked(
                        f"{BLOCKED_MESSAGE}; a aprovação não foi registrada", problems
                    )
            await session.execute(
                insert(version_approvals).values(
                    company_id=company_id,
                    robot_id=robot_id,
                    version_no=version_no,
                    approved_by=principal.subject,
                )
            )
            approvals = [*earlier, principal.subject]
            if len(approvals) < required:
                await audit.append(
                    session,
                    company_id=company_id,
                    actor=principal.subject,
                    action="robo.versao_aprovada",
                    target=f"robo:{robot_id}/versao:{version_no}",
                    payload={
                        "impressao": version["fingerprint"],
                        "aprovacoes": len(approvals),
                        "exigidas": required,
                    },
                )
                return version_info(robot_id, version, approvals)
            await session.execute(
                update(robot_versions)
                .where(
                    robot_versions.c.robot_id == robot_id,
                    robot_versions.c.status == VersionStatus.PUBLISHED.value,
                )
                .values(status=VersionStatus.SUPERSEDED.value)
            )
            row = (
                (
                    await session.execute(
                        update(robot_versions)
                        .where(robot_versions.c.id == version["id"])
                        .values(status=VersionStatus.PUBLISHED.value, approved_by=principal.subject)
                        .returning(*_VERSION_COLUMNS)
                    )
                )
                .mappings()
                .one()
            )
            await session.execute(
                update(robots)
                .where(robots.c.id == robot_id)
                .values(
                    status=RobotStatus.PUBLISHED.value,
                    current_version_no=version_no,
                    updated_at=func.now(),
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="robo.versao_publicada",
                target=f"robo:{robot_id}/versao:{version_no}",
                payload={
                    "impressao": version["fingerprint"],
                    "criada_por": version["created_by"],
                    "aprovadores": approvals,
                },
            )
        return version_info(robot_id, row, approvals)


class PgRobotCatalog(PgService):
    """Robôs publicados visíveis para um principal (:class:`RobotCatalog`)."""

    async def list_published(self, principal: Principal) -> list[RobotSummary]:
        query = published_query(principal).order_by(robots.c.slug)
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [summary(row) for row in rows]

    async def get_published(self, principal: Principal, slug: str) -> RobotSummary | None:
        query = published_query(principal).where(robots.c.slug == slug)
        async with self.db.tenant_session(principal.company_id) as session:
            row = (await session.execute(query)).mappings().one_or_none()
        return None if row is None else summary(row)

    async def search(self, principal: Principal, query: str, limit: int = 10) -> list[RobotSummary]:
        """Busca em português (com radicais e prefixos), sem depender de acentos.

        Cada palavra da consulta conta separadamente (basta uma bater), e o
        resultado vem ordenado pela relevância e depois pelo nome técnico.
        """
        limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
        terms = _WORD.findall(fold_text(query))[:_MAX_SEARCH_TERMS]
        if not terms:
            return (await self.list_published(principal))[:limit]
        tsquery = func.to_tsquery(
            literal_column("'portuguese'::regconfig"), " | ".join(f"{t}:*" for t in terms)
        )
        document = robot_versions.c.search_document
        rank = func.ts_rank(document, tsquery)
        statement = (
            published_query(principal)
            .where(document.op("@@")(tsquery))
            .order_by(rank.desc(), robots.c.slug)
            .limit(limit)
        )
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(statement)).mappings().all()
        return [summary(row) for row in rows]
