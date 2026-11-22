"""Capturas de tela do RaPA para o manual de instalação, configuração e testes.

Sobe um mundo de demonstração inteiro só com as ferramentas locais (sem Docker) e
fotografa as telas de verdade::

    uv run python docs/manual/capturar.py --saida docs/manual/img

O roteiro:

1. um cluster PostgreSQL temporário (``initdb`` e ``pg_ctl`` do PATH ou de
   ``/usr/lib/postgresql/*/bin``), com os papéis ``rapa_owner``, ``rapa_app`` e
   ``rapa_dispatcher`` criados como no README, as migrações e as permissões
   (``python -m rapa.control.db.migrate``);
2. os portais de teste v1 e v2 (``rapa.fixtures.portal``) em ``portal-v1.rapa.test`` e
   ``portal-v2.rapa.test``, que o Chromium resolve para 127.0.0.1;
3. a "Empresa Demonstração" pela linha de comando de verdade
   (``python -m rapa.control.admin``), com as áreas Fiscal e Financeiro e sete
   pessoas de nomes inventados (administração da empresa, criação, duas de aprovação,
   operação, auditoria e custódia do certificado A1); o arquivo de tokens de
   desenvolvimento e o de segredos dos portais;
4. o serviço inteiro (``python -m rapa.server``) com o despachante, os webhooks, o
   vídeo das falhas e a documentação da API (``RAPA_DOCS_API=1``) ligados;
5. o estado da demonstração pela API REST, pelo MCP e pela interface: robôs
   publicados por quem aprova, execuções que deram certo e uma que falhou (com telas,
   vídeo e o retrato para o conserto), agendas, webhooks, um lote de CSV, o
   certificado A1 com custodiante e autorização, um robô com 1 de 2 aprovações, uma
   gravação ao vivo no portal v1 e o consumo do mês;
6. as capturas, com o Playwright (1440x900, escala 2, pt-BR), entrando pelo login de
   desenvolvimento com a pessoa certa para cada tela;
7. ``capturas.json`` com a legenda, a pessoa e o endereço de cada imagem.

Opções: ``--saida`` (a pasta das imagens; as antigas só são trocadas no fim, com todas
as novas prontas), ``--porta`` (a do serviço, se livre; padrão 8000), ``--chromium`` (o
Chromium completo; padrão: ``RAPA_CHROMIUM_PATH``, ``/usr/bin/chromium``,
``/opt/pw-browsers/chromium`` ou o do Playwright) e ``--depurar PASTA`` (numa falha,
guarda ali a tela de cada página aberta, as capturas já feitas e o log do serviço).

No fim (também num erro ou num Ctrl+C), tudo para e as pastas temporárias somem. Nenhum
token, chave ou senha aparece nas imagens: cada página é conferida antes da captura.
"""

import argparse
import asyncio
import contextlib
import csv
import hashlib
import io
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from playwright.async_api import (
    Browser,
    BrowserContext,
    FloatRect,
    Locator,
    Page,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout
from psycopg import sql

from rapa.control.services.webhooks import sign_delivery
from rapa.core.robot import Manifest
from rapa.fixtures.portal import (
    DEFAULT_PASSWORD,
    DEFAULT_TOTP_SECRET,
    DEFAULT_USER,
    Variant,
    create_app,
    current_totp,
)
from rapa.fixtures.robots import baixar_notas, emitir_protocolo, login_steps, portal_credentials
from rapa.fixtures.server import HOST_RESOLVER_ARG

REPO = Path(__file__).resolve().parents[2]
EMPRESA = "Empresa Demonstração"
DOMINIO = "demonstracao.example"
VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
#: Portas preferidas (quando livres), para os endereços nas imagens não mudarem a cada vez.
PORTA_RAPA = 8000
PORTAS_PORTAL: dict[Variant, int] = {"v1": 8101, "v2": 8102}
_CHROMIUM = ("/usr/bin/chromium", "/opt/pw-browsers/chromium")
#: Entradas inventadas (os CPFs são os de teste do portal, com dígitos válidos).
PESSOAS_PROTOCOLO = {
    "rest": {"nome": "Helena Duarte", "cpf": "529.982.247-25", "uf": "SP"},
    "mcp": {"nome": "Lucas Teixeira", "cpf": "111.444.777-35", "uf": "MG"},
    "webhook": {"nome": "Marina Lopes", "cpf": "935.411.347-80", "uf": "RJ"},
}
LOTE = [
    ("Nome completo", "CPF", "UF"),
    ("Igor Batista", "529.982.247-25", "SP"),
    ("Júlia Freitas", "111.444.777-35", "PR"),
    ("Kátia Moreira", "935.411.347-80", "BA"),
]
ARQUIVO_LOTE = "protocolos-novembro.csv"
_FINISHED = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})


def log(message: str) -> None:
    print(f"[capturar] {message}", flush=True)


# ---------------------------------------------------------------------------
# Pessoas da demonstração
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    #: Papéis para ``usuario --papel`` (``{fiscal}`` e ``{financeiro}`` viram os ids).
    roles: tuple[str, ...]
    description: str

    @property
    def subject(self) -> str:
        return f"{self.key}@{DOMINIO}"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.description}) · {self.subject}"


ANA = Persona("ana.lima", "Ana Lima", (), "administração da empresa")
BRUNO = Persona(
    "bruno.reis",
    "Bruno Reis",
    ("creator:{fiscal}", "creator:{financeiro}"),
    "criação de robôs no Fiscal e no Financeiro",
)
CARLA = Persona(
    "carla.souza",
    "Carla Souza",
    ("approver:{fiscal}", "approver:{financeiro}"),
    "aprovação no Fiscal e no Financeiro",
)
DANIEL = Persona(
    "daniel.rocha",
    "Daniel Rocha",
    ("approver:{fiscal}", "approver:{financeiro}"),
    "aprovação no Fiscal e no Financeiro",
)
EDUARDA = Persona(
    "eduarda.martins", "Eduarda Martins", ("operator",), "operação na empresa inteira"
)
FABIO = Persona("fabio.nunes", "Fábio Nunes", ("auditor",), "auditoria na empresa inteira")
GABRIELA = Persona(
    "gabriela.prado",
    "Gabriela Prado",
    ("credential_custodian:{financeiro}",),
    "custódia do certificado A1 no Financeiro",
)
PESSOAS = (ANA, BRUNO, CARLA, DANIEL, EDUARDA, FABIO, GABRIELA)


# ---------------------------------------------------------------------------
# Processos e pastas
# ---------------------------------------------------------------------------


def _free_port(preferred: int | None = None) -> int:
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Como o uvicorn: as conexões da rodada anterior (TIME_WAIT) não seguram a porta.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", preferred))
            except OSError:
                pass
            else:
                return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(path)))


def find_pg_bindir() -> Path:
    """Os binários do servidor PostgreSQL, como os testes procuram."""
    initdb = shutil.which("initdb")
    if initdb and shutil.which("pg_ctl"):
        return Path(initdb).parent
    for bindir in sorted(Path("/usr/lib/postgresql").glob("*/bin"), key=_version_key, reverse=True):
        if (bindir / "initdb").exists() and (bindir / "pg_ctl").exists():
            return bindir
    raise SystemExit("PostgreSQL não encontrado: instale os binários do servidor (initdb e pg_ctl)")


def find_chromium(explicit: str | None) -> str | None:
    """O Chromium completo: o pedido, ``RAPA_CHROMIUM_PATH`` ou um caminho conhecido.

    Sem nenhum, ``None``: o Playwright usa o navegador que ele mesmo instalou.
    """
    for candidate in (explicit, os.environ.get("RAPA_CHROMIUM_PATH"), *_CHROMIUM):
        if candidate and Path(candidate).exists():
            return candidate
    return shutil.which("chromium")


