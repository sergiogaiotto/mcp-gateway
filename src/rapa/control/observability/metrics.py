"""Métricas Prometheus do plano de controle, numa porta própria.

O servidor de métricas (:func:`serve_metrics`) é outro ouvinte do processo, na porta
``RAPA_METRICAS_PORTA`` (padrão 9100; 0 desliga), no mesmo endereço do serviço (o
``--host`` de ``python -m rapa.server``: no pod, todas as interfaces; na máquina de
quem desenvolve, só ``127.0.0.1``), e só responde ``GET /metrics``: a aplicação HTTP
pública não tem essa rota, e a porta não passa pelo gateway (o coletor lê direto do
pod). Cada conexão tem prazo em cada leitura e escrita, e só algumas são atendidas ao
mesmo tempo (as de mais são fechadas na hora): conexões paradas nunca juntam threads
no processo do serviço. Cada processo tem o seu :class:`Metrics`, com um registro próprio.

As regras:

- **Cardinalidade limitada.** Nenhuma métrica tem id de execução, caminho cru, nome
  de robô, de credencial ou de pessoa. Os pedidos HTTP são contados pela rota modelo
  (``/v1/execucoes/{run_id}``), nunca pelo caminho; o que não casa com nenhuma rota
  conta como ``desconhecida``. Os rótulos de situação, origem, método e resultado só
  aceitam valores conhecidos (o resto vira ``outra``/``outro``/``erro``).
- **Empresa só quando pedido.** Por padrão, nada tem o rótulo ``empresa``; com
  ``RAPA_METRICAS_POR_EMPRESA=1``, as execuções concluídas, a fila e os avisos do A1
  ganham o rótulo (o uuid da empresa), e só elas.
- **Nada que trave o serviço.** Um erro ao coletar uma métrica nunca derruba o pedido,
  a execução nem a coleta das outras.

O que existe:

- ``rapa_execucoes_total{situacao, origem}``: execuções que o despachante deste
  processo concluiu (o resultado gravado, o recolhimento de uma concessão vencida, a
  que falha ao ser pega porque esta versão não lê o manifesto dela);
- ``rapa_execucao_duracao_segundos{situacao}``: do início ao fim da tentativa;
- ``rapa_fila_execucoes``, ``rapa_fila_espera_maxima_segundos``,
  ``rapa_execucoes_em_andamento``, ``rapa_a1_em_aviso{tipo}`` e
  ``rapa_a1_dias_para_vencer``: lidos do banco pelo despachante a cada 30 s
  (:func:`read_platform_snapshot`), com a hora da leitura em ``rapa_banco_lido_em_segundos``;
- ``rapa_despachante_*``: a saúde do despachante e de cada etapa do laço;
- ``rapa_webhook_entregas_total{resultado}``: as entregas de webhook por resultado;
- ``rapa_sessoes_abertas``: as sessões de gravação abertas por este processo;
- ``rapa_http_pedidos_total{metodo, rota, codigo}`` e
  ``rapa_http_duracao_segundos{metodo, rota}``;
- ``rapa_info{versao}`` e as métricas de processo do ``prometheus_client``.
"""

import asyncio
import contextlib
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID
from wsgiref.simple_server import WSGIRequestHandler

