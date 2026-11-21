"""Onde ficam as sessões de gravação e as gravações: o protocolo e a versão em memória.

As regras (quem pode, limites, situações) ficam em
:class:`rapa.control.sessions.service.RecordingSessions`; o armazenamento só
guarda, sempre dentro da empresa informada, e faz de forma atômica o que precisa
ser atômico: contar as sessões abertas da empresa e incluir a nova, guardar a
gravação e marcar a sessão como parada, registrar cada passo na auditoria.

A implementação de produção é :class:`rapa.control.services.sessions.PgSessionStore`
(PostgreSQL, com isolamento por linha). :class:`MemorySessionStore` segue as mesmas
regras, para os testes da interface e o modo de demonstração em memória.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from rapa.core.contracts import (
    Conflict,
    QuotaExceeded,
    RecordingSessionInfo,
    RecordingSessionStatus,
)
from rapa.core.ids import new_id

__all__ = [
    "LIVE_STATUSES",
    "MemorySessionStore",
    "SessionRecord",
    "SessionStore",
    "company_limit_error",
]

LIVE_STATUSES = frozenset({RecordingSessionStatus.STARTING, RecordingSessionStatus.ACTIVE})


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Uma linha de ``recording_sessions`` (com o número da gravação, se houver)."""

    company_id: UUID
    session_id: UUID
    area_id: UUID
    owner: str
    title: str
    start_url: str
    status: RecordingSessionStatus
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    end_reason: str | None = None
    robot_id: UUID | None = None
    version_no: int | None = None
    recording_id: UUID | None = None
    #: O processo da API que segura o navegador ("" se nenhum disse).
    instance_id: str = ""
    #: Última vez que esse processo confirmou a sessão (None: na criação).
    seen_at: datetime | None = None

    @property
    def last_seen(self) -> datetime:
        return self.seen_at or self.created_at

    def info(self) -> RecordingSessionInfo:
        return RecordingSessionInfo(
            session_id=self.session_id,
            company_id=self.company_id,
            area_id=self.area_id,
            owner=self.owner,
            title=self.title,
            start_url=self.start_url,
            status=self.status,
            created_at=self.created_at,
            expires_at=self.expires_at,
            started_at=self.started_at,
            stopped_at=self.stopped_at,
            end_reason=self.end_reason,
            recording_id=self.recording_id,
            robot_id=self.robot_id,
            version_no=self.version_no,
        )


def company_limit_error(limit: int) -> QuotaExceeded:
    noun = "gravação aberta" if limit == 1 else "gravações abertas"
    return QuotaExceeded(
        f"sua empresa já tem {limit} {noun}; pare uma delas ou espere terminar", 30
    )


class SessionStore(Protocol):
    """Armazenamento das sessões de gravação (sempre dentro da empresa informada)."""

    async def area_exists(self, company_id: UUID, area_id: UUID) -> bool: ...

    async def interactive_reserve(self) -> int | None:
        """Vagas da reserva interativa da plataforma (``None`` se não configurada)."""
        ...

    async def insert(self, record: SessionRecord, *, company_limit: int) -> None:
        """Inclui a sessão se a empresa tiver menos de ``company_limit`` abertas (e audita)."""
        ...

    async def get(self, company_id: UUID, session_id: UUID) -> SessionRecord | None: ...

    async def list_owned(self, company_id: UUID, owner: str, limit: int) -> list[SessionRecord]:
        """Sessões da pessoa, das mais novas às mais antigas."""
        ...

    async def list_live(self, company_id: UUID) -> list[SessionRecord]: ...

    async def reserve_in_use(self, *, now: datetime) -> int:
        """Navegadores da reserva interativa em uso na plataforma: as sessões abertas (de
        todas as empresas) e as execuções interativas (validações) rodando."""
        ...

    async def touch(
        self, company_id: UUID, session_ids: Collection[UUID], *, instance_id: str, at: datetime
    ) -> None:
        """Renova ``seen_at`` das sessões abertas que este processo segura."""
        ...

    async def list_stale(self, *, before: datetime) -> list[tuple[UUID, UUID]]:
        """Sessões abertas (empresa, sessão), de qualquer empresa, sem renovação desde ``before``."""
        ...

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
        """Muda a situação só se a atual estiver em ``expected``; devolve a sessão ou None."""
        ...

    async def save_recording(
        self,
        company_id: UUID,
        session_id: UUID,
        recording: Mapping[str, Any],
        *,
        actor: str,
        stopped_at: datetime,
    ) -> SessionRecord:
        """Guarda a gravação e marca a sessão como parada (e audita), na mesma transação."""
        ...

    async def get_recording(
        self, company_id: UUID, session_id: UUID
    ) -> tuple[UUID, dict[str, Any]] | None: ...

    async def mark_drafted(
        self,
        company_id: UUID,
        session_id: UUID,
        *,
        robot_id: UUID,
        version_no: int,
        actor: str,
    ) -> SessionRecord:
        """Liga a sessão ao rascunho criado (e audita)."""
        ...


@dataclass
class _Audit:
    company_id: UUID
    actor: str
    action: str
    target: str


