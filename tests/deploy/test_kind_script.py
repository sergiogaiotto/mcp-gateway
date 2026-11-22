"""O ponta a ponta do job kind (deploy/kind/ponta_a_ponta.py), aqui contra o serviço em processo.

O fluxo da REST (criar, validar, publicar, executar e ler os artefatos) roda contra o serviço
inteiro sobre PostgreSQL, Chromium e o portal de teste, como no kind; a conferência da cifra
no S3 roda contra um filer do SeaweedFS falso, que aceita objetos cifrados e recusa os em
claro; e o cofre, contra um OpenBao falso que só aceita o token raiz.
"""

import json
import os
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from rapa.fixtures.server import RunningPortal
from tests.control.api.support import asgi_client, running
from tests.control.db.conftest import app_db, pg_database, superuser_engine  # noqa: F401
from tests.control.db.support import Tenant
from tests.deploy.test_ci_scripts import load
from tests.e2e.conftest import platform_app  # noqa: F401
from tests.e2e.support import TOKENS

script = load("deploy/kind/ponta_a_ponta.py")

_DIR = 1 << 31


@pytest.mark.database
@pytest.mark.browser
async def test_publish_and_run_through_the_rest_api(
    platform_app: tuple[FastAPI, Tenant, Tenant],  # noqa: F811
    portal_v1: RunningPortal,
) -> None:
    app, tenant, _ = platform_app
    async with running(app), asgi_client(app) as client:
        status, artifacts = await script.publish_and_run(
            client,
            area=str(tenant.area_id),
            portal=portal_v1.base_url,
            creator=TOKENS["creator"],
            approver=TOKENS["approver"],
            operator=TOKENS["operator"],
            timeout_s=120,
        )
    assert status["state"] == "succeeded"
    assert artifacts and all(a.content for a in artifacts)


def _filer(objects: dict[str, bytes]) -> httpx2.AsyncClient:
    """Um filer do SeaweedFS falso: lista as pastas em JSON e entrega os objetos."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path in objects:
            return httpx2.Response(200, content=objects[path])
        prefix = path.rstrip("/") + "/"
        children: dict[str, bool] = {}
        for name in objects:
            if name.startswith(prefix):
                rest = name[len(prefix) :]
                child, _, deeper = rest.partition("/")
                children[prefix + child] = bool(deeper) or children.get(prefix + child, False)
        if not children:
            return httpx2.Response(404)
        entries = [
            {"FullPath": full, "Mode": (_DIR | 0o755) if is_dir else 0o644}
            for full, is_dir in sorted(children.items())
        ]
        return httpx2.Response(200, json={"Path": path, "Entries": entries})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url="http://filer")


CONTENT = b"%PDF-1.4 nota fiscal 0001 do portal de teste, conteudo em claro"
PREFIX = "/buckets/rapa-artefatos/empresas/e1/execucoes/r1/1"


def _artifacts() -> list[Any]:
    return [script.Artifact(name="nota-0001.pdf", sha256="0" * 64, content=CONTENT)]


async def test_check_encrypted_accepts_only_ciphertext() -> None:
    cipher = os.urandom(len(CONTENT) + 32)
    async with _filer({f"{PREFIX}/nota-0001.pdf.enc": cipher}) as filer:
        found = await script.check_encrypted(
            filer, bucket="rapa-artefatos", company="e1", run_id="r1", artifacts=_artifacts()
        )
    assert found == [f"{PREFIX}/nota-0001.pdf.enc"]


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ({f"{PREFIX}/nota-0001.pdf.enc": b"cabecalho" + CONTENT}, "em claro"),
        ({f"{PREFIX}/nota-0001.pdf": os.urandom(80)}, "sem cifra"),
        ({}, "nenhum objeto"),
        ({f"{PREFIX}/outra.pdf.enc": os.urandom(80)}, "não estão no bucket"),
    ],
)
async def test_check_encrypted_refuses_plaintext_and_missing_objects(
    objects: dict[str, bytes], message: str
) -> None:
    async with _filer(objects) as filer:
        with pytest.raises(AssertionError, match=message):
            await script.check_encrypted(
                filer, bucket="rapa-artefatos", company="e1", run_id="r1", artifacts=_artifacts()
            )


def _bao(seen: list[tuple[str, dict[str, Any]]]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Vault-Token") != "root":
            return httpx2.Response(403)
        seen.append((request.url.path, json.loads(request.content)))
        return httpx2.Response(200, json={"data": {"version": 1}})

    return handler


async def test_portal_secrets_go_under_the_company_folder() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    transport = httpx2.MockTransport(_bao(seen))
    async with httpx2.AsyncClient(transport=transport, base_url="http://bao") as bao:
        await script.write_portal_secrets(bao, "root", "rapa", "e1")
        with pytest.raises(AssertionError, match="403"):
            await script.write_portal_secrets(bao, "outro", "rapa", "e1")
    assert [path for path, _ in seen] == [
        "/v1/rapa/data/empresas/e1/portal/login",
        "/v1/rapa/data/empresas/e1/portal/otp",
    ]
    assert set(seen[0][1]["data"]) == {"username", "password"}
    assert set(seen[1][1]["data"]) == {"totp_secret"}
