"""Retenção: cada prazo, os lotes, as partições, as funções do banco e a outra empresa."""

import asyncio
import json
from collections import Counter
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.artifacts import ArtifactStore, LocalArtifactStore, attempt_dir
from rapa.control.db import Database
from rapa.control.services import (
    Dispatcher,
    PgArtifacts,
    Retention,
    RetentionPolicy,
    verify_chain,
)
from rapa.control.services.a1 import ensure_a1_ready
from rapa.control.services.lifecycle import freeze_company, unfreeze_company
from rapa.core.contracts import ArtifactStatus, Gone, RunTrigger
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    PgDatabase,
    Tenant,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
)
from tests.control.db.test_dispatcher import _BrokenStepQueue, _succeed
from tests.control.evidence.support import (
    FAILURE_FILES,
    PDF,
    PNG,
    EvidenceWorld,
    age_run,
    run_row,
)

pytestmark = pytest.mark.database


def _files(world: EvidenceWorld, tenant: Tenant, run_id: UUID) -> list[Path]:
    run_dir = attempt_dir(world.store.root, tenant.company_id, run_id, 1).parent
    return sorted(run_dir.rglob("*.*")) if run_dir.exists() else []


def _retention(world: EvidenceWorld, db: Database, **changes: Any) -> Retention:
    policy = RetentionPolicy(**{"batch_size": 50, **changes.pop("policy", {})})
    return Retention(db, world.store, policy, **changes)


async def _sqlstate(
    db: Database, *statements: str, params: dict[str, Any] | None = None
) -> str | None:
    """Roda os comandos numa transação do papel de ``db``; devolve o SQLSTATE do erro."""
    try:
        async with db.dispatcher_session() as session:
            for statement in statements:
                await session.execute(text(statement), params or {})
    except DBAPIError as exc:
        return getattr(exc.orig, "sqlstate", None)
    return None


