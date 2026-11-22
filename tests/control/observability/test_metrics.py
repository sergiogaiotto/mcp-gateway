"""Métricas Prometheus: rota modelo, rótulos limitados, empresa só quando pedida, porta própria."""

import asyncio
import logging
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from rapa.control.observability import metrics as metrics_module
from rapa.control.observability.metrics import (
    UNMATCHED_ROUTE,
    CompanyCounts,
    HttpMetricsMiddleware,
    MeteredWebhooks,
    Metrics,
    PlatformSnapshot,
    instrument_http,
    route_template,
    serve_metrics,
    watch_platform,
)
from rapa.control.services import Dispatcher, ReapReport
from rapa.control.services.queue import ConcludedRun
from rapa.control.sessions import RecordingSessions
from rapa.core.contracts import (
    CompanyFrozen,
    InvalidInput,
    QuotaExceeded,
    RunState,
    SignatureInvalid,
    WebhookDelivery,
)
from rapa.core.ids import new_id
from rapa.server import DispatcherSupervisor
from tests.control.api.support import INPUTS, TOKEN_A, asgi_client, build_world, make_app

from .support import UUID_PATTERN, samples, value


def _scope(path: str, method: str = "GET") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


# ---------------------------------------------------------------------------
# Pedidos HTTP
# ---------------------------------------------------------------------------


async def test_requests_are_counted_by_route_template_never_by_raw_path() -> None:
    world = await build_world()
    metrics = Metrics()
    app = make_app(world)
    instrument_http(app, metrics)
    run_id = new_id()
    async with asgi_client(app, TOKEN_A) as client:
        assert (await client.get(f"/v1/execucoes/{run_id}")).status_code == 404
        assert (await client.get(f"/v1/execucoes/{new_id()}")).status_code == 404
        assert (await client.get(f"/caminho/{run_id}/que/nao/existe")).status_code == 404
        # Um método que ninguém usa conta como "outro", pela rota que casou (405).
        assert (await client.request("BREW", "/v1/catalogo")).status_code == 405
        # O 413 da proteção do corpo (por fora das rotas) também conta pela rota.
        big = {"entradas": {**INPUTS, "nome": "x" * (2 * 1024 * 1024)}}
        response = await client.post("/v1/catalogo/emitir_protocolo/execucoes", json=big)
        assert response.status_code == 413
    requests = samples(metrics, "rapa_http_pedidos_total")
    assert requests == {
        (("codigo", "404"), ("metodo", "GET"), ("rota", "/v1/execucoes/{run_id}")): 2.0,
        (("codigo", "404"), ("metodo", "GET"), ("rota", UNMATCHED_ROUTE)): 1.0,
        (("codigo", "405"), ("metodo", "outro"), ("rota", "/v1/catalogo")): 1.0,
        (("codigo", "413"), ("metodo", "POST"), ("rota", "/v1/catalogo/{slug}/execucoes")): 1.0,
    }
    assert (
        value(
            metrics, "rapa_http_duracao_segundos_count", metodo="GET", rota="/v1/execucoes/{run_id}"
        )
        == 2.0
    )
    # Nem o id da execução nem o caminho cru aparecem em lugar nenhum.
    text = metrics.render().decode()
    assert str(run_id) not in text and "/caminho/" not in text
    assert UUID_PATTERN.search(text) is None


def test_route_template_goes_through_routers_and_mounts(tmp_path: Path) -> None:
    inner = FastAPI()

    @inner.get("/paginas/{page_id}")
    async def page(page_id: str) -> dict[str, str]:  # pragma: no cover - só o roteamento
        return {}

    router = APIRouter(prefix="/v2")

    @router.get("/itens/{item_id}")
    async def item(item_id: str) -> dict[str, str]:  # pragma: no cover - só o roteamento
        return {}

    outer = FastAPI()
    outer.include_router(router)
    outer.mount("/estaticos", StaticFiles(directory=tmp_path))
    outer.mount("/ui", inner)
    routes = outer.routes
    assert route_template(routes, _scope("/v2/itens/42")) == "/v2/itens/{item_id}"
    assert route_template(routes, _scope("/ui/paginas/7")) == "/ui/paginas/{page_id}"
    assert route_template(routes, _scope("/estaticos/a/b.css")) == "/estaticos/{path}"
    # Dentro de um Mount, o que não casa com nenhuma rota dele também não tem modelo.
    assert route_template(routes, _scope("/ui/nada/9")) is None
    assert route_template(routes, _scope("/v2/itens/42", "POST")) == "/v2/itens/{item_id}"