def ffmpeg_browsers_path(base: Path) -> str | None:
    """Onde o Playwright acha o ``ffmpeg`` da revisão dele (o vídeo das falhas).

    Se a revisão certa já está instalada, nada muda. Senão, monta em ``base`` uma pasta
    com ``ffmpeg-<revisão>/ffmpeg-linux`` apontando para o ``ffmpeg`` do sistema ou de
    outra revisão do Playwright, como os testes fazem. Sem nenhum, ``None``: as
    execuções rodam sem vídeo (o servidor avisa no log).
    """
    import playwright

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "ffmpeg")
    except OSError, ValueError, KeyError, StopIteration:
        return None
    installed = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright"
    )
    if (installed / f"ffmpeg-{revision}" / "ffmpeg-linux").is_file():
        return str(installed)
    candidates = []
    if system := shutil.which("ffmpeg"):
        candidates.append(Path(system))
    candidates += sorted(installed.glob("ffmpeg-*/ffmpeg-linux"), reverse=True)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            target = base / f"ffmpeg-{revision}"
            target.mkdir(parents=True, exist_ok=True)
            (target / "ffmpeg-linux").symlink_to(candidate)
            return str(base)
    return None


def _python(*args: str, env: Mapping[str, str] | None = None) -> str:
    """Roda ``python -m ...`` com o mesmo interpretador e devolve a saída."""
    result = subprocess.run(  # noqa: S603 - os módulos do próprio RaPA
        [sys.executable, *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=REPO,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'python {' '.join(args[:4])} ...' falhou ({result.returncode}): "
            f"{result.stderr.strip()[-2000:]}"
        )
    return result.stdout.strip()


@dataclass
class PgCluster:
    """Um cluster temporário, numa pasta e numa porta livres (como o dos testes)."""

    bindir: Path
    base: Path
    port: int
    password: str
    run_as: str | None

    def _run(self, *args: str) -> None:
        subprocess.run(  # noqa: S603 - binários do próprio PostgreSQL
            list(args), check=True, capture_output=True, text=True, user=self.run_as, cwd=self.base
        )

    def url(self, user: str, password: str, database: str = "rapa") -> str:
        return f"postgresql://{user}:{password}@127.0.0.1:{self.port}/{database}"

    def connect(self, database: str = "postgres") -> psycopg.Connection:
        return psycopg.connect(
            host="127.0.0.1",
            port=self.port,
            user="postgres",
            password=self.password,
            dbname=database,
            autocommit=True,
            connect_timeout=10,
        )

    def stop(self) -> None:
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            self._run(
                str(self.bindir / "pg_ctl"), "-D", str(self.base / "data"), "-m", "fast", "stop"
            )


@contextlib.contextmanager
def pg_cluster() -> Iterator[PgCluster]:
    bindir = find_pg_bindir()
    run_as: str | None = None
    owner: tuple[int, int] | None = None
    if os.geteuid() == 0:
        # O initdb não roda como root: usa o usuário "postgres" do sistema.
        try:
            account = pwd.getpwnam("postgres")
        except KeyError:
            raise SystemExit("rodando como root sem o usuário 'postgres' do sistema") from None
        run_as, owner = account.pw_name, (account.pw_uid, account.pw_gid)
    base = Path(tempfile.mkdtemp(prefix="rapa-manual-pg-"))
    cluster = PgCluster(bindir, base, _free_port(), secrets.token_hex(16), run_as)
    try:
        pwfile = base / "senha"
        pwfile.write_text(cluster.password, encoding="utf-8")
        if owner is not None:
            os.chown(base, *owner)
            os.chown(pwfile, *owner)
        options = (
            f"-p {cluster.port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off "
            "-c synchronous_commit=off -c full_page_writes=off -c max_connections=200"
        )
        try:
            # Conexões pela rede com senha (scram), como num servidor de verdade.
            cluster._run(
                str(bindir / "initdb"),
                "-D",
                str(base / "data"),
                "-U",
                "postgres",
                f"--pwfile={pwfile}",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "-E",
                "UTF8",
                "--no-locale",
            )
            pwfile.unlink()
            cluster._run(
                str(bindir / "pg_ctl"),
                "-D",
                str(base / "data"),
                "-l",
                str(base / "server.log"),
                "-w",
                "-t",
                "60",
                "-o",
                options,
                "start",
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"não foi possível subir o PostgreSQL temporário: {exc.stderr}"
            ) from None
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(base, ignore_errors=True)


@dataclass(frozen=True)
class Database:
    cluster: PgCluster
    passwords: dict[str, str]

    @property
    def owner_url(self) -> str:
        return self.cluster.url("rapa_owner", self.passwords["rapa_owner"])

    @property
    def app_url(self) -> str:
        return self.cluster.url("rapa_app", self.passwords["rapa_app"])

    @property
    def dispatcher_url(self) -> str:
        return self.cluster.url("rapa_dispatcher", self.passwords["rapa_dispatcher"])


def create_database(cluster: PgCluster) -> Database:
    """Os três papéis e o banco, como no README; depois as migrações e as permissões."""
    passwords = {
        name: secrets.token_hex(16) for name in ("rapa_owner", "rapa_app", "rapa_dispatcher")
    }
    statements = (
        ("rapa_owner", ""),
        ("rapa_app", " NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"),
        ("rapa_dispatcher", " NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE"),
    )
    with cluster.connect() as conn:
        for role, attributes in statements:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}" + attributes).format(
                    sql.Identifier(role), sql.Literal(passwords[role])
                )
            )
        conn.execute("CREATE DATABASE rapa OWNER rapa_owner")
    db = Database(cluster, passwords)
    log("migrações do banco (como o dono das tabelas)")
    # A URL (com a senha) vai pelo ambiente: nem a linha de comando nem o erro a mostram.
    owner = {"RAPA_DATABASE_URL": db.owner_url}
    _python("-m", "rapa.control.db.migrate", env=owner)
    _python("-m", "rapa.control.db.migrate", "--somente-permissoes", env=owner)
    return db


# ---------------------------------------------------------------------------
# Portais de teste
# ---------------------------------------------------------------------------


@dataclass
class Portal:
    variant: Variant
    port: int
    server: uvicorn.Server
    thread: threading.Thread
    app: Any

    @property
    def host(self) -> str:
        return f"portal-{self.variant}.rapa.test"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def protocols(self) -> list[dict[str, Any]]:
        return list(self.app.state.portal.protocolos)


@contextlib.contextmanager
def run_portal(variant: Variant) -> Iterator[Portal]:
    """Como ``rapa.fixtures.server.run_portal``, mas numa porta fixa quando ela está livre."""
    app = create_app(variant)
    port = _free_port(PORTAS_PORTAL[variant])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"portal-{variant}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"o portal {variant} não subiu em 15 s")
        time.sleep(0.02)
    try:
        yield Portal(variant, port, server, thread, app)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# A empresa pela linha de comando
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    company_id: UUID
    fiscal: UUID
    financeiro: UUID


def create_company(db: Database) -> Company:
    env = {"RAPA_DATABASE_URL": db.app_url}

    def admin(*args: str) -> UUID:
        return UUID(_python("-m", "rapa.control.admin", *args, env=env).splitlines()[-1])

    company = admin(
        "empresa",
        "criar",
        EMPRESA,
        "--organizacao",
        "empresa-demonstracao",
        "--admin",
        ANA.subject,
        "--nome-admin",
        ANA.name,
        "--vagas",
        "2",
        "--por",
        "equipe-da-plataforma",
    )
    fiscal = admin("area", str(company), "Fiscal", "--por", "equipe-da-plataforma")
    financeiro = admin("area", str(company), "Financeiro", "--por", "equipe-da-plataforma")
    areas = {"fiscal": fiscal, "financeiro": financeiro}
    for person in PESSOAS:
        if person is ANA:
            continue  # já entrou com a empresa
        roles = [r.format(**areas) for r in person.roles]
        admin(
            "usuario",
            str(company),
            person.subject,
            "--nome",
            person.name,
            *[part for role in roles for part in ("--papel", role)],
            "--por",
            ANA.subject,
        )
    return Company(company, fiscal, financeiro)


