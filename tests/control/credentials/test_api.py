"""REST das credenciais e do certificado A1 sobre o PostgreSQL, e o ``RestBackend``."""

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
from rapa.core.contracts import (
    Conflict,
    CredentialService,
    CredentialStatus,
    Forbidden,
    NotFound,
    VersionStatus,
)
from rapa.core.robot import CredentialKind
from rapa.ui.backend import RestBackend
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import PROTOCOLO_INPUTS, reset_queue

from .support import (
    CERT,
    FINGERPRINT,
    ORIGIN,
    OTHER_FINGERPRINT,
    A1World,
    a1_world,
    draft_a1,
    in_days,
)

pytestmark = pytest.mark.database

TOKENS = {
    "admin": "token-adm-area",
    "custodian": "token-custodiante",
    "creator": "token-criadora",
    "approver": "token-aprovador",
    "approver2": "token-aprovadora",
    "operator": "token-operador",
    "outsider": "token-vizinho",
    "other": "token-outra-empresa",
}


@dataclass
class Api:
    a: A1World
    b: A1World
    client: httpx2.AsyncClient


def _app(a: A1World, b: A1World, credentials: CredentialService | None) -> Any:
    people = {
        TOKENS["admin"]: a.area_admin,
        TOKENS["custodian"]: a.custodian,
        TOKENS["creator"]: a.tenant.creator,
        TOKENS["approver"]: a.tenant.approver,
        TOKENS["approver2"]: a.approver2,
        TOKENS["operator"]: a.tenant.operator,
        TOKENS["outsider"]: a.tenant.outsider,
        TOKENS["other"]: b.tenant.admin,
    }
    return create_http_app(
        catalog=a.platform,
        runs=a.platform,
        admin=a.platform,
        verifier=StaticTokenVerifier(people),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        credentials=credentials,
    )


@pytest.fixture
async def api(app_db: Database, superuser_engine: AsyncEngine) -> AsyncIterator[Api]:
    await reset_queue(superuser_engine, total_slots=10)
    a = await a1_world(app_db, "API A")
    b = await a1_world(app_db, "API B")
    async with asgi_client(_app(a, b, a.credentials)) as client:
        yield Api(a, b, client)


def _register_body(world: A1World, **changes: Any) -> dict[str, Any]:
    body = {
        "area_id": str(world.tenant.area_id),
        "nome": CERT,
        "tipo": "pfx_a1",
        "origem": ORIGIN,
        "caminho_cofre": "empresas/a/certificado",
        "custodiante": world.custodian.subject,
        "validade": in_days(200).isoformat(),
        "impressao": FINGERPRINT,
    }
    body.update(changes)
    return body


