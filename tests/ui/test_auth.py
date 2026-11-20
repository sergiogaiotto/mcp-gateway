"""Login e sessões da interface: rotas /auth, login de desenvolvimento e OIDC simulado."""

import asyncio
import base64
import hashlib
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from joserfc import jwt
from joserfc.jwk import RSAKey

from rapa.core.contracts import Principal, Role
from rapa.core.ids import new_id
from rapa.ui.auth import (
    BINDING_COOKIE,
    SESSION_COOKIE,
    DevLogin,
    DevProfile,
    LoginError,
    MemorySessionStore,
    OidcLogin,
    OidcSettings,
    TokenSet,
    UiAuth,
    UiSession,
    safe_return_path,
)

COMPANY = new_id()
PRINCIPAL = Principal(
    subject="ana@empresa", company_id=COMPANY, company_roles=frozenset({Role.OPERATOR})
)
ISSUER = "https://sso.rapa.test/realms/rapa"
CLIENT_ID = "rapa-ui"
CLIENT_SECRET = "segredo-do-cliente"
REDIRECT = "https://rapa.test/auth/oidc/retorno"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _dev() -> DevLogin:
    return DevLogin(
        [
            DevProfile("ana", "Ana (operadora)", PRINCIPAL),
            DevProfile(
                "api",
                "Com token",
                PRINCIPAL.model_copy(update={"subject": "api"}),
                access_token="token-fixo",
            ),
        ]
    )


def _client(auth: UiAuth) -> httpx2.AsyncClient:
    app = FastAPI()
    app.include_router(auth.router())
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="https://rapa.test")


def test_safe_return_path() -> None:
    assert safe_return_path("/robos?versao=2") == "/robos?versao=2"
    for bad in (
        None,
        "",
        "https://evil.test/",
        "//evil.test/x",
        "/\\evil.test",
        "robos",
        "/entrar?volta=/x",
        "/auth/sair",
        "/a b",
        "/x\r\nSet-Cookie: a=b",
        "/" + "a" * 600,
    ):
        assert safe_return_path(bad) == "/catalogo"


def test_dev_login_configuration_errors() -> None:
    with pytest.raises(ValueError, match="ao menos uma forma"):
        UiAuth()
    with pytest.raises(ValueError, match="ao menos um perfil"):
        DevLogin([])
    with pytest.raises(ValueError, match="repetido"):
        DevLogin([DevProfile("ana", "A", PRINCIPAL), DevProfile("ana", "B", PRINCIPAL)])
    with pytest.raises(ValueError, match="inválida"):
        DevLogin([DevProfile("Ana!", "A", PRINCIPAL)])
    assert "token-fixo" not in repr(_dev().get("api"))


async def test_only_configured_login_routes_exist() -> None:
    async with _client(UiAuth(dev=_dev())) as client:
        assert (await client.get("/auth/oidc/entrar")).status_code == 404
        assert (await client.get("/auth/oidc/retorno")).status_code == 404
    keycloak = FakeKeycloak()
    async with _client(UiAuth(oidc=_oidc(keycloak, Resolver(PRINCIPAL)))) as client:
        assert (await client.get("/auth/dev/concluir", params={"bilhete": "x"})).status_code == 404
        assert (await client.get("/auth/sair")).status_code == 303


async def test_binding_cookie_flags() -> None:
    auth = UiAuth(dev=_dev())
    async with _client(auth) as client:
        response = await client.get("/auth/vinculo", params={"volta": "/robos"})
    assert response.status_code == 303
    assert response.headers["location"] == "/entrar?cookie=1&volta=%2Frobos"
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{BINDING_COOKIE}=")
    for flag in ("httponly", "secure", "samesite=lax", "path=/"):
        assert flag in cookie
    assert response.headers["cache-control"] == "no-store"


async def test_dev_login_flow_and_logout() -> None:
    store = MemorySessionStore()
    auth = UiAuth(dev=_dev(), store=store)
    async with _client(auth) as client:
        await client.get("/auth/vinculo")
        binding = client.cookies[BINDING_COOKIE]
        ticket = await auth.create_dev_ticket(binding, "api", "/robos")
        response = await client.get("/auth/dev/concluir", params={"bilhete": ticket})
        assert response.status_code == 303
        assert response.headers["location"] == "/robos"
        set_cookie = response.headers["set-cookie"]
        assert set_cookie.startswith(f"{SESSION_COOKIE}=")
        assert "token-fixo" not in set_cookie and "HttpOnly" in set_cookie
        cookie = client.cookies[SESSION_COOKIE]
        (session,) = store._sessions.values()
        assert session.key != cookie and session.key == hashlib.sha256(cookie.encode()).hexdigest()
        assert session.principal.subject == "api"
        assert session.access_token == "token-fixo"
        assert "token-fixo" not in repr(session)

        # O bilhete é de uso único.
        again = await client.get("/auth/dev/concluir", params={"bilhete": ticket})
        assert again.headers["location"] == "/entrar?erro=login_expirado"

        # Um novo login encerra a sessão anterior (sem fixação de sessão).
        ticket2 = await auth.create_dev_ticket(binding, "ana", "/catalogo")
        await client.get("/auth/dev/concluir", params={"bilhete": ticket2})
        (session2,) = store._sessions.values()
        assert session2.principal.subject == "ana@empresa"

        out = await client.get("/auth/sair")
        assert out.headers["location"] == "/entrar?saiu=1"
        assert f'{SESSION_COOKIE}=""' in out.headers["set-cookie"]
        assert len(store) == 0