@pytest.fixture
async def world(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> EvidenceWorld:
    await reset_queue(superuser_engine, total_slots=20)
    return EvidenceWorld(app_db, dispatcher_db, tmp_path / "artefatos")


async def _tenant(world: EvidenceWorld, app_db: Database, name: str) -> Tenant:
    tenant = await make_tenant(app_db, name)
    await publish(world.platform, tenant, protocolo())
    return tenant


async def test_each_deadline_and_the_other_company(
    world: EvidenceWorld,
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
) -> None:
    a = await _tenant(world, app_db, "Retenção A")
    b = await _tenant(world, app_db, "Retenção B")
    # Cada execução guarda uma tela, o vídeo e um arquivo baixado (FAILURE_FILES).
    everything = sorted(FAILURE_FILES)
    download = ["nota-001.pdf"]
    cases: dict[str, tuple[Tenant, str, float, list[str]]] = {
        # nome: (empresa, estado, dias desde o fim, arquivos que ficam)
        "sucesso_velho": (a, "succeeded", 8, download),
        "sucesso_novo": (a, "succeeded", 6, everything),
        "sucesso_de_tres_meses": (a, "succeeded", 91, []),
        "cancelada_velha": (a, "cancelled", 8, download),
        "falha_de_um_mes": (a, "failed", 30, everything),
        "falha_velha": (a, "failed", 91, []),
        "indeterminada_de_um_mes": (a, "indeterminate", 30, everything),
        "indeterminada_velha": (a, "indeterminate", 91, []),
        "outra_sucesso_velho": (b, "succeeded", 8, download),
        "outra_falha_de_um_mes": (b, "failed", 30, everything),
    }
    ids: dict[str, UUID] = {}
    for name, (tenant, state, days, _) in cases.items():
        ids[name] = await world.run(tenant, state=state)
        await age_run(superuser_engine, ids[name], days)
    before = {name: _files(world, cases[name][0], run_id) for name, run_id in ids.items()}
    assert all(len(files) == 3 for files in before.values())

    report = await _retention(world, dispatcher_db).tick()

    service = PgArtifacts(app_db, world.store)
    for name, (tenant, _state, _days, kept) in cases.items():
        row = await run_row(superuser_engine, ids[name])
        gone = kept == []
        captures_gone = kept == download
        assert (row["artifacts_expired_at"] is not None) is gone, name
        assert (row["captures_expired_at"] is not None) is captures_gone, name
        assert (ids[name] in report.artifacts) is gone, name
        assert (ids[name] in report.captures) is captures_gone, name
        assert [path.name for path in _files(world, tenant, ids[name])] == kept, name
        # A lista de artefatos fica (nomes, tamanhos e hashes), com a marca de cada um.
        assert len(row["artifacts"]) == 3, name
        listed = {
            item.name: item.status
            for item in await service.list_artifacts(tenant.operator, ids[name])
        }
        assert sorted(n for n, st in listed.items() if st is ArtifactStatus.AVAILABLE) == kept
        for artifact, status in listed.items():
            if status is ArtifactStatus.EXPIRED:
                with pytest.raises(Gone):
                    await service.open_artifact(tenant.operator, ids[name], artifact)
    # O arquivo baixado de uma execução que deu certo ainda é servido (evidência: 90 dias).
    content = await service.open_artifact(a.operator, ids["sucesso_velho"], "nota-001.pdf")
    assert await content.read() == PDF
    assert not report.behind and report.failed == []
    for tenant in (a, b):
        async with app_db.tenant_session(tenant.company_id) as session:
            chain = await verify_chain(session, tenant.company_id)
            actions = Counter((await session.scalars(text("SELECT action FROM audit_log"))).all())
        assert chain.ok, chain.problems
        mine = [kept for t, *_rest, kept in cases.values() if t is tenant]
        assert actions["execucao.artefatos_expirados"] == mine.count([])
        assert actions["execucao.capturas_expiradas"] == mine.count(download)
    # Uma segunda volta não faz nada de novo.
    again = await _retention(world, dispatcher_db).tick()
    assert not set(again.artifacts + again.captures) & set(ids.values())
    # Passado o prazo das evidências, o arquivo baixado da que deu certo também sai.
    await age_run(superuser_engine, ids["sucesso_velho"], 91)
    later = await _retention(world, dispatcher_db).tick()
    assert ids["sucesso_velho"] in later.artifacts
    assert _files(world, a, ids["sucesso_velho"]) == []
    row = await run_row(superuser_engine, ids["sucesso_velho"])
    assert row["artifacts_expired_at"] is not None and row["captures_expired_at"] is not None
    assert {
        item.status for item in await service.list_artifacts(a.operator, ids["sucesso_velho"])
    } == {ArtifactStatus.EXPIRED}


async def test_batches_are_small_and_the_rest_waits(
    world: EvidenceWorld, app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await _retention(world, dispatcher_db).tick()  # o que outros testes deixaram vencido
    tenant = await _tenant(world, app_db, "Lotes")
    ids = []
    for offset in range(5):
        run_id = await world.run(tenant, state="succeeded", files={"t.png": ("screenshot", PNG)})
        await age_run(superuser_engine, run_id, 5000 + offset)
        ids.append(run_id)
    small = _retention(world, dispatcher_db, policy={"batch_size": 2}, max_batches=1)
    first = await small.tick()
    # Um lote de duas, das mais antigas; o resto fica para a próxima volta.
    assert first.artifacts == [ids[4], ids[3]]
    assert first.behind and small.behind
    rest = await _retention(world, dispatcher_db, policy={"batch_size": 2}).tick()
    assert set(ids[:3]) <= set(rest.artifacts)
    for run_id in ids:
        assert (await run_row(superuser_engine, run_id))["artifacts_expired_at"] is not None


class _FlakyStore:
    """Store que não consegue apagar os arquivos de uma execução."""

    def __init__(self, inner: ArtifactStore, broken: UUID) -> None:
        self.inner = inner
        self.broken = broken

    async def put(self, *args: Any) -> Any:  # pragma: no cover - não é usado aqui
        return await self.inner.put(*args)

    async def open(self, *args: Any) -> Any:  # pragma: no cover - não é usado aqui
        return await self.inner.open(*args)

    async def delete_run(
        self, company_id: UUID, run_id: UUID, *, keep: Collection[tuple[int, str]] = ()
    ) -> None:
        if run_id == self.broken:
            raise PermissionError("sem permissão na pasta")
        await self.inner.delete_run(company_id, run_id, keep=keep)

    def list_company(self, company_id: UUID) -> Any:  # pragma: no cover - não é usado aqui
        return self.inner.list_company(company_id)

    async def delete_company(self, company_id: UUID) -> int:  # pragma: no cover
        return await self.inner.delete_company(company_id)

    async def release_attempt(self, *args: Any) -> None:  # pragma: no cover
        await self.inner.release_attempt(*args)

    async def staging_companies(self) -> list[UUID]:
        return await self.inner.staging_companies()

    async def discard_staged_company(self, company_id: UUID) -> None:  # pragma: no cover
        await self.inner.discard_staged_company(company_id)


class _CountingStore:
    """O store de verdade, anotando cada execução que a retenção mandou apagar."""

    def __init__(self, inner: ArtifactStore) -> None:
        self.inner = inner
        self.deleted: list[UUID] = []

    async def put(self, *args: Any) -> Any:  # pragma: no cover - não é usado aqui
        return await self.inner.put(*args)

    async def open(self, *args: Any) -> Any:  # pragma: no cover - não é usado aqui
        return await self.inner.open(*args)

    async def delete_run(
        self, company_id: UUID, run_id: UUID, *, keep: Collection[tuple[int, str]] = ()
    ) -> None:
        self.deleted.append(run_id)
        await self.inner.delete_run(company_id, run_id, keep=keep)


async def test_runs_with_nothing_listed_are_only_marked(
    world: EvidenceWorld,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    """Sem nada na lista que a etapa expira, a execução só ganha a marca (não volta na
    próxima busca), sem auditoria; o store só é chamado para a que chegou a ser lançada,
    e o que sobrou na pasta dela (um executor que caiu) sai do mesmo jeito."""
    await _retention(world, dispatcher_db).tick()  # o que outros testes deixaram vencido
    tenant = await _tenant(world, app_db, "Nada na lista")
    # Nunca lançadas: canceladas na fila pelo congelamento (sem tentativa, sem pasta).
    queued = [
        (
            await world.platform.start(
                tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
            )
        ).run_id
        for _ in range(2)
    ]
    await freeze_company(owner_engine, tenant.company_id, reason="teste", requested_by="teste")
    await unfreeze_company(owner_engine, tenant.company_id, requested_by="teste")
    # Lançadas sem arquivo nenhum (o lançador cria a pasta da tentativa, vazia).
    empty_success = await world.run(tenant, state="succeeded", files={})
    empty_failure = await world.run(tenant, state="failed", files={})
    # O executor caiu depois de guardar uma tela: o arquivo está na pasta, fora da lista.
    crashed = await world.run(tenant, state="failed", files={})
    stray = attempt_dir(world.store.root, tenant.company_id, crashed, 1) / "tela-perdida.png"
    stray.write_bytes(PNG)
    # Só um arquivo baixado: no prazo curto, nada da lista sai.
    download = await world.run(tenant, state="succeeded", files={"nota-001.pdf": ("download", PDF)})
    listed = await world.run(tenant, state="failed")  # a tela, o vídeo e o download
    ages = {
        queued[0]: 8,
        queued[1]: 91,
        empty_success: 8,
        empty_failure: 91,
        crashed: 91,
        download: 8,
        listed: 91,
    }
    for run_id, days in ages.items():
        await age_run(superuser_engine, run_id, days)
    ours = set(ages)
    store = _CountingStore(world.store)

    report = await Retention(dispatcher_db, store, RetentionPolicy(batch_size=50)).tick()

    assert set(store.deleted) & ours == ours - set(queued)  # a fila nunca teve pasta
    assert set(report.empty) & ours == ours - {listed}
    assert set(report.artifacts) & ours == {listed} and not set(report.captures) & ours
    for run_id, days in ages.items():
        row = await run_row(superuser_engine, run_id)
        assert (row["artifacts_expired_at"] is not None) is (days == 91), run_id
        assert (row["captures_expired_at"] is not None) is (days == 8), run_id
    assert not stray.exists() and _files(world, tenant, crashed) == []
    assert [path.name for path in _files(world, tenant, download)] == ["nota-001.pdf"]
    async with superuser_engine.connect() as conn:
        audited = (
            await conn.execute(
                text(
                    "SELECT action, target FROM audit_log WHERE company_id = :c "
                    "AND action LIKE 'execucao.%expirad%'"
                ),
                {"c": tenant.company_id},
            )
        ).all()
    assert [tuple(row) for row in audited] == [
        ("execucao.artefatos_expirados", f"execucao:{listed}")
    ]
    # Marcadas, não voltam: a volta seguinte não pega nem chama o store para elas.
    again = await Retention(dispatcher_db, store, RetentionPolicy(batch_size=50)).tick()
    assert not set(again.empty + again.artifacts + again.captures) & ours
    assert len([run_id for run_id in store.deleted if run_id in ours]) == len(ours) - 2


async def test_files_that_cannot_be_deleted_keep_the_run_for_the_next_round(
    world: EvidenceWorld, app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await _tenant(world, app_db, "Pasta presa")
    stuck = await world.run(tenant, state="succeeded")
    fine = await world.run(tenant, state="succeeded")
    for run_id in (stuck, fine):
        await age_run(superuser_engine, run_id, 9)
    flaky = Retention(dispatcher_db, _FlakyStore(world.store, stuck), RetentionPolicy())
    report = await flaky.tick()
    assert stuck in report.failed and fine in report.captures
    # Sem a marca e com os arquivos: a API continua servindo, e a próxima volta tenta de novo.
    assert (await run_row(superuser_engine, stuck))["captures_expired_at"] is None
    assert len(_files(world, tenant, stuck)) == 3
    later = await _retention(world, dispatcher_db).tick()
    assert stuck in later.captures
    assert [path.name for path in _files(world, tenant, stuck)] == ["nota-001.pdf"]


async def _snapshot(
    superuser: AsyncEngine, tenant: Tenant, run_id: UUID, attempt: int, days: float
) -> None:
    snapshot = {"error_code": "elemento_nao_encontrado", "step_id": "entrar"}
    async with superuser.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_snapshots (company_id, run_id, attempt, step_id, error_code, "
                "snapshot, snapshot_sha256, created_at) VALUES (:company, :run, :attempt, "
                "'entrar', 'elemento_nao_encontrado', CAST(:snapshot AS jsonb), :sha, "
                "now() - :age)"
            ),
            {
                "company": tenant.company_id,
                "run": run_id,
                "attempt": attempt,
                "snapshot": json.dumps(snapshot),
                "sha": "b" * 64,
                "age": timedelta(days=days),
            },
        )


async def _snapshots(superuser: AsyncEngine, run_id: UUID) -> list[int]:
    async with superuser.connect() as conn:
        return list(
            (
                await conn.scalars(
                    text("SELECT attempt FROM run_snapshots WHERE run_id = :run ORDER BY attempt"),
                    {"run": run_id},
                )
            ).all()
        )


async def test_failure_snapshots_and_expired_idempotency_keys(
    world: EvidenceWorld, app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    a = await _tenant(world, app_db, "Retratos A")
    b = await _tenant(world, app_db, "Retratos B")
    run_a = await world.run(a)
    run_b = await world.run(b)
    await _snapshot(superuser_engine, a, run_a, 1, 91)
    await _snapshot(superuser_engine, a, run_a, 2, 89)
    await _snapshot(superuser_engine, b, run_b, 1, 120)
    old = await world.platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave-velha"
    )
    live = await world.platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave-nova"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 hour' "
                "WHERE key = 'chave-velha'"
            )
        )

    report = await _retention(world, dispatcher_db).tick()

    # Os retratos com mais de 90 dias somem, das duas empresas; o mais novo fica.
    assert await _snapshots(superuser_engine, run_a) == [2]
    assert await _snapshots(superuser_engine, run_b) == []
    assert report.snapshots >= 2
    async with app_db.tenant_session(a.company_id) as session:
        actions = (await session.scalars(text("SELECT action FROM audit_log"))).all()
        keys = (await session.scalars(text("SELECT key FROM idempotency_keys ORDER BY key"))).all()
    assert actions.count("execucao.retrato_apagado") == 1
    # A chave vencida some; a que vale fica, e a de antes continua valendo do mesmo jeito.
    assert keys == ["chave-nova"] and report.idempotency_keys >= 1
    again = await world.platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave-nova"
    )
    assert again.run_id == live.run_id
    fresh = await world.platform.start(
        a.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave-velha"
    )
    assert fresh.run_id != old.run_id


