"""Na hora de rodar: o certificado A1 é conferido ao enfileirar (todas as origens) e de novo
pelo despachante logo antes de lançar o executor; e os avisos de vencimento."""

import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import (
    CredentialAlerts,
    Dispatcher,
    PgBatches,
    PgRunQueue,
    PgSchedules,
    PgWebhooks,
    ReapReport,
    Scheduler,
    WebhookKeys,
)
from rapa.control.services.a1 import record_expiry_alerts
from rapa.core.contracts import (
    CertificateBlocked,
    RunnerJob,
    RunState,
    RunTrigger,
    ScheduleOccurrenceStatus,
    ScheduleSpec,
    WebhookSpec,
)
from rapa.core.ids import new_id
from rapa.runner.errors import ErrorCode
from tests.control.db.support import PROTOCOLO_INPUTS, protocolo, publish, reset_queue, result
from tests.control.publishing.support import SERVER_KEY, reset_schedules, signed

from .support import (
    CERT,
    OTHER_FINGERPRINT,
    A1World,
    a1_protocolo,
    a1_world,
    in_days,
    publish_a1,
    register_a1,
    set_not_after,
)

pytestmark = pytest.mark.database

SLUG = "emitir_protocolo"

type Breaker = Callable[[A1World, Any], Awaitable[None]]


async def _credential_id(world: A1World) -> UUID:
    [credential] = await world.credentials.list(world.custodian)
    return credential.credential_id


async def _revoke_credential(world: A1World, _: Any) -> None:
    await world.credentials.revoke(world.custodian, await _credential_id(world))


async def _expire(world: A1World, superuser: AsyncEngine) -> None:
    await set_not_after(superuser, await _credential_id(world), in_days(-1))


async def _revoke_authorization(world: A1World, _: Any) -> None:
    robot = next(r for r in await world.platform.list_robots(world.custodian) if r.slug == SLUG)
    [authorization] = await world.credentials.list_authorizations(world.custodian, robot.robot_id)
    await world.credentials.revoke_authorization(world.custodian, authorization.authorization_id)


async def _swap_certificate(world: A1World, _: Any) -> None:
    await world.credentials.renew(
        world.custodian,
        await _credential_id(world),
        not_after=in_days(400),
        fingerprint=OTHER_FINGERPRINT,
    )


async def _fingerprint_changed_behind(world: A1World, superuser: AsyncEngine) -> None:
    """A impressão muda direto no banco: a autorização ativa fica com a impressão antiga."""
    async with superuser.begin() as conn:
        await conn.execute(
            text("UPDATE credentials SET fingerprint_sha256 = :f WHERE id = :id"),
            {"f": OTHER_FINGERPRINT, "id": await _credential_id(world)},
        )


BREAKERS: dict[str, Breaker] = {
    "revogada": _revoke_credential,
    "vencida": _expire,
    "sem_autorizacao": _revoke_authorization,
    "impressao_trocada": _swap_certificate,
    "impressao_antiga": _fingerprint_changed_behind,
}


#: O que a lista de pendências diz em cada caso.
EXPECTED = {
    "revogada": "foi revogado",
    "vencida": "venceu em",
    "sem_autorizacao": "falta a autorização do custodiante",
    "impressao_trocada": "falta a autorização do custodiante",
    "impressao_antiga": "é de outro certificado",
}


