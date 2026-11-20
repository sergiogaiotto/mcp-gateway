"""Configuração do serviço e o processo ``python -m rapa.server`` de verdade."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from rapa.control.auth import TokenVerifier
from rapa.control.db import Database
from rapa.control.mcp import McpGateway
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

    def build(settings: ServerSettings, db: Any) -> FakeDispatcher:
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
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db: fake)
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
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db: fake)
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
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db: fake)
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
    monkeypatch.setattr("rapa.server.build_dispatcher", lambda settings, db: dispatcher)
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
def server_process(
    pg_database: PgDatabase,
    tmp_path: Path,
    tokens: dict[str, Any],
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
def test_server_process_serves_api_mcp_and_ui(server_process: str, tokens: dict[str, Any]) -> None:
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
