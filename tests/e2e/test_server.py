"""Configuração do serviço e o processo ``python -m rapa.server`` de verdade."""

import asyncio
import gzip
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sqlalchemy import text

from rapa.control.auth import TokenVerifier
from rapa.control.db import Database
from rapa.control.mcp import McpGateway
from rapa.control.observability import glitchtip as glitchtip_module
from rapa.control.observability import tracing as tracing_module
from rapa.control.observability.glitchtip import (
    OMITTED_MESSAGE,
    configure_glitchtip,
    report_unhandled,
)
from rapa.control.observability.metrics import MeteredWebhooks, serve_metrics
from rapa.control.observability.tracing import RunSpans, Tracing, activate_tracing
from rapa.control.services import Dispatcher, PgPlatform, ReapReport
from rapa.core.contracts import Forbidden, Role, RunnerJob, VersionStatus
from rapa.core.ids import new_id
from rapa.fixtures.robots import baixar_notas
from rapa.server import (
    DispatcherSupervisor,
    ServerSettings,
    _DispatcherHealth,
    _stop_dispatcher,
    create_server_app,
    dispatcher_pool_size,
    load_dev_tokens,
)
from rapa.ui.auth import UiAuth
from rapa.ui.backend import NotAuthenticated
from rapa.ui.context import UiContext, UiSettings, Viewer
from tests.control.api.support import asgi_client, running
from tests.control.db.support import PgDatabase, make_tenant, protocolo
from tests.control.observability.support import leaked

DB = "postgresql://rapa_app:senha@banco:5432/rapa"


def _tokens(tmp_path: Path, tokens: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps(tokens or {"t1": {"sujeito": "ana", "empresa": str(new_id())}}))
    return path


def test_minimal_dev_configuration(tmp_path: Path) -> None:
    settings = ServerSettings.from_env(
        {
            "RAPA_DATABASE_URL": DB,
            "RAPA_DEV": "1",
            "RAPA_TOKENS_DEV": str(_tokens(tmp_path)),
            "RAPA_ARGS_NAVEGADOR": "--host-resolver-rules='MAP *.rapa.test 127.0.0.1' --x",
            "RAPA_LIMITE_POR_SEGUNDO": "0",
            "RAPA_MCP_ORIGENS": "https://a.exemplo, https://b.exemplo",
            "RAPA_CONCORRENCIA": "8",
        }
    )
    assert settings.http and settings.ui and settings.dev
    assert settings.dispatcher_database_url is None
    assert settings.browser_args == ("--host-resolver-rules=MAP *.rapa.test 127.0.0.1", "--x")
    assert settings.rate_limit_per_s is None
    assert settings.mcp_allowed_origins == {"https://a.exemplo", "https://b.exemplo"}
    assert settings.concurrency == 8
    # A URL do banco (com senha) não aparece na representação.
    assert "senha" not in repr(settings)


def test_recording_sessions_and_llm_configuration(tmp_path: Path) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}
    defaults = ServerSettings.from_env(base)
    assert defaults.sessions and defaults.sessions_per_company == 2
    assert defaults.sessions_max is None
    assert (defaults.session_idle_s, defaults.session_max_s) == (600.0, 3600.0)
    assert defaults.llm_base_url is None
    matrix = tmp_path / "matriz.json"
    settings = ServerSettings.from_env(
        {
            **base,
            "RAPA_SESSOES_POR_EMPRESA": "3",
            "RAPA_SESSOES_MAX": "5",
            "RAPA_SESSAO_OCIOSA_S": "300",
            "RAPA_SESSAO_MAX_S": "1800",
            "RAPA_PASTA_SESSOES": str(tmp_path / "sessoes"),
            "RAPA_LLM_BASE_URL": "https://hub.exemplo/v1",
            "RAPA_LLM_MODEL": "qwen-latest",
            "RAPA_LLM_API_KEY": "sk-chave-que-nao-aparece",
            "RAPA_LLM_CAPACIDADES": str(matrix),
        }
    )
    assert (settings.sessions_per_company, settings.sessions_max) == (3, 5)
    assert (settings.session_idle_s, settings.session_max_s) == (300.0, 1800.0)
    assert settings.sessions_dir == tmp_path / "sessoes"
    assert settings.llm_capabilities_file == matrix
    assert "sk-chave-que-nao-aparece" not in repr(settings)
    assert not ServerSettings.from_env({**base, "RAPA_SESSOES": "0"}).sessions
    for env, message in (
        ({"RAPA_LLM_BASE_URL": "https://hub.exemplo/v1"}, "endereço e o modelo"),
        ({"RAPA_LLM_BASE_URL": "https://hub", "RAPA_LLM_MODEL": "q"}, "RAPA_LLM_CAPACIDADES"),
        ({"RAPA_SESSAO_OCIOSA_S": "7200"}, "prazo máximo"),
        ({"RAPA_SESSOES_POR_EMPRESA": "0"}, "sessions_per_company"),
    ):
        with pytest.raises(ValueError, match=message):
            ServerSettings.from_env({**base, **env})


def test_publishing_configuration(tmp_path: Path) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}
    defaults = ServerSettings.from_env(base)
    assert defaults.webhook_key is None
    assert (defaults.schedule_catch_up_s, defaults.schedule_interval_s) == (600.0, 5.0)
    assert (defaults.batch_max_rows, defaults.batch_max_bytes) == (1000, 5 * 1024 * 1024)
    assert (defaults.schedules_per_company, defaults.webhook_lookups_per_s) == (200, 200.0)
    key = "chave-dos-webhooks-que-nunca-aparece-em-lugar-nenhum"
    settings = ServerSettings.from_env(
        {
            **base,
            "RAPA_CHAVE_WEBHOOKS": key,
            "RAPA_AGENDA_TOLERANCIA_S": "120",
            "RAPA_AGENDA_INTERVALO_S": "1",
            "RAPA_LOTE_MAX_LINHAS": "50",
            "RAPA_LOTE_MAX_BYTES": "65536",
            "RAPA_AGENDAS_POR_EMPRESA": "30",
            "RAPA_WEBHOOKS_POR_SEGUNDO": "50",
        }
    )
    assert settings.webhook_key == key and key not in repr(settings)
    assert (settings.schedule_catch_up_s, settings.schedule_interval_s) == (120.0, 1.0)
    assert (settings.batch_max_rows, settings.batch_max_bytes) == (50, 65536)
    assert (settings.schedules_per_company, settings.webhook_lookups_per_s) == (30, 50.0)
    for env, field in (
        ({"RAPA_CHAVE_WEBHOOKS": "curta"}, "webhook_key"),
        ({"RAPA_LOTE_MAX_LINHAS": "0"}, "batch_max_rows"),
        ({"RAPA_AGENDA_TOLERANCIA_S": "-1"}, "schedule_catch_up_s"),
        ({"RAPA_AGENDAS_POR_EMPRESA": "0"}, "schedules_per_company"),
        ({"RAPA_WEBHOOKS_POR_SEGUNDO": "0"}, "webhook_lookups_per_s"),
    ):
        with pytest.raises(ValueError, match=field):
            ServerSettings.from_env({**base, **env})

    # Sem a chave, o serviço sobe sem webhooks; com ela, os três serviços existem.
    app = create_server_app(ServerSettings.from_env({**base, "RAPA_UI": "0"}))
    assert app.state.publishing.webhooks is None and app.state.publishing.schedules is not None
    app = create_server_app(
        ServerSettings.from_env({**base, "RAPA_UI": "0", "RAPA_CHAVE_WEBHOOKS": key})
    )
    publishing = app.state.publishing
    # A entrega passa pelas métricas (contada pelo resultado) e chega ao mesmo serviço.
    assert isinstance(publishing.receiver, MeteredWebhooks)
    assert publishing.webhooks is publishing.receiver.inner is not None
    assert publishing.batches is not None and publishing.batch_max_bytes == 5 * 1024 * 1024
    # Os limites novos chegam aos serviços.
    app = create_server_app(
        ServerSettings.from_env(
            {
                **base,
                "RAPA_UI": "0",
                "RAPA_CHAVE_WEBHOOKS": key,
                "RAPA_AGENDAS_POR_EMPRESA": "30",
                "RAPA_WEBHOOKS_POR_SEGUNDO": "50",
            }
        )
    )
    publishing = app.state.publishing
    assert publishing.schedules.max_enabled_per_company == 30
    assert publishing.webhooks.gate.lookups_per_s == 50.0


