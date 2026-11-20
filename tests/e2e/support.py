"""Auxiliares dos testes de ponta a ponta."""

import asyncio
from typing import Any

import httpx2

from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from tests.control.api.support import auth
from tests.control.db.support import Tenant

INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
_LOGIN = {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD}
_OTP = {"totp_secret": DEFAULT_TOTP_SECRET}
#: Segredos dos robôs de exemplo e dos robôs gravados no portal v1 (nomes do compilador).
SECRETS = {
    "portal": _LOGIN,
    "portal_otp": _OTP,
    "portal_v1": _LOGIN,
    "portal_v1_otp": _OTP,
}
TOKENS = {
    "creator": "token-criadora",
    "approver": "token-aprovador",
    "operator": "token-operadora",
    "outsider": "token-vizinho",
    "other": "token-outra-empresa",
}


async def wait_finished(client: httpx2.AsyncClient, run_id: str, token: str) -> dict[str, Any]:
    """Consulta a execução pelo REST até ela terminar (até 60 s)."""
    for _ in range(600):
        response = await client.get(f"/v1/execucoes/{run_id}", headers=auth(token))
        assert response.status_code == 200, response.text
        status: dict[str, Any] = response.json()
        if status["finished_at"] is not None:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"a execução {run_id} não terminou em 60 s")


async def publish(
    client: httpx2.AsyncClient, tenant: Tenant, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Cria e valida como criadora, confere que ela não publica, e publica como aprovador."""
    body = {"area_id": str(tenant.area_id), "manifesto": manifest}
    created = await client.post("/v1/robos", json=body, headers=auth(TOKENS["creator"]))
    assert created.status_code == 201, created.text
    base = created.headers["location"]
    validated = await client.post(f"{base}/validar", headers=auth(TOKENS["creator"]))
    assert validated.status_code == 200, validated.text
    # Quem criou não publica; quem aprova, sim.
    refused = await client.post(f"{base}/publicar", headers=auth(TOKENS["creator"]))
    assert refused.status_code == 403
    published = await client.post(f"{base}/publicar", headers=auth(TOKENS["approver"]))
    assert published.status_code == 200, published.text
    info: dict[str, Any] = published.json()
    assert info["status"] == "published"
    return info
