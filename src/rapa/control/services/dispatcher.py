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

Nada daqui devolve uma execução à fila: o resultado (real ou sintético, de
:func:`failure_result`) sempre a conclui. Uma execução interrompida sem resultado
(despachante que cai ou que encerra sem esperar) fica para o recolhedor, que só a
devolve à fila se nenhum passo que grava tinha começado (ver
:mod:`rapa.control.services.queue`).

Um resultado em mãos não se perde por uma queda curta do banco: a concessão
continua sendo renovada e a gravação é repetida, com espera crescente, até dar
certo ou até o prazo da execução (mais uma folga). Enquanto isso, o recolhedor
deste despachante não devolve a execução à fila. Erros inesperados (pool de
conexões esgotado, por exemplo) não derrubam o laço nem a renovação.

Cada etapa do laço (criar partições de eventos, recolher concessões vencidas,
disparar as agendas vencidas, quando há um agendador, e pegar execuções) tem a
sua própria proteção: um erro persistente numa delas não
impede as outras. A etapa de manutenção que falha é repetida com espera
crescente, e :attr:`Dispatcher.healthy` fica falso quando alguma etapa falha sem
parar há ``unhealthy_after_s`` ou mais.

O disparo das agendas fica no mesmo laço, antes de pegar execuções, mas com tempo
limitado por volta (``Scheduler.max_tick_s``): muitas agendas vencidas atrasam o
despacho no máximo esse tanto. Quando o agendador fica para trás, a volta seguinte
começa sem esperar ``poll_interval``, e o despacho roda entre uma volta e outra.

Certificado A1: logo antes de lançar o executor de um robô que declara um
certificado A1, o despachante confere de novo se ele ainda pode ser usado
(:meth:`PgRunQueue.a1_block`: registrado, não revogado, não vencido e com a
autorização do custodiante na impressão atual) e se a versão tem as duas aprovações
de quem não a criou nem é custodiante. Se não puder, nada é lançado e a
execução termina ``failed`` com o código ``certificado_a1_bloqueado``; se a
conferência falhar (banco fora do ar), também não lança (falha fechada). Com
``alerts``, uma etapa da manutenção (``"certificates"``, a cada 6 horas) registra os
avisos de vencimento dos certificados.

A retenção (:class:`rapa.control.services.retention.Retention`, a etapa
``"retention"``) roda uma vez por hora, também com tempo limitado por volta; se
ficar para trás, volta em ``retention_catch_up`` segundos.

Com um :class:`rapa.control.artifacts.ArtifactStore`, os artefatos do resultado
(as telas, os arquivos baixados e o vídeo da falha) são registrados por ele antes
de gravar o resultado: só o que o store aceita (no lugar certo, sem caminho) vai
para ``runs.artifacts``. Um artefato recusado fica de fora; o resultado é gravado
do mesmo jeito.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError

from rapa.control.artifacts import ArtifactStore
from rapa.control.services._common import now_utc
from rapa.control.services.queue import PgRunQueue, StaleAttempt
from rapa.core.contracts import InvalidInput, RunMode, RunnerJob, RunState, uses_a1
from rapa.core.robot import Effect
from rapa.runner.errors import ErrorCode

__all__ = [
    "Dispatcher",
    "ExpiryWatch",
    "Launcher",
    "RetentionTicker",
    "Ticker",
    "WorkerNotStarted",
    "failure_result",
]

logger = logging.getLogger("rapa.control.dispatcher")

type Launcher = Callable[[RunnerJob], Awaitable[Mapping[str, Any]]]

#: Espera máxima entre duas tentativas de gravar o resultado.
_FINISH_RETRY_MAX_S = 5.0
#: Espera máxima antes de repetir uma etapa da manutenção que falhou.
_MAINTENANCE_RETRY_MAX_S = 60.0

