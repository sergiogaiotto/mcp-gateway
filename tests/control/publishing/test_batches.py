"""Lotes no PostgreSQL: prévia sem criar nada, tudo ou nada, prioridade, andamento e resultado."""

import csv
import io
from typing import Any

import openpyxl
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgRunQueue, verify_chain
from rapa.control.services._common import PRIORITY_BY_TRIGGER
from rapa.control.services.sheets import SheetLimits
from rapa.core.contracts import (
    BatchFormat,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RunState,
    RunTrigger,
)
from tests.control.db.support import result

from .support import (
    PROTOCOL_ROWS,
    World,
    build_world,
    csv_bytes,
    protocol_sheet,
    runs_of,
    xlsx_bytes,
)

pytestmark = pytest.mark.database


async def _world(app_db: Database, superuser: AsyncEngine, **kwargs: Any) -> World:
    return await build_world(app_db, superuser, **kwargs)


async def _count(superuser: AsyncEngine, table: str) -> int:
    async with superuser.connect() as conn:
        return int(await conn.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def test_preview_checks_every_row_and_creates_nothing(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    robot = world.robots_a["emitir_protocolo"]
    before = (await _count(superuser_engine, "runs"), await _count(superuser_engine, "batches"))
    data = csv_bytes(
        [("Nome completo", "CPF", "UF", "Obs"), PROTOCOL_ROWS[0], ("Fulano", "000", "SP", "x")]
    )
    preview = await world.batches.preview_batch(world.a.operator, robot, "../pasta/lote.csv", data)
    assert preview.file_name == "lote.csv" and preview.format is BatchFormat.CSV
    assert preview.rows == 2 and preview.error_rows == 1 and not preview.ok
    assert [(e.line, e.errors) for e in preview.errors] == [
        (3, ["cpf: valor fora do formato esperado"])
    ]
    assert [(c.header, c.input) for c in preview.columns][-1] == ("Obs", None)
    assert preview.sample == [{"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}]
    assert preview.version_no == 1 and len(preview.file_sha256) == 64
    good = await world.batches.preview_batch(world.a.operator, robot, "lote.csv", protocol_sheet())
    assert good.ok and good.rows == 3
    after = (await _count(superuser_engine, "runs"), await _count(superuser_engine, "batches"))
    assert after == before


async def test_one_bad_row_and_nothing_is_created(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    robot = world.robots_a["emitir_protocolo"]
    before = await _count(superuser_engine, "runs")
    rows = [*PROTOCOL_ROWS, ("Sem CPF", "", "SP")]
    with pytest.raises(InvalidInput) as caught:
        await world.batches.create_batch(world.a.operator, robot, "lote.csv", protocol_sheet(rows))
    assert caught.value.message == "o lote tem erros; nada foi criado"
    assert caught.value.errors == ["linha 5: campo obrigatório ausente: cpf"]
    assert await _count(superuser_engine, "runs") == before
    assert await world.batches.list_batches(world.a.operator) == []


async def test_create_one_run_per_row_with_the_lowest_priority(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    robot = world.robots_a["emitir_protocolo"]
    rows = [("Nome completo", "CPF", "UF"), *PROTOCOL_ROWS]
    info = await world.batches.create_batch(world.a.operator, robot, "lote.xlsx", xlsx_bytes(rows))
    assert info.total == 3 and info.format is BatchFormat.XLSX and info.version_no == 1
    assert info.counts == {RunState.QUEUED: 3} and not info.finished
    runs = await runs_of(superuser_engine, "batch", world.a.company_id)
    assert len(runs) == 3
    assert {run["priority"] for run in runs} == {PRIORITY_BY_TRIGGER[RunTrigger.BATCH]} == {3}
    assert [run["inputs"]["cpf"] for run in runs] == [row[1] for row in PROTOCOL_ROWS]
    batch_rows = await world.batches.list_batch_rows(world.a.viewer, info.batch_id)
    assert [(r.row_no, r.line) for r in batch_rows] == [(1, 2), (2, 3), (3, 4)]
    assert {r.run_id for r in batch_rows} == {run["id"] for run in runs}

    # Uma execução interativa da REST passa à frente do lote inteiro.
    rest = await world.platform.start(
        world.a.operator,
        "emitir_protocolo",
        dict(zip(("nome", "cpf", "uf"), PROTOCOL_ROWS[0], strict=True)),
        RunTrigger.REST,
    )
    queue = PgRunQueue(dispatcher_db)
    first = await queue.claim_next()
    assert first is not None and first.run_id == rest.run_id

    # O andamento é a soma das execuções; o resultado só traz as saídas declaradas.
    for _ in range(3):
        job = await queue.claim_next()
        assert job is not None
        outputs = {"protocolo": f"PRT-{job.run_id.hex[:6]}", "nao_declarada": "x"}
        state = "failed" if job.inputs["uf"] == "RJ" else "succeeded"
        extra: dict[str, Any] = {"outputs": outputs if state == "succeeded" else None}
        if state == "failed":
            extra.update(error_code="elemento_nao_encontrado", error_message="O botão sumiu.")
        assert await queue.finish(
            job.run_id, job.attempt, result(job.run_id, job.attempt, state, **extra)
        )
    done = await world.batches.get_batch(world.a.viewer, info.batch_id)
    assert done is not None and done.finished and done.done == 3
    assert done.counts == {RunState.SUCCEEDED: 2, RunState.FAILED: 1}
    by_line = {
        r.line: r for r in await world.batches.list_batch_rows(world.a.viewer, info.batch_id)
    }
    assert (
        by_line[3].state is RunState.FAILED and by_line[3].error_code == "elemento_nao_encontrado"
    )
    assert by_line[2].outputs is not None and set(by_line[2].outputs) == {"protocolo"}

    csv_file = await world.batches.batch_results(world.a.viewer, info.batch_id, BatchFormat.CSV)
    assert csv_file.file_name.endswith(".csv") and csv_file.media_type.startswith("text/csv")
    table = list(csv.reader(io.StringIO(csv_file.content.decode("utf-8-sig")), delimiter=";"))
    assert table[0] == [
        "linha",
        "execucao",
        "situacao",
        "inicio",
        "fim",
        "protocolo",
        "erro",
        "mensagem",
    ]
    assert [(row[0], row[2]) for row in table[1:]] == [
        ("2", "Concluída"),
        ("3", "Falhou"),
        ("4", "Concluída"),
    ]
    assert "nao_declarada" not in csv_file.content.decode("utf-8-sig")
    # As entradas (que podem ser sensíveis) não vão para o arquivo.
    assert PROTOCOL_ROWS[0][1] not in csv_file.content.decode("utf-8-sig")
    xlsx_file = await world.batches.batch_results(world.a.viewer, info.batch_id, BatchFormat.XLSX)
    sheet = openpyxl.load_workbook(io.BytesIO(xlsx_file.content)).active
    assert sheet.max_row == 4 and sheet.cell(row=3, column=3).value == "Falhou"

    async with app_db.tenant_session(world.a.company_id) as session:
        chain = await verify_chain(session, world.a.company_id)
        actions = (await session.scalars(text("SELECT action FROM audit_log"))).all()
    assert chain.ok
    assert actions.count("lote.criado") == 1
    assert actions.count("execucao.iniciada") == 4


async def test_idempotency_key_returns_the_same_batch(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    robot = world.robots_a["emitir_protocolo"]
    data = protocol_sheet()
    first = await world.batches.create_batch(
        world.a.operator, robot, "a.csv", data, idempotency_key="k1"
    )
    again = await world.batches.create_batch(
        world.a.operator, robot, "a.csv", data, idempotency_key="k1"
    )
    assert again.batch_id == first.batch_id
    assert len(await runs_of(superuser_engine, "batch", world.a.company_id)) == 3
    with pytest.raises(Conflict):
        await world.batches.create_batch(
            world.a.operator,
            robot,
            "a.csv",
            protocol_sheet(PROTOCOL_ROWS[:2]),
            idempotency_key="k1",
        )


async def test_queue_cap_is_all_or_nothing(app_db: Database, superuser_engine: AsyncEngine) -> None:
    world = await _world(app_db, superuser_engine, max_queued=2)
    robot = world.robots_a["emitir_protocolo"]
    with pytest.raises(QuotaExceeded) as caught:
        await world.batches.create_batch(world.a.operator, robot, "a.csv", protocol_sheet())
    assert "3 execuções" in caught.value.message
    assert await runs_of(superuser_engine, "batch", world.a.company_id) == []
    assert await world.batches.list_batches(world.a.operator) == []
    two = await world.batches.create_batch(
        world.a.operator, robot, "a.csv", protocol_sheet(PROTOCOL_ROWS[:2])
    )
    assert two.total == 2


async def test_sheet_caps_are_configurable(app_db: Database, superuser_engine: AsyncEngine) -> None:
    world = await _world(app_db, superuser_engine, limits=SheetLimits(max_rows=2))
    robot = world.robots_a["emitir_protocolo"]
    with pytest.raises(InvalidInput) as caught:
        await world.batches.preview_batch(world.a.operator, robot, "a.csv", protocol_sheet())
    assert "limite de 2 linhas" in caught.value.message
    with pytest.raises(InvalidInput):
        await world.batches.create_batch(
            world.a.operator,
            robot,
            "a.csv",
            xlsx_bytes([("Nome completo", "CPF", "UF"), PROTOCOL_ROWS[0]], formula_at=(2, 1)),
        )


async def test_permissions_and_other_companies(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    a, b = world.a, world.b
    robot = world.robots_a["emitir_protocolo"]
    for who in (a.viewer, a.creator):
        with pytest.raises(Forbidden):
            await world.batches.preview_batch(who, robot, "a.csv", protocol_sheet())
        with pytest.raises(Forbidden):
            await world.batches.create_batch(who, robot, "a.csv", protocol_sheet())
    for who in (a.outsider, b.admin):
        with pytest.raises(NotFound):
            await world.batches.create_batch(who, robot, "a.csv", protocol_sheet())
    info = await world.batches.create_batch(a.admin, robot, "a.csv", protocol_sheet())
    assert [x.batch_id for x in await world.batches.list_batches(a.viewer)] == [info.batch_id]
    for who in (a.outsider, b.admin):
        assert await world.batches.get_batch(who, info.batch_id) is None
        assert await world.batches.list_batches(who) == []
        with pytest.raises(NotFound):
            await world.batches.list_batch_rows(who, info.batch_id)
        with pytest.raises(NotFound):
            await world.batches.batch_results(who, info.batch_id, BatchFormat.CSV)
