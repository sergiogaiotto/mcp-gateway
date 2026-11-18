"""Despachante: pega execuções da fila e as entrega a um lançador.

O lançador é injetado: uma função assíncrona ``launch(job) -> dict`` que roda a
execução (por exemplo, ``python -m rapa.runner.worker`` num processo local, ou
um pod de uso único) e devolve o RESULT.json da interface A. Os eventos ficam a
cargo do lançador, que os envia com :meth:`PgRunQueue.ingest_events`.

O laço é: recolher concessões vencidas → pegar execuções até o limite de
concorrência → lançar cada uma com renovação periódica da concessão → gravar o
resultado. Se a tentativa deixar de ser a atual (renovação recusada), o
lançamento é cancelado e o resultado descartado.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import DBAPIError

from rapa.control.services._common import now_utc
from rapa.control.services.queue import PgRunQueue, StaleAttempt
from rapa.core.contracts import InvalidInput, RunMode, RunnerJob, RunState
from rapa.core.robot import Effect

__all__ = ["Dispatcher", "Launcher", "failure_result"]

logger = logging.getLogger("rapa.control.dispatcher")

type Launcher = Callable[[RunnerJob], Awaitable[Mapping[str, Any]]]

_FINISH_RETRIES = 3


def failure_result(job: RunnerJob, error_code: str, message: str) -> dict[str, Any]:
    """Resultado sintético quando o lançador não entrega um RESULT.json válido.

    Se o robô tem passo irreversível e rodaria por inteiro, não dá para saber se
    ele chegou a começar: o estado fica indeterminado para uma pessoa decidir.
    """
    may_have_written = job.mode is RunMode.FULL and job.manifest.effect() is Effect.IRREVERSIBLE
    return {
        "run_id": str(job.run_id),
        "attempt": job.attempt,
        "state": (RunState.INDETERMINATE if may_have_written else RunState.FAILED).value,
        "outputs": None,
        "error_code": error_code,
        "error_message": message,
        "failed_step_id": None,
        "irreversible_started": False,
        "steps_done": 0,
        "artifacts": [],
    }


class Dispatcher:
    """Laço de despacho com concorrência limitada."""

    def __init__(
        self,
        queue: PgRunQueue,
        launch: Launcher,
        *,
        concurrency: int = 4,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 15.0,
        reap_interval: float = 30.0,
        partitions_interval: float = 6 * 3600.0,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        if concurrency < 1:
            raise ValueError("a concorrência precisa ser pelo menos 1")
        self.queue = queue
        self.launch = launch
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.reap_interval = reap_interval
        self.partitions_interval = partitions_interval
        self.clock = clock
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_reap: datetime | None = None
        self._last_partitions: datetime | None = None
        self._wakeup = asyncio.Event()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def wake(self) -> None:
        """Acorda o laço antes do próximo intervalo (por exemplo, depois de enfileirar)."""
        self._wakeup.set()

    async def maintenance(self) -> None:
        """Recolhe concessões vencidas e cria partições de eventos, quando for a hora."""
        now = self.clock()
        if self._last_partitions is None or now - self._last_partitions >= timedelta(
            seconds=self.partitions_interval
        ):
            await self.queue.ensure_event_partitions()
            self._last_partitions = now
        if self._last_reap is None or now - self._last_reap >= timedelta(
            seconds=self.reap_interval
        ):
            report = await self.queue.reap_expired(now=now)
            self._last_reap = now
            if report.requeued or report.indeterminate or report.failed:
                logger.warning(
                    "concessões vencidas: %d de volta à fila, %d indeterminadas, %d falharam",
                    len(report.requeued),
                    len(report.indeterminate),
                    len(report.failed),
                )

    async def run_once(self) -> int:
        """Pega execuções até encher a concorrência; devolve quantas lançou."""
        launched = 0
        while len(self._tasks) < self.concurrency:
            job = await self.queue.claim_next(now=self.clock())
            if job is None:
                break
            task = asyncio.create_task(self._handle(job), name=f"execucao-{job.run_id}")
            self._tasks.add(task)
            task.add_done_callback(self._done)
            launched += 1
        return launched

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._wakeup.set()
        if not task.cancelled() and task.exception() is not None:
            logger.error("falha inesperada no despacho: %s", type(task.exception()).__name__)

    async def drain(self) -> None:
        """Espera as execuções em andamento terminarem."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        """Roda até ``stop`` ser acionado; depois espera o que está em andamento."""
        while not stop.is_set():
            try:
                await self.maintenance()
                await self.run_once()
            except DBAPIError as exc:
                logger.error("erro de banco no despacho: %s", type(exc.orig).__name__)
            self._wakeup.clear()
            waiters = [
                asyncio.ensure_future(stop.wait()),
                asyncio.ensure_future(self._wakeup.wait()),
            ]
            try:
                await asyncio.wait(
                    waiters, timeout=self.poll_interval, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()
        await self.drain()

    async def _heartbeats(
        self, job: RunnerJob, launch_task: asyncio.Task[Any], stale: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                current = await self.queue.heartbeat(job.run_id, job.attempt, now=self.clock())
            except DBAPIError as exc:
                logger.warning("renovação da concessão falhou: %s", type(exc.orig).__name__)
                continue
            if not current:
                logger.warning(
                    "a tentativa %d de %s ficou antiga; cancelando", job.attempt, job.run_id
                )
                stale.set()
                launch_task.cancel()
                return

    async def _handle(self, job: RunnerJob) -> None:
        launch_task: asyncio.Task[Mapping[str, Any]] = asyncio.create_task(
            _call(self.launch, job), name=f"lancamento-{job.run_id}"
        )
        stale = asyncio.Event()
        beats = asyncio.create_task(self._heartbeats(job, launch_task, stale))
        result: Mapping[str, Any]
        try:
            result = await launch_task
        except asyncio.CancelledError:
            if not stale.is_set():
                raise
            # A tentativa ficou antiga: outra já assumiu, nada a gravar.
            return
        except Exception as exc:  # o lançador falhou sem entregar resultado
            logger.error("o lançador falhou: %s", type(exc).__name__)
            result = failure_result(
                job, "falha_no_lancador", "O executor terminou sem entregar o resultado."
            )
        finally:
            beats.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beats
        await self._finish(job, result)

    async def _finish(self, job: RunnerJob, result: Mapping[str, Any]) -> None:
        for attempt in range(1, _FINISH_RETRIES + 1):
            try:
                await self.queue.finish(job.run_id, job.attempt, result, now=self.clock())
                return
            except StaleAttempt:
                logger.warning("resultado descartado: a tentativa %d ficou antiga", job.attempt)
                return
            except InvalidInput:
                logger.error("o lançador devolveu um resultado inválido para %s", job.run_id)
                result = failure_result(
                    job, "resultado_invalido", "O executor devolveu um resultado inválido."
                )
            except DBAPIError as exc:
                logger.warning(
                    "gravação do resultado falhou (tentativa %d): %s",
                    attempt,
                    type(exc.orig).__name__,
                )
                await asyncio.sleep(0.2 * attempt)
        logger.error("não foi possível gravar o resultado de %s", job.run_id)


async def _call(launch: Launcher, job: RunnerJob) -> Mapping[str, Any]:
    return await launch(job)
