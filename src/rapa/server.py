"""O RaPA num processo só: REST, MCP, interface e despachante sobre o PostgreSQL.

Uso::

    python -m rapa.server

A configuração vem de variáveis de ambiente (veja :meth:`ServerSettings.from_env`).
Em produção, o despachante roda em réplicas próprias (``RAPA_DESPACHANTE=1`` e
``RAPA_HTTP=0``) e o lançador local dá lugar aos pods de uso único; na Fase 0 e
em desenvolvimento, tudo cabe num processo.

O esquema do banco é criado antes, com ``python -m rapa.control.db.migrate``.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shlex
import sys
import tempfile
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from rapa.control.auth import (
    Authenticator,
    AuthError,
    BearerVerifier,
    DevTokenVerifier,
    JwtSettings,
    TokenVerifier,
)
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.launcher import (
    LocalLauncherSettings,
    LocalProcessLauncher,
    NoSecrets,
    SecretSource,
    StaticSecretSource,
)
from rapa.control.services import Dispatcher, PgPlatform, PgPrincipalResolver, PgRunQueue
from rapa.control.services.queue import DEFAULT_MAX_DURATION
from rapa.control.services.sessions import PgSessionStore
from rapa.control.sessions import (
    GeneralizationAssistant,
    HubAssistant,
    LocalSessionLauncher,
    LocalSessionSettings,
    RecordingSessions,
    SessionLimits,
)
from rapa.core.contracts import Principal

__all__ = [
    "AuthenticatorLoginResolver",
    "DispatcherSupervisor",
    "ServerSettings",
    "build_sessions",
    "create_server_app",
    "dispatcher_pool_size",
    "main",
]

logger = logging.getLogger("rapa.server")

_TRUE = {"1", "true", "sim", "yes", "on"}
_FALSE = {"0", "false", "nao", "não", "no", "off", ""}


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} precisa ser 1 ou 0")


def _path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    return Path(raw) if raw else None


class ServerSettings(BaseModel):
    """Configuração do serviço."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Banco com o papel da aplicação (``rapa_app``, sujeito ao isolamento por linha).
    database_url: str = Field(min_length=1, repr=False)
    #: Banco com o papel do despachante; sem ele, este processo não despacha.
    dispatcher_database_url: str | None = Field(default=None, repr=False)
    http: bool = True
    ui: bool = True
    public_url: str | None = None
    #: Modo de desenvolvimento: tokens fixos, login sem senha e segredos em arquivo.
    dev: bool = False
    dev_tokens_file: Path | None = None
    dev_secrets_file: Path | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_url: str | None = None
    organization_aliases: dict[str, UUID] = Field(default_factory=dict)
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    oidc_redirect_uri: str | None = None
    cookie_secure: bool = True
    mcp_allowed_origins: frozenset[str] = frozenset()
    rate_limit_per_s: float | None = Field(default=20.0, gt=0)
    concurrency: int = Field(default=4, ge=1, le=256)
    poll_interval_s: float = Field(default=1.0, gt=0)
    work_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "rapa-trabalho")
    artifacts_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "rapa-artefatos"
    )
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    #: Arquivos de entrada, numa subpasta por empresa (``<pasta>/<uuid da empresa>/``).
    files_dir: Path | None = None
    #: Sessões de gravação (o navegador na nuvem da demonstração) neste processo.
    sessions: bool = True
    sessions_per_company: int = Field(default=2, ge=1, le=1000)
    #: Sessões abertas ao mesmo tempo neste processo; None usa a reserva interativa.
    sessions_max: int | None = Field(default=None, ge=1, le=10_000)
    session_idle_s: float = Field(default=600.0, gt=0, le=86_400)
    session_max_s: float = Field(default=3600.0, gt=0, le=86_400)
    #: Pasta dos perfis temporários dos navegadores das sessões (apagados no fim de cada uma).
    sessions_dir: Path | None = None
    #: Hub de LLM para as propostas da revisão (sem ele, só as sugestões das regras).
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_capabilities_file: Path | None = None

    @model_validator(mode="after")
    def _check(self) -> ServerSettings:
        if not self.http and self.dispatcher_database_url is None:
            raise ValueError("sem HTTP e sem despachante, o processo não teria o que fazer")
        jwt = (self.jwt_issuer, self.jwt_audience, self.jwks_url)
        if any(jwt) and not all(jwt):
            raise ValueError("para tokens do Keycloak, informe emissor, público e URL das chaves")
        if self.http and not all(jwt) and not (self.dev and self.dev_tokens_file):
            raise ValueError(
                "configure a verificação de tokens do Keycloak (RAPA_JWT_*) ou, só em "
                "desenvolvimento, RAPA_DEV=1 com RAPA_TOKENS_DEV"
            )
        if (self.dev_tokens_file or self.dev_secrets_file) and not self.dev:
            raise ValueError("tokens e segredos em arquivo só valem com RAPA_DEV=1")
        llm = (self.llm_base_url, self.llm_model)
        if any(llm) and not all(llm):
            raise ValueError("para o hub de LLM, informe o endereço e o modelo (RAPA_LLM_*)")
        if self.llm_base_url and self.llm_capabilities_file is None:
            raise ValueError(
                "para usar o hub de LLM, informe a matriz da sonda em RAPA_LLM_CAPACIDADES"
            )
        if self.session_idle_s > self.session_max_s:
            raise ValueError("o tempo sem atividade de uma sessão não pode passar do prazo máximo")
        oidc = (self.oidc_client_id, self.oidc_redirect_uri)
        if any(oidc) and not all(oidc):
            raise ValueError("para o login OIDC, informe o cliente e o endereço de retorno")
        if self.ui and self.http and self.oidc_client_id and not self.jwt_issuer:
            raise ValueError("o login OIDC da interface precisa do emissor (RAPA_JWT_ISSUER)")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        """Lê a configuração das variáveis ``RAPA_*``.

        - ``RAPA_DATABASE_URL`` (obrigatória) e ``RAPA_DISPATCHER_DATABASE_URL``;
        - ``RAPA_HTTP``, ``RAPA_UI`` (1 ou 0) e ``RAPA_PUBLIC_URL``;
        - ``RAPA_JWT_ISSUER``, ``RAPA_JWT_AUDIENCE``, ``RAPA_JWKS_URL`` e
          ``RAPA_ORGANIZATION_ALIASES`` (JSON ``{"alias": "uuid"}``);
        - ``RAPA_OIDC_CLIENT_ID``, ``RAPA_OIDC_CLIENT_SECRET``, ``RAPA_OIDC_REDIRECT_URI``;
        - ``RAPA_DEV``, ``RAPA_TOKENS_DEV`` (JSON ``{"token": {"sujeito": ..., "empresa": ...}}``)
          e ``RAPA_SEGREDOS_DEV`` (JSON ``{"uuid da empresa": {"credencial": {...}}}``);
        - ``RAPA_CONCORRENCIA``, ``RAPA_PASTA_TRABALHO``, ``RAPA_PASTA_ARTEFATOS``,
          ``RAPA_PASTA_ARQUIVOS``, ``RAPA_CHROMIUM_PATH`` e ``RAPA_ARGS_NAVEGADOR``
          (argumentos separados por espaço, com aspas se preciso). A pasta de
          arquivos de entrada tem uma subpasta por empresa:
          ``<RAPA_PASTA_ARQUIVOS>/<uuid da empresa>/<arquivo>``; cada execução só
          enxerga a subpasta da própria empresa, e arquivos soltos na raiz não valem;
        - ``RAPA_MCP_ORIGENS`` (separadas por vírgula), ``RAPA_LIMITE_POR_SEGUNDO``
          (0 desliga) e ``RAPA_COOKIE_SEGURO``;
        - sessões de gravação (a demonstração no navegador da nuvem): ``RAPA_SESSOES``
          (1 ou 0; liga por padrão), ``RAPA_SESSOES_POR_EMPRESA`` (abertas ao mesmo tempo
          por empresa; padrão 2), ``RAPA_SESSOES_MAX`` (abertas ao mesmo tempo neste
          processo; padrão: o tamanho da reserva interativa, 10% das vagas),
          ``RAPA_SESSAO_OCIOSA_S`` (segundos sem entrada até encerrar; padrão 600),
          ``RAPA_SESSAO_MAX_S`` (prazo máximo; padrão 3600) e ``RAPA_PASTA_SESSOES``
          (onde ficam os perfis temporários dos navegadores, apagados no fim). Os
          navegadores usam ``RAPA_CHROMIUM_PATH`` e ``RAPA_ARGS_NAVEGADOR``;
        - hub de LLM para as propostas da revisão: ``RAPA_LLM_BASE_URL``,
          ``RAPA_LLM_MODEL``, ``RAPA_LLM_API_KEY`` e ``RAPA_LLM_CAPACIDADES`` (a
          matriz gravada por ``python -m rapa.llm.probe --out``). Sem o hub, a revisão
          mostra só as sugestões das regras, com um aviso.
        """
        env = os.environ if env is None else env
        data: dict[str, Any] = {
            "database_url": env.get("RAPA_DATABASE_URL", ""),
            "dispatcher_database_url": env.get("RAPA_DISPATCHER_DATABASE_URL") or None,
            "http": _flag(env, "RAPA_HTTP", True),
            "ui": _flag(env, "RAPA_UI", True),
            "public_url": env.get("RAPA_PUBLIC_URL") or None,
            "dev": _flag(env, "RAPA_DEV", False),
            "dev_tokens_file": _path(env, "RAPA_TOKENS_DEV"),
            "dev_secrets_file": _path(env, "RAPA_SEGREDOS_DEV"),
            "jwt_issuer": env.get("RAPA_JWT_ISSUER") or None,
            "jwt_audience": env.get("RAPA_JWT_AUDIENCE") or None,
            "jwks_url": env.get("RAPA_JWKS_URL") or None,
            "oidc_client_id": env.get("RAPA_OIDC_CLIENT_ID") or None,
            "oidc_client_secret": env.get("RAPA_OIDC_CLIENT_SECRET") or None,
            "oidc_redirect_uri": env.get("RAPA_OIDC_REDIRECT_URI") or None,
            "cookie_secure": _flag(env, "RAPA_COOKIE_SEGURO", True),
            "chromium": env.get("RAPA_CHROMIUM_PATH") or None,
            "browser_args": tuple(shlex.split(env.get("RAPA_ARGS_NAVEGADOR", ""))),
            "files_dir": _path(env, "RAPA_PASTA_ARQUIVOS"),
            "sessions": _flag(env, "RAPA_SESSOES", True),
            "sessions_dir": _path(env, "RAPA_PASTA_SESSOES"),
            "llm_base_url": env.get("RAPA_LLM_BASE_URL") or None,
            "llm_model": env.get("RAPA_LLM_MODEL") or None,
            "llm_api_key": env.get("RAPA_LLM_API_KEY") or None,
            "llm_capabilities_file": _path(env, "RAPA_LLM_CAPACIDADES"),
        }
        for key, name, kind in (
            ("sessions_per_company", "RAPA_SESSOES_POR_EMPRESA", int),
            ("sessions_max", "RAPA_SESSOES_MAX", int),
            ("session_idle_s", "RAPA_SESSAO_OCIOSA_S", float),
            ("session_max_s", "RAPA_SESSAO_MAX_S", float),
        ):
            if raw := env.get(name, "").strip():
                data[key] = kind(raw)
        if aliases := env.get("RAPA_ORGANIZATION_ALIASES"):
            data["organization_aliases"] = json.loads(aliases)
        if origins := env.get("RAPA_MCP_ORIGENS"):
            data["mcp_allowed_origins"] = frozenset(
                o.strip() for o in origins.split(",") if o.strip()
            )
        if (rate := env.get("RAPA_LIMITE_POR_SEGUNDO")) is not None:
            data["rate_limit_per_s"] = float(rate) or None
        if concurrency := env.get("RAPA_CONCORRENCIA"):
            data["concurrency"] = int(concurrency)
        for key, name in (
            ("work_dir", "RAPA_PASTA_TRABALHO"),
            ("artifacts_dir", "RAPA_PASTA_ARTEFATOS"),
        ):
            if (value := _path(env, name)) is not None:
                data[key] = value
        return cls.model_validate(data)


