"""Sessões de gravação e gravações no PostgreSQL (:class:`PgSessionStore`).

Tudo roda em transações fixadas na empresa (isolamento por linha). A contagem
das sessões abertas da empresa e a inclusão da nova rodam sob uma trava
consultiva da empresa, então duas aberturas ao mesmo tempo não passam juntas do
limite. Guardar a gravação, marcar a sessão como parada e auditar ficam na mesma
transação; criar, parar e o rascunho entram na auditoria encadeada da empresa
(o conteúdo da gravação, nunca: só o resumo, como em toda auditoria).
"""

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import areas, platform_settings, recording_sessions, recordings
from rapa.control.services import audit
from rapa.control.services._common import PgService
from rapa.control.services.admission import Capacity
from rapa.control.sessions.store import LIVE_STATUSES, SessionRecord, company_limit_error
from rapa.core.contracts import Conflict, RecordingSessionStatus
from rapa.core.ids import new_id

__all__ = ["PgSessionStore"]

#: Trava consultiva da abertura de sessões (segundo argumento: a empresa).
SESSION_LOCK_KEY = 7104

_LIVE = [s.value for s in LIVE_STATUSES]


def _query() -> Select[Any]:
    return select(
        recording_sessions,
        recordings.c.id.label("recording_id"),
    ).select_from(
        recording_sessions.outerjoin(
            recordings,
            (recordings.c.company_id == recording_sessions.c.company_id)
            & (recordings.c.session_id == recording_sessions.c.id),
        )
    )