def test_repair_configuration(tmp_path: Path) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}
    assert ServerSettings.from_env(base).repair_candidates == 8
    settings = ServerSettings.from_env({**base, "RAPA_CONSERTO_CANDIDATOS": "3"})
    assert settings.repair_candidates == 3
    for value in ("0", "21"):
        with pytest.raises(ValueError, match="repair_candidates"):
            ServerSettings.from_env({**base, "RAPA_CONSERTO_CANDIDATOS": value})
    app = create_server_app(
        ServerSettings.from_env({**base, "RAPA_UI": "0", "RAPA_CONSERTO_CANDIDATOS": "3"})
    )
    assert app.state.repairs.limit == 3 and app.state.repairs.assistant is None


def test_a1_configuration(tmp_path: Path) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}
    assert ServerSettings.from_env(base).a1_warn_days == 30
    for value in ("0", "3651"):
        with pytest.raises(ValueError, match="a1_warn_days"):
            ServerSettings.from_env({**base, "RAPA_A1_AVISO_DIAS": value})
    app = create_server_app(
        ServerSettings.from_env({**base, "RAPA_UI": "0", "RAPA_A1_AVISO_DIAS": "45"})
    )
    assert app.state.credentials.warn_days == 45
    # O despachante ganha a etapa dos avisos de vencimento, com o mesmo prazo.
    dispatcher_app = create_server_app(
        ServerSettings.from_env(
            {
                "RAPA_DATABASE_URL": DB,
                "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:x@banco:5432/rapa",
                "RAPA_HTTP": "0",
                "RAPA_A1_AVISO_DIAS": "15",
            }
        )
    )
    alerts = dispatcher_app.state.dispatcher.alerts
    assert alerts is not None and alerts.warn_days == 15
    assert dispatcher_app.state.dispatcher.alerts_interval == 6 * 3600


def test_dispatcher_gets_the_scheduler(tmp_path: Path) -> None:
    base = {
        "RAPA_DATABASE_URL": DB,
        "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:x@banco:5432/rapa",
        "RAPA_HTTP": "0",
        "RAPA_AGENDA_INTERVALO_S": "2",
        "RAPA_AGENDA_TOLERANCIA_S": "90",
    }
    app = create_server_app(ServerSettings.from_env(base))
    dispatcher = app.state.dispatcher
    assert isinstance(dispatcher, Dispatcher)
    assert dispatcher.scheduler is not None and dispatcher.schedule_interval == 2.0
    assert dispatcher.scheduler.catch_up == timedelta(seconds=90)  # type: ignore[attr-defined]


def test_keycloak_configuration() -> None:
    settings = ServerSettings.from_env(
        {
            "RAPA_DATABASE_URL": DB,
            "RAPA_JWT_ISSUER": "https://sso.exemplo/realms/rapa",
            "RAPA_JWT_AUDIENCE": "https://rapa.exemplo",
            "RAPA_JWKS_URL": "https://sso.exemplo/realms/rapa/protocol/openid-connect/certs",
            "RAPA_ORGANIZATION_ALIASES": json.dumps({"fiscal": str(new_id())}),
            "RAPA_OIDC_CLIENT_ID": "rapa-ui",
            "RAPA_OIDC_REDIRECT_URI": "https://rapa.exemplo/auth/oidc/retorno",
        }
    )
    assert settings.oidc_client_id == "rapa-ui" and not settings.dev
    assert isinstance(next(iter(settings.organization_aliases.values())), UUID)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "database_url"),
        ({"RAPA_DATABASE_URL": DB}, "Keycloak"),
        ({"RAPA_DATABASE_URL": DB, "RAPA_JWT_ISSUER": "https://sso"}, "emissor, público"),
        ({"RAPA_DATABASE_URL": DB, "RAPA_HTTP": "0"}, "não teria o que fazer"),
        ({"RAPA_DATABASE_URL": DB, "RAPA_DEV": "talvez"}, "1 ou 0"),
    ],
)
def test_invalid_configuration_is_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ServerSettings.from_env(env)


def test_dev_files_need_dev_mode(tmp_path: Path) -> None:
    env = {
        "RAPA_DATABASE_URL": DB,
        "RAPA_JWT_ISSUER": "https://sso.exemplo/realms/rapa",
        "RAPA_JWT_AUDIENCE": "https://rapa.exemplo",
        "RAPA_JWKS_URL": "https://sso.exemplo/certs",
        "RAPA_SEGREDOS_DEV": str(tmp_path / "segredos.json"),
    }
    with pytest.raises(ValueError, match="RAPA_DEV=1"):
        ServerSettings.from_env(env)


