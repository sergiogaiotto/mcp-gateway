"""As métricas que vêm do banco: execuções concluídas pela fila e a leitura da fila e do A1."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.observability.metrics import Metrics, read_platform_snapshot
from rapa.control.services import PgPlatform, PgRunQueue
from rapa.control.services.queue import ConcludedRun
from rapa.core.contracts import CredentialInfo, RunState, RunTrigger
from rapa.core.events import RunEventKind
from rapa.core.robot import CredentialKind
from tests.control.company.support import freeze
from tests.control.credentials.support import ORIGIN, A1World, a1_world, in_days, set_not_after
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    event,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
    result,
)

from .support import value

pytestmark = pytest.mark.database


class _Recorder:
    def __init__(self) -> None:
        self.runs: list[ConcludedRun] = []

    def run_concluded(self, run: ConcludedRun) -> None:
        self.runs.append(run)


class _Broken:
    def run_concluded(self, run: ConcludedRun) -> None:
        raise RuntimeError("falha do observador com detalhe interno")


async def test_finished_runs_reach_the_metrics_with_the_final_state(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Metricas da fila")
    await publish(platform, tenant, protocolo())
    recorder = _Recorder()
    queue = PgRunQueue(dispatcher_db, observer=recorder)

    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.MCP
    )
    job = await queue.claim_next()
    assert job is not None and job.run_id == run.run_id
    assert recorder.runs == []
    # Um resultado de outra tentativa é recusado antes de gravar: nada conta.
    with pytest.raises(Exception):  # noqa: B017 - qualquer recusa serve aqui
        await queue.finish(job.run_id, job.attempt + 1, result(job.run_id, job.attempt + 1))
    assert recorder.runs == []
    body = result(
        job.run_id,
        job.attempt,
        "failed",
        error_code="elemento_nao_encontrado",
        error_message="mensagem com dado da execução",
        irreversible_started=True,
    )
    assert await queue.finish(job.run_id, job.attempt, body)
    [concluded] = recorder.runs
    # A situação é a final, decidida pela fila (depois de um passo irreversível,
    # indeterminada), não a que o executor informou; a origem vem da execução.
    assert concluded.company_id == tenant.company_id
    assert concluded.state is RunState.INDETERMINATE
    assert concluded.trigger == "mcp"
    assert concluded.duration_s is not None and 0 <= concluded.duration_s < 60
    # A mesma execução não conta duas vezes.
    assert not await queue.finish(job.run_id, job.attempt, body)
    assert len(recorder.runs) == 1

    # Com as métricas de verdade: contada pela situação e pela origem.
    metrics = Metrics()
    counted = PgRunQueue(dispatcher_db, observer=metrics)
    second = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.SCHEDULE
    )
    job = await counted.claim_next()
    assert job is not None and job.run_id == second.run_id
    assert await counted.finish(job.run_id, job.attempt, result(job.run_id, job.attempt))
    assert value(metrics, "rapa_execucoes_total", situacao="succeeded", origem="schedule") == 1.0
    assert value(metrics, "rapa_execucao_duracao_segundos_count", situacao="succeeded") == 1.0


async def test_a_broken_observer_never_changes_what_the_queue_did(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Observador quebrado")
    await publish(platform, tenant, protocolo())
    queue = PgRunQueue(dispatcher_db, observer=_Broken())
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    job = await queue.claim_next()
    assert job is not None and job.run_id == run.run_id
    with caplog.at_level(logging.WARNING):
        assert await queue.finish(job.run_id, job.attempt, result(job.run_id, job.attempt))
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.SUCCEEDED
    assert "falha ao registrar uma execução concluída: RuntimeError" in caplog.text
    assert "detalhe interno" not in caplog.text


async def test_runs_failed_for_an_unreadable_manifest_count_too(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Manifesto ilegivel")
    version = await publish(platform, tenant, protocolo())
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.SCHEDULE
    )
    # Uma versão que esta versão da plataforma não lê (o gatilho de imutabilidade fica
    # de fora só nesta transação do superusuário).
    async with superuser_engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(
            text("UPDATE robot_versions SET manifest = '{\"formato\": 999}' WHERE id = :id"),
            {"id": version.version_id},
        )
    metrics = Metrics()
    queue = PgRunQueue(dispatcher_db, observer=metrics)
    assert await queue.claim_next() is None
    status = await platform.get(tenant.operator, run.run_id)
    assert status is not None and status.state is RunState.FAILED
    assert status.error_code == "manifesto_invalido"
    # Conta como falha, pela origem; sem duração (não chegou a começar).
    assert value(metrics, "rapa_execucoes_total", situacao="failed", origem="schedule") == 1.0
    assert value(metrics, "rapa_execucao_duracao_segundos_count", situacao="failed") is None
    # Só uma vez: a execução não volta a ser pega.
    assert await queue.claim_next() is None
    assert value(metrics, "rapa_execucoes_total", situacao="failed", origem="schedule") == 1.0


async def test_reaped_runs_count_only_when_they_end(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    tenant = await make_tenant(app_db, "Recolhidas")
    await publish(platform, tenant, protocolo())
    recorder = _Recorder()
    queue = PgRunQueue(dispatcher_db, lease=timedelta(seconds=10), observer=recorder)
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.WEBHOOK
    )
    rid = run.run_id
    now = datetime.now(UTC)
    assert await queue.claim_next(now=now) is not None
    # Sem nada que grave: de volta à fila, e não conta (a execução não terminou).
    report = await queue.reap_expired(now=now + timedelta(seconds=11))
    assert report.requeued == [rid] and report.concluded == []
    assert recorder.runs == []
    later = now + timedelta(seconds=20)
    job = await queue.claim_next(now=later)
    assert job is not None and job.attempt == 2
    await queue.ingest_events(
        rid,
        2,
        [
            event(rid, 2, job.first_seq, RunEventKind.RUN_STARTED),
            event(
                rid,
                2,
                job.first_seq + 1,
                RunEventKind.STEP_STARTED,
                "emitir",
                efeito="write",
                grava=True,
            ),
        ],
    )
    report = await queue.reap_expired(now=later + timedelta(seconds=11))
    assert report.failed == [rid]
    [concluded] = recorder.runs
    assert concluded.state is RunState.FAILED and concluded.trigger == "webhook"
    assert concluded.duration_s is not None and concluded.duration_s >= 11
    assert report.concluded == recorder.runs


async def _a1(world: A1World, name: str, fingerprint: str, not_after: datetime) -> CredentialInfo:
    return await world.credentials.register(
        world.area_admin,
        world.tenant.area_id,
        name=name,
        kind=CredentialKind.PFX_A1,
        origin=ORIGIN,
        vault_path=f"empresas/{world.tenant.company_id}/{name}",
        custodian_subject=world.custodian.subject,
        not_after=not_after,
        fingerprint=fingerprint,
    )


async def test_platform_snapshot_reads_the_queue_and_the_a1_warnings(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    owner_engine: AsyncEngine,
) -> None:
    await reset_queue(superuser_engine, total_slots=5)
    platform = PgPlatform(app_db)
    world = await a1_world(app_db, "Leitura da fila")
    tenant = world.tenant
    await publish(platform, tenant, protocolo())
    for _ in range(3):
        await platform.start(tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db)
    assert await queue.claim_next() is not None
    # Certificados: um vencendo em 5 dias, um vencido, um longe do aviso, um revogado.
    await _a1(world, "a1_vencendo", "a1" * 32, in_days(5))
    expired = await _a1(world, "a1_vencido", "b2" * 32, in_days(200))
    await set_not_after(superuser_engine, expired.credential_id, in_days(-2))
    await _a1(world, "a1_longe", "c3" * 32, in_days(300))
    revoked = await _a1(world, "a1_revogado", "d4" * 32, in_days(1))
    await world.credentials.revoke(world.custodian, revoked.credential_id)
    frozen = await a1_world(app_db, "Congelada com A1")
    await _a1(frozen, "a1_congelada", "e5" * 32, in_days(3))
    await freeze(owner_engine, frozen.tenant.company_id)

    now = datetime.now(UTC)
    snapshot = await read_platform_snapshot(dispatcher_db, now=now, warn_days=30)
    counts = snapshot.companies[tenant.company_id]
    assert (counts.queued, counts.running) == (2, 1)
    assert counts.oldest_queued_s is not None and 0 <= counts.oldest_queued_s < 120
    assert (counts.a1_expiring, counts.a1_expired) == (1, 1)
    assert counts.a1_days_left is not None and -2.1 < counts.a1_days_left < -1.9
    # Empresa congelada não recebe aviso (como na etapa dos avisos do despachante).
    assert frozen.tenant.company_id not in snapshot.companies

    metrics = Metrics(per_company=True)
    metrics.update_platform(snapshot)
    company = str(tenant.company_id)
    assert value(metrics, "rapa_fila_execucoes", empresa=company) == 2.0
    assert value(metrics, "rapa_a1_em_aviso", empresa=company, tipo="vencido") == 1.0
    text = metrics.render().decode()
    # Sem nomes: nem das credenciais, nem da empresa, nem das pessoas.
    for secret in ("a1_vencendo", "a1_vencido", "Leitura da fila", world.custodian.subject):
        assert secret not in text
