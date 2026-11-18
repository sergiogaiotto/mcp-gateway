"""Login e sessões da interface, no padrão BFF (backend for frontend).

O navegador só recebe um cookie opaco (``rapa_sessao``, HttpOnly, SameSite=Lax),
que aponta para uma sessão guardada no servidor da interface. Tokens do
provedor de identidade ficam nessa sessão e nunca vão para o navegador. A
autoridade nunca vem do ``app.storage`` do NiceGUI: toda página e toda ação
consultam a sessão no :class:`SessionStore`.

Duas formas de entrar:

- **desenvolvimento** (:class:`DevLogin`): escolher um dos perfis configurados
  no servidor. Só existe quando ligada explicitamente;
- **OIDC** (:class:`OidcLogin`): fluxo de código com PKCE e ``nonce`` contra o
  Keycloak, com Authlib. O ``state`` e o verificador PKCE ficam no servidor.

Os pedidos de login pendentes (o bilhete do login de desenvolvimento e o
``state`` do OIDC) ficam presos ao navegador por um segundo cookie,
``rapa_vinculo``, criado antes de a pessoa escolher como entrar. Assim um
bilhete ou um retorno do provedor capturado por outra pessoa não serve em outro
navegador (proteção contra CSRF de login).
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.integrations.starlette_client import StarletteIntegration, StarletteOAuth2App
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from rapa.core.contracts import Principal

__all__ = [
    "BINDING_COOKIE",
    "HOME_PATH",
    "LOGIN_ERRORS",
    "LOGIN_PATH",
    "SESSION_COOKIE",
    "DevLogin",
    "DevProfile",
    "LoginError",
    "LoginResolver",
    "MemorySessionStore",
    "OidcAccessDenied",
    "OidcError",
    "OidcLogin",
    "OidcSettings",
    "PendingLogin",
    "SessionStore",
    "TokenSet",
    "UiAuth",
    "UiSession",
    "safe_return_path",
]

logger = logging.getLogger(__name__)

SESSION_COOKIE = "rapa_sessao"
BINDING_COOKIE = "rapa_vinculo"
LOGIN_PATH = "/entrar"
HOME_PATH = "/catalogo"

#: Mensagens mostradas na tela de login para cada código de erro (``/entrar?erro=…``).
LOGIN_ERRORS: dict[str, str] = {
    "login_expirado": "O pedido de login expirou. Tente de novo.",
    "login_invalido": "Não foi possível confirmar o login neste navegador. Tente de novo.",
    "perfil_desconhecido": "Esse perfil de desenvolvimento não existe.",
    "oidc_cancelado": "O login no provedor de identidade foi cancelado.",
    "oidc_falhou": "O provedor de identidade não confirmou o login. Tente de novo em instantes.",
    "sem_acesso": "Sua conta não tem acesso ao RaPA. Peça um convite ao administrador da empresa.",
    "sessao_expirada": "Sua sessão expirou. Entre de novo.",
}

_SECRET_MAX = 200
_BINDING_MAX_AGE = timedelta(hours=12)
_DEV_TICKET_TTL = timedelta(minutes=2)
_OIDC_PENDING_TTL = timedelta(minutes=10)
_TOUCH_EVERY = timedelta(seconds=30)
_REFRESH_MARGIN_S = 30.0
_PROFILE_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    """Só o hash dos segredos do navegador fica guardado no servidor."""
    return hashlib.sha256(value.encode()).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def safe_return_path(value: str | None, default: str = HOME_PATH) -> str:
    """Caminho interno para onde voltar depois do login; qualquer outra coisa vira ``default``.

    Recusa endereços absolutos, ``//host``, barras invertidas, caracteres de
    controle e as próprias rotas de login, para não haver redirecionamento
    aberto nem laço.
    """
    if not value or len(value) > 512 or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    if parts.path == LOGIN_PATH or parts.path.startswith(("/auth/", "/_nicegui")):
        return default
    return value


# ---------------------------------------------------------------------------
# Sessões
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenSet:
    """Tokens do provedor de identidade. Ficam só no servidor."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    id_token: str | None = field(default=None, repr=False)
    #: Instante (segundos desde a época) em que o token de acesso vence.
    expires_at: float | None = None

    def expires_soon(self, now: float, margin_s: float = _REFRESH_MARGIN_S) -> bool:
        return self.expires_at is not None and self.expires_at - margin_s <= now