def test_dev_tokens_file_format(tmp_path: Path) -> None:
    company = new_id()
    path = _tokens(tmp_path, {"abc": {"sujeito": "ana", "empresa": str(company)}})
    assert load_dev_tokens(path) == {"abc": ("ana", company)}
    for bad in ({}, {"abc": {"sujeito": "ana"}}, {"abc": "ana"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_dev_tokens(path)


DISPATCHER_DB = "postgresql://rapa_dispatcher:senha@banco:5432/rapa"


@pytest.mark.parametrize("concurrency", [1, 4, 40])
def test_dispatcher_pool_is_sized_from_concurrency(concurrency: int) -> None:
    # Cada execução usa até duas conexões juntas (concessão e eventos ou resultado) e
    # o laço principal mais duas; o padrão do SQLAlchemy (5) não dava para 4 execuções.
    settings = ServerSettings(
        database_url=DB,
        dispatcher_database_url=DISPATCHER_DB,
        http=False,
        concurrency=concurrency,
    )
    app = create_server_app(settings)
    assert dispatcher_pool_size(concurrency) == 2 * concurrency + 2
    assert app.state.dispatcher.queue.db.engine.pool.size() == 2 * concurrency + 2


class FakeDispatcher:
    """Despachante que para com erro nas primeiras ``crashes`` voltas."""

    def __init__(self, crashes: int) -> None:
        self.crashes = crashes
        self.runs = 0
        self.running = asyncio.Event()
        # O que o supervisor lê do despachante de verdade para a saúde e o encerramento.
        self.healthy = True
        self.stalled = False
        self.failing: dict[str, datetime] = {}
        self.in_flight = 0

    async def drain(self) -> None:  # pragma: no cover - nada em andamento
        raise AssertionError("nada em andamento para esperar")

    async def run(self, stop: asyncio.Event) -> None:
        self.runs += 1
        if self.runs <= self.crashes:
            raise RuntimeError("falha inesperada com dados sigilosos")
        self.running.set()
        await stop.wait()


async def _until(condition: Callable[[], bool]) -> None:
    """Espera ``condition`` ficar verdadeira (até 5 s)."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a condição não ficou verdadeira em 5 s")


async def test_dispatcher_supervisor_restarts_a_dead_dispatcher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeDispatcher(crashes=2)
    supervisor = DispatcherSupervisor(
        fake,  # type: ignore[arg-type]
        restart_delay_s=0.2,
        max_restart_delay_s=1.0,
    )
    stop = asyncio.Event()
    task = supervisor.start(stop)
    await _until(lambda: not supervisor.healthy)
    async with asyncio.timeout(5):
        await fake.running.wait()
    assert supervisor.healthy and supervisor.restarts == 2 and fake.runs == 3
    stopped = [r.getMessage() for r in caplog.records if "despachante parou" in r.getMessage()]
    assert stopped == [
        "o despachante parou (RuntimeError); reiniciando em 0.2 s",
        "o despachante parou (RuntimeError); reiniciando em 0.4 s",
    ]
    # Só o tipo do erro vai para o registro, nunca a mensagem.
    assert "sigilosos" not in caplog.text
    stop.set()
    await task
    assert not supervisor.healthy and supervisor.restarts == 2


async def test_dispatcher_supervisor_waits_longer_after_each_crash() -> None:
    fake = FakeDispatcher(crashes=1000)
    supervisor = DispatcherSupervisor(
        fake,  # type: ignore[arg-type]
        restart_delay_s=0.01,
        max_restart_delay_s=0.04,
    )
    stop = asyncio.Event()
    task = supervisor.start(stop)
    await _until(lambda: supervisor.restarts >= 6)
    # A espera dobra a cada parada e para no máximo: nada de laço apertado.
    assert fake.runs <= 7
    stop.set()
    async with asyncio.timeout(1):
        await task
    assert not supervisor.healthy


async def test_health_reports_a_stopped_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    fakes: list[FakeDispatcher] = []

    def build(settings: ServerSettings, db: Any, *_: Any) -> FakeDispatcher:
        fakes.append(FakeDispatcher(crashes=1000 if len(fakes) == 0 else 0))
        return fakes[-1]

    monkeypatch.setattr("rapa.server.build_dispatcher", build)
    settings = ServerSettings(database_url=DB, dispatcher_database_url=DISPATCHER_DB, http=False)
    for crashing in (True, False):
        app = create_server_app(settings)
        supervisor: DispatcherSupervisor = app.state.dispatcher_supervisor
        async with running(app), asgi_client(app) as client:
            if crashing:
                # O despachante morre logo e fica esperando para reiniciar: a saúde acusa.
                await _until(lambda s=supervisor: not s.healthy)
                response = await client.get("/saude")
                assert response.status_code == 503
                assert response.json() == {"estado": "despachante_parado"}
            else:
                await fakes[-1].running.wait()
                response = await client.get("/saude")
                assert response.status_code == 200
                assert response.json() == {"estado": "ok"}


class _HangingQueue:
    """Fila em que a criação de partições espera para sempre (trava no banco) ou falha."""

    def __init__(self, *, hang: bool) -> None:
        self.hang = hang
        self.claims = 0

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        if self.hang:
            await asyncio.Event().wait()
        raise PermissionError("sem permissão para criar partição: detalhe interno")

    async def reap_expired(self, *, now: Any = None, skip: Any = ()) -> ReapReport:
        return ReapReport(requeued=[], indeterminate=[], failed=[])

    async def claim_next(self, *, now: Any = None) -> None:
        self.claims += 1
        return None


async def _never_launch(job: Any) -> dict[str, Any]:  # pragma: no cover - a fila está vazia
    raise AssertionError("nada para lançar")


@pytest.mark.parametrize("hang", [False, True], ids=["falha_sem_parar", "volta_presa"])
async def test_health_reports_a_failing_or_stuck_dispatcher(hang: bool) -> None:
    queue = _HangingQueue(hang=hang)
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        _never_launch,
        poll_interval=0.01,
        unhealthy_after_s=0.1,
    )
    supervisor = DispatcherSupervisor(dispatcher)
    inner = FastAPI()

    @inner.get("/saude")
    async def health() -> dict[str, str]:
        return {"estado": "ok"}

    app = _DispatcherHealth(inner, supervisor)
    stop = asyncio.Event()
    task = supervisor.start(stop)
    try:
        await _until(lambda: not supervisor.healthy)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get("/saude")
        assert response.status_code == 503
        body = response.json()
        assert body["estado"] == "despachante_com_falha"
        if hang:
            # A manutenção não falhou, só não terminou: a volta presa é que acusa.
            assert body == {"estado": "despachante_com_falha", "volta_presa": True}
        else:
            # Uma falha de manutenção não impede de pegar execuções da fila.
            assert body == {"estado": "despachante_com_falha", "etapas": ["partitions"]}
            assert queue.claims > 1
        # Só nomes de etapas, nunca o texto do erro.
        assert "detalhe interno" not in response.text
    finally:
        stop.set()
        if hang:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


JWT = {
    "jwt_issuer": "https://sso.exemplo/realms/rapa",
    "jwt_audience": "https://rapa.exemplo",
    "jwks_url": "https://sso.exemplo/realms/rapa/protocol/openid-connect/certs",
}


def _full_service() -> FastAPI:
    """REST, MCP e despachante juntos (sem a interface, que é global ao processo)."""
    return create_server_app(
        ServerSettings(database_url=DB, dispatcher_database_url=DISPATCHER_DB, ui=False, **JWT)
    )


def _record_closing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Anota cada banco e cada verificador de tokens que a aplicação fecha."""
    closed: list[str] = []
    dispose, aclose = Database.dispose, TokenVerifier.aclose

    async def record_dispose(self: Database) -> None:
        closed.append("banco")
        await dispose(self)

    async def record_aclose(self: TokenVerifier) -> None:
        closed.append("verificador")
        await aclose(self)

    monkeypatch.setattr(Database, "dispose", record_dispose)
    monkeypatch.setattr(TokenVerifier, "aclose", record_aclose)
    return closed


async def test_service_starts_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDispatcher(crashes=0)
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db, *_: fake)
    closed = _record_closing(monkeypatch)
    app = _full_service()
    async with running(app), asgi_client(app) as client:
        await fake.running.wait()
        # Entrar de novo com o serviço de pé: recusado, e o que está rodando segue.
        with pytest.raises(RuntimeError, match="já foi iniciado uma vez"):
            async with running(app):
                pass
        assert (await client.get("/saude")).status_code == 200
        assert closed == [] and fake.runs == 1
    assert sorted(closed) == ["banco", "banco", "verificador"]
    # Depois de encerrado (outro TestClient na mesma aplicação, por exemplo): recusado
    # antes de abrir qualquer coisa, sem despachante novo nem nada para fechar.
    with pytest.raises(RuntimeError, match="já foi iniciado uma vez"):
        async with running(app):
            pass
    assert sorted(closed) == ["banco", "banco", "verificador"] and fake.runs == 1
    assert not app.state.dispatcher_supervisor.healthy


@asynccontextmanager
async def _failing_mcp(self: McpGateway) -> AsyncIterator[None]:
    raise RuntimeError("falha ao subir")
    yield  # pragma: no cover


def _failing_start(self: DispatcherSupervisor, stop: asyncio.Event) -> asyncio.Task[None]:
    raise RuntimeError("falha ao subir")


@pytest.mark.parametrize("where", ["mcp", "despachante"])
async def test_failed_startup_still_closes_databases_and_verifier(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    closed = _record_closing(monkeypatch)
    if where == "mcp":
        monkeypatch.setattr(McpGateway, "run", _failing_mcp)
    else:
        monkeypatch.setattr(DispatcherSupervisor, "start", _failing_start)
    app = _full_service()
    # Dentro do grupo de tarefas do SDK do MCP, o erro chega dentro de um grupo de exceções.
    failure = pytest.RaisesExc(RuntimeError, match="falha ao subir")
    with pytest.RaisesGroup(failure, allow_unwrapped=True):
        async with running(app):
            pass  # pragma: no cover
    assert sorted(closed) == ["banco", "banco", "verificador"]


class Fatal(BaseException):
    """Erro que nem a supervisão pega: a própria supervisão para."""


class ScriptedDispatcher:
    """Para com erro na primeira volta, roda na segunda e morre de vez com ``die``."""

    def __init__(self) -> None:
        self.runs = 0
        self.running = asyncio.Event()
        self.die = asyncio.Event()
        # O que o supervisor lê do despachante de verdade para a saúde e o encerramento.
        self.healthy = True
        self.stalled = False
        self.failing: dict[str, datetime] = {}
        self.in_flight = 0

    async def drain(self) -> None:  # pragma: no cover - nada em andamento
        raise AssertionError("nada em andamento para esperar")

    async def run(self, stop: asyncio.Event) -> None:
        self.runs += 1
        if self.runs == 1:
            raise RuntimeError("falha inesperada")
        self.running.set()
        await self.die.wait()
        raise Fatal


async def test_full_service_health_follows_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ScriptedDispatcher()
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db, *_: fake)
    closed = _record_closing(monkeypatch)
    app = _full_service()
    supervisor: DispatcherSupervisor = app.state.dispatcher_supervisor
    async with running(app), asgi_client(app) as client:
        # Parou com erro: enquanto espera para reiniciar, a saúde acusa.
        await _until(lambda: fake.runs == 1 and not supervisor.healthy)
        response = await client.get("/saude")
        assert response.status_code == 503
        assert response.json() == {"estado": "despachante_parado"}
        assert response.headers["cache-control"] == "no-store"
        async with asyncio.timeout(5):
            await fake.running.wait()
        assert (await client.get("/saude")).status_code == 200
        # A supervisão em si morreu: a saúde acusa e continua acusando.
        fake.die.set()
        await _until(lambda: not supervisor.healthy)
        await asyncio.sleep(0.05)
        assert (await client.get("/saude")).status_code == 503
        assert fake.runs == 2
    # O encerramento segue até o fim mesmo assim.
    assert sorted(closed) == ["banco", "banco", "verificador"]


UI = {"oidc_client_id": "rapa-ui", "oidc_redirect_uri": "https://rapa.exemplo/auth/oidc/retorno"}


def _service_with_ui(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> FastAPI:
    """O serviço com a interface ligada (a configuração de produção, ``RAPA_UI=1``).

    No lugar do NiceGUI, que é global ao processo, uma montagem que faz com o ciclo
    de vida da aplicação o mesmo que ``ui.run_with``: envolve o que encontrar, com a
    subida da interface antes e o encerramento dela depois.
    """

    def mount(app: FastAPI, **kwargs: Any) -> None:
        wrapped = app.router.lifespan_context

        @asynccontextmanager
        async def ui_lifespan(application: FastAPI) -> AsyncIterator[Any]:
            events.append("interface: subida")
            async with wrapped(application) as state:
                yield state
            events.append("interface: encerramento")

        app.router.lifespan_context = ui_lifespan

    monkeypatch.setattr("rapa.ui.app.create_ui", mount)
    return create_server_app(
        ServerSettings(database_url=DB, dispatcher_database_url=DISPATCHER_DB, **JWT, **UI)
    )


class _RecordingDispatcher(FakeDispatcher):
    """Anota quando o laço termina de parar."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(crashes=0)
        self.events = events

    async def run(self, stop: asyncio.Event) -> None:
        await super().run(stop)
        self.events.append("despachante: parou")


async def test_service_with_ui_starts_only_once_and_closes_databases_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _record_closing(monkeypatch)
    fake = _RecordingDispatcher(events)
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db, *_: fake)
    app = _service_with_ui(monkeypatch, events)
    async with running(app), asgi_client(app) as client:
        await fake.running.wait()
        assert events == ["interface: subida"]
        # A recusa vem antes de tudo, inclusive da subida da interface.
        with pytest.raises(RuntimeError, match="já foi iniciado uma vez"):
            async with running(app):
                pass  # pragma: no cover
        assert events == ["interface: subida"] and fake.runs == 1
        assert (await client.get("/saude")).status_code == 200
    # O despachante para primeiro; a interface encerra depois dele e antes de o
    # verificador e os bancos serem fechados.
    closing = [
        "interface: subida",
        "despachante: parou",
        "interface: encerramento",
        "verificador",
        "banco",
        "banco",
    ]
    assert events == closing
    with pytest.raises(RuntimeError, match="já foi iniciado uma vez"):
        async with running(app):
            pass  # pragma: no cover
    assert events == closing and fake.runs == 1


class _OneRunQueue:
    """Fila com uma execução só, que grava o resultado em memória."""

    def __init__(self, job: RunnerJob) -> None:
        self.job: RunnerJob | None = job
        self.finished: list[Mapping[str, Any]] = []

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        return 0

    async def reap_expired(self, *, now: Any = None, skip: Any = ()) -> ReapReport:
        return ReapReport()

    async def claim_next(self, *, now: Any = None) -> RunnerJob | None:
        job, self.job = self.job, None
        return job

    async def heartbeat(self, run_id: UUID, attempt: int, *, now: Any = None) -> bool:
        return True

    async def finish(
        self, run_id: UUID, attempt: int, result: Mapping[str, Any], *, now: Any = None
    ) -> bool:
        self.finished.append(result)
        return True


class _SlowRun:
    """Lançador cuja execução só termina com ``release``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancelled = False
        self.job = RunnerJob(
            run_id=new_id(),
            company_id=new_id(),
            attempt=1,
            manifest=baixar_notas("https://portal.test", "portal.test"),
            inputs={},
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def __call__(self, job: RunnerJob) -> dict[str, Any]:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"run_id": str(job.run_id), "attempt": job.attempt, "state": "succeeded"}


class _CrashingDispatcher(Dispatcher):
    """Despachante de verdade que, na primeira volta, pega uma execução e para com
    ``error`` (fora das proteções do laço): a execução segue em andamento nele."""

    def __init__(self, error: BaseException) -> None:
        self.launch_stub = _SlowRun()
        self.queue_stub = _OneRunQueue(self.launch_stub.job)
        super().__init__(self.queue_stub, self.launch_stub, poll_interval=0.01)  # type: ignore[arg-type]
        self.error = error
        self.crashed = False

    async def run(self, stop: asyncio.Event) -> None:
        if not self.crashed:
            self.crashed = True
            await self.run_once()
            raise self.error
        await super().run(stop)  # pragma: no cover - o teste encerra antes de reiniciar


def test_drain_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="encerramento"):
        DispatcherSupervisor(FakeDispatcher(crashes=0), drain_timeout_s=0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("falha inesperada"), Fatal()],
    ids=["esperando_para_reiniciar", "supervisao_morta"],
)
async def test_shutdown_waits_for_runs_left_by_a_stopped_dispatcher_loop(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    dispatcher = _CrashingDispatcher(error)
    supervisor = DispatcherSupervisor(dispatcher, restart_delay_s=30.0, max_restart_delay_s=30.0)
    stop = asyncio.Event()
    task = supervisor.start(stop)
    # O laço parou com a execução em andamento: ou espera para reiniciar, ou a
    # própria supervisão morreu.
    await _until(lambda: dispatcher.in_flight == 1 and not supervisor.healthy)
    if isinstance(error, Fatal):
        await _until(task.done)
    stopping = asyncio.create_task(_stop_dispatcher(supervisor, stop, task))
    await asyncio.sleep(0.1)
    assert not stopping.done() and dispatcher.in_flight == 1
    dispatcher.launch_stub.release.set()
    async with asyncio.timeout(5):
        await stopping
    assert dispatcher.in_flight == 0 and not dispatcher.launch_stub.cancelled
    assert [r["state"] for r in dispatcher.queue_stub.finished] == ["succeeded"]
    assert "esperando 1 execução(ões) em andamento" in caplog.text


async def test_shutdown_interrupts_runs_that_outlast_the_drain_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = _CrashingDispatcher(RuntimeError("falha inesperada"))
    supervisor = DispatcherSupervisor(
        dispatcher, restart_delay_s=30.0, max_restart_delay_s=30.0, drain_timeout_s=0.2
    )
    stop = asyncio.Event()
    task = supervisor.start(stop)
    await _until(lambda: dispatcher.in_flight == 1 and not supervisor.healthy)
    async with asyncio.timeout(5):
        await _stop_dispatcher(supervisor, stop, task)
    # Interrompida (o lançador para o executor) antes de os bancos serem fechados, e
    # nada é gravado: a concessão vence e o recolhimento decide (devolve à fila ou,
    # com um passo irreversível começado, marca como indeterminada).
    assert dispatcher.in_flight == 0 and dispatcher.launch_stub.cancelled
    assert dispatcher.queue_stub.finished == []
    assert "1 execução(ões) ainda em andamento depois de 0.2 s" in caplog.text


async def test_full_service_shutdown_drains_runs_before_closing_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = _CrashingDispatcher(Fatal())
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db, *_: dispatcher)
    closed = _record_closing(monkeypatch)
    app = _full_service()
    supervisor: DispatcherSupervisor = app.state.dispatcher_supervisor
    leave = asyncio.Event()

    async def serve() -> None:
        # O SDK do MCP exige abrir e fechar o grupo de tarefas na mesma tarefa.
        async with running(app):
            await leave.wait()

    serving = asyncio.create_task(serve())
    await _until(lambda: dispatcher.in_flight == 1 and not supervisor.healthy)
    leave.set()
    await asyncio.sleep(0.1)
    # A supervisão morreu, mas o encerramento espera a execução antes de fechar os bancos.
    assert not serving.done() and closed == []
    dispatcher.launch_stub.release.set()
    async with asyncio.timeout(5):
        await serving
    assert [r["state"] for r in dispatcher.queue_stub.finished] == ["succeeded"]
    assert sorted(closed) == ["banco", "banco", "verificador"]


