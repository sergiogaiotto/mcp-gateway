"""Saúde do serviço: vivo (``/saude``) e pronto (``/saude/pronto``).

``/saude`` continua o de sempre: responde enquanto o processo atende e, num processo
com despachante, 503 enquanto ele está parado ou com uma etapa falhando há tempo
demais (:class:`rapa.server.DispatcherSupervisor`). É a sonda de vida.

``/saude/pronto`` é a sonda de prontidão: confere, ao mesmo tempo e cada uma com um
prazo curto, as :class:`ReadinessCheck` do processo:

- ``banco``: ``SELECT 1`` no banco da aplicação (:class:`DatabaseCheck`);
- ``banco_despachante``: o mesmo no banco do despachante, quando o processo tem um;
- ``despachante``: a saúde do despachante, quando há (:class:`DispatcherCheck`);
- as checagens do cofre e do armazenamento de objetos, quando configurados: a parte do
  OpenBao e do S3 traz as dela com a mesma interface (``name`` e ``async ready()``, o
  ``ReadinessCheck`` de ``rapa.control.backends``), e elas entram pelo ``extra`` de
  :func:`rapa.server.build_readiness_checks`.

Responde 200 ``{"estado": "pronto"}`` ou 503 ``{"estado": "nao_pronto", "falhas":
[...]}`` com os nomes do que falhou e nada mais (nem o tipo nem o texto do erro). O
resultado vale por um segundo e as sondas simultâneas esperam a mesma conferência: muitas
chamadas não viram muitas consultas. Uma checagem que passa a falhar vai para o log uma
vez, pelo nome e pelo tipo do erro; a volta também.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from rapa.control.db import Database

__all__ = [
    "READY_PATH",
    "DatabaseCheck",
    "DispatcherCheck",
    "Readiness",
    "ReadinessCheck",
]

logger = logging.getLogger("rapa.control.saude")

#: O caminho da sonda de prontidão.
READY_PATH = "/saude/pronto"
#: Prazo de cada checagem.
DEFAULT_CHECK_TIMEOUT_S = 2.0
#: Por quanto tempo um resultado vale para as sondas seguintes.
DEFAULT_CACHE_S = 1.0


class ReadinessCheck(Protocol):
    """Uma checagem de prontidão: um nome curto e estável e a conferência.

    ``ready`` devolve se está pronto; levantar (ou passar do prazo) conta como não
    pronto. O nome é o que aparece na resposta (``banco``, ``openbao``...), então nunca
    traz dados. É a mesma forma das checagens do cofre e do armazenamento de objetos
    (``BaoReadiness`` e ``S3Readiness``), que entram sem adaptação.
    """

    @property
    def name(self) -> str: ...

    async def ready(self) -> bool: ...


class DatabaseCheck:
    """``SELECT 1`` num banco (o da aplicação ou o do despachante)."""

    def __init__(self, db: Database, name: str = "banco") -> None:
        self.db = db
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def ready(self) -> bool:
        async with self.db.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True


class _Healthy(Protocol):
    @property
    def healthy(self) -> bool: ...


class DispatcherCheck:
    """O despachante deste processo está rodando e com as etapas em dia."""

    name = "despachante"

    def __init__(self, supervisor: _Healthy) -> None:
        self.supervisor = supervisor

    async def ready(self) -> bool:
        return bool(self.supervisor.healthy)


class Readiness:
    """As checagens de prontidão do processo e a resposta de ``/saude/pronto``."""

    def __init__(
        self,
        checks: Sequence[ReadinessCheck],
        *,
        timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
        cache_s: float = DEFAULT_CACHE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        names = [check.name for check in checks]
        if len(set(names)) != len(names):
            raise ValueError("cada checagem de prontidão precisa de um nome próprio")
        if timeout_s <= 0 or cache_s < 0:
            raise ValueError("o prazo das checagens precisa ser positivo")
        self.checks = list(checks)
        self.timeout_s = timeout_s
        self.cache_s = cache_s
        self.clock = clock
        self._result: tuple[float, list[str]] | None = None
        self._pending: asyncio.Future[list[str]] | None = None
        self._failing: set[str] = set()

    def add(self, check: ReadinessCheck) -> None:
        """Acrescenta uma checagem (as do cofre e do armazenamento, por exemplo)."""
        if any(existing.name == check.name for existing in self.checks):
            raise ValueError(f"já existe uma checagem de prontidão chamada {check.name}")
        self.checks.append(check)
        self._result = None

    async def _one(self, check: ReadinessCheck) -> str | None:
        """O nome da checagem, se ela falhou; ``None`` se está pronta."""
        error: str | None = None
        try:
            async with asyncio.timeout(self.timeout_s):
                ready = await check.ready()
        except TimeoutError:
            ready, error = False, "tempo esgotado"
        except Exception as exc:  # banco fora, cofre selado, rede: não pronto
            ready, error = False, type(exc).__name__
        if not ready:
            if check.name not in self._failing:
                self._failing.add(check.name)
                logger.warning(
                    "a checagem de prontidão %s falhou%s",
                    check.name,
                    f": {error}" if error else "",
                )
            return check.name
        if check.name in self._failing:
            self._failing.discard(check.name)
            logger.warning("a checagem de prontidão %s voltou a passar", check.name)
        return None

    async def _evaluate(self) -> list[str]:
        results = await asyncio.gather(*(self._one(check) for check in self.checks))
        failures = [name for name in results if name is not None]
        self._result = (self.clock(), failures)
        return failures

    async def failures(self) -> list[str]:
        """Os nomes das checagens que falharam (vazio: pronto)."""
        cached = self._result
        if cached is not None and self.clock() - cached[0] < self.cache_s:
            return list(cached[1])
        pending = self._pending
        if pending is None or pending.done():
            pending = asyncio.ensure_future(self._evaluate())
            self._pending = pending
        # A conferência segue mesmo se quem pediu desistir (as outras sondas esperam ela).
        return list(await asyncio.shield(pending))

    async def endpoint(self) -> JSONResponse:
        """A resposta de ``/saude/pronto``."""
        failures = await self.failures()
        headers = {"Cache-Control": "no-store"}
        if failures:
            return JSONResponse(
                {"estado": "nao_pronto", "falhas": failures}, status_code=503, headers=headers
            )
        return JSONResponse({"estado": "pronto"}, headers=headers)

    async def aclose(self) -> None:
        """Espera a conferência em andamento (o encerramento do serviço)."""
        pending = self._pending
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
