"""Despachante: pega execuções da fila e as entrega a um lançador.

O lançador é injetado: uma função assíncrona ``launch(job) -> dict`` que roda a
execução (por exemplo, ``python -m rapa.runner.worker`` num processo local, ou
um pod de uso único) e devolve o RESULT.json da interface A. Os eventos ficam a
cargo do lançador, que os envia com :meth:`PgRunQueue.ingest_events`. Se falhar
antes de iniciar o executor, o lançador levanta :class:`WorkerNotStarted`.

O laço é: recolher concessões vencidas → pegar execuções até o limite de
concorrência → lançar cada uma com renovação periódica da concessão → gravar o
resultado. Se a tentativa deixar de ser a atual (renovação recusada), o
lançamento é cancelado e o resultado descartado.

Um resultado em mãos não se perde por uma queda curta do banco: a concessão
continua sendo renovada e a gravação é repetida, com espera crescente, até dar
certo ou até o prazo da execução (mais uma folga). Enquanto isso, o recolhedor
deste despachante não devolve a execução à fila. Erros inesperados (pool de
conexões esgotado, por exemplo) não derrubam o laço nem a renovação.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import DataError, DBAPIError, IntegrityError

from rapa.control.services._common import now_utc
from rapa.control.services.queue import PgRunQueue, StaleAttempt
from rapa.core.contracts import InvalidInput, RunMode, RunnerJob, RunState
from rapa.core.robot import Effect

__all__ = ["Dispatcher", "Launcher", "WorkerNotStarted", "failure_result"]

logger = logging.getLogger("rapa.control.dispatcher")

type Launcher = Callable[[RunnerJob], Awaitable[Mapping[str, Any]]]

#: Espera máxima entre duas tentativas de gravar o resultado.
_FINISH_RETRY_MAX_S = 5.0


class WorkerNotStarted(Exception):
    """O lançador falhou antes de iniciar o executor.

    Por exemplo: cofre de segredos fora do ar, pasta da tentativa que não pôde
    ser criada ou programa do executor que não abriu. Nenhum passo pode ter
    rodado, então a execução é uma falha comum, nunca um indeterminado.
    """


def failure_result(
    job: RunnerJob, error_code: str, message: str, *, started: bool = True
) -> dict[str, Any]:
    """Resultado sintético quando o lançador não entrega um RESULT.json válido.

    Se o executor chegou a ser iniciado e o robô tem passo irreversível e rodaria
    por inteiro, não dá para saber se ele chegou a começar: o estado fica
    indeterminado para uma pessoa decidir. Com ``started=False`` (o executor nem
    foi iniciado), nada pode ter rodado e o estado é "falhou".
    """
    may_have_written = (
        started and job.mode is RunMode.FULL and job.manifest.effect() is Effect.IRREVERSIBLE
    )
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


def _error_name(exc: BaseException) -> str:
    """Nome do erro para o log, sem a mensagem (que pode trazer dados da execução)."""
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        return type(exc.orig).__name__
    return type(exc).__name__


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
        finish_retry_s: float = 0.2,
        finish_grace_s: float = 300.0,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """``finish_retry_s`` é a primeira espera entre tentativas de gravar o
        resultado (dobra a cada falha, até 5 s); ``finish_grace_s`` é quanto
        tempo além do prazo da execução o despachante insiste em gravá-lo."""
        if concurrency < 1:
            raise ValueError("a concorrência precisa ser pelo menos 1")
        if finish_retry_s <= 0 or finish_grace_s < 0:
            raise ValueError("as esperas para gravar o resultado precisam ser positivas")
        self.queue = queue
        self.launch = launch
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.reap_interval = reap_interval
        self.partitions_interval = partitions_interval
        self.finish_retry_s = finish_retry_s
        self.finish_grace_s = finish_grace_s
        self.clock = clock
        self._tasks: dict[asyncio.Task[None], RunnerJob] = {}
        self._last_reap: datetime | None = None
        self._last_partitions: datetime | None = None
        self._wakeup = asyncio.Event()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def wake(self) -> None:
        """Acorda o laço antes do próximo intervalo (por exemplo, depois de enfileirar)."""
        self._wakeup.set()

    def _held(self, now: datetime) -> list[UUID]:
        """Execuções que este despachante ainda acompanha, dentro do prazo mais a folga."""
        grace = timedelta(seconds=self.finish_grace_s)
        return [job.run_id for job in self._tasks.values() if now < job.deadline + grace]

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
            # As execuções em andamento aqui não são recolhidas: se a concessão
            # venceu, foi porque o banco caiu, não porque o executor sumiu.
            report = await self.queue.reap_expired(now=now, skip=self._held(now))
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
            self._tasks[task] = job
            task.add_done_callback(self._done)
            launched += 1
        return launched

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)
        self._wakeup.set()
        if not task.cancelled() and task.exception() is not None:
            logger.error("falha inesperada no despacho: %s", type(task.exception()).__name__)

    async def drain(self) -> None:
        """Espera as execuções em andamento terminarem."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # Com tarefas já concluídas, o ``gather`` volta sem dar a vez ao laço, e
            # o callback que as tira da lista ainda não rodou: sem esta pausa, o
            # laço giraria para sempre.
            await asyncio.sleep(0)

    async def run(self, stop: asyncio.Event) -> None:
        """Roda até ``stop`` ser acionado; depois espera o que está em andamento.

        Nenhum erro de uma volta encerra o laço: ele registra o tipo do erro e
        tenta de novo depois do intervalo. Só o cancelamento o interrompe.
        """
        while not stop.is_set():
            try:
                await self.maintenance()
                await self.run_once()
            except DBAPIError as exc:
                logger.error("erro de banco no despacho: %s", _error_name(exc))
            except Exception as exc:  # pool esgotado, erro inesperado: o laço continua
                logger.error("falha no despacho: %s", _error_name(exc))
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
            except Exception as exc:  # banco fora do ar, pool esgotado: tenta na próxima
                logger.warning("renovação da concessão falhou: %s", _error_name(exc))
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
        try:
            result: Mapping[str, Any]
            try:
                result = await launch_task
            except asyncio.CancelledError:
                if not stale.is_set():
                    raise
                # A tentativa ficou antiga: outra já assumiu, nada a gravar.
                return
            except WorkerNotStarted as exc:
                logger.error("o executor não foi iniciado: %s", _error_name(exc.__cause__ or exc))
                result = failure_result(
                    job,
                    "executor_nao_iniciado",
                    "O executor não pôde ser iniciado; nenhum passo foi executado.",
                    started=False,
                )
            except Exception as exc:  # o lançador falhou sem entregar resultado
                logger.error("o lançador falhou: %s", type(exc).__name__)
                result = failure_result(
                    job, "falha_no_lancador", "O executor terminou sem entregar o resultado."
                )
            # A concessão segue renovada até o resultado ser gravado.
            await self._finish(job, result)
        finally:
            beats.cancel()
            # Erros da renovação já foram registrados; não impedem gravar o resultado.
            await asyncio.gather(beats, return_exceptions=True)

    async def _finish(self, job: RunnerJob, result: Mapping[str, Any]) -> None:
        """Grava o resultado, repetindo com espera crescente enquanto o banco falhar.

        Para quando grava, quando a tentativa fica antiga ou quando passa do prazo
        da execução mais ``finish_grace_s``. Um resultado que o banco recusa por
        conteúdo (inválido, por exemplo) vira uma falha sintética, gravada no lugar.
        """
        loop = asyncio.get_running_loop()
        remaining = max(0.0, (job.deadline - self.clock()).total_seconds())
        give_up = loop.time() + remaining + self.finish_grace_s
        delay = self.finish_retry_s
        replaced = False
        tries = 0
        while True:
            tries += 1
            try:
                await self.queue.finish(job.run_id, job.attempt, result, now=self.clock())
                return
            except StaleAttempt:
                logger.warning("resultado descartado: a tentativa %d ficou antiga", job.attempt)
                return
            except (InvalidInput, DataError, IntegrityError) as exc:
                if replaced:
                    logger.error(
                        "o resultado sintético de %s foi recusado: %s",
                        job.run_id,
                        _error_name(exc),
                    )
                else:
                    logger.error("o resultado de %s foi recusado: %s", job.run_id, _error_name(exc))
                    result = failure_result(
                        job, "resultado_invalido", "O executor devolveu um resultado inválido."
                    )
                    replaced = True
                    continue
            except Exception as exc:  # banco fora do ar, pool esgotado: tenta de novo
                logger.warning(
                    "gravação do resultado falhou (tentativa %d): %s", tries, _error_name(exc)
                )
            if loop.time() + delay > give_up:
                logger.error("não foi possível gravar o resultado de %s", job.run_id)
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, max(_FINISH_RETRY_MAX_S, self.finish_retry_s))


async def _call(launch: Launcher, job: RunnerJob) -> Mapping[str, Any]:
    return await launch(job)