async def _partitions(superuser: AsyncEngine) -> list[str]:
    async with superuser.connect() as conn:
        return list(
            (
                await conn.scalars(
                    text(
                        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                        "WHERE i.inhparent = 'run_events'::regclass ORDER BY c.relname"
                    )
                )
            ).all()
        )


async def test_old_event_partitions_are_dropped(
    world: EvidenceWorld,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    async with owner_engine.begin() as conn:
        for name, start, end in (
            ("run_events_2001_01", "2001-01-01", "2001-02-01"),
            ("run_events_2001_02", "2001-02-01", "2001-03-01"),
        ):
            await conn.execute(
                text(
                    f"CREATE TABLE {name} PARTITION OF run_events "
                    f"FOR VALUES FROM ('{start}') TO ('{end}')"
                )
            )
            await conn.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))
    current = f"run_events_{datetime.now(UTC):%Y_%m}"
    assert {"run_events_2001_01", "run_events_2001_02", current} <= set(
        await _partitions(superuser_engine)
    )
    # Com um prazo que ainda cobre fevereiro de 2001, só janeiro sai.
    days = (datetime.now(UTC) - datetime(2001, 2, 15, tzinfo=UTC)).days
    report = await _retention(world, dispatcher_db, policy={"events_days": days}).tick()
    left = await _partitions(superuser_engine)
    assert report.partitions == 1
    assert "run_events_2001_01" not in left and "run_events_2001_02" in left
    report = await _retention(world, dispatcher_db, policy={"events_days": 400}).tick()
    left = await _partitions(superuser_engine)
    assert report.partitions == 1 and "run_events_2001_02" not in left
    # As partições do mês atual e dos próximos ficam.
    assert current in left


