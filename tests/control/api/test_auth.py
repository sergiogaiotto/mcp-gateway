"""Verificação dos tokens do Keycloak com chaves geradas localmente e JWKS falso."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rapa.control.auth import (
    AccessDenied,
    Authenticator,
    AuthUnavailable,
    JwksClient,
    JwtSettings,
    StaticTokenVerifier,
    TokenVerifier,
    Unauthenticated,
    bearer_token,
)
from rapa.core.contracts import Role
from rapa.fixtures.memory import make_principal
from tests.control.api.support import (
    DictResolver,
    World,
    asgi_client,
    auth,
    make_app,
    mcp_client,
    running,
)

ISSUER = "https://sso.rapa.test/realms/rapa"
AUDIENCE = "https://rapa.rapa.test/mcp"
JWKS_URL = "https://sso.rapa.test/realms/rapa/protocol/openid-connect/certs"


@dataclass
class Keys:
    rsa: Any = field(default_factory=lambda: rsa.generate_private_key(65537, 2048))
    rsa_new: Any = field(default_factory=lambda: rsa.generate_private_key(65537, 2048))
    ec: Any = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


def _jwk(private_key: Any, kid: str) -> dict[str, Any]:
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        data: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(public, as_dict=True)
        data["alg"] = "RS256"
    else:
        data = jwt.algorithms.ECAlgorithm.to_jwk(public, as_dict=True)
        data["alg"] = "ES256"
    data.update({"kid": kid, "use": "sig"})
    return data


class FakeJwks:
    """Servidor de chaves falso (sem rede), contando as buscas."""

    def __init__(self, *jwks: dict[str, Any]) -> None:
        self.keys = list(jwks)
        self.calls = 0
        self.fail = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        assert str(request.url) == JWKS_URL
        assert "authorization" not in request.headers
        if self.fail:
            return httpx2.Response(503)
        # Chave de cifragem do Keycloak: precisa ser ignorada.
        enc = {
            "kid": "enc-1",
            "kty": "RSA",
            "use": "enc",
            "alg": "RSA-OAEP",
            "n": "AQAB",
            "e": "AQAB",
        }
        return httpx2.Response(200, json={"keys": [enc, *self.keys]})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hmac_token(payload: dict[str, Any], secret: bytes, *, kid: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    body = _b64(json.dumps(payload).encode())
    signature = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(signature)}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_verifier(fake: FakeJwks, clock: Clock | None = None, **overrides: Any) -> TokenVerifier:
    settings = JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL, **overrides)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler))
    jwks = JwksClient(
        settings.jwks_url,
        algorithms=settings.algorithms,
        cache_ttl_s=settings.jwks_cache_ttl_s,
        min_refresh_interval_s=settings.jwks_min_refresh_interval_s,
        http_client=client,
        clock=clock or time.monotonic,
    )
    return TokenVerifier(settings, jwks=jwks)


COMPANY = UUID("018f2b5e-0000-7000-8000-000000000001")
OTHER = UUID("018f2b5e-0000-7000-8000-000000000002")


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": ["account", AUDIENCE],
        "sub": "f3c1d2e4-usuario",
        "azp": "rapa-ui",
        "typ": "Bearer",
        "iat": now,
        "exp": now + 300,
        "organization": {"empresa-a": {"id": str(COMPANY)}},
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign(key: Any, payload: dict[str, Any], *, kid: str = "rsa-1", alg: str = "RS256") -> str:
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def fake(keys: Keys) -> FakeJwks:
    return FakeJwks(_jwk(keys.rsa, "rsa-1"), _jwk(keys.ec, "ec-1"))


# -- casos válidos ------------------------------------------------------------


async def test_valid_rs256_and_es256(keys: Keys, fake: FakeJwks) -> None:
    verifier = make_verifier(fake)
    verified = await verifier.verify(sign(keys.rsa, claims()))
    assert verified.subject == "f3c1d2e4-usuario"
    assert verified.company_id == COMPANY
    assert verified.client_id == "rapa-ui"
    assert verified.principal is None
    es = await verifier.verify(sign(keys.ec, claims(), kid="ec-1", alg="ES256"))
    assert es.company_id == COMPANY
    # As chaves ficam em cache: uma busca só.
    await verifier.verify(sign(keys.rsa, claims()))
    assert fake.calls == 1


async def test_list_of_aliases_uses_configured_mapping(keys: Keys, fake: FakeJwks) -> None:
    verifier = make_verifier(fake, organization_aliases={"empresa-a": COMPANY})
    verified = await verifier.verify(sign(keys.rsa, claims(organization=["empresa-a"])))
    assert verified.company_id == COMPANY
    with pytest.raises(AccessDenied):
        await verifier.verify(sign(keys.rsa, claims(organization=["desconhecida"])))
    # Identificador do token divergente do mapeamento configurado: recusa.
    divergent = claims(organization={"empresa-a": {"id": str(OTHER)}})
    with pytest.raises(AccessDenied):
        await verifier.verify(sign(keys.rsa, divergent))


async def test_leeway_accepts_recently_expired(keys: Keys, fake: FakeJwks) -> None:
    verifier = make_verifier(fake, leeway_s=30)
    now = int(time.time())
    verified = await verifier.verify(sign(keys.rsa, claims(iat=now - 600, exp=now - 10)))
    assert verified.subject


# -- casos inválidos (401) ----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"aud": "https://outro-servico.rapa.test"}, "outro destino"),
        ({"aud": ["account"]}, "outro destino"),
        ({"iss": "https://sso.rapa.test/realms/outro"}, "emissor"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expirado"),
        ({"nbf": int(time.time()) + 3600}, "ainda não é válido"),
        ({"sub": None}, "obrigatórias"),
        ({"exp": None}, "obrigatórias"),
        ({"typ": "ID"}, "token de acesso"),
        ({"typ": "Refresh"}, "token de acesso"),
    ],
)
async def test_invalid_claims_are_unauthenticated(
    keys: Keys, fake: FakeJwks, overrides: dict[str, Any], message: str
) -> None:
    verifier = make_verifier(fake, leeway_s=5)
    with pytest.raises(Unauthenticated) as error:
        await verifier.verify(sign(keys.rsa, claims(**overrides)))
    assert message in error.value.message
    assert error.value.status_code == 401


async def test_bad_signatures_and_algorithms(keys: Keys, fake: FakeJwks) -> None:
    verifier = make_verifier(fake)
    other_key = rsa.generate_private_key(65537, 2048)
    forged = sign(other_key, claims())
    public_pem = keys.rsa.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    tokens = [
        forged,
        jwt.encode(claims(), "segredo-qualquer" * 4, algorithm="HS256", headers={"kid": "rsa-1"}),
        # Confusão de algoritmo: HMAC com a chave pública como segredo.
        _hmac_token(claims(), public_pem, kid="rsa-1"),
        jwt.encode(claims(), None, algorithm="none", headers={"kid": "rsa-1"}),
        sign(keys.rsa, claims(), kid="ec-1"),  # kid de chave EC com alg RS256
        jwt.encode(claims(), keys.rsa, algorithm="RS512", headers={"kid": "rsa-1"}),
        jwt.encode(claims(), keys.rsa, algorithm="RS256"),  # sem kid
        "nao.e.jwt",
        "x" * 20_000,
    ]
    for token in tokens:
        with pytest.raises(Unauthenticated):
            await verifier.verify(token)


async def test_key_rotation_refetches_once(keys: Keys, fake: FakeJwks) -> None:
    clock = Clock()
    verifier = make_verifier(fake, clock=clock, jwks_min_refresh_interval_s=30)
    await verifier.verify(sign(keys.rsa, claims()))
    assert fake.calls == 1
    # O provedor gira a chave: um kid novo força uma busca.
    fake.keys.append(_jwk(keys.rsa_new, "rsa-2"))
    clock.now += 31
    rotated = await verifier.verify(sign(keys.rsa_new, claims(), kid="rsa-2"))
    assert rotated.company_id == COMPANY
    assert fake.calls == 2
    # Um kid desconhecido logo depois não derruba o provedor com buscas.
    for _ in range(5):
        with pytest.raises(Unauthenticated):
            await verifier.verify(sign(keys.rsa_new, claims(), kid="inventado"))
    assert fake.calls == 2
    # Chave removida do provedor deixa de valer quando o cache expira.
    fake.keys = [_jwk(keys.rsa_new, "rsa-2")]
    clock.now += 301
    with pytest.raises(Unauthenticated):
        await verifier.verify(sign(keys.rsa, claims()))
    assert fake.calls == 3


async def test_jwks_unavailable(keys: Keys, fake: FakeJwks) -> None:
    clock = Clock()
    fake.fail = True
    verifier = make_verifier(fake, clock=clock)
    with pytest.raises(AuthUnavailable) as error:
        await verifier.verify(sign(keys.rsa, claims()))
    assert error.value.status_code == 503
    # Com chaves já conhecidas, uma falha do provedor não derruba a verificação.
    fake.fail = False
    clock.now += 31
    await verifier.verify(sign(keys.rsa, claims()))
    fake.fail = True
    clock.now += 1000
    assert (await verifier.verify(sign(keys.rsa, claims()))).company_id == COMPANY


# -- empresa (403) ------------------------------------------------------------


@pytest.mark.parametrize(
    "organization",
    [
        None,
        [],
        {},
        ["empresa-a", "empresa-b"],
        {"empresa-a": {"id": str(COMPANY)}, "empresa-b": {"id": str(OTHER)}},
        {"empresa-a": {"id": "nao-e-uuid"}},
        {"empresa-a": {"id": 42}},
        42,
    ],
)
async def test_organization_must_be_exactly_one(
    keys: Keys, fake: FakeJwks, organization: Any
) -> None:
    verifier = make_verifier(fake, organization_aliases={"empresa-a": COMPANY})
    payload = claims(organization=organization)
    if organization is None:
        payload.pop("organization", None)
    with pytest.raises(AccessDenied) as error:
        await verifier.verify(sign(keys.rsa, payload))
    assert error.value.status_code == 403


# -- principal ----------------------------------------------------------------


async def test_authenticator_resolves_roles_from_database(keys: Keys, fake: FakeJwks) -> None:
    principal = make_principal(COMPANY, Role.OPERATOR, subject="f3c1d2e4-usuario")
    resolver = DictResolver({("f3c1d2e4-usuario", COMPANY): principal})
    authenticator = Authenticator(make_verifier(fake), resolver)
    # Papéis no token são ignorados: só valem os do banco.
    token = sign(keys.rsa, claims(realm_access={"roles": ["company_admin"]}))
    resolved = await authenticator.authenticate(f"Bearer {token}")
    assert resolved.company_roles == frozenset({Role.OPERATOR})
    assert resolved.client_id == "rapa-ui"
    assert resolver.calls == [("f3c1d2e4-usuario", COMPANY)]
    unknown = sign(keys.rsa, claims(sub="outra-pessoa"))
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {unknown}")


async def test_resolver_returning_other_company_is_denied(keys: Keys, fake: FakeJwks) -> None:
    wrong = make_principal(OTHER, Role.OPERATOR, subject="f3c1d2e4-usuario")
    resolver = DictResolver({("f3c1d2e4-usuario", COMPANY): wrong})
    authenticator = Authenticator(make_verifier(fake), resolver)
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {sign(keys.rsa, claims())}")


def test_bearer_header_parsing() -> None:
    assert bearer_token("Bearer abc.def-ghi_~+/=") == "abc.def-ghi_~+/="
    assert bearer_token("bearer abc") == "abc"
    for header in (None, "", "Bearer", "Bearer ", "Basic abc", "Bearer a b", "Bearerabc"):
        with pytest.raises(Unauthenticated):
            bearer_token(header)


def test_settings_refuse_weak_configuration() -> None:
    with pytest.raises(ValidationError):
        JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL, algorithms={"HS256"})
    with pytest.raises(ValidationError):
        JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url="http://sso.exemplo.com/certs")
    local = JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url="http://localhost:8080/c")
    assert local.algorithms == frozenset({"RS256", "ES256"})
    with pytest.raises(ValueError):
        StaticTokenVerifier({})
    with pytest.raises(ValueError):
        StaticTokenVerifier({"com espaço": make_principal(COMPANY)})


async def test_static_tokens() -> None:
    principal = make_principal(COMPANY, Role.VIEWER, subject="dev")
    verifier = StaticTokenVerifier({"token-dev": principal})
    assert (await verifier.verify("token-dev")).principal == principal
    with pytest.raises(Unauthenticated):
        await verifier.verify("token-devx")


# -- pela aplicação HTTP ------------------------------------------------------


@pytest.fixture
def jwt_world(world: World, keys: Keys, fake: FakeJwks) -> Iterator[tuple[TestClient, Any]]:
    subject_a = "sub-operadora-a"
    principal = world.operator_a.model_copy(update={"subject": subject_a})
    resolver = DictResolver({(subject_a, world.company_a): principal})
    app = make_app(
        world, verifier=make_verifier(fake), resolver=resolver, allow_static_tokens=False
    )

    def token(**overrides: Any) -> str:
        values: dict[str, Any] = {
            "sub": subject_a,
            "organization": {"empresa-a": {"id": str(world.company_a)}},
            **overrides,
        }
        return sign(keys.rsa, claims(**values))

    with TestClient(app) as client:
        yield client, token


def test_rest_with_keycloak_tokens(jwt_world: tuple[TestClient, Any], world: World) -> None:
    client, token = jwt_world
    ok = client.get("/v1/catalogo", headers=auth(token()))
    assert ok.status_code == 200
    assert {r["company_id"] for r in ok.json()} == {str(world.company_a)}

    wrong_audience = client.get("/v1/catalogo", headers=auth(token(aud="https://outro.rapa.test")))
    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["erro"] == "nao_autenticado"
    expired = client.get(
        "/v1/catalogo",
        headers=auth(token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)),
    )
    assert expired.status_code == 401
    two = {"empresa-a": {"id": str(world.company_a)}, "empresa-b": {"id": str(world.company_b)}}
    two_orgs = client.get("/v1/catalogo", headers=auth(token(organization=two)))
    assert two_orgs.status_code == 403
    assert two_orgs.json()["erro"] == "acesso_negado"
    unknown = client.get("/v1/catalogo", headers=auth(token(sub="quem-sera")))
    assert unknown.status_code == 403
    # Trocar a organização do token por outra empresa não dá acesso aos robôs dela.
    other = client.get(
        "/v1/catalogo",
        headers=auth(token(organization={"empresa-b": {"id": str(world.company_b)}})),
    )
    assert other.status_code == 403


async def test_mcp_with_keycloak_tokens(async_world: World, keys: Keys, fake: FakeJwks) -> None:
    subject_b = "sub-operador-b"
    principal = async_world.operator_b.model_copy(update={"subject": subject_b})
    resolver = DictResolver({(subject_b, async_world.company_b): principal})
    app = make_app(async_world, verifier=make_verifier(fake), resolver=resolver)
    org = {"empresa-b": {"id": str(async_world.company_b)}}
    good = sign(keys.rsa, claims(sub=subject_b, organization=org))
    bad = sign(keys.rsa, claims(sub=subject_b, organization=org, aud="https://outro.rapa.test"))
    two = {**org, "empresa-a": {"id": str(async_world.company_a)}}
    ambiguous = sign(keys.rsa, claims(sub=subject_b, organization=two))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    async with running(app):
        async with mcp_client(app, good) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        async with asgi_client(app, bad) as http:
            wrong_audience = await http.post("/mcp", json=body, headers=headers)
        async with asgi_client(app, ambiguous) as http:
            two_orgs = await http.post("/mcp", json=body, headers=headers)
    assert "consultar_saldo" in names and "baixar_notas" not in names
    assert wrong_audience.status_code == 401
    assert 'error="invalid_token"' in wrong_audience.headers["www-authenticate"]
    assert two_orgs.status_code == 403
    assert two_orgs.json()["erro"] == "acesso_negado"