@dataclass
class MemorySessionStore:
    """Sessões e gravações em memória, com as mesmas regras do PostgreSQL."""

    reserve: int | None = 3
    #: Execuções interativas (validações) rodando na reserva, para os testes.
    interactive_runs: int = 0
    areas: set[tuple[UUID, UUID]] = field(default_factory=set)
    sessions: dict[tuple[UUID, UUID], SessionRecord] = field(default_factory=dict)
    recordings: dict[tuple[UUID, UUID], tuple[UUID, dict[str, Any]]] = field(default_factory=dict)
    audit: list[_Audit] = field(default_factory=list)

    def add_area(self, company_id: UUID, area_id: UUID) -> None:
        self.areas.add((company_id, area_id))

    async def area_exists(self, company_id: UUID, area_id: UUID) -> bool:
        return (company_id, area_id) in self.areas

    async def interactive_reserve(self) -> int | None:
        return self.reserve

    async def insert(self, record: SessionRecord, *, company_limit: int) -> None:
        live = [
            s
            for s in self.sessions.values()
            if s.company_id == record.company_id
            and s.status in LIVE_STATUSES
            and s.expires_at > record.created_at
        ]
        if len(live) >= company_limit:
            raise company_limit_error(company_limit)
        self.sessions[record.company_id, record.session_id] = replace(
            record, seen_at=record.last_seen
        )
        self._audit(record.company_id, record.owner, "gravacao.sessao_criada", record)

    def _audit(self, company_id: UUID, actor: str, action: str, record: SessionRecord) -> None:
        self.audit.append(_Audit(company_id, actor, action, f"sessao:{record.session_id}"))

    async def get(self, company_id: UUID, session_id: UUID) -> SessionRecord | None:
        return self.sessions.get((company_id, session_id))

    async def list_owned(self, company_id: UUID, owner: str, limit: int) -> list[SessionRecord]:
        owned = [
            s for s in self.sessions.values() if s.company_id == company_id and s.owner == owner
        ]
        return sorted(owned, key=lambda s: s.created_at, reverse=True)[:limit]

    async def list_live(self, company_id: UUID) -> list[SessionRecord]:
        return [
            s
            for s in self.sessions.values()
            if s.company_id == company_id and s.status in LIVE_STATUSES
        ]

    async def reserve_in_use(self, *, now: datetime) -> int:
        live = [
            s for s in self.sessions.values() if s.status in LIVE_STATUSES and s.expires_at > now
        ]
        return len(live) + self.interactive_runs

    async def touch(
        self, company_id: UUID, session_ids: Collection[UUID], *, instance_id: str, at: datetime
    ) -> None:
        for session_id in session_ids:
            record = self.sessions.get((company_id, session_id))
            if (
                record is not None
                and record.status in LIVE_STATUSES
                and record.instance_id == instance_id
            ):
                self.sessions[company_id, session_id] = replace(record, seen_at=at)

    async def list_stale(self, *, before: datetime) -> list[tuple[UUID, UUID]]:
        return [
            (s.company_id, s.session_id)
            for s in self.sessions.values()
            if s.status in LIVE_STATUSES and s.last_seen < before
        ]

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
        record = self.sessions.get((company_id, session_id))
        if record is None or record.status not in expected:
            return None
        changes: dict[str, Any] = {"status": status}
        if end_reason is not None:
            changes["end_reason"] = end_reason
        if started_at is not None:
            changes["started_at"] = started_at
        if stopped_at is not None:
            changes["stopped_at"] = stopped_at
        updated = replace(record, **changes)
        self.sessions[company_id, session_id] = updated
        return updated

    async def save_recording(
        self,
        company_id: UUID,
        session_id: UUID,
        recording: Mapping[str, Any],
        *,
        actor: str,
        stopped_at: datetime,
    ) -> SessionRecord:
        record = self.sessions.get((company_id, session_id))
        if record is None or record.status not in LIVE_STATUSES:
            raise Conflict("a sessão de gravação já terminou")
        if (company_id, session_id) in self.recordings:
            raise Conflict("a sessão de gravação já tem uma gravação")
        recording_id = new_id()
        self.recordings[company_id, session_id] = (recording_id, dict(recording))
        updated = replace(
            record,
            status=RecordingSessionStatus.STOPPED,
            stopped_at=stopped_at,
            recording_id=recording_id,
        )
        self.sessions[company_id, session_id] = updated
        self._audit(company_id, actor, "gravacao.sessao_parada", updated)
        return updated

    async def get_recording(
        self, company_id: UUID, session_id: UUID
    ) -> tuple[UUID, dict[str, Any]] | None:
        found = self.recordings.get((company_id, session_id))
        return (found[0], dict(found[1])) if found is not None else None

    async def mark_drafted(
        self,
        company_id: UUID,
        session_id: UUID,
        *,
        robot_id: UUID,
        version_no: int,
        actor: str,
    ) -> SessionRecord:
        record = self.sessions.get((company_id, session_id))
        if record is None or not record.status.reviewable:
            raise Conflict("a sessão de gravação não tem gravação para revisar")
        updated = replace(
            record,
            status=RecordingSessionStatus.DRAFTED,
            robot_id=robot_id,
            version_no=version_no,
        )
        self.sessions[company_id, session_id] = updated
        self._audit(company_id, actor, "gravacao.rascunho_criado", updated)
        return updated