async def _a_minute_later(auth: UiAuth, key: str) -> None:
    """Simula o prazo da última conferência do principal ter passado."""
    session = await auth.store.load(key)
    assert session is not None and session.principal_checked_at is not None
    session.principal_checked_at -= timedelta(minutes=1)
    await auth.store.save(session)


@pytest.mark.database
async def test_ui_session_follows_the_database_like_the_api(
    app_db: Any,
    pg_database: PgDatabase,
    superuser_engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(app_db, "Revoga")
    platform = PgPlatform(app_db)
    version = await platform.create_version(tenant.creator, tenant.area_id, protocolo())
    await platform.mark_validated(tenant.creator, version.robot_id, version.version_no)
    tokens = {
        f"token-{label}": {"sujeito": p.subject, "empresa": str(tenant.company_id)}
        for label, p in (("aprovador", tenant.approver), ("operador", tenant.operator))
    }
    mounted: dict[str, Any] = {}

    def capture_ui(app: Any, **kwargs: Any) -> None:
        mounted.update(kwargs)

    # A interface de verdade (NiceGUI) é global ao processo; aqui basta a montagem.
    monkeypatch.setattr("rapa.ui.app.create_ui", capture_ui)
    settings = ServerSettings(
        database_url=pg_database.app_url,
        dev=True,
        dev_tokens_file=_tokens(tmp_path, tokens),
        rate_limit_per_s=None,
    )
    app = create_server_app(settings)
    auth: UiAuth = mounted["auth"]
    assert auth.principals is not None and auth.dev is not None
    ctx = UiContext(mounted["backend_factory"], auth, UiSettings())
    profiles = {p.principal.subject: p for p in auth.dev.profiles.values()}

    async def login(subject: str) -> tuple[str, Viewer]:
        profile = profiles[subject]
        _, session = await auth.start_session(profile.principal, profile.label, "dev")
        return session.key, Viewer(ctx, session)

    async with running(app):
        approver_key, approver = await login(tenant.approver.subject)
        operator_key, operator = await login(tenant.operator.subject)
        await approver.backend()
        await (await operator.backend()).list_runs()

        async with superuser_engine.begin() as conn:
            # O aprovador continua ativo, mas perde o papel de aprovador.
            await conn.execute(
                text(
                    "DELETE FROM memberships WHERE role = 'approver' AND user_id = "
                    "(SELECT id FROM users WHERE subject = :s)"
                ),
                {"s": tenant.approver.subject},
            )
            # O operador é desativado e perde todos os vínculos.
            await conn.execute(
                text("UPDATE users SET active = false WHERE subject = :s"),
                {"s": tenant.operator.subject},
            )
            await conn.execute(
                text(
                    "DELETE FROM memberships WHERE user_id = "
                    "(SELECT id FROM users WHERE subject = :s)"
                ),
                {"s": tenant.operator.subject},
            )
        await _a_minute_later(auth, approver_key)
        await _a_minute_later(auth, operator_key)

        with pytest.raises(Forbidden):
            await (await approver.backend()).publish_version(version.robot_id, 1)
        assert not approver.principal.has_role(Role.APPROVER, tenant.area_id)
        with pytest.raises(NotAuthenticated):
            await operator.backend()
        assert await auth.store.load(operator_key) is None

    current = await platform.get_version(tenant.creator, version.robot_id, 1)
    assert current is not None and current.status is VersionStatus.VALIDATED
    assert await platform.list_recent(tenant.creator, 10) == []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def metrics_port() -> int:
    """A porta das métricas do processo (livre: nunca a 9100 de um outro serviço da máquina)."""
    return _free_port()


@pytest.fixture
def server_process(
    pg_database: PgDatabase,
    tmp_path: Path,
    tokens: dict[str, Any],
    metrics_port: int,
) -> Iterator[str]:
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "TMPDIR"} or key.startswith("PLAYWRIGHT_")
    }
    env.update(
        {
            "RAPA_DATABASE_URL": pg_database.app_url,
            "RAPA_DISPATCHER_DATABASE_URL": pg_database.dispatcher_url,
            "RAPA_DEV": "1",
            "RAPA_COOKIE_SEGURO": "0",
            "RAPA_TOKENS_DEV": str(_tokens(tmp_path, tokens)),
            "RAPA_PASTA_TRABALHO": str(tmp_path / "trabalho"),
            "RAPA_PASTA_ARTEFATOS": str(tmp_path / "artefatos"),
            "RAPA_METRICAS_PORTA": str(metrics_port),
        }
    )
    process = subprocess.Popen(  # noqa: S603 - comando fixo, sem shell
        [sys.executable, "-m", "rapa.server", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                stderr = process.stderr.read().decode() if process.stderr else ""
                raise AssertionError(f"o servidor saiu com {process.returncode}: {stderr[-2000:]}")
            try:
                if httpx2.get(f"{base}/saude", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("o servidor não respondeu em 30 s")
            time.sleep(0.2)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
async def tokens(app_db: Any) -> dict[str, Any]:
    tenant = await make_tenant(app_db, "Servidor")
    return {
        "token-operadora": {
            "sujeito": tenant.operator.subject,
            "empresa": str(tenant.company_id),
        },
        "token-sem-usuario": {"sujeito": "ninguem", "empresa": str(tenant.company_id)},
    }


@pytest.mark.database
def test_server_process_serves_api_mcp_and_ui(
    server_process: str, tokens: dict[str, Any], metrics_port: int
) -> None:
    operator = tokens["token-operadora"]
    me = httpx2.get(
        f"{server_process}/v1/eu", headers={"Authorization": "Bearer token-operadora"}, timeout=5
    )
    assert me.status_code == 200, me.text
    assert me.json()["subject"] == operator["sujeito"]
    # Token conhecido, mas sem usuário ativo no banco: sem acesso.
    denied = httpx2.get(
        f"{server_process}/v1/eu", headers={"Authorization": "Bearer token-sem-usuario"}, timeout=5
    )
    assert denied.status_code == 403
    assert httpx2.get(f"{server_process}/v1/catalogo", timeout=5).status_code == 401
    # MCP sem token: 401 com o desafio Bearer.
    mcp = httpx2.post(f"{server_process}/mcp", json={}, timeout=5)
    assert mcp.status_code == 401
    # A interface responde com a tela de entrada, com um perfil por token válido.
    with httpx2.Client(base_url=server_process, follow_redirects=True, timeout=10) as browser:
        page = browser.get("/catalogo")
    assert page.status_code == 200
    assert page.url.path == "/entrar"
    assert "RaPA" in page.text
    # Pronto: o banco, o do despachante e o despachante respondem.
    ready = httpx2.get(f"{server_process}/saude/pronto", timeout=5)
    assert ready.status_code == 200 and ready.json() == {"estado": "pronto"}
    # As métricas ficam na porta própria, nunca na do serviço.
    assert httpx2.get(f"{server_process}/metrics", timeout=5).status_code == 404
    metrics = ""
    for _ in range(100):
        metrics = httpx2.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=5).text
        # A primeira leitura da fila pelo despachante sai logo na subida.
        if "rapa_fila_execucoes " in metrics:
            break
        time.sleep(0.1)
    assert "rapa_fila_execucoes " in metrics
    assert "rapa_despachante_saudavel 1.0" in metrics
    assert 'rapa_despachante_etapa_ok{etapa="claim"} 1.0' in metrics
    assert 'rota="/v1/eu"' in metrics and operator["empresa"] not in metrics


# ---------------------------------------------------------------------------
# Observabilidade e saúde
# ---------------------------------------------------------------------------

#: Um banco que recusa a conexão na hora (porta fechada).
DOWN_DB = "postgresql://rapa_app:senha-que-nao-aparece@127.0.0.1:1/rapa"
DOWN_DISPATCHER_DB = "postgresql://rapa_dispatcher:senha-que-nao-aparece@127.0.0.1:1/rapa"


def test_observability_configuration(tmp_path: Path) -> None:
    base = {"RAPA_DATABASE_URL": DB, "RAPA_DEV": "1", "RAPA_TOKENS_DEV": str(_tokens(tmp_path))}
    defaults = ServerSettings.from_env(base)
    assert (defaults.metrics_port, defaults.metrics_per_company, defaults.log_json) == (
        9100,
        False,
        False,
    )
    assert defaults.otel_endpoint is None and defaults.glitchtip_dsn is None
    dsn = "https://chave-do-projeto@glitchtip.exemplo/3"
    settings = ServerSettings.from_env(
        {
            **base,
            "RAPA_METRICAS_PORTA": "0",
            "RAPA_METRICAS_POR_EMPRESA": "1",
            "RAPA_LOG_JSON": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://coletor.exemplo:4318",
            "OTEL_SERVICE_NAME": "rapa-api",
            "RAPA_GLITCHTIP_DSN": dsn,
        }
    )
    assert settings.metrics_port == 0 and settings.metrics_per_company and settings.log_json
    assert settings.otel_endpoint == "http://coletor.exemplo:4318"
    assert settings.otel_service_name == "rapa-api"
    # O DSN tem a chave do projeto: nunca na representação.
    assert settings.glitchtip_dsn == dsn and "chave-do-projeto" not in repr(settings)
    for env, message in (
        ({"RAPA_METRICAS_PORTA": "70000"}, "metrics_port"),
        ({"RAPA_METRICAS_PORTA": "-1"}, "metrics_port"),
        ({"RAPA_LOG_JSON": "talvez"}, "1 ou 0"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "coletor:4318"}, "OTEL_EXPORTER_OTLP_ENDPOINT"),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c/v1/traces"}, "só liga com"),
    ):
        with pytest.raises(ValueError, match=message):
            ServerSettings.from_env({**base, **env})


#: Segredos da configuração: nenhum pedaço deles pode aparecer numa recusa.
KEY = "Kq7vZx93WmPd"
PASSWORD = "Hj5Rt2Lw9Bnq"
WEBHOOK_KEY = "Zp4Yt8QsXcGe"
LLM_KEY = "Mv6Nb3Ty7UeA"
OIDC_SECRET = "Fw2Dk8Pm4RsC"


def test_configuration_errors_never_carry_secrets(tmp_path: Path) -> None:
    base = {
        "RAPA_DATABASE_URL": f"postgresql://rapa_app:{PASSWORD}@banco:5432/rapa",
        "RAPA_DISPATCHER_DATABASE_URL": f"postgresql://rapa_dispatcher:{PASSWORD}@banco/rapa",
        "RAPA_DEV": "1",
        "RAPA_TOKENS_DEV": str(_tokens(tmp_path)),
        "RAPA_LLM_API_KEY": LLM_KEY,
        "RAPA_OIDC_CLIENT_SECRET": OIDC_SECRET,
        "RAPA_GLITCHTIP_DSN": f"https://{KEY}@glitchtip.exemplo/3",
    }
    assert ServerSettings.from_env(base).glitchtip_dsn == base["RAPA_GLITCHTIP_DSN"]
    for extra, message in (
        # O DSN recusado pela forma (o sentry_sdk recusaria ao ligar).
        ({"RAPA_GLITCHTIP_DSN": f"ftp://{KEY}@glitchtip.exemplo/3"}, "http:// ou https://"),
        ({"RAPA_GLITCHTIP_DSN": f"https://{KEY}@glitchtip.exemplo/"}, "número do projeto"),
        ({"RAPA_GLITCHTIP_DSN": "https://glitchtip.exemplo/3"}, "chave do projeto"),
        # Uma regra do modelo que nada tem com os segredos: o pydantic punha na mensagem
        # o fim do dicionário inteiro, com o DSN.
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c/v1/traces"}, "só liga com"),
        ({"RAPA_LLM_BASE_URL": "http://hub.exemplo"}, "hub de LLM"),
        # Um campo recusado: o pydantic punha o valor.
        ({"RAPA_CHAVE_WEBHOOKS": WEBHOOK_KEY}, "webhook_key"),
    ):
        with pytest.raises(ValueError, match=message) as refused:
            ServerSettings.from_env({**base, **extra})
        detail = str(refused.value)
        for secret in (KEY, PASSWORD, WEBHOOK_KEY, LLM_KEY, OIDC_SECRET):
            assert not leaked(secret, detail), (extra, detail)
    # Montada direto (sem as variáveis), a mesma coisa.
    with pytest.raises(ValueError, match="não teria o que fazer") as refused:
        ServerSettings(database_url=f"postgresql://rapa_app:{PASSWORD}@banco/rapa", http=False)
    assert not leaked(PASSWORD, str(refused.value))


def test_metrics_are_wired_into_the_queue_dispatcher_sessions_and_webhooks(tmp_path: Path) -> None:
    key = "chave-dos-webhooks-que-nunca-aparece-em-lugar-nenhum"
    app = create_server_app(
        ServerSettings(
            database_url=DOWN_DB,
            dispatcher_database_url=DOWN_DISPATCHER_DB,
            dev=True,
            dev_tokens_file=_tokens(tmp_path),
            ui=False,
            webhook_key=key,
            metrics_per_company=True,
        )
    )
    metrics = app.state.metrics
    assert metrics.per_company
    # Cada execução concluída pela fila conta nas métricas do processo.
    assert app.state.dispatcher.queue.observer is metrics
    assert isinstance(app.state.publishing.receiver, MeteredWebhooks)
    assert app.state.publishing.receiver.metrics is metrics
    assert metrics._dispatcher.supervisor is app.state.dispatcher_supervisor
    assert metrics._sessions.sessions is app.state.sessions is not None
    # Sem OTEL_EXPORTER_OTLP_ENDPOINT, nada de rastreio.
    assert app.state.tracing is None and app.state.dispatcher.tracer is None
    assert {check.name for check in app.state.readiness.checks} == {
        "banco",
        "banco_despachante",
        "despachante",
    }


async def test_readiness_names_what_is_down_while_liveness_stays_up(tmp_path: Path) -> None:
    app = create_server_app(
        ServerSettings(database_url=DOWN_DB, dev=True, dev_tokens_file=_tokens(tmp_path), ui=False)
    )
    async with running(app), asgi_client(app) as client:
        # Vivo: o processo atende (o banco fora não derruba a sonda de vida).
        assert (await client.get("/saude")).status_code == 200
        response = await client.get("/saude/pronto")
        assert response.status_code == 503
        assert response.json() == {"estado": "nao_pronto", "falhas": ["banco"]}
        assert response.headers["cache-control"] == "no-store"
        assert "senha-que-nao-aparece" not in response.text
        # A porta das métricas é outra: a aplicação pública não tem /metrics.
        assert (await client.get("/metrics")).status_code == 404
    metrics = app.state.metrics.render().decode()
    assert 'codigo="503",metodo="GET",rota="/saude/pronto"' in metrics


async def test_readiness_of_a_dispatcher_process_follows_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "rapa.server.build_dispatcher", lambda settings, db, *_: FakeDispatcher(crashes=1000)
    )
    app = create_server_app(
        ServerSettings(database_url=DOWN_DB, dispatcher_database_url=DOWN_DISPATCHER_DB, http=False)
    )
    supervisor: DispatcherSupervisor = app.state.dispatcher_supervisor
    async with running(app), asgi_client(app) as client:
        await _until(lambda: not supervisor.healthy)
        response = await client.get("/saude/pronto")
        assert response.status_code == 503
        assert response.json() == {
            "estado": "nao_pronto",
            "falhas": ["banco", "banco_despachante", "despachante"],
        }