@dataclass(slots=True)
class UiSession:
    """Sessão de uma pessoa na interface, guardada no servidor."""

    #: Hash do valor do cookie; o valor em si só existe no navegador.
    key: str
    principal: Principal
    display_name: str
    method: Literal["dev", "oidc"]
    created_at: datetime
    expires_at: datetime
    last_seen: datetime
    tokens: TokenSet | None = field(default=None, repr=False)

    @property
    def access_token(self) -> str | None:
        return self.tokens.access_token if self.tokens is not None else None


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """Login começado e ainda não concluído (bilhete de desenvolvimento ou ``state`` do OIDC)."""

    kind: Literal["dev", "oidc"]
    #: Hash do cookie de vínculo do navegador que começou o login.
    binding: str
    return_to: str
    expires_at: datetime
    profile: str | None = None
    nonce: str | None = field(default=None, repr=False)
    code_verifier: str | None = field(default=None, repr=False)


class SessionStore(Protocol):
    """Onde ficam as sessões e os logins pendentes (memória, ou uma tabela do Postgres)."""

    async def save(self, session: UiSession) -> None: ...

    async def load(self, key: str) -> UiSession | None: ...

    async def delete(self, key: str) -> None: ...

    async def put_pending(self, key: str, pending: PendingLogin) -> None: ...

    async def pop_pending(self, key: str) -> PendingLogin | None: ...

    async def prune(self, now: datetime) -> None: ...


