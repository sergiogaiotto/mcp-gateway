"""A operação por empresa junto com as outras partes: o certificado A1 e as evidências.

Congelar recusa também o que o A1 cria (registrar, renovar, autorizar, aprovar), mas
deixa revogar e ler; a retenção e os avisos de vencimento pulam a empresa congelada; o
despachante não pega a execução dela (nem chega a conferir o certificado); e exportar e
apagar levam as tabelas e os arquivos das duas partes, sem deixar nenhuma linha.
"""

import hashlib
import json
import tarfile
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import (
    CredentialAlerts,
    Dispatcher,
    PgArtifacts,
    PgRunQueue,
    Retention,
    RetentionPolicy,
)
from rapa.control.services.lifecycle import export_company, purge_company, unfreeze_company
from rapa.core.contracts import CompanyFrozen, CredentialStatus, RunnerJob, RunTrigger
from rapa.core.robot import CredentialKind, Manifest
from tests.control.credentials.support import (
    CERT,
    ORIGIN,
    OTHER_FINGERPRINT,
    A1World,
    a1_world,
    in_days,
    publish_a1,
    ready_a1,
)
from tests.control.db.support import PROTOCOLO_INPUTS, protocolo, publish, reset_queue, result
from tests.control.evidence.support import PDF, PNG, EvidenceWorld, age_run, run_row

from .support import copy_row, freeze

pytestmark = pytest.mark.database

A1_SLUG = "emitir_protocolo"
#: Um robô sem certificado na mesma empresa (para as evidências da empresa congelada).
PLAIN_SLUG = "protocolo_sem_certificado"
#: As tabelas do certificado A1 (revisão 0006) em que o banco recusa a linha nova da
#: empresa congelada.
A1_GUARDED = ("credentials", "a1_authorizations", "version_approvals")

#: Toda tabela com company_id, pelo information_schema (partições incluídas): uma tabela
#: nova entra sozinha, sem lista para esquecer.
_COMPANY_TABLES = text(
    "SELECT c.table_name FROM information_schema.columns c "
    "JOIN information_schema.tables t "
    "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
    "WHERE c.table_schema = 'public' AND c.column_name = 'company_id' "
    "AND t.table_type = 'BASE TABLE' ORDER BY c.table_name"
)


async def _rows(superuser: AsyncEngine, company: UUID) -> dict[str, int]:
    """Linhas da empresa em cada tabela com company_id (e a linha dela em companies)."""
    async with superuser.connect() as conn:
        tables = list((await conn.scalars(_COMPANY_TABLES)).all())
        counts = {
            table: int(
                await conn.scalar(
                    text(f'SELECT count(*) FROM "{table}" WHERE company_id = :c'),  # noqa: S608
                    {"c": company},
                )
                or 0
            )
            for table in tables
        }
        counts["companies"] = int(
            await conn.scalar(text("SELECT count(*) FROM companies WHERE id = :c"), {"c": company})
            or 0
        )
    return counts


def _plain() -> Manifest:
    data = protocolo().model_dump(mode="json")
    data["name"] = PLAIN_SLUG
    return Manifest.model_validate(data)


async def _snapshot(superuser: AsyncEngine, company: UUID, run_id: UUID, days: float) -> None:
    """Um retrato da falha gravado há ``days`` dias."""
    async with superuser.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_snapshots (company_id, run_id, attempt, step_id, error_code, "
                "snapshot, snapshot_sha256, created_at) VALUES (:company, :run, 1, 'entrar', "
                "'elemento_nao_encontrado', CAST(:snapshot AS jsonb), :sha, now() - :age)"
            ),
            {
                "company": company,
                "run": run_id,
                "snapshot": json.dumps({"error_code": "elemento_nao_encontrado"}),
                "sha": "b" * 64,
                "age": timedelta(days=days),
            },
        )


async def _expired_key(world: A1World, slug: str, superuser: AsyncEngine) -> None:
    """Uma chave de idempotência da empresa, já vencida (a execução dela, cancelada)."""
    tenant = world.tenant
    run = await world.platform.start(
        tenant.operator, slug, PROTOCOLO_INPUTS, RunTrigger.REST, f"chave-{tenant.company_id}"
    )
    async with superuser.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET state = 'cancelled', finished_at = now() WHERE id = :r"),
            {"r": run.run_id},
        )
        await conn.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = now() - interval '1 hour' "
                "WHERE company_id = :c"
            ),
            {"c": tenant.company_id},
        )


