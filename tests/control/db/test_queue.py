"""Fila de execuções: admissão justa, reserva interativa, cerca e recolhedor."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher, _EventTail
from rapa.control.services import (
    EventConflict,
    PgPlatform,
    PgRunQueue,
    StaleAttempt,
    verify_chain,
)
from rapa.core.contracts import InvalidInput, RunMode, RunnerJob, RunState, RunTrigger
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Effect
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import Interpreter
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    ListEventSink,
    LocalFileProvider,
)

from .support import (
    PROTOCOLO_INPUTS,
    Tenant,
    event,
    make_tenant,
    notas,
    protocolo,
    publish,
    reset_queue,
    result,
)

pytestmark = pytest.mark.database


async def _tenant_with_robots(
    platform: PgPlatform, app_db: Database, name: str, **quota: object
) -> Tenant:
    tenant = await make_tenant(app_db, name, **quota)  # type: ignore[arg-type]
    await publish(platform, tenant, protocolo())
    await publish(platform, tenant, notas())
    return tenant


async def _enqueue(
    platform: PgPlatform, tenant: Tenant, count: int, trigger: RunTrigger = RunTrigger.REST
) -> list[UUID]:
    out = []
    for _ in range(count):
        status = await platform.start(tenant.operator, "baixar_notas", {}, trigger)
        out.append(status.run_id)
    return out


async def _validate(platform: PgPlatform, tenant: Tenant, slug: str = "baixar_notas") -> UUID:
    """Enfileira a validação da versão publicada: uma execução interativa."""
    robot = await platform.get_published(tenant.creator, slug)
    assert robot is not None
    status = await platform.start_validation(tenant.creator, robot.robot_id, robot.version_no, {})
    return status.run_id


async def _claim_all(queue: PgRunQueue) -> list[RunnerJob]:
    jobs = []
    while (job := await queue.claim_next()) is not None:
        jobs.append(job)
        assert len(jobs) < 100, "a fila não parou de entregar execuções"
    return jobs


async def test_fair_admission_between_two_companies(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    # 10 vagas, 1 reservada para interativo: 9 para as duas empresas (4 garantidas cada).
    await reset_queue(superuser_engine, total_slots=10, reserve_ratio=0.10)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    a = await _tenant_with_robots(platform, app_db, "Justa A", guaranteed_slots=4)
    b = await _tenant_with_robots(platform, app_db, "Justa B", guaranteed_slots=4)
    await _enqueue(platform, a, 12)  # A chega primeiro e com muito mais
    await _enqueue(platform, b, 12)

    jobs = await _claim_all(queue)
    per_company = Counter(job.company_id for job in jobs)
    # A pega a garantia e a única vaga ociosa; B pega a garantia inteira, mesmo chegando depois.
    assert per_company == {a.company_id: 5, b.company_id: 4}
    assert all(job.attempt == 1 for job in jobs)
    assert all(job.mode is RunMode.FULL for job in jobs)
    assert [job.company_id for job in jobs][:4] == [a.company_id] * 4

    # A execução pela interface conta na cota da empresa: não há vaga para ela.
    await platform.start(a.operator, "baixar_notas", {}, RunTrigger.MANUAL)
    assert await queue.claim_next() is None
    # A vaga reservada continua livre para uma execução interativa (validação).
    validation = await _validate(platform, a)
    job = await queue.claim_next()
    assert job is not None and job.run_id == validation
    assert await queue.claim_next() is None

    # A vaga liberada pela A volta para ela, que chegou primeiro (as duas pedem emprestado).
    finished = next(j for j in jobs if j.company_id == a.company_id)
    await queue.finish(finished.run_id, finished.attempt, result(finished.run_id, 1))
    job = await queue.claim_next()
    assert job is not None and job.company_id == a.company_id


async def test_guarantee_beats_borrowing(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10, reserve_ratio=0.10)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    a = await _tenant_with_robots(platform, app_db, "Empresta A", guaranteed_slots=4)
    b = await _tenant_with_robots(platform, app_db, "Empresta B", guaranteed_slots=4)
    await _enqueue(platform, a, 20)

    jobs = await _claim_all(queue)
    # Sozinha, A empresta até o dobro da garantia, nunca além.
    assert len(jobs) == 8

    await _enqueue(platform, b, 5)
    first_b = await _claim_all(queue)
    assert [j.company_id for j in first_b] == [b.company_id]  # só sobrou 1 vaga

    # Duas vagas liberadas pela A vão para a B, que ainda está abaixo da garantia.
    for job in jobs[:2]:
        await queue.finish(job.run_id, job.attempt, result(job.run_id, 1))
    after = await _claim_all(queue)
    assert [j.company_id for j in after] == [b.company_id, b.company_id]


async def test_company_without_guarantee_only_runs_interactive(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, "Sem cota", guaranteed_slots=0)
    await _enqueue(platform, tenant, 2)
    assert await queue.claim_next() is None
    # O botão "Executar" da interface roda em produção: conta na cota como a API.
    await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.MANUAL)
    assert await queue.claim_next() is None
    validation = await _validate(platform, tenant)
    job = await queue.claim_next()
    assert job is not None and job.run_id == validation


async def test_interactive_reserve(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10, reserve_ratio=0.10)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(
        platform, app_db, "Reserva", guaranteed_slots=20, max_borrow_factor=1
    )
    await _enqueue(platform, tenant, 15)
    batch = await _claim_all(queue)
    assert len(batch) == 9  # 10 vagas menos a reserva

    first = await _validate(platform, tenant)
    job = await queue.claim_next()
    assert job is not None and job.run_id == first
    second = await _validate(platform, tenant)
    assert await queue.claim_next() is None  # as 10 vagas estão ocupadas

    # Uma vaga liberada vai primeiro para o interativo que espera (a garantia que
    # ele ocupa é da própria empresa).
    await queue.finish(batch[0].run_id, 1, result(batch[0].run_id, 1))
    job = await queue.claim_next()
    assert job is not None and job.run_id == second
    # Com 2 interativos rodando (1 além da reserva), o lote fica com 8 vagas: hoje usa 7.
    await queue.finish(batch[1].run_id, 1, result(batch[1].run_id, 1))
    job = await queue.claim_next()
    assert job is not None and job.run_id not in {first, second}
    assert await queue.claim_next() is None


async def test_ui_runs_cannot_starve_another_company(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    # 4 vagas, 1 reservada para interativo: 3 para as empresas.
    await reset_queue(superuser_engine, total_slots=4, reserve_ratio=0.25)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    a = await _tenant_with_robots(platform, app_db, "Fome A", guaranteed_slots=1)
    b = await _tenant_with_robots(platform, app_db, "Fome B", guaranteed_slots=2)
    # B pede 2 pela API (a garantia dela); A clica em "Executar" 10 vezes na interface.
    b_runs = await _enqueue(platform, b, 2)
    await _enqueue(platform, a, 10, RunTrigger.MANUAL)

    jobs = await _claim_all(queue)
    per_company = Counter(job.company_id for job in jobs)
    assert per_company == {a.company_id: 1, b.company_id: 2}
    assert {j.run_id for j in jobs if j.company_id == b.company_id} == set(b_runs)
    # A reserva segue livre para gravação, controle ao vivo e testes.
    validation = await _validate(platform, a)
    job = await queue.claim_next()
    assert job is not None and job.run_id == validation


async def test_interactive_overflow_never_takes_another_company_guarantee(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=4, reserve_ratio=0.25)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    a = await _tenant_with_robots(platform, app_db, "Valida A", guaranteed_slots=1)
    b = await _tenant_with_robots(platform, app_db, "Valida B", guaranteed_slots=2)
    # Sem ninguém na fila, as validações da A usam a reserva e depois vagas ociosas.
    validations = [await _validate(platform, a), await _validate(platform, a)]
    assert [j.run_id for j in await _claim_all(queue)] == validations

    # Com a B esperando a garantia, a validação seguinte da A não toma a vaga dela.
    b_runs = await _enqueue(platform, b, 2)
    waiting = await _validate(platform, a)
    jobs = await _claim_all(queue)
    assert [j.run_id for j in jobs] == b_runs
    status = await platform.get(a.creator, waiting)
    assert status is not None and status.state is RunState.QUEUED


async def test_priority_order(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, "Prioridade", guaranteed_slots=10)
    order = [
        RunTrigger.BATCH,
        RunTrigger.SCHEDULE,
        RunTrigger.WEBHOOK,
        RunTrigger.MCP,
        RunTrigger.REST,
        RunTrigger.MANUAL,
    ]
    ids = {trigger: (await _enqueue(platform, tenant, 1, trigger))[0] for trigger in order}
    ids[RunTrigger.VALIDATION] = await _validate(platform, tenant)
    jobs = await _claim_all(queue)
    by_id = {v: k for k, v in ids.items()}
    # Interativo (validação) primeiro; a interface vem junto com MCP e API, por ordem de chegada.
    assert [by_id[j.run_id] for j in jobs] == [
        RunTrigger.VALIDATION,
        RunTrigger.MCP,
        RunTrigger.REST,
        RunTrigger.MANUAL,
        RunTrigger.SCHEDULE,
        RunTrigger.WEBHOOK,
        RunTrigger.BATCH,
    ]


async def test_claim_returns_pinned_job(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, max_duration=timedelta(minutes=5))
    tenant = await _tenant_with_robots(platform, app_db, "Job")
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    # Uma versão nova publicada depois não muda a execução já enfileirada.
    await publish(platform, tenant, protocolo().model_copy(update={"title": "Emitir protocolo v2"}))
    now = datetime(2030, 1, 1, tzinfo=UTC)
    job = await queue.claim_next(now=now)
    assert job is not None
    assert job.run_id == run.run_id
    assert job.company_id == tenant.company_id
    assert job.manifest == protocolo()
    assert job.inputs == PROTOCOLO_INPUTS
    assert job.deadline == now + timedelta(minutes=5)
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.RUNNING
    assert status.started_at == now and status.queue_position is None


async def test_fencing_rejects_stale_attempts(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=30))
    tenant = await _tenant_with_robots(platform, app_db, "Cerca")
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    now = datetime.now(UTC)
    first = await queue.claim_next(now=now)
    assert first is not None and first.attempt == 1
    assert (
        await queue.ingest_events(
            run.run_id, 1, [event(run.run_id, 1, 0, RunEventKind.RUN_STARTED)]
        )
        == 1
    )
    assert await queue.heartbeat(run.run_id, 1, now=now)

    # O executor some; o recolhedor devolve a execução para a fila.
    report = await queue.reap_expired(now=now + timedelta(seconds=31))
    assert report.requeued == [run.run_id]
    with pytest.raises(StaleAttempt):
        await queue.ingest_events(
            run.run_id, 1, [event(run.run_id, 1, 1, RunEventKind.STEP_STARTED)]
        )
    with pytest.raises(StaleAttempt):
        await queue.finish(run.run_id, 1, result(run.run_id, 1))
    assert not await queue.heartbeat(run.run_id, 1, now=now)

    second = await queue.claim_next(now=now + timedelta(seconds=32))
    assert second is not None and second.run_id == run.run_id and second.attempt == 2
    with pytest.raises(StaleAttempt):
        await queue.ingest_events(
            run.run_id, 1, [event(run.run_id, 1, 1, RunEventKind.STEP_STARTED)]
        )
    with pytest.raises(StaleAttempt):
        await queue.finish(run.run_id, 1, result(run.run_id, 1, "failed"))
    assert not await queue.heartbeat(run.run_id, 1, now=now)
    assert await queue.heartbeat(run.run_id, 2, now=now)
    assert (
        await queue.ingest_events(
            run.run_id, 2, [event(run.run_id, 2, 1, RunEventKind.RUN_STARTED)]
        )
        == 1
    )
    # Evento de outra tentativa no lote da tentativa atual é recusado.
    with pytest.raises(InvalidInput):
        await queue.ingest_events(
            run.run_id, 2, [event(run.run_id, 1, 2, RunEventKind.STEP_STARTED)]
        )
    assert await queue.finish(
        run.run_id, 2, result(run.run_id, 2, outputs={"notas": []}, steps_done=9)
    )
    assert not await queue.finish(run.run_id, 2, result(run.run_id, 2))  # já concluída
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None
    assert status.state is RunState.SUCCEEDED
    assert status.outputs == {"notas": []}
    assert status.steps_done == 9


async def test_event_ingestion_is_idempotent(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, "Eventos")
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    job = await queue.claim_next()
    assert job is not None
    rid = run.run_id
    batch = [
        event(rid, 1, 0, RunEventKind.RUN_STARTED),
        event(rid, 1, 1, RunEventKind.STEP_STARTED, "abrir_login"),
        event(rid, 1, 2, RunEventKind.STEP_SUCCEEDED, "abrir_login"),
    ]
    assert await queue.ingest_events(rid, 1, batch) == 3
    assert await queue.ingest_events(rid, 1, batch) == 0
    # O mesmo seq com conteúdo diferente é recusado, e nada do lote é gravado: um
    # reenvio só é ignorado quando é o mesmo evento (o lançador confirma o aviso
    # irreversível só depois de a gravação dar certo).
    changed = event(rid, 1, 2, RunEventKind.STEP_FAILED, "abrir_login")
    novel = event(rid, 1, 3, RunEventKind.STEP_STARTED, "digitar_usuario")
    with pytest.raises(EventConflict):
        await queue.ingest_events(rid, 1, [e.model_dump(mode="json") for e in (changed, novel)])
    with pytest.raises(EventConflict):
        await queue.ingest_events(rid, 1, [novel, novel.model_copy(update={"step_id": "outro"})])
    # Reenvio parcial com um evento novo.
    more = [batch[2], novel]
    assert await queue.ingest_events(rid, 1, [e.model_dump(mode="json") for e in more]) == 1
    assert await queue.ingest_events(rid, 1, []) == 0
    async with app_db.tenant_session(tenant.company_id) as session:
        rows = (
            await session.execute(
                text("SELECT seq, kind FROM run_events WHERE run_id = :id ORDER BY seq"),
                {"id": rid},
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (0, "run_started"),
        (1, "step_started"),
        (2, "step_succeeded"),
        (3, "step_started"),
    ]
    status = await platform.get(tenant.operator, rid)
    assert status is not None and status.steps_done == 1

    # O banco também recusa (run_id, seq) repetido entre partições.
    with pytest.raises(DBAPIError) as caught:
        async with dispatcher_db.dispatcher_session() as session:
            await session.execute(
                text(
                    "INSERT INTO run_events (company_id, run_id, attempt, seq, at, kind) "
                    "VALUES (:c, :r, 1, 0, now() + interval '1 month', 'run_started')"
                ),
                {"c": tenant.company_id, "r": rid},
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "23505"
    with pytest.raises(InvalidInput):
        await queue.ingest_events(rid, 1, [{"run_id": str(rid), "attempt": 1, "seq": -1}])


async def test_human_wait_state_follows_events(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, "Espera")
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    assert await queue.claim_next() is not None
    rid = run.run_id
    await queue.ingest_events(rid, 1, [event(rid, 1, 0, RunEventKind.HUMAN_NEEDED, "entrar")])
    status = await platform.get(tenant.operator, rid)
    assert status is not None and status.state is RunState.AWAITING_HUMAN
    await queue.ingest_events(rid, 1, [event(rid, 1, 1, RunEventKind.STEP_SUCCEEDED, "entrar")])
    status = await platform.get(tenant.operator, rid)
    assert status is not None and status.state is RunState.RUNNING


async def test_reaper_before_and_after_irreversible_step(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant_with_robots(platform, app_db, "Recolhedor")
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    rid = run.run_id
    now = datetime.now(UTC)

    # Antes do passo irreversível: volta para a fila, sem perder o lugar.
    assert await queue.claim_next(now=now) is not None
    await queue.ingest_events(
        rid,
        1,
        [
            event(rid, 1, 0, RunEventKind.RUN_STARTED),
            event(rid, 1, 1, RunEventKind.STEP_STARTED, "abrir_protocolo", efeito="read"),
            event(rid, 1, 2, RunEventKind.STEP_SUCCEEDED, "abrir_protocolo"),
        ],
    )
    assert (await queue.reap_expired(now=now + timedelta(seconds=5))).requeued == []
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.requeued == [rid] and report.indeterminate == []
    status = await platform.get(tenant.operator, rid)
    assert status is not None and status.state is RunState.QUEUED and status.started_at is None

    # Depois do passo irreversível: indeterminado, nunca de volta à fila.
    later = now + timedelta(seconds=20)
    job = await queue.claim_next(now=later)
    assert job is not None and job.attempt == 2
    # A tentativa 2 numera os eventos depois dos da tentativa 1.
    assert job.first_seq == 3
    await queue.ingest_events(
        rid,
        2,
        [
            event(rid, 2, 3, RunEventKind.RUN_STARTED),
            event(
                rid,
                2,
                4,
                RunEventKind.STEP_STARTED,
                "emitir",
                efeito="irreversible",
                irreversivel=True,
            ),
        ],
    )
    report = await queue.reap_expired(now=later + timedelta(seconds=11))
    assert report.indeterminate == [rid] and report.requeued == []
    status = await platform.get(tenant.operator, rid)
    assert status is not None
    assert status.state is RunState.INDETERMINATE
    assert status.error_code == "executor_sem_resposta"
    assert status.finished_at is not None
    # O resultado atrasado da tentativa não muda o indeterminado.
    assert not await queue.finish(rid, 2, result(rid, 2, outputs={"protocolo": "PRT-1"}))
    async with dispatcher_db.dispatcher_session() as session:
        actions = (
            await session.scalars(
                text(
                    "SELECT action FROM audit_log WHERE company_id = :c "
                    "AND target = :t ORDER BY seq"
                ),
                {"c": tenant.company_id, "t": f"execucao:{rid}"},
            )
        ).all()
        chain = await verify_chain(session, tenant.company_id)
    assert actions == ["execucao.iniciada", "execucao.reenfileirada", "execucao.indeterminada"]
    assert chain.ok, chain.problems


async def test_irreversible_marked_by_the_warning_and_attempts_limit(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=1), max_attempts=2)
    tenant = await _tenant_with_robots(platform, app_db, "Manifesto")
    now = datetime.now(UTC)

    # Só o aviso ("irreversivel": true) marca o início do passo irreversível. O
    # passo do manifesto anunciado sem o aviso falhou antes de agir (por exemplo,
    # botão não encontrado) e não impede a volta para a fila.
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    assert await queue.claim_next(now=now) is not None
    await queue.ingest_events(
        run.run_id,
        1,
        [event(run.run_id, 1, 0, RunEventKind.STEP_STARTED, "emitir", efeito="irreversible")],
    )
    assert (await queue.reap_expired(now=now + timedelta(seconds=2))).requeued == [run.run_id]
    job = await queue.claim_next(now=now)
    assert job is not None and job.run_id == run.run_id and job.attempt == 2
    await queue.ingest_events(
        run.run_id,
        2,
        [
            event(
                run.run_id,
                2,
                job.first_seq,
                RunEventKind.STEP_STARTED,
                "emitir",
                irreversivel=True,
            )
        ],
    )
    assert (await queue.reap_expired(now=now + timedelta(seconds=2))).indeterminate == [run.run_id]

    # Leitura que perde a concessão em todas as tentativas acaba falhando.
    reading = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    for attempt in (1, 2):
        job = await queue.claim_next(now=now)
        assert job is not None and job.attempt == attempt
        report = await queue.reap_expired(now=now + timedelta(seconds=2))
    assert report.failed == [reading.run_id]
    status = await platform.get(tenant.operator, reading.run_id)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == "tentativas_esgotadas"


@pytest.mark.parametrize(
    ("reported", "irreversible", "expected"),
    [
        ("succeeded", True, RunState.SUCCEEDED),
        ("failed", False, RunState.FAILED),
        ("failed", True, RunState.INDETERMINATE),
        ("cancelled", True, RunState.INDETERMINATE),
        ("indeterminate", False, RunState.INDETERMINATE),
        ("running", False, RunState.FAILED),
    ],
)
async def test_finish_maps_runner_result(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    reported: str,
    irreversible: bool,
    expected: RunState,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, f"Resultado {reported}")
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    job = await queue.claim_next()
    assert job is not None
    artifact = {
        "kind": "screenshot",
        "name": "tela.png",
        "path": "/tmp/tela.png",
        "size": 10,
        "sha256": "a" * 64,
    }
    assert await queue.finish(
        run.run_id,
        1,
        result(
            run.run_id,
            1,
            reported,
            irreversible_started=irreversible,
            error_code=None if reported == "succeeded" else "falhou",
            error_message=None if reported == "succeeded" else "O passo falhou.",
            failed_step_id=None if reported == "succeeded" else "emitir",
            artifacts=[artifact],
        ),
    )
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is expected
    async with dispatcher_db.dispatcher_session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT irreversible_started_at IS NOT NULL, artifacts, failed_step_id, "
                    "lease_expires_at FROM runs WHERE id = :id"
                ),
                {"id": run.run_id},
            )
        ).one()
    assert row[0] is irreversible
    assert row[1] == [artifact]
    assert row[3] is None
    if reported != "succeeded":
        assert row[2] == "emitir"
        assert status.error_code is not None and status.error_message


async def test_finish_rejects_malformed_results(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db)
    tenant = await _tenant_with_robots(platform, app_db, "Malformado")
    run = await platform.start(tenant.operator, "baixar_notas", {}, RunTrigger.REST)
    assert await queue.claim_next() is not None
    with pytest.raises(InvalidInput):
        await queue.finish(run.run_id, 1, {"run_id": str(run.run_id)})
    with pytest.raises(InvalidInput):
        await queue.finish(run.run_id, 1, result(run.run_id, 2))
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.RUNNING


# ---------------------------------------------------------------------------
# Numeração dos eventos entre tentativas e confirmação do aviso irreversível
# ---------------------------------------------------------------------------


def _launcher(queue: PgRunQueue, tmp_path: Path) -> LocalProcessLauncher:
    return LocalProcessLauncher(
        queue,
        LocalLauncherSettings(work_dir=tmp_path / "trabalho", artifacts_dir=tmp_path / "artefatos"),
    )


async def _deliver(
    queue: PgRunQueue, job: RunnerJob, tmp_path: Path, events: list[RunEvent]
) -> Path:
    """Entrega os eventos como o lançador local faz com o EVENTS.jsonl do worker.

    Devolve a pasta das confirmações (``<seq>.ack``) que o worker espera.
    """
    work = tmp_path / f"{job.run_id}-{job.attempt}"
    (work / "ack").mkdir(parents=True)
    (work / "events.jsonl").write_text(
        "".join(e.model_dump_json() + "\n" for e in events), encoding="utf-8"
    )
    await _launcher(queue, tmp_path)._forward(job, _EventTail(work / "events.jsonl"), work, [])
    return work / "ack"


def _warning(run_id: UUID, attempt: int, seq: int) -> RunEvent:
    """Aviso de início do passo irreversível, como o interpretador o emite."""
    return event(
        run_id,
        attempt,
        seq,
        RunEventKind.STEP_STARTED,
        "emitir",
        tipo="click",
        titulo="Emitir protocolo",
        efeito="irreversible",
        irreversivel=True,
    )


async def _run_row(db: Database, run_id: UUID) -> tuple[str, datetime | None, int]:
    async with db.dispatcher_session() as session:
        row = (
            await session.execute(
                text("SELECT state, irreversible_started_at, attempt FROM runs WHERE id = :id"),
                {"id": run_id},
            )
        ).one()
    return row[0], row[1], row[2]


async def _stored(db: Database, run_id: UUID) -> list[tuple[int, int, str]]:
    async with db.dispatcher_session() as session:
        rows = (
            await session.execute(
                text("SELECT attempt, seq, kind FROM run_events WHERE run_id = :id ORDER BY seq"),
                {"id": run_id},
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _first_attempt_lost_before_emitting(
    queue: PgRunQueue, platform: PgPlatform, tenant: Tenant, now: datetime
) -> UUID:
    """Tentativa 1 grava 7 eventos (com um "step_healed" a mais) e perde a concessão."""
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    rid = run.run_id
    job = await queue.claim_next(now=now)
    assert job is not None and job.attempt == 1 and job.first_seq == 0
    await queue.ingest_events(
        rid,
        1,
        [
            event(rid, 1, 0, RunEventKind.RUN_STARTED),
            event(rid, 1, 1, RunEventKind.STEP_STARTED, "abrir_protocolo", efeito="read"),
            event(rid, 1, 2, RunEventKind.STEP_SUCCEEDED, "abrir_protocolo"),
            event(rid, 1, 3, RunEventKind.STEP_STARTED, "digitar_cpf", efeito="write"),
            event(rid, 1, 4, RunEventKind.STEP_HEALED, "digitar_cpf"),
            event(rid, 1, 5, RunEventKind.STEP_SUCCEEDED, "digitar_cpf"),
            event(rid, 1, 6, RunEventKind.ARTIFACT, "digitar_cpf"),
        ],
    )
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.requeued == [rid]
    return rid


async def test_new_attempt_continues_event_numbering(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant_with_robots(platform, app_db, "Numeração")
    now = datetime.now(UTC)
    rid = await _first_attempt_lost_before_emitting(queue, platform, tenant, now)

    later = now + timedelta(seconds=12)
    job = await queue.claim_next(now=later)
    assert job is not None and job.attempt == 2
    # A tentativa 2 continua a numeração da execução, depois do último evento gravado.
    assert job.first_seq == 7

    # O interpretador da tentativa 2 numera a partir dali.
    sink = ListEventSink()
    interpreter = Interpreter.from_job(
        job,
        secrets=DictSecretProvider({}),
        files=LocalFileProvider(None),
        artifacts=DirectoryArtifactSink(tmp_path / "telas"),
        events=sink,
    )
    await interpreter.fail_without_browser(ErrorCode.NAVEGADOR_INDISPONIVEL, "sem navegador")
    assert [e.seq for e in sink.events] == [7, 8]
    assert all(e.attempt == 2 for e in sink.events)

    # O aviso do passo irreversível da tentativa 2 é gravado antes de ser confirmado.
    acks = await _deliver(
        queue,
        job,
        tmp_path,
        [
            event(rid, 2, 7, RunEventKind.RUN_STARTED),
            event(rid, 2, 8, RunEventKind.STEP_STARTED, "abrir_protocolo", efeito="read"),
            event(rid, 2, 9, RunEventKind.STEP_SUCCEEDED, "abrir_protocolo"),
            _warning(rid, 2, 10),
        ],
    )
    assert (acks / "10.ack").exists()
    state, irreversible_at, _ = await _run_row(dispatcher_db, rid)
    assert state == "running" and irreversible_at is not None
    stored = await _stored(dispatcher_db, rid)
    assert [s[1] for s in stored] == list(range(11))
    assert [s[0] for s in stored] == [1] * 7 + [2] * 4

    # O executor some depois do aviso: indeterminado, nunca de volta à fila.
    report = await queue.reap_expired(now=later + timedelta(seconds=11))
    assert report.indeterminate == [rid] and report.requeued == []


async def test_reused_event_numbers_are_refused_and_never_acknowledged(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant_with_robots(platform, app_db, "Numeração repetida")
    now = datetime.now(UTC)
    rid = await _first_attempt_lost_before_emitting(queue, platform, tenant, now)
    job = await queue.claim_next(now=now + timedelta(seconds=12))
    assert job is not None and job.attempt == 2

    # Um executor que recomeça do zero repete números da tentativa 1: o lote é
    # recusado, o aviso não é confirmado e o executor é parado.
    with pytest.raises(StaleAttempt):
        await _deliver(
            queue,
            job,
            tmp_path,
            [
                event(rid, 2, 0, RunEventKind.RUN_STARTED),
                event(rid, 2, 1, RunEventKind.STEP_STARTED, "abrir_protocolo", efeito="read"),
                event(rid, 2, 2, RunEventKind.STEP_SUCCEEDED, "abrir_protocolo"),
                _warning(rid, 2, 3),
            ],
        )
    assert not (tmp_path / f"{rid}-2" / "ack" / "3.ack").exists()
    state, irreversible_at, _ = await _run_row(dispatcher_db, rid)
    assert state == "running" and irreversible_at is None
    assert all(attempt == 1 for attempt, _, _ in await _stored(dispatcher_db, rid))

    # Mesmo número de uma tentativa anterior, ainda que com o mesmo conteúdo, também.
    with pytest.raises(EventConflict):
        await queue.ingest_events(rid, 2, [event(rid, 2, 6, RunEventKind.ARTIFACT, "digitar_cpf")])
    assert await queue.ingest_events(rid, 2, [event(rid, 2, 7, RunEventKind.RUN_STARTED)]) == 1

    # Número abaixo do início da tentativa, mesmo sem colidir (eventos da tentativa
    # anterior que não chegaram ao banco), também é recusado.
    gap = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    gid = gap.run_id
    assert await queue.claim_next(now=now) is not None
    await queue.ingest_events(
        gid,
        1,
        [event(gid, 1, 0, RunEventKind.RUN_STARTED), event(gid, 1, 5, RunEventKind.ARTIFACT)],
    )
    assert (await queue.reap_expired(now=now + timedelta(seconds=11))).requeued == [gid]
    job = await queue.claim_next(now=now + timedelta(seconds=12))
    assert job is not None and job.run_id == gid and job.first_seq == 6
    with pytest.raises(EventConflict):
        await queue.ingest_events(gid, 2, [_warning(gid, 2, 3)])
    state, irreversible_at, _ = await _run_row(dispatcher_db, gid)
    assert state == "running" and irreversible_at is None
    assert await queue.ingest_events(gid, 2, [_warning(gid, 2, 6)]) == 1


async def test_events_after_the_run_finished_are_refused_and_never_acknowledged(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))  # 3 tentativas
    tenant = await _tenant_with_robots(platform, app_db, "Depois do fim")
    now = datetime.now(UTC)

    # A concessão vence nas 3 tentativas (banco fora do ar): a execução falha de vez.
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    rid = run.run_id
    job: RunnerJob | None = None
    for attempt in (1, 2, 3):
        job = await queue.claim_next(now=now)
        assert job is not None and job.attempt == attempt
        report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert job is not None and report.failed == [rid]

    # O worker da tentativa 3 seguia vivo e chega ao passo irreversível: nada é
    # gravado nem confirmado, e o lançador para o worker.
    with pytest.raises(StaleAttempt):
        await _deliver(queue, job, tmp_path, [_warning(rid, 3, job.first_seq)])
    assert not (tmp_path / f"{rid}-3" / "ack" / f"{job.first_seq}.ack").exists()
    state, irreversible_at, _ = await _run_row(dispatcher_db, rid)
    assert state == "failed" and irreversible_at is None
    assert not await queue.finish(rid, 3, result(rid, 3, "failed"))

    # Indeterminada pelo recolhedor: os passos seguintes do worker também são recusados.
    other = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    oid = other.run_id
    job = await queue.claim_next(now=now)
    assert job is not None and job.run_id == oid
    await queue.ingest_events(oid, 1, [_warning(oid, 1, 0)])
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.indeterminate == [oid]
    with pytest.raises(StaleAttempt):
        await queue.ingest_events(oid, 1, [event(oid, 1, 1, RunEventKind.STEP_SUCCEEDED, "emitir")])
    assert [seq for _, seq, _ in await _stored(dispatcher_db, oid)] == [0]


async def test_irreversible_step_that_failed_before_its_warning_is_a_plain_failure(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10))
    tenant = await _tenant_with_robots(platform, app_db, "Botão sumiu")
    now = datetime.now(UTC)
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    rid = run.run_id
    job = await queue.claim_next(now=now)
    assert job is not None

    # O botão "Emitir" não foi encontrado: o interpretador anuncia o passo sem o
    # aviso (nada foi enviado ao portal) e informa a falha.
    sink = ListEventSink()
    interpreter = Interpreter.from_job(
        job,
        secrets=DictSecretProvider({}),
        files=LocalFileProvider(None),
        artifacts=DirectoryArtifactSink(tmp_path / "telas"),
        events=sink,
    )
    emitir = next(s for s, _ in job.manifest.iter_steps() if s.effect is Effect.IRREVERSIBLE)
    await interpreter._emit_step_started(emitir)
    late = sink.events[0]
    assert late.data.get("efeito") == "irreversible" and "irreversivel" not in late.data
    failed = event(rid, 1, 1, RunEventKind.STEP_FAILED, "emitir", codigo="elemento_nao_encontrado")
    await queue.ingest_events(rid, 1, [late, failed])
    state, irreversible_at, _ = await _run_row(dispatcher_db, rid)
    assert state == "running" and irreversible_at is None

    # Sem resultado (executor sumiu), a execução pode voltar para a fila.
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.requeued == [rid] and report.indeterminate == []

    # Com resultado, é uma falha comum, não um indeterminado.
    job = await queue.claim_next(now=now + timedelta(seconds=12))
    assert job is not None and job.attempt == 2
    first = job.first_seq
    await queue.ingest_events(
        rid,
        2,
        [
            event(rid, 2, first, RunEventKind.STEP_STARTED, "emitir", efeito="irreversible"),
            event(rid, 2, first + 1, RunEventKind.STEP_FAILED, "emitir"),
        ],
    )
    assert await queue.finish(
        rid,
        2,
        result(
            rid,
            2,
            "failed",
            error_code="elemento_nao_encontrado",
            error_message="O botão não foi encontrado.",
            failed_step_id="emitir",
        ),
    )
    status = await platform.get(tenant.operator, rid)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == "elemento_nao_encontrado"
