"""Sessões de gravação no PostgreSQL: limites sob trava, isolamento, travas e a reserva na fila."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgPlatform, PgRunQueue, verify_chain
from rapa.control.services.queue import _loads
from rapa.control.services.sessions import PgSessionStore
from rapa.control.sessions import LiveRelay, RecordingSessions, SessionLimits, SessionRecord
from rapa.control.sessions.service import STALE_AFTER
from rapa.core.contracts import (
    DraftChoices,
    QuotaExceeded,
    RecordingSessionStatus,
    StepNameChoice,
)
from rapa.core.ids import new_id
from tests.control.db.support import Tenant, make_tenant, notas, publish, reset_queue
from tests.control.sessions.support import DEMO_PASSWORD, FakeLauncher

pytestmark = pytest.mark.database

URL = "https://portal.exemplo.gov.br/login"


def _record(tenant: Tenant, **changes: object) -> SessionRecord:
    now = datetime.now(UTC)
    data: dict[str, object] = {
        "company_id": tenant.company_id,
        "session_id": new_id(),
        "area_id": tenant.area_id,
        "owner": tenant.creator.subject,
        "title": "",
        "start_url": URL,
        "status": RecordingSessionStatus.ACTIVE,
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
    }
    data.update(changes)
    return SessionRecord(**data)  # type: ignore[arg-type]


async def test_service_on_postgres_from_session_to_draft(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=30)
    tenant = await make_tenant(app_db, "Gravação PG")
    launcher = FakeLauncher()
    service = RecordingSessions(
        PgSessionStore(app_db),
        launcher,
        PgPlatform(app_db),
        limits=SessionLimits(per_company=1),
        relay=LiveRelay(stop_timeout_s=10),
    )
    try:
        info = await service.create_session(tenant.creator, tenant.area_id, URL, "Cadastro")
        assert info.status is RecordingSessionStatus.ACTIVE and info.started_at is not None
        with pytest.raises(QuotaExceeded):
            await service.create_session(tenant.admin, tenant.area_id, URL)
        assert await service.get_session(tenant.outsider, info.session_id) is None
        stopped = await service.stop_session(tenant.creator, info.session_id)
        assert stopped.status is RecordingSessionStatus.STOPPED and stopped.recording_id
        # A vaga da empresa volta quando a sessão para.
        second = await service.create_session(tenant.admin, tenant.area_id, URL)
        proposals = await service.get_proposals(tenant.creator, info.session_id)
        assert not proposals.model_available
        version = await service.create_draft(
            tenant.creator,
            info.session_id,
            DraftChoices(
                name="cadastro_gravado",
                title="Cadastro gravado",
                description="Cadastra uma pessoa.",
                step_names=[StepNameChoice(step_id="clicar_salvar", title="Salvar a pessoa")],
            ),
        )
        assert version.created_by == tenant.creator.subject and version.violations == []
        drafted = await service.get_session(tenant.creator, info.session_id)
        assert drafted is not None and drafted.status is RecordingSessionStatus.DRAFTED
        assert drafted.robot_id == version.robot_id
        assert [s.session_id for s in await service.list_sessions(tenant.creator)] == [
            info.session_id
        ]
        assert [s.session_id for s in await service.list_sessions(tenant.admin)] == [
            second.session_id
        ]
    finally:
        await service.aclose()
        await launcher.aclose()
    async with superuser_engine.connect() as conn:
        actions = (
            await conn.scalars(
                text(
                    "SELECT action FROM audit_log WHERE company_id = :c AND action LIKE 'gravacao.%' "
                    "ORDER BY seq"
                ),
                {"c": tenant.company_id},
            )
        ).all()
        dumps = [
            json.dumps(row, default=str, ensure_ascii=False)
            for table in ("recording_sessions", "recordings", "audit_log", "robot_versions")
            for row in (
                await conn.execute(
                    text(f"SELECT row_to_json(t) FROM {table} t WHERE company_id = :c"),  # noqa: S608
                    {"c": tenant.company_id},
                )
            ).scalars()
        ]
    assert actions == [
        "gravacao.sessao_criada",
        "gravacao.sessao_parada",
        "gravacao.sessao_criada",
        "gravacao.rascunho_criado",
    ]
    assert dumps and all(DEMO_PASSWORD not in dump for dump in dumps)
    async with app_db.tenant_session(tenant.company_id) as session:
        assert (await verify_chain(session, tenant.company_id)).ok


async def test_company_cap_holds_under_concurrent_openings(app_db: Database) -> None:
    tenant = await make_tenant(app_db, "Concorrência")
    store = PgSessionStore(app_db)
    results = await asyncio.gather(
        *(store.insert(_record(tenant), company_limit=2) for _ in range(6)),
        return_exceptions=True,
    )
    assert sum(1 for r in results if r is None) == 2
    assert all(isinstance(r, QuotaExceeded) for r in results if r is not None)
    assert len(await store.list_live(tenant.company_id)) == 2
    # Uma sessão vencida pelo prazo não conta no limite.
    other = await make_tenant(app_db, "Outra concorrência")
    now = datetime.now(UTC)
    await store.insert(
        _record(other, created_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)),
        company_limit=1,
    )
    await store.insert(_record(other), company_limit=1)
    with pytest.raises(QuotaExceeded):
        await store.insert(_record(other), company_limit=1)


async def test_isolation_guards_and_permissions(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    a = await make_tenant(app_db, "Isola A")
    b = await make_tenant(app_db, "Isola B")
    store = PgSessionStore(app_db)
    record = _record(a)
    await store.insert(record, company_limit=5)
    await store.save_recording(
        a.company_id,
        record.session_id,
        {"version": 1, "started_at": datetime.now(UTC).isoformat(), "events": []},
        actor=a.creator.subject,
        stopped_at=datetime.now(UTC),
    )
    # A empresa B não vê nem toca as linhas da A.
    assert await store.get(b.company_id, record.session_id) is None
    assert await store.get_recording(b.company_id, record.session_id) is None
    async with app_db.tenant_session(b.company_id) as session:
        for table in ("recording_sessions", "recordings"):
            count = await session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count == 0, table
        updated = await session.execute(
            text("UPDATE recording_sessions SET status = 'failed' WHERE id = :id"),
            {"id": record.session_id},
        )
        assert updated.rowcount == 0  # type: ignore[attr-defined]
    refused = {
        # Dona, área e endereço nunca mudam; parada não volta a gravar.
        "UPDATE recording_sessions SET status = 'active' WHERE id = :id": "23514",
        "UPDATE recording_sessions SET owner = 'outra' WHERE id = :id": "42501",
        # A gravação é imutável, e a aplicação não apaga nada.
        "UPDATE recordings SET recording = '{}' WHERE session_id = :id": "42501",
        "DELETE FROM recording_sessions WHERE id = :id": "42501",
        "DELETE FROM recordings WHERE session_id = :id": "42501",
    }
    for statement, sqlstate in refused.items():
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(a.company_id) as session:
                await session.execute(text(statement), {"id": record.session_id})
        assert getattr(caught.value.orig, "sqlstate", None) == sqlstate, statement
    # Nem o superusuário troca a dona nem reabre a sessão (a trava vale para todos).
    for statement in (
        "UPDATE recording_sessions SET owner = 'outra' WHERE id = :id",
        "UPDATE recordings SET created_by = 'outra' WHERE session_id = :id",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with superuser_engine.begin() as conn:
                await conn.execute(text(statement), {"id": record.session_id})
        assert getattr(caught.value.orig, "sqlstate", None) == "23514", statement
    # O despachante só lê a situação das sessões, nunca a gravação.
    async with dispatcher_db.dispatcher_session() as session:
        assert (
            await session.scalar(
                text("SELECT count(company_id) FROM recording_sessions WHERE status = 'stopped'")
            )
            >= 1
        )
    for statement in (
        "SELECT owner FROM recording_sessions",
        "SELECT recording FROM recordings",
        "UPDATE recording_sessions SET status = 'failed'",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with dispatcher_db.dispatcher_session() as session:
                await session.execute(text(statement))
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", statement


async def test_queue_counts_live_sessions_in_the_interactive_reserve(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE recording_sessions SET status = 'expired', stopped_at = now() "
                "WHERE status IN ('starting', 'active')"
            )
        )
    tenant = await make_tenant(app_db, "Reserva")
    store = PgSessionStore(app_db)

    async def interactive() -> int:
        async with dispatcher_db.dispatcher_session() as session:
            _, running, _ = await _loads(session)
        return running

    assert await interactive() == 0
    live = _record(tenant)
    await store.insert(live, company_limit=5)
    await store.insert(_record(tenant, status=RecordingSessionStatus.STARTING), company_limit=5)
    assert await interactive() == 2
    await store.set_status(
        tenant.company_id,
        live.session_id,
        expected={RecordingSessionStatus.ACTIVE},
        status=RecordingSessionStatus.EXPIRED,
        end_reason="ociosa",
    )
    assert await interactive() == 1


async def test_guard_keeps_drafts_and_robot_links_honest(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=30)
    tenant = await make_tenant(app_db, "Trava do rascunho")
    launcher = FakeLauncher()
    service = RecordingSessions(
        PgSessionStore(app_db),
        launcher,
        PgPlatform(app_db),
        limits=SessionLimits(per_company=3),
        relay=LiveRelay(stop_timeout_s=10),
    )
    creator, area = tenant.creator, tenant.area_id
    choices = DraftChoices(
        name="cadastro_gravado", title="Cadastro gravado", description="Cadastra uma pessoa."
    )
    try:
        drafted = await service.create_session(creator, area, URL)
        await service.stop_session(creator, drafted.session_id)
        first = await service.create_draft(creator, drafted.session_id, choices)
        # Criar o rascunho de novo faz uma versão nova do mesmo robô: a versão avança.
        second = await service.create_draft(creator, drafted.session_id, choices)
        assert second.robot_id == first.robot_id and second.version_no == first.version_no + 1
        stopped = await service.create_session(creator, area, URL)
        await service.stop_session(creator, stopped.session_id)
        live = await service.create_session(creator, area, URL)
        params = {"robot": first.robot_id, "old": first.version_no}
        refused = [
            # Um rascunho não volta a ser só uma sessão parada.
            (drafted, "UPDATE recording_sessions SET status = 'stopped' WHERE id = :id", "23514"),
            # A versão de um mesmo robô só avança, e precisa existir.
            (drafted, "UPDATE recording_sessions SET version_no = :old WHERE id = :id", "23514"),
            (drafted, "UPDATE recording_sessions SET version_no = 99 WHERE id = :id", "23503"),
            # O robô só muda ao criar um rascunho de uma sessão parada.
            (
                stopped,
                "UPDATE recording_sessions SET robot_id = :robot, version_no = :old WHERE id = :id",
                "23514",
            ),
            (
                live,
                "UPDATE recording_sessions SET status = 'drafted', robot_id = :robot,"
                " version_no = :old WHERE id = :id",
                "23514",
            ),
            # O processo que abriu a sessão nunca muda.
            (live, "UPDATE recording_sessions SET instance_id = 'outro' WHERE id = :id", "23514"),
        ]
        for session, statement, sqlstate in refused:
            with pytest.raises(DBAPIError) as caught:
                async with superuser_engine.begin() as conn:
                    await conn.execute(text(statement), {**params, "id": session.session_id})
            assert getattr(caught.value.orig, "sqlstate", None) == sqlstate, statement
        # A aplicação renova a sessão aberta, mas não troca o processo dela.
        async with app_db.tenant_session(tenant.company_id) as session:
            await session.execute(
                text("UPDATE recording_sessions SET seen_at = now() WHERE id = :id"),
                {"id": live.session_id},
            )
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(tenant.company_id) as session:
                await session.execute(
                    text("UPDATE recording_sessions SET instance_id = 'outro' WHERE id = :id"),
                    {"id": live.session_id},
                )
        assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    finally:
        await service.aclose()
        await launcher.aclose()


async def test_reserve_in_use_heartbeat_and_sessions_nobody_holds(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE recording_sessions SET status = 'expired', stopped_at = now() "
                "WHERE status IN ('starting', 'active')"
            )
        )
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Quem segura")
    # Sem o papel do despachante, a API não enxerga as outras empresas.
    alone = PgSessionStore(app_db)
    assert await alone.reserve_in_use(now=datetime.now(UTC)) is None
    assert await alone.list_stale(before=datetime.now(UTC)) is None
    store = PgSessionStore(app_db, fleet=dispatcher_db)
    now = datetime.now(UTC)
    held = _record(tenant, instance_id="api-1", seen_at=now)
    orphan = _record(tenant, instance_id="api-0", seen_at=now - timedelta(minutes=10))
    for record in (held, orphan):
        await store.insert(record, company_limit=5)
    assert await store.reserve_in_use(now=now) == 2
    key_held, key_orphan = (
        (tenant.company_id, held.session_id),
        (tenant.company_id, orphan.session_id),
    )
    stale = await store.list_stale(before=now - STALE_AFTER)
    assert key_orphan in stale and key_held not in stale
    # Só o processo que segura a sessão a renova.
    await store.touch(tenant.company_id, [orphan.session_id], instance_id="api-1", at=now)
    assert key_orphan in await store.list_stale(before=now - STALE_AFTER)
    later = now + timedelta(minutes=5)
    await store.touch(tenant.company_id, [held.session_id], instance_id="api-1", at=later)
    assert key_held not in await store.list_stale(before=later - STALE_AFTER)
    assert (await store.get(*key_held)).seen_at == later  # type: ignore[union-attr]
    # Uma validação rodando ocupa a mesma reserva (na fila, ainda não).
    for record in (held, orphan):
        await store.set_status(
            tenant.company_id,
            record.session_id,
            expected={RecordingSessionStatus.ACTIVE},
            status=RecordingSessionStatus.EXPIRED,
            end_reason="perdida",
        )
    await publish(platform, tenant, notas())
    robot = await platform.get_published(tenant.creator, "baixar_notas")
    assert robot is not None
    await platform.start_validation(tenant.creator, robot.robot_id, robot.version_no, {})
    assert await store.reserve_in_use(now=now) == 0
    assert await PgRunQueue(dispatcher_db).claim_next() is not None
    assert await store.reserve_in_use(now=now) == 1
