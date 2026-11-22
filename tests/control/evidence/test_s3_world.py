"""O store de objetos com o banco de verdade: o despachante cifra e guarda, a API lê de
volta, a retenção destrói a chave depois dos objetos, ``run_keys`` só aceita inclusões, e
a saída da empresa exporta decifrado e apaga os objetos, os segredos e a chave transit."""

import asyncio
import io
import json
import tarfile
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.artifacts import ArtifactMissing, S3ArtifactStore, attempt_dir, object_key
from rapa.control.db import Database
from rapa.control.keys import KeyUnavailable, PgRunKeys, TransitCompanyKeys
from rapa.control.services import (
    Dispatcher,
    PgArtifacts,
    PgProvisioning,
    PgRunQueue,
    Retention,
    RetentionPolicy,
)
from rapa.control.services.lifecycle import (
    LocalKeyDestroyer,
    export_company,
    purge_company,
    unfreeze_company,
)
from rapa.control.vault import OpenBaoClient, OpenBaoKeyDestroyer
from rapa.core.contracts import (
    InvalidInput,
    NotFound,
    RunnerJob,
    RunTrigger,
    ServiceUnavailable,
)
from rapa.core.ids import new_id
from rapa.core.robot import CredentialKind, CredentialRef
from tests.control.company.support import audit_actions, company_counts, freeze
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    Tenant,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
)
from tests.control.evidence.support import (
    FAILURE_FILES,
    PDF,
    PNG,
    EvidenceWorld,
    age_run,
    run_row,
)
from tests.control.vault.fakebao import API_ROLE, OPERATION_ROLE, ROLE, ROLES, FakeBao
from tests.control.vault.support import bao_client

from .s3support import BUCKET, Overridden, bucket_objects, fake_s3, s3_store, stage

pytestmark = pytest.mark.database


class S3World:
    """Execuções que terminam pelo despachante de verdade, com o store de objetos.

    Cada papel fala com o OpenBao falso com a própria política: o despachante embrulha
    (e cria a chave da empresa), a API só desembrulha e a operação exporta e apaga.
    """

    def __init__(
        self,
        app_db: Database,
        dispatcher_db: Database,
        staging: Path,
        s3: Any,
        bao: FakeBao,
        clients: dict[str, OpenBaoClient],
    ) -> None:
        self.evidence = EvidenceWorld(app_db, dispatcher_db, staging)
        self.s3 = s3
        self.bao = bao
        self.client = clients[OPERATION_ROLE]
        self.store = s3_store(
            s3,
            staging,
            run_keys=PgRunKeys(dispatcher_db),
            keys=TransitCompanyKeys(clients[ROLE]),
        )
        self.api_store = s3_store(
            s3, staging, run_keys=PgRunKeys(app_db), keys=TransitCompanyKeys(clients[API_ROLE])
        )
        self.evidence.store = self.store  # type: ignore[assignment]
        self.evidence.dispatcher = Dispatcher(
            PgRunQueue(dispatcher_db),
            self.evidence.launcher,
            concurrency=4,
            artifacts=self.store,
            artifact_retry_s=0.01,
        )
        self.artifacts = PgArtifacts(app_db, self.api_store)

    async def tenant(self, app_db: Database, name: str) -> Tenant:
        tenant = await make_tenant(app_db, name)
        await publish(self.evidence.platform, tenant, protocolo())
        return tenant

    async def run(self, tenant: Tenant, **kwargs: Any) -> UUID:
        return await self.evidence.run(tenant, **kwargs)

    def owner_store(self, owner_engine: AsyncEngine) -> S3ArtifactStore:
        return s3_store(
            self.s3,
            self.store.staging_root,
            run_keys=PgRunKeys(Database(owner_engine)),
            keys=TransitCompanyKeys(self.client),
        )


@pytest.fixture
def s3() -> Iterator[Any]:
    with fake_s3() as client:
        yield client