async def test_dispatcher_and_app_cannot_delete_outside_the_functions(
    world: EvidenceWorld,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    pg_database: PgDatabase,
) -> None:
    tenant = await _tenant(world, app_db, "Travas")
    run_id = await world.run(tenant)
    await _snapshot(superuser_engine, tenant, run_id, 3, 200)
    current = f"run_events_{datetime.now(UTC):%Y_%m}"
    denied = "42501"
    for statements in (
        ("DELETE FROM run_snapshots",),
        ("DELETE FROM idempotency_keys",),
        ("SET LOCAL rapa.retention = 'run_snapshots'", "DELETE FROM run_snapshots"),
        (f"DROP TABLE {current}",),
        ("UPDATE run_snapshots SET step_id = 'outro'",),
    ):
        assert await _sqlstate(dispatcher_db, *statements) == denied, statements
    assert await _sqlstate(app_db, "DELETE FROM run_snapshots") == denied
    for call in (
        "SELECT * FROM rapa_purge_run_snapshots(1, 10)",
        "SELECT rapa_purge_idempotency_keys(10)",
        "SELECT rapa_drop_event_partitions(400)",
    ):
        assert await _sqlstate(app_db, call) == denied, call
    # Nem o dono apaga um retrato sem passar pela função (a trava de só inclusões vale),
    # e mesmo com a marca, um retrato com menos de um dia não sai.
    owner = Database(owner_engine)
    company = f"SELECT set_config('rapa.company_id', '{tenant.company_id}', true)"
    assert await _sqlstate(owner, company, "DELETE FROM run_snapshots") == "23514"
    await _snapshot(superuser_engine, tenant, run_id, 4, 0)
    assert (
        await _sqlstate(
            owner,
            company,
            "SELECT set_config('rapa.retention', 'run_snapshots', true)",
            "DELETE FROM run_snapshots WHERE attempt = 4",
        )
        == "23514"
    )
    # Parâmetros fora da regra são recusados pela própria função.
    for call in (
        "SELECT * FROM rapa_purge_run_snapshots(0, 10)",
        "SELECT * FROM rapa_purge_run_snapshots(1, 0)",
        "SELECT * FROM rapa_purge_run_snapshots(1, 5000)",
        "SELECT rapa_purge_idempotency_keys(NULL)",
        "SELECT rapa_drop_event_partitions(0)",
    ):
        assert await _sqlstate(dispatcher_db, call) == "22023", call
    # A marca só vale dentro da função: depois dela, a transação não fica marcada.
    async with dispatcher_db.dispatcher_session() as session:
        await session.execute(text("SELECT * FROM rapa_purge_run_snapshots(150, 10)"))
        left = await session.scalar(text("SELECT current_setting('rapa.retention', true)"))
    assert left in ("", None)
    assert await _snapshots(superuser_engine, run_id) == [4]
    assert pg_database.owner  # o dono das funções é o das tabelas


