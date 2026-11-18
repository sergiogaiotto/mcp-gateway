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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from rapa.core.contracts import Principal

__all__ = ["AuthenticatorLoginResolver", "ServerSettings", "create_server_app", "main"]

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
    files_dir: Path | None = None

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
          (argumentos separados por espaço, com aspas se preciso);
        - ``RAPA_MCP_ORIGENS`` (separadas por vírgula), ``RAPA_LIMITE_POR_SEGUNDO``
          (0 desliga) e ``RAPA_COOKIE_SEGURO``.
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
        }
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
        backend_factory=ContractsBackendFactory.from_platform(platform),
        auth=UiAuth(dev=dev_login, oidc=oidc, cookie_secure=settings.cookie_secure),
    )


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


def create_server_app(settings: ServerSettings) -> FastAPI:
    """Monta a aplicação: REST e MCP, interface (opcional) e despachante (opcional)."""
    db = Database.from_url(settings.database_url)
    dispatcher_db = (
        Database.from_url(settings.dispatcher_database_url)
        if settings.dispatcher_database_url
        else None
    )
    platform = PgPlatform(db)
    resolver = PgPrincipalResolver(db)
    verifier = _verifier(settings) if settings.http else None
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
        )
    else:
        app = FastAPI(title="RaPA (despachante)", docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/saude", include_in_schema=False)
        async def health() -> dict[str, str]:
            return {"estado": "ok"}

    dispatcher = build_dispatcher(settings, dispatcher_db) if dispatcher_db else None
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[Any]:
        async with inner(application) as state:
            stop = asyncio.Event()
            task = asyncio.create_task(dispatcher.run(stop)) if dispatcher else None
            try:
                yield state
            finally:
                stop.set()
                if task is not None:
                    await task
                if isinstance(verifier, TokenVerifier):
                    await verifier.aclose()
                await db.dispose()
                if dispatcher_db is not None:
                    await dispatcher_db.dispose()

    app.router.lifespan_context = lifespan
    app.state.dispatcher = dispatcher
    if settings.http and settings.ui:
        _mount_ui(app, settings, platform, Authenticator(verifier, resolver))  # type: ignore[arg-type]
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