async def test_an_error_before_the_response_counts_as_500_and_still_propagates() -> None:
    metrics = Metrics()

    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("falha com detalhe interno")

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        return None

    middleware = HttpMetricsMiddleware(
        boom, metrics=metrics, routes=lambda: [Route("/robos/{robot_id}", endpoint=endpoint)]
    )

    async def receive() -> dict[str, Any]:  # pragma: no cover - nada é lido
        return {"type": "http.request", "body": b""}

    async def send(message: Any) -> None:  # pragma: no cover - nada é enviado
        return None

    with pytest.raises(RuntimeError):
        await middleware(_scope(f"/robos/{new_id()}"), receive, send)
    assert (
        value(
            metrics, "rapa_http_pedidos_total", metodo="GET", rota="/robos/{robot_id}", codigo="500"
        )
        == 1.0
    )


# ---------------------------------------------------------------------------
# Execuções, webhooks, fila, despachante e sessões
# ---------------------------------------------------------------------------


def test_runs_are_counted_by_state_and_origin_and_by_company_only_when_asked() -> None:
    company = new_id()
    metrics = Metrics()
    metrics.run_concluded(ConcludedRun(company, RunState.SUCCEEDED, "rest", 12.5))
    metrics.run_concluded(ConcludedRun(company, RunState.SUCCEEDED, "rest", 3.0))
    metrics.run_concluded(ConcludedRun(company, RunState.FAILED, "uma-origem-nova", None))
    assert samples(metrics, "rapa_execucoes_total") == {
        (("origem", "rest"), ("situacao", "succeeded")): 2.0,
        # Origem fora da lista conhecida: "outra" (o rótulo nunca cresce sem limite).
        (("origem", "outra"), ("situacao", "failed")): 1.0,
    }
    assert value(metrics, "rapa_execucao_duracao_segundos_count", situacao="succeeded") == 2.0
    assert value(metrics, "rapa_execucao_duracao_segundos_sum", situacao="succeeded") == 15.5
    # Sem início (não chegou a rodar), não entra na duração.
    assert value(metrics, "rapa_execucao_duracao_segundos_count", situacao="failed") is None
    assert str(company) not in metrics.render().decode()

    per_company = Metrics(per_company=True)
    per_company.run_concluded(ConcludedRun(company, RunState.INDETERMINATE, "schedule", 1.0))
    assert samples(per_company, "rapa_execucoes_total") == {
        (("empresa", str(company)), ("origem", "schedule"), ("situacao", "indeterminate")): 1.0
    }
    # A duração nunca tem a empresa (é um histograma: muitas séries por rótulo).
    assert (
        value(per_company, "rapa_execucao_duracao_segundos_count", situacao="indeterminate") == 1.0
    )


class _Receiver:
    def __init__(self, outcome: WebhookDelivery | Exception) -> None:
        self.outcome = outcome

    async def deliver(self, webhook_id: UUID, **kwargs: Any) -> WebhookDelivery:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_webhook_deliveries_are_counted_by_result() -> None:
    metrics = Metrics()
    delivery = WebhookDelivery(
        webhook_id=new_id(),
        delivery_id="entrega-1",
        run_id=new_id(),
        state=RunState.QUEUED,
        duplicate=False,
    )
    outcomes: list[WebhookDelivery | Exception] = [
        delivery,
        delivery.model_copy(update={"duplicate": True}),
        SignatureInvalid("assinatura inválida"),
        SignatureInvalid("assinatura inválida"),
        QuotaExceeded("muitas", retry_after_s=3),
        CompanyFrozen("congelada"),
        InvalidInput("entradas inválidas"),
        RuntimeError("falha inesperada"),
    ]
    for outcome in outcomes:
        receiver = MeteredWebhooks(_Receiver(outcome), metrics)
        call = receiver.deliver(
            new_id(), timestamp="1", delivery_id="entrega-1", signature="s", body=b"{}"
        )
        if isinstance(outcome, Exception):
            # O erro segue para a API como antes: a métrica só conta.
            with pytest.raises(type(outcome)):
                await call
        else:
            assert await call is outcome
    assert samples(metrics, "rapa_webhook_entregas_total") == {
        (("resultado", "aceita"),): 1.0,
        (("resultado", "repetida"),): 1.0,
        (("resultado", "assinatura_invalida"),): 2.0,
        (("resultado", "limite_excedido"),): 1.0,
        (("resultado", "empresa_congelada"),): 1.0,
        (("resultado", "entrada_invalida"),): 1.0,
        (("resultado", "erro"),): 1.0,
    }
    # Um resultado fora do formato de código nunca vira rótulo.
    metrics.webhook_delivery("Assinatura Inválida: 9f2c...")
    assert value(metrics, "rapa_webhook_entregas_total", resultado="erro") == 2.0