# ---------------------------------------------------------------------------
# O serviço
# ---------------------------------------------------------------------------


@dataclass
class Service:
    process: subprocess.Popen[bytes]
    base_url: str
    log_path: Path

    def tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        # O serviço, os workers e os navegadores das gravações estão no mesmo grupo.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=10)


@contextlib.contextmanager
def run_service(env: Mapping[str, str], port: int, log_path: Path) -> Iterator[Service]:
    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("RAPA_") and k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    with log_path.open("wb") as out:
        process = subprocess.Popen(  # noqa: S603 - o próprio serviço do RaPA
            [sys.executable, "-m", "rapa.server", "--port", str(port)],
            env={**clean, **env},
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=REPO,
            start_new_session=True,
        )
    service = Service(process, f"http://127.0.0.1:{port}", log_path)
    try:
        yield service
    finally:
        service.stop()


async def wait_healthy(service: Service, limit_s: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    async with httpx2.AsyncClient(base_url=service.base_url, trust_env=False, timeout=5) as client:
        while time.monotonic() < deadline:
            if service.process.poll() is not None:
                raise RuntimeError(f"o serviço parou ao subir:\n{service.tail()}")
            with contextlib.suppress(httpx2.HTTPError):
                response = await client.get("/saude")
                if response.status_code == 200:
                    data: dict[str, Any] = response.json()
                    return data
            await asyncio.sleep(0.5)
    raise RuntimeError(f"o serviço não respondeu /saude em {limit_s:.0f} s:\n{service.tail()}")


# ---------------------------------------------------------------------------
# A API REST
# ---------------------------------------------------------------------------


class Api:
    """Chamadas ao REST em nome de cada pessoa (o token fica só aqui)."""

    def __init__(self, client: httpx2.AsyncClient, tokens: Mapping[str, str]) -> None:
        self.client = client
        self.tokens = tokens

    def headers(self, who: Persona) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who.key]}"}

    async def call(
        self,
        who: Persona,
        method: str,
        path: str,
        *,
        expect: Sequence[int] = (200, 201, 202),
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        response = await self.client.request(
            method, path, headers={**self.headers(who), **(headers or {})}, **kwargs
        )
        if response.status_code not in expect:
            raise RuntimeError(
                f"{method} {path} como {who.key}: {response.status_code} {response.text}"
            )
        return response.json() if response.content else None

    async def publish(
        self, area: UUID, manifest: Manifest, approver: Persona = CARLA
    ) -> dict[str, Any]:
        """Bruno cria e valida; quem aprova publica (quem cria nunca publica)."""
        body = {"area_id": str(area), "manifesto": manifest.model_dump(mode="json")}
        created = await self.call(BRUNO, "POST", "/v1/robos", json=body)
        base = f"/v1/robos/{created['robot_id']}/versoes/{created['version_no']}"
        await self.call(BRUNO, "POST", f"{base}/validar")
        info: dict[str, Any] = await self.call(approver, "POST", f"{base}/publicar")
        return info

    async def start(self, slug: str, inputs: Mapping[str, Any]) -> str:
        status = await self.call(
            EDUARDA, "POST", f"/v1/catalogo/{slug}/execucoes", json={"entradas": dict(inputs)}
        )
        return str(status["run_id"])

    async def wait_run(self, run_id: str, limit_s: float = 180) -> dict[str, Any]:
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            status: dict[str, Any] = await self.call(EDUARDA, "GET", f"/v1/execucoes/{run_id}")
            if status["finished_at"] is not None:
                return status
            await asyncio.sleep(0.5)
        raise RuntimeError(f"a execução {run_id} não terminou em {limit_s:.0f} s")


async def until[T](condition: Callable[[], Awaitable[T | None]], what: str, limit_s: float) -> T:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        value = await condition()
        if value:
            return value
        await asyncio.sleep(0.5)
    raise RuntimeError(f"{what} não aconteceu em {limit_s:.0f} s")


# ---------------------------------------------------------------------------
# Os robôs da demonstração
# ---------------------------------------------------------------------------


def verificar_portal(portal: Portal) -> Manifest:
    """Feito para o layout antigo do portal (botão "Entrar") e rodando no v2 ("Acessar").

    Não digita credencial nenhuma: a falha fica com o vídeo, as telas e o retrato da
    página para o conserto.
    """
    return Manifest.model_validate(
        {
            "name": "verificar_portal",
            "title": "Verificar o acesso ao portal",
            "description": "Abre a página de acesso do portal e confere que o login está disponível.",
            "allowed_domains": [portal.host],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir_login",
                    "title": "Abrir a página de login",
                    "url": f"{portal.base_url}/login",
                },
                {
                    "kind": "wait_for",
                    "id": "aguardar_titulo",
                    "title": "Aguardar o título Entrar",
                    "target": {
                        "intent": "título Entrar",
                        "locators": [{"by": "role", "value": "heading", "name": "Entrar"}],
                    },
                },
                {
                    "kind": "wait_for",
                    "id": "conferir_botao",
                    "title": "Conferir o botão Entrar",
                    "timeout_ms": 3000,
                    "target": {
                        "intent": "botão Entrar",
                        "locators": [{"by": "role", "value": "button", "name": "Entrar"}],
                    },
                },
            ],
        }
    )


def consultar_total(portal: Portal) -> Manifest:
    """Login e leitura do total do relatório (que aparece com atraso), só leitura."""
    return Manifest.model_validate(
        {
            "name": "consultar_total_notas",
            "title": "Consultar o total de notas",
            "description": "Entra no portal, abre o relatório do mês e lê o total de notas.",
            "allowed_domains": [portal.host],
            "credentials": portal_credentials(portal.base_url),
            "steps": [
                *login_steps(portal.base_url),
                {
                    "kind": "navigate",
                    "id": "abrir_relatorio",
                    "title": "Abrir o relatório",
                    "url": f"{portal.base_url}/lento",
                },
                {
                    "kind": "extract_text",
                    "id": "ler_total",
                    "title": "Ler o total de notas",
                    "target": {
                        "intent": "total de notas",
                        "locators": [{"by": "test_id", "value": "total"}],
                    },
                    "into": "total",
                },
            ],
            "outputs": [
                {
                    "name": "total",
                    "title": "Total de notas",
                    "type": "string",
                    "source": "vars.total",
                }
            ],
        }
    )


def protocolo_a1(portal: Portal) -> Manifest:
    """O robô de protocolo do portal declarando também o certificado digital A1."""
    data = emitir_protocolo(portal.base_url, portal.host).model_dump(mode="json")
    data.update(
        name="emitir_protocolo_a1",
        title="Emitir protocolo com certificado A1",
        description="Entra no portal, emite um protocolo e assina com o certificado da empresa.",
    )
    data["credentials"].append(
        {
            "name": "certificado",
            "kind": "pfx_a1",
            "origin": portal.base_url,
            "description": "Certificado digital A1 da empresa",
        }
    )
    return Manifest.model_validate(data)


@dataclass
class Demo:
    """Os identificadores que as capturas usam."""

    robots: dict[str, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)
    schedule_id: str = ""
    batch_id: str = ""
    session_id: str = ""
    #: Valores que nunca podem aparecer numa página capturada.
    secrets: list[str] = field(default_factory=list)