from fastapi.routing import RouteContext, iter_route_contexts
from prometheus_client import (
    GC_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    disable_created_metrics,
    make_wsgi_app,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.exposition import ThreadingWSGIServer
from sqlalchemy import func, or_, select, text
from starlette.routing import BaseRoute, Match, Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rapa.control.db.schema import companies, credentials, runs
from rapa.core.contracts import (
    CompanyStatus,
    ContractError,
    RunState,
    RunTrigger,
    WebhookDelivery,
)
from rapa.core.robot import CredentialKind

if TYPE_CHECKING:
    from fastapi import FastAPI

    from rapa.control.db import Database
    from rapa.control.services.queue import ConcludedRun

__all__ = [
    "DEFAULT_METRICS_PORT",
    "METRICS_MAX_CONNECTIONS",
    "METRICS_TIMEOUT_S",
    "SNAPSHOT_INTERVAL_S",
    "UNMATCHED_ROUTE",
    "CompanyCounts",
    "HttpMetricsMiddleware",
    "MeteredWebhooks",
    "Metrics",
    "MetricsServer",
    "PlatformSnapshot",
    "instrument_http",
    "read_platform_snapshot",
    "route_template",
    "serve_metrics",
    "watch_platform",
]

logger = logging.getLogger("rapa.control.metricas")

#: Porta padrão do servidor de métricas (``RAPA_METRICAS_PORTA``).
DEFAULT_METRICS_PORT = 9100
#: De quantos em quantos segundos o despachante lê a fila e os certificados do banco.
SNAPSHOT_INTERVAL_S = 30.0
#: Rota de um pedido que não casou com nenhuma rota da aplicação (nunca o caminho cru).
UNMATCHED_ROUTE = "desconhecida"
#: Prazo de cada leitura e escrita numa conexão com o servidor de métricas.
METRICS_TIMEOUT_S = 5.0
#: Conexões atendidas ao mesmo tempo pelo servidor de métricas (as de mais são fechadas).
METRICS_MAX_CONNECTIONS = 8

_STATES = frozenset(state.value for state in RunState)
_TRIGGERS = frozenset(trigger.value for trigger in RunTrigger)
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
#: Resultado de uma entrega de webhook: um código estável (o de :class:`ContractError`).
_RESULT = re.compile(r"[a-z][a-z0-9_]{0,39}")
#: Até quantos níveis de ``Mount`` a rota modelo é procurada.
_MAX_MOUNT_DEPTH = 8

_RUN_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1200.0, 1800.0, 3600.0)
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
#: Situações de uma execução que ainda não terminou (contadas na leitura do banco).
_OPEN_STATES = (RunState.QUEUED.value, RunState.RUNNING.value, RunState.AWAITING_HUMAN.value)
#: Limite de cada consulta da leitura do banco.
_SNAPSHOT_STATEMENT_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# Leitura do banco: fila e certificados A1
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyCounts:
    """O que a leitura do banco achou de uma empresa."""

    queued: int = 0
    #: Há quanto tempo a execução mais antiga da fila espera, em segundos.
    oldest_queued_s: float | None = None
    running: int = 0
    #: Certificados A1 ativos que vencem dentro do prazo do aviso.
    a1_expiring: int = 0
    #: Certificados A1 ativos já vencidos (ou sem data de validade).
    a1_expired: int = 0
    #: Dias até o vencimento mais próximo entre os certificados em aviso (negativo: vencido).
    a1_days_left: float | None = None


@dataclass(frozen=True, slots=True)
class PlatformSnapshot:
    """A fila e os avisos do A1 de todas as empresas, numa leitura."""

    read_at: datetime
    companies: Mapping[UUID, CompanyCounts] = field(default_factory=dict)