@pytest.mark.database
async def test_readiness_is_ready_with_the_database_up(
    pg_database: PgDatabase, tmp_path: Path
) -> None:
    app = create_server_app(
        ServerSettings(
            database_url=pg_database.app_url,
            dev=True,
            dev_tokens_file=_tokens(tmp_path),
            ui=False,
            sessions=False,
        )
    )
    async with running(app), asgi_client(app) as client:
        response = await client.get("/saude/pronto")
    assert response.status_code == 200 and response.json() == {"estado": "pronto"}


def _main_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str) -> None:
    for name in list(os.environ):
        if name.startswith(("RAPA_", "OTEL_")):
            monkeypatch.delenv(name)
    env = {
        "RAPA_DATABASE_URL": DOWN_DB,
        "RAPA_DEV": "1",
        "RAPA_TOKENS_DEV": str(_tokens(tmp_path)),
        "RAPA_UI": "0",
        "RAPA_SESSOES": "0",
        "RAPA_VIDEO_EM_FALHAS": "0",
        **extra,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_main_refuses_metrics_on_the_service_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rapa.server import main

    _main_env(monkeypatch, tmp_path, RAPA_METRICAS_PORTA="8123")
    assert main(["--port", "8123"]) == 2
    assert "RAPA_METRICAS_PORTA não pode ser a porta do serviço" in capsys.readouterr().err


def test_main_does_not_start_without_the_metrics_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from rapa.server import main

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        port = busy.getsockname()[1]
        _main_env(monkeypatch, tmp_path, RAPA_METRICAS_PORTA=str(port))
        assert main(["--port", str(_free_port())]) == 2
    assert f"não foi possível abrir a porta {port} das métricas: OSError" in caplog.text


def test_main_refuses_a_malformed_dsn_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rapa.server import main

    for dsn in (
        f"ftp://{KEY}@glitchtip.exemplo/1",
        f"https://{KEY}@glitchtip.exemplo/",
        "https://glitchtip.exemplo/1",
    ):
        _main_env(monkeypatch, tmp_path, RAPA_GLITCHTIP_DSN=dsn, RAPA_LOG_JSON="1")
        # A saída limpa de configuração inválida, nunca a pilha do BadDsn.
        assert main(["--port", str(_free_port())]) == 2
        err = capsys.readouterr().err
        assert err.startswith("rapa.server: configuração inválida:"), err
        assert "RAPA_GLITCHTIP_DSN" in err and "Traceback" not in err
        assert not leaked(KEY, err), err
    assert not glitchtip_module._enabled


class _Envelopes(Transport):
    """Guarda os envelopes do GlitchTip em memória."""

    def __init__(self, dsn: str) -> None:
        super().__init__({"dsn": dsn})
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def events(self) -> list[dict[str, Any]]:
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "event" and isinstance(item.payload.json, dict)
        ]