def _snapshot() -> tuple[UUID, UUID, PlatformSnapshot]:
    first, second = new_id(), new_id()
    snapshot = PlatformSnapshot(
        read_at=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        companies={
            first: CompanyCounts(queued=3, oldest_queued_s=40.0, running=2, a1_expiring=1),
            second: CompanyCounts(
                queued=2, oldest_queued_s=90.0, a1_expired=1, a1_expiring=1, a1_days_left=-2.5
            ),
        },
    )
    return first, second, snapshot


def test_queue_and_a1_gauges_are_summed_unless_asked_per_company() -> None:
    first, second, snapshot = _snapshot()
    metrics = Metrics()
    # Antes da primeira leitura do banco, nada (nem zeros que pareceriam verdade).
    assert samples(metrics, "rapa_fila_execucoes") == {}
    metrics.update_platform(snapshot)
    assert value(metrics, "rapa_fila_execucoes") == 5.0
    assert value(metrics, "rapa_fila_espera_maxima_segundos") == 90.0
    assert value(metrics, "rapa_execucoes_em_andamento") == 2.0
    assert value(metrics, "rapa_a1_em_aviso", tipo="vencendo") == 2.0
    assert value(metrics, "rapa_a1_em_aviso", tipo="vencido") == 1.0
    assert value(metrics, "rapa_a1_dias_para_vencer") == -2.5
    assert value(metrics, "rapa_banco_lido_em_segundos") == snapshot.read_at.timestamp()
    assert UUID_PATTERN.search(metrics.render().decode()) is None

    per_company = Metrics(per_company=True)
    per_company.update_platform(snapshot)
    assert samples(per_company, "rapa_fila_execucoes") == {
        (("empresa", str(first)),): 3.0,
        (("empresa", str(second)),): 2.0,
    }
    assert samples(per_company, "rapa_a1_dias_para_vencer") == {(("empresa", str(second)),): -2.5}
    assert value(per_company, "rapa_a1_em_aviso", empresa=str(first), tipo="vencendo") == 1.0


async def test_the_platform_watch_survives_database_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, snapshot = _snapshot()
    calls = 0

    async def read(db: Any, *, now: datetime, warn_days: int) -> PlatformSnapshot:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError("conexão recusada para rapa_dispatcher:senha@banco")
        assert warn_days == 30
        return snapshot

    monkeypatch.setattr(metrics_module, "read_platform_snapshot", read)
    metrics = Metrics()
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="rapa.control.metricas"):
        task = asyncio.create_task(
            watch_platform(
                metrics,
                object(),  # type: ignore[arg-type]
                stop,
                warn_days=30,
                clock=lambda: datetime.now(UTC),
                interval_s=0.01,
            )
        )
        for _ in range(500):
            if value(metrics, "rapa_fila_execucoes") is not None:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert value(metrics, "rapa_fila_execucoes") == 5.0
    # Duas falhas seguidas, um aviso só (e só o tipo do erro); depois, a volta.
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "falha ao ler a fila para as métricas: OSError",
        "a leitura da fila para as métricas voltou a funcionar",
    ]
    assert "senha" not in caplog.text


class _PartitionsFail:
    """Fila em que só a criação de partições falha."""

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        raise PermissionError("sem permissão: detalhe interno")

    async def reap_expired(self, *, now: Any = None, skip: Any = ()) -> ReapReport:
        return ReapReport()

    async def claim_next(self, *, now: Any = None) -> None:
        return None


async def _never_launch(job: Any) -> dict[str, Any]:  # pragma: no cover - a fila está vazia
    raise AssertionError("nada para lançar")