async def read_platform_snapshot(
    db: Database, *, now: datetime, warn_days: int
) -> PlatformSnapshot:
    """Lê do banco (papel do despachante) a fila e os certificados A1 em aviso.

    São duas consultas agrupadas por empresa, com tempo limitado. Os certificados contam
    como no aviso de vencimento (:func:`rapa.control.services.a1.record_expiry_alerts`):
    não revogados, de empresas que não estão congeladas, vencidos ou vencendo em até
    ``warn_days`` dias. Nenhum nome sai daqui, só contagens e prazos.
    """
    horizon = now + timedelta(days=warn_days)
    expired = or_(credentials.c.not_after.is_(None), credentials.c.not_after <= now)
    found: dict[UUID, dict[str, Any]] = {}
    async with db.dispatcher_session() as session:
        await session.execute(
            text(f"SET LOCAL statement_timeout = {_SNAPSHOT_STATEMENT_TIMEOUT_MS}")
        )
        for company_id, state, count, oldest in (
            await session.execute(
                select(runs.c.company_id, runs.c.state, func.count(), func.min(runs.c.queued_at))
                .where(runs.c.state.in_(_OPEN_STATES))
                .group_by(runs.c.company_id, runs.c.state)
            )
        ).all():
            entry = found.setdefault(company_id, {})
            if state == RunState.QUEUED.value:
                entry["queued"] = int(count)
                if oldest is not None:
                    entry["oldest_queued_s"] = max(0.0, (now - oldest).total_seconds())
            else:
                entry["running"] = entry.get("running", 0) + int(count)
        frozen = select(companies.c.id).where(companies.c.status == CompanyStatus.FROZEN.value)
        for company_id, overdue, expiring, earliest in (
            await session.execute(
                select(
                    credentials.c.company_id,
                    func.count().filter(expired),
                    func.count().filter(~expired),
                    func.min(credentials.c.not_after),
                )
                .where(
                    credentials.c.kind == CredentialKind.PFX_A1.value,
                    credentials.c.revoked_at.is_(None),
                    or_(credentials.c.not_after.is_(None), credentials.c.not_after <= horizon),
                    credentials.c.company_id.not_in(frozen),
                )
                .group_by(credentials.c.company_id)
            )
        ).all():
            entry = found.setdefault(company_id, {})
            entry["a1_expired"] = int(overdue)
            entry["a1_expiring"] = int(expiring)
            if earliest is not None:
                entry["a1_days_left"] = (earliest - now).total_seconds() / 86_400
    return PlatformSnapshot(
        read_at=now,
        companies={company_id: CompanyCounts(**values) for company_id, values in found.items()},
    )


async def watch_platform(
    metrics: Metrics,
    db: Database,
    stop: asyncio.Event,
    *,
    warn_days: int,
    clock: Callable[[], datetime],
    interval_s: float = SNAPSHOT_INTERVAL_S,
) -> None:
    """Lê a fila e os certificados a cada ``interval_s`` até ``stop``.

    Uma leitura que falha (banco fora) é registrada só pelo tipo do erro, uma vez até
    voltar a dar certo; a última leitura boa continua exposta, com a hora dela em
    ``rapa_banco_lido_em_segundos`` (quem consulta vê que ficou velha).
    """
    failing = False
    while not stop.is_set():
        try:
            snapshot = await read_platform_snapshot(db, now=clock(), warn_days=warn_days)
        except Exception as exc:  # banco fora, tempo esgotado: tenta na próxima
            if not failing:
                logger.warning("falha ao ler a fila para as métricas: %s", type(exc).__name__)
            failing = True
        else:
            if failing:
                logger.warning("a leitura da fila para as métricas voltou a funcionar")
            failing = False
            metrics.update_platform(snapshot)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


# ---------------------------------------------------------------------------
# Coletores lidos na hora da coleta
# ---------------------------------------------------------------------------


class _PlatformCollector:
    """A última leitura do banco, por empresa (só com o rótulo ligado) ou somada."""

    def __init__(self, per_company: bool) -> None:
        self.per_company = per_company
        self.snapshot: PlatformSnapshot | None = None

    def collect(self) -> Iterator[Metric]:
        snapshot = self.snapshot
        if snapshot is None:
            return
        labels = ["empresa"] if self.per_company else []
        queued = GaugeMetricFamily(
            "rapa_fila_execucoes", "Execuções esperando na fila.", labels=labels
        )
        waiting = GaugeMetricFamily(
            "rapa_fila_espera_maxima_segundos",
            "Há quanto tempo espera a execução mais antiga da fila.",
            labels=labels,
        )
        running = GaugeMetricFamily(
            "rapa_execucoes_em_andamento",
            "Execuções rodando ou esperando uma pessoa.",
            labels=labels,
        )
        a1 = GaugeMetricFamily(
            "rapa_a1_em_aviso",
            "Certificados A1 ativos no aviso de vencimento (vencendo ou vencidos).",
            labels=[*labels, "tipo"],
        )
        days = GaugeMetricFamily(
            "rapa_a1_dias_para_vencer",
            "Dias até o vencimento mais próximo entre os certificados A1 em aviso "
            "(negativo: já venceu).",
            labels=labels,
        )
        if self.per_company:
            for company_id, counts in sorted(snapshot.companies.items()):
                key = [str(company_id)]
                if counts.queued or counts.running:
                    queued.add_metric(key, counts.queued)
                    running.add_metric(key, counts.running)
                if counts.oldest_queued_s is not None:
                    waiting.add_metric(key, counts.oldest_queued_s)
                if counts.a1_expiring or counts.a1_expired:
                    a1.add_metric([*key, "vencendo"], counts.a1_expiring)
                    a1.add_metric([*key, "vencido"], counts.a1_expired)
                if counts.a1_days_left is not None:
                    days.add_metric(key, counts.a1_days_left)
        else:
            values = list(snapshot.companies.values())
            queued.add_metric([], sum(c.queued for c in values))
            running.add_metric([], sum(c.running for c in values))
            waits = [c.oldest_queued_s for c in values if c.oldest_queued_s is not None]
            waiting.add_metric([], max(waits, default=0.0))
            a1.add_metric(["vencendo"], sum(c.a1_expiring for c in values))
            a1.add_metric(["vencido"], sum(c.a1_expired for c in values))
            lefts = [c.a1_days_left for c in values if c.a1_days_left is not None]
            if lefts:
                days.add_metric([], min(lefts))
        read = GaugeMetricFamily(
            "rapa_banco_lido_em_segundos",
            "Quando a fila e os certificados foram lidos do banco (segundos desde 1970).",
            value=snapshot.read_at.timestamp(),
        )
        yield from (queued, waiting, running, a1, days, read)


