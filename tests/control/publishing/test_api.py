"""REST de publicação sobre o PostgreSQL: ``/v1/agendas``, webhooks e ``/v1/lotes``."""

import csv
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import openpyxl
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.api.publishing import PublishingServices
from rapa.control.auth import StaticTokenVerifier
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.services import PgWebhooks, WebhookKeys
from rapa.control.services.webhooks import WebhookGate, sign_delivery
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import PROTOCOLO_INPUTS

from .support import (
    PROTOCOL_ROWS,
    SERVER_KEY,
    World,
    build_world,
    csv_bytes,
    protocol_sheet,
    runs_of,
)

pytestmark = pytest.mark.database

TOKENS = {
    "operator": "token-operadora-a",
    "viewer": "token-leitora-a",
    "creator": "token-criadora-a",
    "outsider": "token-vizinho-a",
    "other": "token-admin-b",
}


@dataclass
class Api:
    world: World
    client: httpx2.AsyncClient
    app: FastAPI


def _app(world: World, publishing: PublishingServices | None, **settings: Any) -> FastAPI:
    people = {
        TOKENS["operator"]: world.a.operator,
        TOKENS["viewer"]: world.a.viewer,
        TOKENS["creator"]: world.a.creator,
        TOKENS["outsider"]: world.a.outsider,
        TOKENS["other"]: world.b.admin,
    }
    options: dict[str, Any] = {"allow_static_tokens": True, "rate_limit_per_s": None}
    options.update(settings)
    return create_http_app(
        catalog=world.platform,
        runs=world.platform,
        admin=world.platform,
        verifier=StaticTokenVerifier(people),
        resolver=DictResolver(),
        settings=HttpSettings(**options),
        publishing=publishing,
    )


def _services(world: World, **extra: Any) -> PublishingServices:
    return PublishingServices(
        schedules=world.schedules,
        webhooks=world.webhooks,
        receiver=world.webhooks,
        batches=world.batches,
        **extra,
    )


@pytest.fixture
async def api(app_db: Database, superuser_engine: AsyncEngine) -> AsyncIterator[Api]:
    world = await build_world(app_db, superuser_engine)
    app = _app(world, _services(world, batch_max_bytes=3 * 1024 * 1024))
    # Sem o lifespan: o REST não precisa do gateway MCP, que exige fechar na mesma tarefa.
    async with asgi_client(app) as client:
        yield Api(world, client, app)


def _who(name: str) -> dict[str, str]:
    return auth(TOKENS[name])


# -- agendas --------------------------------------------------------------------------


async def test_schedules_crud(api: Api) -> None:
    client, world = api.client, api.world
    body = {
        "robo_id": str(world.robots_a["emitir_protocolo"]),
        "titulo": "Protocolo diário",
        "cron": "0 9 * * 1-5",
        "entradas": PROTOCOLO_INPUTS,
    }
    refused = await client.post("/v1/agendas", json=body, headers=_who("viewer"))
    assert refused.status_code == 403
    for who in ("outsider", "other"):
        assert (await client.post("/v1/agendas", json=body, headers=_who(who))).status_code == 404
    bad = await client.post(
        "/v1/agendas", json={**body, "cron": "0 0 31 2 *"}, headers=_who("operator")
    )
    assert bad.status_code == 422 and bad.json()["erro"] == "entrada_invalida"
    assert any("nunca acontece" in d for d in bad.json()["detalhes"])
    created = await client.post("/v1/agendas", json=body, headers=_who("operator"))
    assert created.status_code == 201, created.text
    info = created.json()
    assert created.headers["location"] == f"/v1/agendas/{info['schedule_id']}"
    assert info["timezone"] == "America/Sao_Paulo" and info["enabled"] is True
    path = created.headers["location"]
    assert (await client.get(path, headers=_who("viewer"))).json() == info
    for who in ("outsider", "other"):
        assert (await client.get(path, headers=_who(who))).status_code == 404
        assert (await client.get("/v1/agendas", headers=_who(who))).json() == []
    listed = await client.get("/v1/agendas", headers=_who("viewer"))
    assert [s["schedule_id"] for s in listed.json()] == [info["schedule_id"]]
    changed = await client.patch(path, json={"ativa": False}, headers=_who("operator"))
    assert changed.status_code == 200 and changed.json()["next_run_at"] is None
    assert (
        await client.patch(path, json={"ativa": True}, headers=_who("viewer"))
    ).status_code == 403
    extra = await client.patch(path, json={"desconhecido": 1}, headers=_who("operator"))
    assert extra.status_code == 422
    occurrences = await client.get(f"{path}/ocorrencias", headers=_who("viewer"))
    assert occurrences.status_code == 200 and occurrences.json() == []
    assert (await client.delete(path, headers=_who("viewer"))).status_code == 403
    deleted = await client.delete(path, headers=_who("operator"))
    assert deleted.status_code == 204
    assert (await client.get(path, headers=_who("operator"))).status_code == 404