@pytest.mark.parametrize("broken", list(BREAKERS))
async def test_every_origin_refuses_to_enqueue_a_blocked_certificate(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, broken: str
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    await reset_schedules(superuser_engine)
    world = await a1_world(app_db, f"Origens {broken}")
    tenant = world.tenant
    version = await publish_a1(world)
    # Antes de quebrar, roda (e a agenda, o webhook e o lote são criados).
    ok = await world.platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert ok.state is RunState.QUEUED
    schedules = PgSchedules(app_db)
    schedule = await schedules.create_schedule(
        tenant.operator,
        ScheduleSpec(
            robot_id=version.robot_id, title="Diária", cron="0 9 * * *", inputs=PROTOCOLO_INPUTS
        ),
    )
    webhooks = PgWebhooks(app_db, WebhookKeys(SERVER_KEY))
    hook = await webhooks.create_webhook(
        tenant.operator, version.robot_id, WebhookSpec(title="ERP")
    )

    await BREAKERS[broken](world, superuser_engine)

    # REST, MCP e a interface (o mesmo serviço, com a origem de cada um).
    for trigger in (RunTrigger.REST, RunTrigger.MCP, RunTrigger.MANUAL):
        with pytest.raises(CertificateBlocked) as caught:
            await world.platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, trigger)
        assert any(EXPECTED[broken] in e for e in caught.value.errors), caught.value.errors
    # A validação de uma versão nova (o rascunho também abriria o certificado).
    draft = await world.platform.create_version(tenant.creator, tenant.area_id, a1_protocolo())
    with pytest.raises(CertificateBlocked):
        await world.platform.start_validation(
            tenant.creator, draft.robot_id, draft.version_no, PROTOCOLO_INPUTS
        )
    # O webhook: a entrega é recusada (422) sem criar nada.
    body = json.dumps(PROTOCOLO_INPUTS).encode()
    with pytest.raises(CertificateBlocked):
        await webhooks.deliver(hook.webhook.webhook_id, **signed(hook.secret, body, "entrega-1"))
    # O lote: nenhuma linha entra.
    sheet = b"Nome completo;CPF;UF\r\nMaria;529.982.247-25;SP\r\n"
    with pytest.raises(CertificateBlocked):
        await PgBatches(app_db).create_batch(tenant.operator, version.robot_id, "lote.csv", sheet)
    # A agenda: a ocorrência fica registrada como ignorada, com o motivo.
    assert schedule.next_run_at is not None
    fired = await Scheduler(app_db, dispatcher_db).fire(
        tenant.company_id, schedule.schedule_id, schedule.next_run_at
    )
    assert fired == 0
    [occurrence] = await schedules.list_occurrences(tenant.operator, schedule.schedule_id)
    assert occurrence.status is ScheduleOccurrenceStatus.SKIPPED
    assert occurrence.reason == "certificado_bloqueado"

    async with superuser_engine.connect() as conn:
        triggers = (
            await conn.execute(
                text("SELECT trigger FROM runs WHERE company_id = :c ORDER BY created_at"),
                {"c": tenant.company_id},
            )
        ).all()
        deliveries = await conn.scalar(
            text("SELECT count(*) FROM webhook_deliveries WHERE company_id = :c"),
            {"c": tenant.company_id},
        )
        batches = await conn.scalar(
            text("SELECT count(*) FROM batches WHERE company_id = :c"), {"c": tenant.company_id}
        )
    assert [t[0] for t in triggers] == ["rest"]
    assert deliveries == 0 and batches == 0


