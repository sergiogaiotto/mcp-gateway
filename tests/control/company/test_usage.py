"""Consumo mensal por área e por origem, com os números conferidos à mão."""

import asyncio
import json
import secrets
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.admin import main
from rapa.control.db import Database
from rapa.control.services import PgBatches, PgCompanyAdmin, PgPlatform
from rapa.control.services.company import current_month, month_period
from rapa.core.contracts import Forbidden, InvalidInput, Role, RunState, RunTrigger, UsageReport
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    PgDatabase,
    Tenant,
    make_tenant,
    protocolo,
    publish,
)
from tests.control.publishing.support import protocol_sheet

from .support import person, set_company_rows

pytestmark = pytest.mark.database

SLUG = "emitir_protocolo"


def _at(text_value: str) -> datetime:
    return datetime.fromisoformat(text_value).replace(tzinfo=UTC)


async def _scenario(app_db: Database, superuser: AsyncEngine) -> tuple[Tenant, dict[str, UUID]]:
    """Execuções, um lote e sessões em horários escolhidos (novembro de 2026, em Brasília)."""
    tenant = await make_tenant(app_db, f"Consumo {secrets.token_hex(3)}", guaranteed_slots=10)
    platform = PgPlatform(app_db)
    robot = (await publish(platform, tenant, protocolo())).robot_id
    ids: dict[str, UUID] = {}
    for name in ("ok", "falhou", "virada"):
        run = await platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
        ids[name] = run.run_id
    timing = {
        # 90 s, deu certo.
        "ok": ("succeeded", "2026-11-10T12:00:00", "2026-11-10T12:00:00", "2026-11-10T12:01:30"),
        # 120 s, falhou.
        "falhou": ("failed", "2026-11-10T13:00:00", "2026-11-10T13:00:00", "2026-11-10T13:02:00"),
        # Pedida às 23h59 de 30/11 em Brasília (02h59 de 1/12 em UTC): conta em novembro.
        "virada": (
            "succeeded",
            "2026-12-01T02:59:00",
            "2026-12-01T02:59:00",
            "2026-12-01T02:59:30",
        ),
    }
    for name, (state, created, started, finished) in timing.items():
        await set_company_rows(
            superuser,
            "UPDATE runs SET state = :state, created_at = :created, started_at = :started, "
            "finished_at = :finished, lease_expires_at = NULL WHERE id = :id",
            state=state,
            created=_at(created),
            started=_at(started),
            finished=_at(finished),
            id=ids[name],
        )
    template = ids["ok"]
    # Uma de agenda, cancelada na fila (nunca começou: zero minuto).
    await set_company_rows(
        superuser,
        "INSERT INTO runs (id, company_id, area_id, robot_id, version_id, trigger, priority, "
        "state, requested_by, finished_at, created_at, queued_at) SELECT gen_random_uuid(), "
        "company_id, area_id, robot_id, version_id, 'schedule', 2, 'cancelled', 'agenda', "
        ":at, :at, :at FROM runs WHERE id = :id",
        at=_at("2026-11-12T09:00:00"),
        id=template,
    )
    # Uma na outra área às 23h30 de 31/10 em Brasília (02h30 de 1/11 em UTC): é de outubro.
    await set_company_rows(
        superuser,
        "INSERT INTO runs (id, company_id, area_id, robot_id, version_id, trigger, priority, "
        "state, requested_by, started_at, finished_at, created_at, queued_at) SELECT "
        "gen_random_uuid(), company_id, :area, robot_id, version_id, 'rest', 1, 'succeeded', "
        "'teste', :start, :end, :start, :start FROM runs WHERE id = :id",
        area=tenant.other_area_id,
        start=_at("2026-11-01T02:30:00"),
        end=_at("2026-11-01T02:31:00"),
        id=template,
    )
    # Um lote de 3 linhas (as execuções ficam na fila: zero minuto).
    batch = await PgBatches(app_db).create_batch(
        tenant.operator, robot, "lote.csv", protocol_sheet()
    )
    await set_company_rows(
        superuser,
        "UPDATE batches SET created_at = :at WHERE id = :id",
        bypass_guards=True,
        at=_at("2026-11-15T12:00:00"),
        id=batch.batch_id,
    )
    await set_company_rows(
        superuser,
        "UPDATE runs SET created_at = :at WHERE id IN "
        "(SELECT run_id FROM batch_rows WHERE batch_id = :id)",
        at=_at("2026-11-15T12:00:00"),
        id=batch.batch_id,
    )
    # Uma sessão de gravação de 10 min na Fiscal, e uma de dezembro na outra área.
    for area, created, stopped in (
        (tenant.area_id, "2026-11-05T13:00:00", "2026-11-05T13:10:00"),
        (tenant.other_area_id, "2026-12-05T13:00:00", "2026-12-05T13:20:00"),
    ):
        await set_company_rows(
            superuser,
            "INSERT INTO recording_sessions (company_id, id, area_id, owner, start_url, status, "
            "created_at, started_at, stopped_at, expires_at, seen_at) VALUES (:company, "
            "gen_random_uuid(), :area, 'criadora', 'https://portal.exemplo.gov.br/', 'stopped', "
            ":created, :created, :stopped, :created + interval '1 hour', :created)",
            company=tenant.company_id,
            area=area,
            created=_at(created),
            stopped=_at(stopped),
        )
    ids["robot"] = robot
    return tenant, ids


