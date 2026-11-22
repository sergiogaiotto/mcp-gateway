"""OpenTelemetry: nada sem o endpoint; com ele, pedidos, clientes e execuções sem dados."""

import asyncio
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import httpx2
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from rapa.control.observability import tracing as tracing_module
from rapa.control.observability.logs import JsonFormatter
from rapa.control.observability.tracing import (
    SAFE_ATTRIBUTES,
    Tracing,
    activate_tracing,
    current_trace_id,
    scrub_span,
)
from rapa.control.services import Dispatcher, ReapReport
from rapa.core.contracts import RunnerJob
from rapa.core.ids import new_id
from tests.control.api.support import INPUTS, TOKEN_A, asgi_client, build_world, make_app, serve
from tests.control.db.support import protocolo

#: Valores que nunca podem sair num span.
SECRET_INPUT = "valor-sigiloso-da-entrada"
SECRET_TOKEN = TOKEN_A
SECRET_QUERY = "consulta-sigilosa"
SECRET_OUTPUT = "saida-sigilosa"
SECRET_MESSAGE = "mensagem-sigilosa-do-erro"


def _dump(spans: Any) -> str:
    return "\n".join(span.to_json() for span in spans)


@pytest.fixture
def traced() -> Iterator[tuple[Tracing, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    tracing = activate_tracing(endpoint="http://coletor.exemplo:4318", exporter=exporter)
    try:
        yield tracing, exporter
    finally:
        tracing.shutdown()
    assert current_trace_id() is None


def test_without_the_endpoint_nothing_of_opentelemetry_or_sentry_is_loaded() -> None:
    program = """
import json, logging, sys, tempfile
from pathlib import Path
from rapa.server import ServerSettings, create_server_app
from rapa.control.observability.logs import JsonFormatter
tokens = Path(tempfile.mkdtemp()) / "t.json"
tokens.write_text(json.dumps({"t": {"sujeito": "ana", "empresa": "COMPANY"}}))
settings = ServerSettings.from_env({
    "RAPA_DATABASE_URL": "postgresql://rapa_app:x@127.0.0.1:1/rapa",
    "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:x@127.0.0.1:1/rapa",
    "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(tokens), "RAPA_UI": "0",
    "RAPA_VIDEO_EM_FALHAS": "0",
})
app = create_server_app(settings)
JsonFormatter().format(logging.LogRecord("x", 20, "x.py", 1, "oi", (), None))
loaded = sorted(
    name for name in sys.modules
    if name.startswith(("opentelemetry.sdk", "opentelemetry.instrumentation",
                        "opentelemetry.exporter", "sentry_sdk"))
)
print(json.dumps({"loaded": loaded, "tracing": app.state.tracing is None,
                  "tracer": app.state.dispatcher.tracer is None}))
""".replace("COMPANY", str(new_id()))
    done = subprocess.run(  # noqa: S603 - comando fixo, sem shell
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"loaded": [], "tracing": True, "tracer": True}


def _span(**overrides: Any) -> ReadableSpan:
    context = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    values: dict[str, Any] = {
        "name": "POST /v1/catalogo/{slug}/execucoes",
        "context": context,
        "kind": SpanKind.SERVER,
        "attributes": {
            "http.request.method": "POST",
            "http.route": "/v1/catalogo/{slug}/execucoes",
            "http.response.status_code": 500,
            "url.path": f"/v1/execucoes/{new_id()}",
            "url.query": f"token={SECRET_QUERY}",
            "url.full": f"https://rapa.exemplo/v1?token={SECRET_QUERY}",
            "http.target": f"/v1?token={SECRET_QUERY}",
            "http.url": f"https://rapa.exemplo/v1?token={SECRET_QUERY}",
            "http.request.header.authorization": (f"Bearer {SECRET_TOKEN}",),
            "http.request.header.cookie": ("sessao=abc",),
            "client.address": "203.0.113.9",
            "user_agent.original": "navegador",
            "http.request.body": SECRET_INPUT,
            "rapa.execucao.id": "ok",
        },
        "events": [
            Event(
                "exception",
                {
                    "exception.type": "RuntimeError",
                    "exception.message": SECRET_MESSAGE,
                    "exception.stacktrace": f"Traceback ... {SECRET_MESSAGE}",
                },
            ),
            Event("log", {"message": SECRET_MESSAGE}),
        ],
        "status": Status(StatusCode.ERROR, f"RuntimeError: {SECRET_MESSAGE}"),
        "start_time": 1,
        "end_time": 2,
    }
    values.update(overrides)
    return ReadableSpan(**values)


def test_scrubbed_span_keeps_only_the_safe_list() -> None:
    clean = scrub_span(_span())
    assert set(clean.attributes or {}) == {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "rapa.execucao.id",
    }
    assert {
        key for key in (clean.attributes or {}) if not key.startswith("rapa.")
    } <= SAFE_ATTRIBUTES
    assert [(event.name, dict(event.attributes or {})) for event in clean.events] == [
        ("exception", {"exception.type": "RuntimeError"})
    ]
    assert clean.status.status_code is StatusCode.ERROR and clean.status.description is None
    assert clean.name == "POST /v1/catalogo/{slug}/execucoes"
    assert clean.context == _span().context and clean.kind is SpanKind.SERVER
    dumped = clean.to_json()
    for secret in (SECRET_QUERY, SECRET_TOKEN, SECRET_INPUT, SECRET_MESSAGE, "203.0.113.9", "abc"):
        assert secret not in dumped


async def test_requests_and_http_clients_are_traced_without_any_data(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    world = await build_world()
    app = make_app(world)
    tracing.instrument_app(app)
    async with asgi_client(app, TOKEN_A) as client:
        response = await client.post(
            f"/v1/catalogo/emitir_protocolo/execucoes?token={SECRET_QUERY}",
            json={"entradas": {**INPUTS, "nome": SECRET_INPUT}},
        )
        assert response.status_code == 202, response.text
        assert (await client.get("/saude")).status_code == 200

    upstream = FastAPI()

    @upstream.get("/chaves")
    async def keys() -> dict[str, str]:
        return {"chave": SECRET_OUTPUT}

    with serve(upstream) as base:
        async with httpx2.AsyncClient(trust_env=False) as http:
            reply = await http.get(
                f"{base}/chaves?token={SECRET_QUERY}",
                headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
            )
            assert reply.status_code == 200
    tracing.flush()
    spans = exporter.get_finished_spans()
    servers = [span for span in spans if span.kind is SpanKind.SERVER]
    clients = [span for span in spans if span.kind is SpanKind.CLIENT]
    # A saúde fica de fora; o pedido sai com a rota modelo no nome.
    assert [span.name for span in servers] == ["POST /v1/catalogo/{slug}/execucoes"]
    assert (servers[0].attributes or {}).get("http.route") == "/v1/catalogo/{slug}/execucoes"
    assert len(clients) == 1
    # Do cliente, só o método e o código (a URL, com a consulta, fica de fora).
    client_attributes = dict(clients[0].attributes or {})
    assert client_attributes.get("http.method", client_attributes.get("http.request.method")) == (
        "GET"
    )
    assert 200 in client_attributes.values()
    dumped = _dump(spans)
    for secret in (SECRET_QUERY, SECRET_TOKEN, SECRET_INPUT, SECRET_OUTPUT, "Maria da Silva"):
        assert secret not in dumped
    for span in spans:
        for key in span.attributes or {}:
            assert key in SAFE_ATTRIBUTES or key.startswith("rapa."), key


class _OneJob:
    """Fila com uma execução só, que guarda o resultado gravado."""

    def __init__(self, job: RunnerJob) -> None:
        self.jobs = [job]
        self.finished: list[Mapping[str, Any]] = []

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        return 0

    async def reap_expired(self, *, now: Any = None, skip: Any = ()) -> ReapReport:
        return ReapReport()

    async def claim_next(self, *, now: Any = None) -> RunnerJob | None:
        return self.jobs.pop() if self.jobs else None

    async def heartbeat(self, run_id: Any, attempt: int, *, now: Any = None) -> bool:
        return True

    async def finish(self, run_id: Any, attempt: int, result: Any, *, now: Any = None) -> bool:
        self.finished.append(result)
        return True


async def test_each_run_gets_a_span_and_the_logs_carry_its_trace_id(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = traced
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=2,
        manifest=protocolo(),
        inputs={"nome": SECRET_INPUT, "cpf": "529.982.247-25", "uf": "SP"},
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    seen: dict[str, Any] = {}
    formatter = JsonFormatter()

    async def launch(launched: RunnerJob) -> dict[str, Any]:
        record = logging.LogRecord("rapa.control.launcher", 30, __file__, 1, "lançando", (), None)
        seen["trace"] = current_trace_id()
        seen["log"] = json.loads(formatter.format(record))
        return {
            "run_id": str(launched.run_id),
            "attempt": launched.attempt,
            "state": "failed",
            "outputs": {"protocolo": SECRET_OUTPUT},
            "error_code": "elemento_nao_encontrado",
            "error_message": SECRET_MESSAGE,
            "failed_step_id": "entrar",
            "irreversible_started": False,
            "steps_done": 1,
            "artifacts": [],
        }

    queue = _OneJob(job)
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        launch,
        poll_interval=0.01,
        tracer=tracing.run_spans(),
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    for _ in range(500):
        if queue.finished:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 5)
    tracing.flush()
    [span] = [span for span in exporter.get_finished_spans() if span.name == "execucao"]
    assert dict(span.attributes or {}) == {
        "rapa.execucao.id": str(job.run_id),
        "rapa.execucao.tentativa": 2,
        "rapa.execucao.modo": "full",
        "rapa.empresa.id": str(job.company_id),
        "rapa.execucao.situacao": "failed",
        "rapa.execucao.erro": "elemento_nao_encontrado",
    }
    trace = format(span.context.trace_id, "032x")
    # O span da execução é o corrente enquanto o despachante cuida dela.
    assert seen["trace"] == trace
    assert seen["log"]["rastreio"] == trace
    dumped = span.to_json()
    for secret in (SECRET_INPUT, SECRET_OUTPUT, SECRET_MESSAGE, "529.982.247-25"):
        assert secret not in dumped
    # Fora de um span, nenhum id de rastreio.
    assert current_trace_id() is None


class _BrokenTracer:
    def run(self, job: RunnerJob) -> Any:
        raise RuntimeError("rastreio quebrado")


async def test_a_broken_tracer_never_stops_a_run(caplog: pytest.LogCaptureFixture) -> None:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=protocolo(),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def launch(launched: RunnerJob) -> dict[str, Any]:
        return {"run_id": str(launched.run_id), "attempt": 1, "state": "succeeded"}

    queue = _OneJob(job)
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        launch,
        poll_interval=0.01,
        tracer=_BrokenTracer(),
    )
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="rapa.control.dispatcher"):
        loop = asyncio.create_task(dispatcher.run(stop))
        for _ in range(500):
            if queue.finished:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(loop, 5)
    assert [result["state"] for result in queue.finished] == ["succeeded"]
    assert "falha ao abrir o span da execução: RuntimeError" in caplog.text


def test_activation_happens_once_per_process(
    traced: tuple[Tracing, InMemorySpanExporter],
) -> None:
    tracing, _ = traced
    assert activate_tracing(endpoint="http://outro.exemplo:4318") is tracing
    assert tracing_module._process is tracing


class _Collector(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, bytes]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_spans_go_to_the_otlp_traces_path_of_the_endpoint() -> None:
    _Collector.received = []
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/"
    tracing = activate_tracing(endpoint=endpoint, service_name="rapa-despachante")
    try:
        job = RunnerJob(
            run_id=new_id(),
            company_id=new_id(),
            attempt=1,
            manifest=protocolo(),
            inputs={"nome": SECRET_INPUT},
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
        with tracing.run_spans().run(job) as span:
            span.finished({"state": "succeeded", "error_code": None})
        tracing.flush()
    finally:
        tracing.shutdown()
        server.shutdown()
        server.server_close()
    assert [path for path, _ in _Collector.received] == ["/v1/traces"]
    body = _Collector.received[0][1]
    assert b"rapa-despachante" in body and str(job.run_id).encode() in body
    assert SECRET_INPUT.encode() not in body