async def test_a_robot_without_a1_is_not_affected(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    world = await a1_world(app_db, "Sem A1")
    await register_a1(world)
    await world.credentials.revoke(world.custodian, await _credential_id(world))
    await publish(world.platform, world.tenant, protocolo())
    run = await world.platform.start(world.tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert run.state is RunState.QUEUED


class _Launches:
    def __init__(self) -> None:
        self.jobs: list[RunnerJob] = []

    async def __call__(self, job: RunnerJob) -> Mapping[str, Any]:
        self.jobs.append(job)
        return result(job.run_id, job.attempt, outputs={"protocolo": "1"})


@pytest.mark.parametrize("broken", list(BREAKERS))
async def test_the_dispatcher_checks_again_before_launching(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, broken: str
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    world = await a1_world(app_db, f"Despachante {broken}")
    tenant = world.tenant
    await publish_a1(world)
    queued = await world.platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    # Depois de entrar na fila, o certificado deixa de valer.
    await BREAKERS[broken](world, superuser_engine)
    launches = _Launches()
    dispatcher = Dispatcher(PgRunQueue(dispatcher_db), launches, heartbeat_interval=0.05)
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    assert launches.jobs == []  # nada abriu no navegador
    status = await world.platform.get(tenant.operator, queued.run_id)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == ErrorCode.CERTIFICADO_A1_BLOQUEADO.value
    assert status.error_message is not None
    assert "nada foi aberto no navegador" in status.error_message
    assert "empresas/" not in status.error_message


async def test_the_dispatcher_launches_when_the_certificate_is_fine(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    world = await a1_world(app_db, "Despachante com o certificado em dia")
    await publish_a1(world)
    queued = await world.platform.start(
        world.tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST
    )
    launches = _Launches()
    dispatcher = Dispatcher(PgRunQueue(dispatcher_db), launches, heartbeat_interval=0.05)
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    assert [job.run_id for job in launches.jobs] == [queued.run_id]
    status = await world.platform.get(world.tenant.operator, queued.run_id)
    assert status is not None and status.state is RunState.SUCCEEDED


class _QueueWithoutDatabase:
    """Fila falsa: a conferência do A1 falha (banco fora do ar)."""

    def __init__(self, job: RunnerJob) -> None:
        self.job: RunnerJob | None = job
        self.finished: list[Mapping[str, Any]] = []

    async def claim_next(self, *, now: datetime | None = None) -> RunnerJob | None:
        job, self.job = self.job, None
        return job

    async def a1_block(self, job: RunnerJob, *, now: datetime | None = None) -> str | None:
        raise ConnectionError("banco fora do ar")

    async def heartbeat(self, run_id: UUID, attempt: int, *, now: datetime | None = None) -> bool:
        return True

    async def finish(
        self, run_id: UUID, attempt: int, result: Mapping[str, Any], *, now: datetime | None = None
    ) -> bool:
        self.finished.append(result)
        return True

    async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
        return 0

    async def reap_expired(self, *, now: datetime | None = None, skip: Any = ()) -> ReapReport:
        return ReapReport()


async def test_the_dispatcher_does_not_launch_when_it_cannot_check() -> None:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=a1_protocolo(),
        inputs=PROTOCOLO_INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    queue = _QueueWithoutDatabase(job)
    launches = _Launches()
    dispatcher = Dispatcher(queue, launches)  # type: ignore[arg-type]
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    assert launches.jobs == []
    assert [(r["state"], r["error_code"]) for r in queue.finished] == [
        ("failed", "certificado_a1_bloqueado")
    ]


# -- avisos de vencimento ----------------------------------------------------------------


async def _alerts(superuser: AsyncEngine, credential_id: UUID) -> list[tuple[Any, ...]]:
    async with superuser.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT day, kind FROM credential_alerts WHERE credential_id = :id ORDER BY day, kind"
            ),
            {"id": credential_id},
        )
        return [tuple(row) for row in rows]


async def test_expiry_alerts_once_a_day(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = await a1_world(app_db, "Avisos")
    tenant = world.tenant
    soon = await register_a1(world, not_after=in_days(20))
    far = await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name="certificado_longe",
        kind=soon.kind,
        origin=soon.origin,
        vault_path="empresas/x/longe",
        custodian_subject=world.custodian.subject,
        not_after=in_days(200),
    )
    revoked = await world.credentials.register(
        world.area_admin,
        tenant.area_id,
        name="certificado_revogado",
        kind=soon.kind,
        origin=soon.origin,
        vault_path="empresas/x/revogado",
        custodian_subject=world.custodian.subject,
        not_after=in_days(5),
    )
    await world.credentials.revoke(world.custodian, revoked.credential_id)
    alerts = CredentialAlerts(dispatcher_db, warn_days=30)
    # Meio-dia em Brasília: o dia do aviso não muda com o fuso.
    today = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0)
    caplog.set_level(logging.WARNING, logger="rapa.control.a1")
    await alerts.check(today)
    await alerts.check(today + timedelta(hours=6))  # a segunda volta do dia não repete
    assert await _alerts(superuser_engine, soon.credential_id) == [(today.date(), "vencendo")]
    assert await _alerts(superuser_engine, far.credential_id) == []
    assert await _alerts(superuser_engine, revoked.credential_id) == []
    # No dia seguinte, outro aviso; depois de vencer, o aviso é de vencido.
    tomorrow = today + timedelta(days=1)
    await alerts.check(tomorrow)
    after = today + timedelta(days=21)
    await alerts.check(after)
    await alerts.check(after + timedelta(hours=1))
    assert await _alerts(superuser_engine, soon.credential_id) == [
        (today.date(), "vencendo"),
        (tomorrow.date(), "vencendo"),
        (after.date(), "vencida"),
    ]
    async with superuser_engine.connect() as conn:
        actions = Counter(
            row[0]
            for row in await conn.execute(
                text("SELECT action FROM audit_log WHERE company_id = :c AND target = :t"),
                {"c": tenant.company_id, "t": f"credencial:{soon.credential_id}"},
            )
        )
    assert actions["credencial.a1_vencendo"] == 2 and actions["credencial.a1_vencida"] == 1
    logged = [r.getMessage() for r in caplog.records if r.name == "rapa.control.a1"]
    assert any(CERT in m and str(tenant.company_id) in m for m in logged)
    assert not any("empresas/" in m for m in logged)


async def test_expiry_alerts_reach_every_certificate_in_batches(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Os certificados vencidos que ninguém revoga não tomam o lugar dos outros: cada lote
    pega só os que ainda não têm o aviso do dia, e a volta segue até acabar."""
    world = await a1_world(app_db, "Avisos em lotes")
    first = await register_a1(world, not_after=in_days(10))
    ids = [first.credential_id]
    for number in range(4):
        info = await world.credentials.register(
            world.area_admin,
            world.tenant.area_id,
            name=f"certificado_{number}",
            kind=first.kind,
            origin=first.origin,
            vault_path=f"empresas/x/lote-{number}",
            custodian_subject=world.custodian.subject,
            not_after=in_days(11 + number),
        )
        ids.append(info.credential_id)
    today = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0)

    # Um lote de 2, duas vezes: o segundo pega outros dois, não os mesmos de novo.
    async with dispatcher_db.dispatcher_session() as session:
        batch_one = await record_expiry_alerts(session, now=today, within_days=30, limit=2)
    async with dispatcher_db.dispatcher_session() as session:
        batch_two = await record_expiry_alerts(session, now=today, within_days=30, limit=2)
    assert batch_one.seen == 2 and batch_two.seen == 2
    one = {a.credential_id for a in batch_one.alerts}
    two = {a.credential_id for a in batch_two.alerts}
    assert len(one) == 2 and len(two) == 2 and not one & two

    # A etapa repete os lotes até não sobrar nenhum: todos os cinco têm o aviso do dia.
    await CredentialAlerts(dispatcher_db, warn_days=30, batch_size=2).check(today)
    for credential_id in ids:
        assert await _alerts(superuser_engine, credential_id) == [(today.date(), "vencendo")]
    # E a volta seguinte do dia não acha mais nada para estes.
    async with dispatcher_db.dispatcher_session() as session:
        again = await record_expiry_alerts(session, now=today, within_days=30, limit=1000)
    assert not {a.credential_id for a in again.alerts} & set(ids)


async def test_alert_step_runs_in_the_dispatcher_loop_and_its_failure_does_not_stop_it(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    world = await a1_world(app_db, "Etapa dos avisos")
    soon = await register_a1(world, not_after=in_days(3))

    class Broken:
        calls = 0

        async def check(self, now: datetime | None = None) -> int:
            Broken.calls += 1
            raise RuntimeError("falha inesperada")

    dispatcher = Dispatcher(
        PgRunQueue(dispatcher_db),
        _Launches(),
        alerts=CredentialAlerts(dispatcher_db),
        alerts_interval=3600,
    )
    await dispatcher.maintenance()
    assert await _alerts(superuser_engine, soon.credential_id) != []
    assert "certificates" not in dispatcher.failing
    failing = Dispatcher(PgRunQueue(dispatcher_db), _Launches(), alerts=Broken(), alerts_interval=0)
    await failing.maintenance()
    assert Broken.calls == 1 and "certificates" in failing.failing
    # O despacho segue.
    assert await failing.run_once() >= 0
