"""Isolamento entre empresas, provado como ``rapa_app`` (o papel da API e da interface).

A empresa B não vê nada da A (robôs, versões, execuções, eventos, credenciais,
auditoria, usuários, sessões de gravação, agendas, webhooks e lotes), não consegue
gravar com o company_id da A, e uma transação sem empresa não vê nada de ninguém.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from rapa.control.db import Database
from rapa.control.db.schema import TENANT_TABLES
from rapa.control.services import (
    PgBatches,
    PgPlatform,
    PgProvisioning,
    PgRunQueue,
    PgSchedules,
    PgWebhooks,
    Scheduler,
    WebhookKeys,
)
from rapa.control.services.sessions import PgSessionStore
from rapa.control.services.webhooks import sign_delivery
from rapa.control.sessions import SessionRecord
from rapa.core.contracts import (
    BatchFormat,
    NotFound,
    RecordingSessionStatus,
    RunTrigger,
    ScheduleSpec,
    WebhookSpec,
)
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import CredentialRef

from .support import PROTOCOLO_INPUTS, Tenant, event, make_tenant, protocolo, publish, reset_queue

pytestmark = pytest.mark.database


@dataclass(frozen=True)
class World:
    a: Tenant
    b: Tenant
    a_robot: UUID
    a_run: UUID
    a_session: UUID
    #: A agenda, o webhook e o lote da empresa A.
    a_published: dict[str, UUID]


async def _world(app_db: Database, dispatcher_db: Database, superuser: AsyncEngine) -> World:
    await reset_queue(superuser, total_slots=10)
    platform = PgPlatform(app_db)
    a = await make_tenant(app_db, "Empresa A")
    b = await make_tenant(app_db, "Empresa B")
    provisioning = PgProvisioning(app_db)
    for tenant in (a, b):
        await provisioning.register_credential(
            tenant.company_id,
            tenant.area_id,
            CredentialRef(name="portal", kind="password", origin="https://portal.rapa.test"),
            vault_path=f"empresas/{tenant.company_id}/portal",
        )
    a_version = await publish(platform, a, protocolo())
    await publish(platform, b, protocolo())
    a_run = await platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k"
    )
    await platform.start(b.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "k")
    queue = PgRunQueue(dispatcher_db)
    for _ in range(2):
        job = await queue.claim_next()
        assert job is not None
        await queue.ingest_events(
            job.run_id,
            job.attempt,
            [event(job.run_id, job.attempt, 0, RunEventKind.RUN_STARTED)],
        )
    sessions = {tenant.company_id: await _stopped_session(app_db, tenant) for tenant in (a, b)}
    published = {}
    for tenant in (a, b):
        published[tenant.company_id] = await _publishing_rows(app_db, dispatcher_db, tenant)
    return World(
        a=a,
        b=b,
        a_robot=a_version.robot_id,
        a_run=a_run.run_id,
        a_session=sessions[a.company_id],
        a_published=published[a.company_id],
    )


async def _publishing_rows(
    app_db: Database, dispatcher_db: Database, tenant: Tenant
) -> dict[str, UUID]:
    """Uma agenda com uma ocorrência, um webhook com uma entrega e um lote com uma linha."""
    robot_id = next(
        s.robot_id
        for s in await PgPlatform(app_db).list_published(tenant.admin)
        if s.slug == "emitir_protocolo"
    )
    schedule = await PgSchedules(app_db).create_schedule(
        tenant.operator,
        ScheduleSpec(robot_id=robot_id, title="Diária", cron="0 9 * * *", inputs=PROTOCOLO_INPUTS),
    )
    assert schedule.next_run_at is not None
    await Scheduler(app_db, dispatcher_db).fire(
        tenant.company_id, schedule.schedule_id, schedule.next_run_at
    )
    webhooks = PgWebhooks(app_db, WebhookKeys("chave-do-servidor-de-teste-com-32-caracteres"))
    created = await webhooks.create_webhook(tenant.operator, robot_id, WebhookSpec(title="ERP"))
    body = json.dumps(PROTOCOLO_INPUTS).encode()
    stamp = str(int(datetime.now(UTC).timestamp()))
    await webhooks.deliver(
        created.webhook.webhook_id,
        timestamp=stamp,
        delivery_id="entrega-1",
        signature=sign_delivery(created.secret, stamp, "entrega-1", body),
        body=body,
    )
    sheet = b"Nome completo;CPF;UF\r\nMaria;529.982.247-25;SP\r\n"
    batch = await PgBatches(app_db).create_batch(tenant.operator, robot_id, "lote.csv", sheet)
    return {
        "schedule": schedule.schedule_id,
        "webhook": created.webhook.webhook_id,
        "batch": batch.batch_id,
    }


async def _stopped_session(db: Database, tenant: Tenant) -> UUID:
    """Uma sessão de gravação parada, com a gravação, para a empresa."""
    store = PgSessionStore(db)
    now = datetime.now(UTC)
    record = SessionRecord(
        company_id=tenant.company_id,
        session_id=new_id(),
        area_id=tenant.area_id,
        owner=tenant.creator.subject,
        title="Demonstração",
        start_url="http://portal-v1.rapa.test:8080/login",
        status=RecordingSessionStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    await store.insert(record, company_limit=10)
    await store.save_recording(
        tenant.company_id,
        record.session_id,
        {"version": 1, "started_at": now.isoformat(), "events": []},
        actor=tenant.creator.subject,
        stopped_at=now,
    )
    return record.session_id


async def _counts(session: AsyncSession, company_id: UUID) -> dict[str, tuple[int, int]]:
    """Por tabela: (linhas visíveis, linhas visíveis de outra empresa)."""
    out = {}
    for table in TENANT_TABLES:
        column = table.c.id if table.name == "companies" else table.c.company_id
        visible = await session.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
        foreign = await session.scalar(
            select(text("count(*)")).select_from(table).where(column != company_id)
        )
        out[table.name] = (int(visible or 0), int(foreign or 0))
    return out


async def test_company_b_sees_nothing_of_company_a(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.tenant_session(world.b.company_id) as session:
        counts = await _counts(session, world.b.company_id)
        a_ids = {
            "robots": await session.scalar(
                text("SELECT count(*) FROM robots WHERE id = :id"), {"id": world.a_robot}
            ),
            "runs": await session.scalar(
                text("SELECT count(*) FROM runs WHERE id = :id"), {"id": world.a_run}
            ),
            "run_events": await session.scalar(
                text("SELECT count(*) FROM run_events WHERE run_id = :id"), {"id": world.a_run}
            ),
            "recordings": await session.scalar(
                text("SELECT count(*) FROM recordings WHERE session_id = :id"),
                {"id": world.a_session},
            ),
        }
    for table, (visible, foreign) in counts.items():
        assert foreign == 0, f"{table}: B enxerga linhas de outra empresa"
        assert visible > 0, f"{table}: B deveria ver as próprias linhas"
    assert a_ids == {"robots": 0, "runs": 0, "run_events": 0, "recordings": 0}
    assert await PgSessionStore(app_db).get(world.b.company_id, world.a_session) is None
    assert await PgSessionStore(app_db).get_recording(world.b.company_id, world.a_session) is None

    # Pelos serviços, com o principal da B.
    platform = PgPlatform(app_db)
    admin_b = world.b.admin
    assert all(s.company_id == world.b.company_id for s in await platform.list_published(admin_b))
    assert all(r.company_id == world.b.company_id for r in await platform.list_robots(admin_b))
    assert await platform.get_version(admin_b, world.a_robot, 1) is None
    assert await platform.get(admin_b, world.a_run) is None
    assert all(
        s.company_id == world.b.company_id for s in await platform.search(admin_b, "protocolo")
    )

    # Agendas, webhooks e lotes da A: nem pelos serviços nem pelo SQL.
    published = world.a_published
    schedules = PgSchedules(app_db)
    assert await schedules.get_schedule(admin_b, published["schedule"]) is None
    assert all(s.company_id == world.b.company_id for s in await schedules.list_schedules(admin_b))
    with pytest.raises(NotFound):
        await schedules.list_occurrences(admin_b, published["schedule"])
    webhooks = PgWebhooks(app_db, WebhookKeys("chave-do-servidor-de-teste-com-32-caracteres"))
    assert await webhooks.get_webhook(admin_b, world.a_robot, published["webhook"]) is None
    with pytest.raises(NotFound):
        await webhooks.list_webhooks(admin_b, world.a_robot)
    batches = PgBatches(app_db)
    assert await batches.get_batch(admin_b, published["batch"]) is None
    assert all(x.company_id == world.b.company_id for x in await batches.list_batches(admin_b))
    with pytest.raises(NotFound):
        await batches.batch_results(admin_b, published["batch"], BatchFormat.CSV)
    async with app_db.tenant_session(world.b.company_id) as session:
        for table, column, key in (
            ("schedules", "id", "schedule"),
            ("schedule_occurrences", "schedule_id", "schedule"),
            ("webhooks", "id", "webhook"),
            ("webhook_deliveries", "webhook_id", "webhook"),
            ("batches", "id", "batch"),
            ("batch_rows", "batch_id", "batch"),
        ):
            count = await session.scalar(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :id"),  # noqa: S608
                {"id": published[key]},
            )
            assert count == 0, table


async def test_writes_with_another_company_id_fail_with_check(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, dispatcher_db, superuser_engine)
    a, b = world.a, world.b
    attempts = {
        "companies": ("INSERT INTO companies (id, name) VALUES (:new, 'Intrusa')", {}),
        "areas": (
            "INSERT INTO areas (id, company_id, name) VALUES (:new, :a, 'Invasão')",
            {},
        ),
        "robots": (
            "INSERT INTO robots (id, company_id, area_id, slug, title, status) "
            "VALUES (:new, :a, :area, 'robo_intruso', 'x', 'draft')",
            {"area": a.area_id},
        ),
        "audit_log": (
            "INSERT INTO audit_log (company_id, actor, action, target, payload_digest) "
            "VALUES (:a, 'b', 'teste.invasao', '', repeat('0', 64))",
            {},
        ),
        "credentials": (
            "INSERT INTO credentials (id, company_id, area_id, name, kind, origin, vault_path) "
            "VALUES (:new, :a, :area, 'x', 'password', 'https://x.rapa.test', 'x')",
            {"area": a.area_id},
        ),
        "recording_sessions": (
            "INSERT INTO recording_sessions (company_id, id, area_id, owner, start_url, "
            "expires_at) VALUES (:a, :new, :area, 'b', 'https://x.rapa.test/', "
            "now() + interval '1 hour')",
            {"area": a.area_id},
        ),
        "recordings": (
            "INSERT INTO recordings (company_id, id, session_id, created_by, recording) "
            "VALUES (:a, :new, :sessao, 'b', '{}')",
            {"sessao": world.a_session},
        ),
        "schedules": (
            "INSERT INTO schedules (company_id, id, robot_id, title, cron, created_by, "
            "updated_by) VALUES (:a, :new, :robo, 'x', '* * * * *', 'b', 'b')",
            {"robo": world.a_robot},
        ),
        "schedule_occurrences": (
            "INSERT INTO schedule_occurrences (company_id, schedule_id, scheduled_for, status, "
            "reason) VALUES (:a, :agenda, now(), 'skipped', 'perdida')",
            {"agenda": world.a_published["schedule"]},
        ),
        "webhooks": (
            "INSERT INTO webhooks (company_id, id, robot_id, title, key_salt, created_by, "
            "updated_by) VALUES (:a, :new, :robo, 'x', '\\x00'::bytea, 'b', 'b')",
            {"robo": world.a_robot},
        ),
        "webhook_deliveries": (
            "INSERT INTO webhook_deliveries (company_id, webhook_id, delivery_id, body_sha256, "
            "run_id) VALUES (:a, :webhook, 'x', repeat('0', 64), :run)",
            {"webhook": world.a_published["webhook"], "run": world.a_run},
        ),
        "batches": (
            "INSERT INTO batches (company_id, id, robot_id, version_id, file_name, file_format, "
            "file_sha256, total_rows, columns, created_by) VALUES (:a, :new, :robo, :new, 'x', "
            "'csv', repeat('0', 64), 1, '{}', 'b')",
            {"robo": world.a_robot},
        ),
        "batch_rows": (
            "INSERT INTO batch_rows (company_id, batch_id, row_no, line_no, run_id) "
            "VALUES (:a, :lote, 99, 2, :run)",
            {"lote": world.a_published["batch"], "run": world.a_run},
        ),
    }
    for table, (statement, extra) in attempts.items():
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(b.company_id) as session:
                await session.execute(
                    text(statement), {"new": new_id(), "a": a.company_id, **extra}
                )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", table
        assert "row-level security" in str(caught.value.orig), table

    # Mudar a empresa de uma linha própria para a da A também é barrado.
    with pytest.raises(DBAPIError) as caught:
        async with app_db.tenant_session(b.company_id) as session:
            await session.execute(text("UPDATE robots SET company_id = :a"), {"a": a.company_id})
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"

    # Atualizar ou apagar linhas da A não alcança nada.
    async with app_db.tenant_session(b.company_id) as session:
        updated = await session.execute(
            text("UPDATE robots SET title = 'tomado' WHERE id = :id"), {"id": world.a_robot}
        )
        deleted = await session.execute(
            text("DELETE FROM idempotency_keys WHERE company_id = :a"), {"a": a.company_id}
        )
    assert updated.rowcount == 0  # type: ignore[attr-defined]
    assert deleted.rowcount == 0  # type: ignore[attr-defined]


async def test_no_tenant_sees_nothing(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.engine.connect() as conn:
        # Conexão nova: o parâmetro nunca foi definido (NULL).
        for table in TENANT_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
            assert count == 0, table.name
        await conn.rollback()
        # Mesma conexão depois de uma transação com empresa: o valor local some ('').
        async with conn.begin():
            await conn.execute(
                text("SELECT set_config('rapa.company_id', :id, true)"), {"id": str(new_id())}
            )
        assert await conn.scalar(text("SELECT current_setting('rapa.company_id', true)")) == ""
        for table in TENANT_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
            assert count == 0, table.name
        await conn.rollback()
        # Sem empresa, nenhuma inclusão passa.
        with pytest.raises(DBAPIError) as caught:
            await conn.execute(
                text("INSERT INTO companies (id, name) VALUES (:id, 'x')"), {"id": new_id()}
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"


async def test_session_level_tenant_does_not_leak_through_pool(app_db: Database) -> None:
    company = new_id()
    async with app_db.tenant_session(company) as session:
        assert await session.scalar(text("SELECT current_setting('rapa.company_id')")) == str(
            company
        )
    async with app_db.dispatcher_session() as session:
        assert await session.scalar(text("SELECT current_setting('rapa.company_id', true)")) in (
            "",
            None,
        )
    with pytest.raises(TypeError):
        async with app_db.tenant_session(str(company)):  # type: ignore[arg-type]
            pass


async def test_dispatcher_writes_events_with_run_company(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """O company_id do evento vem da execução, nunca do executor."""
    world = await _world(app_db, dispatcher_db, superuser_engine)
    async with app_db.tenant_session(world.a.company_id) as session:
        rows = (
            await session.execute(
                text("SELECT company_id, kind FROM run_events WHERE run_id = :id"),
                {"id": world.a_run},
            )
        ).all()
    assert rows == [(world.a.company_id, "run_started")]