class _SupervisorView(Protocol):
    """O que as métricas leem do :class:`rapa.server.DispatcherSupervisor`."""

    @property
    def healthy(self) -> bool: ...

    restarts: int
    dispatcher: Any


class _DispatcherCollector:
    """A saúde do despachante e de cada etapa do laço, lida na hora da coleta."""

    def __init__(self) -> None:
        self.supervisor: _SupervisorView | None = None

    def collect(self) -> Iterator[Metric]:
        supervisor = self.supervisor
        if supervisor is None:
            return
        try:
            yield from self._collect(supervisor)
        except Exception as exc:  # uma leitura no meio de uma troca: fica para a próxima coleta
            logger.debug("falha ao ler a saúde do despachante: %s", type(exc).__name__)

    @staticmethod
    def _collect(supervisor: _SupervisorView) -> Iterator[Metric]:
        dispatcher = supervisor.dispatcher
        yield GaugeMetricFamily(
            "rapa_despachante_saudavel",
            "1 quando o despachante roda e nenhuma etapa do laço falha há tempo demais.",
            value=1.0 if supervisor.healthy else 0.0,
        )
        yield CounterMetricFamily(
            "rapa_despachante_reinicios",
            "Vezes que o despachante parou sem pedido e foi reiniciado.",
            value=float(supervisor.restarts),
        )
        yield GaugeMetricFamily(
            "rapa_despachante_volta_presa",
            "1 quando uma volta do laço está presa há tempo demais.",
            value=1.0 if bool(getattr(dispatcher, "stalled", False)) else 0.0,
        )
        yield GaugeMetricFamily(
            "rapa_despachante_execucoes_em_andamento",
            "Execuções que este despachante acompanha agora.",
            value=float(getattr(dispatcher, "in_flight", 0)),
        )
        status = getattr(dispatcher, "step_status", None)
        steps = status() if callable(status) else {}
        ok = GaugeMetricFamily(
            "rapa_despachante_etapa_ok",
            "1 quando a etapa do laço deu certo na última vez.",
            labels=["etapa"],
        )
        failures = GaugeMetricFamily(
            "rapa_despachante_etapa_falhas_seguidas",
            "Falhas seguidas da etapa do laço.",
            labels=["etapa"],
        )
        last_ok = GaugeMetricFamily(
            "rapa_despachante_etapa_ultimo_sucesso_segundos",
            "Quando a etapa do laço deu certo pela última vez (segundos desde 1970).",
            labels=["etapa"],
        )
        for name, step in sorted(steps.items()):
            ok.add_metric([name], 0.0 if step.failures else 1.0)
            failures.add_metric([name], float(step.failures))
            if step.last_ok is not None:
                last_ok.add_metric([name], step.last_ok.timestamp())
        yield from (ok, failures, last_ok)