@pytest.fixture
def memory_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[list[dict[str, Any]], InMemorySpanExporter]]:
    """``activate_tracing`` do servidor com um exportador em memória (guarda as chamadas)."""
    exporter = InMemorySpanExporter()
    calls: list[dict[str, Any]] = []
    created: list[Tracing] = []

    def activate(**options: Any) -> Tracing:
        calls.append(options)
        tracing = activate_tracing(**options, exporter=exporter)
        created.append(tracing)
        return tracing

    monkeypatch.setattr("rapa.server.activate_tracing", activate)
    try:
        yield calls, exporter
    finally:
        for tracing in created:
            if tracing_module._process is tracing:
                tracing.shutdown()


async def test_tracing_is_wired_into_requests_and_the_dispatcher(
    tmp_path: Path, memory_tracing: tuple[list[dict[str, Any]], InMemorySpanExporter]
) -> None:
    calls, exporter = memory_tracing
    settings = {
        "database_url": DOWN_DB,
        "dev": True,
        "dev_tokens_file": _tokens(tmp_path),
        "ui": False,
        "sessions": False,
        "otel_endpoint": "http://coletor.exemplo:4318",
        "otel_traces_endpoint": "http://coletor.exemplo:4318/traces",
        "otel_service_name": "rapa-api",
    }
    app = create_server_app(ServerSettings(**settings, dispatcher_database_url=DOWN_DISPATCHER_DB))
    # A configuração chega ao rastreio, e o despachante ganha os spans das execuções.
    assert [
        {key: call[key] for key in ("endpoint", "traces_endpoint", "service_name")}
        for call in calls
    ] == [
        {
            "endpoint": "http://coletor.exemplo:4318",
            "traces_endpoint": "http://coletor.exemplo:4318/traces",
            "service_name": "rapa-api",
        }
    ]
    tracing = app.state.tracing
    assert isinstance(tracing, Tracing)
    tracer = app.state.dispatcher.tracer
    assert isinstance(tracer, RunSpans)
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=protocolo(),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    with tracer.run(job) as span:
        span.finished({"state": "succeeded"})
    # Os pedidos da aplicação de verdade (sem o despachante, que não sobe aqui).
    served = create_server_app(ServerSettings(**settings))
    assert served.state.tracing is tracing
    async with running(served), asgi_client(served) as client:
        assert (await client.get("/v1/eu")).status_code == 401
        assert (await client.get("/saude")).status_code == 200
        assert (await client.get("/saude/pronto")).status_code == 503
    tracing.flush()
    spans = exporter.get_finished_spans()
    # A saúde fica de fora; o pedido sai com a rota modelo.
    servers = [span for span in spans if span.kind is SpanKind.SERVER]
    assert [(span.attributes or {}).get("http.route") for span in servers] == ["/v1/eu"]
    [run_span] = [span for span in spans if span.name == "execucao"]
    assert (run_span.attributes or {})["rapa.execucao.id"] == str(job.run_id)


