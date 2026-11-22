"""REST do conserto sobre o PostgreSQL: ``/v1/execucoes/{id}/conserto``."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.auth import StaticTokenVerifier
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.services import PgPlatform, PgRepairs
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    NotFound,
    RepairService,
    RepairSuggestionStatus,
)
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_USER
from rapa.llm.repair import RepairAnswer
from rapa.ui.backend import RestBackend
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import (
    Tenant,
    failed_run,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
)
from tests.control.repairs.test_service import FakeAssistant
from tests.repair.support import login_page

pytestmark = pytest.mark.database

TOKENS = {
    "creator": "token-criadora",
    "approver": "token-aprovador",
    "operator": "token-operadora",
    "outsider": "token-vizinho",
    "other_creator": "token-criadora-b",
    "other_admin": "token-admin-b",
}


@dataclass
class Api:
    a: Tenant
    b: Tenant
    client: httpx2.AsyncClient
    run_a: UUID
    run_b: UUID
    assistant: FakeAssistant


def _app(a: Tenant, b: Tenant, platform: PgPlatform, repairs: RepairService | None) -> Any:
    people = {
        TOKENS["creator"]: a.creator,
        TOKENS["approver"]: a.approver,
        TOKENS["operator"]: a.operator,
        TOKENS["outsider"]: a.outsider,
        TOKENS["other_creator"]: b.creator,
        TOKENS["other_admin"]: b.admin,
    }
    return create_http_app(
        catalog=platform,
        runs=platform,
        admin=platform,
        verifier=StaticTokenVerifier(people),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        repairs=repairs,
    )


@pytest.fixture
async def api(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> AsyncIterator[Api]:
    await reset_queue(superuser_engine, total_slots=10)
    platform = PgPlatform(app_db)
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    runs = []
    for tenant in (a, b):
        await publish(platform, tenant, protocolo())
        runs.append(
            await failed_run(
                app_db, dispatcher_db, tenant, snapshot=login_page().model_dump(mode="json")
            )
        )
    assistant = FakeAssistant(*[RepairAnswer(candidate_id="c1", reason="Login.")] * 4)
    repairs = PgRepairs(app_db, assistant=assistant)
    async with asgi_client(_app(a, b, platform, repairs)) as client:
        yield Api(a, b, client, runs[0], runs[1], assistant)


async def test_get_and_create_with_permissions(api: Api) -> None:
    path = f"/v1/execucoes/{api.run_a}/conserto"
    got = await api.client.get(path, headers=auth(TOKENS["creator"]))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["step"]["step_id"] == "entrar"
    assert [c["candidate_id"] for c in body["candidates"]] == ["c1", "c2", "c3"]
    assert body["suggestion"] == {
        "status": "escolhido",
        "candidate_id": "c1",
        "reason": "Login.",
        "message": body["suggestion"]["message"],
    }
    for secret in (DEFAULT_PASSWORD, DEFAULT_USER):
        assert secret not in got.text
    quiet = await api.client.get(f"{path}?sugestao=false", headers=auth(TOKENS["approver"]))
    assert quiet.json()["suggestion"]["status"] == "desligado"

    # Só o papel de criação pede; quem aprova e quem opera, não.
    for token in ("approver", "operator"):
        refused = await api.client.post(path, json={"candidato": "c1"}, headers=auth(TOKENS[token]))
        assert refused.status_code == 403, token
    assert (await api.client.get(path, headers=auth(TOKENS["operator"]))).status_code == 403
    bad = await api.client.post(path, json={"candidato": "x"}, headers=auth(TOKENS["creator"]))
    assert bad.status_code == 422
    extra = await api.client.post(
        path, json={"candidato": "c1", "manifesto": {}}, headers=auth(TOKENS["creator"])
    )
    assert extra.status_code == 422
    created = await api.client.post(path, json={"candidato": "c1"}, headers=auth(TOKENS["creator"]))
    assert created.status_code == 201, created.text
    proposal = created.json()
    assert proposal["version_no"] == 2 and proposal["model_candidate_id"] == "c1"
    location = created.headers["location"]
    assert location == f"/v1/robos/{proposal['robot_id']}/versoes/2"
    again = await api.client.post(path, json={"candidato": "c2"}, headers=auth(TOKENS["creator"]))
    assert again.status_code == 409

    # Quem pediu valida, mas não publica; quem aprova publica.
    validated = await api.client.post(f"{location}/validar", headers=auth(TOKENS["creator"]))
    assert validated.status_code == 200, validated.text
    own = await api.client.post(f"{location}/publicar", headers=auth(TOKENS["creator"]))
    assert own.status_code == 403
    published = await api.client.post(f"{location}/publicar", headers=auth(TOKENS["approver"]))
    assert published.status_code == 200, published.text
    assert published.json()["approved_by"] == api.a.approver.subject


async def test_other_area_and_other_company_see_nothing(api: Api) -> None:
    for token, run_id in (
        ("outsider", api.run_a),
        ("other_creator", api.run_a),
        ("other_admin", api.run_a),
        ("creator", api.run_b),
    ):
        path = f"/v1/execucoes/{run_id}/conserto"
        got = await api.client.get(path, headers=auth(TOKENS[token]))
        assert got.status_code == 404, token
        posted = await api.client.post(path, json={"candidato": "c1"}, headers=auth(TOKENS[token]))
        assert posted.status_code == 404, token
    # A empresa B conserta a própria execução.
    own = await api.client.get(
        f"/v1/execucoes/{api.run_b}/conserto", headers=auth(TOKENS["other_creator"])
    )
    assert own.status_code == 200
    assert (await api.client.get(f"/v1/execucoes/{api.run_a}/conserto")).status_code == 401


async def test_without_the_service_it_is_503(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    tenant = await make_tenant(app_db, "Sem conserto")
    platform = PgPlatform(app_db)
    async with asgi_client(_app(tenant, tenant, platform, None)) as client:
        got = await client.get(
            f"/v1/execucoes/{UUID(int=5)}/conserto", headers=auth(TOKENS["creator"])
        )
    assert got.status_code == 503


async def test_rest_backend_of_the_interface(api: Api) -> None:
    backend = RestBackend(api.client, TOKENS["creator"])
    view = await backend.get_repair(api.run_a, suggest=False)
    assert view.suggestion.status is RepairSuggestionStatus.OFF
    assert view.candidates[0].name == "Acessar"
    proposal = await backend.create_repair(api.run_a, view.candidates[0].candidate_id)
    assert proposal.version_no == 2 and proposal.model_candidate_id is None
    with pytest.raises(Conflict):
        await backend.create_repair(api.run_a, "c1")
    with pytest.raises(NotFound):
        await backend.get_repair(api.run_b)
    with pytest.raises(Forbidden):
        await RestBackend(api.client, TOKENS["operator"]).get_repair(api.run_a)
