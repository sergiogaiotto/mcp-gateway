"""REST de ``/v1/empresa`` sobre o PostgreSQL, e o RestBackend da interface sobre ele."""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.auth import StaticTokenVerifier
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.services import PgCompanyAdmin, PgPlatform
from rapa.core.contracts import (
    CompanyFrozen,
    CompanyStatus,
    Forbidden,
    Principal,
    Role,
    RoleGrant,
    VisibleNames,
)
from rapa.ui.backend import RestBackend
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import Tenant, make_tenant

from .support import freeze, person

pytestmark = pytest.mark.database

TOKENS = {
    "admin": "token-admin-a",
    "operator": "token-operador-a",
    "boss": "token-chefe-a",
    "auditor": "token-auditora-a",
    "other": "token-admin-b",
}


@dataclass
class Api:
    a: Tenant
    b: Tenant
    boss: Principal
    client: httpx2.AsyncClient
    app: Any


def _app(people: dict[str, Principal], company: PgCompanyAdmin | None, platform: PgPlatform) -> Any:
    return create_http_app(
        catalog=platform,
        runs=platform,
        admin=platform,
        verifier=StaticTokenVerifier({TOKENS[k]: p for k, p in people.items()}),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        company=company,
    )


@pytest.fixture
async def api(app_db: Database) -> AsyncIterator[Api]:
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    tag = secrets.token_hex(4)
    boss = await person(app_db, a.company_id, f"chefe-{tag}@a", (Role.AREA_ADMIN, a.area_id))
    auditor = await person(app_db, a.company_id, f"auditora-{tag}@a", (Role.AUDITOR, None))
    people = {
        "admin": a.admin,
        "operator": a.operator,
        "boss": boss,
        "auditor": auditor,
        "other": b.admin,
    }
    app = _app(people, PgCompanyAdmin(app_db), PgPlatform(app_db))
    async with asgi_client(app) as client:
        yield Api(a, b, boss, client, app)


def _h(who: str) -> dict[str, str]:
    return auth(TOKENS[who])


async def test_company_users_and_roles(api: Api) -> None:
    client, a = api.client, api.a
    got = await client.get("/v1/empresa", headers=_h("operator"))
    assert got.status_code == 200
    assert got.json()["name"] == "Empresa A" and got.json()["status"] == "active"
    users = await client.get("/v1/empresa/usuarios", headers=_h("admin"))
    assert users.status_code == 200
    operator = next(u for u in users.json() if u["subject"] == a.operator.subject)
    assert (await client.get("/v1/empresa/usuarios", headers=_h("operator"))).status_code == 403
    path = f"/v1/empresa/usuarios/{operator['user_id']}/papeis"
    body = {"papeis": [{"papel": "creator", "area_id": str(a.other_area_id)}, {"papel": "viewer"}]}
    changed = await client.put(path, json=body, headers=_h("admin"))
    assert changed.status_code == 200, changed.text
    assert {(r["role"], r["area_id"]) for r in changed.json()["roles"]} == {
        ("creator", str(a.other_area_id)),
        ("viewer", None),
    }
    # Quem administra só uma área: não dá papel em outra (403); corpo inválido: 422.
    refused = await client.put(
        path,
        json={"papeis": [{"papel": "operator", "area_id": str(a.other_area_id)}]},
        headers=_h("boss"),
    )
    assert refused.status_code == 403
    for bad in ({"papeis": [{"papel": "chefe"}]}, {"papeis": [], "extra": 1}):
        assert (await client.put(path, json=bad, headers=_h("admin"))).status_code == 422
    # Usuário de outra empresa não existe aqui.
    stranger = (await client.get("/v1/empresa/usuarios", headers=_h("other"))).json()[0]
    missing = await client.put(
        f"/v1/empresa/usuarios/{stranger['user_id']}/papeis",
        json={"papeis": []},
        headers=_h("admin"),
    )
    assert missing.status_code == 404
    # Desativar e reativar; a si mesmo, não.
    off = await client.post(
        f"/v1/empresa/usuarios/{operator['user_id']}/desativar", headers=_h("admin")
    )
    assert off.status_code == 200 and off.json()["active"] is False
    on = await client.post(
        f"/v1/empresa/usuarios/{operator['user_id']}/reativar", headers=_h("admin")
    )
    assert on.json()["active"] is True
    me = next(u for u in users.json() if u["subject"] == a.admin.subject)
    myself = await client.post(
        f"/v1/empresa/usuarios/{me['user_id']}/desativar", headers=_h("admin")
    )
    assert myself.status_code == 409
    assert myself.json()["erro"] == "conflito"


async def test_areas_and_usage(api: Api) -> None:
    client = api.client
    created = await client.post("/v1/empresa/areas", json={"nome": "Compras"}, headers=_h("admin"))
    assert created.status_code == 201, created.text
    area = created.json()
    assert created.headers["location"] == f"/v1/empresa/areas/{area['area_id']}"
    again = await client.post("/v1/empresa/areas", json={"nome": "Compras"}, headers=_h("admin"))
    assert again.status_code == 409
    renamed = await client.patch(
        f"/v1/empresa/areas/{area['area_id']}", json={"nome": "Suprimentos"}, headers=_h("admin")
    )
    assert renamed.json()["name"] == "Suprimentos"
    assert (
        await client.post("/v1/empresa/areas", json={"nome": "X"}, headers=_h("boss"))
    ).status_code == 403
    names = {a["name"] for a in (await client.get("/v1/empresa/areas", headers=_h("admin"))).json()}
    assert "Suprimentos" in names
    for who in ("admin", "auditor"):
        usage = await client.get("/v1/empresa/consumo?mes=2026-11", headers=_h(who))
        assert usage.status_code == 200, usage.text
        assert usage.json()["month"] == "2026-11"
    current = await client.get("/v1/empresa/consumo", headers=_h("auditor"))
    assert current.status_code == 200
    assert (await client.get("/v1/empresa/consumo", headers=_h("operator"))).status_code == 403
    bad = await client.get("/v1/empresa/consumo?mes=2026-13", headers=_h("admin"))
    assert bad.status_code == 422