class _OpenSessions(Protocol):
    @property
    def open_count(self) -> int: ...


class _SessionsCollector:
    """As sessões de gravação abertas por este processo."""

    def __init__(self) -> None:
        self.sessions: _OpenSessions | None = None

    def collect(self) -> Iterator[Metric]:
        sessions = self.sessions
        if sessions is None:
            return
        yield GaugeMetricFamily(
            "rapa_sessoes_abertas",
            "Sessões de gravação abertas por este processo.",
            value=float(sessions.open_count),
        )


# ---------------------------------------------------------------------------
# As métricas do processo
# ---------------------------------------------------------------------------


class Metrics:
    """As métricas de um processo, num registro próprio (nada no registro global)."""

    def __init__(self, *, per_company: bool = False, version: str = "0") -> None:
        # Sem as séries ``*_created`` de cada contador: dobrariam as séries sem dizer nada.
        disable_created_metrics()  # type: ignore[no-untyped-call]
        registry = CollectorRegistry()
        self.registry = registry
        self.per_company = per_company
        # As métricas de processo (memória, CPU, descritores, coleta de lixo).
        ProcessCollector(registry=registry)
        PlatformCollector(registry=registry)
        if GC_COLLECTOR is not None:
            GCCollector(registry=registry)
        Gauge("rapa_info", "Versão do RaPA neste processo.", ["versao"], registry=registry).labels(
            version
        ).set(1)
        run_labels = ["situacao", "origem", *(["empresa"] if per_company else [])]
        self.runs = Counter(
            "rapa_execucoes",
            "Execuções concluídas pelo despachante deste processo, por situação e origem.",
            run_labels,
            registry=registry,
        )
        self.run_duration = Histogram(
            "rapa_execucao_duracao_segundos",
            "Duração das execuções concluídas, do início ao fim da tentativa.",
            ["situacao"],
            buckets=_RUN_BUCKETS,
            registry=registry,
        )
        self.webhook_deliveries = Counter(
            "rapa_webhook_entregas",
            "Entregas de webhook recebidas, por resultado (aceita, repetida ou o código do erro).",
            ["resultado"],
            registry=registry,
        )
        self.http_requests = Counter(
            "rapa_http_pedidos",
            "Pedidos HTTP por método, rota modelo e código da resposta.",
            ["metodo", "rota", "codigo"],
            registry=registry,
        )
        self.http_duration = Histogram(
            "rapa_http_duracao_segundos",
            "Duração dos pedidos HTTP por método e rota modelo.",
            ["metodo", "rota"],
            buckets=_HTTP_BUCKETS,
            registry=registry,
        )
        self._platform = _PlatformCollector(per_company)
        self._dispatcher = _DispatcherCollector()
        self._sessions = _SessionsCollector()
        for collector in (self._platform, self._dispatcher, self._sessions):
            registry.register(collector)

    # -- o que os serviços informam ---------------------------------------------

    def run_concluded(self, run: ConcludedRun) -> None:
        """Uma execução concluída (o observador de :class:`rapa.control.services.queue.PgRunQueue`)."""
        state = run.state.value if run.state.value in _STATES else "outra"
        trigger = run.trigger if run.trigger in _TRIGGERS else "outra"
        labels = [state, trigger]
        if self.per_company:
            labels.append(str(run.company_id))
        self.runs.labels(*labels).inc()
        if run.duration_s is not None and run.duration_s >= 0:
            self.run_duration.labels(state).observe(run.duration_s)

    def webhook_delivery(self, result: str) -> None:
        """Uma entrega de webhook: ``aceita``, ``repetida`` ou o código estável do erro."""
        self.webhook_deliveries.labels(result if _RESULT.fullmatch(result) else "erro").inc()

    def http_request(
        self, scope: Scope, status: int, elapsed_s: float, routes: Sequence[BaseRoute]
    ) -> None:
        """Um pedido HTTP, pela rota modelo (``scope`` é o de antes do roteamento)."""
        method = str(scope.get("method", "")).upper()
        method = method if method in _METHODS else "outro"
        route = route_template(routes, scope) or UNMATCHED_ROUTE
        code = str(status) if 100 <= status <= 599 else "outro"
        self.http_requests.labels(method, route, code).inc()
        self.http_duration.labels(method, route).observe(max(0.0, elapsed_s))

    def update_platform(self, snapshot: PlatformSnapshot) -> None:
        """Troca a leitura do banco exposta (a fila e os avisos do A1)."""
        self._platform.snapshot = snapshot

    def watch_dispatcher(self, supervisor: _SupervisorView | None) -> None:
        """Passa a expor a saúde do despachante deste processo."""
        self._dispatcher.supervisor = supervisor

    def watch_sessions(self, sessions: _OpenSessions | None) -> None:
        """Passa a expor as sessões de gravação abertas por este processo."""
        self._sessions.sessions = sessions

    def render(self) -> bytes:
        """O texto da coleta, no formato do Prometheus."""
        from prometheus_client import generate_latest

        return generate_latest(self.registry)