#: Etapas do laço acompanhadas pela saúde do despachante, com a descrição para o log.
_STEPS = {
    "partitions": "criação das partições de eventos",
    "reap": "recolhimento das concessões vencidas",
    "schedules": "disparo das agendas",
    "certificates": "aviso de vencimento dos certificados A1",
    "retention": "retenção das evidências e dos dados antigos",
    "claim": "despacho de execuções",
}
#: Mensagem quando não deu para conferir o certificado A1 antes de lançar.
_A1_UNCHECKED = (
    "Não foi possível conferir o certificado A1 do robô antes de lançar; nada foi aberto "
    "no navegador."
)
#: Artefatos registrados, no máximo, por resultado (o que passar disso fica de fora).
_MAX_ARTIFACTS = 10_000


class Ticker(Protocol):
    """O agendador (:class:`rapa.control.services.schedules.Scheduler`), visto do laço."""

    @property
    def behind(self) -> bool:
        """A última volta deixou agendas vencidas para trás (a próxima vem sem esperar)."""
        ...

    async def tick(self, now: datetime | None = None) -> int:
        """Põe na fila as ocorrências vencidas; devolve quantas execuções entraram.

        Cada volta precisa ser curta (o despacho vem logo depois, no mesmo laço):
        o que não couber fica para a próxima, com :attr:`behind` verdadeiro.
        """
        ...


class ExpiryWatch(Protocol):
    """O aviso de vencimento dos certificados A1, visto do laço
    (:class:`rapa.control.services.credentials.CredentialAlerts`)."""

    async def check(self, now: datetime | None = None) -> int:
        """Registra os avisos do dia que faltam; devolve quantos registrou."""
        ...


class RetentionTicker(Protocol):
    """A retenção (:class:`rapa.control.services.retention.Retention`), vista do laço."""

    @property
    def behind(self) -> bool:
        """A última volta parou no limite de tempo com coisa vencida para apagar."""
        ...

    async def tick(self, now: datetime | None = None) -> object:
        """Apaga o que passou do prazo, em lotes pequenos e com tempo limitado."""
        ...