async def build_demo(
    api: Api, company: Company, v1: Portal, v2: Portal, db: Database, base_url: str
) -> Demo:
    demo = Demo()
    log("robôs: Bruno cria e valida, Carla e Daniel publicam")
    for key, area, manifest, approver in (
        ("notas", company.fiscal, baixar_notas(v1.base_url, v1.host), CARLA),
        ("protocolo", company.fiscal, emitir_protocolo(v1.base_url, v1.host), CARLA),
        ("verificar", company.fiscal, verificar_portal(v2), DANIEL),
        ("total", company.financeiro, consultar_total(v1), DANIEL),
    ):
        info = await api.publish(area, manifest, approver)
        demo.robots[key] = info["robot_id"]

    log("credenciais: a senha do portal e o certificado A1 (só a referência ao cofre)")
    await api.call(
        ANA,
        "POST",
        "/v1/credenciais",
        json={
            "area_id": str(company.fiscal),
            "nome": "portal",
            "tipo": "password",
            "origem": v1.base_url,
            "caminho_cofre": "kv/empresa-demonstracao/fiscal/portal",
        },
    )
    await api.call(
        ANA,
        "POST",
        "/v1/credenciais",
        json={
            "area_id": str(company.financeiro),
            "nome": "certificado",
            "tipo": "pfx_a1",
            "origem": v1.base_url,
            "caminho_cofre": "kv/empresa-demonstracao/financeiro/certificado-a1",
            "custodiante": GABRIELA.subject,
            "validade": (datetime.now(UTC) + timedelta(days=300)).isoformat(),
            "impressao": hashlib.sha256(b"certificado A1 de demonstracao").hexdigest(),
        },
    )
    log("robô com A1: autorizado pela custodiante e com 1 de 2 aprovações")
    body = {
        "area_id": str(company.financeiro),
        "manifesto": protocolo_a1(v1).model_dump(mode="json"),
    }
    created = await api.call(BRUNO, "POST", "/v1/robos", json=body)
    demo.robots["a1"] = created["robot_id"]
    version = f"/v1/robos/{created['robot_id']}/versoes/{created['version_no']}"
    await api.call(BRUNO, "POST", f"{version}/validar")
    await api.call(
        GABRIELA,
        "POST",
        f"/v1/robos/{created['robot_id']}/autorizacoes-a1",
        json={"credencial": "certificado"},
    )
    approved = await api.call(CARLA, "POST", f"{version}/publicar")
    if approved["status"] != "validated" or len(approved["approvals"]) != 1:
        raise RuntimeError(f"o robô com A1 deveria ficar com 1 de 2 aprovações: {approved}")

    log("execuções pelo REST e pelo MCP")
    demo.runs["notas"] = await api.start("baixar_notas", {})
    demo.runs["protocolo"] = await api.start("emitir_protocolo", PESSOAS_PROTOCOLO["rest"])
    demo.runs["falha"] = await api.start("verificar_portal", {})
    demo.runs["total"] = await api.start("consultar_total_notas", {})
    async with (
        httpx2.AsyncClient(
            base_url=base_url, headers=api.headers(EDUARDA), trust_env=False, timeout=60
        ) as http,
        Client(streamable_http_client(f"{base_url}/mcp", http_client=http), mode="legacy") as mcp,
    ):
        result = await mcp.call_tool("emitir_protocolo", PESSOAS_PROTOCOLO["mcp"])
        if result.is_error or result.structured_content is None:
            raise RuntimeError(f"a chamada MCP falhou: {result.content}")
        demo.runs["mcp"] = str(result.structured_content["run_id"])

    log("agendas: uma ligada (com uma ocorrência antecipada) e uma desligada")
    schedule = await api.call(
        EDUARDA,
        "POST",
        "/v1/agendas",
        json={
            "robo_id": demo.robots["notas"],
            "titulo": "Notas fiscais toda manhã",
            "cron": "0 8 * * 1-5",
            "entradas": {},
        },
    )
    demo.schedule_id = schedule["schedule_id"]
    await api.call(
        EDUARDA,
        "POST",
        "/v1/agendas",
        json={
            "robo_id": demo.robots["verificar"],
            "titulo": "Conferência do portal em horário comercial",
            "cron": "*/30 8-18 * * 1-5",
            "entradas": {},
            "ativa": False,
        },
    )
    # Em vez de esperar as 8h do próximo dia útil, a ocorrência é antecipada para agora.
    with db.cluster.connect("rapa") as conn:
        conn.execute(
            "UPDATE schedules SET next_run_at = now() - interval '1 second' WHERE id = %s",
            (demo.schedule_id,),
        )

    async def scheduled_run() -> str | None:
        occurrences = await api.call(EDUARDA, "GET", f"/v1/agendas/{demo.schedule_id}/ocorrencias")
        runs = [o["run_id"] for o in occurrences if o["status"] == "enqueued"]
        return str(runs[0]) if runs else None

    demo.runs["agenda"] = await until(scheduled_run, "a ocorrência da agenda", 60)

    log("webhooks: uma entrega assinada vira execução")
    made = await api.call(
        EDUARDA,
        "POST",
        f"/v1/robos/{demo.robots['protocolo']}/webhooks",
        json={"titulo": "Pedidos do ERP", "por_minuto": 60},
    )
    hook, secret = made["webhook"], made["secret"]
    demo.secrets.append(secret)
    other = await api.call(
        EDUARDA,
        "POST",
        f"/v1/robos/{demo.robots['notas']}/webhooks",
        json={"titulo": "Integração do financeiro", "por_minuto": 30},
    )
    demo.secrets.append(other["secret"])
    payload = json.dumps(PESSOAS_PROTOCOLO["webhook"]).encode()
    stamp = str(int(time.time()))
    delivered = await api.client.post(
        hook["path"],
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-RaPA-Timestamp": stamp,
            "X-RaPA-Delivery": "pedido-1001",
            "X-RaPA-Signature": sign_delivery(secret, stamp, "pedido-1001", payload),
        },
    )
    if delivered.status_code != 202:
        raise RuntimeError(f"a entrega do webhook falhou: {delivered.status_code} {delivered.text}")
    demo.runs["webhook"] = str(delivered.json()["run_id"])

    log("lote de 3 linhas, de um CSV")
    sheet = io.StringIO()
    csv.writer(sheet, delimiter=";", lineterminator="\r\n").writerows(LOTE)
    data = {"robo_id": demo.robots["protocolo"]}
    files = {"arquivo": (ARQUIVO_LOTE, sheet.getvalue().encode(), "text/csv")}
    preview = await api.call(EDUARDA, "POST", "/v1/lotes/previa", data=data, files=files)
    if preview["error_rows"]:
        raise RuntimeError(f"o lote tem linhas com erro: {preview}")
    batch = await api.call(EDUARDA, "POST", "/v1/lotes", data=data, files=files)
    demo.batch_id = batch["batch_id"]

    log("esperando as execuções terminarem")
    for key, run_id in demo.runs.items():
        status = await api.wait_run(run_id)
        expected = "failed" if key == "falha" else "succeeded"
        if status["state"] != expected:
            raise RuntimeError(f"a execução '{key}' terminou como {status['state']}: {status}")

    async def batch_done() -> dict[str, Any] | None:
        info: dict[str, Any] = await api.call(EDUARDA, "GET", f"/v1/lotes/{demo.batch_id}")
        done = sum(n for state, n in info["counts"].items() if state in _FINISHED)
        return info if done == info["total"] else None

    info = await until(batch_done, "o fim do lote", 240)
    if info["counts"] != {"succeeded": len(LOTE) - 1}:
        raise RuntimeError(f"o lote não deu certo: {info}")
    artifacts = await api.call(EDUARDA, "GET", f"/v1/execucoes/{demo.runs['falha']}/artefatos")
    kinds = {item["kind"] for item in artifacts}
    if "video" not in kinds:
        log("AVISO: a execução que falhou não guardou vídeo (falta o ffmpeg do Playwright?)")
    repair = await api.call(BRUNO, "GET", f"/v1/execucoes/{demo.runs['falha']}/conserto")
    if not repair["candidates"]:
        raise RuntimeError("o conserto não tem candidatos")
    return demo


