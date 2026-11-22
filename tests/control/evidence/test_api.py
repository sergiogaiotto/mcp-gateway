"""REST das evidências sobre o PostgreSQL: ``/v1/execucoes/{id}/artefatos``."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.artifacts import attempt_dir
from rapa.control.auth import StaticTokenVerifier
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.services import PgArtifacts, PgPlatform
from rapa.core.contracts import ArtifactService, ArtifactStatus, Gone, NotFound
from rapa.ui.backend import RestBackend
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import Tenant, make_tenant, protocolo, publish, reset_queue
from tests.control.evidence.support import PDF, PNG, WEBM, EvidenceWorld, run_row

pytestmark = pytest.mark.database

TOKENS = {
    "creator": "token-criadora",
    "approver": "token-aprovador",
    "operator": "token-operadora",
    "viewer": "token-leitora",
    "admin": "token-admin",
    "outsider": "token-vizinho",
    "other_admin": "token-admin-b",
}


@dataclass
class Api:
    a: Tenant
    b: Tenant
    client: httpx2.AsyncClient
    failed: UUID
    succeeded: UUID
    other: UUID
    world: EvidenceWorld
    root: Path


def _app(a: Tenant, b: Tenant, platform: PgPlatform, artifacts: ArtifactService | None) -> Any:
    people = {
        TOKENS["creator"]: a.creator,
        TOKENS["approver"]: a.approver,
        TOKENS["operator"]: a.operator,
        TOKENS["viewer"]: a.viewer,
        TOKENS["admin"]: a.admin,
        TOKENS["outsider"]: a.outsider,
        TOKENS["other_admin"]: b.admin,
    }
    return create_http_app(
        catalog=platform,
        runs=platform,
        admin=platform,
        verifier=StaticTokenVerifier(people),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        artifacts=artifacts,
    )


@pytest.fixture
async def api(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Api]:
    await reset_queue(superuser_engine, total_slots=10)
    root = tmp_path / "artefatos"
    world = EvidenceWorld(app_db, dispatcher_db, root)
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    for tenant in (a, b):
        await publish(world.platform, tenant, protocolo())
    failed = await world.run(a)
    succeeded = await world.run(
        a, state="succeeded", files={"tela-001-emitir-depois.png": ("screenshot", PNG)}
    )
    other = await world.run(b)
    service = PgArtifacts(app_db, world.store)
    async with asgi_client(_app(a, b, world.platform, service)) as client:
        yield Api(a, b, client, failed, succeeded, other, world, root)


def _files(run_id: UUID) -> str:
    return f"/v1/execucoes/{run_id}/artefatos"


async def test_list_shows_the_evidence_without_paths(
    api: Api, superuser_engine: AsyncEngine
) -> None:
    listed = await api.client.get(_files(api.failed), headers=auth(TOKENS["operator"]))
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "private, no-store"
    body = listed.json()
    by_name = {item["name"]: item for item in body}
    assert set(by_name) == {"tela-001-entrar-falha.png", "video-falha.webm", "nota-001.pdf"}
    for name, kind, media, data in (
        ("tela-001-entrar-falha.png", "screenshot", "image/png", PNG),
        ("video-falha.webm", "video", "video/webm", WEBM),
        ("nota-001.pdf", "download", "application/pdf", PDF),
    ):
        item = by_name[name]
        assert item["kind"] == kind and item["media_type"] == media
        assert item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["status"] == "disponivel" and item["created_at"]
        assert set(item) == {"name", "kind", "size", "sha256", "media_type", "created_at", "status"}
    # Nenhum caminho do disco, nem na lista, nem no banco, nem na situação da execução.
    assert str(api.root) not in listed.text and "path" not in listed.text
    row = await run_row(superuser_engine, api.failed)
    assert str(api.root) not in json.dumps(row, default=str)
    assert all("path" not in item for item in row["artifacts"])
    status = await api.client.get(f"/v1/execucoes/{api.failed}", headers=auth(TOKENS["operator"]))
    assert status.status_code == 200 and str(api.root) not in status.text


@pytest.mark.parametrize(
    ("name", "media", "disposition", "data"),
    [
        ("tela-001-entrar-falha.png", "image/png", "inline", PNG),
        ("video-falha.webm", "video/webm", "inline", WEBM),
        ("nota-001.pdf", "application/pdf", "attachment", PDF),
    ],
)
async def test_file_comes_with_the_right_headers(
    api: Api, name: str, media: str, disposition: str, data: bytes
) -> None:
    got = await api.client.get(f"{_files(api.failed)}/{name}", headers=auth(TOKENS["viewer"]))
    assert got.status_code == 200, got.text
    assert got.content == data
    assert got.headers["content-type"].split(";")[0] == media
    assert got.headers["content-disposition"] == f'{disposition}; filename="{name}"'
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["cache-control"] == "private, no-store"
    assert got.headers["content-length"] == str(len(data))


async def test_who_sees_the_run_sees_its_artifacts(api: Api) -> None:
    shot = "tela-001-entrar-falha.png"
    for token in ("creator", "approver", "operator", "viewer", "admin"):
        listed = await api.client.get(_files(api.failed), headers=auth(TOKENS[token]))
        assert listed.status_code == 200, token
        got = await api.client.get(f"{_files(api.failed)}/{shot}", headers=auth(TOKENS[token]))
        assert got.status_code == 200, token
    # Outra área e outra empresa: a execução não existe.
    for token, run_id in (
        ("outsider", api.failed),
        ("other_admin", api.failed),
        ("admin", api.other),
    ):
        listed = await api.client.get(_files(run_id), headers=auth(TOKENS[token]))
        assert listed.status_code == 404, token
        assert listed.json()["erro"] == "nao_encontrado"
        got = await api.client.get(f"{_files(run_id)}/{shot}", headers=auth(TOKENS[token]))
        assert got.status_code == 404, token
    own = await api.client.get(_files(api.other), headers=auth(TOKENS["other_admin"]))
    assert own.status_code == 200 and len(own.json()) == 3
    assert (await api.client.get(_files(api.failed))).status_code == 401
    unknown = await api.client.get(_files(UUID(int=7)), headers=auth(TOKENS["admin"]))
    assert unknown.status_code == 404


async def test_names_with_slash_dots_or_outside_the_list_are_not_found(api: Api) -> None:
    # Um arquivo que está na pasta da tentativa, mas não na lista da execução, não sai.
    folder = attempt_dir(api.root, api.a.company_id, api.failed, 1)
    (folder / "solto.png").write_bytes(PNG)
    base = _files(api.failed)
    for path in (
        f"{base}/solto.png",
        f"{base}/nao-existe.png",
        f"{base}/%2E%2E",
        f"{base}/..%2F..%2Fsegredo",
        f"{base}/a%2Fb.png",
        f"{base}/a/b.png",
        f"{base}/tela-001-entrar-falha.png%00",
        f"{base}/.tela.png",
        f"{_files(api.succeeded)}/nota-001.pdf",  # é de outra execução
    ):
        got = await api.client.get(path, headers=auth(TOKENS["admin"]))
        assert got.status_code == 404, path
        assert PNG not in got.content
    # O cliente HTTP resolve ".." antes de enviar; o serviço recusa do mesmo jeito.
    service = PgArtifacts(api.world.platform.db, api.world.store)
    for name in ("..", "../tela-001-entrar-falha.png", "a/b.png", "solto.png", ""):
        with pytest.raises(NotFound):
            await service.open_artifact(api.a.admin, api.failed, name)


async def test_expired_artifacts_show_as_expired_and_are_not_served(
    api: Api, superuser_engine: AsyncEngine
) -> None:
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET artifacts_expired_at = now() WHERE id = :id"), {"id": api.failed}
        )
    listed = await api.client.get(_files(api.failed), headers=auth(TOKENS["operator"]))
    assert {item["status"] for item in listed.json()} == {"expirado"}
    # O arquivo ainda está no disco aqui, mas a API não o serve mais.
    got = await api.client.get(
        f"{_files(api.failed)}/video-falha.webm", headers=auth(TOKENS["operator"])
    )
    assert got.status_code == 410 and got.json()["erro"] == "expirado"
    assert WEBM not in got.content
    # A outra execução da empresa segue disponível.
    fine = await api.client.get(_files(api.succeeded), headers=auth(TOKENS["operator"]))
    assert {item["status"] for item in fine.json()} == {"disponivel"}
    row = await run_row(superuser_engine, api.failed)
    assert row["artifacts_expired_at"] is not None and len(row["artifacts"]) == 3


async def test_expired_captures_leave_the_downloads_of_a_successful_run(
    api: Api, superuser_engine: AsyncEngine
) -> None:
    """O prazo curto de uma execução que deu certo: a tela e o vídeo expiram, o arquivo
    baixado (a evidência) segue disponível até o prazo das evidências."""
    run_id = await api.world.run(api.a, state="succeeded")
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET captures_expired_at = now() WHERE id = :id"), {"id": run_id}
        )
    headers = auth(TOKENS["operator"])
    listed = await api.client.get(_files(run_id), headers=headers)
    assert {item["name"]: item["status"] for item in listed.json()} == {
        "tela-001-entrar-falha.png": "expirado",
        "video-falha.webm": "expirado",
        "nota-001.pdf": "disponivel",
    }
    for name in ("tela-001-entrar-falha.png", "video-falha.webm"):
        got = await api.client.get(f"{_files(run_id)}/{name}", headers=headers)
        assert got.status_code == 410 and got.json()["erro"] == "expirado", name
    pdf = await api.client.get(f"{_files(run_id)}/nota-001.pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.content == PDF


async def test_without_the_service_it_is_503(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    tenant = await make_tenant(app_db, "Sem evidências")
    platform = PgPlatform(app_db)
    async with asgi_client(_app(tenant, tenant, platform, None)) as client:
        got = await client.get(_files(UUID(int=5)), headers=auth(TOKENS["operator"]))
        assert got.status_code == 503
        got = await client.get(f"{_files(UUID(int=5))}/x.png", headers=auth(TOKENS["operator"]))
        assert got.status_code == 503


async def test_rest_backend_of_the_interface(api: Api, superuser_engine: AsyncEngine) -> None:
    backend = RestBackend(api.client, TOKENS["operator"])
    listed = await backend.list_artifacts(api.failed)
    assert {item.name for item in listed} == {
        "tela-001-entrar-falha.png",
        "video-falha.webm",
        "nota-001.pdf",
    }
    content = await backend.open_artifact(api.failed, "video-falha.webm")
    assert content.media_type == "video/webm" and content.inline and content.size == len(WEBM)
    assert await content.read() == WEBM
    pdf = await backend.open_artifact(api.failed, "nota-001.pdf")
    assert pdf.media_type == "application/pdf" and not pdf.inline
    await pdf.aclose()
    with pytest.raises(NotFound):
        await backend.open_artifact(api.other, "video-falha.webm")
    with pytest.raises(NotFound):
        await backend.open_artifact(api.failed, "../video-falha.webm")
    with pytest.raises(NotFound):
        await RestBackend(api.client, TOKENS["outsider"]).list_artifacts(api.failed)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET artifacts_expired_at = now() WHERE id = :id"), {"id": api.failed}
        )
    with pytest.raises(Gone):
        await backend.open_artifact(api.failed, "video-falha.webm")
    assert {item.status for item in await backend.list_artifacts(api.failed)} == {
        ArtifactStatus.EXPIRED
    }