def test_main_wires_glitchtip_tracing_and_the_metrics_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_tracing: tuple[list[dict[str, Any]], InMemorySpanExporter],
) -> None:
    import uvicorn

    from rapa.server import _version, main

    calls, _ = memory_tracing
    dsn = f"https://{KEY}@glitchtip.exemplo/3"
    transport = _Envelopes(dsn)
    configured: list[tuple[str, str | None]] = []

    def configure(value: str, *, release: str | None = None) -> Any:
        configured.append((value, release))
        return configure_glitchtip(value, release=release, transport=transport)

    monkeypatch.setattr("rapa.server.configure_glitchtip", configure)
    metrics_port = _free_port()
    seen: dict[str, Any] = {}

    def run(app: FastAPI, **options: Any) -> None:
        seen["app"], seen["options"] = app, options
        # Com o serviço de pé: o GlitchTip ligado e as métricas na porta própria.
        seen["enabled"] = glitchtip_module._enabled
        report_unhandled(LookupError(f"falha com {PASSWORD}"))
        scraped = httpx2.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=5)
        seen["metrics"] = scraped.status_code

    monkeypatch.setattr(uvicorn, "run", run)
    _main_env(
        monkeypatch,
        tmp_path,
        RAPA_DISPATCHER_DATABASE_URL=DOWN_DISPATCHER_DB,
        RAPA_GLITCHTIP_DSN=dsn,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://coletor.exemplo:4318",
        RAPA_METRICAS_PORTA=str(metrics_port),
    )
    assert main(["--port", str(_free_port())]) == 0
    assert configured == [(dsn, f"rapa@{_version()}")]
    assert [call["endpoint"] for call in calls] == ["http://coletor.exemplo:4318"]
    app = seen["app"]
    # O rastreio que o main ligou é o da aplicação e o do despachante.
    assert isinstance(app.state.tracing, Tracing)
    assert isinstance(app.state.dispatcher.tracer, RunSpans)
    assert seen["enabled"] and seen["metrics"] == 200
    [event] = transport.events()
    [exception] = event["exception"]["values"]
    assert exception["type"] == "LookupError" and exception["value"] == OMITTED_MESSAGE
    assert event["release"] == f"rapa@{_version()}"
    assert PASSWORD not in json.dumps(event)
    # No fim, tudo desligado: o GlitchTip, o rastreio e a porta das métricas.
    assert not glitchtip_module._enabled and tracing_module._process is None
    with pytest.raises(httpx2.TransportError):
        httpx2.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=1)