def load_dev_tokens(path: Path) -> dict[str, tuple[str, UUID]]:
    """Lê ``{"token": {"sujeito": "...", "empresa": "uuid"}}``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("o arquivo de tokens precisa ser um objeto JSON com pelo menos um token")
    tokens: dict[str, tuple[str, UUID]] = {}
    for token, identity in raw.items():
        if not isinstance(identity, dict) or set(identity) != {"sujeito", "empresa"}:
            raise ValueError("cada token precisa de 'sujeito' e 'empresa'")
        tokens[str(token)] = (str(identity["sujeito"]), UUID(str(identity["empresa"])))
    return tokens


class AuthenticatorLoginResolver:
    """Resolvedor do login OIDC da interface sobre o autenticador da API.

    O token de acesso recebido no login passa pela mesma verificação dos
    pedidos REST e MCP, e os papéis vêm do banco.
    """

    def __init__(self, authenticator: Authenticator) -> None:
        self.authenticator = authenticator

    async def resolve(self, claims: Mapping[str, Any], access_token: str) -> Principal | None:
        try:
            return await self.authenticator.authenticate(f"Bearer {access_token}")
        except AuthError:
            return None


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Roda a corrotina até o fim, mesmo dentro de um laço de eventos já rodando."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _dev_profiles(
    database_url: str, tokens: Mapping[str, tuple[str, UUID]]
) -> list[tuple[str, Principal]]:
    db = Database.from_url(database_url)
    try:
        resolver = PgPrincipalResolver(db)
        out: list[tuple[str, Principal]] = []
        for token, (subject, company_id) in tokens.items():
            principal = await resolver.resolve(subject, company_id)
            if principal is None:
                logger.warning("token de desenvolvimento sem usuário ativo no banco; ignorado")
                continue
            out.append((token, principal))
        return out
    finally:
        await db.dispose()


def _secret_source(settings: ServerSettings) -> SecretSource:
    if settings.dev_secrets_file is not None:
        return StaticSecretSource.from_file(settings.dev_secrets_file)
    # O cofre (OpenBao) entra na Fase 1; até lá, robôs com credenciais falham sem digitar nada.
    return NoSecrets()


def _verifier(settings: ServerSettings) -> BearerVerifier:
    if settings.jwt_issuer and settings.jwt_audience and settings.jwks_url:
        return TokenVerifier(
            JwtSettings(
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                jwks_url=settings.jwks_url,
                organization_aliases=settings.organization_aliases,
            )
        )
    if settings.dev_tokens_file is None:  # pragma: no cover - barrado pela validação
        raise ValueError("sem verificação de tokens configurada")
    return DevTokenVerifier(load_dev_tokens(settings.dev_tokens_file))


def _mount_ui(
    app: FastAPI,
    settings: ServerSettings,
    platform: PgPlatform,
    authenticator: Authenticator,
    sessions: RecordingSessions | None = None,
) -> None:
    from rapa.ui.app import create_ui
    from rapa.ui.auth import DevLogin, DevProfile, OidcLogin, OidcSettings, UiAuth
    from rapa.ui.backend import ContractsBackendFactory

    dev_login = None
    if settings.dev and settings.dev_tokens_file is not None:
        profiles = _run_sync(
            _dev_profiles(settings.database_url, load_dev_tokens(settings.dev_tokens_file))
        )
        dev_login = DevLogin(
            DevProfile(
                key=f"perfil{index}",
                label=principal.subject,
                principal=principal,
                description="perfil de desenvolvimento",
                access_token=token,
            )
            for index, (token, principal) in enumerate(profiles, start=1)
        )
    oidc = None
    if settings.oidc_client_id and settings.oidc_redirect_uri and settings.jwt_issuer:
        oidc = OidcLogin(
            OidcSettings(
                issuer=settings.jwt_issuer,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                redirect_uri=settings.oidc_redirect_uri,
            ),
            AuthenticatorLoginResolver(authenticator),
        )
    if dev_login is None and oidc is None:
        raise ValueError("a interface precisa do login OIDC (RAPA_OIDC_*) ou do modo RAPA_DEV")
    create_ui(
        app,
        backend_factory=ContractsBackendFactory.from_platform(platform, sessions=sessions),
        # A interface age direto nos serviços, então a sessão confere de novo no banco
        # (conta ativa e papéis), como a API faz a cada pedido.
        auth=UiAuth(
            dev=dev_login,
            oidc=oidc,
            cookie_secure=settings.cookie_secure,
            principals=authenticator.resolver,
        ),
    )


def dispatcher_pool_size(concurrency: int) -> int:
    """Conexões do pool do banco do despachante para ``concurrency`` execuções juntas.

    Cada execução em andamento pode usar duas conexões ao mesmo tempo (a renovação
    da concessão e a gravação dos eventos ou do resultado), e o laço principal usa
    mais duas (pegar a próxima execução e a manutenção da fila). Com menos, as
    execuções disputam o pool e a renovação da concessão pode atrasar.
    """
    return 2 * concurrency + 2


#: Espera máxima pelas execuções em andamento no encerramento. Toda execução termina
#: sozinha antes disso: o prazo dela é de no máximo ``DEFAULT_MAX_DURATION``, o
#: lançador para o executor logo depois do prazo e o despachante insiste em gravar o
#: resultado por até 5 minutos além dele. Passar daqui é sinal de algo preso.
_DRAIN_TIMEOUT_S = DEFAULT_MAX_DURATION.total_seconds() + 600.0


class DispatcherSupervisor:
    """Mantém o despachante deste processo rodando e diz se ele está de pé.

    O laço do despachante já sobrevive aos erros de cada volta. Se mesmo assim ele
    parar sem ter sido pedido (um erro inesperado), o supervisor registra o tipo do
    erro e o reinicia depois de ``restart_delay_s``, espera que dobra a cada parada
    seguida, até ``max_restart_delay_s``. Reiniciar é seguro: as execuções em
    andamento continuam no mesmo :class:`Dispatcher`, que só volta a pegar novas.
    Enquanto o despachante não está rodando, :attr:`healthy` é falso e ``/saude``
    responde 503.

    ``drain_timeout_s`` é quanto o encerramento espera, depois de o laço parar, as
    execuções que ficaram em andamento (veja :func:`_stop_dispatcher`).
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        restart_delay_s: float = 1.0,
        max_restart_delay_s: float = 30.0,
        drain_timeout_s: float = _DRAIN_TIMEOUT_S,
    ) -> None:
        if restart_delay_s <= 0 or max_restart_delay_s < restart_delay_s:
            raise ValueError("as esperas para reiniciar o despachante precisam ser positivas")
        if drain_timeout_s <= 0:
            raise ValueError("a espera pelas execuções no encerramento precisa ser positiva")
        self.dispatcher = dispatcher
        self.restart_delay_s = restart_delay_s
        self.max_restart_delay_s = max_restart_delay_s
        self.drain_timeout_s = drain_timeout_s
        #: Quantas vezes o despachante parou sem pedido e foi reiniciado.
        self.restarts = 0
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def healthy(self) -> bool:
        """O despachante está rodando e nenhuma etapa do laço falha (ou trava) há tempo demais."""
        running = self._running and self._task is not None and not self._task.done()
        return running and self.dispatcher.healthy

    @property
    def problem(self) -> dict[str, Any]:
        """O que ``/saude`` mostra quando não está saudável: só nomes, nunca o texto do erro."""
        if not (self._running and self._task is not None and not self._task.done()):
            return {"estado": "despachante_parado"}
        detail: dict[str, Any] = {"estado": "despachante_com_falha"}
        if self.dispatcher.stalled:
            detail["volta_presa"] = True
        failing = sorted(self.dispatcher.failing)
        if failing:
            detail["etapas"] = failing
        return detail

    def start(self, stop: asyncio.Event) -> asyncio.Task[None]:
        """Começa a supervisionar; a tarefa termina quando ``stop`` é acionado.

        Só uma vez por supervisor: uma segunda chamada levanta ``RuntimeError``
        antes de criar qualquer tarefa.
        """
        if self._task is not None:
            raise RuntimeError("o despachante já foi iniciado")
        self._running = True
        self._task = asyncio.create_task(self._supervise(stop), name="despachante")
        self._task.add_done_callback(self._ended)
        return self._task

    async def _supervise(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        delay = self.restart_delay_s
        while True:
            self._running = True
            started = loop.time()
            error: str | None = None
            try:
                await self.dispatcher.run(stop)
            except Exception as exc:
                # Só o tipo: a mensagem de um erro inesperado pode trazer dados.
                error = type(exc).__name__
            finally:
                self._running = False
            if stop.is_set():
                if error is not None:
                    logger.error("o despachante falhou ao encerrar: %s", error)
                return
            reason = error or "parou sem pedido"
            if loop.time() - started >= self.max_restart_delay_s:
                delay = self.restart_delay_s  # rodou bem por um tempo: espera curta de novo
            self.restarts += 1
            logger.error("o despachante parou (%s); reiniciando em %.1f s", reason, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            if stop.is_set():
                return
            delay = min(delay * 2, self.max_restart_delay_s)

    def _ended(self, task: asyncio.Task[None]) -> None:
        self._running = False
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("a supervisão do despachante parou: %s", type(error).__name__)


async def _stop_dispatcher(
    supervisor: DispatcherSupervisor, stop: asyncio.Event, task: asyncio.Task[None]
) -> None:
    """Pede a parada e espera o despachante terminar o que está em andamento.

    Quando está rodando, o laço do despachante espera sozinho as execuções em
    andamento ao parar. Mas, se ele estava parado na espera para reiniciar, ou se a própria
    supervisão já tinha parado com erro (registrado em
    :meth:`DispatcherSupervisor._ended`), as execuções continuam no despachante e
    são esperadas aqui, antes de o ``lifespan`` fechar os bancos.

    Essa espera vai até ``drain_timeout_s``. O que ainda estiver em andamento é
    registrado (só a contagem) e interrompido: o lançador para o executor e nada é
    gravado; quando a concessão vencer, o recolhimento devolve a execução à fila
    se nenhum passo que grava tinha começado, a marca como falha se um passo que
    grava já tinha começado, ou como indeterminada se um passo irreversível já
    tinha começado.
    """
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    dispatcher = supervisor.dispatcher
    if dispatcher.in_flight == 0:
        return
    logger.warning(
        "o laço do despachante já tinha parado; esperando %d execução(ões) em andamento",
        dispatcher.in_flight,
    )
    draining = asyncio.ensure_future(dispatcher.drain())
    try:
        done, _ = await asyncio.wait({draining}, timeout=supervisor.drain_timeout_s)
        if not done:
            logger.error(
                "%d execução(ões) ainda em andamento depois de %g s; interrompidas sem "
                "gravar o resultado (o recolhimento decide quando a concessão vencer)",
                dispatcher.in_flight,
                supervisor.drain_timeout_s,
            )
    finally:
        if not draining.done():
            # Cancelar a espera cancela as execuções: o lançador para cada executor.
            draining.cancel()
            await asyncio.gather(draining, return_exceptions=True)


class _DispatcherHealth:
    """Faz ``/saude`` responder 503 enquanto o despachante deste processo estiver parado,
    com uma etapa falhando há tempo demais ou com uma volta do laço presa."""

    def __init__(self, app: ASGIApp, supervisor: DispatcherSupervisor) -> None:
        self.app = app
        self.supervisor = supervisor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/saude" and not self.supervisor.healthy:
            response = JSONResponse(
                self.supervisor.problem,
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_dispatcher(settings: ServerSettings, db: Database) -> Dispatcher:
    """Despachante com o lançador local, sobre o banco do papel do despachante."""
    queue = PgRunQueue(db)
    launcher = LocalProcessLauncher(
        queue,
        LocalLauncherSettings(
            work_dir=settings.work_dir,
            artifacts_dir=settings.artifacts_dir,
            chromium=settings.chromium,
            browser_args=settings.browser_args,
            files_dir=settings.files_dir,
        ),
        _secret_source(settings),
    )
    return Dispatcher(
        queue, launcher, concurrency=settings.concurrency, poll_interval=settings.poll_interval_s
    )


def _assistant(settings: ServerSettings) -> GeneralizationAssistant | None:
    if not (settings.llm_base_url and settings.llm_model):
        return None
    from rapa.llm.client import HubClient

    capabilities = str(settings.llm_capabilities_file) if settings.llm_capabilities_file else None
    return HubAssistant(
        lambda: HubClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_s=60.0,
            max_retries=1,
        ),
        capabilities,
    )


def build_sessions(
    settings: ServerSettings, db: Database, platform: PgPlatform
) -> tuple[RecordingSessions, LocalSessionLauncher]:
    """Serviço das sessões de gravação com o lançador local (um processo por sessão)."""
    launcher = LocalSessionLauncher(
        LocalSessionSettings(
            chromium=settings.chromium,
            browser_args=settings.browser_args,
            profile_dir=settings.sessions_dir,
        )
    )
    service = RecordingSessions(
        PgSessionStore(db),
        launcher,
        platform,
        limits=SessionLimits(
            per_company=settings.sessions_per_company,
            global_max=settings.sessions_max,
            idle_timeout_s=settings.session_idle_s,
            max_duration_s=settings.session_max_s,
        ),
        assistant=_assistant(settings),
    )
    return service, launcher


#: Intervalo da varredura que encerra as sessões cujo navegador sumiu.
_SWEEP_INTERVAL_S = 15.0


async def _sweep_sessions(sessions: RecordingSessions, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_INTERVAL_S)
        if stop.is_set():
            return
        try:
            await sessions.sweep()
        except Exception as exc:
            logger.warning("falha na varredura das sessões de gravação: %s", type(exc).__name__)


async def _close_sessions(
    sessions: RecordingSessions,
    launcher: LocalSessionLauncher,
    stop: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    with contextlib.suppress(Exception):
        await sessions.aclose()
    await launcher.aclose()


def create_server_app(settings: ServerSettings) -> FastAPI:
    """Monta a aplicação: REST e MCP, interface (opcional) e despachante (opcional).

    O ``lifespan`` da aplicação roda uma vez só: para subir de novo, monte outra.
    Ele envolve tudo, inclusive a interface: o despachante para (e espera as
    execuções em andamento), depois o gateway MCP e a interface encerram, e só
    então o verificador de tokens e os bancos são fechados, mesmo se a subida
    falhar no meio.
    """
    db = Database.from_url(settings.database_url)
    dispatcher_db = (
        Database.from_url(
            settings.dispatcher_database_url,
            pool_size=dispatcher_pool_size(settings.concurrency),
        )
        if settings.dispatcher_database_url
        else None
    )
    platform = PgPlatform(db)
    resolver = PgPrincipalResolver(db)
    verifier = _verifier(settings) if settings.http else None
    sessions, session_launcher = (
        build_sessions(settings, db, platform)
        if settings.http and settings.sessions
        else (None, None)
    )
    if verifier is not None:
        app = create_http_app(
            catalog=platform,
            runs=platform,
            admin=platform,
            verifier=verifier,
            resolver=resolver,
            settings=HttpSettings(
                public_url=settings.public_url,
                authorization_servers=(settings.jwt_issuer,) if settings.jwt_issuer else (),
                allow_static_tokens=settings.dev,
                mcp_allowed_origins=settings.mcp_allowed_origins,
                rate_limit_per_s=settings.rate_limit_per_s,
            ),
            sessions=sessions,
        )
    else:
        app = FastAPI(title="RaPA (despachante)", docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/saude", include_in_schema=False)
        async def health() -> dict[str, str]:
            return {"estado": "ok"}

    dispatcher = build_dispatcher(settings, dispatcher_db) if dispatcher_db else None
    supervisor = DispatcherSupervisor(dispatcher) if dispatcher else None
    if supervisor is not None:
        app.add_middleware(_DispatcherHealth, supervisor=supervisor)
    app.state.dispatcher = dispatcher
    app.state.dispatcher_supervisor = supervisor
    app.state.sessions = sessions
    if settings.http and settings.ui:
        # Antes de envolver o lifespan: o NiceGUI (ui.run_with) envolve o que encontrar
        # com a subida e o encerramento dele, e esse conjunto fica dentro do nosso.
        _mount_ui(app, settings, platform, Authenticator(verifier, resolver), sessions)  # type: ignore[arg-type]
    inner = app.router.lifespan_context
    started = False

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[Any]:
        # A interface, o gateway MCP e o despachante só sobem uma vez por aplicação.
        # Uma segunda entrada (outro TestClient na mesma aplicação, por exemplo) é
        # recusada aqui, por fora de tudo, antes de abrir qualquer coisa (nem a subida
        # do NiceGUI roda de novo) ou de mexer no que já está rodando.
        nonlocal started
        if started:
            raise RuntimeError(
                "o serviço desta aplicação já foi iniciado uma vez; "
                "monte outra com create_server_app"
            )
        started = True
        async with contextlib.AsyncExitStack() as stack:
            # Fechados por último e sempre, mesmo se a subida falhar no meio.
            stack.push_async_callback(db.dispose)
            if dispatcher_db is not None:
                stack.push_async_callback(dispatcher_db.dispose)
            if isinstance(verifier, TokenVerifier):
                stack.push_async_callback(verifier.aclose)
            # A interface (se houver) e o gateway MCP: encerram antes de fechar os bancos.
            state = await stack.enter_async_context(inner(application))
            if sessions is not None and session_launcher is not None:
                # Os navegadores das sessões fecham antes dos bancos (a situação é gravada).
                sweep_stop = asyncio.Event()
                sweep = asyncio.create_task(
                    _sweep_sessions(sessions, sweep_stop), name="sessoes-de-gravacao"
                )
                stack.push_async_callback(
                    _close_sessions, sessions, session_launcher, sweep_stop, sweep
                )
            if supervisor is not None:
                stop = asyncio.Event()
                stack.push_async_callback(
                    _stop_dispatcher, supervisor, stop, supervisor.start(stop)
                )
            yield state

    # Por último: nada instalado depois fica por fora da recusa da segunda entrada.
    app.router.lifespan_context = lifespan
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.server",
        description="Sobe o RaPA (REST, MCP, interface e despachante) num processo só.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = ServerSettings.from_env()
    except ValueError as exc:
        print(f"rapa.server: configuração inválida: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    app = create_server_app(settings)
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
