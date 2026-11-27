"""GlitchTip: só as exceções não tratadas, e nada do pedido, do usuário ou da mensagem."""

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from rapa.control.observability import glitchtip as glitchtip_module
from rapa.control.observability.glitchtip import (
    OMITTED_MESSAGE,
    configure_glitchtip,
    dsn_problem,
    report_unhandled,
    scrub_event,
)
from rapa.control.services import Dispatcher
from rapa.core.contracts import RunnerJob
from rapa.core.ids import new_id
from rapa.server import DispatcherSupervisor
from tests.control.api.support import TOKEN_A, BrokenCatalog, build_world, make_app
from tests.control.db.support import protocolo

from .support import leaked

DSN = "https://chave-publica@glitchtip.exemplo/1"
#: A chave do projeto nos DSN recusados: nenhum pedaço dela pode aparecer na recusa.
KEY = "Kq7vZx93WmPd"
COOKIE = "sessao-sigilosa"
BODY_SECRET = "corpo-sigiloso"


class _Capture(Transport):
    """Guarda os envelopes em memória, no lugar do envio."""

    def __init__(self) -> None:
        super().__init__({"dsn": DSN})
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def events(self) -> list[dict[str, Any]]:
        out = []
        for envelope in self.envelopes:
            for item in envelope.items:
                if item.type == "event":
                    payload = item.payload.json
                    assert isinstance(payload, dict)
                    out.append(payload)
        return out

    def raw(self) -> str:
        return "\n".join(
            envelope.serialize().decode("utf-8", "replace") for envelope in self.envelopes
        )


@pytest.fixture
def capture() -> Iterator[_Capture]:
    transport = _Capture()
    client = configure_glitchtip(DSN, release="rapa@teste", transport=transport)
    try:
        yield transport
    finally:
        client.close()
    assert not glitchtip_module._enabled