async def test_dev_ticket_bound_to_browser_and_short_lived() -> None:
    clock = Clock()
    auth = UiAuth(dev=_dev(), cookie_secure=False, clock=clock)
    async with _client(auth) as victim, _client(auth) as attacker:
        await attacker.get("/auth/vinculo")
        await victim.get("/auth/vinculo")
        ticket = await auth.create_dev_ticket(attacker.cookies[BINDING_COOKIE], "ana", "/")
        stolen = await victim.get("/auth/dev/concluir", params={"bilhete": ticket})
        assert stolen.headers["location"] == "/entrar?erro=login_invalido"
        assert SESSION_COOKIE not in victim.cookies
        # Recusado uma vez, o bilhete não serve nem para o navegador certo.
        late = await attacker.get("/auth/dev/concluir", params={"bilhete": ticket})
        assert late.headers["location"] == "/entrar?erro=login_expirado"

        ticket = await auth.create_dev_ticket(attacker.cookies[BINDING_COOKIE], "ana", "/")
        clock.now += timedelta(minutes=3)
        expired = await attacker.get("/auth/dev/concluir", params={"bilhete": ticket})
        assert expired.headers["location"] == "/entrar?erro=login_expirado"
        forged = await attacker.get("/auth/dev/concluir", params={"bilhete": "x" * 500})
        assert forged.headers["location"] == "/entrar?erro=login_expirado"

    with pytest.raises(LoginError) as caught:
        await auth.create_dev_ticket("vinculo", "desconhecido", "/")
    assert caught.value.code == "perfil_desconhecido"
    with pytest.raises(LoginError) as caught:
        await auth.create_dev_ticket(None, "ana", "/")
    assert caught.value.code == "login_invalido"


async def test_session_idle_and_absolute_expiry() -> None:
    clock = Clock()
    auth = UiAuth(
        dev=_dev(),
        clock=clock,
        session_ttl=timedelta(hours=2),
        idle_timeout=timedelta(minutes=30),
    )
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    clock.now += timedelta(minutes=20)
    assert await auth.session_by_key(session.key) is not None
    clock.now += timedelta(minutes=20)  # 20 min desde o último uso
    assert await auth.session_by_key(session.key) is not None
    clock.now += timedelta(minutes=31)
    assert await auth.session_by_key(session.key) is None

    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    for _ in range(5):
        clock.now += timedelta(minutes=25)
        found = await auth.session_by_key(session.key)
    assert found is None  # passou das 2 horas, mesmo em uso


# ---------------------------------------------------------------------------
# OIDC com um Keycloak simulado
# ---------------------------------------------------------------------------


class FakeKeycloak:
    """Provedor OIDC mínimo: descoberta, JWKS e endpoint de token com PKCE."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "chave-1"})
        self.issuer = ISSUER
        self.codes: dict[str, dict[str, str]] = {}
        self.claims_override: dict[str, Any] = {}
        self.refreshes = 0
        self.token_requests: list[dict[str, list[str]]] = []

    def authorize(self, url: str) -> str:
        """Simula a pessoa entrando: devolve o código para o retorno."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["redirect_uri"] == REDIRECT
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        code = f"codigo-{len(self.codes)}"
        self.codes[code] = params
        return code

    def _id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "kc-123",
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "name": "Ana Souza",
            **self.claims_override,
        }
        return jwt.encode({"alg": "RS256", "kid": "chave-1"}, claims, self.key)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/realms/rapa/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if path == "/realms/rapa/protocol/openid-connect/certs":
            return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/realms/rapa/protocol/openid-connect/token":
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return httpx2.Response(401, json={"error": "invalid_client"})
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            if form["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                return httpx2.Response(
                    200,
                    json={
                        "access_token": f"acesso-renovado-{self.refreshes}",
                        "token_type": "Bearer",
                        "expires_in": 300,
                    },
                )
            params = self.codes.pop(form["code"][0], None)
            if params is None:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            verifier = form["code_verifier"][0]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if challenge != params["code_challenge"] or form["redirect_uri"] != [REDIRECT]:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(
                200,
                json={
                    "access_token": "acesso-1",
                    "refresh_token": "renovacao-1",
                    "id_token": self._id_token(params["nonce"]),
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            )
        return httpx2.Response(404)


class Resolver:
    def __init__(self, principal: Principal | None) -> None:
        self.principal = principal
        self.calls: list[tuple[dict[str, Any], str]] = []

    async def resolve(self, claims: Mapping[str, Any], access_token: str) -> Principal | None:
        self.calls.append((dict(claims), access_token))
        return self.principal


def _oidc(keycloak: FakeKeycloak, resolver: Resolver) -> OidcLogin:
    return OidcLogin(
        OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT,
            post_logout_redirect_uri="https://rapa.test/entrar",
        ),
        resolver,
        transport=httpx2.MockTransport(keycloak.handler),
    )


