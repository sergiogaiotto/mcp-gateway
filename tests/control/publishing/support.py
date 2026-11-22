"""Auxiliares dos testes de agenda, webhook e lote: o mundo de duas empresas e utilidades."""

import io
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import openpyxl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import (
    PgBatches,
    PgPlatform,
    PgSchedules,
    PgWebhooks,
    WebhookKeys,
)
from rapa.control.services.sheets import SheetLimits
from rapa.control.services.webhooks import WebhookLimiter, sign_delivery
from tests.control.db.support import Tenant, make_tenant, notas, protocolo, publish, reset_queue

#: Chave do servidor dos webhooks nos testes (nunca a de produção).
SERVER_KEY = "chave-de-teste-dos-webhooks-com-mais-de-32-caracteres"
PROTOCOL_ROWS = (
    ("Maria da Silva", "529.982.247-25", "SP"),
    ("João Souza", "111.444.777-35", "RJ"),
    ("Ana Lima", "935.411.347-80", "MG"),
)


class FakeClock:
    """Relógio que só anda quando o teste manda."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono


@dataclass
class World:
    a: Tenant
    b: Tenant
    platform: PgPlatform
    schedules: PgSchedules
    webhooks: PgWebhooks
    batches: PgBatches
    clock: FakeClock
    #: Robôs publicados de cada empresa: nome técnico → identificador.
    robots_a: dict[str, UUID]
    robots_b: dict[str, UUID]


async def build_world(
    app_db: Database,
    superuser: AsyncEngine,
    *,
    total_slots: int = 30,
    clock: FakeClock | None = None,
    max_queued: int = 1000,
    limits: SheetLimits | None = None,
) -> World:
    await reset_queue(superuser, total_slots=total_slots)
    await reset_schedules(superuser)
    platform = PgPlatform(app_db)
    a = await make_tenant(app_db, "Empresa A", max_queued=max_queued)
    b = await make_tenant(app_db, "Empresa B")
    robots_a = {}
    robots_b = {}
    for manifest in (protocolo(), notas()):
        robots_a[manifest.name] = (await publish(platform, a, manifest)).robot_id
        robots_b[manifest.name] = (await publish(platform, b, manifest)).robot_id
    clock = clock or FakeClock(datetime(2026, 11, 20, 12, 0, tzinfo=UTC))
    return World(
        a=a,
        b=b,
        platform=platform,
        schedules=PgSchedules(app_db, clock=clock),
        webhooks=PgWebhooks(
            app_db,
            WebhookKeys(SERVER_KEY),
            limiter=WebhookLimiter(clock=clock.monotonic),
            clock=clock,
        ),
        batches=PgBatches(app_db, limits=limits),
        clock=clock,
        robots_a=robots_a,
        robots_b=robots_b,
    )


async def reset_schedules(superuser: AsyncEngine) -> None:
    """Desliga as agendas de outros testes: o agendador olha todas as empresas."""
    async with superuser.begin() as conn:
        await conn.execute(
            text(
                "UPDATE schedules SET enabled = false, next_run_at = NULL "
                "WHERE archived_at IS NULL AND enabled"
            )
        )


def signed(
    secret: str, body: bytes, delivery: str, *, at: datetime | float | None = None
) -> dict[str, Any]:
    """Argumentos de ``deliver`` com a assinatura certa."""
    if isinstance(at, datetime):
        stamp = str(int(at.timestamp()))
    elif at is None:
        stamp = str(int(time.time()))
    else:
        stamp = str(int(at))
    return {
        "timestamp": stamp,
        "delivery_id": delivery,
        "signature": sign_delivery(secret, stamp, delivery, body),
        "body": body,
    }


def csv_bytes(
    rows: Sequence[Sequence[str]], *, delimiter: str = ";", encoding: str = "utf-8"
) -> bytes:
    lines = [delimiter.join(row) for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode(encoding)


def xlsx_bytes(
    rows: Sequence[Sequence[Any]], *, formula_at: tuple[int, int] | None = None
) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(list(row))
    if formula_at is not None:
        sheet.cell(row=formula_at[0], column=formula_at[1]).value = "=1+1"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def protocol_sheet(rows: Sequence[Sequence[str]] = PROTOCOL_ROWS) -> bytes:
    return csv_bytes([("Nome completo", "CPF", "UF"), *rows])


async def runs_of(superuser: AsyncEngine, trigger: str, company: UUID) -> list[dict[str, Any]]:
    async with superuser.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, state, trigger, priority, inputs, idempotency_key, requested_by "
                        "FROM runs WHERE trigger = :trigger AND company_id = :company "
                        "ORDER BY created_at, id"
                    ),
                    {"trigger": trigger, "company": company},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def table_dump(superuser: AsyncEngine, *tables: str) -> str:
    """Todo o conteúdo das tabelas, como texto (para procurar segredos)."""
    parts: list[str] = []
    async with superuser.connect() as conn:
        for table in tables:
            rows = (await conn.execute(text(f"SELECT * FROM {table}"))).all()  # noqa: S608
            parts.append(repr([tuple(row) for row in rows]))
    return "\n".join(parts)


__all__ = [
    "PROTOCOL_ROWS",
    "SERVER_KEY",
    "FakeClock",
    "World",
    "build_world",
    "csv_bytes",
    "make_tenant",
    "protocol_sheet",
    "reset_schedules",
    "runs_of",
    "signed",
    "table_dump",
    "xlsx_bytes",
]