# ---------------------------------------------------------------------------
# Pedidos HTTP pela rota modelo
# ---------------------------------------------------------------------------


def route_template(routes: Sequence[BaseRoute], scope: Scope, _depth: int = 0) -> str | None:
    """A rota modelo (``/v1/execucoes/{run_id}``) que casa com o pedido, ou ``None``.

    ``scope`` é o do pedido antes do roteamento. As rotas de um ``include_router`` são
    vistas com o caminho inteiro (``iter_route_contexts`` do FastAPI), e os ``Mount`` (a
    interface montada em ``/``) somam o prefixo de cada um. Um método que a rota não
    aceita (405) ainda conta pela rota. Nunca devolve o caminho cru.
    """
    partial: tuple[RouteContext, Scope] | None = None
    for context in iter_route_contexts(routes):
        match, child = context.matches(scope)
        if match is Match.FULL:
            return _template(context, {**scope, **child}, _depth)
        if match is Match.PARTIAL and partial is None:
            partial = (context, {**scope, **child})
    if partial is not None:
        return _template(partial[0], partial[1], _depth)
    return None


def _template(context: RouteContext, scope: Scope, depth: int) -> str | None:
    path = context.path
    if not isinstance(path, str):
        return None
    route = context.original_route
    if not isinstance(route, Mount):
        return path
    inner = getattr(route.app, "routes", None)
    if inner is None:
        # Um app sem rotas (arquivos estáticos): o prefixo e o resto do caminho como modelo.
        return route.path_format
    if depth >= _MAX_MOUNT_DEPTH:
        return None
    sub = route_template(inner, scope, depth + 1)
    return None if sub is None else path + sub