async def _oidc_login(client: httpx2.AsyncClient, keycloak: FakeKeycloak) -> httpx2.Response:
    start = await client.get("/auth/oidc/entrar", params={"volta": "/robos"})
    assert start.status_code == 303
    code = keycloak.authorize(start.headers["location"])
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await client.get("/auth/oidc/retorno", params={"code": code, "state": state})


async def test_oidc_login_keeps_tokens_on_server() -> None:
    keycloak, resolver, store = FakeKeycloak(), Resolver(PRINCIPAL), MemorySessionStore()
    auth = UiAuth(oidc=_oidc(keycloak, resolver), store=store)
    async with _client(auth) as client:
        done = await _oidc_login(client, keycloak)
        assert done.status_code == 303 and done.headers["location"] == "/robos"
        raw = str(done.headers) + done.text
        for token in ("acesso-1", "renovacao-1", "eyJ"):
            assert token not in raw
        (session,) = store._sessions.values()
        assert session.method == "oidc" and session.display_name == "Ana Souza"
        assert session.tokens is not None
        assert session.tokens.access_token == "acesso-1"
        assert session.tokens.refresh_token == "renovacao-1"
        assert "acesso-1" not in repr(session) and "renovacao-1" not in repr(session.tokens)
        ((claims, access),) = resolver.calls
        assert claims["sub"] == "kc-123" and access == "acesso-1"

        # Token perto de vencer: a sessão renova sozinha com o token de renovação.
        session.tokens.expires_at = time.time() + 5
        fresh = await auth.fresh_session(session.key)
        assert fresh is not None and fresh.access_token == "acesso-renovado-1"
        assert fresh.tokens is not None and fresh.tokens.refresh_token == "renovacao-1"
        assert keycloak.token_requests[-1]["refresh_token"] == ["renovacao-1"]

        out = await client.get("/auth/sair")
        location = urlsplit(out.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == (
            f"{ISSUER}/protocol/openid-connect/logout"
        )
        query = parse_qs(location.query)
        assert query == {
            "client_id": [CLIENT_ID],
            "post_logout_redirect_uri": ["https://rapa.test/entrar"],
        }
        assert len(store) == 0


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"nonce": "outro"}, "oidc_falhou"),
        ({"aud": "outro-cliente"}, "oidc_falhou"),
        ({"iss": "https://outro.test/realms/x"}, "oidc_falhou"),
        ({"exp": int(time.time()) - 3600}, "oidc_falhou"),
    ],
)
async def test_oidc_rejects_bad_id_tokens(override: dict[str, Any], expected: str) -> None:
    keycloak, store = FakeKeycloak(), MemorySessionStore()
    keycloak.claims_override = override
    auth = UiAuth(oidc=_oidc(keycloak, Resolver(PRINCIPAL)), store=store)
    async with _client(auth) as client:
        done = await _oidc_login(client, keycloak)
    assert done.headers["location"].startswith(f"/entrar?erro={expected}")
    assert len(store) == 0


async def test_oidc_denied_cancelled_and_mixed_up() -> None:
    keycloak = FakeKeycloak()
    auth = UiAuth(oidc=_oidc(keycloak, Resolver(None)))
    async with _client(auth) as client:
        denied = await _oidc_login(client, keycloak)
        assert denied.headers["location"] == "/entrar?erro=sem_acesso"

        start = await client.get("/auth/oidc/entrar")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        cancelled = await client.get(
            "/auth/oidc/retorno", params={"error": "access_denied", "state": state}
        )
        assert cancelled.headers["location"] == "/entrar?erro=oidc_cancelado"
        # O state cancelado não serve mais.
        replay = await client.get("/auth/oidc/retorno", params={"code": "x", "state": state})
        assert replay.headers["location"] == "/entrar?erro=login_expirado"

    async with _client(auth) as other_browser:
        unknown = await other_browser.get(
            "/auth/oidc/retorno", params={"code": "x", "state": "nao-existe"}
        )
        assert unknown.headers["location"] == "/entrar?erro=login_expirado"

    mixed = FakeKeycloak()
    mixed.issuer = "https://impostor.test/realms/rapa"
    auth = UiAuth(oidc=_oidc(mixed, Resolver(PRINCIPAL)))
    async with _client(auth) as client:
        refused = await client.get("/auth/oidc/entrar", params={"volta": "/robos"})
    assert refused.headers["location"] == "/entrar?erro=oidc_falhou&volta=%2Frobos"


