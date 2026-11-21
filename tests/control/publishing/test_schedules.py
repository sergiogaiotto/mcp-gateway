"""Agendas no PostgreSQL: regras, próxima ocorrência, idempotência com dois despachantes e atrasos."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import (
    Dispatcher,
    PgRunQueue,
    PgSchedules,
    ReapReport,
    Scheduler,
    verify_chain,
)
from rapa.control.services._common import PRIORITY_BY_TRIGGER
from rapa.control.services.audit import payload_digest
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    RunTrigger,
    ScheduleChanges,
    ScheduleOccurrenceStatus,
    ScheduleSpec,
)
from tests.control.db.support import PROTOCOLO_INPUTS, make_tenant, result

from .support import FakeClock, World, build_world, runs_of

pytestmark = pytest.mark.database

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # sexta-feira, 9h em São Paulo


async def _world(app_db: Database, superuser: AsyncEngine, **kwargs: Any) -> World:
    return await build_world(app_db, superuser, clock=FakeClock(NOW), **kwargs)


def _spec(world: World, **changes: Any) -> ScheduleSpec:
    data: dict[str, Any] = {
        "robot_id": world.robots_a["emitir_protocolo"],
        "title": "Protocolo diário",
        "cron": "0 9 * * 1-5",
        "inputs": PROTOCOLO_INPUTS,
    }
    data.update(changes)
    return ScheduleSpec(**data)


def _scheduler(app_db: Database, dispatcher_db: Database, **kwargs: Any) -> Scheduler:
    return Scheduler(app_db, dispatcher_db, **kwargs)


async def test_create_validates_and_computes_the_next_run(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    a = world.a
    info = await world.schedules.create_schedule(a.operator, _spec(world))
    # Sexta às 12h UTC (9h local, a própria ocorrência): a próxima é segunda às 9h.
    assert info.next_run_at == datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert info.timezone == "America/Sao_Paulo" and info.enabled
    assert info.robot_slug == "emitir_protocolo" and info.area_id == a.area_id
    # O administrador da empresa também pode; num fuso sem horário de verão de agora.
    other = await world.schedules.create_schedule(
        a.admin, _spec(world, cron="30 7 * * *", timezone="Europe/Lisbon", title="Lisboa")
    )
    assert other.next_run_at == datetime(2026, 11, 21, 7, 30, tzinfo=UTC)

    for bad, message in (
        ({"cron": "61 * * * *"}, "minuto"),
        ({"cron": "0 0 31 2 *"}, "nunca acontece"),
        ({"timezone": "Lua/Base"}, "fuso"),
        ({"inputs": {"nome": "Maria"}}, "campo obrigatório ausente: cpf"),
        ({"inputs": {**PROTOCOLO_INPUTS, "extra": 1}}, "campo não declarado: extra"),
        ({"title": "   "}, "em branco"),
    ):
        with pytest.raises(InvalidInput) as caught:
            await world.schedules.create_schedule(a.operator, _spec(world, **bad))
        assert any(message in detail for detail in caught.value.errors), (bad, caught.value.errors)
    # Valores das entradas nunca aparecem nas mensagens.
    with pytest.raises(InvalidInput) as caught:
        await world.schedules.create_schedule(
            a.operator, _spec(world, inputs={**PROTOCOLO_INPUTS, "cpf": "segredo-123"})
        )
    assert "segredo-123" not in repr(caught.value.errors)


async def test_only_operators_and_admins_of_the_area_manage(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    a, b = world.a, world.b
    for who in (a.creator, a.viewer):
        with pytest.raises(Forbidden):
            await world.schedules.create_schedule(who, _spec(world))
    # De outra área e de outra empresa, o robô não existe.
    for who in (a.outsider, b.admin):
        with pytest.raises(NotFound):
            await world.schedules.create_schedule(who, _spec(world))
    info = await world.schedules.create_schedule(a.operator, _spec(world))
    # Quem só lê vê; quem não tem papel na área, não.
    assert await world.schedules.get_schedule(a.viewer, info.schedule_id) == info
    assert [s.schedule_id for s in await world.schedules.list_schedules(a.viewer)] == [
        info.schedule_id
    ]
    for who in (a.outsider, b.admin):
        assert await world.schedules.get_schedule(who, info.schedule_id) is None
        assert await world.schedules.list_schedules(who) == []
        with pytest.raises(NotFound):
            await world.schedules.update_schedule(
                who, info.schedule_id, ScheduleChanges(enabled=False)
            )
        with pytest.raises(NotFound):
            await world.schedules.list_occurrences(who, info.schedule_id)
    for who in (a.viewer, a.creator):
        with pytest.raises(Forbidden):
            await world.schedules.update_schedule(
                who, info.schedule_id, ScheduleChanges(enabled=False)
            )
        with pytest.raises(Forbidden):
            await world.schedules.delete_schedule(who, info.schedule_id)


async def test_draft_robot_cannot_be_scheduled(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    tenant = await make_tenant(app_db, "Rascunhos")
    from tests.control.db.support import protocolo

    draft = await world.platform.create_version(tenant.creator, tenant.area_id, protocolo())
    with pytest.raises(Conflict):
        await world.schedules.create_schedule(
            tenant.operator,
            ScheduleSpec(
                robot_id=draft.robot_id, title="x", cron="* * * * *", inputs=PROTOCOLO_INPUTS
            ),
        )


async def test_tick_enqueues_each_occurrence_once(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(world.a.operator, _spec(world, cron="*/5 * * * *"))
    due = info.next_run_at
    assert due == NOW + timedelta(minutes=5)
    scheduler = _scheduler(app_db, dispatcher_db)
    assert await scheduler.tick(NOW) == 0  # ainda não venceu
    assert await scheduler.tick(due) == 1
    assert await scheduler.tick(due) == 0  # a mesma ocorrência não vira outra execução
    assert await scheduler.tick(due + timedelta(minutes=1)) == 0
    [run] = await runs_of(superuser_engine, "schedule", world.a.company_id)
    assert run["priority"] == PRIORITY_BY_TRIGGER[RunTrigger.SCHEDULE] == 2
    assert run["inputs"] == PROTOCOLO_INPUTS
    assert run["idempotency_key"] == f"{info.schedule_id}:{due.isoformat()}"
    assert run["requested_by"] == f"agenda:{info.schedule_id}"
    [occurrence] = await world.schedules.list_occurrences(world.a.viewer, info.schedule_id)
    assert occurrence.status is ScheduleOccurrenceStatus.ENQUEUED
    assert occurrence.scheduled_for == due and occurrence.run_id == run["id"]
    after = await world.schedules.get_schedule(world.a.operator, info.schedule_id)
    assert after is not None
    assert after.next_run_at == due + timedelta(minutes=5)
    assert after.last_occurrence_at == due
    # A execução aparece para quem vê a área, com a origem certa.
    status = await world.platform.get(world.a.viewer, run["id"])
    assert status is not None and status.trigger is RunTrigger.SCHEDULE


async def test_two_dispatchers_never_create_two_runs_for_an_occurrence(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    ids = []
    for minute in range(6):
        info = await world.schedules.create_schedule(
            world.a.operator, _spec(world, cron=f"{minute} * * * *", title=f"Agenda {minute}")
        )
        ids.append(info.schedule_id)
    first, second = _scheduler(app_db, dispatcher_db), _scheduler(app_db, dispatcher_db)
    moment = NOW + timedelta(hours=1, minutes=10)
    total = 0
    for _ in range(3):
        counts = await asyncio.gather(
            first.tick(moment), second.tick(moment), first.tick(moment), second.tick(moment)
        )
        total += sum(counts)
    runs = await runs_of(superuser_engine, "schedule", world.a.company_id)
    assert total == len(runs) == 6
    keys = [run["idempotency_key"] for run in runs]
    assert len(set(keys)) == 6
    async with superuser_engine.connect() as conn:
        occurrences = (
            await conn.execute(
                text(
                    "SELECT schedule_id, status, reason, count(*) FROM schedule_occurrences "
                    "WHERE company_id = :c GROUP BY schedule_id, status, reason"
                ),
                {"c": world.a.company_id},
            )
        ).all()
    # Cada agenda roda só a ocorrência das 13hMM; a das 12hMM (de 1 a 5) ficou como perdida.
    expected = [(sid, "enqueued", None, 1) for sid in ids]
    expected += [(sid, "skipped", "perdida", 1) for sid in ids[1:]]
    assert sorted(map(tuple, occurrences), key=str) == sorted(expected, key=str)


async def test_missed_occurrences_beyond_the_window_are_skipped_and_recorded(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(
        world.a.operator, _spec(world, cron="*/10 * * * *")
    )
    # O serviço ficou fora do ar por uma hora: 13h10 a 14h00 venceram.
    scheduler = _scheduler(app_db, dispatcher_db, catch_up=timedelta(minutes=15))
    later = NOW + timedelta(hours=2, minutes=3)  # 14h03
    assert await scheduler.tick(later) == 1
    occurrences = await world.schedules.list_occurrences(world.a.viewer, info.schedule_id, 100)
    by_time = {o.scheduled_for: (o.status, o.reason) for o in occurrences}
    # Só a mais nova dentro da tolerância roda, a outra dentro dela fica como substituída,
    # e as que passaram da tolerância (12h10 a 13h40) são um registro só, na primeira.
    assert by_time == {
        NOW + timedelta(minutes=10): (ScheduleOccurrenceStatus.SKIPPED, "perdida"),
        NOW + timedelta(hours=1, minutes=50): (ScheduleOccurrenceStatus.SKIPPED, "substituida"),
        NOW + timedelta(hours=2): (ScheduleOccurrenceStatus.ENQUEUED, None),
    }
    # A auditoria diz a primeira e a última perdidas.
    assert await _ignored_digests(superuser_engine, info.schedule_id) == [
        payload_digest(
            {
                "motivos": {"perdida": 1, "substituida": 1},
                "primeira": (NOW + timedelta(minutes=10)).isoformat(),
                "ultima": (NOW + timedelta(hours=1, minutes=50)).isoformat(),
                "perdidas": {
                    "primeira": (NOW + timedelta(minutes=10)).isoformat(),
                    "ultima": (NOW + timedelta(hours=1, minutes=40)).isoformat(),
                },
            }
        )
    ]
    after = await world.schedules.get_schedule(world.a.operator, info.schedule_id)
    assert after is not None and after.next_run_at == NOW + timedelta(hours=2, minutes=10)
    # Fora da tolerância, nada roda: só fica registrado.
    zero = _scheduler(app_db, dispatcher_db, catch_up=timedelta(0))
    assert await zero.tick(NOW + timedelta(hours=5, minutes=1)) == 0
    reasons = {
        o.reason
        for o in await world.schedules.list_occurrences(world.a.viewer, info.schedule_id, 200)
    }
    assert reasons == {None, "perdida", "substituida"}
    async with app_db.tenant_session(world.a.company_id) as session:
        chain = await verify_chain(session, world.a.company_id)
        actions = set((await session.scalars(text("SELECT action FROM audit_log"))).all())
    assert chain.ok
    assert {"agenda.criada", "agenda.ocorrencias_ignoradas", "execucao.iniciada"} <= actions


async def _ignored_digests(superuser: AsyncEngine, schedule_id: Any) -> list[str]:
    async with superuser.connect() as conn:
        rows = await conn.scalars(
            text(
                "SELECT payload_digest FROM audit_log WHERE action = "
                "'agenda.ocorrencias_ignoradas' AND target = :t ORDER BY seq"
            ),
            {"t": f"agenda:{schedule_id}"},
        )
        return list(rows.all())


async def test_long_outage_is_one_missed_entry_and_counts_again_from_the_window(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Dois dias fora do ar com uma agenda de minuto em minuto: um registro, não 2880."""
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(world.a.operator, _spec(world, cron="* * * * *"))
    first = NOW + timedelta(minutes=1)
    assert info.next_run_at == first
    scheduler = _scheduler(app_db, dispatcher_db, catch_up=timedelta(minutes=10))
    later = NOW + timedelta(days=2, seconds=30)
    assert await scheduler.tick(later) == 1
    occurrences = await world.schedules.list_occurrences(world.a.viewer, info.schedule_id, 200)
    by_time = {o.scheduled_for: (o.status, o.reason) for o in occurrences}
    back_at = NOW + timedelta(days=2)
    # A última perdida é a de 11h50 (agora menos a tolerância); de 11h51 a 12h00 estão na
    # tolerância: só a mais nova roda e as outras nove ficam como substituídas.
    assert by_time.pop(first) == (ScheduleOccurrenceStatus.SKIPPED, "perdida")
    assert by_time.pop(back_at) == (ScheduleOccurrenceStatus.ENQUEUED, None)
    assert by_time == {
        back_at - timedelta(minutes=n): (ScheduleOccurrenceStatus.SKIPPED, "substituida")
        for n in range(1, 10)
    }
    assert await _ignored_digests(superuser_engine, info.schedule_id) == [
        payload_digest(
            {
                "motivos": {"perdida": 1, "substituida": 9},
                "primeira": first.isoformat(),
                "ultima": (back_at - timedelta(minutes=1)).isoformat(),
                "perdidas": {
                    "primeira": first.isoformat(),
                    "ultima": (back_at - timedelta(minutes=10)).isoformat(),
                },
            }
        )
    ]
    after = await world.schedules.get_schedule(world.a.operator, info.schedule_id)
    assert after is not None
    assert after.next_run_at == back_at + timedelta(minutes=1)
    assert after.last_occurrence_at == back_at
    # Fora do ar de novo, agora sem nada dentro da tolerância: só o registro da perdida.
    zero = _scheduler(app_db, dispatcher_db, catch_up=timedelta(0))
    assert await zero.tick(back_at + timedelta(days=1, seconds=10)) == 0
    after = await world.schedules.get_schedule(world.a.operator, info.schedule_id)
    assert after is not None
    assert after.next_run_at == back_at + timedelta(days=1, minutes=1)
    async with superuser_engine.connect() as conn:
        total = await conn.scalar(
            text("SELECT count(*) FROM schedule_occurrences WHERE schedule_id = :s"),
            {"s": info.schedule_id},
        )
    assert total == 12