@pytest.fixture
async def world(
    app_db: Database,
    dispatcher_db: Database,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
    s3: Any,
) -> AsyncIterator[S3World]:
    await reset_queue(superuser_engine, total_slots=20)
    bao = FakeBao()
    clients = {role: bao_client(bao, tmp_path, role=role) for role in ROLES}
    try:
        yield S3World(app_db, dispatcher_db, tmp_path / "passagem", s3, bao, clients)
    finally:
        for client in clients.values():
            await client.aclose()
    assert bao.refusals == []  # cada papel só pediu o que a política dele deixa


async def _keys_of(superuser: AsyncEngine, company_id: UUID) -> dict[UUID, bytes]:
    async with superuser.connect() as conn:
        rows = await conn.execute(
            text("SELECT run_id, wrapped_key FROM run_keys WHERE company_id = :c"),
            {"c": company_id},
        )
        return {row[0]: bytes(row[1]) for row in rows}


async def test_the_dispatcher_encrypts_and_the_api_reads_back(
    world: S3World, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await world.tenant(app_db, "Objetos A")
    run_id = await world.run(tenant)
    listed = await world.artifacts.list_artifacts(tenant.operator, run_id)
    assert {item.name for item in listed} == set(FAILURE_FILES)
    for name, (_, data) in FAILURE_FILES.items():
        content = await world.artifacts.open_artifact(tenant.operator, run_id, name)
        assert content.size == len(data)
        assert await content.read() == data
    # No bucket, só cifrado; na pasta de passagem, nada sobrou.
    objects = bucket_objects(world.s3)
    assert set(objects) == {
        object_key(tenant.company_id, run_id, 1, name) for name in FAILURE_FILES
    }
    for name, (_, data) in FAILURE_FILES.items():
        assert data[8:40] not in objects[object_key(tenant.company_id, run_id, 1, name)]
    assert not any(path.is_file() for path in world.store.staging_root.rglob("*"))
    # Uma chave por execução, embrulhada pela chave transit da empresa (nunca exportável).
    keys = await _keys_of(superuser_engine, tenant.company_id)
    assert list(keys) == [run_id] and keys[run_id].startswith(b"vault:v1:")
    company_key = world.bao.keys[f"empresa-{tenant.company_id}"]
    assert company_key.exportable is False and company_key.plaintext_backup is False
    # Outra empresa não vê a execução (nem as chaves dela).
    other = await world.tenant(app_db, "Objetos B")
    assert await _keys_of(superuser_engine, other.company_id) == {}
    assert await PgRunKeys(app_db).get(other.company_id, run_id) is None


def _staged_files(world: S3World) -> list[Path]:
    return [path for path in world.store.staging_root.rglob("*") if not path.is_dir()]


async def test_registration_leaves_no_plaintext_in_the_staging_folder(
    world: S3World, app_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Nem o que o executor deixou sem informar, nem o que o store recusou: a pasta de
    passagem da tentativa sai inteira depois do registro."""
    tenant = await world.tenant(app_db, "Passagem limpa")
    outside = tmp_path / "fora.pdf"
    outside.write_bytes(PDF)
    run_id = await world.run(
        tenant,
        files={**FAILURE_FILES, "nota-002.pdf": ("Tipo Errado", PDF)},
        unreported={"baixado-sem-aviso.pdf": PDF, "tela-sem-aviso.png": PNG},
        outside={"fora.pdf": outside},
    )
    row = await run_row(superuser_engine, run_id)
    assert {item["name"] for item in row["artifacts"]} == set(FAILURE_FILES)
    assert _staged_files(world) == []
    assert not attempt_dir(world.store.staging_root, tenant.company_id, run_id, 1).exists()
    assert outside.read_bytes() == PDF  # o de fora nunca é tocado


class _FlakyKeys:
    """A chave da empresa que não embrulha nas primeiras vezes (o cofre caiu um pouco)."""

    def __init__(self, inner: Any, failures: int) -> None:
        self.inner = inner
        self.failures = failures

    async def wrap(self, *args: Any) -> Any:
        if self.failures:
            self.failures -= 1
            raise KeyUnavailable("o OpenBao não respondeu")
        return await self.inner.wrap(*args)

    async def unwrap(self, *args: Any) -> Any:
        return await self.inner.unwrap(*args)


@pytest.mark.parametrize("down", ["s3", "cofre"])
async def test_a_short_outage_does_not_lose_the_evidence(
    world: S3World, app_db: Database, superuser_engine: AsyncEngine, down: str
) -> None:
    tenant = await world.tenant(app_db, f"Queda curta {down}")
    if down == "s3":
        failures = [2]

        def upload(*args: Any, **kwargs: Any) -> Any:
            if failures[0]:
                failures[0] -= 1
                raise ConnectionError("S3 fora do ar")
            return world.s3.upload_fileobj(*args, **kwargs)

        world.store.client = Overridden(world.s3, upload_fileobj=upload)
    else:
        world.store.keys = _FlakyKeys(world.store.keys, failures=2)
    run_id = await world.run(tenant)
    row = await run_row(superuser_engine, run_id)
    assert {item["name"] for item in row["artifacts"]} == set(FAILURE_FILES)
    assert row["error_code"] == "elemento_nao_encontrado"
    assert "não puderam ser guardados" not in row["error_message"]
    for name, (_, data) in FAILURE_FILES.items():
        content = await world.artifacts.open_artifact(tenant.operator, run_id, name)
        assert await content.read() == data
    assert _staged_files(world) == []


async def test_a_long_outage_is_written_in_the_run_and_no_plaintext_stays(
    world: S3World, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await world.tenant(app_db, "Queda longa")

    def upload(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("S3 fora do ar")

    world.store.client = Overridden(world.s3, upload_fileobj=upload)
    run_id = await world.run(tenant, state="succeeded")
    row = await run_row(superuser_engine, run_id)
    assert row["artifacts"] == [] and row["error_code"] == "artefatos_nao_guardados"
    assert row["error_message"].startswith(f"{len(FAILURE_FILES)} artefato(s) desta execução")
    assert bucket_objects(world.s3) == {} and _staged_files(world) == []


async def test_a_damaged_object_is_counted_in_the_export_and_refused_by_the_api(
    world: S3World, app_db: Database, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    tenant = await world.tenant(app_db, "Objeto danificado")
    damaged, cut, fine = [await world.run(tenant) for _ in range(3)]
    key = object_key(tenant.company_id, damaged, 1, "nota-001.pdf")
    world.s3.put_object(Bucket=BUCKET, Key=key, Body=b"RAPA")  # o cabeçalho trocado
    key = object_key(tenant.company_id, cut, 1, "video-falha.webm")
    raw = world.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    world.s3.put_object(Bucket=BUCKET, Key=key, Body=raw[:-5])  # cortado no fim
    output = tmp_path / "empresa.tar.gz"
    exported = await export_company(
        owner_engine, tenant.company_id, output, artifacts=world.owner_store(owner_engine)
    )
    with tarfile.open(output, "r:gz") as tar:
        contents = {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }
    assert json.loads(contents["manifesto.json"])["artefatos_ilegiveis"] == 2
    assert exported.files == 3 * len(FAILURE_FILES) - 2
    assert f"artefatos/{damaged}/1/nota-001.pdf" not in contents
    assert f"artefatos/{cut}/1/video-falha.webm" not in contents
    for name, (_, data) in FAILURE_FILES.items():
        assert contents[f"artefatos/{fine}/1/{name}"] == data
    # Pela API: o danificado responde 404 (com o motivo), e o resto continua servido.
    with pytest.raises(NotFound, match="danificado"):
        await world.artifacts.open_artifact(tenant.operator, damaged, "nota-001.pdf")
    content = await world.artifacts.open_artifact(
        tenant.operator, damaged, "tela-001-entrar-falha.png"
    )
    assert await content.read() == PNG


async def test_the_api_answers_503_when_the_object_store_refuses(
    world: S3World, app_db: Database
) -> None:
    tenant = await world.tenant(app_db, "S3 recusando")
    run_id = await world.run(tenant)

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    world.api_store.client = Overridden(world.s3, get_object=refused)
    with pytest.raises(ServiceUnavailable):
        await world.artifacts.open_artifact(tenant.operator, run_id, "nota-001.pdf")


async def test_retention_sweeps_the_staging_of_a_company_that_is_gone(
    world: S3World, app_db: Database, dispatcher_db: Database
) -> None:
    """A saída da empresa rodou noutro pod: a retenção do despachante leva a passagem."""
    tenant = await world.tenant(app_db, "Passagem viva")
    root = world.store.staging_root
    job = await _claim(world, tenant, dispatcher_db)
    gone = new_id()
    stage(root, gone, job.run_id, 1, "tela-001.png", PNG)
    live = stage(root, tenant.company_id, job.run_id, job.attempt, "tela-001.png", PNG)
    report = await Retention(dispatcher_db, world.store, RetentionPolicy()).tick()
    assert report.staged_companies == 1
    assert not (root / str(gone)).exists()
    assert live.exists()


async def _claim(world: S3World, tenant: Tenant, dispatcher_db: Database) -> RunnerJob:
    """Uma execução da empresa pega da fila (rodando, sem despachante que a acompanhe)."""
    run = await world.evidence.platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    job = await PgRunQueue(dispatcher_db).claim_next()
    assert job is not None and job.run_id == run.run_id
    return job


async def test_a_stale_attempt_releases_its_staging_at_once(
    world: S3World, app_db: Database, dispatcher_db: Database
) -> None:
    """A tentativa que fica antiga (o recolhedor de outra réplica a devolveu à fila) não
    registra nada: o texto claro que ela deixou na passagem sai na hora, não fica para a
    retenção (que pode rodar noutra réplica, longe deste disco)."""
    tenant = await world.tenant(app_db, "Passagem antiga")
    run = await world.evidence.platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    root = world.store.staging_root
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def launch(job: RunnerJob) -> Mapping[str, Any]:
        stage(root, job.company_id, job.run_id, job.attempt, "nota-001.pdf", PDF)
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("a tentativa devia ter ficado antiga")  # pragma: no cover

    dispatcher = Dispatcher(
        PgRunQueue(dispatcher_db), launch, heartbeat_interval=0.02, artifacts=world.store
    )
    assert await dispatcher.run_once() == 1
    await asyncio.wait_for(started.wait(), 5)
    reaper = PgRunQueue(dispatcher_db)
    report = await reaper.reap_expired(now=reaper.clock() + timedelta(minutes=5))
    assert report.requeued == [run.run_id]
    await asyncio.wait_for(cancelled.wait(), 5)
    await dispatcher.drain()
    assert not attempt_dir(root, tenant.company_id, run.run_id, 1).exists()


async def test_each_replica_sweeps_the_staging_of_attempts_that_are_not_live(
    world: S3World, app_db: Database, dispatcher_db: Database
) -> None:
    """Cada réplica do despachante tem a própria passagem (o emptyDir do pod), e a retenção
    de uma execução pode rodar noutra réplica: a volta da retenção de cada uma apaga, no
    próprio disco, a passagem de toda tentativa que não é mais a atual de uma execução
    rodando (a de um despachante que caiu antes de registrar), e deixa a da viva."""
    tenant = await world.tenant(app_db, "Passagem da outra réplica")
    finished = await world.run(tenant)
    job = await _claim(world, tenant, dispatcher_db)
    replica = s3_store(world.s3, world.store.staging_root.parent / "outra-replica")
    root = replica.staging_root
    left = stage(root, tenant.company_id, finished, 1, "nota-001.pdf", PDF)
    old = stage(root, tenant.company_id, job.run_id, job.attempt + 1, "nota-001.pdf", PDF)
    live = stage(root, tenant.company_id, job.run_id, job.attempt, "tela-001.png", PNG)
    # A execução viva debaixo de outra empresa (que existe): não é a tentativa viva.
    stranger = await world.tenant(app_db, "Passagem de outra empresa")
    other = stage(root, stranger.company_id, job.run_id, job.attempt, "tela-001.png", PNG)
    report = await Retention(dispatcher_db, replica, RetentionPolicy()).tick()
    assert not left.exists() and not left.parent.parent.exists()
    assert not old.exists()
    assert not other.exists()
    assert live.exists()
    assert report.staged_attempts == 3 and report.staged_companies == 0


async def test_the_api_answers_503_when_the_key_vault_is_down(
    world: S3World, app_db: Database
) -> None:
    tenant = await world.tenant(app_db, "Objetos sem cofre")
    run_id = await world.run(tenant)
    world.bao.sealed = True
    world.bao.keys.clear()  # o transit não responde como deveria
    with pytest.raises(ServiceUnavailable):
        await world.artifacts.open_artifact(tenant.operator, run_id, "nota-001.pdf")


async def test_retention_destroys_the_run_key_after_the_objects(
    world: S3World,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    a = await world.tenant(app_db, "Retenção cifrada")
    frozen = await world.tenant(app_db, "Retenção congelada")
    old_failure = await world.run(a)
    old_success = await world.run(
        a,
        state="succeeded",
        files={"tela-001.png": ("screenshot", PNG), "nota-001.pdf": ("download", PDF)},
    )
    fresh = await world.run(a)
    kept_frozen = await world.run(frozen)
    await age_run(superuser_engine, old_failure, 91)
    await age_run(superuser_engine, old_success, 8)
    # Uma "cópia de segurança" do objeto da falha velha, antes de a retenção apagar.
    backup_key = object_key(a.company_id, old_failure, 1, "nota-001.pdf")
    backup = world.s3.get_object(Bucket=BUCKET, Key=backup_key)["Body"].read()
    retention = Retention(dispatcher_db, world.store, RetentionPolicy(batch_size=50))
    report = await retention.tick()
    assert old_failure in report.artifacts and old_success in report.captures
    assert report.run_keys == 1
    keys = await _keys_of(superuser_engine, a.company_id)
    assert set(keys) == {old_success, fresh}  # só a que perdeu tudo perdeu a chave
    assert {k for k in bucket_objects(world.s3) if f"/{old_failure}/" in k} == set()
    assert {k for k in bucket_objects(world.s3) if f"/{old_success}/" in k} == {
        object_key(a.company_id, old_success, 1, "nota-001.pdf")
    }
    assert "execucao.chave_destruida" in await audit_actions(superuser_engine, a.company_id)
    # O backup do objeto volta para o bucket: sem a chave, não abre.
    world.s3.put_object(Bucket=BUCKET, Key=backup_key, Body=backup)
    with pytest.raises(ArtifactMissing):
        await world.api_store.open(a.company_id, old_failure, 1, "nota-001.pdf")
    content = await world.artifacts.open_artifact(a.operator, old_success, "nota-001.pdf")
    assert await content.read() == PDF
    # Empresa congelada: mesmo com a marca de expirado, a chave fica (nada dela expira).
    await freeze(owner_engine, frozen.company_id)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET artifacts_expired_at = now() WHERE id = :id"),
            {"id": kept_frozen},
        )
    assert (await retention.tick()).run_keys == 0
    assert set(await _keys_of(superuser_engine, frozen.company_id)) == {kept_frozen}
    await unfreeze_company(owner_engine, frozen.company_id)
    assert (await retention.tick()).run_keys == 1
    assert await _keys_of(superuser_engine, frozen.company_id) == {}


async def _sqlstate(
    engine_or_db: Any, *statements: str, run: UUID, company: UUID | None = None
) -> str | None:
    """Roda os comandos (com ``:run``) numa transação; devolve o SQLSTATE do erro."""
    try:
        if isinstance(engine_or_db, Database):
            async with engine_or_db.dispatcher_session() as session:
                for statement in statements:
                    await session.execute(text(statement), {"run": run})
        else:
            async with engine_or_db.begin() as conn:
                if company is not None:
                    await conn.execute(
                        text("SELECT set_config('rapa.company_id', :c, true)"), {"c": str(company)}
                    )
                for statement in statements:
                    await conn.execute(text(statement), {"run": run})
    except DBAPIError as exc:
        return getattr(exc.orig, "sqlstate", None)
    return None


async def test_run_keys_only_take_inserts_and_the_retention_function(
    world: S3World,
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
) -> None:
    tenant = await world.tenant(app_db, "Chaves só inclusões")
    run_id = await world.run(tenant)
    update = "UPDATE run_keys SET key_version = 9 WHERE run_id = :run"
    delete = "DELETE FROM run_keys WHERE run_id = :run"
    marker = "SELECT set_config('rapa.retention', 'run_keys', true)"
    # O dono das tabelas, sem a marca da função (ou com a marca, numa execução que não
    # expirou): a trava recusa mudar e apagar.
    for statements in ((update,), (delete,), (marker, delete)):
        state = await _sqlstate(owner_engine, *statements, run=run_id, company=tenant.company_id)
        assert state == "23514", statements
    # O despachante nem tem a permissão; a função confere o tamanho do lote.
    assert await _sqlstate(dispatcher_db, delete, run=run_id) == "42501"
    assert await _sqlstate(dispatcher_db, update, run=run_id) == "42501"
    purge = "SELECT * FROM rapa_purge_run_keys(0)"
    assert await _sqlstate(dispatcher_db, purge, run=run_id) == "22023"
    # A função só apaga a chave de uma execução que perdeu todos os artefatos.
    async with dispatcher_db.dispatcher_session() as session:
        gone = (await session.execute(text("SELECT * FROM rapa_purge_run_keys(10)"))).all()
    assert gone == []
    assert await PgRunKeys(app_db).get(tenant.company_id, run_id) is not None


async def test_export_decrypts_and_purge_destroys_objects_secrets_and_the_company_key(
    world: S3World,
    app_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    a = await world.tenant(app_db, "Saída cifrada")
    b = await world.tenant(app_db, "Fica cifrada")
    runs = [await world.run(a), await world.run(a)]
    other_run = await world.run(b)
    for company in (a.company_id, b.company_id):
        world.bao.put_secret(f"empresas/{company}/portal/acesso", {"password": "senha-x"})
        world.bao.put_secret(f"empresas/{company}/a1/principal", {"pfx_base64": "AAAA"})
    store = world.owner_store(owner_engine)

    output = tmp_path / "empresa.tar.gz"
    exported = await export_company(owner_engine, a.company_id, output, artifacts=store)
    with tarfile.open(output, "r:gz") as tar:
        contents = {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }
    manifest = json.loads(contents["manifesto.json"])
    assert manifest["artefatos_ilegiveis"] == 0
    assert exported.files == 2 * len(FAILURE_FILES)
    for run_id in runs:
        for name, (_, data) in FAILURE_FILES.items():
            assert contents[f"artefatos/{run_id}/1/{name}"] == data  # decifrados
    exported_keys = [
        json.loads(line) for line in io.StringIO(contents["tabelas/run_keys.jsonl"].decode())
    ]
    assert len(exported_keys) == 2 and all(row["wrapped_key"] is None for row in exported_keys)
    assert b"vault:v1:" not in b"".join(contents.values())

    await freeze(owner_engine, a.company_id)
    before = await company_counts(superuser_engine, a.company_id)
    destroyer = OpenBaoKeyDestroyer(world.client, store=store)

    class BrokenStore(S3ArtifactStore):
        async def delete_company(self, company_id: UUID) -> int:
            raise ConnectionError("S3 fora do ar")

    broken = BrokenStore(
        client=store.client,
        bucket=store.bucket,
        keys=store.keys,
        run_keys=store.run_keys,
        staging_root=store.staging_root,
    )
    # O S3 falha: nada do banco sai, nada do cofre sai, e o comando pode ser repetido.
    with pytest.raises(ConnectionError):
        await purge_company(
            owner_engine,
            a.company_id,
            confirm_name="Saída cifrada",
            artifacts=broken,
            key_destroyer=destroyer,
        )
    assert await company_counts(superuser_engine, a.company_id) == before
    assert len(world.bao.secrets) == 4 and len(world.bao.keys) == 2
    purged = await purge_company(
        owner_engine,
        a.company_id,
        confirm_name="Saída cifrada",
        artifacts=store,
        key_destroyer=destroyer,
    )
    assert purged.keys == {"segredos": 2, "chaves_transit": 1, "objetos": 0}
    assert set((await company_counts(superuser_engine, a.company_id)).values()) == {0}
    # Nada da empresa sobra no bucket, no KV nem no transit; a outra fica inteira.
    assert all(f"empresas/{a.company_id}/" not in key for key in bucket_objects(world.s3))
    assert sorted(world.bao.secrets) == [
        f"empresas/{b.company_id}/a1/principal",
        f"empresas/{b.company_id}/portal/acesso",
    ]
    assert set(world.bao.keys) == {f"empresa-{b.company_id}"}
    content = await world.artifacts.open_artifact(b.operator, other_run, "nota-001.pdf")
    assert await content.read() == PDF
    async with superuser_engine.connect() as conn:
        event = await conn.scalar(
            text("SELECT data FROM platform_events WHERE target = :t"),
            {"t": f"empresa:{a.company_id}"},
        )
    assert event["artefatos"] == purged.artifacts == 2 * len(FAILURE_FILES)
    assert event["chaves"] == {"segredos": 2, "chaves_transit": 1, "objetos": 0}
    assert "senha-x" not in json.dumps(event)


async def test_purge_refuses_a_configuration_that_does_not_reach_the_company(
    world: S3World,
    app_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Falha fechada: sem o S3, sem o transit ou sem o cofre dos segredos configurados,
    nada sai do banco (as credenciais, as chaves e os objetos ficariam esquecidos)."""
    a = await world.tenant(app_db, "Saída sem configuração")
    await world.run(a)
    await PgProvisioning(app_db).register_credential(
        a.company_id,
        a.area_id,
        CredentialRef(name="portal", kind=CredentialKind.PASSWORD, origin="https://portal.test"),
        "portal/acesso",
    )
    world.bao.put_secret(f"empresas/{a.company_id}/portal/acesso", {"password": "senha-x"})
    await freeze(owner_engine, a.company_id)
    before = await company_counts(superuser_engine, a.company_id)
    objects = bucket_objects(world.s3)
    store = world.owner_store(owner_engine)

    async def purge(**kwargs: Any) -> Any:
        return await purge_company(
            owner_engine, a.company_id, confirm_name="Saída sem configuração", **kwargs
        )

    # O Job da operação sem nada do cofre nem do S3: o store local e o destruidor que só
    # registra. Antes, isto apagava as linhas e deixava tudo para trás.
    with pytest.raises(InvalidInput) as nothing:
        await purge(artifacts_dir=tmp_path / "artefatos", key_destroyer=LocalKeyDestroyer())
    assert len(nothing.value.errors) == 3
    assert "RAPA_ARMAZENAMENTO=s3" in nothing.value.errors[0]
    assert "RAPA_CHAVES=openbao" in nothing.value.errors[1]
    assert "--segredos-fora-do-openbao" in nothing.value.errors[2]
    # Com o S3 mas sem o cofre: as chaves do transit e os segredos ainda ficariam.
    with pytest.raises(InvalidInput) as no_vault:
        await purge(artifacts=store, key_destroyer=LocalKeyDestroyer())
    assert len(no_vault.value.errors) == 2
    # Só o transit (os segredos de outro jeito), sem dizer onde estão os segredos.
    transit_only = OpenBaoKeyDestroyer(world.client, secrets=False, store=store)
    with pytest.raises(InvalidInput) as no_secrets:
        await purge(artifacts=store, key_destroyer=transit_only)
    assert [message[:40] for message in no_secrets.value.errors] == [
        "a empresa tem 1 credencial(is) registrad"
    ]
    assert await company_counts(superuser_engine, a.company_id) == before
    assert bucket_objects(world.s3) == objects
    assert f"empresa-{a.company_id}" in world.bao.keys and world.bao.secrets
    # Quem pede diz que os segredos não estão no OpenBao: sai, e o registro diz quantos.
    purged = await purge(artifacts=store, key_destroyer=transit_only, secrets_outside_vault=True)
    assert purged.keys == {"chaves_transit": 1, "objetos": 0}
    assert set((await company_counts(superuser_engine, a.company_id)).values()) == {0}
    async with superuser_engine.connect() as conn:
        event = await conn.scalar(
            text("SELECT data FROM platform_events WHERE target = :t"),
            {"t": f"empresa:{a.company_id}"},
        )
    assert event["segredos_fora_do_openbao"] == 1
    assert f"empresas/{a.company_id}/portal/acesso" in world.bao.secrets  # não alcançado