def _check_november(report: UsageReport, tenant: Tenant) -> None:
    assert report.month == "2026-11"
    assert report.timezone == "America/Sao_Paulo"
    assert report.period_start == _at("2026-11-01T03:00:00")
    assert report.period_end == _at("2026-12-01T03:00:00")
    by_area = {area.area_id: area for area in report.areas}
    fiscal, legal = by_area[tenant.area_id], by_area[tenant.other_area_id]
    assert (fiscal.area_name, legal.area_name) == ("Fiscal", "Jurídico")
    triggers = {t.trigger: t for t in fiscal.by_trigger}
    rest = triggers[RunTrigger.REST]
    assert rest.runs == 3
    assert rest.runs_by_state == {RunState.FAILED: 1, RunState.SUCCEEDED: 2}
    assert rest.browser_minutes == 4.0  # (90 + 120 + 30) s
    assert triggers[RunTrigger.SCHEDULE].runs_by_state == {RunState.CANCELLED: 1}
    assert triggers[RunTrigger.SCHEDULE].browser_minutes == 0
    assert triggers[RunTrigger.BATCH].runs == 3
    assert triggers[RunTrigger.BATCH].browser_minutes == 0
    assert (fiscal.runs, fiscal.browser_minutes) == (7, 4.0)
    assert (fiscal.sessions, fiscal.session_minutes) == (1, 10.0)
    assert (fiscal.batches, fiscal.batch_rows) == (1, 3)
    assert (legal.runs, legal.sessions, legal.batches, legal.by_trigger) == (0, 0, 0, [])
    assert (report.runs, report.browser_minutes) == (7, 4.0)
    assert (report.sessions, report.session_minutes) == (1, 10.0)
    assert (report.batches, report.batch_rows) == (1, 3)