def _record(row: RowMapping) -> SessionRecord:
    return SessionRecord(
        company_id=row["company_id"],
        session_id=row["id"],
        area_id=row["area_id"],
        owner=row["owner"],
        title=row["title"],
        start_url=row["start_url"],
        status=RecordingSessionStatus(row["status"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        started_at=row["started_at"],
        stopped_at=row["stopped_at"],
        end_reason=row["end_reason"],
        robot_id=row["robot_id"],
        version_no=row["version_no"],
        recording_id=row["recording_id"],
    )


class PgSessionStore(PgService):
    """Armazenamento das sessões de gravação (:class:`rapa.control.sessions.store.SessionStore`)."""

    async def area_exists(self, company_id: UUID, area_id: UUID) -> bool:
        async with self.db.tenant_session(company_id) as session:
            found = await session.scalar(
                select(areas.c.id).where(areas.c.company_id == company_id, areas.c.id == area_id)
            )
        return found is not None

    async def interactive_reserve(self) -> int | None:
        async with self.db.dispatcher_session() as session:
            row = (
                await session.execute(
                    select(
                        platform_settings.c.total_browser_slots,
                        platform_settings.c.interactive_reserve_ratio,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return Capacity(int(row[0]), row[1]).reserve

    async def _get(
        self, session: AsyncSession, company_id: UUID, session_id: UUID
    ) -> SessionRecord | None:
        row = (
            (
                await session.execute(
                    _query().where(
                        recording_sessions.c.company_id == company_id,
                        recording_sessions.c.id == session_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _record(row)

    async def insert(self, record: SessionRecord, *, company_limit: int) -> None:
        company_id = record.company_id
        async with self.db.tenant_session(company_id) as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key, hashtext(:company))"),
                {"key": SESSION_LOCK_KEY, "company": str(company_id)},
            )
            live = await session.scalar(
                select(func.count())
                .select_from(recording_sessions)
                .where(
                    recording_sessions.c.company_id == company_id,
                    recording_sessions.c.status.in_(_LIVE),
                    recording_sessions.c.expires_at > record.created_at,
                )
            )
            if int(live or 0) >= company_limit:
                raise company_limit_error(company_limit)
            await session.execute(
                insert(recording_sessions).values(
                    company_id=company_id,
                    id=record.session_id,
                    area_id=record.area_id,
                    owner=record.owner,
                    title=record.title,
                    start_url=record.start_url,
                    status=record.status.value,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=record.owner,
                action="gravacao.sessao_criada",
                target=f"sessao:{record.session_id}",
                payload={"area": str(record.area_id), "expira_em": record.expires_at.isoformat()},
            )

    async def get(self, company_id: UUID, session_id: UUID) -> SessionRecord | None:
        async with self.db.tenant_session(company_id) as session:
            return await self._get(session, company_id, session_id)

    async def list_owned(self, company_id: UUID, owner: str, limit: int) -> list[SessionRecord]:
        query = (
            _query()
            .where(
                recording_sessions.c.company_id == company_id,
                recording_sessions.c.owner == owner,
            )
            .order_by(recording_sessions.c.created_at.desc(), recording_sessions.c.id)
            .limit(limit)
        )
        async with self.db.tenant_session(company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [_record(row) for row in rows]

    async def list_live(self, company_id: UUID) -> list[SessionRecord]:
        query = _query().where(
            recording_sessions.c.company_id == company_id,
            recording_sessions.c.status.in_(_LIVE),
        )
        async with self.db.tenant_session(company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [_record(row) for row in rows]

    async def set_status(
        self,
        company_id: UUID,
        session_id: UUID,
        *,
        expected: Collection[RecordingSessionStatus],
        status: RecordingSessionStatus,
        end_reason: str | None = None,
        started_at: datetime | None = None,
        stopped_at: datetime | None = None,
    ) -> SessionRecord | None:
        values: dict[str, Any] = {"status": status.value}
        if end_reason is not None:
            values["end_reason"] = end_reason
        if started_at is not None:
            values["started_at"] = started_at
        if stopped_at is not None:
            values["stopped_at"] = stopped_at
        async with self.db.tenant_session(company_id) as session:
            result = await session.execute(
                update(recording_sessions)
                .where(
                    recording_sessions.c.company_id == company_id,
                    recording_sessions.c.id == session_id,
                    recording_sessions.c.status.in_([s.value for s in expected]),
                )
                .values(**values)
                .returning(recording_sessions.c.id)
            )
            if result.first() is None:
                return None
            return await self._get(session, company_id, session_id)

    async def save_recording(
        self,
        company_id: UUID,
        session_id: UUID,
        recording: Mapping[str, Any],
        *,
        actor: str,
        stopped_at: datetime,
    ) -> SessionRecord:
        async with self.db.tenant_session(company_id) as session:
            result = await session.execute(
                update(recording_sessions)
                .where(
                    recording_sessions.c.company_id == company_id,
                    recording_sessions.c.id == session_id,
                    recording_sessions.c.status.in_(_LIVE),
                )
                .values(status=RecordingSessionStatus.STOPPED.value, stopped_at=stopped_at)
                .returning(recording_sessions.c.id)
            )
            if result.first() is None:
                raise Conflict("a sessão de gravação já terminou")
            recording_id = new_id()
            await session.execute(
                insert(recordings).values(
                    company_id=company_id,
                    id=recording_id,
                    session_id=session_id,
                    created_by=actor,
                    recording=dict(recording),
                )
            )
            events = recording.get("events")
            await audit.append(
                session,
                company_id=company_id,
                actor=actor,
                action="gravacao.sessao_parada",
                target=f"sessao:{session_id}",
                payload={
                    "gravacao": str(recording_id),
                    "eventos": len(events) if isinstance(events, list) else 0,
                },
            )
            record = await self._get(session, company_id, session_id)
        if record is None:  # pragma: no cover - a linha acabou de ser atualizada
            raise Conflict("a sessão de gravação sumiu")
        return record

    async def get_recording(
        self, company_id: UUID, session_id: UUID
    ) -> tuple[UUID, dict[str, Any]] | None:
        async with self.db.tenant_session(company_id) as session:
            row = (
                await session.execute(
                    select(recordings.c.id, recordings.c.recording).where(
                        recordings.c.company_id == company_id,
                        recordings.c.session_id == session_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return row[0], dict(row[1])

    async def mark_drafted(
        self,
        company_id: UUID,
        session_id: UUID,
        *,
        robot_id: UUID,
        version_no: int,
        actor: str,
    ) -> SessionRecord:
        async with self.db.tenant_session(company_id) as session:
            result = await session.execute(
                update(recording_sessions)
                .where(
                    recording_sessions.c.company_id == company_id,
                    recording_sessions.c.id == session_id,
                    recording_sessions.c.status.in_(
                        [RecordingSessionStatus.STOPPED.value, RecordingSessionStatus.DRAFTED.value]
                    ),
                )
                .values(
                    status=RecordingSessionStatus.DRAFTED.value,
                    robot_id=robot_id,
                    version_no=version_no,
                )
                .returning(recording_sessions.c.id)
            )
            if result.first() is None:
                raise Conflict("a sessão de gravação não tem gravação para revisar")
            await audit.append(
                session,
                company_id=company_id,
                actor=actor,
                action="gravacao.rascunho_criado",
                target=f"sessao:{session_id}",
                payload={"robo": str(robot_id), "versao": version_no},
            )
            record = await self._get(session, company_id, session_id)
        if record is None:  # pragma: no cover - a linha acabou de ser atualizada
            raise Conflict("a sessão de gravação sumiu")
        return record