@pytest.mark.parametrize(("argv", "host"), [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")])
def test_the_metrics_port_listens_where_the_service_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], host: str
) -> None:
    import uvicorn

    from rapa.server import main

    hosts: list[str | None] = []

    def serve(metrics: Any, port: int, host: str | None = None) -> Any:
        hosts.append(host)  # None: o main não disse onde (o padrão da função)
        return serve_metrics(metrics, port) if host is None else serve_metrics(metrics, port, host)

    seen: dict[str, Any] = {}
    monkeypatch.setattr("rapa.server.serve_metrics", serve)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: seen.update(options))
    _main_env(monkeypatch, tmp_path, RAPA_METRICAS_PORTA=str(_free_port()))
    assert main([*argv, "--port", str(_free_port())]) == 0
    # Com o padrão, nada fica aberto na rede: as métricas seguem o --host do serviço.
    assert hosts == [host] and seen["host"] == host


class _FakeCollector(BaseHTTPRequestHandler):
    """Um GlitchTip e um coletor OTLP falsos: guarda o caminho, os cabeçalhos e o corpo."""

    received: list[tuple[str, dict[str, str], bytes]]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        type(self).received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_server_process_sends_clean_errors_and_traces(tmp_path: Path) -> None:
    """O processo de verdade, com ``RAPA_GLITCHTIP_DSN`` e ``OTEL_EXPORTER_OTLP_ENDPOINT``."""
    _FakeCollector.received = []
    collector = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCollector)
    thread = threading.Thread(target=collector.serve_forever, daemon=True)
    thread.start()
    collector_port = collector.server_address[1]
    port = _free_port()
    token = "token-do-glitchtip-que-nao-sai"
    cookie = "cookie-que-nao-sai"
    query = "consulta-que-nao-sai"
    tokens = _tokens(tmp_path, {token: {"sujeito": "ana", "empresa": str(new_id())}})
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "TMPDIR"} or key.startswith("PLAYWRIGHT_")
    }
    env.update(
        {
            "RAPA_DATABASE_URL": DOWN_DB,
            "RAPA_DEV": "1",
            "RAPA_TOKENS_DEV": str(tokens),
            "RAPA_UI": "0",
            "RAPA_SESSOES": "0",
            "RAPA_VIDEO_EM_FALHAS": "0",
            "RAPA_METRICAS_PORTA": "0",
            "RAPA_GLITCHTIP_DSN": f"http://{KEY}@127.0.0.1:{collector_port}/7",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{collector_port}",
            "OTEL_BSP_SCHEDULE_DELAY": "100",
        }
    )
    process = subprocess.Popen(  # noqa: S603 - comando fixo, sem shell
        [sys.executable, "-m", "rapa.server", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "o servidor saiu antes de responder"
            try:
                if httpx2.get(f"{base}/saude", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            assert time.monotonic() < deadline, "o servidor não respondeu em 30 s"
            time.sleep(0.2)
        # Com o banco fora, o pedido autenticado cai no último recurso da API: 500.
        failed = httpx2.get(
            f"{base}/v1/eu?busca={query}",
            headers={"Authorization": f"Bearer {token}", "Cookie": f"sessao={cookie}"},
            timeout=10,
        )
        assert failed.status_code == 500
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            paths = [path for path, _, _ in _FakeCollector.received]
            if "/api/7/envelope/" in paths and "/v1/traces" in paths:
                break
            time.sleep(0.1)
    finally:
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
        collector.shutdown()
        collector.server_close()
    received = list(_FakeCollector.received)
    envelopes = [body for path, _, body in received if path == "/api/7/envelope/"]
    traces = [body for path, _, body in received if path == "/v1/traces"]
    assert envelopes, stderr.decode()[-2000:]
    assert traces, stderr.decode()[-2000:]
    # O GlitchTip recebe a exceção sem a mensagem, sem o pedido e sem os dados dele.
    events = [
        json.loads(line)
        for body in envelopes
        for line in body.decode().splitlines()
        if line.startswith("{") and '"exception"' in line
    ]
    assert events
    for event in events:
        for item in event["exception"]["values"]:
            assert item["value"] == OMITTED_MESSAGE
        assert not {"request", "user", "extra", "breadcrumbs", "tags"} & set(event)
    sent = b"".join(envelopes + traces)
    for secret in (token, cookie, query, "senha-que-nao-aparece"):
        assert secret.encode() not in sent
    # O coletor recebe os spans dos pedidos, pela rota modelo.
    assert b"/v1/eu" in b"".join(traces)


def test_server_process_without_database_logs_json_and_serves_metrics(tmp_path: Path) -> None:
    """O processo de verdade: logs em JSON, métricas na porta própria e prontidão 503."""
    port, metrics_port = _free_port(), _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "TMPDIR"} or key.startswith("PLAYWRIGHT_")
    }
    env.update(
        {
            "RAPA_DATABASE_URL": DOWN_DB,
            "RAPA_DEV": "1",
            "RAPA_TOKENS_DEV": str(_tokens(tmp_path)),
            "RAPA_UI": "0",
            "RAPA_SESSOES": "0",
            "RAPA_VIDEO_EM_FALHAS": "0",
            "RAPA_METRICAS_PORTA": str(metrics_port),
            "RAPA_LOG_JSON": "1",
        }
    )
    process = subprocess.Popen(  # noqa: S603 - comando fixo, sem shell
        [sys.executable, "-m", "rapa.server", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, "o servidor saiu antes de responder"
            try:
                if httpx2.get(f"{base}/saude", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            assert time.monotonic() < deadline, "o servidor não respondeu em 30 s"
            time.sleep(0.2)
        ready = httpx2.get(f"{base}/saude/pronto", timeout=5)
        assert ready.status_code == 503 and ready.json()["falhas"] == ["banco"]
        assert httpx2.get(f"{base}/metrics", timeout=5).status_code == 404
        scraped = httpx2.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=5)
        assert scraped.status_code == 200
        assert 'rapa_http_pedidos_total{codigo="503",metodo="GET",rota="/saude/pronto"} 1.0' in (
            scraped.text
        )
        assert "process_resident_memory_bytes" in scraped.text
    finally:
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
    lines = [line for line in stderr.decode().splitlines() if line.strip()]
    assert lines, "o servidor não registrou nada"
    payloads = [json.loads(line) for line in lines]  # toda linha é um objeto JSON
    for payload in payloads:
        assert {"hora", "nivel", "logger", "mensagem"} <= set(payload)
    loggers = {payload["logger"] for payload in payloads}
    # As linhas do uvicorn e as do serviço, todas no mesmo formato.
    assert "uvicorn.error" in loggers and "rapa.control.saude" in loggers
    assert "senha-que-nao-aparece" not in stderr.decode()