async def test_dispatcher_health_is_exposed_per_loop_step() -> None:
    dispatcher = Dispatcher(
        _PartitionsFail(),  # type: ignore[arg-type]
        _never_launch,
        poll_interval=0.01,
        unhealthy_after_s=0.05,
    )
    supervisor = DispatcherSupervisor(dispatcher)
    metrics = Metrics()
    metrics.watch_dispatcher(supervisor)
    stop = asyncio.Event()
    task = supervisor.start(stop)
    try:
        for _ in range(500):
            if not supervisor.healthy and dispatcher.step_status()["claim"].last_ok is not None:
                break
            await asyncio.sleep(0.01)
        assert value(metrics, "rapa_despachante_saudavel") == 0.0
        ok = samples(metrics, "rapa_despachante_etapa_ok")
        # Só as etapas que este despachante roda (sem agendador, avisos do A1 ou retenção).
        assert ok == {
            (("etapa", "claim"),): 1.0,
            (("etapa", "partitions"),): 0.0,
            (("etapa", "reap"),): 1.0,
        }
        failures = value(metrics, "rapa_despachante_etapa_falhas_seguidas", etapa="partitions")
        assert failures is not None and failures >= 1
        assert value(metrics, "rapa_despachante_etapa_ultimo_sucesso_segundos", etapa="reap")
        assert value(metrics, "rapa_despachante_reinicios_total") == 0.0
        assert value(metrics, "rapa_despachante_execucoes_em_andamento") == 0.0
        assert "detalhe interno" not in metrics.render().decode()
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


def test_open_recording_sessions_are_a_gauge() -> None:
    sessions = RecordingSessions(None, None, None)  # type: ignore[arg-type]
    metrics = Metrics()
    assert samples(metrics, "rapa_sessoes_abertas") == {}
    metrics.watch_sessions(sessions)
    assert value(metrics, "rapa_sessoes_abertas") == 0.0
    sessions._live[new_id()] = new_id()
    sessions._live[new_id()] = new_id()
    assert sessions.open_count == 2
    assert value(metrics, "rapa_sessoes_abertas") == 2.0


def test_process_metrics_and_version_are_exposed() -> None:
    metrics = Metrics(version="1.2.3")
    text = metrics.render().decode()
    assert value(metrics, "rapa_info", versao="1.2.3") == 1.0
    assert "python_info" in text
    assert "process_resident_memory_bytes" in text
    # Sem as séries *_created, que dobrariam as séries sem dizer nada.
    metrics.run_concluded(ConcludedRun(new_id(), RunState.SUCCEEDED, "rest", 1.0))
    assert "_created" not in metrics.render().decode()


# ---------------------------------------------------------------------------
# O servidor de métricas
# ---------------------------------------------------------------------------


def test_metrics_server_serves_only_get_metrics() -> None:
    metrics = Metrics()
    server = serve_metrics(metrics, 0, host="127.0.0.1")
    base = f"http://127.0.0.1:{server.port}"
    try:
        response = httpx2.get(f"{base}/metrics", timeout=5)
        assert response.status_code == 200
        assert 'rapa_info{versao="0"} 1.0' in response.text
        for path in ("/", "/saude", "/metrics/extra", "/v1/execucoes"):
            assert httpx2.get(f"{base}{path}", timeout=5).status_code == 404
        assert httpx2.post(f"{base}/metrics", timeout=5).status_code == 405
    finally:
        server.stop()
    with pytest.raises(httpx2.TransportError):
        httpx2.get(f"{base}/metrics", timeout=1)


def test_metrics_server_refuses_a_busy_port() -> None:
    metrics = Metrics()
    first = serve_metrics(metrics, 0, host="127.0.0.1")
    try:
        with pytest.raises(OSError):
            serve_metrics(metrics, first.port, host="127.0.0.1")
    finally:
        first.stop()


def test_metrics_server_listens_only_on_loopback_by_default() -> None:
    server = serve_metrics(Metrics(), 0)
    try:
        assert server._server.server_address[0] == "127.0.0.1"
        assert httpx2.get(f"http://127.0.0.1:{server.port}/metrics", timeout=5).status_code == 200
    finally:
        server.stop()


