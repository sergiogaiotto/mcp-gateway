"""Execuções recentes de cada pessoa, para a tela Execuções.

A API ainda não lista execuções; a interface guarda os identificadores das
execuções que cada pessoa iniciou por ela e consulta o estado de cada uma na
API. A chave é sempre a empresa e o ``subject`` do principal autenticado.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from rapa.core.contracts import Principal

__all__ = ["MemoryRunHistory", "RecentRun", "RunHistory"]


@dataclass(frozen=True, slots=True)
class RecentRun:
    run_id: UUID
    robot_slug: str
    robot_title: str
    started_at: datetime


class RunHistory(Protocol):
    async def record(self, principal: Principal, run: RecentRun) -> None: ...

    async def recent(self, principal: Principal, limit: int = 20) -> list[RecentRun]: ...


class MemoryRunHistory:
    """Histórico em memória, até ``keep`` execuções por pessoa."""

    def __init__(self, keep: int = 100) -> None:
        self._keep = keep
        self._runs: dict[tuple[UUID, str], list[RecentRun]] = {}

    async def record(self, principal: Principal, run: RecentRun) -> None:
        runs = self._runs.setdefault((principal.company_id, principal.subject), [])
        runs[:] = [r for r in runs if r.run_id != run.run_id]
        runs.insert(0, run)
        del runs[self._keep :]

    async def recent(self, principal: Principal, limit: int = 20) -> list[RecentRun]:
        return list(self._runs.get((principal.company_id, principal.subject), [])[:limit])