# ---------------------------------------------------------------------------
# Capturas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shot:
    file: str
    caption: str
    persona: str
    url: str
    full_page: bool


class Camera:
    """O navegador das capturas: uma sessão (contexto) por pessoa, pelo login de desenvolvimento."""

    def __init__(
        self, browser: Browser, base_url: str, out: Path, forbidden: Sequence[str]
    ) -> None:
        self.browser = browser
        self.base_url = base_url
        self.out = out
        self.forbidden = [value for value in forbidden if value]
        self.shots: list[Shot] = []
        self.pages: dict[str, Page] = {}
        self.contexts: list[BrowserContext] = []
        #: Erros do JavaScript e do console das páginas (mostrados numa falha).
        self.console: list[str] = []

    async def context(self, **extra: Any) -> BrowserContext:
        context = await self.browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            **extra,
        )
        context.set_default_timeout(30_000)
        context.on("page", self._watch)
        self.contexts.append(context)
        return context

    def _watch(self, page: Page) -> None:
        def console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                self.console.append(f"{page.url}: {message.type}: {message.text}")

        page.on("console", console)
        page.on("pageerror", lambda error: self.console.append(f"{page.url}: {error}"))

    async def as_person(self, who: Persona) -> Page:
        """A página de quem entrou pelo login de desenvolvimento (uma vez por pessoa)."""
        if who.key in self.pages:
            return self.pages[who.key]
        page = await (await self.context()).new_page()
        await page.goto(f"{self.base_url}/entrar")
        await page.get_by_role("button", name=f"Entrar como {who.subject}").click()
        await page.wait_for_url("**/catalogo")
        await page.get_by_text("Catálogo de robôs").wait_for()
        self.pages[who.key] = page
        return page

    async def open(self, page: Page, path: str, *texts: str) -> None:
        await page.goto(f"{self.base_url}{path}")
        for text in texts:
            await page.get_by_text(text, exact=False).first.wait_for()

    async def settle(self, page: Page, *, keep_focus: bool = False) -> None:
        """Espera a página assentar: fontes, nenhuma notificação, nenhum carregamento.

        A página volta ao topo (o cabeçalho fixo fica no lugar dele numa captura da página
        inteira), o mouse sai de cima dos botões e, sem ``keep_focus``, nenhum campo fica
        com o foco.
        """
        await page.mouse.move(VIEWPORT["width"] - 1, VIEWPORT["height"] - 1)
        if not keep_focus:
            await page.evaluate(
                "() => { const e = document.activeElement; if (e && e !== document.body) e.blur(); }"
            )
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.evaluate("document.fonts.ready.then(() => true)")
        for selector in (".q-notification", ".q-spinner", ".q-inner-loading", ".q-loading"):
            await page.locator(selector).first.wait_for(state="detached", timeout=20_000)
        await page.wait_for_load_state("networkidle")
        # Dois quadros de animação: o Vue e o Quasar terminam de desenhar.
        await page.evaluate(
            "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"
        )

    async def check(self, page: Page, file: str) -> None:
        """Nada de erro na tela, nem um segredo no HTML da página."""
        for text in ("Algo deu errado", "Página não encontrada", "Connection lost"):
            if await page.get_by_text(text).filter(visible=True).count():
                raise RuntimeError(f"{file}: a página mostra '{text}'")
        if await page.locator(".q-notification").count():
            raise RuntimeError(f"{file}: uma notificação ficou na tela")
        content = await page.content()
        for value in self.forbidden:
            if value in content:
                raise RuntimeError(f"{file}: um segredo apareceria na captura")

    async def shoot(
        self,
        page: Page,
        file: str,
        caption: str,
        persona: str,
        url: str,
        *,
        full_page: bool = False,
        clip: FloatRect | None = None,
        keep_focus: bool = False,
    ) -> None:
        await self.settle(page, keep_focus=keep_focus)
        await self.check(page, file)
        await page.screenshot(
            path=self.out / file,
            full_page=full_page,
            clip=clip,
            animations="disabled",
            caret="hide",
        )
        self.shots.append(Shot(file, caption, persona, url, full_page))
        log(f"  {file}")

    async def dump(self, folder: Path) -> None:
        """Na falha: a tela de cada página aberta, para ver o que deu errado."""
        for line in self.console[-30:]:
            log(f"  console: {line}")
        number = 0
        for context in self.contexts:
            for page in context.pages:
                number += 1
                with contextlib.suppress(Exception):
                    await page.screenshot(path=folder / f"falha-{number:02d}.png", full_page=True)
                    log(f"  falha-{number:02d}.png: {page.url}")

    async def close(self) -> None:
        for context in self.contexts:
            with contextlib.suppress(Exception):
                await context.close()


async def _choose(page: Page, label: str, option: str) -> None:
    """Escolhe a opção ``option`` (pelo texto) do seletor do Quasar com o rótulo ``label``."""
    title = page.locator(".q-field__label", has_text=re.compile(f"^{re.escape(label)}$"))
    await page.locator(".q-select").filter(has=title).first.click()
    await page.get_by_role("option", name=option, exact=True).click()
    await page.get_by_role("option").first.wait_for(state="detached")


async def _pretty_json(page: Page) -> None:
    """Liga o "Pretty-print" do visualizador de JSON do Chromium.

    A caixa fica num shadow DOM fechado, então o clique é no rótulo, pela posição.
    """
    await page.locator("pre").first.wait_for()
    await page.mouse.click(40, 10)
    await page.wait_for_function("() => document.querySelector('pre').innerText.includes('\\n  ')")


async def _canvas_point(canvas: Locator, x: float, y: float) -> tuple[float, float]:
    box = await canvas.bounding_box()
    size = await canvas.evaluate("c => [c.width, c.height]")
    if box is None:
        raise RuntimeError("a tela ao vivo não está visível")
    return box["x"] + x * box["width"] / size[0], box["y"] + y * box["height"] / size[1]


class LiveDriver:
    """Faz a tarefa na tela ao vivo como a pessoa faria: mouse e teclado sobre o canvas.

    Um navegador do mesmo tamanho da tela remota (sem nenhum evento no outro) só mede
    onde ficam os campos, como no teste de ponta a ponta da gravação.
    """

    def __init__(self, page: Page, canvas: Locator) -> None:
        self.page = page
        self.canvas = canvas

    async def click(self, target: Locator) -> None:
        box = await target.bounding_box()
        if box is None:
            raise RuntimeError("elemento sem posição no medidor")
        x, y = await _canvas_point(
            self.canvas, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        )
        await self.page.mouse.click(x, y)
        await asyncio.sleep(0.4)

    async def type(self, text: str) -> None:
        await self.page.keyboard.type(text, delay=40)
        await asyncio.sleep(0.4)

    async def wait_address(self, suffix: str) -> None:
        address = self.page.get_by_label("Endereço da página")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (await address.input_value()).endswith(suffix):
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"a tela ao vivo não chegou em {suffix}")


_PAINTED = """c => {
  if (!c.width || !c.height) return false;
  const d = c.getContext('2d').getImageData(Math.floor(c.width / 2), Math.floor(c.height / 4), 1, 1).data;
  return d[3] > 0 && (d[0] + d[1] + d[2]) > 600;
}"""