async def test_register_list_renew_and_revoke(api: Api) -> None:
    client, world = api.client, api.a
    refused = await client.post(
        "/v1/credenciais", json=_register_body(world), headers=auth(TOKENS["creator"])
    )
    assert refused.status_code == 403
    for changes in ({"valor": "segredo"}, {"validade": "2030-01-01T00:00:00"}, {"tipo": "x"}):
        bad = await client.post(
            "/v1/credenciais", json=_register_body(world, **changes), headers=auth(TOKENS["admin"])
        )
        assert bad.status_code == 422, changes
    missing = await client.post(
        "/v1/credenciais",
        json=_register_body(world, custodiante=None),
        headers=auth(TOKENS["admin"]),
    )
    assert missing.status_code == 422
    assert "custodiante: obrigatório para o certificado A1" in missing.json()["detalhes"]
    created = await client.post(
        "/v1/credenciais", json=_register_body(world), headers=auth(TOKENS["admin"])
    )
    assert created.status_code == 201, created.text
    info = created.json()
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["location"] == f"/v1/credenciais/{info['credential_id']}"
    assert info["vault_path"] == "empresas/a/certificado"
    again = await client.post(
        "/v1/credenciais", json=_register_body(world), headers=auth(TOKENS["admin"])
    )
    assert again.status_code == 409

    # Quem opera vê os metadados, nunca o caminho no cofre.
    listed = await client.get("/v1/credenciais", headers=auth(TOKENS["operator"]))
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    [seen] = listed.json()
    assert seen["vault_path"] is None and seen["custodian_subject"] == world.custodian.subject
    assert (await client.get("/v1/credenciais", headers=auth(TOKENS["outsider"]))).json() == []
    other_area = await client.get(
        f"/v1/credenciais?area_id={world.tenant.area_id}", headers=auth(TOKENS["outsider"])
    )
    assert other_area.status_code == 404
    assert (await client.get("/v1/credenciais", headers=auth(TOKENS["other"]))).json() == []

    soon = await client.get(
        "/v1/credenciais/vencimentos?dias=365", headers=auth(TOKENS["custodian"])
    )
    assert [c["name"] for c in soon.json()] == [CERT]
    assert (
        await client.get("/v1/credenciais/vencimentos", headers=auth(TOKENS["custodian"]))
    ).json() == []
    assert (
        await client.get("/v1/credenciais/vencimentos?dias=-1", headers=auth(TOKENS["custodian"]))
    ).status_code == 422

    path = f"/v1/credenciais/{info['credential_id']}"
    renew = {"validade": in_days(500).isoformat(), "impressao": OTHER_FINGERPRINT}
    assert (await client.patch(path, json=renew, headers=auth(TOKENS["admin"]))).status_code == 403
    renewed = await client.patch(path, json=renew, headers=auth(TOKENS["custodian"]))
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["fingerprint_sha256"] == OTHER_FINGERPRINT
    assert (
        await client.post(f"{path}/revogar", headers=auth(TOKENS["operator"]))
    ).status_code == 403
    assert (await client.post(f"{path}/revogar", headers=auth(TOKENS["other"]))).status_code == 404
    revoked = await client.post(f"{path}/revogar", headers=auth(TOKENS["admin"]))
    assert revoked.status_code == 200 and revoked.json()["status"] == "revogada"
    assert (await client.post(f"{path}/revogar", headers=auth(TOKENS["admin"]))).status_code == 409


async def test_authorizations_and_two_approvals(api: Api) -> None:
    client, world = api.client, api.a
    await client.post("/v1/credenciais", json=_register_body(world), headers=auth(TOKENS["admin"]))
    version = await draft_a1(world)
    base = f"/v1/robos/{version.robot_id}"

    # Sem a autorização, a aprovação é recusada com a lista do que falta.
    blocked = await client.post(f"{base}/versoes/1/publicar", headers=auth(TOKENS["approver"]))
    assert blocked.status_code == 422
    assert blocked.json()["erro"] == "certificado_a1_bloqueado"
    assert any("falta a autorização" in d for d in blocked.json()["detalhes"])
    approvals = await client.get(f"{base}/versoes/1/aprovacoes", headers=auth(TOKENS["operator"]))
    assert approvals.status_code == 200
    assert approvals.json()["required"] == 2 and approvals.json()["missing"] == 2
    assert approvals.json()["problems"]

    body = {"credencial": CERT}
    refused = await client.post(
        f"{base}/autorizacoes-a1", json=body, headers=auth(TOKENS["approver"])
    )
    assert refused.status_code == 403
    assert (
        await client.post(
            f"{base}/autorizacoes-a1", json={"credencial": "X"}, headers=auth(TOKENS["custodian"])
        )
    ).status_code == 422
    created = await client.post(
        f"{base}/autorizacoes-a1", json=body, headers=auth(TOKENS["custodian"])
    )
    assert created.status_code == 201, created.text
    authorization = created.json()
    assert authorization["current"] is True
    listed = await client.get(f"{base}/autorizacoes-a1", headers=auth(TOKENS["operator"]))
    assert [a["authorization_id"] for a in listed.json()] == [authorization["authorization_id"]]
    for token in ("outsider", "other"):
        assert (
            await client.get(f"{base}/autorizacoes-a1", headers=auth(TOKENS[token]))
        ).status_code == 404
        assert (
            await client.get(f"{base}/versoes/1/aprovacoes", headers=auth(TOKENS[token]))
        ).status_code == 404

    first = await client.post(f"{base}/versoes/1/publicar", headers=auth(TOKENS["approver"]))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "validated"
    assert first.json()["approvals"] == [world.tenant.approver.subject]
    assert first.json()["approvals_required"] == 2
    twice = await client.post(f"{base}/versoes/1/publicar", headers=auth(TOKENS["approver"]))
    assert twice.status_code == 409
    assert (
        twice.json()["mensagem"] == "você já aprovou esta versão; falta a aprovação de outra pessoa"
    )
    second = await client.post(f"{base}/versoes/1/publicar", headers=auth(TOKENS["approver2"]))
    assert second.status_code == 200 and second.json()["status"] == "published"

    # Rodar pelo REST; depois de revogar a autorização, a fila recusa (422).
    started = await client.post(
        "/v1/catalogo/emitir_protocolo/execucoes",
        json={"entradas": PROTOCOLO_INPUTS},
        headers=auth(TOKENS["operator"]),
    )
    assert started.status_code == 202, started.text
    wrong_robot = await client.post(
        f"/v1/robos/{UUID(int=7)}/autorizacoes-a1/{authorization['authorization_id']}/revogar",
        headers=auth(TOKENS["custodian"]),
    )
    assert wrong_robot.status_code == 404
    revoked = await client.post(
        f"{base}/autorizacoes-a1/{authorization['authorization_id']}/revogar",
        headers=auth(TOKENS["custodian"]),
    )
    assert revoked.status_code == 200 and revoked.json()["current"] is False
    refused_run = await client.post(
        "/v1/catalogo/emitir_protocolo/execucoes",
        json={"entradas": PROTOCOLO_INPUTS},
        headers=auth(TOKENS["operator"]),
    )
    assert refused_run.status_code == 422
    assert refused_run.json()["erro"] == "certificado_a1_bloqueado"