async def _count(superuser: AsyncEngine, sql: str, **params: Any) -> int:
    async with superuser.connect() as conn:
        return int(await conn.scalar(text(sql), params) or 0)


# -- exportar e apagar ------------------------------------------------------------------


async def test_export_and_purge_cover_the_a1_and_evidence_tables(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Credenciais, autorizações, aprovações, avisos, artefatos, retratos e as marcas da
    retenção: a exportação leva tudo (sem material de chave) e apagar não deixa nenhuma
    linha da empresa em nenhuma tabela com company_id, sem tocar a outra."""
    await reset_queue(superuser_engine, total_slots=20)
    name = "Saída com A1 e evidências"
    a = await a1_world(app_db, name)
    b = await a1_world(app_db, "Fica com A1 e evidências")
    for world in (a, b):
        await publish_a1(world)
    root = tmp_path / "artefatos"
    evidence = EvidenceWorld(app_db, dispatcher_db, root)
    old_failure = await evidence.run(a.tenant)
    old_success = await evidence.run(
        a.tenant,
        state="succeeded",
        files={"tela-001.png": ("screenshot", PNG), "nota-001.pdf": ("download", PDF)},
    )
    fresh = await evidence.run(a.tenant)
    other = await evidence.run(b.tenant)
    await _snapshot(superuser_engine, a.tenant.company_id, fresh, 0)
    # A retenção marca as duas execuções antigas (uma perde tudo, a outra só as telas).
    await age_run(superuser_engine, old_failure, 91)
    await age_run(superuser_engine, old_success, 8)
    report = await Retention(dispatcher_db, evidence.store, RetentionPolicy()).tick()
    assert old_failure in report.artifacts and old_success in report.captures
    assert (await run_row(superuser_engine, old_failure))["artifacts_expired_at"] is not None
    assert (await run_row(superuser_engine, old_success))["captures_expired_at"] is not None
    # E o aviso de vencimento do certificado (vence em um ano: dentro de 400 dias).
    await CredentialAlerts(dispatcher_db, warn_days=400).check()
    company = a.tenant.company_id
    before = await _rows(superuser_engine, company)
    for table in (*A1_GUARDED, "credential_alerts", "run_snapshots", "runs", "audit_log"):
        assert before[table] > 0, table

    # Exportar: toda tabela com company_id (fora as partições, lidas pela tabela-mãe).
    output = tmp_path / "empresa.tar.gz"
    exported = await export_company(owner_engine, company, output, artifacts_dir=root)
    with tarfile.open(output, "r:gz") as tar:
        contents = {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }
    manifest = json.loads(contents["manifesto.json"])
    tables = {entry["tabela"]: entry for entry in manifest["tabelas"]}
    async with superuser_engine.connect() as conn:
        partitions = set(
            (
                await conn.scalars(
                    text(
                        "SELECT c.relname FROM pg_inherits i JOIN pg_class c "
                        "ON c.oid = i.inhrelid WHERE i.inhparent = 'run_events'::regclass"
                    )
                )
            ).all()
        )
    assert set(tables) == set(before) - partitions
    for table in (*A1_GUARDED, "credential_alerts", "run_snapshots", "runs"):
        assert tables[table]["linhas"] == before[table] == exported.tables[table], table
        assert tables[table]["colunas_sem_valor"] == [], table
        body = contents[tables[table]["arquivo"]]
        assert hashlib.sha256(body).hexdigest() == tables[table]["sha256"], table
    exported_runs = {
        row["id"]: row
        for row in map(json.loads, contents["tabelas/runs.jsonl"].decode().splitlines())
    }
    assert exported_runs[str(old_failure)]["artifacts_expired_at"] is not None
    assert exported_runs[str(old_success)]["captures_expired_at"] is not None
    # Do certificado sai só a referência ao cofre e os metadados (o valor nunca passa
    # pelo RaPA); nenhuma coluna é material de chave.
    credential = json.loads(contents["tabelas/credentials.jsonl"].decode().splitlines()[0])
    assert credential["kind"] == "pfx_a1" and credential["fingerprint_sha256"]
    # Os arquivos que ainda existem: os da execução nova e o download da que deu certo.
    files = {entry["arquivo"] for entry in manifest["arquivos"]}
    assert files == {
        f"artefatos/{fresh}/1/tela-001-entrar-falha.png",
        f"artefatos/{fresh}/1/video-falha.webm",
        f"artefatos/{fresh}/1/nota-001.pdf",
        f"artefatos/{old_success}/1/nota-001.pdf",
    }
    assert str(b.tenant.company_id).encode() not in b"".join(contents.values())

    # Apagar: só congelada; depois, nenhuma linha da empresa em nenhuma tabela.
    await freeze(owner_engine, company)
    other_before = await _rows(superuser_engine, b.tenant.company_id)
    # Sem o OpenBao (os segredos do A1 não estão nele): quem pede precisa dizer.
    purged = await purge_company(
        owner_engine, company, confirm_name=name, artifacts_dir=root, secrets_outside_vault=True
    )
    assert purged.tables["a1_authorizations"] == before["a1_authorizations"]
    after = await _rows(superuser_engine, company)
    assert set(after) == set(before)
    assert set(after.values()) == {0}, {table: n for table, n in after.items() if n}
    assert not (root / str(company)).exists()
    # A outra empresa continua igual, com as evidências dela.
    assert await _rows(superuser_engine, b.tenant.company_id) == other_before
    artifacts = PgArtifacts(app_db, evidence.store)
    listed = await artifacts.list_artifacts(b.tenant.operator, other)
    assert {artifact.name for artifact in listed} == {
        "tela-001-entrar-falha.png",
        "video-falha.webm",
        "nota-001.pdf",
    }
    content = await artifacts.open_artifact(b.tenant.operator, other, "nota-001.pdf")
    assert await content.read() == PDF


async def _guarded_delete(
    engine: AsyncEngine, table: str, tenant: UUID, marker: UUID | None
) -> str | None:
    """Tenta apagar as linhas da empresa em ``table`` (e desfaz); devolve o SQLSTATE."""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('rapa.company_id', :c, true)"), {"c": str(tenant)}
            )
            if marker is not None:
                await conn.execute(
                    text("SELECT set_config('rapa.purge_company', :c, true)"), {"c": str(marker)}
                )
            deleted = await conn.execute(
                text(f"DELETE FROM {table} WHERE company_id = :c"),  # noqa: S608
                {"c": tenant},
            )
            assert deleted.rowcount > 0, table
            raise RuntimeError("rollback")  # nunca confirma: só interessa se foi aceito
    except RuntimeError:
        return None
    except DBAPIError as exc:
        return getattr(exc.orig, "sqlstate", None)


@pytest.mark.parametrize(
    "table", ["a1_authorizations", "version_approvals", "credential_alerts", "run_snapshots"]
)
async def test_guards_of_the_parts_accept_only_the_owner_purge(
    table: str,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """As travas do A1 e dos retratos aceitam apagar só pelo dono, com a marca da empresa."""
    await reset_queue(superuser_engine, total_slots=20)
    a = await a1_world(app_db, "Travas A")
    b = await a1_world(app_db, "Travas B")
    await publish_a1(a)
    await CredentialAlerts(dispatcher_db, warn_days=400).check()
    evidence = EvidenceWorld(app_db, dispatcher_db, tmp_path / "artefatos")
    run_id = await evidence.run(a.tenant)
    await _snapshot(superuser_engine, a.tenant.company_id, run_id, 0)
    company = a.tenant.company_id
    # O dono sem a marca ou com a de outra empresa, e o superusuário com a marca (não é o
    # dono): a trava recusa. A aplicação e o despachante nem têm a permissão.
    assert await _guarded_delete(owner_engine, table, company, None) == "23514"
    assert await _guarded_delete(owner_engine, table, company, b.tenant.company_id) == "23514"
    assert await _guarded_delete(superuser_engine, table, company, company) == "23514"
    assert await _guarded_delete(app_db.engine, table, company, company) == "42501"
    assert await _guarded_delete(dispatcher_db.engine, table, company, company) == "42501"
    # Só o dono com a marca da própria empresa passa (e foi desfeito).
    assert await _guarded_delete(owner_engine, table, company, company) is None
    assert (await _rows(superuser_engine, company))[table] > 0


# -- empresa congelada ------------------------------------------------------------------


async def test_frozen_company_refuses_new_a1_writes_but_revoking_and_reading_stay(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    await reset_queue(superuser_engine, total_slots=20)
    world = await a1_world(app_db, "Congelada com A1")
    tenant = world.tenant
    version = await ready_a1(world)
    first = await world.platform.publish(tenant.approver, version.robot_id, version.version_no)
    assert first.approvals == [tenant.approver.subject] and first.approvals_required == 2
    await publish(world.platform, tenant, _plain())
    evidence = EvidenceWorld(app_db, dispatcher_db, tmp_path / "artefatos")
    run_id = await evidence.run(tenant, slug=PLAIN_SLUG)
    [credential] = await world.credentials.list(world.area_admin)
    [authorization] = await world.credentials.list_authorizations(world.custodian, version.robot_id)
    # Ativa, a cópia de uma linha só esbarra na chave (nenhuma trava recusa antes).
    for table in A1_GUARDED:
        assert (await copy_row(app_db, tenant.company_id, table))[0] == "23505", table

    await freeze(owner_engine, tenant.company_id)

    # O banco recusa a linha nova nas tabelas do A1, mesmo sem os serviços.
    for table in A1_GUARDED:
        code, message = await copy_row(app_db, tenant.company_id, table)
        assert code == "23514" and "a empresa está congelada" in message, table
    # Registrar, renovar, autorizar e aprovar: nada novo.
    with pytest.raises(CompanyFrozen):
        await world.credentials.register(
            world.area_admin,
            tenant.area_id,
            name="senha_do_portal",
            kind=CredentialKind.PASSWORD,
            origin=ORIGIN,
            vault_path="empresas/portal/senha",
        )
    with pytest.raises(CompanyFrozen):
        await world.credentials.renew(
            world.custodian,
            credential.credential_id,
            not_after=in_days(400),
            fingerprint=OTHER_FINGERPRINT,
        )
    with pytest.raises(CompanyFrozen):
        await world.credentials.authorize(world.custodian, version.robot_id, CERT)
    with pytest.raises(CompanyFrozen):
        await world.platform.publish(world.approver2, version.robot_id, version.version_no)
    # Ler continua: as credenciais, as aprovações e as evidências.
    approvals = await world.credentials.approvals(
        tenant.approver, version.robot_id, version.version_no
    )
    assert [a.approved_by for a in approvals.approvals] == [tenant.approver.subject]
    assert approvals.missing == 1
    artifacts = PgArtifacts(app_db, evidence.store)
    assert len(await artifacts.list_artifacts(tenant.operator, run_id)) == 3
    content = await artifacts.open_artifact(tenant.operator, run_id, "tela-001-entrar-falha.png")
    assert await content.read() == PNG
    # Revogar só tira acesso: continua valendo.
    revoked = await world.credentials.revoke_authorization(
        world.custodian, authorization.authorization_id
    )
    assert revoked.revoked_at is not None
    gone = await world.credentials.revoke(world.area_admin, credential.credential_id)
    assert gone.status is CredentialStatus.REVOKED
    # Nada novo foi criado nas tabelas do A1 depois do congelamento.
    rows = await _rows(superuser_engine, tenant.company_id)
    assert (rows["credentials"], rows["a1_authorizations"], rows["version_approvals"]) == (
        1,
        1,
        2,  # a primeira aprovação do A1 e a do robô sem certificado
    )


async def test_retention_and_expiry_alerts_skip_a_frozen_company(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Na saída da empresa, as evidências ficam para a exportação: nada dela expira."""
    await reset_queue(superuser_engine, total_slots=20)
    frozen = await a1_world(app_db, "Retenção congelada")
    active = await a1_world(app_db, "Retenção ativa")
    evidence = EvidenceWorld(app_db, dispatcher_db, tmp_path / "artefatos")
    runs: dict[UUID, UUID] = {}
    for world in (frozen, active):
        company = world.tenant.company_id
        # Um certificado que vence em 10 dias (o aviso) e um robô sem certificado.
        await world.credentials.register(
            world.area_admin,
            world.tenant.area_id,
            name=CERT,
            kind=CredentialKind.PFX_A1,
            origin=ORIGIN,
            vault_path=f"empresas/{company}/certificado-a1",
            custodian_subject=world.custodian.subject,
            not_after=in_days(10),
        )
        await publish(world.platform, world.tenant, _plain())
        runs[company] = await evidence.run(world.tenant, slug=PLAIN_SLUG)
        await age_run(superuser_engine, runs[company], 91)
        await _snapshot(superuser_engine, company, runs[company], 91)
        await _expired_key(world, PLAIN_SLUG, superuser_engine)
    company = frozen.tenant.company_id
    partition = "run_events_1999_03"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TABLE {partition} PARTITION OF run_events "
                "FOR VALUES FROM ('1999-03-01') TO ('1999-04-01')"
            )
        )
        await conn.execute(text(f"ALTER TABLE {partition} ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text(f"ALTER TABLE {partition} FORCE ROW LEVEL SECURITY"))
    try:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO run_events (company_id, run_id, attempt, seq, at, kind) "
                    "VALUES (:c, :r, 1, 9999, '1999-03-15', 'antigo')"
                ),
                {"c": company, "r": runs[company]},
            )
        await freeze(owner_engine, company)
        retention = Retention(dispatcher_db, evidence.store, RetentionPolicy(events_days=400))
        report = await retention.tick()
        alerts = CredentialAlerts(dispatcher_db, warn_days=30)
        await alerts.check()

        def counts_sql(table: str) -> str:
            return f"SELECT count(*) FROM {table} WHERE company_id = :c"  # noqa: S608

        # A ativa perde o que passou do prazo e recebe o aviso.
        other = active.tenant.company_id
        assert runs[other] in report.artifacts
        for table in ("run_snapshots", "idempotency_keys"):
            assert await _count(superuser_engine, counts_sql(table), c=other) == 0, table
        assert await _count(superuser_engine, counts_sql("credential_alerts"), c=other) == 1
        # A congelada fica como estava: artefatos, retrato, chave, a partição e sem aviso.
        assert runs[company] not in report.artifacts
        row = await run_row(superuser_engine, runs[company])
        assert row["artifacts_expired_at"] is None
        listed = await PgArtifacts(app_db, evidence.store).list_artifacts(
            frozen.tenant.operator, runs[company]
        )
        assert len(listed) == 3
        for table in ("run_snapshots", "idempotency_keys"):
            assert await _count(superuser_engine, counts_sql(table), c=company) == 1, table
        assert await _count(superuser_engine, counts_sql("credential_alerts"), c=company) == 0
        assert await _count(superuser_engine, f"SELECT count(*) FROM {partition}") == 1  # noqa: S608
        assert report.partitions == 0

        # Descongelada, tudo segue o prazo de novo.
        await unfreeze_company(owner_engine, company, requested_by="teste")
        report = await retention.tick()
        assert runs[company] in report.artifacts and report.partitions == 1
        for table in ("run_snapshots", "idempotency_keys"):
            assert await _count(superuser_engine, counts_sql(table), c=company) == 0, table
        await alerts.check()
        assert await _count(superuser_engine, counts_sql("credential_alerts"), c=company) == 1
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {partition}"))


