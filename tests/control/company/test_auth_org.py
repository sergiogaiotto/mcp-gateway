"""A empresa pela organização guardada no banco (sem reinício) e o usuário criado no primeiro login."""

import secrets
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.auth import (
    AccessDenied,
    Authenticator,
    AuthUnavailable,
    CachedOrganizationDirectory,
    JwksClient,
    JwtSettings,
    TokenVerifier,
)
from rapa.control.db import Database
from rapa.control.services import PgCompanyAdmin, PgOrganizationDirectory, PgPrincipalResolver
from rapa.control.services.lifecycle import create_company
from rapa.core.ids import new_id
from tests.control.api.test_auth import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    Clock,
    FakeJwks,
    Keys,
    _jwk,
    claims,
    sign,
)

from .support import audit_actions, freeze

pytestmark = pytest.mark.database


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


@pytest.fixture
def fake(keys: Keys) -> FakeJwks:
    return FakeJwks(_jwk(keys.rsa, "rsa-1"))


def _verifier(
    fake: FakeJwks, directory: CachedOrganizationDirectory | None, **overrides: object
) -> TokenVerifier:
    settings = JwtSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL, **overrides)  # type: ignore[arg-type]
    jwks = JwksClient(
        settings.jwks_url,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler)),
    )
    return TokenVerifier(settings, jwks=jwks, organizations=directory)


async def _company(app_db: Database, organization: str, admin: str) -> UUID:
    return await create_company(
        app_db,
        name=f"Empresa {secrets.token_hex(3)}",
        organization=organization,
        admin_subject=admin,
        area_names=["Fiscal"],
        requested_by="teste",
    )


async def test_organization_from_the_database_without_restart(
    app_db: Database, keys: Keys, fake: FakeJwks
) -> None:
    tag = secrets.token_hex(4)
    keycloak_id = str(new_id())
    admin = f"admin-{tag}@empresa"
    clock = Clock()
    directory = CachedOrganizationDirectory(PgOrganizationDirectory(app_db), clock=clock)
    authenticator = Authenticator(_verifier(fake, directory), PgPrincipalResolver(app_db))
    token = sign(keys.rsa, claims(sub=admin, organization={f"empresa-{tag}": {"id": keycloak_id}}))
    # Antes da empresa existir: o id da organização não é empresa nenhuma.
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {token}")
    company = await _company(app_db, keycloak_id, admin)
    # O "não ligada" fica no cache por até 60 s; depois, a empresa nova vale, sem reiniciar.
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {token}")
    clock.now += 61
    principal = await authenticator.authenticate(f"Bearer {token}")
    assert principal.company_id == company and principal.subject == admin
    # Pelo apelido (a lista do Keycloak) também.
    alias = f"apelido-{tag}"
    other = await _company(app_db, alias, f"outra-{tag}@empresa")
    by_alias = await authenticator.verifier.verify(
        sign(keys.rsa, claims(sub="x", organization=[alias]))
    )
    assert (by_alias.company_id, by_alias.organization_linked) == (other, True)


async def test_static_map_and_database_must_agree(
    app_db: Database, keys: Keys, fake: FakeJwks
) -> None:
    tag = secrets.token_hex(4)
    alias = f"acme-{tag}"
    company = await _company(app_db, alias, f"ana-{tag}@acme")
    directory = CachedOrganizationDirectory(PgOrganizationDirectory(app_db))
    token = sign(keys.rsa, claims(organization=[alias]))
    # O mapa estático diz outra empresa: recusa.
    disagree = _verifier(fake, directory, organization_aliases={alias: new_id()})
    with pytest.raises(AccessDenied):
        await disagree.verify(token)
    # O mesmo nos dois: aceita.
    agree = _verifier(fake, directory, organization_aliases={alias: company})
    assert (await agree.verify(token)).company_id == company
    # O id do objeto do Keycloak ligado a uma empresa e o apelido a outra: recusa.
    keycloak_id = str(new_id())
    second = await _company(app_db, keycloak_id, f"bia-{tag}@acme")
    assert second != company
    mixed = sign(keys.rsa, claims(organization={alias: {"id": keycloak_id}}))
    with pytest.raises(AccessDenied):
        await _verifier(fake, directory).verify(mixed)


