"""Configuração do serviço e o processo ``python -m rapa.server`` de verdade."""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest

from rapa.core.ids import new_id
from rapa.server import ServerSettings, load_dev_tokens
from tests.control.db.conftest import app_db, pg_database  # noqa: F401
from tests.control.db.support import PgDatabase, make_tenant

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server_process(
    pg_database: PgDatabase,  # noqa: F811
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
async def tokens(app_db: Any) -> dict[str, Any]:  # noqa: F811
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