def test_metrics_server_also_listens_on_ipv6() -> None:
    if not socket.has_ipv6:  # pragma: no cover - depende da máquina
        pytest.skip("sem IPv6 nesta máquina")
    try:
        server = serve_metrics(Metrics(), 0, host="::1")
    except OSError:  # pragma: no cover - depende da máquina
        pytest.skip("sem ::1 nesta máquina")
    try:
        response = httpx2.get(f"http://[::1]:{server.port}/metrics", timeout=5)
        assert response.status_code == 200
    finally:
        server.stop()


def _closed_by_server(sock: socket.socket, timeout_s: float) -> bool:
    sock.settimeout(timeout_s)
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True
    except TimeoutError:
        return False


def _connection_threads() -> int:
    """As threads que atendem uma conexão do servidor de métricas."""
    return sum("process_request_thread" in thread.name for thread in threading.enumerate())


def test_idle_connections_never_pile_up_threads(capfd: pytest.CaptureFixture[str]) -> None:
    """Conexões paradas: prazo em cada leitura e um teto de conexões atendidas juntas."""
    server = serve_metrics(Metrics(), 0, host="127.0.0.1", timeout_s=0.5, max_connections=3)
    base = f"http://127.0.0.1:{server.port}"
    try:
        idle = [socket.create_connection(("127.0.0.1", server.port)) for _ in range(20)]
        try:
            time.sleep(0.2)
            # Só três threads, por mais conexões que cheguem; as de mais fecham na hora.
            assert 1 <= _connection_threads() <= 3
            # As atendidas também fecham, quando o prazo da leitura vence.
            assert all(_closed_by_server(sock, 5.0) for sock in idle)
        finally:
            for sock in idle:
                sock.close()
        deadline = time.monotonic() + 5
        while _connection_threads() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _connection_threads() == 0
        # Um coletor que manda o pedido pela metade também é solto pelo prazo.
        with socket.create_connection(("127.0.0.1", server.port)) as slow:
            slow.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n")
            assert _closed_by_server(slow, 5.0)
        # E a coleta de verdade continua respondendo.
        assert httpx2.get(f"{base}/metrics", timeout=5).status_code == 200
    finally:
        server.stop()
    # Nenhuma pilha no stderr pelos prazos vencidos.
    assert "Traceback" not in capfd.readouterr().err


def test_metrics_server_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positivos"):
        serve_metrics(Metrics(), 0, timeout_s=0.0)
    with pytest.raises(ValueError, match="positivos"):
        serve_metrics(Metrics(), 0, max_connections=0)


def test_every_series_is_bounded_after_real_traffic() -> None:
    """Nenhuma métrica com id de execução, de webhook ou de empresa, nem caminho cru."""
    metrics = Metrics()
    company, run_id = new_id(), new_id()
    for state in RunState:
        metrics.run_concluded(ConcludedRun(company, state, "webhook", 2.0))
    _, _, snapshot = _snapshot()
    metrics.update_platform(snapshot)
    routes = [Route("/v1/execucoes/{run_id}", endpoint=lambda request: None)]
    for path in (f"/v1/execucoes/{run_id}", f"/v1/webhooks/{new_id()}", "/../../etc/passwd"):
        metrics.http_request(_scope(path), 200, 0.01, routes)
    metrics.http_request(_scope("/x", "DELETE"), 999, -1.0, routes)
    text = metrics.render().decode()
    assert UUID_PATTERN.search(text) is None
    assert "passwd" not in text and "webhooks" not in text
    rotas = {dict(labels)["rota"] for labels in samples(metrics, "rapa_http_pedidos_total")}
    assert rotas == {"/v1/execucoes/{run_id}", UNMATCHED_ROUTE}
    assert value(
        metrics, "rapa_http_pedidos_total", metodo="DELETE", rota=UNMATCHED_ROUTE, codigo="outro"
    )


def test_the_platform_snapshot_age_is_visible() -> None:
    metrics = Metrics()
    read_at = datetime.now(UTC) - timedelta(minutes=5)
    metrics.update_platform(PlatformSnapshot(read_at=read_at))
    # Sem nenhuma empresa com fila: zeros (somado), e a hora da leitura para ver a idade.
    assert value(metrics, "rapa_fila_execucoes") == 0.0
    assert value(metrics, "rapa_fila_espera_maxima_segundos") == 0.0
    assert value(metrics, "rapa_banco_lido_em_segundos") == read_at.timestamp()
    assert samples(metrics, "rapa_a1_dias_para_vencer") == {}