async def test_oidc_state_bound_to_browser() -> None:
    keycloak = FakeKeycloak()
    auth = UiAuth(oidc=_oidc(keycloak, Resolver(PRINCIPAL)))
    async with _client(auth) as attacker, _client(auth) as victim:
        start = await attacker.get("/auth/oidc/entrar")
        code = keycloak.authorize(start.headers["location"])
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        await victim.get("/auth/vinculo")
        forced = await victim.get("/auth/oidc/retorno", params={"code": code, "state": state})
    assert forced.headers["location"] == "/entrar?erro=login_invalido"
    assert SESSION_COOKIE not in victim.cookies


async def test_failed_refresh_ends_session() -> None:
    keycloak, store = FakeKeycloak(), MemorySessionStore()
    auth = UiAuth(oidc=_oidc(keycloak, Resolver(PRINCIPAL)), store=store)
    _, session = await auth.start_session(
        PRINCIPAL,
        "Ana",
        "oidc",
        TokenSet("acesso-velho", refresh_token=None, expires_at=time.time() - 10),
    )
    assert await auth.fresh_session(session.key) is None
    assert len(store) == 0
    with pytest.raises(LoginError):
        await auth.create_dev_ticket("vinculo", "ana", "/")


# ---------------------------------------------------------------------------
# O principal da sessão acompanha o banco e o provedor
# ---------------------------------------------------------------------------


class Principals:
    """Resolvedor de principais com papéis que o teste muda (como o banco)."""

    def __init__(self, principal: Principal | None) -> None:
        self.current: Principal | None = principal
        self.calls: list[tuple[str, Any]] = []
        self.error: Exception | None = None

    async def resolve(self, subject: str, company_id: Any) -> Principal | None:
        self.calls.append((subject, company_id))
        if self.error is not None:
            raise self.error
        return self.current


async def test_session_principal_is_rechecked_and_follows_role_changes() -> None:
    clock, store = Clock(), MemorySessionStore()
    principals = Principals(PRINCIPAL)
    auth = UiAuth(
        dev=_dev(),
        store=store,
        clock=clock,
        principals=principals,
        principal_ttl=timedelta(seconds=30),
    )
    # O perfil de desenvolvimento é configurado na subida: o primeiro uso já confere.
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    found = await auth.session_by_key(session.key)
    assert found is not None and principals.calls == [("ana@empresa", COMPANY)]

    # Dentro do prazo curto, sem nova consulta (a tela consulta a cada 2 s).
    clock.now += timedelta(seconds=10)
    assert await auth.session_by_key(session.key) is not None
    assert len(principals.calls) == 1

    # Papéis mudaram no banco: a sessão passa a usar os novos, sem pedir login de novo.
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    principals.current = approver
    clock.now += timedelta(seconds=25)
    found = await auth.session_by_key(session.key)
    assert found is not None and found.principal == approver
    assert len(principals.calls) == 2
    stored = await store.load(session.key)
    assert stored is not None and stored.principal == approver

    # Conta desativada (ou sem vínculo com a empresa): a sessão acaba no próximo uso.
    principals.current = None
    clock.now += timedelta(seconds=31)
    assert await auth.session_by_key(session.key) is None
    assert len(store) == 0


async def test_session_ends_when_the_rechecked_principal_is_someone_else() -> None:
    clock, store = Clock(), MemorySessionStore()
    principals = Principals(PRINCIPAL.model_copy(update={"subject": "outra@empresa"}))
    auth = UiAuth(dev=_dev(), store=store, clock=clock, principals=principals)
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    assert await auth.session_by_key(session.key) is None
    assert len(store) == 0

    principals.current = PRINCIPAL.model_copy(update={"company_id": new_id()})
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    assert await auth.session_by_key(session.key) is None


async def test_recheck_failure_never_falls_back_to_the_old_principal() -> None:
    clock, store = Clock(), MemorySessionStore()
    principals = Principals(PRINCIPAL)
    auth = UiAuth(dev=_dev(), store=store, clock=clock, principals=principals)
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    principals.error = ConnectionError("banco fora do ar")
    with pytest.raises(ConnectionError):
        await auth.session_by_key(session.key)
    # A sessão não foi dada como conferida: a próxima tentativa consulta de novo.
    principals.error = None
    principals.current = None
    assert await auth.session_by_key(session.key) is None