class HttpMetricsMiddleware:
    """Conta cada pedido HTTP pela rota modelo, pelo método e pelo código da resposta.

    Fica por fora de tudo (inclusive dos 413 e 503 das proteções). Um erro que escapa
    antes da resposta conta como 500. A coleta nunca muda o pedido nem a resposta.
    """

    def __init__(
        self, app: ASGIApp, *, metrics: Metrics, routes: Callable[[], Sequence[BaseRoute]]
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Antes do roteamento, que mexe no scope (raiz dos Mount, parâmetros do caminho).
        original = dict(scope)
        status = 500
        started = time.perf_counter()

        async def tracking_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        finally:
            try:
                self.metrics.http_request(
                    original, status, time.perf_counter() - started, self.routes()
                )
            except Exception as exc:  # a métrica nunca derruba o pedido
                logger.debug("falha ao contar o pedido HTTP: %s", type(exc).__name__)


def instrument_http(app: FastAPI, metrics: Metrics) -> None:
    """Põe :class:`HttpMetricsMiddleware` por fora dos middlewares que ``app`` já tem."""
    app.add_middleware(HttpMetricsMiddleware, metrics=metrics, routes=lambda: app.routes)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class _Receiver(Protocol):
    async def deliver(
        self,
        webhook_id: UUID,
        *,
        timestamp: str | None,
        delivery_id: str | None,
        signature: str | None,
        body: bytes,
        client: str | None = None,
    ) -> WebhookDelivery: ...


class MeteredWebhooks:
    """O recebedor das entregas de webhook, contando cada uma pelo resultado.

    ``aceita`` (pôs uma execução na fila), ``repetida`` (a mesma entrega de novo) ou o
    código estável do erro (``assinatura_invalida``, ``limite_excedido``, ``conflito``,
    ``empresa_congelada``, ``entrada_invalida``...); um erro inesperado conta como ``erro``.
    """

    def __init__(self, inner: _Receiver, metrics: Metrics) -> None:
        self.inner = inner
        self.metrics = metrics

    def __repr__(self) -> str:
        return f"MeteredWebhooks({self.inner!r})"

    async def deliver(
        self,
        webhook_id: UUID,
        *,
        timestamp: str | None,
        delivery_id: str | None,
        signature: str | None,
        body: bytes,
        client: str | None = None,
    ) -> WebhookDelivery:
        try:
            delivery = await self.inner.deliver(
                webhook_id,
                timestamp=timestamp,
                delivery_id=delivery_id,
                signature=signature,
                body=body,
                client=client,
            )
        except ContractError as exc:
            self.metrics.webhook_delivery(exc.code)
            raise
        except Exception:
            self.metrics.webhook_delivery("erro")
            raise
        self.metrics.webhook_delivery("repetida" if delivery.duplicate else "aceita")
        return delivery


# ---------------------------------------------------------------------------
# O servidor de métricas
# ---------------------------------------------------------------------------


class _QuietHandler(WSGIRequestHandler):
    """Sem uma linha de log por coleta, e com o prazo do servidor em cada leitura e escrita."""

    server: _MetricsWSGIServer

    def setup(self) -> None:
        super().setup()
        # Vale para as leituras e escritas pelos arquivos do socket, criados acima.
        self.connection.settimeout(self.server.timeout_s)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _MetricsWSGIServer(ThreadingWSGIServer):
    """Uma thread por conexão, até ``max_connections``; a de mais é fechada sem resposta."""

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[WSGIRequestHandler],
        *,
        timeout_s: float,
        max_connections: int,
    ) -> None:
        if timeout_s <= 0 or max_connections < 1:
            raise ValueError("o prazo e as conexões do servidor de métricas precisam ser positivos")
        self.timeout_s = timeout_s
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Um prazo vencido ou o coletor que desistiu: nada de pilha no stderr.
        return


class _MetricsWSGIServer6(_MetricsWSGIServer):
    address_family = socket.AF_INET6


def _only_metrics(app: Callable[..., Iterable[bytes]]) -> Callable[..., Iterable[bytes]]:
    """Só ``/metrics``; qualquer outro caminho é 404."""

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics":
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"nao encontrado\n"]
        return app(environ, start_response)

    return guarded


class MetricsServer:
    """O servidor de métricas rodando numa thread própria."""

    def __init__(self, server: ThreadingWSGIServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def serve_metrics(
    metrics: Metrics,
    port: int,
    host: str = "127.0.0.1",
    *,
    timeout_s: float = METRICS_TIMEOUT_S,
    max_connections: int = METRICS_MAX_CONNECTIONS,
) -> MetricsServer:
    """Sobe o servidor de ``GET /metrics`` em ``host:port``, numa thread do processo.

    ``host`` é o endereço do serviço (o ``--host``; padrão, só ``127.0.0.1``). Cada
    leitura e escrita de uma conexão tem ``timeout_s``, e só ``max_connections`` são
    atendidas ao mesmo tempo. Levanta ``OSError`` se a porta estiver ocupada: o processo
    não sobe sem as métricas que foram pedidas.
    """
    server_class = _MetricsWSGIServer6 if ":" in host else _MetricsWSGIServer
    server = server_class(
        (host, port), _QuietHandler, timeout_s=timeout_s, max_connections=max_connections
    )
    server.set_app(_only_metrics(make_wsgi_app(metrics.registry)))
    thread = threading.Thread(target=server.serve_forever, name="rapa-metricas", daemon=True)
    thread.start()
    return MetricsServer(server, thread)
