"""Ponta a ponta de Publicar: agenda, webhook e lote de 3 linhas rodando de verdade no portal.

O serviço inteiro num processo, sobre PostgreSQL e Chromium: o robô de exemplo
``emitir_protocolo`` é publicado pelo REST e roda por uma agenda (o agendador do
despachante), por um webhook assinado (sem token) e por um lote de três linhas.
Cada execução entra no portal de teste com usuário, senha e TOTP e emite um
protocolo; o resultado do lote traz o número de cada um.
"""

import asyncio
import csv
import io
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from rapa.control.db import Database
from rapa.control.services import verify_chain
from rapa.control.services.webhooks import sign_delivery
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import RunningPortal
from tests.control.api.support import asgi_client, auth, running
from tests.control.db.support import Tenant
from tests.e2e.conftest import WEBHOOK_KEY
from tests.e2e.support import INPUTS, TOKENS, publish, wait_finished

pytestmark = [pytest.mark.database, pytest.mark.browser]

BATCH = [
    ("Nome completo", "CPF", "UF"),
    ("Ana Lima", "935.411.347-80", "MG"),
    ("João Souza", "111.444.777-35", "RJ"),
    ("Maria Costa", "529.982.247-25", "SP"),
]


async def _wait(condition: Any, what: str, limit_s: float = 120.0) -> Any:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        value = await condition()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"{what} não aconteceu em {limit_s:.0f} s")


async def test_schedule_webhook_and_batch_run_the_robot_on_the_portal(
    publishing_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    app_db: Database,
    superuser_engine: Any,
) -> None:
    app, tenant, _other = publishing_app
    operator = auth(TOKENS["operator"])
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host).model_dump(mode="json")

    async with running(app), asgi_client(app) as client:
        robot = (await publish(client, tenant, manifest))["robot_id"]

        # -- agenda: a ocorrência vencida vira uma execução de origem "schedule" ---------
        created = await client.post(
            "/v1/agendas",
            json={
                "robo_id": robot,
                "titulo": "Protocolo anual",
                "cron": "0 0 1 1 *",
                "entradas": INPUTS,
            },
            headers=operator,
        )
        assert created.status_code == 201, created.text
        schedule = created.json()
        # Em vez de esperar o 1º de janeiro, a ocorrência é antecipada para agora.
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE schedules SET next_run_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": schedule["schedule_id"]},
            )

        async def scheduled_run() -> Any:
            response = await client.get(
                f"/v1/agendas/{schedule['schedule_id']}/ocorrencias", headers=operator
            )
            runs = [o["run_id"] for o in response.json() if o["status"] == "enqueued"]
            return runs[0] if runs else None

        schedule_run = await _wait(scheduled_run, "a ocorrência da agenda", 30)

        # -- webhook: a entrega assinada, sem token, vira uma execução de origem "webhook" -
        made = await client.post(
            f"/v1/robos/{robot}/webhooks", json={"titulo": "ERP"}, headers=operator
        )
        assert made.status_code == 201, made.text
        hook, secret = made.json()["webhook"], made.json()["secret"]
        body = json.dumps({**INPUTS, "nome": "Pedro Webhook"}).encode()
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-RaPA-Timestamp": stamp,
            "X-RaPA-Delivery": "pedido-42",
            "X-RaPA-Signature": sign_delivery(secret, stamp, "pedido-42", body),
        }
        delivered = await client.post(hook["path"], content=body, headers=headers)
        assert delivered.status_code == 202, delivered.text
        webhook_run = delivered.json()["run_id"]
        # O sistema repetiu a entrega: a mesma execução, nada novo.
        again = await client.post(hook["path"], content=body, headers=headers)
        assert again.status_code == 202 and again.json()["run_id"] == webhook_run

        # -- lote de 3 linhas: prévia, criação (tudo ou nada) e uma execução por linha -----
        sheet = ("\r\n".join(";".join(row) for row in BATCH) + "\r\n").encode()
        form = {
            "data": {"robo_id": robot},
            "files": {"arquivo": ("pessoas.csv", sheet, "text/csv")},
        }
        preview = await client.post("/v1/lotes/previa", headers=operator, **form)
        assert preview.status_code == 200, preview.text
        assert preview.json()["rows"] == 3 and preview.json()["error_rows"] == 0
        batch = await client.post("/v1/lotes", headers=operator, **form)
        assert batch.status_code == 201, batch.text
        batch_path = batch.headers["location"]

        for run_id, trigger in ((schedule_run, "schedule"), (webhook_run, "webhook")):
            done = await wait_finished(client, run_id, TOKENS["operator"])
            assert done["state"] == "succeeded", done
            assert done["trigger"] == trigger
            assert done["outputs"]["protocolo"].startswith("PRT-")

        async def batch_done() -> Any:
            info = (await client.get(batch_path, headers=operator)).json()
            finished = sum(
                info["counts"].get(s, 0)
                for s in ("succeeded", "failed", "indeterminate", "cancelled")
            )
            return info if finished == info["total"] else None

        info = await _wait(batch_done, "o fim do lote", 180)
        assert info["counts"] == {"succeeded": 3}, info
        result = await client.get(f"{batch_path}/resultado", headers=operator)
        table = list(csv.reader(io.StringIO(result.content.decode("utf-8-sig")), delimiter=";"))
        assert [row[2] for row in table[1:]] == ["Concluída"] * 3
        protocols = {row[5] for row in table[1:]}

        listed = await client.get("/v1/execucoes", headers=operator)
        triggers = sorted(run["trigger"] for run in listed.json())
        assert triggers == ["batch", "batch", "batch", "schedule", "webhook"]

    # O portal emitiu um protocolo por execução, com os dados de cada linha.
    issued = portal_v1.state.protocolos
    assert len(issued) == 5
    assert {p["nome"] for p in issued} == {
        "Maria da Silva",
        "Pedro Webhook",
        "Ana Lima",
        "João Souza",
        "Maria Costa",
    }
    assert protocols <= {p["numero"] for p in issued} and len(protocols) == 3

    # A chave do webhook, a do servidor e os segredos do portal não ficam em lugar nenhum.
    stored: list[str] = []
    async with superuser_engine.connect() as conn:
        for table in (
            "webhooks",
            "webhook_deliveries",
            "schedules",
            "schedule_occurrences",
            "batches",
            "batch_rows",
            "runs",
            "run_events",
            "audit_log",
        ):
            rows = (await conn.execute(text(f"SELECT * FROM {table}"))).all()  # noqa: S608
            stored.append(json.dumps([list(row) for row in rows], default=str))
    dump = "\n".join(stored)
    for value in (secret, WEBHOOK_KEY, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert value not in dump
    async with app_db.tenant_session(tenant.company_id) as session:
        chain = await verify_chain(session, tenant.company_id)
        actions = (await session.scalars(text("SELECT action FROM audit_log"))).all()
    assert chain.ok, chain.problems
    for action in ("agenda.criada", "webhook.criado", "lote.criado"):
        assert action in actions
    assert actions.count("execucao.iniciada") == 5
