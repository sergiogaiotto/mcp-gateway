"""Configuração do serviço e o processo ``python -m rapa.server`` de verdade."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text

from rapa.control.services import PgPlatform
from rapa.core.contracts import Forbidden, Role, VersionStatus
from rapa.core.ids import new_id
from rapa.server import (
    DispatcherSupervisor,
    ServerSettings,
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