async def test_an_alias_never_reaches_the_company_of_an_organization_id(
    app_db: Database, keys: Keys, fake: FakeJwks
) -> None:
    """O id do token só como id, o apelido só como apelido (o formato de UUID é o de um id)."""
    tag = secrets.token_hex(4)
    keycloak_id = str(new_id())
    # Guardado na forma canônica (a do token), mesmo se digitado em maiúsculas.
    company = await _company(app_db, keycloak_id.upper(), f"dona-{tag}@empresa")
    verifier = _verifier(fake, CachedOrganizationDirectory(PgOrganizationDirectory(app_db)))
    own = await verifier.verify(
        sign(keys.rsa, claims(organization={f"empresa-{tag}": {"id": keycloak_id}}))
    )
    assert (own.company_id, own.organization_linked) == (company, True)
    # Outra organização cujo apelido é o id da primeira nunca chega à empresa dela.
    for organization in (
        {keycloak_id: {"id": str(new_id())}},
        {keycloak_id.upper(): {"id": str(new_id())}},
    ):
        other = await verifier.verify(sign(keys.rsa, claims(organization=organization)))
        assert other.company_id != company and not other.organization_linked
    for organization in ([keycloak_id], [keycloak_id.replace("-", "")]):
        with pytest.raises(AccessDenied):
            await verifier.verify(sign(keys.rsa, claims(organization=organization)))


async def test_database_down_fails_closed(keys: Keys, fake: FakeJwks) -> None:
    class Broken:
        async def company_for(self, organization: str) -> UUID | None:
            raise ConnectionError("banco fora do ar")

    verifier = _verifier(fake, CachedOrganizationDirectory(Broken()))
    with pytest.raises(AuthUnavailable):
        await verifier.verify(sign(keys.rsa, claims(organization=["qualquer"])))


async def test_first_login_creates_the_user_without_roles(
    app_db: Database, superuser_engine: AsyncEngine, keys: Keys, fake: FakeJwks
) -> None:
    tag = secrets.token_hex(4)
    alias = f"jit-{tag}"
    company = await _company(app_db, alias, f"admin-{tag}@jit")
    resolver = PgPrincipalResolver(app_db)
    directory = CachedOrganizationDirectory(PgOrganizationDirectory(app_db))
    authenticator = Authenticator(_verifier(fake, directory), resolver, provisioner=resolver)
    newcomer = f"nova-{tag}@jit"
    long_name = "Maria " + "x" * 300
    token = sign(keys.rsa, claims(sub=newcomer, organization=[alias], name=long_name))
    principal = await authenticator.authenticate(f"Bearer {token}")
    assert principal.company_id == company and principal.subject == newcomer
    assert principal.company_roles == frozenset() and principal.area_roles == {}
    async with superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT display_name, active FROM users WHERE subject = :s"), {"s": newcomer}
            )
        ).one()
    assert row[1] is True and len(row[0]) == 200 and row[0].startswith("Maria x")
    actions = await audit_actions(superuser_engine, company)
    assert actions.count("usuario.criado_no_login") == 1
    # O segundo login não cria de novo.
    again = await authenticator.authenticate(f"Bearer {token}")
    assert again.user_id == principal.user_id
    assert (await audit_actions(superuser_engine, company)).count("usuario.criado_no_login") == 1
    # Desativado continua sem entrar (e nada é criado de novo).
    admin = await resolver.resolve(f"admin-{tag}@jit", company)
    assert admin is not None and principal.user_id is not None
    await PgCompanyAdmin(app_db).deactivate_user(admin, principal.user_id)
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {token}")
    assert (await audit_actions(superuser_engine, company)).count("usuario.criado_no_login") == 1