async def test_client_id_from_login_is_kept_after_recheck() -> None:
    clock = Clock()
    with_client = PRINCIPAL.model_copy(update={"client_id": "rapa-ui"})
    auth = UiAuth(dev=_dev(), clock=clock, principals=Principals(PRINCIPAL))
    _, session = await auth.start_session(with_client, "Ana", "oidc")
    found = await auth.session_by_key(session.key)
    assert found is not None and found.principal == with_client


async def test_token_refresh_rechecks_access_with_the_new_token() -> None:
    keycloak, store = FakeKeycloak(), MemorySessionStore()
    resolver = Resolver(PRINCIPAL)
    auth = UiAuth(oidc=_oidc(keycloak, resolver), store=store)
    async with _client(auth) as client:
        await _oidc_login(client, keycloak)
    (session,) = store._sessions.values()
    assert session.tokens is not None

    # Renovou e a conta ainda tem acesso: papéis atualizados com o token novo.
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    resolver.principal = approver
    session.tokens.expires_at = time.time() + 5
    fresh = await auth.fresh_session(session.key)
    assert fresh is not None and fresh.access_token == "acesso-renovado-1"
    assert fresh.principal == approver
    assert resolver.calls[-1] == ({}, "acesso-renovado-1")

    # Tirada da organização no Keycloak: o token renovado não dá mais acesso.
    resolver.principal = None
    assert fresh.tokens is not None
    fresh.tokens.expires_at = time.time() + 5
    assert await auth.fresh_session(session.key) is None
    assert resolver.calls[-1] == ({}, "acesso-renovado-2")
    assert len(store) == 0


@pytest.mark.parametrize("problem", ["outra pessoa", "erro"])
async def test_token_refresh_ends_session_on_identity_change_or_error(problem: str) -> None:
    keycloak, store = FakeKeycloak(), MemorySessionStore()
    resolver = Resolver(PRINCIPAL)
    auth = UiAuth(oidc=_oidc(keycloak, resolver), store=store)
    async with _client(auth) as client:
        await _oidc_login(client, keycloak)
    (session,) = store._sessions.values()
    assert session.tokens is not None
    if problem == "erro":

        async def broken(claims: Mapping[str, Any], access_token: str) -> Principal | None:
            raise ConnectionError("banco fora do ar")

        resolver.resolve = broken  # type: ignore[method-assign]
    else:
        resolver.principal = PRINCIPAL.model_copy(update={"subject": "outra@empresa"})
    session.tokens.expires_at = time.time() + 5
    assert await auth.fresh_session(session.key) is None
    assert len(store) == 0


# ---------------------------------------------------------------------------
# Corridas: o que encerrou a sessão no meio de uma conferência continua valendo
# ---------------------------------------------------------------------------


class SlowPrincipals:
    """Resolvedor que lê o banco na hora e só responde quando ``release`` é acionado."""

    def __init__(self, principal: Principal | None) -> None:
        self.current = principal
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, subject: str, company_id: Any) -> Principal | None:
        answer = self.current  # lido antes da latência, como uma consulta ao banco
        self.entered.set()
        await self.release.wait()
        return answer


@pytest.mark.parametrize("how", ["end_session", "rota /auth/sair", "novo login"])
async def test_logout_during_the_principal_check_is_not_undone(how: str) -> None:
    clock, store = Clock(), MemorySessionStore()
    slow = SlowPrincipals(PRINCIPAL)
    auth = UiAuth(dev=_dev(), store=store, clock=clock, principals=slow)
    cookie, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    # O temporizador de uma aba começa a conferir o principal; o banco demora.
    poll = asyncio.create_task(auth.session_by_key(session.key))
    await slow.entered.wait()
    # Enquanto isso, a pessoa sai (ou entra de novo, o que encerra a sessão anterior).
    if how == "end_session":
        await auth.end_session(session.key)
    else:
        async with _client(auth) as client:
            client.cookies.set(SESSION_COOKIE, cookie, domain="rapa.test")
            if how == "rota /auth/sair":
                assert (await client.get("/auth/sair")).status_code == 303
            else:
                await client.get("/auth/vinculo")
                ticket = await auth.create_dev_ticket(client.cookies[BINDING_COOKIE], "ana", "/")
                done = await client.get("/auth/dev/concluir", params={"bilhete": ticket})
                assert done.status_code == 303
                assert client.cookies[SESSION_COOKIE] != cookie
    assert await store.load(session.key) is None
    slow.release.set()
    # A conferência que estava no meio não traz a sessão encerrada de volta.
    assert await poll is None
    assert await store.load(session.key) is None
    clock.now += timedelta(seconds=1)
    assert await auth.session_by_key(session.key) is None


