"""Bilhetes de uso único da visão ao vivo.

O navegador da pessoa não manda o token de acesso no websocket: ele recebe da
interface (que pediu à API, já autenticada) um bilhete aleatório que vale por
60 s e uma vez só, para uma sessão. Os bilhetes ficam só na memória deste
processo, guardados pelo SHA-256 (nunca o valor), e nunca vão para o banco nem
para o log. Quem resgata recebe o principal que pediu o bilhete, para a API
conferir de novo a sessão e os papéis.
"""

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from rapa.core.contracts import Principal

__all__ = ["TICKET_TTL_S", "TicketBook", "TicketGrant"]

TICKET_TTL_S = 60.0
_MAX_PER_SESSION = 5


@dataclass(frozen=True, slots=True)
class TicketGrant:
    session_id: UUID
    principal: Principal
    expires_at: float


def _digest(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode()).digest()


class TicketBook:
    """Emite e resgata bilhetes (cada um, uma vez e dentro do prazo)."""

    def __init__(
        self, *, ttl_s: float = TICKET_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._grants: dict[bytes, TicketGrant] = {}

    def __len__(self) -> int:
        return len(self._grants)

    def _prune(self, now: float) -> None:
        for key in [k for k, grant in self._grants.items() if grant.expires_at <= now]:
            del self._grants[key]

    def issue(self, session_id: UUID, principal: Principal) -> tuple[str, float]:
        """Novo bilhete e o instante (do relógio monotônico) em que ele vence."""
        now = self._clock()
        self._prune(now)
        same = [k for k, g in self._grants.items() if g.session_id == session_id]
        for key in same[: max(0, len(same) - _MAX_PER_SESSION + 1)]:
            del self._grants[key]
        ticket = secrets.token_urlsafe(32)
        expires_at = now + self.ttl_s
        self._grants[_digest(ticket)] = TicketGrant(session_id, principal, expires_at)
        return ticket, expires_at

    def redeem(self, ticket: str, session_id: UUID) -> TicketGrant | None:
        """O que o bilhete dá, uma vez só; None se não existe, venceu ou é de outra sessão."""
        if not ticket or len(ticket) > 200:
            return None
        grant = self._grants.pop(_digest(ticket), None)
        if grant is None or grant.expires_at <= self._clock() or grant.session_id != session_id:
            return None
        return grant

    def revoke(self, session_id: UUID) -> None:
        for key in [k for k, g in self._grants.items() if g.session_id == session_id]:
            del self._grants[key]