async def test_first_login_is_refused_where_it_should_be(
    app_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    keys: Keys,
    fake: FakeJwks,
) -> None:
    tag = secrets.token_hex(4)
    alias = f"jit2-{tag}"
    company = await _company(app_db, alias, f"admin-{tag}@jit2")
    resolver = PgPrincipalResolver(app_db)
    directory = CachedOrganizationDirectory(PgOrganizationDirectory(app_db))
    authenticator = Authenticator(_verifier(fake, directory), resolver, provisioner=resolver)

    async def users() -> int:
        async with superuser_engine.connect() as conn:
            return int(
                await conn.scalar(
                    text("SELECT count(*) FROM users WHERE company_id = :c"), {"c": company}
                )
                or 0
            )

    start = await users()
    # Sem vínculo explícito (o id do objeto do Keycloak usado direto como empresa): não cria.
    direct = sign(
        keys.rsa, claims(sub=f"direto-{tag}@x", organization={"sem-vinculo": {"id": str(company)}})
    )
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {direct}")
    # Sujeito que já é de outra empresa: o sub é único na plataforma.
    taken = sign(keys.rsa, claims(sub=f"admin-{tag}@jit2", organization=[alias]))
    other_alias = f"outra-{tag}"
    await _company(app_db, other_alias, f"chefe-{tag}@outra")
    cross = sign(keys.rsa, claims(sub=f"admin-{tag}@jit2", organization=[other_alias]))
    assert (await authenticator.authenticate(f"Bearer {taken}")).company_id == company
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {cross}")
    # Empresa congelada não ganha usuário novo (mas quem já existe entra para ler).
    await freeze(owner_engine, company)
    frozen = sign(keys.rsa, claims(sub=f"tarde-{tag}@jit2", organization=[alias]))
    with pytest.raises(AccessDenied):
        await authenticator.authenticate(f"Bearer {frozen}")
    assert (await authenticator.authenticate(f"Bearer {taken}")).company_id == company
    assert await users() == start
    # Sem o criador de usuários (tokens de desenvolvimento, por exemplo), nada é criado.
    plain = Authenticator(_verifier(fake, directory), resolver)
    with pytest.raises(AccessDenied):
        await plain.authenticate(
            f"Bearer {sign(keys.rsa, claims(sub=f'mais-{tag}@x', organization=[alias]))}"
        )


async def test_lookup_function_is_the_only_way_across_companies(
    app_db: Database, dispatcher_db: Database
) -> None:
    tag = secrets.token_hex(4)
    company = await _company(app_db, f"func-{tag}", f"admin-{tag}@func")
    async with app_db.dispatcher_session() as session:
        found = await session.scalar(
            text("SELECT rapa_company_for_organization(:o)"), {"o": f"func-{tag}"}
        )
        assert found == company
        # A marca do dono não serve à aplicação: sem empresa, ela não vê empresa nenhuma.
        await session.execute(text("SELECT set_config('rapa.organization_lookup', 'on', true)"))
        assert await session.scalar(text("SELECT count(*) FROM companies")) == 0
    # A função desliga a marca antes de voltar.
    async with app_db.dispatcher_session() as session:
        await session.scalar(text("SELECT rapa_company_for_organization('x')"))
        assert (
            await session.scalar(text("SELECT current_setting('rapa.organization_lookup', true)"))
            or ""
        ) == ""
    # O despachante não chama a função.
    with pytest.raises(DBAPIError) as denied:
        async with dispatcher_db.dispatcher_session() as session:
            await session.execute(text("SELECT rapa_company_for_organization('x')"))
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"


def test_server_reads_organizations_from_the_database() -> None:
    """Com os tokens do Keycloak, o serviço liga o vínculo do banco (com o cache) ao verificador."""
    from rapa.server import ServerSettings, _verifier

    settings = ServerSettings.from_env(
        {
            "RAPA_DATABASE_URL": "postgresql://rapa_app:x@127.0.0.1:1/rapa",
            "RAPA_JWT_ISSUER": ISSUER,
            "RAPA_JWT_AUDIENCE": AUDIENCE,
            "RAPA_JWKS_URL": JWKS_URL,
            "RAPA_UI": "0",
        }
    )
    verifier = _verifier(settings, Database.from_url(settings.database_url))
    assert isinstance(verifier, TokenVerifier)
    assert isinstance(verifier.organizations, CachedOrganizationDirectory)
    assert isinstance(verifier.organizations.inner, PgOrganizationDirectory)
    assert verifier.organizations.ttl_s == 60