class MemorySessionStore:
    """Sessões em memória, para desenvolvimento, testes e uma réplica só."""

    def __init__(self, *, max_sessions: int = 10_000, max_pending: int = 10_000) -> None:
        self._sessions: dict[str, UiSession] = {}
        self._pending: dict[str, PendingLogin] = {}
        self._max_sessions = max_sessions
        self._max_pending = max_pending

    def __len__(self) -> int:
        return len(self._sessions)

    async def save(self, session: UiSession) -> None:
        self._sessions[session.key] = session
        while len(self._sessions) > self._max_sessions:
            del self._sessions[next(iter(self._sessions))]

    async def load(self, key: str) -> UiSession | None:
        return self._sessions.get(key)

    async def delete(self, key: str) -> None:
        self._sessions.pop(key, None)

    async def put_pending(self, key: str, pending: PendingLogin) -> None:
        self._pending[key] = pending
        while len(self._pending) > self._max_pending:
            del self._pending[next(iter(self._pending))]

    async def pop_pending(self, key: str) -> PendingLogin | None:
        return self._pending.pop(key, None)

    async def prune(self, now: datetime) -> None:
        for key in [k for k, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[key]
        for key in [k for k, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[key]


# ---------------------------------------------------------------------------
# Login de desenvolvimento
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DevProfile:
    """Perfil escolhível no login de desenvolvimento, configurado no servidor."""

    key: str
    label: str
    principal: Principal
    description: str = ""
    #: Token fixo para usar a API REST em desenvolvimento (fica só no servidor).
    access_token: str | None = field(default=None, repr=False)


class DevLogin:
    """Login de desenvolvimento: a pessoa escolhe um perfil configurado no servidor."""

    def __init__(self, profiles: Iterable[DevProfile]) -> None:
        self.profiles: dict[str, DevProfile] = {}
        for profile in profiles:
            if not _PROFILE_KEY.fullmatch(profile.key):
                raise ValueError(f"chave de perfil inválida: {profile.key!r}")
            if profile.key in self.profiles:
                raise ValueError(f"perfil repetido: {profile.key!r}")
            self.profiles[profile.key] = profile
        if not self.profiles:
            raise ValueError("o login de desenvolvimento precisa de ao menos um perfil")

    def get(self, key: str | None) -> DevProfile | None:
        return self.profiles.get(key) if key else None


# ---------------------------------------------------------------------------
# OIDC (Keycloak)
# ---------------------------------------------------------------------------


class OidcError(Exception):
    """O provedor de identidade não confirmou o login."""


class OidcAccessDenied(OidcError):
    """Login confirmado, mas a conta não tem acesso ao RaPA."""


class LoginResolver(Protocol):
    """Busca o principal (empresa e papéis) de quem acabou de entrar.

    Recebe as declarações já verificadas do ID token e o token de acesso. Os
    papéis vêm do banco, nunca do token; ``None`` quer dizer sem acesso. Na
    integração, use o autenticador da camada HTTP, por exemplo
    ``await authenticator.authenticate(f"Bearer {access_token}")``.
    """

    async def resolve(self, claims: Mapping[str, Any], access_token: str) -> Principal | None: ...


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """Configuração do cliente OIDC (confidencial, do lado do servidor)."""

    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = field(default=None, repr=False)
    scope: str = "openid profile email"
    post_logout_redirect_uri: str | None = None
    timeout_s: float = 10.0
    leeway_s: int = 60


def _display_name(claims: Mapping[str, Any], principal: Principal) -> str:
    for key in ("name", "preferred_username", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return principal.subject


class OidcLogin:
    """Fluxo de código com PKCE (S256) e ``nonce``, usando o Authlib."""

    def __init__(
        self,
        settings: OidcSettings,
        resolver: LoginResolver,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._resolver = resolver
        self._transport = transport
        self._clock = clock
        self._auth_method = "client_secret_basic" if settings.client_secret else "none"
        client_kwargs: dict[str, Any] = {
            "scope": settings.scope,
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": self._auth_method,
            "timeout": settings.timeout_s,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._app = StarletteOAuth2App(
            StarletteIntegration("rapa"),
            name="rapa",
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            server_metadata_url=f"{settings.issuer.rstrip('/')}/.well-known/openid-configuration",
            client_kwargs=client_kwargs,
        )

    async def _metadata(self) -> dict[str, Any]:
        try:
            metadata: dict[str, Any] = await self._app.load_server_metadata()
        except Exception as exc:
            logger.warning("falha ao ler a configuração do provedor OIDC: %s", type(exc).__name__)
            raise OidcError("provedor indisponível") from None
        if metadata.get("issuer") != self.settings.issuer:
            # Defesa contra confusão de provedores: o emissor precisa ser o configurado.
            self._app.server_metadata.pop("_loaded_at", None)
            raise OidcError("o emissor do provedor não é o configurado")
        return metadata

    async def authorization_request(self) -> tuple[str, str, str, str]:
        """Endereço de login no provedor e os segredos do pedido: (url, state, nonce, verificador)."""
        await self._metadata()
        try:
            data = await self._app.create_authorization_url(self.settings.redirect_uri)
        except Exception as exc:
            logger.warning("falha ao montar o pedido de login OIDC: %s", type(exc).__name__)
            raise OidcError("pedido de login inválido") from None
        return str(data["url"]), str(data["state"]), str(data["nonce"]), str(data["code_verifier"])

    def _token_set(self, token: Mapping[str, Any], previous: TokenSet | None = None) -> TokenSet:
        access = token.get("access_token")
        if not isinstance(access, str) or not access:
            raise OidcError("o provedor não devolveu token de acesso")
        expires_at: float | None = None
        if isinstance(token.get("expires_at"), (int, float)):
            expires_at = float(token["expires_at"])
        elif isinstance(token.get("expires_in"), (int, float)):
            expires_at = self._clock() + float(token["expires_in"])
        refresh = token.get("refresh_token")
        id_token = token.get("id_token")
        return TokenSet(
            access_token=access,
            refresh_token=refresh
            if isinstance(refresh, str)
            else (previous.refresh_token if previous else None),
            id_token=id_token
            if isinstance(id_token, str)
            else (previous.id_token if previous else None),
            expires_at=expires_at,
        )

    async def complete(
        self, code: str, nonce: str | None, code_verifier: str | None
    ) -> tuple[Principal, str, TokenSet]:
        """Troca o código pelos tokens, confere o ID token e busca o principal."""
        if not nonce or not code_verifier:
            raise OidcError("pedido de login incompleto")
        await self._metadata()
        try:
            token = await self._app.fetch_access_token(
                redirect_uri=self.settings.redirect_uri, code=code, code_verifier=code_verifier
            )
        except Exception as exc:
            logger.warning("o provedor OIDC recusou a troca do código: %s", type(exc).__name__)
            raise OidcError("troca do código recusada") from None
        if not isinstance(token, Mapping) or not token.get("id_token"):
            raise OidcError("o provedor não devolveu ID token")
        tokens = self._token_set(token)
        options = {
            "iss": {"essential": True, "value": self.settings.issuer},
            "aud": {"essential": True, "value": self.settings.client_id},
            "sub": {"essential": True},
            "exp": {"essential": True},
        }
        try:
            claims = await self._app.parse_id_token(
                dict(token), nonce=nonce, claims_options=options, leeway=self.settings.leeway_s
            )
        except Exception as exc:
            logger.warning("ID token recusado: %s", type(exc).__name__)
            raise OidcError("ID token inválido") from None
        if claims is None:
            raise OidcError("ID token ausente")
        principal = await self._resolver.resolve(dict(claims), tokens.access_token)
        if principal is None:
            raise OidcAccessDenied("conta sem acesso")
        return principal, _display_name(claims, principal), tokens

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        """Renova o token de acesso com o token de renovação."""
        if tokens.refresh_token is None:
            raise OidcError("sem token de renovação")
        metadata = await self._metadata()
        kwargs: dict[str, Any] = {"timeout": self.settings.timeout_s}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with AsyncOAuth2Client(
                client_id=self.settings.client_id,
                client_secret=self.settings.client_secret,
                token_endpoint_auth_method=self._auth_method,
                **kwargs,
            ) as client:
                token = await client.refresh_token(
                    metadata["token_endpoint"], refresh_token=tokens.refresh_token
                )
        except Exception as exc:
            logger.warning("renovação do token recusada: %s", type(exc).__name__)
            raise OidcError("renovação recusada") from None
        return self._token_set(token, previous=tokens)

    async def logout_url(self) -> str | None:
        """Endereço de saída no provedor (sem mandar o ID token ao navegador)."""
        try:
            metadata = await self._metadata()
        except OidcError:
            return None
        endpoint = metadata.get("end_session_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        params = {"client_id": self.settings.client_id}
        if self.settings.post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = self.settings.post_logout_redirect_uri
        return f"{endpoint}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Orquestração e rotas
# ---------------------------------------------------------------------------


class LoginError(Exception):
    """Falha de login com um código de :data:`LOGIN_ERRORS`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class UiAuth:
    """Sessões da interface, login de desenvolvimento e OIDC, e as rotas ``/auth``."""

    def __init__(
        self,
        *,
        dev: DevLogin | None = None,
        oidc: OidcLogin | None = None,
        store: SessionStore | None = None,
        cookie_secure: bool = True,
        session_ttl: timedelta = timedelta(hours=8),
        idle_timeout: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if dev is None and oidc is None:
            raise ValueError("configure ao menos uma forma de login (desenvolvimento ou OIDC)")
        self.dev = dev
        self.oidc = oidc
        self.store: SessionStore = store if store is not None else MemorySessionStore()
        self.cookie_secure = cookie_secure
        self.session_ttl = session_ttl
        self.idle_timeout = idle_timeout
        self._clock = clock
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    # -- sessões ---------------------------------------------------------------

    async def session_from_request(self, request: Request) -> UiSession | None:
        """Sessão do cookie da requisição, se existir e ainda valer."""
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie or len(cookie) > _SECRET_MAX:
            return None
        return await self.session_by_key(_digest(cookie))

    async def session_by_key(self, key: str) -> UiSession | None:
        session = await self.store.load(key)
        if session is None:
            return None
        now = self._clock()
        if now >= session.expires_at or now - session.last_seen >= self.idle_timeout:
            await self.end_session(key)
            return None
        if now - session.last_seen >= _TOUCH_EVERY:
            session.last_seen = now
            await self.store.save(session)
        return session

    async def fresh_session(self, key: str) -> UiSession | None:
        """Como :meth:`session_by_key`, renovando o token de acesso se estiver para vencer."""
        session = await self.session_by_key(key)
        if session is None or session.method != "oidc" or self.oidc is None:
            return session
        tokens = session.tokens
        if tokens is None or not tokens.expires_soon(time.time()):
            return session
        # Uma renovação por sessão de cada vez: o Keycloak pode girar o token de renovação.
        lock = self._refresh_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                return await self._refresh(key)
        finally:
            if not lock.locked() and self._refresh_locks.get(key) is lock:
                del self._refresh_locks[key]

    async def _refresh(self, key: str) -> UiSession | None:
        session = await self.store.load(key)
        if session is None or session.tokens is None or self.oidc is None:
            return None
        if not session.tokens.expires_soon(time.time()):
            return session
        try:
            session.tokens = await self.oidc.refresh(session.tokens)
        except OidcError:
            await self.end_session(key)
            return None
        await self.store.save(session)
        return session

    async def start_session(
        self,
        principal: Principal,
        display_name: str,
        method: Literal["dev", "oidc"],
        tokens: TokenSet | None = None,
    ) -> tuple[str, UiSession]:
        """Cria a sessão e devolve o valor do cookie (que só vai para o navegador)."""
        now = self._clock()
        await self.store.prune(now)
        cookie = _new_secret()
        session = UiSession(
            key=_digest(cookie),
            principal=principal,
            display_name=display_name,
            method=method,
            created_at=now,
            expires_at=now + self.session_ttl,
            last_seen=now,
            tokens=tokens,
        )
        await self.store.save(session)
        return cookie, session

    async def end_session(self, key: str) -> None:
        self._refresh_locks.pop(key, None)
        await self.store.delete(key)

    # -- login de desenvolvimento ----------------------------------------------

    async def create_dev_ticket(
        self, binding_cookie: str | None, profile_key: str, return_to: str | None
    ) -> str:
        """Bilhete de uso único (2 min) para concluir o login com um perfil de desenvolvimento."""
        if self.dev is None:
            raise LoginError("perfil_desconhecido")
        if self.dev.get(profile_key) is None:
            raise LoginError("perfil_desconhecido")
        if not binding_cookie or len(binding_cookie) > _SECRET_MAX:
            raise LoginError("login_invalido")
        ticket = _new_secret()
        await self.store.put_pending(
            _digest(ticket),
            PendingLogin(
                kind="dev",
                binding=_digest(binding_cookie),
                return_to=safe_return_path(return_to),
                expires_at=self._clock() + _DEV_TICKET_TTL,
                profile=profile_key,
            ),
        )
        return ticket

    # -- rotas -----------------------------------------------------------------

    def _set_cookie(self, response: Response, name: str, value: str, max_age: timedelta) -> None:
        response.set_cookie(
            name,
            value,
            max_age=int(max_age.total_seconds()),
            path="/",
            secure=self.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    @staticmethod
    def login_error_redirect(code: str, return_to: str | None = None) -> RedirectResponse:
        target = f"{LOGIN_PATH}?erro={quote(code)}"
        if return_to:
            target += f"&volta={quote(safe_return_path(return_to), safe='')}"
        response = RedirectResponse(target, status_code=303)
        _no_store(response)
        return response

    async def _take_pending(
        self, secret: str, kind: Literal["dev", "oidc"], request: Request
    ) -> PendingLogin:
        if not secret or len(secret) > _SECRET_MAX:
            raise LoginError("login_expirado")
        pending = await self.store.pop_pending(_digest(secret))
        if pending is None or pending.kind != kind or pending.expires_at <= self._clock():
            raise LoginError("login_expirado")
        binding = request.cookies.get(BINDING_COOKIE)
        if (
            not binding
            or len(binding) > _SECRET_MAX
            or not hmac.compare_digest(_digest(binding), pending.binding)
        ):
            raise LoginError("login_invalido")
        return pending

    async def _finish_login(
        self,
        request: Request,
        principal: Principal,
        display_name: str,
        method: Literal["dev", "oidc"],
        tokens: TokenSet | None,
        return_to: str,
    ) -> Response:
        old = request.cookies.get(SESSION_COOKIE)
        if old and len(old) <= _SECRET_MAX:
            await self.end_session(_digest(old))  # nunca reaproveita a sessão anterior
        cookie, _ = await self.start_session(principal, display_name, method, tokens)
        response = RedirectResponse(safe_return_path(return_to), status_code=303)
        self._set_cookie(response, SESSION_COOKIE, cookie, self.session_ttl)
        return _no_store(response)

    def router(self) -> APIRouter:
        """Rotas ``/auth/*`` (FastAPI puro, fora do NiceGUI, para poder gravar cookies)."""
        router = APIRouter(prefix="/auth", include_in_schema=False)

        @router.get("/vinculo")
        async def binding(request: Request, volta: str = "") -> Response:
            target = f"{LOGIN_PATH}?cookie=1"
            if volta:
                target += f"&volta={quote(safe_return_path(volta), safe='')}"
            response = RedirectResponse(target, status_code=303)
            if not request.cookies.get(BINDING_COOKIE):
                self._set_cookie(response, BINDING_COOKIE, _new_secret(), _BINDING_MAX_AGE)
            return _no_store(response)

        if self.dev is not None:
            self._add_dev_routes(router, self.dev)
        if self.oidc is not None:
            self._add_oidc_routes(router, self.oidc)

        @router.get("/sair")
        async def logout(request: Request) -> Response:
            target = f"{LOGIN_PATH}?saiu=1"
            cookie = request.cookies.get(SESSION_COOKIE)
            if cookie and len(cookie) <= _SECRET_MAX:
                key = _digest(cookie)
                session = await self.store.load(key)
                await self.end_session(key)
                if session is not None and session.method == "oidc" and self.oidc is not None:
                    target = await self.oidc.logout_url() or target
            response = RedirectResponse(target, status_code=303)
            response.delete_cookie(
                SESSION_COOKIE, path="/", secure=self.cookie_secure, httponly=True, samesite="lax"
            )
            return _no_store(response)

        return router

    def _add_dev_routes(self, router: APIRouter, dev: DevLogin) -> None:
        @router.get("/dev/concluir")
        async def dev_finish(request: Request, bilhete: str = "") -> Response:
            try:
                pending = await self._take_pending(bilhete, "dev", request)
                profile = dev.get(pending.profile)
                if profile is None:
                    raise LoginError("perfil_desconhecido")
            except LoginError as exc:
                return self.login_error_redirect(exc.code)
            tokens = TokenSet(profile.access_token) if profile.access_token else None
            return await self._finish_login(
                request, profile.principal, profile.label, "dev", tokens, pending.return_to
            )

    def _add_oidc_routes(self, router: APIRouter, oidc: OidcLogin) -> None:
        @router.get("/oidc/entrar")
        async def oidc_start(request: Request, volta: str = "") -> Response:
            binding_cookie = request.cookies.get(BINDING_COOKIE) or ""
            new_binding = not binding_cookie or len(binding_cookie) > _SECRET_MAX
            if new_binding:
                binding_cookie = _new_secret()
            try:
                url, state, nonce, verifier = await oidc.authorization_request()
            except OidcError:
                return self.login_error_redirect("oidc_falhou", volta)
            await self.store.put_pending(
                _digest(state),
                PendingLogin(
                    kind="oidc",
                    binding=_digest(binding_cookie),
                    return_to=safe_return_path(volta),
                    expires_at=self._clock() + _OIDC_PENDING_TTL,
                    nonce=nonce,
                    code_verifier=verifier,
                ),
            )
            response = RedirectResponse(url, status_code=303)
            if new_binding:
                self._set_cookie(response, BINDING_COOKIE, binding_cookie, _BINDING_MAX_AGE)
            return _no_store(response)

        @router.get("/oidc/retorno")
        async def oidc_callback(
            request: Request, code: str = "", state: str = "", error: str = ""
        ) -> Response:
            if error:
                if state and len(state) <= _SECRET_MAX:
                    await self.store.pop_pending(_digest(state))
                return self.login_error_redirect(
                    "oidc_cancelado" if error == "access_denied" else "oidc_falhou"
                )
            try:
                pending = await self._take_pending(state, "oidc", request)
            except LoginError as exc:
                return self.login_error_redirect(exc.code)
            if not code or len(code) > 4096:
                return self.login_error_redirect("oidc_falhou", pending.return_to)
            try:
                principal, name, tokens = await oidc.complete(
                    code, pending.nonce, pending.code_verifier
                )
            except OidcAccessDenied:
                return self.login_error_redirect("sem_acesso")
            except OidcError:
                return self.login_error_redirect("oidc_falhou", pending.return_to)
            return await self._finish_login(
                request, principal, name, "oidc", tokens, pending.return_to
            )