def _event() -> dict[str, Any]:
    return {
        "event_id": "a" * 32,
        "level": "error",
        "platform": "python",
        "release": "rapa@1",
        "server_name": "rapa-web-0",
        "message": "erro com dado",
        "logentry": {"message": "erro com dado"},
        "request": {
            "url": "https://rapa.exemplo/v1/eu",
            "headers": {"Authorization": f"Bearer {TOKEN_A}", "Cookie": f"s={COOKIE}"},
            "cookies": {"s": COOKIE},
            "data": BODY_SECRET,
        },
        "user": {"id": "ana", "email": "ana@exemplo.com.br", "ip_address": "203.0.113.9"},
        "extra": {"entradas": BODY_SECRET},
        "tags": {"empresa": "x"},
        "breadcrumbs": {"values": [{"message": BODY_SECRET}]},
        "modules": {"rapa": "1"},
        "contexts": {
            "runtime": {"name": "CPython", "version": "3.14"},
            "trace": {"trace_id": "b" * 32, "span_id": "c" * 16, "description": BODY_SECRET},
            "response": {"status_code": 500, "body": BODY_SECRET},
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "module": None,
                    "value": f"falhou com {BODY_SECRET}",
                    "mechanism": {"type": "generic", "handled": False, "data": BODY_SECRET},
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "rapa/server.py",
                                "function": "main",
                                "lineno": 10,
                                "vars": {"token": TOKEN_A},
                                "context_line": "    raise RuntimeError(mensagem)",
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_before_send_keeps_only_the_closed_list() -> None:
    clean = scrub_event(_event(), None)
    assert clean is not None
    assert set(clean) == {
        "event_id",
        "level",
        "platform",
        "release",
        "server_name",
        "exception",
        "contexts",
    }
    [exception] = clean["exception"]["values"]
    assert exception == {
        "type": "RuntimeError",
        "value": OMITTED_MESSAGE,
        "mechanism": {"type": "generic", "handled": False},
        "stacktrace": {
            "frames": [
                # Sem o código-fonte em volta e sem as variáveis (como no log).
                {"filename": "rapa/server.py", "function": "main", "lineno": 10}
            ]
        },
    }
    assert clean["contexts"] == {
        "runtime": {"name": "CPython", "version": "3.14"},
        "trace": {"trace_id": "b" * 32, "span_id": "c" * 16},
    }
    dumped = json.dumps(clean)
    for secret in (
        TOKEN_A,
        COOKIE,
        BODY_SECRET,
        "ana@exemplo.com.br",
        "203.0.113.9",
        "erro com dado",
    ):
        assert secret not in dumped


def test_unhandled_api_errors_reach_glitchtip_without_request_or_message(
    capture: _Capture,
) -> None:
    world = asyncio.run(build_world())
    app = make_app(world, catalog=BrokenCatalog())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/v1/catalogo?busca={BODY_SECRET}",
            headers={"Authorization": f"Bearer {TOKEN_A}", "Cookie": f"sessao={COOKIE}"},
        )
        assert response.status_code == 500
    [event] = capture.events()
    [exception] = event["exception"]["values"]
    assert exception["type"] == "RuntimeError" and exception["value"] == OMITTED_MESSAGE
    assert exception["stacktrace"]["frames"]
    for key in ("request", "user", "extra", "breadcrumbs", "tags", "modules", "message"):
        assert key not in event
    raw = capture.raw()
    for secret in (TOKEN_A, COOKIE, BODY_SECRET, BrokenCatalog.SECRET):
        assert secret not in raw
    for frame in exception["stacktrace"]["frames"]:
        assert not {"vars", "context_line", "pre_context", "post_context"} & set(frame)


def test_logs_and_handled_errors_never_become_events(capture: _Capture) -> None:
    logger = logging.getLogger("rapa.control.dispatcher")
    logger.error("falha na etapa de %s: %s", "despacho de execuções", "OperationalError")
    logger.warning("aviso qualquer com %s", BODY_SECRET)
    try:
        raise ValueError(BODY_SECRET)
    except ValueError:
        pass  # tratada: não vai
    assert capture.events() == []
    # A exceção que o último recurso manda vai, sem rastros (breadcrumbs) dos logs de antes.
    report_unhandled(KeyError(BODY_SECRET))
    [event] = capture.events()
    assert "breadcrumbs" not in event
    assert BODY_SECRET not in capture.raw()


def test_without_the_dsn_nothing_is_sent() -> None:
    assert not glitchtip_module._enabled
    # Sem o GlitchTip ligado, mandar é um no-op (nem precisa do sentry_sdk).
    report_unhandled(RuntimeError("nada vai"))


def test_the_sdk_options_send_nothing_personal(capture: _Capture) -> None:
    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["max_breadcrumbs"] == 0
    assert options["traces_sample_rate"] is None
    assert options["default_integrations"] is False
    assert options["auto_enabling_integrations"] is False
    assert options["before_send"] is scrub_event
    collection = options["data_collection"]
    assert collection["user_info"] is False and collection["http_bodies"] == []
    assert collection["cookies"]["mode"] == "off"
    assert collection["http_headers"]["request"]["mode"] == "off"
    assert collection["stack_frame_variables"] is False
    assert collection["frame_context_lines"] == 0
    integrations = {type(i).__name__ for i in options["integrations"]}
    assert integrations == {"ExcepthookIntegration", "DedupeIntegration", "AtexitIntegration"}


class _CrashingDispatcher:
    """Despachante que para com um erro inesperado (com dado na mensagem) e depois roda."""

    def __init__(self) -> None:
        self.runs = 0
        self.healthy = True
        self.stalled = False
        self.failing: dict[str, Any] = {}
        self.in_flight = 0

    async def run(self, stop: Any) -> None:
        self.runs += 1
        if self.runs == 1:
            raise RuntimeError(f"falha inesperada com {BODY_SECRET}")
        await stop.wait()


async def test_dispatcher_crashes_reach_glitchtip_without_the_message(capture: _Capture) -> None:
    fake = _CrashingDispatcher()
    supervisor = DispatcherSupervisor(fake, restart_delay_s=0.01)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = supervisor.start(stop)
    try:
        for _ in range(500):
            if fake.runs >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
    [event] = capture.events()
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert BODY_SECRET not in capture.raw()


async def test_an_unexpected_dispatch_failure_reaches_glitchtip(capture: _Capture) -> None:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=protocolo(),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    jobs = [job]

    class _Queue:
        async def claim_next(self, *, now: Any = None) -> RunnerJob | None:
            return jobs.pop() if jobs else None

    async def launch(launched: RunnerJob) -> dict[str, Any]:  # pragma: no cover - não chega
        raise AssertionError("não lança")

    dispatcher = Dispatcher(_Queue(), launch)  # type: ignore[arg-type]

    async def broken(launched: RunnerJob) -> None:
        raise LookupError(BODY_SECRET)

    dispatcher._dispatch = broken  # type: ignore[method-assign,assignment]
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    [event] = capture.events()
    assert event["exception"]["values"][-1]["type"] == "LookupError"
    assert BODY_SECRET not in capture.raw()


@pytest.mark.parametrize(
    ("dsn", "problem"),
    [
        (f"ftp://{KEY}@glitchtip.exemplo/1", "http:// ou https://"),
        ("https://glitchtip.exemplo/1", "chave do projeto"),
        (f"https://{KEY}@glitchtip.exemplo/", "número do projeto"),
        (f"https://{KEY}@glitchtip.exemplo/abc", "número do projeto"),
        (f"https://{KEY}@/1", "servidor"),
        (f"https://{KEY}@glitchtip.exemplo:99999/1", "não é um endereço válido"),
    ],
)
def test_a_malformed_dsn_is_refused_without_its_key(dsn: str, problem: str) -> None:
    found = dsn_problem(dsn)
    assert found is not None and problem in found
    assert not leaked(KEY, found)
    # Ligar com ele também é recusado antes do sentry_sdk, e nada fica ligado.
    with pytest.raises(RuntimeError, match="RAPA_GLITCHTIP_DSN") as refused:
        configure_glitchtip(dsn)
    assert not leaked(KEY, str(refused.value))
    assert not glitchtip_module._enabled


def test_well_formed_dsns_pass() -> None:
    for dsn in (
        DSN,
        "http://chave@127.0.0.1:8000/12",
        "https://chave:segredo@glitchtip.exemplo/caminho/7",
    ):
        assert dsn_problem(dsn) is None, dsn


def test_a_dsn_the_sdk_refuses_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Se a leitura do sentry_sdk recusar algo que a nossa deixou passar: o mesmo erro
    # limpo (sem a mensagem do sentry_sdk, que cita o caminho do DSN).
    monkeypatch.setattr(glitchtip_module, "dsn_problem", lambda dsn: None)
    with pytest.raises(RuntimeError, match="não é um DSN válido") as refused:
        configure_glitchtip(f"https://{KEY}@glitchtip.exemplo/{KEY}")
    assert not leaked(KEY, str(refused.value))
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert not glitchtip_module._enabled


def test_an_exception_that_kills_the_process_reaches_glitchtip(tmp_path: Path) -> None:
    """O ``sys.excepthook``: a exceção que derruba o processo vai, limpa."""
    out = tmp_path / "eventos.jsonl"
    program = f"""
import json, sys
from sentry_sdk.transport import Transport
from rapa.control.observability.glitchtip import configure_glitchtip

class FileTransport(Transport):
    def capture_envelope(self, envelope):
        with open(sys.argv[1], "a", encoding="utf-8") as handle:
            for item in envelope.items:
                if item.type == "event":
                    handle.write(json.dumps(item.payload.json) + "\\n")

configure_glitchtip({DSN!r}, release="rapa@teste", transport=FileTransport({{"dsn": {DSN!r}}}))
segredo = {BODY_SECRET!r}
raise LookupError("falha fatal com " + segredo)
"""
    done = subprocess.run(  # noqa: S603 - comando fixo, sem shell
        [sys.executable, "-c", program, str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # O processo morre como sempre (o gancho de antes ainda escreve a pilha no stderr).
    assert done.returncode == 1 and "LookupError" in done.stderr
    raw = out.read_text(encoding="utf-8")
    [event] = [json.loads(line) for line in raw.splitlines()]
    [exception] = event["exception"]["values"]
    assert exception["type"] == "LookupError" and exception["value"] == OMITTED_MESSAGE
    assert exception["mechanism"] == {"type": "excepthook", "handled": False}
    assert event["release"] == "rapa@teste"
    assert BODY_SECRET not in raw