# -- o despachante: congelada antes do certificado --------------------------------------


class _Launcher:
    def __init__(self) -> None:
        self.launched: list[UUID] = []

    async def __call__(self, job: RunnerJob) -> Mapping[str, Any]:
        self.launched.append(job.run_id)
        return result(job.run_id, job.attempt)


async def test_a_frozen_company_run_is_not_even_claimed_for_the_a1_check(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    """A ordem do despachante: empresa congelada não é pega (nem chega a conferir o
    certificado); descongelada, a conferência do A1 vale e a execução falha sem lançar."""
    await reset_queue(superuser_engine, total_slots=20)
    world = await a1_world(app_db, "Fila congelada com A1")
    other = await a1_world(app_db, "Fila ativa")
    await publish_a1(world)
    await publish(other.platform, other.tenant, _plain())
    queued = await world.platform.start(
        world.tenant.operator, A1_SLUG, PROTOCOLO_INPUTS, RunTrigger.REST
    )
    [credential] = await world.credentials.list(world.area_admin)
    # Depois de a execução entrar na fila: o certificado é revogado e a empresa fica
    # congelada sem cancelar a fila (o caso de uma linha que escapou do cancelamento).
    await world.credentials.revoke(world.area_admin, credential.credential_id)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE companies SET status = 'frozen', status_reason = 'teste', "
                "status_changed_at = now() WHERE id = :c"
            ),
            {"c": world.tenant.company_id},
        )
    launcher = _Launcher()
    dispatcher = Dispatcher(PgRunQueue(dispatcher_db), launcher, concurrency=4)
    assert await dispatcher.run_once() == 0
    await dispatcher.drain()
    status = await world.platform.get(world.tenant.operator, queued.run_id)
    assert status is not None
    assert (status.state.value, status.error_code) == ("queued", None)
    # A outra empresa segue sendo atendida.
    started = await other.platform.start(
        other.tenant.operator, PLAIN_SLUG, PROTOCOLO_INPUTS, RunTrigger.REST
    )
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    assert launcher.launched == [started.run_id]
    # Descongelada: pega, confere o certificado (revogado) e termina sem lançar.
    await unfreeze_company(owner_engine, world.tenant.company_id, requested_by="teste")
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    status = await world.platform.get(world.tenant.operator, queued.run_id)
    assert status is not None
    assert (status.state.value, status.error_code) == ("failed", "certificado_a1_bloqueado")
    assert launcher.launched == [started.run_id]