async def record_live(camera: Camera, api: Api, demo: Demo, company: Company, v1: Portal) -> None:
    """05 e 06: a gravação ao vivo no portal v1 e a revisão, pela interface."""
    page = await camera.as_person(BRUNO)
    await camera.open(page, "/gravar", "Nova gravação")
    await _choose(page, "Área", "Fiscal")
    await page.get_by_label("Endereço inicial").fill(f"{v1.base_url}/login")
    await page.get_by_label("Nome da tarefa (opcional)").fill("Emitir protocolo no portal")
    await page.get_by_role("button", name="Começar a gravar").click()
    await page.wait_for_url(re.compile(r".*/gravar/[0-9a-f-]{36}$"))
    demo.session_id = page.url.rsplit("/", 1)[-1]
    session = await api.call(BRUNO, "GET", f"/v1/sessoes/{demo.session_id}")
    if session["area_id"] != str(company.fiscal):
        raise RuntimeError("a gravação abriu fora da área Fiscal")
    await page.locator("#rapa-ao-vivo").wait_for(state="attached")
    try:
        # A página da gravação tem de começar sozinha, como para qualquer pessoa.
        await page.wait_for_function("() => window.RAPA_AO_VIVO !== undefined", timeout=15_000)
    except PlaywrightTimeout:
        raise RuntimeError("a visão ao vivo não começou (o script da página não rodou)") from None
    canvas = page.locator('canvas[aria-label="Tela do navegador da gravação"]')
    await canvas.wait_for()
    await until(lambda: canvas.evaluate(_PAINTED), "a primeira imagem da tela ao vivo", 60)
    # O tamanho da tela remota acompanha o da página (o pedido sai 300 ms depois de conectar).
    await asyncio.sleep(2)
    await until(lambda: canvas.evaluate(_PAINTED), "a tela ao vivo no tamanho novo", 30)
    width, height = await canvas.evaluate("c => [c.width, c.height]")
    measure_context = await camera.browser.new_context(
        viewport={"width": width, "height": height}, locale="pt-BR"
    )
    try:
        measure = await measure_context.new_page()
        live = LiveDriver(page, canvas)
        await live.wait_address("/login")
        await measure.goto(f"{v1.base_url}/login")
        await live.click(measure.get_by_label("Usuário"))
        await live.type(DEFAULT_USER)
        await live.click(measure.get_by_label("Senha"))
        await live.type(DEFAULT_PASSWORD)
        await live.click(measure.get_by_role("button", name="Entrar"))
        await live.wait_address("/login/codigo")
        await measure.get_by_label("Usuário").fill(DEFAULT_USER)
        await measure.get_by_label("Senha").fill(DEFAULT_PASSWORD)
        await measure.get_by_role("button", name="Entrar").click()
        await measure.wait_for_url("**/login/codigo")
        await live.click(measure.get_by_label("Código de verificação"))
        await live.type(current_totp())
        await page.keyboard.press("Enter")
        await live.wait_address("/inicio")
        await measure.get_by_label("Código de verificação").fill(current_totp())
        await measure.get_by_role("button", name="Confirmar").click()
        await measure.wait_for_url("**/inicio")
        await live.click(measure.get_by_role("link", name="Emitir protocolo"))
        await live.wait_address("/protocolo")
        await measure.goto(f"{v1.base_url}/protocolo")
        await live.click(measure.get_by_label("Nome completo"))
        await live.type("Olívia Campos")
        await live.click(measure.get_by_label("CPF"))
        await live.type("39053344705")
        await live.click(measure.get_by_text("Declaro que as informações são verdadeiras"))
        await asyncio.sleep(1.5)  # o próximo quadro da tela remota
        await camera.shoot(
            page,
            "05-gravar-ao-vivo.png",
            "Gravação ao vivo: o navegador da nuvem mostra o portal de teste v1 e a pessoa "
            "preenche o formulário com o próprio mouse e teclado.",
            BRUNO.label,
            "/gravar/{sessao}",
            full_page=True,
            # O contorno azul do foco mostra onde fica a tela do navegador da nuvem.
            keep_focus=True,
        )
        before = len(v1.protocols)
        await live.click(measure.get_by_role("button", name="Emitir"))

        async def issued() -> bool:
            return len(v1.protocols) > before

        await until(issued, "o protocolo da gravação", 30)
    finally:
        await measure_context.close()
    await asyncio.sleep(1)
    await page.get_by_role("button", name="Parar e revisar").click()
    await page.wait_for_url("**/revisar")
    inputs_title = page.get_by_text("Entradas do robô", exact=True)
    await inputs_title.wait_for()
    await page.get_by_label("Nome técnico").fill("emitir_protocolo_gravado")
    await page.get_by_label("Título", exact=True).first.fill("Emitir protocolo (gravado)")
    await page.get_by_label("Descrição", exact=True).fill(
        "Emite um protocolo no portal para a pessoa informada (robô gravado pela interface)."
    )
    parameters = page.locator(".q-card").filter(has=inputs_title)
    accept = parameters.get_by_role("button", name="Aceitar")
    for index in range(await accept.count()):
        await accept.nth(index).click()
    await camera.shoot(
        page,
        "06-revisao-gravacao.png",
        "Revisão da gravação: os dados do robô, as entradas propostas pelas regras (aceitas "
        "aqui) e os nomes dos passos, para conferir antes de criar o rascunho; sem o hub de "
        "LLM, um aviso no topo.",
        BRUNO.label,
        "/gravar/{sessao}/revisar",
        full_page=True,
    )
    await page.get_by_role("button", name="Criar rascunho").click()
    await page.wait_for_url(re.compile(r".*/robos/[0-9a-f-]{36}\?versao=\d+$"))
    await page.get_by_text("Marcar como validada").wait_for()


