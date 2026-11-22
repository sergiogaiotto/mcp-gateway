"""Empresa congelada: lê tudo, mas nada novo acontece; a fila é cancelada e o que roda termina."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.api.publishing import PublishingServices
from rapa.control.auth import StaticTokenVerifier
from rapa.control.db import Database
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.services import PgRepairs, PgRunQueue, Scheduler
from rapa.control.services.lifecycle import unfreeze_company
from rapa.control.services.repairs import FROZEN_SUGGESTION
from rapa.control.services.sessions import PgSessionStore
from rapa.control.sessions import LiveRelay, RecordingSessions, SessionLimits
from rapa.core.contracts import (
    COMPANY_FROZEN_MESSAGE,
    CompanyFrozen,
    Forbidden,
    RepairSuggestionStatus,
    RunState,
    RunTrigger,
    ScheduleChanges,
    ScheduleSpec,
    WebhookChanges,
    WebhookSpec,
)
from rapa.llm.repair import RepairAnswer
from tests.control.api.support import DictResolver, asgi_client, auth
from tests.control.db.support import PROTOCOLO_INPUTS, failed_run, protocolo, result
from tests.control.publishing.support import (
    FakeClock,
    World,
    build_world,
    protocol_sheet,
    runs_of,
    signed,
)
from tests.control.repairs.test_service import FakeAssistant
from tests.control.sessions.support import FakeLauncher
from tests.repair.support import login_page

from .support import audit_actions, copy_row, freeze

pytestmark = pytest.mark.database

SLUG = "emitir_protocolo"


@pytest.fixture
async def world(app_db: Database, superuser_engine: AsyncEngine) -> AsyncIterator[World]:
    yield await build_world(app_db, superuser_engine)


async def _runs(superuser: AsyncEngine, company: Any) -> list[dict[str, Any]]:
    async with superuser.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, state, trigger, error_code FROM runs WHERE company_id = :c "
                        "ORDER BY created_at, id"
                    ),
                    {"c": company},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def test_frozen_company_refuses_every_write_path(
    world: World, app_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    a = world.a
    robot = world.robots_a[SLUG]
    # Antes de congelar: uma versão validada (para publicar), um rascunho (para validar),
    # uma agenda e um webhook (para mudar).
    validated = await world.platform.create_version(a.creator, a.area_id, protocolo())
    await world.platform.mark_validated(a.creator, robot, validated.version_no)
    draft = await world.platform.create_version(a.creator, a.area_id, protocolo())
    schedule = await world.schedules.create_schedule(
        a.operator,
        ScheduleSpec(robot_id=robot, title="Diária", cron="0 9 * * *", inputs=PROTOCOLO_INPUTS),
    )
    hook = await world.webhooks.create_webhook(a.operator, robot, WebhookSpec(title="ERP"))
    before = len(await _runs(superuser_engine, a.company_id))

    report = await freeze(owner_engine, a.company_id)
    assert report.changed

    # Toda origem de execução: REST, MCP, interface e a validação.
    for trigger in (RunTrigger.REST, RunTrigger.MCP, RunTrigger.MANUAL):
        with pytest.raises(CompanyFrozen) as caught:
            await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, trigger)
        assert "a empresa está congelada" in caught.value.message
    with pytest.raises(CompanyFrozen):
        await world.platform.start_validation(a.creator, robot, draft.version_no, PROTOCOLO_INPUTS)
    # Versões: criar, validar e publicar.
    with pytest.raises(CompanyFrozen):
        await world.platform.create_version(a.creator, a.area_id, protocolo())
    with pytest.raises(CompanyFrozen):
        await world.platform.mark_validated(a.creator, robot, draft.version_no)
    with pytest.raises(CompanyFrozen):
        await world.platform.publish(a.approver, robot, validated.version_no)
    # Agendas e webhooks: criar e mudar.
    with pytest.raises(CompanyFrozen):
        await world.schedules.create_schedule(
            a.operator,
            ScheduleSpec(robot_id=robot, title="Outra", cron="0 10 * * *", inputs=PROTOCOLO_INPUTS),
        )
    with pytest.raises(CompanyFrozen):
        await world.schedules.update_schedule(
            a.operator, schedule.schedule_id, ScheduleChanges(cron="0 11 * * *")
        )
    with pytest.raises(CompanyFrozen):
        await world.webhooks.create_webhook(a.operator, robot, WebhookSpec(title="Outro"))
    with pytest.raises(CompanyFrozen):
        await world.webhooks.update_webhook(
            a.operator, robot, hook.webhook.webhook_id, WebhookChanges(rate_per_minute=10)
        )
    with pytest.raises(CompanyFrozen):
        await world.webhooks.rotate_secret(a.operator, robot, hook.webhook.webhook_id)
    # Lote: recusado antes de ler a planilha.
    with pytest.raises(CompanyFrozen):
        await world.batches.create_batch(a.operator, robot, "lote.csv", protocol_sheet())
    # Sessão de gravação: nem chega a abrir o navegador.
    launcher = FakeLauncher()
    sessions = RecordingSessions(
        PgSessionStore(app_db),
        launcher,
        world.platform,
        limits=SessionLimits(per_company=2),
        relay=LiveRelay(stop_timeout_s=5),
    )
    try:
        with pytest.raises(CompanyFrozen):
            await sessions.create_session(a.creator, a.area_id, "https://portal.exemplo.gov.br/")
        # A revisão de uma gravação também sabe que a empresa está congelada (e não
        # pergunta ao modelo); a outra empresa não.
        assert await sessions.store.company_frozen(a.company_id)
        assert not await sessions.store.company_frozen(world.b.company_id)
    finally:
        await sessions.aclose()
    assert launcher.jobs == []

    # Nada novo foi criado; ler continua valendo, e o que só diminui também.
    assert len(await _runs(superuser_engine, a.company_id)) == before
    assert [s.schedule_id for s in await world.schedules.list_schedules(a.operator)] == [
        schedule.schedule_id
    ]
    assert {r.slug for r in await world.platform.list_robots(a.creator)} >= {SLUG}
    await world.schedules.delete_schedule(a.operator, schedule.schedule_id)
    await world.webhooks.delete_webhook(a.operator, robot, hook.webhook.webhook_id)
    # A outra empresa nem percebe.
    other = await world.platform.start(world.b.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert other.state is RunState.QUEUED

    # Descongelar volta: tudo pode de novo.
    assert await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    again = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert again.state is RunState.QUEUED
    published = await world.platform.publish(a.approver, robot, validated.version_no)
    assert published.status.value == "published"
    actions = await audit_actions(superuser_engine, a.company_id)
    assert "empresa.congelada" in actions and "empresa.descongelada" in actions


async def test_freezing_cancels_the_queue_and_running_runs_finish(
    world: World, dispatcher_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    a, b = world.a, world.b
    queue = PgRunQueue(dispatcher_db)
    running = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    job = await queue.claim_next()
    assert job is not None and job.run_id == running.run_id
    waiting = [
        await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
        for _ in range(2)
    ]
    other = await world.platform.start(b.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)

    report = await freeze(owner_engine, a.company_id)
    assert sorted(report.cancelled) == sorted(w.run_id for w in waiting)
    states = {row["id"]: row for row in await _runs(superuser_engine, a.company_id)}
    for queued in waiting:
        assert states[queued.run_id]["state"] == "cancelled"
        assert states[queued.run_id]["error_code"] == "empresa_congelada"
    assert states[running.run_id]["state"] == "running"
    actions = await audit_actions(superuser_engine, a.company_id)
    assert actions.count("execucao.cancelada") == 2
    # Congelar de novo não muda nada.
    assert not (await freeze(owner_engine, a.company_id)).changed

    # A que estava rodando termina normalmente.
    assert await queue.finish(job.run_id, job.attempt, result(job.run_id, job.attempt))
    assert (await world.platform.get(a.operator, running.run_id)).state is RunState.SUCCEEDED  # type: ignore[union-attr]

    # O despachante não pega execução de empresa congelada, mesmo uma que apareça na fila:
    # nem comum, nem interativa (a validação, que usa a reserva), mesmo mais antiga que a
    # interativa da outra empresa. O banco já recusa a linha nova (veja
    # test_database_refuses_new_rows_of_a_frozen_company): aqui as travas ficam desligadas,
    # para conferir a segunda defesa, a do despachante.
    copy = (
        "INSERT INTO runs (id, company_id, area_id, robot_id, version_id, trigger, mode, "
        "priority, requested_by, queued_at) SELECT gen_random_uuid(), company_id, area_id, "
        "robot_id, version_id, :trigger, :mode, :priority, 'teste', now() - :age * interval '1 s' "
        "FROM runs WHERE id = :id"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        for source, trigger, mode, priority, age in (
            (running.run_id, "rest", "full", 1, 0),
            (running.run_id, "validation", "until_first_write", 0, 60),
            (other.run_id, "validation", "until_first_write", 0, 30),
        ):
            await conn.execute(
                text(copy),
                {"id": source, "trigger": trigger, "mode": mode, "priority": priority, "age": age},
            )
    claimed = [await queue.claim_next() for _ in range(3)]
    assert [job.company_id if job else None for job in claimed] == [
        b.company_id,
        b.company_id,
        None,
    ]
    assert claimed[1] is not None and claimed[1].run_id == other.run_id
    # Descongelada, a fila dela volta a andar.
    await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    late = await queue.claim_next()
    assert late is not None and late.company_id == a.company_id


async def test_reaper_cancels_instead_of_requeueing_a_frozen_company(
    world: World, dispatcher_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    a, b = world.a, world.b
    queue = PgRunQueue(dispatcher_db)
    run_a = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    run_b = await world.platform.start(b.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert await queue.claim_next() is not None
    assert await queue.claim_next() is not None
    await freeze(owner_engine, a.company_id)
    report = await queue.reap_expired(now=datetime.now(UTC) + timedelta(hours=1))
    # Nada tinha gravado: a da empresa congelada é cancelada, a outra volta para a fila.
    assert report.cancelled == [run_a.run_id]
    assert report.requeued == [run_b.run_id]
    row = next(r for r in await _runs(superuser_engine, a.company_id) if r["id"] == run_a.run_id)
    assert (row["state"], row["error_code"]) == ("cancelled", "empresa_congelada")


async def test_schedule_occurrence_is_recorded_as_skipped(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    clock = FakeClock(datetime(2026, 11, 20, 11, 59, tzinfo=UTC))
    world = await build_world(app_db, superuser_engine, clock=clock)
    a = world.a
    schedule = await world.schedules.create_schedule(
        a.operator,
        ScheduleSpec(
            robot_id=world.robots_a[SLUG],
            title="Meio-dia",
            cron="0 12 * * *",
            timezone="UTC",
            inputs=PROTOCOLO_INPUTS,
        ),
    )
    await freeze(owner_engine, a.company_id)
    scheduler = Scheduler(app_db, dispatcher_db, clock=clock)
    clock.now = datetime(2026, 11, 20, 12, 0, 30, tzinfo=UTC)
    assert await scheduler.tick() == 0
    occurrences = await world.schedules.list_occurrences(a.operator, schedule.schedule_id)
    assert [(o.status.value, o.reason) for o in occurrences] == [("skipped", "empresa_congelada")]
    assert await runs_of(superuser_engine, "schedule", a.company_id) == []
    # Descongelada, a próxima ocorrência roda.
    await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    clock.now = datetime(2026, 11, 21, 12, 0, 30, tzinfo=UTC)
    assert await scheduler.tick() == 1


async def test_webhook_answers_423_without_creating_anything(
    world: World, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    a = world.a
    hook = await world.webhooks.create_webhook(
        a.operator, world.robots_a[SLUG], WebhookSpec(title="ERP")
    )
    app = create_http_app(
        catalog=world.platform,
        runs=world.platform,
        admin=world.platform,
        verifier=StaticTokenVerifier({"token-operador": a.operator}),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        publishing=PublishingServices(
            schedules=world.schedules, webhooks=world.webhooks, receiver=world.webhooks
        ),
    )
    body = b'{"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}'
    path = f"/v1/webhooks/{hook.webhook.webhook_id}"
    await freeze(owner_engine, a.company_id)
    async with asgi_client(app) as client:
        frozen = signed(hook.secret, body, "entrega-1", at=world.clock.now)
        headers = {
            "X-RaPA-Timestamp": frozen["timestamp"],
            "X-RaPA-Delivery": frozen["delivery_id"],
            "X-RaPA-Signature": frozen["signature"],
        }
        response = await client.post(path, content=body, headers=headers)
        assert response.status_code == 423, response.text
        assert response.json()["erro"] == "empresa_congelada"
        # Sem a assinatura certa, continua 401: a situação da empresa não vaza.
        wrong = await client.post(
            path, content=body, headers={**headers, "X-RaPA-Signature": "sha256=" + "0" * 64}
        )
        assert wrong.status_code == 401
        # O REST também responde 423 a quem quer começar algo.
        start = await client.post(
            f"/v1/catalogo/{SLUG}/execucoes",
            json={"entradas": PROTOCOLO_INPUTS},
            headers=auth("token-operador"),
        )
        assert start.status_code == 423
    assert await runs_of(superuser_engine, "webhook", a.company_id) == []
    async with superuser_engine.connect() as conn:
        deliveries = await conn.scalar(
            text("SELECT count(*) FROM webhook_deliveries WHERE company_id = :c"),
            {"c": a.company_id},
        )
    assert deliveries == 0


async def test_repair_is_refused_while_frozen(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    world = await build_world(app_db, superuser_engine)
    a = world.a
    run_id = await failed_run(
        app_db, dispatcher_db, a, snapshot=login_page().model_dump(mode="json")
    )
    assistant = FakeAssistant(RepairAnswer(candidate_id="c1", reason="Acessar é o login."))
    repairs = PgRepairs(app_db, assistant=assistant)
    await freeze(owner_engine, a.company_id)
    asked_before = (await audit_actions(superuser_engine, a.company_id)).count(
        "conserto.sugestao_pedida"
    )
    # Ver os candidatos é leitura; perguntar ao modelo e pedir o conserto, não.
    for suggest in (True, False):
        view = await repairs.get_repair(a.creator, run_id, suggest=suggest)
        assert view.candidates
        assert view.suggestion.status is RepairSuggestionStatus.OFF
        assert view.suggestion.message == FROZEN_SUGGESTION
        assert view.blocked_reason == COMPANY_FROZEN_MESSAGE
    assert assistant.calls == []
    actions = await audit_actions(superuser_engine, a.company_id)
    assert actions.count("conserto.sugestao_pedida") == asked_before
    with pytest.raises(CompanyFrozen):
        await repairs.create_repair(a.creator, run_id, view.candidates[0].candidate_id)
    await unfreeze_company(owner_engine, a.company_id, requested_by="teste")
    # Descongelada, a sugestão volta (e a pergunta entra na auditoria).
    active = await repairs.get_repair(a.creator, run_id)
    assert active.suggestion.status is RepairSuggestionStatus.CHOSEN
    assert active.blocked_reason is None and len(assistant.calls) == 1
    actions = await audit_actions(superuser_engine, a.company_id)
    assert actions.count("conserto.sugestao_pedida") == asked_before + 1
    proposal = await repairs.create_repair(a.creator, run_id, view.candidates[0].candidate_id)
    assert proposal.run_id == run_id


async def test_only_the_table_owner_freezes(
    world: World, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    with pytest.raises(Forbidden) as caught:
        await freeze(app_db.engine, world.a.company_id)
    assert "dono das tabelas" in caught.value.message
    # E a aplicação nem tem como mudar a situação de uma empresa por SQL.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError) as denied:
        async with app_db.tenant_session(world.a.company_id) as session:
            await session.execute(text("UPDATE companies SET status = 'active'"))
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    async with superuser_engine.connect() as conn:
        status = await conn.scalar(
            text("SELECT status FROM companies WHERE id = :c"), {"c": world.a.company_id}
        )
    assert status == "active"


async def test_freeze_waits_for_writes_in_progress(
    world: World, app_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    """Congelar espera um início em andamento e cancela a execução que ele criou."""
    import asyncio

    from rapa.control.services._common import ensure_company_active
    from rapa.control.services.runs import enqueue_run

    a = world.a
    published = await world.platform.get_published(a.operator, SLUG)
    assert published is not None
    robot = {
        "robot_id": published.robot_id,
        "area_id": a.area_id,
        "version_id": published.version_id,
        "version_no": published.version_no,
    }
    async with app_db.tenant_session(a.company_id) as session:
        await ensure_company_active(session, a.company_id)
        freezing = asyncio.create_task(freeze(owner_engine, a.company_id))
        await asyncio.sleep(0.3)
        assert not freezing.done()  # espera a trava compartilhada desta escrita
        # A escrita em andamento ainda põe uma execução na fila (a empresa estava ativa).
        queued = await enqueue_run(
            session,
            company_id=a.company_id,
            requested_by=a.operator.subject,
            robot=robot,
            slug=SLUG,
            inputs=PROTOCOLO_INPUTS,
            trigger=RunTrigger.REST,
        )
        assert queued.created
    report = await freezing
    # Quem congelou esperou a escrita terminar e cancelou a execução que ela criou.
    assert report.changed and report.cancelled == [queued.run_id]
    row = next(r for r in await _runs(superuser_engine, a.company_id) if r["id"] == queued.run_id)
    assert (row["state"], row["error_code"]) == ("cancelled", "empresa_congelada")
    with pytest.raises(CompanyFrozen):
        await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)


#: As tabelas em que o banco recusa uma linha nova de empresa congelada (as do
#: certificado A1, da revisão 0006, ficam em ``test_parts.py``).
_FROZEN_GUARDED = (
    "runs",
    "robots",
    "robot_versions",
    "schedules",
    "webhooks",
    "batches",
    "recording_sessions",
    "repair_proposals",
)


async def test_database_refuses_new_rows_of_a_frozen_company(
    world: World,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    """Mesmo um caminho que esqueça ensure_company_active não cria nada numa empresa congelada."""
    a, b = world.a, world.b
    robot = world.robots_a[SLUG]
    # Uma linha da empresa em cada tabela guardada.
    run_id = await failed_run(
        app_db, dispatcher_db, a, snapshot=login_page().model_dump(mode="json")
    )
    repairs = PgRepairs(app_db)
    view = await repairs.get_repair(a.creator, run_id, suggest=False)
    await repairs.create_repair(a.creator, run_id, view.candidates[0].candidate_id)
    await world.schedules.create_schedule(
        a.operator,
        ScheduleSpec(robot_id=robot, title="Diária", cron="0 9 * * *", inputs=PROTOCOLO_INPUTS),
    )
    await world.webhooks.create_webhook(a.operator, robot, WebhookSpec(title="ERP"))
    await world.batches.create_batch(a.operator, robot, "lote.csv", protocol_sheet())
    # Um rascunho (a versão que se copia: uma versão nova nasce rascunho).
    await world.platform.create_version(a.creator, a.area_id, protocolo())
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recording_sessions (company_id, id, area_id, owner, start_url, "
                "status, started_at, stopped_at, expires_at, seen_at) VALUES (:c, "
                "gen_random_uuid(), :area, 'criadora', 'https://portal.exemplo.gov.br/', "
                "'stopped', now(), now(), now() + interval '1 hour', now())"
            ),
            {"c": a.company_id, "area": a.area_id},
        )
    # Ativa, a cópia só esbarra na chave primária: nenhuma outra trava recusa antes.
    for table in _FROZEN_GUARDED:
        assert (await copy_row(app_db, a.company_id, table))[0] == "23505", table
    await freeze(owner_engine, a.company_id)
    for table in _FROZEN_GUARDED:
        code, message = await copy_row(app_db, a.company_id, table)
        assert code == "23514", table
        assert "a empresa está congelada" in message, table
    # A outra empresa nem percebe.
    await world.platform.start(b.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert (await copy_row(app_db, b.company_id, "runs"))[0] == "23505"


async def test_database_guard_waits_for_a_freeze_in_progress(
    world: World, app_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    """A linha nova espera o congelamento em andamento (a mesma trava) e vê a empresa congelada."""
    import asyncio

    from rapa.control.services._common import COMPANY_STATUS_LOCK_CLASS

    a = world.a
    run = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET state = 'cancelled', finished_at = now() WHERE id = :id"),
            {"id": run.run_id},
        )
    async with owner_engine.begin() as conn:
        # O começo de freeze_company: a trava exclusiva e a situação, ainda sem confirmar.
        await conn.execute(
            text("SELECT set_config('rapa.company_id', :c, true)"), {"c": str(a.company_id)}
        )
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:k, hashtext(:c))"),
            {"k": COMPANY_STATUS_LOCK_CLASS, "c": str(a.company_id)},
        )
        await conn.execute(
            text(
                "UPDATE companies SET status = 'frozen', status_reason = 'teste', "
                "status_changed_at = now() WHERE id = :c"
            ),
            {"c": a.company_id},
        )
        inserting = asyncio.create_task(copy_row(app_db, a.company_id, "runs"))
        await asyncio.sleep(0.3)
        assert not inserting.done()  # espera a trava de quem congela
    code, message = await inserting
    assert code == "23514" and "a empresa está congelada" in message


def test_cancelled_code_is_the_runner_error_code() -> None:
    """O código das execuções canceladas no congelamento é o de ``ErrorCode``."""
    from rapa.control.services.queue import COMPANY_FROZEN_CODE
    from rapa.runner.errors import ErrorCode

    assert ErrorCode.EMPRESA_CONGELADA.value == COMPANY_FROZEN_CODE