class _ReportedArtifact(BaseModel):
    """Um artefato como o executor informa no RESULT.json (o caminho é local)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=4000)


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

    "Falhou" é final: a execução nunca volta à fila por aqui. Se o banco já tem o
    início de um passo que grava, :func:`rapa.control.services.queue.finish` põe
    na mensagem o pedido de conferir o sistema de destino.
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


@dataclass(slots=True)
class _StepState:
    """Situação de uma etapa do laço: última vez que deu certo e falhas seguidas."""

    last_ok: datetime | None = None
    failures: int = 0
    failing_since: datetime | None = None
    retry_at: datetime | None = None


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
        unhealthy_after_s: float = 60.0,
        scheduler: Ticker | None = None,
        schedule_interval: float = 5.0,
        alerts: ExpiryWatch | None = None,
        alerts_interval: float = 6 * 3600.0,
        artifacts: ArtifactStore | None = None,
        retention: RetentionTicker | None = None,
        retention_interval: float = 3600.0,
        retention_catch_up: float = 60.0,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """``finish_retry_s`` é a primeira espera entre tentativas de gravar o
        resultado (dobra a cada falha, até 5 s); ``finish_grace_s`` é quanto
        tempo além do prazo da execução o despachante insiste em gravá-lo.

        Com ``scheduler``, a manutenção também dispara as agendas vencidas a cada
        ``schedule_interval`` segundos (a etapa ``"schedules"``), antes de pegar
        execuções: o que entrou na fila já é despachado na mesma volta. Cada volta
        do agendador tem tempo limitado; quando ele fica para trás
        (``scheduler.behind``), a volta seguinte do laço vem sem esperar o intervalo,
        e o despacho continua rodando entre uma e outra.

        Com ``alerts``, a manutenção registra os avisos de vencimento dos
        certificados A1 a cada ``alerts_interval`` segundos (a etapa
        ``"certificates"``).

        Com ``artifacts``, os artefatos de cada resultado são registrados pelo store
        antes de o resultado ser gravado. Com ``retention``, a etapa ``"retention"``
        roda a cada ``retention_interval`` segundos (uma hora), ou a cada
        ``retention_catch_up`` enquanto ficar para trás.

        Uma etapa da manutenção que falha é repetida depois de ``poll_interval``,
        espera que dobra a cada falha seguida, até 60 s. ``unhealthy_after_s`` é
        quanto tempo uma etapa do laço pode falhar sem parar antes de
        :attr:`healthy` ficar falso."""
        if concurrency < 1:
            raise ValueError("a concorrência precisa ser pelo menos 1")
        if finish_retry_s <= 0 or finish_grace_s < 0:
            raise ValueError("as esperas para gravar o resultado precisam ser positivas")
        if unhealthy_after_s < 0:
            raise ValueError("o prazo para acusar falha na saúde não pode ser negativo")
        self.queue = queue
        self.launch = launch
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.reap_interval = reap_interval
        self.partitions_interval = partitions_interval
        self.finish_retry_s = finish_retry_s
        self.finish_grace_s = finish_grace_s
        self.unhealthy_after_s = unhealthy_after_s
        self.scheduler = scheduler
        self.schedule_interval = schedule_interval
        self.alerts = alerts
        self.alerts_interval = alerts_interval
        self.artifacts = artifacts
        self.retention = retention
        self.retention_interval = retention_interval
        self.retention_catch_up = retention_catch_up
        self.clock = clock
        self._tasks: dict[asyncio.Task[None], RunnerJob] = {}
        self._steps = {name: _StepState() for name in _STEPS}
        self._wakeup = asyncio.Event()
        #: Início da volta atual do laço (None antes de começar ou depois de parar).
        self._loop_started: datetime | None = None
        #: O agendador ficou para trás na última volta: a próxima não espera.
        self._schedules_behind = False
        #: A retenção ficou para trás: a próxima volta dela vem em ``retention_catch_up``.
        self._retention_behind = False

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    @property
    def failing(self) -> dict[str, datetime]:
        """Etapas do laço que falharam na última vez, com o início das falhas seguidas.

        As etapas são ``"partitions"`` (partições de eventos), ``"reap"``
        (recolhimento das concessões vencidas), ``"schedules"`` (agendas),
        ``"certificates"`` (avisos de vencimento do A1), ``"retention"`` (retenção) e
        ``"claim"`` (pegar execuções).
        """
        return {
            name: state.failing_since
            for name, state in self._steps.items()
            if state.failing_since is not None
        }

    @property
    def healthy(self) -> bool:
        """Nenhuma etapa do laço falha sem parar há ``unhealthy_after_s`` ou mais.

        Uma falha passageira não muda a saúde; uma etapa que segue falhando (sem
        permissão para criar partições, recolhedor quebrado, banco fora do ar)
        deixa o despachante sem saúde até voltar a dar certo.
        """
        limit = timedelta(seconds=self.unhealthy_after_s)
        now = self.clock()
        if self.stalled:
            return False
        return all(now - since < limit for since in self.failing.values())

    @property
    def stalled(self) -> bool:
        """Uma volta do laço está presa há mais de ``unhealthy_after_s`` (mais o intervalo).

        Pega o caso em que uma etapa não falha, mas também não termina (por
        exemplo, esperando uma trava no banco): sem isto, a saúde seguiria boa.
        """
        started = self._loop_started
        if started is None:
            return False
        limit = timedelta(seconds=self.unhealthy_after_s + self.poll_interval)
        return self.clock() - started > limit

    def wake(self) -> None:
        """Acorda o laço antes do próximo intervalo (por exemplo, depois de enfileirar)."""
        self._wakeup.set()

    def _held(self, now: datetime) -> list[UUID]:
        """Execuções que este despachante ainda acompanha, dentro do prazo mais a folga."""
        grace = timedelta(seconds=self.finish_grace_s)
        return [job.run_id for job in self._tasks.values() if now < job.deadline + grace]

    def _due(self, name: str, now: datetime, interval: float) -> bool:
        """Se é hora da etapa: pelo intervalo normal ou, depois de uma falha, pela espera."""
        state = self._steps[name]
        if state.retry_at is not None:
            return now >= state.retry_at
        return state.last_ok is None or now - state.last_ok >= timedelta(seconds=interval)

    def _succeeded(self, name: str, now: datetime) -> None:
        state = self._steps[name]
        if state.failures:
            logger.warning(
                "a etapa de %s voltou a funcionar depois de %d falhas",
                _STEPS[name],
                state.failures,
            )
        state.last_ok = now
        state.failures = 0
        state.failing_since = None
        state.retry_at = None

    def _failed(self, name: str, now: datetime, exc: Exception, *, backoff: bool) -> None:
        """Registra a falha da etapa (só o tipo do erro); com ``backoff``, adia a próxima."""
        state = self._steps[name]
        state.failures += 1
        if state.failing_since is None:
            state.failing_since = now
        if not backoff:
            logger.error("falha na etapa de %s: %s", _STEPS[name], _error_name(exc))
            return
        # O expoente é limitado para a conta não estourar numa sequência longa de falhas.
        delay = min(
            self.poll_interval * 2 ** min(state.failures - 1, 32),
            max(_MAINTENANCE_RETRY_MAX_S, self.poll_interval),
        )
        state.retry_at = now + timedelta(seconds=delay)
        logger.error(
            "falha na etapa de %s (%d seguidas): %s; nova tentativa em %.1f s",
            _STEPS[name],
            state.failures,
            _error_name(exc),
            delay,
        )

    async def maintenance(self) -> None:
        """Cria partições de eventos e recolhe concessões vencidas, quando for a hora.

        Cada etapa tem a sua proteção: o erro de uma é registrado e não impede a
        outra nem o despacho. A etapa que falhou é repetida com espera crescente.
        """
        now = self.clock()
        if self._due("partitions", now, self.partitions_interval):
            try:
                await self.queue.ensure_event_partitions()
            except Exception as exc:  # sem permissão, nome em uso, banco fora: tenta depois
                self._failed("partitions", now, exc, backoff=True)
            else:
                self._succeeded("partitions", now)
        if self._due("reap", now, self.reap_interval):
            try:
                # As execuções em andamento aqui não são recolhidas: se a concessão
                # venceu, foi porque o banco caiu, não porque o executor sumiu.
                report = await self.queue.reap_expired(now=now, skip=self._held(now))
            except Exception as exc:
                self._failed("reap", now, exc, backoff=True)
            else:
                self._succeeded("reap", now)
                if report.requeued or report.indeterminate or report.failed or report.cancelled:
                    logger.warning(
                        "concessões vencidas: %d de volta à fila, %d indeterminadas, %d falharam, "
                        "%d canceladas (empresa congelada)",
                        len(report.requeued),
                        len(report.indeterminate),
                        len(report.failed),
                        len(report.cancelled),
                    )
        if self.scheduler is not None and (
            self._schedules_behind or self._due("schedules", now, self.schedule_interval)
        ):
            self._schedules_behind = False
            try:
                await self.scheduler.tick(now)
            except Exception as exc:  # banco fora, erro inesperado: tenta de novo depois
                self._failed("schedules", now, exc, backoff=True)
            else:
                self._succeeded("schedules", now)
                self._schedules_behind = bool(self.scheduler.behind)
        if self.alerts is not None and self._due("certificates", now, self.alerts_interval):
            try:
                await self.alerts.check(now)
            except Exception as exc:  # banco fora, erro inesperado: tenta de novo depois
                self._failed("certificates", now, exc, backoff=True)
            else:
                self._succeeded("certificates", now)
        retention_every = (
            self.retention_catch_up if self._retention_behind else self.retention_interval
        )
        if self.retention is not None and self._due("retention", now, retention_every):
            try:
                await self.retention.tick(now)
            except Exception as exc:  # banco fora, trava ocupada, disco: tenta de novo depois
                self._failed("retention", now, exc, backoff=True)
            else:
                self._succeeded("retention", now)
                self._retention_behind = bool(self.retention.behind)

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
        tenta de novo depois do intervalo. Só o cancelamento o interrompe. A
        manutenção e o despacho são protegidos em separado: uma manutenção que
        falha sempre não impede de pegar as execuções da fila.
        """
        while not stop.is_set():
            self._loop_started = self.clock()
            try:
                await self.maintenance()
            except Exception as exc:  # cada etapa já se protege; isto é só uma rede
                logger.error("falha na manutenção da fila: %s", _error_name(exc))
            now = self.clock()
            try:
                await self.run_once()
            except Exception as exc:  # banco fora, pool esgotado, erro inesperado: segue
                self._failed("claim", now, exc, backoff=False)
            else:
                self._succeeded("claim", now)
            # A espera entre as voltas não conta como volta presa.
            self._loop_started = None
            self._wakeup.clear()
            if self._schedules_behind:
                # Agendas vencidas ficaram para trás: a próxima volta vem já (o
                # despacho acima rodou entre as duas), só dando a vez às execuções.
                await asyncio.sleep(0)
                continue
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

    async def _a1_blocked(self, job: RunnerJob) -> str | None:
        """Por que não lançar o executor (certificado A1); ``None`` para os outros robôs."""
        if not uses_a1(job.manifest):
            return None
        try:
            return await self.queue.a1_block(job, now=self.clock())
        except Exception as exc:  # banco fora: não lança sem conferir (falha fechada)
            logger.error("conferência do certificado A1 falhou: %s", _error_name(exc))
            return _A1_UNCHECKED

    async def _handle(self, job: RunnerJob) -> None:
        blocked = await self._a1_blocked(job)
        if blocked is not None:
            logger.warning("execução %s não lançada: certificado A1 bloqueado", job.run_id)
            await self._finish(
                job,
                failure_result(
                    job, ErrorCode.CERTIFICADO_A1_BLOQUEADO.value, blocked, started=False
                ),
            )
            return
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
            # A concessão segue renovada até os artefatos e o resultado serem gravados.
            result = await self._register_artifacts(job, result)
            await self._finish(job, result)
        finally:
            beats.cancel()
            # Erros da renovação já foram registrados; não impedem gravar o resultado.
            await asyncio.gather(beats, return_exceptions=True)

    async def _register_artifacts(
        self, job: RunnerJob, result: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Registra os artefatos do resultado pelo store; o que ele recusa fica de fora.

        Sem store, o resultado segue como veio (a fila guarda os artefatos sem o
        caminho). Um erro do store nunca impede gravar o resultado: só o tipo do erro
        vai para o log, nunca o nome do arquivo (que pode trazer dados da execução).
        """
        store = self.artifacts
        reported = result.get("artifacts")
        if store is None or not isinstance(reported, list) or not reported:
            return result
        registered: list[dict[str, Any]] = []
        refused = 0
        for raw in reported[:_MAX_ARTIFACTS]:
            try:
                item = _ReportedArtifact.model_validate(raw)
                info = await store.put(
                    job.company_id, job.run_id, job.attempt, item.name, item.kind, Path(item.path)
                )
            except (ValidationError, ValueError, OSError) as exc:
                refused += 1
                logger.warning(
                    "artefato de %s recusado pelo armazenamento: %s", job.run_id, type(exc).__name__
                )
                continue
            except Exception as exc:  # store fora do ar: o resultado é gravado sem ele
                refused += 1
                logger.error(
                    "falha ao registrar um artefato de %s: %s", job.run_id, _error_name(exc)
                )
                continue
            registered.append(info.model_dump(mode="json"))
        refused += max(0, len(reported) - _MAX_ARTIFACTS)
        if refused:
            logger.warning("%d artefato(s) de %s ficaram de fora", refused, job.run_id)
        return {**result, "artifacts": registered}

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