# -- webhooks -----------------------------------------------------------------------------


async def _webhook(api: Api, **body: Any) -> tuple[dict[str, Any], str]:
    robot = api.world.robots_a["emitir_protocolo"]
    created = await api.client.post(
        f"/v1/robos/{robot}/webhooks",
        json={"titulo": "ERP", **body},
        headers=_who("operator"),
    )
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    data = created.json()
    return data["webhook"], data["secret"]


def _delivery_headers(secret: str, body: bytes, delivery: str, at: float) -> dict[str, str]:
    stamp = str(int(at))
    return {
        "Content-Type": "application/json",
        "X-RaPA-Timestamp": stamp,
        "X-RaPA-Delivery": delivery,
        "X-RaPA-Signature": sign_delivery(secret, stamp, delivery, body),
    }


async def test_webhook_admin_shows_the_secret_only_once(api: Api) -> None:
    client = api.client
    robot = api.world.robots_a["emitir_protocolo"]
    hook, secret = await _webhook(api)
    base = f"/v1/robos/{robot}/webhooks"
    assert hook["path"] == f"/v1/webhooks/{hook['webhook_id']}"
    for path in (base, f"{base}/{hook['webhook_id']}"):
        response = await client.get(path, headers=_who("viewer"))
        assert response.status_code == 200 and secret not in response.text
        assert "secret" not in response.text
    for who in ("outsider", "other"):
        assert (await client.get(base, headers=_who(who))).status_code == 404
    refused = await client.post(base, json={"titulo": "x"}, headers=_who("viewer"))
    assert refused.status_code == 403
    rotated = await client.post(
        f"{base}/{hook['webhook_id']}/trocar-chave", headers=_who("operator")
    )
    assert rotated.status_code == 200 and rotated.headers["cache-control"] == "no-store"
    assert rotated.json()["secret"] != secret and rotated.json()["webhook"]["key_version"] == 2
    changed = await client.patch(
        f"{base}/{hook['webhook_id']}", json={"por_minuto": 5}, headers=_who("operator")
    )
    assert changed.json()["rate_per_minute"] == 5
    assert (
        await client.delete(f"{base}/{hook['webhook_id']}", headers=_who("operator"))
    ).status_code == 204
    assert (
        await client.get(f"{base}/{hook['webhook_id']}", headers=_who("operator"))
    ).status_code == 404


