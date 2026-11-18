"""Limite de requisições por empresa, em memória (balde de fichas).

Cada empresa tem um balde com ``burst`` fichas que se repõe a ``rate_per_s``
fichas por segundo. Vale para o processo; com várias réplicas, o limite
efetivo é a soma delas. Os limites de execuções simultâneas por empresa e área
ficam na fila, não aqui.
"""

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from rapa.core.contracts import QuotaExceeded

__all__ = ["RateLimiter"]


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Balde de fichas por empresa."""

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("a taxa precisa ser positiva e o balde ter pelo menos uma ficha")
        self.rate_per_s = rate_per_s
        self.burst = burst
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[UUID, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def try_acquire(self, company_id: UUID, cost: float = 1.0) -> float:
        """Consome ``cost`` fichas. Devolve 0 se coube, ou os segundos até caber."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(company_id)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.burst), updated=now)
                self._buckets[company_id] = bucket
                while len(self._buckets) > self._max_keys:
                    self._buckets.popitem(last=False)
            else:
                elapsed = max(0.0, now - bucket.updated)
                bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.rate_per_s)
                bucket.updated = now
                self._buckets.move_to_end(company_id)
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return 0.0
            return (cost - bucket.tokens) / self.rate_per_s

    def check(self, company_id: UUID, cost: float = 1.0) -> None:
        """Como :meth:`try_acquire`, mas levanta :class:`QuotaExceeded` quando não cabe."""
        wait = self.try_acquire(company_id, cost)
        if wait > 0:
            retry_after = max(1, math.ceil(wait))
            raise QuotaExceeded(
                f"limite de requisições da empresa excedido; tente de novo em {retry_after} s",
                retry_after_s=retry_after,
            )