async def test_enabled_schedules_per_company_are_capped(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    capped = PgSchedules(app_db, max_enabled_per_company=2, clock=world.clock)
    a = world.a
    first = await capped.create_schedule(a.operator, _spec(world, title="1"))
    await capped.create_schedule(a.operator, _spec(world, title="2"))
    with pytest.raises(Conflict) as caught:
        await capped.create_schedule(a.operator, _spec(world, title="3"))
    assert "2 agendas ligadas" in caught.value.message
    # Desligada não conta: pode ser criada, mas não ligada enquanto estiver no limite.
    off = await capped.create_schedule(a.operator, _spec(world, title="4", enabled=False))
    with pytest.raises(Conflict):
        await capped.update_schedule(a.operator, off.schedule_id, ScheduleChanges(enabled=True))
    # Mudar uma que já está ligada não esbarra no limite.
    await capped.update_schedule(a.operator, first.schedule_id, ScheduleChanges(title="Um"))
    # Desligar uma abre espaço.
    await capped.update_schedule(a.operator, first.schedule_id, ScheduleChanges(enabled=False))
    on = await capped.update_schedule(a.operator, off.schedule_id, ScheduleChanges(enabled=True))
    assert on.enabled and on.next_run_at is not None
    # Remover também.
    await capped.delete_schedule(a.operator, on.schedule_id)
    await capped.update_schedule(a.operator, first.schedule_id, ScheduleChanges(enabled=True))
    # Cada empresa tem o seu limite, contado sob trava: pedidos juntos não passam dele.
    spec_b = ScheduleSpec(
        robot_id=world.robots_b["emitir_protocolo"],
        title="B",
        cron="0 9 * * *",
        inputs=PROTOCOLO_INPUTS,
    )
    results = await asyncio.gather(
        *(capped.create_schedule(world.b.operator, spec_b) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert all(isinstance(r, Conflict) for r in results if isinstance(r, Exception))
    with pytest.raises(ValueError):
        PgSchedules(app_db, max_enabled_per_company=0)


async def test_tick_stops_at_its_time_budget_and_says_it_is_behind(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    for index in range(5):
        await world.schedules.create_schedule(
            world.a.operator, _spec(world, cron="* * * * *", title=f"A{index}")
        )
    seconds = iter(range(1000))  # cada consulta ao relógio anda um segundo
    scheduler = _scheduler(
        app_db, dispatcher_db, max_tick_s=2.5, monotonic=lambda: float(next(seconds))
    )
    moment = NOW + timedelta(minutes=1)
    # Começa em 0 e confere antes de cada agenda depois da primeira: 1 e 2 cabem, 3 não.
    assert await scheduler.tick(moment) == 3 and scheduler.behind
    assert await scheduler.tick(moment) == 2 and not scheduler.behind
    assert await scheduler.tick(moment) == 0 and not scheduler.behind
    # Uma volta que enche o lote também fica para trás (pode haver mais vencidas).
    # (As execuções anteriores ainda estão na fila: as ocorrências só ficam registradas.)
    small = _scheduler(app_db, dispatcher_db, batch_size=2)
    later = NOW + timedelta(minutes=2)
    behind = []
    for _ in range(3):
        await small.tick(later)
        behind.append(small.behind)
    assert behind == [True, True, False]
    with pytest.raises(ValueError):
        _scheduler(app_db, dispatcher_db, max_tick_s=0)


async def test_dispatcher_keeps_claiming_while_the_scheduler_catches_up() -> None:
    """Agendador atrasado: as voltas seguem sem esperar, e o despacho roda entre elas."""
    events: list[str] = []
    done = asyncio.Event()

    class Catching:
        behind = False
        ticks = 0

        async def tick(self, now: datetime | None = None) -> int:
            self.ticks += 1
            events.append("tick")
            self.behind = self.ticks < 4
            if not self.behind:
                done.set()
            return 0

    class Queue:
        async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
            return 0

        async def reap_expired(self, **_: Any) -> Any:
            return ReapReport()

        async def claim_next(self, now: datetime | None = None) -> None:
            events.append("claim")

    dispatcher = Dispatcher(
        Queue(),  # type: ignore[arg-type]
        lambda job: None,  # type: ignore[arg-type,return-value]
        scheduler=Catching(),
        # Intervalos longos: sem a volta imediata, o teste esperaria uma hora.
        schedule_interval=3600,
        poll_interval=3600,
        clock=lambda: NOW,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(10):
            await done.wait()
    finally:
        stop.set()
        await loop
    assert events == ["tick", "claim"] * 4
    assert dispatcher.failing == {}


async def test_occurrence_is_skipped_while_the_previous_run_is_unfinished_or_queue_full(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(world.a.operator, _spec(world, cron="* * * * *"))
    scheduler = _scheduler(app_db, dispatcher_db)
    first = NOW + timedelta(minutes=1)
    assert await scheduler.tick(first) == 1
    # A execução anterior ainda está na fila: a agenda não se sobrepõe a ela mesma.
    assert await scheduler.tick(first + timedelta(minutes=1)) == 0
    # Termina a anterior; a fila da empresa fica cheia por outro motivo.
    queue = PgRunQueue(dispatcher_db)
    job = await queue.claim_next()
    assert job is not None
    assert await queue.finish(job.run_id, job.attempt, result(job.run_id, job.attempt))
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE company_quotas SET max_queued = 0 WHERE company_id = :c"),
            {"c": world.a.company_id},
        )
    assert await scheduler.tick(first + timedelta(minutes=2)) == 0
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE company_quotas SET max_queued = 1000 WHERE company_id = :c"),
            {"c": world.a.company_id},
        )
        # O robô deixa de estar publicado.
        await conn.execute(
            text("UPDATE robots SET status = 'disabled' WHERE id = :r"),
            {"r": world.robots_a["emitir_protocolo"]},
        )
    assert await scheduler.tick(first + timedelta(minutes=3)) == 0
    reasons = [
        (o.scheduled_for - NOW, o.reason)
        for o in reversed(await world.schedules.list_occurrences(world.a.viewer, info.schedule_id))
    ]
    assert reasons == [
        (timedelta(minutes=1), None),
        (timedelta(minutes=2), "anterior_em_andamento"),
        (timedelta(minutes=3), "fila_cheia"),
        (timedelta(minutes=4), "robo_indisponivel"),
    ]
    assert len(await runs_of(superuser_engine, "schedule", world.a.company_id)) == 1


async def test_new_version_that_refuses_the_inputs_skips_the_occurrence(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(world.a.operator, _spec(world, cron="* * * * *"))
    # Versão 2 com uma entrada obrigatória a mais: a agenda roda a versão publicada.
    from tests.control.db.support import protocolo, publish

    manifest = protocolo()
    extra = manifest.model_dump(mode="json")
    extra["inputs"].append({"name": "email", "title": "E-mail", "type": "string"})
    from rapa.core.robot import Manifest

    await publish(world.platform, world.a, Manifest.model_validate(extra))
    scheduler = _scheduler(app_db, dispatcher_db)
    assert await scheduler.tick(NOW + timedelta(minutes=1)) == 0
    [occurrence] = await world.schedules.list_occurrences(world.a.viewer, info.schedule_id)
    assert occurrence.reason == "entradas_invalidas"


async def test_disable_enable_and_delete(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    schedules, operator = world.schedules, world.a.operator
    info = await schedules.create_schedule(operator, _spec(world, cron="0 * * * *"))
    off = await schedules.update_schedule(
        operator, info.schedule_id, ScheduleChanges(enabled=False)
    )
    assert not off.enabled and off.next_run_at is None
    scheduler = _scheduler(app_db, dispatcher_db)
    assert await scheduler.tick(NOW + timedelta(hours=3)) == 0
    # Ligada de novo três horas depois: conta a partir de agora, sem rodar o que passou.
    world.clock.now = NOW + timedelta(hours=3, minutes=20)
    on = await schedules.update_schedule(
        operator, info.schedule_id, ScheduleChanges(enabled=True, title="Outra", cron="15 * * * *")
    )
    assert on.enabled and on.title == "Outra" and on.cron == "15 * * * *"
    assert on.next_run_at == NOW + timedelta(hours=4, minutes=15)
    assert await schedules.list_occurrences(operator, info.schedule_id) == []
    with pytest.raises(InvalidInput):
        await schedules.update_schedule(operator, info.schedule_id, ScheduleChanges(cron="x"))
    await schedules.delete_schedule(operator, info.schedule_id)
    assert await schedules.get_schedule(operator, info.schedule_id) is None
    assert await schedules.list_schedules(operator) == []
    with pytest.raises(NotFound):
        await schedules.delete_schedule(operator, info.schedule_id)
    assert await scheduler.tick(NOW + timedelta(days=2)) == 0
    # Uma agenda removida não muda mais, nem pelo banco.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with app_db.tenant_session(world.a.company_id) as session:
            await session.execute(
                text("UPDATE schedules SET archived_at = NULL WHERE id = :id"),
                {"id": info.schedule_id},
            )


async def test_dispatcher_runs_the_scheduler_and_dispatches_in_the_same_loop(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    info = await world.schedules.create_schedule(world.a.operator, _spec(world, cron="* * * * *"))
    queue = PgRunQueue(dispatcher_db)
    launched: list[Any] = []
    arrived = asyncio.Event()

    async def launch(job: Any) -> dict[str, Any]:
        launched.append(job)
        arrived.set()
        return result(job.run_id, job.attempt, outputs={"protocolo": "PRT-1"})

    scheduler = _scheduler(app_db, dispatcher_db, clock=lambda: NOW + timedelta(minutes=1))
    dispatcher = Dispatcher(
        queue,
        launch,
        scheduler=scheduler,
        schedule_interval=0,
        poll_interval=0.01,
        partitions_interval=3600,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(20):
            await arrived.wait()
    finally:
        stop.set()
        await loop
    assert launched[0].inputs == PROTOCOLO_INPUTS
    [occurrence] = await world.schedules.list_occurrences(world.a.viewer, info.schedule_id)
    assert occurrence.run_id == launched[0].run_id
    assert dispatcher.failing == {}


async def test_two_dispatcher_loops_dispatch_each_occurrence_once(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine, total_slots=40)
    for minute in range(5):
        await world.schedules.create_schedule(
            world.a.operator, _spec(world, cron=f"{minute} * * * *", title=f"Agenda {minute}")
        )
    moment = NOW + timedelta(hours=1, minutes=8)
    launched: list[Any] = []
    all_launched = asyncio.Event()

    async def launch(job: Any) -> dict[str, Any]:
        launched.append(job.run_id)
        if len(launched) >= 5:
            all_launched.set()
        await asyncio.sleep(0.01)
        return result(job.run_id, job.attempt, outputs={"protocolo": "PRT-1"})

    def loop() -> Dispatcher:
        return Dispatcher(
            PgRunQueue(dispatcher_db),
            launch,
            scheduler=_scheduler(app_db, dispatcher_db, clock=lambda: moment),
            schedule_interval=0,
            poll_interval=0.01,
            partitions_interval=3600,
            concurrency=4,
            clock=lambda: moment,
        )

    stop = asyncio.Event()
    tasks = [asyncio.create_task(loop().run(stop)) for _ in range(2)]
    try:
        async with asyncio.timeout(30):
            await all_launched.wait()
        await asyncio.sleep(0.5)  # tempo para um disparo duplicado aparecer, se houvesse
    finally:
        stop.set()
        await asyncio.gather(*tasks)
    runs = await runs_of(superuser_engine, "schedule", world.a.company_id)
    assert len(runs) == 5 and len({r["idempotency_key"] for r in runs}) == 5
    assert sorted(launched) == sorted(r["id"] for r in runs)  # cada uma despachada uma vez


async def test_failing_scheduler_shows_in_dispatcher_health() -> None:
    class Broken:
        calls = 0
        behind = False

        async def tick(self, now: datetime | None = None) -> int:
            Broken.calls += 1
            raise RuntimeError("dados sigilosos da agenda")

    class Queue:
        async def ensure_event_partitions(self, months_ahead: int = 2) -> int:
            return 0

        async def reap_expired(self, **_: Any) -> Any:
            from rapa.control.services import ReapReport

            return ReapReport()

    now = [NOW]
    dispatcher = Dispatcher(
        Queue(),  # type: ignore[arg-type]
        lambda job: None,  # type: ignore[arg-type,return-value]
        scheduler=Broken(),
        schedule_interval=1,
        unhealthy_after_s=10,
        clock=lambda: now[0],
    )
    await dispatcher.maintenance()
    assert set(dispatcher.failing) == {"schedules"} and dispatcher.healthy
    now[0] = NOW + timedelta(seconds=30)
    await dispatcher.maintenance()
    assert not dispatcher.healthy and Broken.calls == 2


async def test_many_due_schedules_of_one_company_do_not_starve_another(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Uma empresa com mais agendas vencidas do que cabem numa volta não atrasa as das outras.

    Com ``batch_size=2`` (no lugar dos 100 de produção, com milhares de agendas), a
    empresa A tem 6 agendas de minuto em minuto, vencidas antes da agenda diária da B.
    """
    world = await _world(app_db, superuser_engine)
    for index in range(6):
        await world.schedules.create_schedule(
            world.a.operator, _spec(world, cron="* * * * *", title=f"A{index}")
        )
    daily = await world.schedules.create_schedule(
        world.b.operator,
        ScheduleSpec(
            robot_id=world.robots_b["emitir_protocolo"],
            title="Diária da B",
            cron="5 9 * * *",  # 12h05 UTC
            inputs=PROTOCOLO_INPUTS,
        ),
    )
    scheduler = _scheduler(app_db, dispatcher_db, batch_size=2, catch_up=timedelta(minutes=2))
    for minute in range(4):
        await scheduler.tick(NOW + timedelta(minutes=5 + minute, seconds=30))
    occurrences = await world.schedules.list_occurrences(world.b.viewer, daily.schedule_id)
    assert [(o.status, o.reason) for o in occurrences] == [
        (ScheduleOccurrenceStatus.ENQUEUED, None)
    ]