async def test_webhook_delivery_without_token(api: Api) -> None:
    client, world = api.client, api.world
    hook, secret = await _webhook(api, por_minuto=3)
    url = f"/v1/webhooks/{hook['webhook_id']}"
    body = json.dumps(PROTOCOLO_INPUTS).encode()
    now = world.clock.now.timestamp()
    first = await client.post(
        url, content=body, headers=_delivery_headers(secret, body, "p-1", now)
    )
    assert first.status_code == 202, first.text
    delivery = first.json()
    assert delivery["duplicate"] is False and delivery["state"] == "queued"
    assert first.headers["location"] == f"/v1/execucoes/{delivery['run_id']}"
    again = await client.post(
        url, content=body, headers=_delivery_headers(secret, body, "p-1", now)
    )
    assert again.status_code == 202 and again.json()["run_id"] == delivery["run_id"]
    assert again.json()["duplicate"] is True
    # Assinatura errada, corpo trocado, horário velho e identificador malformado: 401, iguais.
    tampered = body.replace(b"Maria", b"Mario")
    refusals = [
        await client.post(url, content=body, headers=_delivery_headers("x" * 40, body, "p-2", now)),
        await client.post(
            url, content=tampered, headers=_delivery_headers(secret, body, "p-2", now)
        ),
        await client.post(
            url, content=body, headers=_delivery_headers(secret, body, "p-2", now - 600)
        ),
        await client.post(
            "/v1/webhooks/nao-e-uuid",
            content=body,
            headers=_delivery_headers(secret, body, "p-2", now),
        ),
        await client.post(url, content=body, headers={"Content-Type": "application/json"}),
    ]
    assert [r.status_code for r in refusals] == [401] * 5
    assert len({r.text for r in refusals}) == 1
    assert refusals[0].json()["erro"] == "assinatura_invalida"
    assert "www-authenticate" not in refusals[0].headers
    # Entradas que o robô não declara: 422, sem repetir os valores.
    extra = json.dumps({**PROTOCOLO_INPUTS, "senha": "segredo-123"}).encode()
    invalid = await client.post(
        url, content=extra, headers=_delivery_headers(secret, extra, "p-3", now)
    )
    assert invalid.status_code == 422 and "segredo-123" not in invalid.text
    assert "campo não declarado: senha" in invalid.json()["detalhes"]
    # Limite por webhook (3 por minuto; as recusadas por assinatura não contam).
    limited = await client.post(
        url, content=body, headers=_delivery_headers(secret, body, "p-4", now)
    )
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert len(await runs_of_api(api)) == 1


async def test_webhook_delivery_refusals_per_client_get_429_before_the_database(
    api: Api,
) -> None:
    world = api.world
    gated = PgWebhooks(
        world.webhooks.db,
        WebhookKeys(SERVER_KEY),
        gate=WebhookGate(failures_per_minute=2, clock=world.clock.monotonic),
        clock=world.clock,
    )
    app = _app(world, PublishingServices(receiver=gated))
    hook, secret = await _webhook(api)
    url = f"/v1/webhooks/{hook['webhook_id']}"
    body = json.dumps(PROTOCOLO_INPUTS).encode()
    now = world.clock.now.timestamp()
    async with asgi_client(app) as client:
        statuses = [
            (
                await client.post(
                    url, content=body, headers=_delivery_headers("x" * 40, body, f"p-{n}", now)
                )
            ).status_code
            for n in range(3)
        ]
        assert statuses == [401, 401, 429]
        limited = await client.post(
            url, content=body, headers=_delivery_headers(secret, body, "p-9", now)
        )
    # O endereço que errou demais espera, mesmo com a assinatura certa.
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) == 30
    assert limited.json()["erro"] == "limite_excedido"


async def runs_of_api(api: Api) -> list[dict[str, Any]]:
    response = await api.client.get("/v1/execucoes", headers=_who("operator"))
    return [run for run in response.json() if run["trigger"] == "webhook"]


async def test_disabled_webhook_and_missing_service(api: Api, app_db: Database) -> None:
    client = api.client
    robot = api.world.robots_a["emitir_protocolo"]
    hook, secret = await _webhook(api)
    await client.patch(
        f"/v1/robos/{robot}/webhooks/{hook['webhook_id']}",
        json={"ativo": False},
        headers=_who("operator"),
    )
    body = json.dumps(PROTOCOLO_INPUTS).encode()
    response = await client.post(
        f"/v1/webhooks/{hook['webhook_id']}",
        content=body,
        headers=_delivery_headers(secret, body, "d-1", api.world.clock.now.timestamp()),
    )
    assert response.status_code == 409
    bare = _app(api.world, None)
    async with asgi_client(bare) as other:
        for method, path in (
            ("GET", "/v1/agendas"),
            ("GET", f"/v1/robos/{robot}/webhooks"),
            ("POST", f"/v1/webhooks/{hook['webhook_id']}"),
            ("GET", "/v1/lotes"),
        ):
            answer = await other.request(method, path, headers=_who("operator"))
            assert answer.status_code == 503, (method, path)