async def test_monthly_usage_by_area_and_trigger(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, _ = await _scenario(app_db, superuser_engine)
    service = PgCompanyAdmin(app_db)
    _check_november(await service.usage(tenant.admin, "2026-11"), tenant)
    october = await service.usage(tenant.admin, "2026-10")
    legal = next(a for a in october.areas if a.area_id == tenant.other_area_id)
    assert (legal.runs, legal.browser_minutes) == (1, 1.0)
    assert october.runs == 1
    december = await service.usage(tenant.admin, "2026-12")
    assert december.runs == 0
    assert (december.sessions, december.session_minutes) == (1, 20.0)


async def test_who_sees_the_usage(app_db: Database, superuser_engine: AsyncEngine) -> None:
    tenant, _ = await _scenario(app_db, superuser_engine)
    service = PgCompanyAdmin(app_db)
    tag = secrets.token_hex(3)
    auditor = await person(app_db, tenant.company_id, f"auditora-{tag}@x", (Role.AUDITOR, None))
    _check_november(await service.usage(auditor, "2026-11"), tenant)
    # Quem audita só a outra área vê só ela.
    local = await person(
        app_db, tenant.company_id, f"auditor-{tag}@x", (Role.AUDITOR, tenant.other_area_id)
    )
    report = await service.usage(local, "2026-11")
    assert [a.area_id for a in report.areas] == [tenant.other_area_id]
    assert report.runs == 0 and report.sessions == 0
    boss = await person(
        app_db, tenant.company_id, f"chefe-{tag}@x", (Role.AREA_ADMIN, tenant.area_id)
    )
    for principal in (tenant.operator, tenant.creator, tenant.viewer, boss):
        with pytest.raises(Forbidden):
            await service.usage(principal, "2026-11")
    for bad in ("2026-13", "26-11", "2026-1", "1999-12"):
        with pytest.raises(InvalidInput):
            await service.usage(tenant.admin, bad)


async def test_unfinished_runs_and_sessions_count_but_add_no_minutes(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Minutos de navegador são ``finished_at - started_at``: o que não terminou ainda não soma.

    Assim o relatório de um mês passado não muda sozinho enquanto algo dele continua aberto.
    """
    tenant = await make_tenant(app_db, f"Aberto {secrets.token_hex(3)}", guaranteed_slots=10)
    platform = PgPlatform(app_db)
    await publish(platform, tenant, protocolo())
    done = await platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    open_run = await platform.start(tenant.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    session_id = UUID(int=secrets.randbits(128))
    try:
        await set_company_rows(
            superuser_engine,
            "UPDATE runs SET state = 'succeeded', created_at = :at, started_at = :at, "
            "finished_at = :at + interval '60 s', lease_expires_at = NULL WHERE id = :id",
            at=_at("2026-11-10T12:00:00"),
            id=done.run_id,
        )
        # Começou em novembro e ainda roda (o prazo do executor está longe).
        await set_company_rows(
            superuser_engine,
            "UPDATE runs SET state = 'running', created_at = :at, started_at = :at, "
            "lease_expires_at = now() + interval '1 day' WHERE id = :id",
            at=_at("2026-11-10T13:00:00"),
            id=open_run.run_id,
        )
        # Uma sessão de gravação ainda aberta (o prazo dela já passou; ninguém a encerrou).
        await set_company_rows(
            superuser_engine,
            "INSERT INTO recording_sessions (company_id, id, area_id, owner, start_url, status, "
            "created_at, started_at, expires_at, seen_at) VALUES (:company, :id, :area, "
            "'criadora', 'https://portal.exemplo.gov.br/', 'active', :at, :at, "
            ":at + interval '1 hour', :at)",
            company=tenant.company_id,
            id=session_id,
            area=tenant.area_id,
            at=_at("2026-11-05T13:00:00"),
        )
        report = await PgCompanyAdmin(app_db).usage(tenant.admin, "2026-11")
        fiscal = next(a for a in report.areas if a.area_id == tenant.area_id)
        rest = next(t for t in fiscal.by_trigger if t.trigger is RunTrigger.REST)
        assert rest.runs_by_state == {RunState.RUNNING: 1, RunState.SUCCEEDED: 1}
        assert (rest.runs, rest.browser_minutes) == (2, 1.0)
        assert (fiscal.sessions, fiscal.session_minutes) == (1, 0)
        assert (report.browser_minutes, report.session_minutes) == (1.0, 0)
    finally:
        # Nada fica aberto para os outros testes (o despachante olha todas as empresas).
        await set_company_rows(
            superuser_engine,
            "UPDATE runs SET state = 'cancelled', finished_at = now(), lease_expires_at = NULL "
            "WHERE id = :id AND state = 'running'",
            id=open_run.run_id,
        )
        await set_company_rows(
            superuser_engine,
            "UPDATE recording_sessions SET status = 'expired', stopped_at = now() WHERE id = :id",
            id=session_id,
        )


def test_month_period_and_current_month() -> None:
    assert month_period("2026-12") == (_at("2026-12-01T03:00:00"), _at("2027-01-01T03:00:00"))
    assert current_month(_at("2026-12-01T02:59:00")) == "2026-11"
    assert current_month(_at("2026-12-01T03:00:00")) == "2026-12"


async def test_cli_usage_prints_the_same_numbers(
    pg_database: PgDatabase,
    app_db: Database,
    superuser_engine: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant, _ = await _scenario(app_db, superuser_engine)
    argv = ["--url", pg_database.app_url, "empresa", "consumo", str(tenant.company_id)]
    code = await asyncio.to_thread(main, [*argv, "--mes", "2026-11", "--json"])
    assert code == 0
    report = UsageReport.model_validate(json.loads(capsys.readouterr().out))
    _check_november(report, tenant)
    assert await asyncio.to_thread(main, [*argv, "--mes", "2026-11"]) == 0
    printed = capsys.readouterr().out
    assert "Total: 7 execuções, 4.0 min de navegador" in printed
    assert await asyncio.to_thread(main, [*argv, "--mes", "novembro"]) == 1
    assert "mês inválido" in capsys.readouterr().err