async def capture_all(camera: Camera, api: Api, demo: Demo, company: Company, v1: Portal) -> None:
    base = camera.base_url

    # -- sem login -------------------------------------------------------------------
    page = await (await camera.context()).new_page()
    await camera.open(page, "/entrar", "Acesso de desenvolvimento", GABRIELA.subject)
    await camera.shoot(
        page,
        "01-entrar.png",
        "Tela de entrada no modo de desenvolvimento: um perfil por pessoa do arquivo de "
        "tokens (em produção, o login é pelo Keycloak).",
        "ninguém (antes do login)",
        "/entrar",
    )

    # -- Bruno: gravar, consertar e os robôs -------------------------------------------------
    await record_live(camera, api, demo, company, v1)
    page = await camera.as_person(BRUNO)
    await camera.open(page, "/gravar", "Suas gravações", "Rascunho criado")
    await page.get_by_label("Endereço inicial").fill(f"{v1.base_url}/login")
    await page.get_by_label("Nome da tarefa (opcional)").fill("Baixar as notas do mês")
    await page.get_by_label("Endereço inicial").blur()
    await camera.shoot(
        page,
        "16-gravar.png",
        "Gravar: a área, o endereço inicial e o nome da tarefa para abrir o navegador da "
        "nuvem, e as gravações anteriores da pessoa.",
        BRUNO.label,
        "/gravar",
    )
    failed = demo.runs["falha"]
    await camera.open(page, f"/execucoes/{failed}/conserto", "Candidatos", "Escolha o elemento")
    await page.locator(".q-radio").first.click()
    await camera.shoot(
        page,
        "09-conserto.png",
        "Consertar este passo: o passo que quebrou, a sugestão do modelo (desligada sem o "
        "hub de LLM) e os elementos candidatos da página na hora da falha.",
        BRUNO.label,
        "/execucoes/{execucao}/conserto",
        full_page=True,
    )
    await page.get_by_role("button", name="Criar a versão consertada").click()
    await page.get_by_text("criada como rascunho").wait_for()
    robot = demo.robots["verificar"]
    await api.call(BRUNO, "POST", f"/v1/robos/{robot}/versoes/2/validar")
    await camera.open(page, "/robos", "Nova versão de robô", "Emitir protocolo (gravado)")
    await camera.shoot(
        page,
        "03-robos.png",
        "Robôs: os robôs das áreas da pessoa, com a situação e a versão publicada, e o "
        "cartão para criar uma versão a partir do manifesto em JSON.",
        BRUNO.label,
        "/robos",
        full_page=True,
    )

    # -- quem aprova ---------------------------------------------------------------------
    page = await camera.as_person(DANIEL)
    await camera.open(
        page, f"/robos/{demo.robots['a1']}", "1 de 2 aprovações", "Certificado digital A1"
    )
    await camera.shoot(
        page,
        "04-robo-versoes-aprovacoes.png",
        "Tela do robô com certificado A1: versões, 1 de 2 aprovações, o cartão do A1 com a "
        "autorização da custodiante, os passos, as entradas e as credenciais.",
        DANIEL.label,
        "/robos/{robo}",
        full_page=True,
    )
    page = await camera.as_person(CARLA)
    await camera.open(page, f"/robos/{robot}?versao=2", "Mudanças em relação à versão 1")
    await camera.shoot(
        page,
        "21-robo-conserto-versao-nova.png",
        "A versão consertada pronta para publicar: só o alvo do passo que quebrou mudou, e "
        "outra pessoa, com o papel de aprovação, publica.",
        CARLA.label,
        "/robos/{robo}?versao=2",
        full_page=True,
    )

    # -- Eduarda: operação ----------------------------------------------------------------
    page = await camera.as_person(EDUARDA)
    await camera.open(page, "/catalogo", "Catálogo de robôs", "Baixar notas fiscais")
    await camera.shoot(
        page,
        "02-catalogo.png",
        "Catálogo: os robôs publicados que a pessoa pode usar, com o efeito, a versão e os "
        "botões Executar e Ver passos.",
        EDUARDA.label,
        "/catalogo",
        full_page=True,
    )
    await camera.open(page, "/executar/emitir_protocolo", "Executar: Emitir protocolo")
    await page.get_by_label("Nome completo *").fill("Nelson Pires")
    await page.get_by_label("CPF *").press_sequentially("52998224725")
    await page.get_by_label("UF *").fill("SC")
    await page.get_by_text("Entendo que esta execução").click()
    await page.get_by_role("button", name="Executar", exact=True).click()
    await page.get_by_text("Número:").wait_for()
    await page.get_by_text("Concluída", exact=True).wait_for(timeout=120_000)
    await page.get_by_text("Resultado").wait_for()
    await camera.shoot(
        page,
        "17-executar.png",
        "Executar pela interface: o formulário das entradas, a confirmação do passo "
        "irreversível e o resultado da execução, atualizado até terminar.",
        EDUARDA.label,
        "/executar/emitir_protocolo",
        full_page=True,
    )
    await camera.open(page, "/execucoes", "Abrir execução pelo número", "Verificar o acesso")
    await camera.shoot(
        page,
        "07-execucoes.png",
        "Execuções: as mais recentes das áreas da pessoa, de todas as origens (interface, "
        "API REST, MCP, agenda, webhook e lote).",
        EDUARDA.label,
        "/execucoes",
        full_page=True,
    )
    await camera.open(page, f"/execucoes/{failed}", "Evidências", "Vídeo da falha")
    video = page.locator("video").first
    # O vídeo roda até o fim (sem som): a tela mostra o último quadro, o da falha.
    ended = await video.evaluate(
        """v => new Promise(resolve => {
          if (v.ended) return resolve(true);
          v.addEventListener('ended', () => resolve(true), { once: true });
          setTimeout(() => resolve(false), 30000);
          v.muted = true;
          v.play().catch(() => resolve(false));
        })"""
    )
    if not ended:
        raise RuntimeError("o vídeo da falha não tocou até o fim")
    await camera.shoot(
        page,
        "08-execucao-evidencias.png",
        "Execução que falhou: o erro, o vídeo da falha, as telas guardadas e o botão "
        "Consertar este passo.",
        EDUARDA.label,
        "/execucoes/{execucao}",
        full_page=True,
    )
    await camera.open(page, f"/execucoes/{demo.runs['notas']}", "Resultado", "Arquivos")
    await camera.shoot(
        page,
        "18-execucao-sucesso.png",
        "Execução que deu certo: os detalhes, a tabela lida no portal e os arquivos "
        "baixados, com tamanho e data.",
        EDUARDA.label,
        "/execucoes/{execucao}",
        full_page=True,
    )
    await camera.open(page, "/agendas", "Notas fiscais toda manhã", "Nova agenda")
    await camera.shoot(
        page,
        "10-agendas.png",
        "Agendas: as agendas cron (ligada e desligada) e o formulário de uma nova, com "
        "atalhos e o fuso de Brasília.",
        EDUARDA.label,
        "/agendas",
        full_page=True,
    )
    await camera.open(page, f"/agendas/{demo.schedule_id}", "Ocorrências", "virou execução")
    await camera.shoot(
        page,
        "19-agenda-detalhe.png",
        "Uma agenda: o robô, a expressão, a próxima ocorrência e o que aconteceu com cada "
        "ocorrência.",
        EDUARDA.label,
        "/agendas/{agenda}",
    )
    await camera.open(page, "/webhooks", "Pedidos do ERP", "Novo webhook")
    await camera.shoot(
        page,
        "11-webhooks.png",
        "Webhooks: o endereço de cada um, o limite por minuto e a última entrega; a chave "
        "de assinatura só aparece uma vez, ao criar ou trocar.",
        EDUARDA.label,
        "/webhooks",
        full_page=True,
    )
    await camera.open(page, "/lotes", ARQUIVO_LOTE, "Novo lote")
    await camera.shoot(
        page,
        "12-lotes.png",
        "Lotes: os lotes enviados, com o andamento, e o envio de uma planilha .xlsx ou .csv "
        "(conferida linha a linha antes de criar).",
        EDUARDA.label,
        "/lotes",
    )
    await camera.open(page, f"/lotes/{demo.batch_id}", "Todas as execuções do lote terminaram")
    await camera.shoot(
        page,
        "20-lote-detalhe.png",
        "Um lote: o andamento, cada linha com a execução e o resultado, e o resultado para "
        "baixar em CSV ou xlsx.",
        EDUARDA.label,
        "/lotes/{lote}",
    )

    # -- Ana e Fábio: credenciais e empresa ----------------------------------------------
    page = await camera.as_person(ANA)
    await camera.open(page, "/credenciais", "Credenciais registradas", "Registrar credencial")
    await camera.shoot(
        page,
        "13-credenciais-a1.png",
        "Credenciais: só a referência ao cofre e os metadados; o certificado A1 com "
        "custodiante, validade e impressão, e o registro de uma credencial nova.",
        ANA.label,
        "/credenciais",
        full_page=True,
    )
    await camera.open(page, "/empresa", "Usuários e papéis", "Consumo de")
    await camera.shoot(
        page,
        "14-empresa-usuarios.png",
        "Empresa (administração): as pessoas com os papéis por área ou na empresa inteira, "
        "as áreas e o consumo do mês.",
        ANA.label,
        "/empresa",
        full_page=True,
    )
    page = await camera.as_person(FABIO)
    await camera.open(page, "/empresa", "Consumo de", "Total:")
    await camera.shoot(
        page,
        "15-empresa-consumo.png",
        "Empresa (auditoria): o consumo do mês por área e por origem, em execuções, minutos "
        "de navegador, sessões de gravação e lotes.",
        FABIO.label,
        "/empresa",
    )

    # -- o próprio serviço -----------------------------------------------------------------
    page = await (await camera.context()).new_page()
    await page.goto(f"{base}/saude")
    await _pretty_json(page)
    await camera.shoot(
        page,
        "22-saude.png",
        "GET /saude: 200 com a versão enquanto o despachante do processo está de pé (503 "
        "quando ele para).",
        "ninguém (sem token)",
        "/saude",
        clip={"x": 0, "y": 0, "width": 720, "height": 100},
    )
    context = await camera.context(extra_http_headers=api.headers(EDUARDA))
    page = await context.new_page()
    await page.goto(f"{base}/v1/openapi-robos.json")
    await _pretty_json(page)
    await camera.shoot(
        page,
        "23-openapi-robos.png",
        "GET /v1/openapi-robos.json: o OpenAPI dos robôs publicados que a pessoa do token "
        "pode chamar pelo REST (o começo do documento).",
        EDUARDA.label + " (token no cabeçalho Authorization)",
        "/v1/openapi-robos.json",
    )
    page = await (await camera.context()).new_page()
    await page.goto(f"{base}/v1/docs")
    await page.locator(".swagger-ui .opblock").first.wait_for()
    await camera.shoot(
        page,
        "24-docs-api.png",
        "GET /v1/docs (com RAPA_DOCS_API=1): a documentação da API REST no Swagger UI, "
        "servido pelo próprio RaPA (sem CDN); a página abre sem token, e o botão Authorize "
        "recebe o token que as chamadas feitas por ela (Try it out) mandam.",
        "ninguém (sem token)",
        "/v1/docs",
    )