# -- lotes ------------------------------------------------------------------------------


def _form(api: Api, data: bytes, name: str = "lote.csv", **extra: str) -> dict[str, Any]:
    return {
        "data": {"robo_id": str(api.world.robots_a["emitir_protocolo"]), **extra},
        "files": {"arquivo": (name, data, "text/csv")},
    }


async def test_batch_preview_create_progress_and_download(
    api: Api, superuser_engine: AsyncEngine
) -> None:
    client, world = api.client, api.world
    bad = csv_bytes([("Nome completo", "Documento", "UF"), PROTOCOL_ROWS[0]])
    preview = await client.post(
        "/v1/lotes/previa",
        headers=_who("operator"),
        **_form(api, bad, colunas=json.dumps({"Documento": "cpf"})),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["error_rows"] == 0 and preview.json()["rows"] == 1
    wrong = csv_bytes([("Nome completo", "CPF", "UF"), ("Maria", "segredo-123", "SP")])
    refused = await client.post("/v1/lotes", headers=_who("operator"), **_form(api, wrong))
    assert refused.status_code == 422 and "segredo-123" not in refused.text
    assert refused.json()["detalhes"] == ["linha 2: cpf: valor fora do formato esperado"]
    assert (
        await client.post("/v1/lotes", headers=_who("viewer"), **_form(api, protocol_sheet()))
    ).status_code == 403
    headers = {**_who("operator"), "Idempotency-Key": "lote-1"}
    created = await client.post("/v1/lotes", headers=headers, **_form(api, protocol_sheet()))
    assert created.status_code == 201, created.text
    batch = created.json()
    assert created.headers["location"] == f"/v1/lotes/{batch['batch_id']}"
    assert batch["total"] == 3 and batch["counts"] == {"queued": 3}
    again = await client.post("/v1/lotes", headers=headers, **_form(api, protocol_sheet()))
    assert again.json()["batch_id"] == batch["batch_id"]
    assert len(await runs_of(superuser_engine, "batch", world.a.company_id)) == 3
    path = created.headers["location"]
    assert (await client.get(path, headers=_who("viewer"))).json()["total"] == 3
    rows = await client.get(f"{path}/linhas", headers=_who("viewer"))
    assert [r["line"] for r in rows.json()] == [2, 3, 4]
    download = await client.get(f"{path}/resultado", headers=_who("viewer"))
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert download.headers["content-disposition"].startswith('attachment; filename="lote-')
    table = list(csv.reader(io.StringIO(download.content.decode("utf-8-sig")), delimiter=";"))
    assert [row[2] for row in table[1:]] == ["Na fila"] * 3
    xlsx = await client.get(f"{path}/resultado?formato=xlsx", headers=_who("viewer"))
    assert openpyxl.load_workbook(io.BytesIO(xlsx.content)).active.max_row == 4
    assert (
        await client.get(f"{path}/resultado?formato=pdf", headers=_who("viewer"))
    ).status_code == 422
    for who in ("outsider", "other"):
        for suffix in ("", "/linhas", "/resultado"):
            assert (await client.get(f"{path}{suffix}", headers=_who(who))).status_code == 404
        assert (await client.get("/v1/lotes", headers=_who(who))).json() == []


async def test_rest_backend_of_the_interface_uses_these_routes(api: Api) -> None:
    from rapa.core.contracts import (
        BatchFormat,
        InvalidInput,
        ScheduleChanges,
        ScheduleSpec,
        WebhookChanges,
        WebhookSpec,
    )
    from rapa.ui.backend import RestBackend

    world = api.world
    robot = world.robots_a["emitir_protocolo"]
    backend = RestBackend(api.client, TOKENS["operator"])
    schedule = await backend.create_schedule(
        ScheduleSpec(robot_id=robot, title="Diária", cron="0 8 * * *", inputs=PROTOCOLO_INPUTS)
    )
    assert [s.schedule_id for s in await backend.list_schedules()] == [schedule.schedule_id]
    off = await backend.update_schedule(schedule.schedule_id, ScheduleChanges(enabled=False))
    assert not off.enabled
    assert await backend.list_occurrences(schedule.schedule_id) == []
    await backend.delete_schedule(schedule.schedule_id)
    assert await backend.get_schedule(schedule.schedule_id) is None
    created = await backend.create_webhook(robot, WebhookSpec(title="ERP"))
    assert created.secret.startswith("whsec_")
    rotated = await backend.rotate_webhook(robot, created.webhook.webhook_id)
    assert rotated.webhook.key_version == 2
    changed = await backend.update_webhook(
        robot, created.webhook.webhook_id, WebhookChanges(rate_per_minute=7)
    )
    assert changed.rate_per_minute == 7
    assert [w.webhook_id for w in await backend.list_webhooks(robot)] == [
        created.webhook.webhook_id
    ]
    await backend.delete_webhook(robot, created.webhook.webhook_id)
    assert await backend.list_webhooks(robot) == []
    preview = await backend.preview_batch(robot, "lote.csv", protocol_sheet())
    assert preview.ok and preview.rows == 3
    with pytest.raises(InvalidInput):
        await backend.create_batch(robot, "lote.xlsx", b"nada")
    batch = await backend.create_batch(robot, "lote.csv", protocol_sheet(), idempotency_key="k")
    assert (
        await backend.create_batch(robot, "lote.csv", protocol_sheet(), idempotency_key="k")
    ).batch_id == batch.batch_id
    assert [b.batch_id for b in await backend.list_batches()] == [batch.batch_id]
    got = await backend.get_batch(batch.batch_id)
    assert got is not None and got.total == 3
    assert len(await backend.list_batch_rows(batch.batch_id)) == 3
    file = await backend.batch_results(batch.batch_id, BatchFormat.XLSX)
    assert file.file_name.endswith(".xlsx") and file.content.startswith(b"PK")
    other = RestBackend(api.client, TOKENS["other"])
    assert await other.get_batch(batch.batch_id) is None


async def test_batch_upload_has_its_own_size_limit(api: Api) -> None:
    # Acima do limite comum (1 MiB) e abaixo do limite dos lotes (3 MiB): aceito.
    long_name = "M" * 1500
    rows = [(f"{long_name} {n}", "529.982.247-25", "SP") for n in range(900)]
    data = csv_bytes([("Nome completo", "CPF", "UF"), *rows])
    assert 1024 * 1024 < len(data) < 3 * 1024 * 1024
    preview = await api.client.post(
        "/v1/lotes/previa", headers=_who("operator"), **_form(api, data)
    )
    assert preview.status_code == 200, preview.text[:300]
    assert preview.json()["rows"] == 900
    # Nos outros endereços, o limite comum continua.
    big = await api.client.post(
        "/v1/agendas",
        content=data,
        headers={**_who("operator"), "Content-Type": "application/json"},
    )
    assert big.status_code == 413
    huge = data * 3
    too_big = await api.client.post(
        "/v1/lotes/previa", headers=_who("operator"), **_form(api, huge)
    )
    assert too_big.status_code == 413
    # O envio de arquivo da interface (o da tela Lotes, pelo NiceGUI montado na mesma
    # aplicação) tem o mesmo limite dos lotes; aqui a interface não está montada (404).
    ui_upload = "/_nicegui/client/4f1c2a/upload/12"
    files = {"files": {"arquivo": ("lote.csv", data, "text/csv")}}
    assert (await api.client.post(ui_upload, **files)).status_code == 404
    huge_files = {"files": {"arquivo": ("lote.csv", huge, "text/csv")}}
    assert (await api.client.post(ui_upload, **huge_files)).status_code == 413
    # Só esse caminho: outro do NiceGUI continua com o limite comum.
    other = "/_nicegui/client/4f1c2a/outro/12"
    assert (await api.client.post(other, **files)).status_code == 413
