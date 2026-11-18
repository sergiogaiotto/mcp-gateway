"""Administração e catálogo de robôs no PostgreSQL.

Mesmas regras de :class:`rapa.fixtures.memory.MemoryPlatform`: tudo é filtrado
pela empresa do principal (e pelo isolamento por linha do banco), um robô só é
visível para quem tem papel na área dele, violações de política impedem a
validação e quem criou uma versão não pode publicá-la.
"""

import re
import unicodedata
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import areas, robot_versions, robots
from rapa.control.services import audit
from rapa.control.services._common import PgService, summary, version_info, visible_areas
from rapa.core import policy
from rapa.core.contracts import (
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
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = ["PgRobotAdmin", "PgRobotCatalog", "fold_text", "published_query"]

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
    query = (
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
            robots.c.company_id == principal.company_id,
            robots.c.status == RobotStatus.PUBLISHED.value,
            robot_versions.c.status == VersionStatus.PUBLISHED.value,
        )
    )
    allowed = visible_areas(principal)
    if allowed is not None:
        query = query.where(robots.c.area_id.in_(allowed))
    return query


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
        if not principal.has_role(Role.CREATOR, area_id):
            raise Forbidden("só quem cria robôs nesta área pode criar versões")
        violations = [v.message for v in policy.check(manifest)]
        fingerprint = manifest.fingerprint()
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
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

    async def get_version(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            try:
                await self._robot(session, principal, robot_id)
                row = await self._version(session, robot_id, version_no)
            except NotFound:
                return None
        return version_info(robot_id, row)

    async def mark_validated(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo:
        async with self.db.tenant_session(principal.company_id) as session:
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
        async with self.db.tenant_session(principal.company_id) as session:
            robot = await self._robot(session, principal, robot_id, lock=True)
            if not principal.has_role(Role.APPROVER, robot["area_id"]):
                raise Forbidden("só quem aprova nesta área pode publicar")
            version = await self._version(session, robot_id, version_no, lock=True)
            if version["created_by"] == principal.subject:
                raise Forbidden("quem criou a versão não pode aprová-la")
            if version["status"] != VersionStatus.VALIDATED.value:
                raise Conflict("a versão precisa estar validada antes de publicar")
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
                company_id=principal.company_id,
                actor=principal.subject,
                action="robo.versao_publicada",
                target=f"robo:{robot_id}/versao:{version_no}",
                payload={"impressao": version["fingerprint"], "criada_por": version["created_by"]},
            )
        return version_info(robot_id, row)


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