async def test_revocation_seen_by_another_request_is_not_undone() -> None:
    clock, store = Clock(), MemorySessionStore()
    slow = SlowPrincipals(PRINCIPAL)
    auth = UiAuth(dev=_dev(), store=store, clock=clock, principals=slow)
    _, session = await auth.start_session(PRINCIPAL, "Ana", "dev")
    first = asyncio.create_task(auth.session_by_key(session.key))  # leu "conta ativa"
    await slow.entered.wait()
    # O administrador desativa a conta; outra aba confere depois, com uma consulta mais
    # rápida (outra réplica, por exemplo), vê a conta sem acesso e encerra a sessão.
    clock.now += timedelta(seconds=1)
    other = UiAuth(dev=_dev(), store=store, clock=clock, principals=Principals(None))
    assert await other.session_by_key(session.key) is None
    assert await store.load(session.key) is None
    slow.release.set()
    assert await first is None
    clock.now += timedelta(seconds=1)
    assert await auth.session_by_key(session.key) is None
    assert len(store) == 0


async def test_an_older_principal_check_never_overwrites_a_newer_one() -> None:
    clock, store = Clock(), MemorySessionStore()
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    slow = SlowPrincipals(approver)
    auth = UiAuth(dev=_dev(), store=store, clock=clock, principals=slow)
    _, session = await auth.start_session(approver, "Ana", "dev")
    first = asyncio.create_task(auth.session_by_key(session.key))  # leu "aprovadora"
    await slow.entered.wait()
    # Ela perde o papel de aprovadora; outra aba confere depois e vê o papel novo.
    clock.now += timedelta(seconds=1)
    other = UiAuth(dev=_dev(), store=store, clock=clock, principals=Principals(PRINCIPAL))
    seen = await other.session_by_key(session.key)
    assert seen is not None and seen.principal == PRINCIPAL
    slow.release.set()
    # A conferência mais antiga termina depois, mas não devolve o papel tirado.
    late = await first
    assert late is not None and late.principal == PRINCIPAL
    stored = await store.load(session.key)
    assert stored is not None and stored.principal == PRINCIPAL
    assert stored.principal_checked_at == clock.now