async def test_whoami_brings_the_names_the_caller_can_see(api: Api, app_db: Database) -> None:
    """``GET /v1/eu`` traz o nome da empresa e os das áreas que a pessoa enxerga.

    Com papel numa área, só ela; com papel na empresa, todas; nunca as de outra empresa
    (a empresa B tem uma área com nome próprio, que não aparece para ninguém da A).
    """
    a, b = api.a, api.b
    await PgCompanyAdmin(app_db).rename_area(b.admin, b.other_area_id, "Tesouraria da B")
    operator = (await api.client.get("/v1/eu", headers=_h("operator"))).json()
    assert operator["subject"] == a.operator.subject
    assert operator["company_name"] == "Empresa A"
    assert operator["area_names"] == {str(a.area_id): "Fiscal"}
    admin = (await api.client.get("/v1/eu", headers=_h("admin"))).json()
    assert admin["company_name"] == "Empresa A"
    assert admin["area_names"] == {str(a.area_id): "Fiscal", str(a.other_area_id): "Jurídico"}
    other = (await api.client.get("/v1/eu", headers=_h("other"))).json()
    assert other["company_name"] == "Empresa B"
    assert other["area_names"] == {
        str(b.area_id): "Fiscal",
        str(b.other_area_id): "Tesouraria da B",
    }
    for who in ("admin", "operator", "boss", "auditor"):
        body = (await api.client.get("/v1/eu", headers=_h(who))).text
        assert "Tesouraria da B" not in body and "Empresa B" not in body
        assert str(b.area_id) not in body and str(b.company_id) not in body
    # A interface pelo REST lê os mesmos nomes.
    names = await RestBackend(api.client, TOKENS["operator"]).visible_names()
    assert names == VisibleNames(company_name="Empresa A", area_names={a.area_id: "Fiscal"})
    # Sem a administração da empresa no servidor, os nomes vêm vazios (o resto, igual).
    app = _app({"operator": a.operator}, None, PgPlatform(app_db))
    async with asgi_client(app) as client:
        bare = (await client.get("/v1/eu", headers=_h("operator"))).json()
    assert bare["subject"] == a.operator.subject
    assert bare["company_name"] is None and bare["area_names"] == {}


async def test_frozen_company_answers_423(api: Api, owner_engine: AsyncEngine) -> None:
    await freeze(owner_engine, api.a.company_id, "saída da empresa")
    response = await api.client.post(
        "/v1/empresa/areas", json={"nome": "Nova"}, headers=_h("admin")
    )
    assert response.status_code == 423
    body = response.json()
    assert body["erro"] == "empresa_congelada"
    assert body["mensagem"].startswith("a empresa está congelada")
    info = (await api.client.get("/v1/empresa", headers=_h("admin"))).json()
    assert info["status"] == "frozen" and info["status_reason"] == "saída da empresa"
    plain = (await api.client.get("/v1/empresa", headers=_h("operator"))).json()
    assert plain["status_reason"] is None


async def test_without_the_service_answers_503(api: Api, app_db: Database) -> None:
    app = _app({"admin": api.a.admin}, None, PgPlatform(app_db))
    async with asgi_client(app) as client:
        response = await client.get("/v1/empresa", headers=_h("admin"))
    assert response.status_code == 503


async def test_rest_backend_mirrors_the_service(api: Api, owner_engine: AsyncEngine) -> None:
    backend = RestBackend(api.client, TOKENS["admin"])
    info = await backend.get_company()
    assert info.company_id == api.a.company_id and info.status is CompanyStatus.ACTIVE
    users = await backend.list_company_users()
    operator = next(u for u in users if u.subject == api.a.operator.subject)
    changed = await backend.set_user_roles(
        operator.user_id, [RoleGrant(role=Role.APPROVER, area_id=api.a.area_id)]
    )
    assert [(g.role, g.area_id) for g in changed.roles] == [(Role.APPROVER, api.a.area_id)]
    assert not (await backend.deactivate_user(operator.user_id)).active
    assert (await backend.reactivate_user(operator.user_id)).active
    area = await backend.create_company_area("Logística")
    assert (await backend.rename_company_area(area.area_id, "Transporte")).name == "Transporte"
    assert {a.name for a in await backend.list_company_areas()} >= {"Fiscal", "Transporte"}
    assert (await backend.company_usage("2026-11")).month == "2026-11"
    with pytest.raises(Forbidden):
        await RestBackend(api.client, TOKENS["operator"]).company_usage()
    await freeze(owner_engine, api.a.company_id)
    with pytest.raises(CompanyFrozen):
        await backend.create_company_area("Outra")