async def test_expired_mark_is_the_only_change_and_never_undone(
    world: EvidenceWorld, app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await _tenant(world, app_db, "Marca")
    expired = await world.run(tenant, state="succeeded")
    captures = await world.run(tenant, state="succeeded")
    fresh = await world.run(tenant, state="succeeded")
    queued = await world.platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    await age_run(superuser_engine, expired, 100)
    await age_run(superuser_engine, captures, 30)
    await _retention(world, dispatcher_db).tick()
    assert (await run_row(superuser_engine, expired))["artifacts_expired_at"] is not None
    assert (await run_row(superuser_engine, captures))["captures_expired_at"] is not None
    check = "23514"
    for statement, target in (
        ("UPDATE runs SET artifacts_expired_at = NULL WHERE id = :id", expired),
        ("UPDATE runs SET artifacts_expired_at = now() WHERE id = :id", expired),
        ("UPDATE runs SET artifacts = '[]' WHERE id = :id", expired),
        ("UPDATE runs SET captures_expired_at = NULL WHERE id = :id", captures),
        ("UPDATE runs SET captures_expired_at = now() WHERE id = :id", captures),
        # Depois da primeira marca, a lista de artefatos não muda mais.
        ("UPDATE runs SET artifacts = '[]' WHERE id = :id", captures),
        ("UPDATE runs SET artifacts_expired_at = now(), artifacts = '[]' WHERE id = :id", captures),
        # Marcar não muda mais nada na execução.
        ("UPDATE runs SET artifacts_expired_at = now(), error_message = 'x' WHERE id = :id", fresh),
        ("UPDATE runs SET captures_expired_at = now(), error_message = 'x' WHERE id = :id", fresh),
        # Só uma execução concluída pode ter a marca.
        ("UPDATE runs SET artifacts_expired_at = now() WHERE id = :id", queued.run_id),
        ("UPDATE runs SET captures_expired_at = now() WHERE id = :id", queued.run_id),
    ):
        state = await _sqlstate(dispatcher_db, statement, params={"id": target})
        assert state == check, statement
    # A outra marca, sozinha, ainda pode vir depois (o prazo das evidências).
    marked = "UPDATE runs SET artifacts_expired_at = now() WHERE id = :id"
    assert await _sqlstate(dispatcher_db, marked, params={"id": captures}) is None


# -- a etapa no laço do despachante ---------------------------------------------------------


class _Ticker:
    def __init__(self, *, behind: bool = False, broken: bool = False) -> None:
        self.calls: list[datetime] = []
        self.behind = behind
        self.broken = broken

    async def tick(self, now: datetime | None = None) -> object:
        assert now is not None
        self.calls.append(now)
        if self.broken:
            raise RuntimeError("banco fora do ar com dados sigilosos")
        return object()


async def test_retention_runs_hourly_in_the_dispatcher_loop() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    now = [start]
    ticker = _Ticker()
    dispatcher = Dispatcher(
        _BrokenStepQueue("nenhuma"),  # type: ignore[arg-type]
        _succeed,
        retention=ticker,
        clock=lambda: now[0],
    )

    async def at(seconds: float) -> None:
        now[0] = start + timedelta(seconds=seconds)
        await dispatcher.maintenance()

    await at(0)
    await at(3599)
    assert len(ticker.calls) == 1
    await at(3600)
    assert len(ticker.calls) == 2
    # Para trás: a próxima volta vem em um minuto, não em uma hora.
    ticker.behind = True
    await at(7200)
    await at(7259)
    assert len(ticker.calls) == 3
    await at(7260)
    assert len(ticker.calls) == 4
    # Uma falha é repetida com espera crescente e aparece na saúde.
    ticker.broken = True
    await at(7320)
    assert dispatcher.failing == {"retention": start + timedelta(seconds=7320)}
    calls = len(ticker.calls)
    await at(7320.5)
    assert len(ticker.calls) == calls
    await at(7321)
    assert len(ticker.calls) == calls + 1
    ticker.broken = ticker.behind = False
    await at(7330)
    assert dispatcher.failing == {}


async def test_retention_step_failure_does_not_block_dispatch() -> None:
    from tests.control.db.test_dispatcher import _job

    queue = _BrokenStepQueue("nenhuma", _job())
    ticker = _Ticker(broken=True)
    dispatcher = Dispatcher(
        queue,  # type: ignore[arg-type]
        _succeed,
        poll_interval=0.01,
        heartbeat_interval=0.01,
        retention=ticker,
    )
    stop = asyncio.Event()
    loop = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(10):
            while not queue.finished:
                assert not loop.done(), "o laço de despacho parou sozinho"
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await loop
    assert [r["state"] for r in queue.finished] == ["succeeded"]
    assert ticker.calls and "retention" in dispatcher.failing


def test_policy_has_a_minimum_of_one_day(tmp_path: Path) -> None:
    for changes in ({"success_days": 0}, {"failure_days": 0}, {"events_days": 0}):
        with pytest.raises(ValueError, match="1 a"):
            RetentionPolicy(**changes)
    with pytest.raises(ValueError):
        RetentionPolicy(batch_size=0)
    # O prazo das capturas não passa o das evidências: a etapa das evidências levaria as
    # capturas das execuções que deram certo antes do prazo delas.
    with pytest.raises(ValueError, match="não pode passar o das evidências"):
        RetentionPolicy(success_days=120, failure_days=90)
    same = RetentionPolicy(success_days=90, failure_days=90)
    assert (same.success_days, same.failure_days) == (90, 90)
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        Retention(None, store, max_tick_s=0)  # type: ignore[arg-type]


async def test_legacy_download_name_with_repeated_dots_stays_for_the_long_deadline(
    world: EvidenceWorld, app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Um download gravado antes do PR D pode ter ``..`` no nome (o saneador de antes
    deixava pontos seguidos): no prazo curto ele fica, como qualquer download."""
    tenant = await _tenant(world, app_db, "Nome antigo")
    run_id = await world.run(tenant, state="succeeded")
    folder = attempt_dir(world.store.root, tenant.company_id, run_id, 1)
    legacy = "nota..fiscal.pdf"
    (folder / "nota-001.pdf").rename(folder / legacy)
    # A forma de ``runs.artifacts`` depois da 0008 numa execução de antes (sem caminho,
    # sem tentativa e sem data), com o nome que o saneador de antes deixava.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET artifacts = (SELECT jsonb_agg(CASE WHEN a->>'kind' = 'download' "
                "THEN jsonb_set(a - 'attempt' - 'created_at', '{name}', to_jsonb(CAST(:name AS text))) "
                "ELSE a - 'attempt' - 'created_at' END) FROM jsonb_array_elements(artifacts) AS a) "
                "WHERE id = :id"
            ),
            {"name": legacy, "id": run_id},
        )
    await age_run(superuser_engine, run_id, 10)

    report = await _retention(world, dispatcher_db).tick()

    assert run_id in report.captures and report.failed == []
    assert [path.name for path in _files(world, tenant, run_id)] == [legacy]
    row = await run_row(superuser_engine, run_id)
    assert row["captures_expired_at"] is not None and row["artifacts_expired_at"] is None
    # Passado o prazo das evidências, sai também.
    await age_run(superuser_engine, run_id, 91)
    later = await _retention(world, dispatcher_db).tick()
    assert run_id in later.artifacts
    assert _files(world, tenant, run_id) == []


async def test_dropping_a_partition_never_holds_run_events_while_checking_the_next(
    world: EvidenceWorld,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    """O DROP de uma partição trava ``run_events`` (exclusiva) até o fim da transação; a
    conferência da partição seguinte (que pode ler a partição inteira) não pode rodar
    com essa trava, ou a ingestão e a leitura de eventos param. Uma partição por vez."""
    olds = (
        ("run_events_2002_01", "2002-01-01", "2002-02-01"),
        ("run_events_2002_02", "2002-02-01", "2002-03-01"),
    )
    async with owner_engine.begin() as conn:
        for name, start, end in olds:
            await conn.execute(
                text(
                    f"CREATE TABLE {name} PARTITION OF run_events "
                    f"FOR VALUES FROM ('{start}') TO ('{end}')"
                )
            )
    retention = _retention(world, dispatcher_db, lock_timeout=timedelta(seconds=30))
    # A conferência da segunda partição fica presa numa trava dela (como uma leitura longa).
    holder = await superuser_engine.connect()
    await holder.begin()
    await holder.execute(text("LOCK TABLE run_events_2002_02 IN ACCESS EXCLUSIVE MODE"))
    tick = asyncio.create_task(retention.tick())
    try:
        for _ in range(200):
            async with superuser_engine.connect() as conn:
                waiting = await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE NOT granted "
                        "AND relation = 'run_events_2002_02'::regclass"
                    )
                )
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting, "a retenção não chegou à segunda partição"
        # Enquanto isso, os eventos de agora continuam sendo lidos (e gravados). O limite
        # vai como constante: a consulta só abre (e trava) as partições de agora.
        since = f"{datetime.now(UTC) - timedelta(days=1):%Y-%m-%d %H:%M:%S}+00"
        async with superuser_engine.connect() as probe:
            await probe.execute(text("SET lock_timeout = '1s'"))
            query = f"SELECT count(*) FROM run_events WHERE at >= '{since}'"  # noqa: S608
            await probe.execute(text(query))
    finally:
        await holder.rollback()
        await holder.close()
        report = await tick
    assert report.partitions == 2
    assert not {name for name, *_ in olds} & set(await _partitions(superuser_engine))


async def test_expired_key_renewed_while_the_purge_waits_stays(
    world: EvidenceWorld,
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _tenant(world, app_db, "Chave renovada")
    first = await world.platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE idempotency_keys SET expires_at = now() - interval '1 hour'")
        )
    # O mesmo pedido depois do prazo: enqueue_run trava a chave vencida e a renova.
    locked = asyncio.Event()
    release = asyncio.Event()

    async def paused(*args: Any, **kwargs: Any) -> Any:
        locked.set()
        await release.wait()
        return await ensure_a1_ready(*args, **kwargs)

    monkeypatch.setattr("rapa.control.services.runs.ensure_a1_ready", paused)
    renewing = asyncio.create_task(
        world.platform.start(
            tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave"
        )
    )
    await locked.wait()
    purging = asyncio.create_task(_retention(world, dispatcher_db).tick())
    waiting = 0
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with superuser_engine.connect() as conn:
            waiting = int(
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query LIKE 'SELECT rapa_purge_idempotency_keys%'"
                    )
                )
                or 0
            )
        if waiting:
            break
    assert waiting, "a exclusão das chaves vencidas não chegou a esperar a trava"
    release.set()
    second = await renewing
    await purging
    monkeypatch.undo()

    # A chave renovada vale de novo: a exclusão que esperava a trava não a apaga, e a
    # repetição do pedido devolve a mesma execução (nada roda duas vezes).
    assert second.run_id != first.run_id
    async with superuser_engine.connect() as conn:
        keys = (
            await conn.execute(
                text(
                    "SELECT run_id, expires_at > now() AS live FROM idempotency_keys "
                    "WHERE company_id = :company"
                ),
                {"company": tenant.company_id},
            )
        ).all()
    assert [(row.run_id, row.live) for row in keys] == [(second.run_id, True)]
    retry = await world.platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST, "chave"
    )
    assert retry.run_id == second.run_id