async def test_without_the_service_it_is_503(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    world = await a1_world(app_db, "Sem credenciais")
    async with asgi_client(_app(world, world, None)) as client:
        for method, path in (
            ("GET", "/v1/credenciais"),
            ("GET", "/v1/credenciais/vencimentos"),
            ("GET", f"/v1/robos/{UUID(int=1)}/autorizacoes-a1"),
            ("GET", f"/v1/robos/{UUID(int=1)}/versoes/1/aprovacoes"),
        ):
            response = await client.request(method, path, headers=auth(TOKENS["admin"]))
            assert response.status_code == 503, path


async def test_rest_backend_of_the_interface(api: Api) -> None:
    world = api.a
    admin = RestBackend(api.client, TOKENS["admin"])
    custodian = RestBackend(api.client, TOKENS["custodian"])
    info = await admin.register_credential(
        world.tenant.area_id,
        name=CERT,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path="empresas/a/certificado",
        custodian_subject=world.custodian.subject,
        not_after=in_days(20),
        fingerprint=FINGERPRINT,
    )
    assert info.status is CredentialStatus.EXPIRING
    assert [c.name for c in await custodian.list_credentials(world.tenant.area_id)] == [CERT]
    assert [c.name for c in await custodian.expiring_credentials()] == [CERT]
    renewed = await custodian.renew_credential(info.credential_id, in_days(300), FINGERPRINT)
    assert renewed.status is CredentialStatus.ACTIVE
    with pytest.raises(Forbidden):
        await admin.renew_credential(info.credential_id, in_days(300))
    version = await draft_a1(world)
    authorization = await custodian.authorize_a1(version.robot_id, CERT)
    assert [a.authorization_id for a in await admin.list_a1_authorizations(version.robot_id)] == [
        authorization.authorization_id
    ]
    with pytest.raises(Conflict):
        await custodian.authorize_a1(version.robot_id, CERT)
    approvals = await admin.version_approvals(version.robot_id, 1)
    assert approvals.required == 2 and approvals.status is VersionStatus.VALIDATED
    revoked = await custodian.revoke_a1_authorization(
        version.robot_id, authorization.authorization_id
    )
    assert not revoked.current
    with pytest.raises(NotFound):
        await RestBackend(api.client, TOKENS["other"]).version_approvals(version.robot_id, 1)
    gone = await admin.revoke_credential(info.credential_id)
    assert gone.status is CredentialStatus.REVOKED