def write_index(out: Path, shots: Sequence[Shot]) -> None:
    data = {
        "descricao": (
            "Capturas de tela do RaPA para o manual, geradas por docs/manual/capturar.py "
            f"sobre a '{EMPRESA}' (dados inventados), em 1440x900 com escala 2."
        ),
        "capturas": [
            {
                "arquivo": shot.file,
                "legenda": shot.caption,
                "persona": shot.persona,
                "url": shot.url,
                "pagina_inteira": shot.full_page,
            }
            for shot in sorted(shots, key=lambda s: s.file)
        ],
        "pessoas": [
            {"nome": person.name, "sujeito": person.subject, "papeis": person.description}
            for person in PESSOAS
        ],
    }
    (out / "capturas.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def publish_images(staging: Path, target: Path) -> None:
    """Troca as imagens antigas pelas novas só no fim, com todas as capturas prontas."""
    target.mkdir(parents=True, exist_ok=True)
    for old in [*target.glob("[0-9][0-9]-*.png"), target / "capturas.json"]:
        old.unlink(missing_ok=True)
    for new in sorted(staging.iterdir()):
        shutil.copy2(new, target / new.name)


# ---------------------------------------------------------------------------
# O roteiro
# ---------------------------------------------------------------------------


async def run_demo(
    service: Service,
    tokens: Mapping[str, str],
    company: Company,
    db: Database,
    v1: Portal,
    v2: Portal,
    chromium: str | None,
    staging: Path,
    forbidden: list[str],
    debug: Path | None,
) -> list[Shot]:
    health = await wait_healthy(service)
    log(f"serviço de pé em {service.base_url} (versão {health.get('versao')})")
    async with httpx2.AsyncClient(base_url=service.base_url, trust_env=False, timeout=60) as client:
        api = Api(client, tokens)
        demo = await build_demo(api, company, v1, v2, db, service.base_url)
        forbidden += demo.secrets
        log("capturas")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=chromium, args=[HOST_RESOLVER_ARG]
            )
            camera = Camera(browser, service.base_url, staging, forbidden)
            try:
                await capture_all(camera, api, demo, company, v1)
            except BaseException:
                if debug is not None:
                    await camera.dump(debug)
                raise
            finally:
                await camera.close()
                await browser.close()
        return camera.shots


def _raise_on_signal(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docs/manual/capturar.py",
        description="Sobe o RaPA com dados de demonstração e captura as telas para o manual.",
    )
    parser.add_argument(
        "--saida", type=Path, default=REPO / "docs" / "manual" / "img", help="pasta das imagens"
    )
    parser.add_argument("--porta", type=int, default=PORTA_RAPA, help="porta do serviço (se livre)")
    parser.add_argument("--chromium", default=None, help="caminho do Chromium (completo)")
    parser.add_argument(
        "--depurar",
        type=Path,
        default=None,
        help="numa falha, guarda aqui a tela de cada página aberta e o log do serviço",
    )
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _raise_on_signal)
    if args.depurar is not None:
        args.depurar.mkdir(parents=True, exist_ok=True)
    chromium = find_chromium(args.chromium)
    work = Path(tempfile.mkdtemp(prefix="rapa-manual-"))
    try:
        with contextlib.ExitStack() as stack:
            log("PostgreSQL temporário")
            cluster = stack.enter_context(pg_cluster())
            db = create_database(cluster)
            log(f"empresa '{EMPRESA}' pela linha de comando")
            company = create_company(db)
            v1 = stack.enter_context(run_portal("v1"))
            v2 = stack.enter_context(run_portal("v2"))
            log(f"portais de teste em {v1.base_url} e {v2.base_url}")
            tokens = {person.key: f"token-{secrets.token_urlsafe(24)}" for person in PESSOAS}
            tokens_file = work / "tokens.json"
            tokens_file.write_text(
                json.dumps(
                    {
                        tokens[p.key]: {"sujeito": p.subject, "empresa": str(company.company_id)}
                        for p in PESSOAS
                    }
                ),
                encoding="utf-8",
            )
            login = {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD}
            otp = {"totp_secret": DEFAULT_TOTP_SECRET}
            secrets_file = work / "segredos.json"
            secrets_file.write_text(
                json.dumps(
                    {
                        str(company.company_id): {
                            "portal": login,
                            "portal_otp": otp,
                            "portal_v1": login,
                            "portal_v1_otp": otp,
                        }
                    }
                ),
                encoding="utf-8",
            )
            for path in (tokens_file, secrets_file):
                path.chmod(0o600)
            webhook_key = secrets.token_hex(32)
            port = _free_port(args.porta)
            env = {
                "RAPA_DATABASE_URL": db.app_url,
                "RAPA_DISPATCHER_DATABASE_URL": db.dispatcher_url,
                "RAPA_DEV": "1",
                "RAPA_COOKIE_SEGURO": "0",
                "RAPA_TOKENS_DEV": str(tokens_file),
                "RAPA_SEGREDOS_DEV": str(secrets_file),
                "RAPA_CHAVE_WEBHOOKS": webhook_key,
                "RAPA_DOCS_API": "1",
                "RAPA_PUBLIC_URL": f"http://127.0.0.1:{port}",
                "RAPA_ARGS_NAVEGADOR": shlex.quote(HOST_RESOLVER_ARG),
                "RAPA_VIDEO_EM_FALHAS": "1",
                "RAPA_AGENDA_INTERVALO_S": "1",
                "RAPA_LIMITE_POR_SEGUNDO": "0",
                "RAPA_PASTA_TRABALHO": str(work / "trabalho"),
                "RAPA_PASTA_ARTEFATOS": str(work / "artefatos"),
                "RAPA_PASTA_SESSOES": str(work / "sessoes"),
                "RAPA_PASTA_ARQUIVOS": str(work / "arquivos"),
            }
            if chromium:
                env["RAPA_CHROMIUM_PATH"] = chromium
            if browsers := ffmpeg_browsers_path(work / "navegadores"):
                env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
            log(f"serviço: python -m rapa.server --port {port}")
            service = stack.enter_context(run_service(env, port, work / "servidor.log"))
            staging = work / "capturas"
            staging.mkdir()
            forbidden = [
                *tokens.values(),
                webhook_key,
                DEFAULT_PASSWORD,
                DEFAULT_TOTP_SECRET,
                *db.passwords.values(),
                cluster.password,
            ]
            try:
                shots = asyncio.run(
                    run_demo(
                        service,
                        tokens,
                        company,
                        db,
                        v1,
                        v2,
                        chromium,
                        staging,
                        forbidden,
                        args.depurar,
                    )
                )
            except BaseException:
                log(f"o roteiro parou; o fim do log do serviço:\n{service.tail()}")
                if args.depurar is not None:
                    shutil.copy2(service.log_path, args.depurar / "servidor.log")
                    for done in staging.glob("*.png"):
                        shutil.copy2(done, args.depurar / done.name)
                raise
            write_index(staging, shots)
            publish_images(staging, args.saida)
            log(f"{len(shots)} capturas em {args.saida}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