async def test_logout_during_a_token_refresh_is_not_undone() -> None:
    keycloak, store = FakeKeycloak(), MemorySessionStore()
    oidc = _oidc(keycloak, Resolver(PRINCIPAL))
    auth = UiAuth(oidc=oidc, store=store)
    _, session = await auth.start_session(
        PRINCIPAL,
        "Ana",
        "oidc",
        TokenSet("acesso-1", refresh_token="renovacao-1", expires_at=time.time() + 5),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    recheck = oidc.recheck

    async def slow_recheck(tokens: TokenSet) -> Principal | None:
        entered.set()
        await release.wait()
        return await recheck(tokens)

    oidc.recheck = slow_recheck  # type: ignore[method-assign]
    refreshing = asyncio.create_task(auth.fresh_session(session.key))
    await entered.wait()
    await auth.end_session(session.key)
    release.set()
    assert await refreshing is None
    assert len(store) == 0 and keycloak.refreshes == 1


async def test_token_refresh_keeps_a_newer_principal_check() -> None:
    clock, keycloak, store = Clock(), FakeKeycloak(), MemorySessionStore()
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    oidc = _oidc(keycloak, Resolver(approver))
    auth = UiAuth(oidc=oidc, store=store, clock=clock)
    _, session = await auth.start_session(
        approver,
        "Ana",
        "oidc",
        TokenSet("acesso-1", refresh_token="renovacao-1", expires_at=time.time() + 5),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    recheck = oidc.recheck

    async def slow_recheck(tokens: TokenSet) -> Principal | None:
        answer = await recheck(tokens)  # leu "aprovadora"
        entered.set()
        await release.wait()
        return answer

    oidc.recheck = slow_recheck  # type: ignore[method-assign]
    refreshing = asyncio.create_task(auth.fresh_session(session.key))
    await entered.wait()
    # Durante a renovação, outra aba confere no banco e vê o papel tirado.
    clock.now += timedelta(seconds=1)
    other = UiAuth(oidc=oidc, store=store, clock=clock, principals=Principals(PRINCIPAL))
    seen = await other.session_by_key(session.key)
    assert seen is not None and seen.principal == PRINCIPAL
    release.set()
    # Fica o principal da conferência mais nova, com os tokens renovados.
    fresh = await refreshing
    assert fresh is not None and fresh.principal == PRINCIPAL
    assert fresh.access_token == "acesso-renovado-1"
    stored = await store.load(session.key)
    assert stored is not None and stored.principal == PRINCIPAL
    assert stored.access_token == "acesso-renovado-1"


class RotatingKeycloak(FakeKeycloak):
    """Como o Keycloak com rotação: cada renovação devolve um token de renovação novo."""

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        response = super().handler(request)
        if request.url.path.endswith("/token") and self.token_requests:
            form = self.token_requests[-1]
            if form["grant_type"] == ["refresh_token"]:
                body = response.json()
                body["refresh_token"] = f"renovacao-{self.refreshes + 1}"
                return httpx2.Response(200, json=body)
        return response


class GatedPrincipals:
    """Resolvedor do banco; com ``gate`` ligado, só responde depois de ``release``."""

    def __init__(self, principal: Principal | None) -> None:
        self.current = principal
        self.gate = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, subject: str, company_id: Any) -> Principal | None:
        answer = self.current  # lido antes da latência, como uma consulta ao banco
        if self.gate:
            self.entered.set()
            await self.release.wait()
        return answer


async def _refresh_overtakes_a_principal_check(
    oidc: OidcLogin, auth: UiAuth, principals: GatedPrincipals, clock: Clock, key: str
) -> UiSession | None:
    """Uma renovação começa (o provedor demora), uma conferência do principal começa
    depois (o banco demora), a renovação termina primeiro e a conferência por último.
    Devolve o que a renovação devolveu."""
    t0 = clock.now
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = oidc.refresh

    async def slow_refresh(tokens: TokenSet) -> TokenSet:
        entered.set()
        await release.wait()
        return await refresh(tokens)

    oidc.refresh = slow_refresh  # type: ignore[method-assign]
    # 25 s depois da última conferência, uma ação renova o token (a conferência não vence).
    clock.now = t0 + timedelta(seconds=25)
    renewing = asyncio.create_task(auth.fresh_session(key))
    await entered.wait()
    # Aos 31 s, o temporizador de outra aba confere o principal no banco.
    clock.now = t0 + timedelta(seconds=31)
    principals.gate = True
    checking = asyncio.create_task(auth.session_by_key(key))
    await principals.entered.wait()
    release.set()
    renewed = await renewing
    principals.release.set()
    await checking
    principals.gate = False
    oidc.refresh = refresh  # type: ignore[method-assign]
    return renewed


async def test_principal_check_never_reverts_rotated_tokens() -> None:
    """A conferência do principal que começou antes do fim de uma renovação não devolve
    à sessão os tokens velhos: o provedor já girou o token de renovação."""
    clock, keycloak, store = Clock(), RotatingKeycloak(), MemorySessionStore()
    oidc = _oidc(keycloak, Resolver(PRINCIPAL))
    principals = GatedPrincipals(PRINCIPAL)
    auth = UiAuth(oidc=oidc, store=store, clock=clock, principals=principals)
    _, session = await auth.start_session(
        PRINCIPAL,
        "Ana",
        "oidc",
        TokenSet("acesso-0", refresh_token="renovacao-0", expires_at=time.time() + 5),
    )
    assert await auth.session_by_key(session.key) is not None  # primeira conferência
    renewed = await _refresh_overtakes_a_principal_check(oidc, auth, principals, clock, session.key)
    assert renewed is not None and renewed.access_token == "acesso-renovado-1"
    stored = await store.load(session.key)
    assert stored is not None and stored.tokens is not None
    assert stored.access_token == "acesso-renovado-1"
    assert stored.tokens.refresh_token == "renovacao-2"
    # A conferência, mais nova que a da renovação, fica com a hora dela.
    assert stored.principal == PRINCIPAL and stored.principal_checked_at == clock.now

    # A próxima ação não renova de novo com o token de renovação já gasto.
    clock.now += timedelta(seconds=1)
    fresh = await auth.fresh_session(session.key)
    assert fresh is not None and fresh.access_token == "acesso-renovado-1"
    used = [
        form["refresh_token"][0]
        for form in keycloak.token_requests
        if form["grant_type"] == ["refresh_token"]
    ]
    assert used == ["renovacao-0"]


async def test_principal_check_after_a_refresh_keeps_the_renewed_tokens() -> None:
    """Sem conferência atrasada: a conferência que vence logo depois da renovação só
    troca o principal e a hora, e os tokens renovados ficam."""
    clock, keycloak, store = Clock(), RotatingKeycloak(), MemorySessionStore()
    oidc = _oidc(keycloak, Resolver(PRINCIPAL))
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    principals = GatedPrincipals(PRINCIPAL)
    auth = UiAuth(oidc=oidc, store=store, clock=clock, principals=principals)
    _, session = await auth.start_session(
        PRINCIPAL,
        "Ana",
        "oidc",
        TokenSet("acesso-0", refresh_token="renovacao-0", expires_at=time.time() + 5),
    )
    renewed = await auth.fresh_session(session.key)
    assert renewed is not None and renewed.access_token == "acesso-renovado-1"
    principals.current = approver
    clock.now += timedelta(seconds=31)
    seen = await auth.session_by_key(session.key)
    assert seen is not None and seen.principal == approver
    assert seen.access_token == "acesso-renovado-1"
    assert seen.tokens is not None and seen.tokens.refresh_token == "renovacao-2"


async def test_role_removed_after_a_refresh_started_stays_removed() -> None:
    """A renovação lê "aprovadora" no provedor; depois a conferência no banco vê o papel
    tirado e grava; a renovação termina por último e não devolve o papel."""
    clock, keycloak, store = Clock(), RotatingKeycloak(), MemorySessionStore()
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    oidc = _oidc(keycloak, Resolver(approver))
    principals = GatedPrincipals(approver)
    auth = UiAuth(oidc=oidc, store=store, clock=clock, principals=principals)
    _, session = await auth.start_session(
        approver,
        "Ana",
        "oidc",
        TokenSet("acesso-0", refresh_token="renovacao-0", expires_at=time.time() + 5),
    )
    t0 = clock.now
    await auth.session_by_key(session.key)
    entered, release = asyncio.Event(), asyncio.Event()
    recheck = oidc.recheck

    async def slow_recheck(tokens: TokenSet) -> Principal | None:
        answer = await recheck(tokens)  # leu "aprovadora"
        entered.set()
        await release.wait()
        return answer

    oidc.recheck = slow_recheck  # type: ignore[method-assign]
    clock.now = t0 + timedelta(seconds=25)
    renewing = asyncio.create_task(auth.fresh_session(session.key))
    await entered.wait()
    clock.now = t0 + timedelta(seconds=31)
    principals.current = PRINCIPAL  # papel tirado no banco
    seen = await auth.session_by_key(session.key)
    assert seen is not None and seen.principal == PRINCIPAL
    release.set()
    fresh = await renewing
    assert fresh is not None and fresh.principal == PRINCIPAL
    assert fresh.access_token == "acesso-renovado-1"
    stored = await store.load(session.key)
    assert stored is not None and stored.principal == PRINCIPAL
    assert stored.principal_checked_at == t0 + timedelta(seconds=31)
    assert stored.access_token == "acesso-renovado-1"
    assert stored.tokens is not None and stored.tokens.refresh_token == "renovacao-2"


async def test_memory_store_writes_only_their_fields_and_never_bring_back_a_session() -> None:
    clock, store = Clock(), MemorySessionStore()
    auth = UiAuth(dev=_dev(), store=store, clock=clock)
    tokens = TokenSet("acesso-1", refresh_token="renovacao-1")
    _, session = await auth.start_session(PRINCIPAL, "Ana", "oidc", tokens)
    approver = PRINCIPAL.model_copy(update={"company_roles": frozenset({Role.APPROVER})})
    earlier, later = clock.now - timedelta(seconds=1), clock.now + timedelta(seconds=1)

    checked = await store.update_principal(session.key, approver, clock.now)
    assert checked is not None and checked.principal == approver
    assert checked.principal_checked_at == clock.now
    # A conferência do principal não mexe nos tokens.
    assert checked.tokens is tokens
    # Uma conferência de antes dessa (outra requisição) não passa por cima dela.
    assert await store.update_principal(session.key, PRINCIPAL, earlier) is checked
    assert await store.load(session.key) is checked

    # A renovação grava os tokens, mas não troca um principal conferido depois do começo dela.
    rotated = TokenSet("acesso-2", refresh_token="renovacao-2")
    renewed = await store.update_tokens(session.key, rotated, PRINCIPAL, earlier)
    assert renewed is not None and renewed.tokens is rotated
    assert renewed.principal == approver and renewed.principal_checked_at == clock.now

    # A mesma conferência e uma mais nova são gravadas, sem desfazer os tokens renovados.
    same = await store.update_principal(session.key, PRINCIPAL, clock.now)
    assert same is not None and same.principal == PRINCIPAL and same.tokens is rotated
    newer = await store.update_tokens(session.key, tokens, approver, later)
    assert newer is not None and newer.principal == approver
    assert newer.principal_checked_at == later and newer.tokens is tokens

    # O último uso só anda para a frente e não mexe no resto.
    touched = await store.touch(session.key, clock.now + timedelta(minutes=1))
    assert touched is not None and touched.last_seen == clock.now + timedelta(minutes=1)
    assert await store.touch(session.key, clock.now) is touched
    assert touched.principal == approver and touched.tokens is tokens
    # Quem guardou uma versão anterior não a vê mudar (como uma linha lida de uma tabela).
    assert session.principal == PRINCIPAL and session.principal_checked_at is None

    # Nenhuma gravação traz de volta uma sessão encerrada.
    await store.delete(session.key)
    assert await store.update_principal(session.key, approver, later) is None
    assert await store.update_tokens(session.key, rotated, approver, later) is None
    assert await store.touch(session.key, later) is None
    assert await store.load(session.key) is None and len(store) == 0
