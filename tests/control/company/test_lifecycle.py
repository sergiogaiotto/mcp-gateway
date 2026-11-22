"""Entrada e saída da empresa: criar, congelar, exportar e apagar (linha de comando e serviços)."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import secrets
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.admin import main
from rapa.control.db import Database
from rapa.control.services import PgPlatform, PgPrincipalResolver, PgRunQueue, lifecycle
from rapa.control.services.lifecycle import (
    export_company,
    purge_company,
    unfreeze_company,
)
from rapa.core.contracts import Conflict, Forbidden, InvalidInput, Role, RunTrigger, WebhookSpec
from rapa.core.ids import new_id
from tests.control.db.support import (
    PROTOCOLO_INPUTS,
    PgDatabase,
    failed_run,
    result,
)
from tests.control.publishing.support import World, build_world
from tests.repair.support import login_page

from .support import audit_actions, company_counts, freeze

pytestmark = pytest.mark.database

SLUG = "emitir_protocolo"


async def _cli(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = await asyncio.to_thread(main, argv)
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err.strip()


async def test_create_company_with_admin_organization_and_areas(
    pg_database: PgDatabase,
    app_db: Database,
    superuser_engine: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tag = secrets.token_hex(4)
    organization = f"org-{tag}"
    admin = f"ana-{tag}@empresa"
    url = ["--url", pg_database.app_url]
    code, out, err = await _cli(
        capsys,
        [
            *url,
            "empresa",
            "criar",
            "Empresa Nova",
            "--organizacao",
            organization,
            "--admin",
            admin,
            "--nome-admin",
            "Ana",
            "--area",
            "Fiscal",
            "--area",
            "Compras",
            "--vagas",
            "3",
            "--fila",
            "50",
            "--emprestimo",
            "1.5",
            "--por",
            "operadora-da-plataforma",
        ],
    )
    assert code == 0, err
    company = UUID(out)
    async with superuser_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT c.name, c.status, c.idp_organization, q.guaranteed_slots, "
                        "q.max_queued, q.max_borrow_factor FROM companies c "
                        "JOIN company_quotas q ON q.company_id = c.id WHERE c.id = :c"
                    ),
                    {"c": company},
                )
            )
            .mappings()
            .one()
        )
        areas = set(
            (
                await conn.scalars(
                    text("SELECT name FROM areas WHERE company_id = :c"), {"c": company}
                )
            ).all()
        )
        actor = await conn.scalar(
            text("SELECT actor FROM audit_log WHERE company_id = :c AND action = 'empresa.criada'"),
            {"c": company},
        )
    assert dict(row) == {
        "name": "Empresa Nova",
        "status": "active",
        "idp_organization": organization,
        "guaranteed_slots": 3,
        "max_queued": 50,
        "max_borrow_factor": pytest.approx(1.5),
    }
    assert areas == {"Fiscal", "Compras"}
    assert actor == "plataforma:operadora-da-plataforma"
    principal = await PgPrincipalResolver(app_db).resolve(admin, company)
    assert principal is not None and principal.company_roles == {Role.COMPANY_ADMIN}
    actions = await audit_actions(superuser_engine, company)
    assert actions == [
        "empresa.criada",
        "area.criada",
        "area.criada",
        "usuario.criado",
        "usuario.papeis_alterados",
    ]
    # A mesma organização ou a mesma pessoa em outra empresa: recusado, sem deixar nada.
    async with superuser_engine.connect() as conn:
        before = await conn.scalar(text("SELECT count(*) FROM companies"))
    for organization_used, subject in ((organization, f"outra-{tag}@x"), (f"org2-{tag}", admin)):
        code, _, err = await _cli(
            capsys,
            [
                *url,
                "empresa",
                "criar",
                "Outra",
                "--organizacao",
                organization_used,
                "--admin",
                subject,
            ],
        )
        assert code == 1
        assert "já" in err
    async with superuser_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM companies")) == before
    # A forma antiga continua cadastrando.
    code, out, _ = await _cli(capsys, [*url, "empresa", "Forma antiga", "--vagas", "2"])
    assert code == 0 and UUID(out)


async def test_freeze_and_unfreeze_from_the_command_line(
    pg_database: PgDatabase,
    app_db: Database,
    superuser_engine: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = await build_world(app_db, superuser_engine)
    company = str(world.a.company_id)
    await world.platform.start(world.a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    owner = ["--url-dono", pg_database.owner_url]
    code, out, err = await _cli(
        capsys, ["empresa", "congelar", company, "--motivo", "fim do contrato", *owner]
    )
    assert code == 0, err
    assert "1 execuções da fila canceladas" in out
    code, out, _ = await _cli(
        capsys, ["empresa", "congelar", company, "--motivo", "de novo", *owner]
    )
    assert code == 0 and "já estava congelada" in out
    # Com a URL da aplicação no lugar da do dono: recusado com uma mensagem clara.
    code, _, err = await _cli(
        capsys, ["empresa", "descongelar", company, "--url-dono", pg_database.app_url]
    )
    assert code == 1 and "dono das tabelas" in err
    code, out, _ = await _cli(capsys, ["empresa", "descongelar", company, *owner])
    assert code == 0 and out == "empresa descongelada"
    # Sem URL do dono nenhuma, o comando nem começa.
    os.environ.pop("RAPA_OWNER_DATABASE_URL", None)
    with pytest.raises(SystemExit):
        main(["empresa", "congelar", company, "--motivo", "x"])


async def _rich_company(world: World, dispatcher_db: Database, app_db: Database) -> dict[str, Any]:
    """Dados em várias tabelas: execuções, eventos, retrato, webhook (com o sal) e agenda."""
    a = world.a
    run_id = await failed_run(
        app_db, dispatcher_db, a, snapshot=login_page().model_dump(mode="json")
    )
    hook = await world.webhooks.create_webhook(
        a.operator, world.robots_a[SLUG], WebhookSpec(title="ERP")
    )
    return {"run_id": run_id, "webhook_id": hook.webhook.webhook_id}


def _folders(base: Path, company: UUID) -> tuple[Path, Path]:
    files, artifacts = base / "arquivos", base / "artefatos"
    (files / str(company) / "sub").mkdir(parents=True)
    (files / str(company) / "sub" / "nota.pdf").write_bytes(b"%PDF-1.4 nota")
    (artifacts / str(company) / "execucao").mkdir(parents=True)
    (artifacts / str(company) / "execucao" / "tela-001.png").write_bytes(b"\x89PNG tela")
    # Um link para fora da pasta da empresa nunca entra na exportação.
    (files / str(company) / "fora").symlink_to(base)
    return files, artifacts


async def test_export_has_every_table_a_checked_manifest_and_no_key_material(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    world = await build_world(app_db, superuser_engine)
    a = world.a
    data = await _rich_company(world, dispatcher_db, app_db)
    files, artifacts = _folders(tmp_path, a.company_id)
    output = tmp_path / "empresa.tar.gz"
    report = await export_company(
        owner_engine, a.company_id, output, files_dir=files, artifacts_dir=artifacts
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    counts = await company_counts(superuser_engine, a.company_id)
    assert report.tables == counts
    assert report.files == 2
    with tarfile.open(output, "r:gz") as tar:
        names = tar.getnames()
        assert names[0] == "manifesto.json"
        manifest = json.loads(tar.extractfile("manifesto.json").read())  # type: ignore[union-attr]
        contents = {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }
        modes = {member.name: member.mode for member in tar.getmembers()}
    assert set(modes.values()) == {0o600}
    assert manifest["empresa"]["id"] == str(a.company_id)
    tables = {entry["tabela"]: entry for entry in manifest["tabelas"]}
    assert set(tables) == set(counts)
    for name, entry in tables.items():
        body = contents[entry["arquivo"]]
        assert hashlib.sha256(body).hexdigest() == entry["sha256"], name
        lines = body.decode().splitlines()
        assert len(lines) == entry["linhas"] == counts[name], name
        for line in lines:
            row = json.loads(line)
            assert row.get("company_id", row.get("id")) == str(a.company_id)
    # Ordem estável: as tabelas-mãe antes das filhas.
    order = [entry["tabela"] for entry in manifest["tabelas"]]
    assert order[0] == "companies"
    assert order.index("robots") < order.index("robot_versions") < order.index("runs")
    # O sal do webhook sai como null, e o valor dele não aparece em lugar nenhum.
    hook = json.loads(contents["tabelas/webhooks.jsonl"].decode().splitlines()[0])
    assert hook["id"] == str(data["webhook_id"]) and hook["key_salt"] is None
    assert tables["webhooks"]["colunas_sem_valor"] == ["key_salt"]
    async with superuser_engine.connect() as conn:
        salt = await conn.scalar(
            text("SELECT key_salt FROM webhooks WHERE id = :id"), {"id": data["webhook_id"]}
        )
    everything = b"".join(contents.values())
    assert bytes(salt).hex().encode() not in everything
    assert str(world.b.company_id).encode() not in everything
    # Os arquivos da empresa, conferidos pelo manifesto; o link para fora ficou de fora.
    assert {f["arquivo"] for f in manifest["arquivos"]} == {
        "arquivos/sub/nota.pdf",
        "artefatos/execucao/tela-001.png",
    }
    for entry in manifest["arquivos"]:
        assert hashlib.sha256(contents[entry["arquivo"]]).hexdigest() == entry["sha256"]
    # Nunca sobrescreve uma exportação.
    with pytest.raises(Conflict):
        await export_company(owner_engine, a.company_id, output)
    # Só o dono exporta.
    with pytest.raises(Forbidden):
        await export_company(app_db.engine, a.company_id, tmp_path / "outra.tar.gz")
    assert not (tmp_path / "outra.tar.gz").exists()


async def test_export_never_follows_a_file_swapped_for_a_link(
    app_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Um arquivo trocado por um link depois da listagem: o .tar.gz guarda o que foi conferido."""
    world = await build_world(app_db, superuser_engine)
    a = world.a
    files, artifacts = _folders(tmp_path, a.company_id)
    outside = tmp_path / "de-fora.txt"
    outside.write_bytes(b"conteudo de fora da pasta da empresa")
    company_files = files / str(a.company_id)
    # Um link para um arquivo de fora e uma fila (que, aberta, prenderia a exportação).
    (company_files / "link-para-fora.txt").symlink_to(outside)
    os.mkfifo(company_files / "fila")
    target = company_files / "sub" / "nota.pdf"
    original = lifecycle._exclusive_output

    @contextlib.contextmanager
    def swap_then_write(path: Path) -> Iterator[Any]:
        # Depois da listagem e do SHA-256, antes de gravar o .tar.gz: a nota vira um link.
        target.unlink()
        target.symlink_to(outside)
        with original(path) as handle:
            yield handle

    monkeypatch.setattr(lifecycle, "_exclusive_output", swap_then_write)
    output = tmp_path / "empresa.tar.gz"
    report = await export_company(
        owner_engine, a.company_id, output, files_dir=files, artifacts_dir=artifacts
    )
    assert target.is_symlink()  # a troca aconteceu mesmo
    assert report.files == 2
    with tarfile.open(output, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifesto.json").read())  # type: ignore[union-attr]
        contents = {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }
    assert contents["arquivos/sub/nota.pdf"] == b"%PDF-1.4 nota"
    for entry in manifest["arquivos"]:
        body = contents[entry["arquivo"]]
        assert (len(body), hashlib.sha256(body).hexdigest()) == (entry["bytes"], entry["sha256"])
    assert {entry["arquivo"] for entry in manifest["arquivos"]} == {
        "arquivos/sub/nota.pdf",
        "artefatos/execucao/tela-001.png",
    }
    assert not any(b"de fora" in body for body in contents.values())


def _is_dir(path: Path) -> bool:
    return path.is_dir()


class FakeDestroyer:
    def __init__(self) -> None:
        self.destroyed: list[UUID] = []

    async def destroy(self, company_id: UUID) -> None:
        self.destroyed.append(company_id)


async def test_purge_leaves_nothing_and_only_for_a_frozen_company(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    world = await build_world(app_db, superuser_engine)
    a, b = world.a, world.b
    await _rich_company(world, dispatcher_db, app_db)
    # Uma execução rodando quando a empresa é congelada.
    queue = PgRunQueue(dispatcher_db)
    await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    job = await queue.claim_next()
    assert job is not None and job.company_id == a.company_id
    files, artifacts = _folders(tmp_path, a.company_id)
    (files / str(b.company_id)).mkdir(parents=True)
    (files / str(b.company_id) / "da-outra.csv").write_text("intacto")
    name = "Empresa A"
    destroyer = FakeDestroyer()

    def purge(engine: AsyncEngine, confirm: str = name) -> Any:
        return purge_company(
            engine,
            a.company_id,
            confirm_name=confirm,
            requested_by="operadora",
            files_dir=files,
            artifacts_dir=artifacts,
            key_destroyer=destroyer,
        )

    # Ativa: recusado.
    with pytest.raises(Conflict) as active:
        await purge(owner_engine)
    assert "congelada" in active.value.message
    await freeze(owner_engine, a.company_id)
    # Nome errado, papel da aplicação e execução ainda rodando: recusados, sem apagar nada.
    with pytest.raises(InvalidInput):
        await purge(owner_engine, "Empresa a")
    with pytest.raises(Forbidden):
        await purge(app_db.engine)
    with pytest.raises(Conflict) as running:
        await purge(owner_engine)
    assert "rodando" in running.value.message
    assert destroyer.destroyed == []
    assert await queue.finish(job.run_id, job.attempt, result(job.run_id, job.attempt))
    other_before = await company_counts(superuser_engine, b.company_id)
    a_before = await company_counts(superuser_engine, a.company_id)
    assert a_before["runs"] > 0 and a_before["audit_log"] > 0 and a_before["run_snapshots"] == 1

    report = await purge(owner_engine)
    assert report.tables == a_before
    after = await company_counts(superuser_engine, a.company_id)
    assert set(after.values()) == {0}, after
    assert await company_counts(superuser_engine, b.company_id) == other_before
    assert not (files / str(a.company_id)).exists()
    assert not (artifacts / str(a.company_id)).exists()
    assert (files / str(b.company_id) / "da-outra.csv").read_text() == "intacto"
    assert _is_dir(tmp_path)  # o link para fora some sozinho, o destino fica
    assert destroyer.destroyed == [a.company_id]
    async with superuser_engine.connect() as conn:
        event = (
            (
                await conn.execute(
                    text(
                        "SELECT action, actor, target, data FROM platform_events WHERE target = :t"
                    ),
                    {"t": f"empresa:{a.company_id}"},
                )
            )
            .mappings()
            .one()
        )
    assert event["action"] == "empresa.apagada"
    assert event["actor"] == "plataforma:operadora"
    assert event["data"]["nome_sha256"] == hashlib.sha256(name.encode()).hexdigest()
    assert event["data"]["linhas"] == a_before
    assert name not in json.dumps(event["data"], ensure_ascii=False)
    # A outra empresa continua funcionando.
    run = await PgPlatform(app_db).start(b.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    assert run.run_id
    # Apagar de novo: a empresa não existe mais.
    from rapa.core.contracts import NotFound

    with pytest.raises(NotFound):
        await purge(owner_engine)
    with pytest.raises(NotFound):
        await unfreeze_company(owner_engine, a.company_id)


async def test_purge_waits_for_open_sessions_and_runs_awaiting_a_person(
    app_db: Database,
    dispatcher_db: Database,
    owner_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    """Sessão de gravação aberta (começando ou ativa) ou execução esperando alguém: recusado."""
    world = await build_world(app_db, superuser_engine)
    a = world.a
    session_id = new_id()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recording_sessions (company_id, id, area_id, owner, start_url, "
                "expires_at) VALUES (:c, :id, :area, 'criadora', "
                "'https://portal.exemplo.gov.br/', now() + interval '1 hour')"
            ),
            {"c": a.company_id, "id": session_id, "area": a.area_id},
        )
    run = await world.platform.start(a.operator, SLUG, PROTOCOLO_INPUTS, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db)
    job = await queue.claim_next()
    assert job is not None and job.run_id == run.run_id
    await freeze(owner_engine, a.company_id)
    destroyer = FakeDestroyer()

    def purge() -> Any:
        return purge_company(
            owner_engine, a.company_id, confirm_name="Empresa A", key_destroyer=destroyer
        )

    async def set_rows(statement: str, **params: Any) -> None:
        async with superuser_engine.begin() as conn:
            await conn.execute(text(statement), params)

    # A execução espera alguém (e a sessão está começando): a execução recusa primeiro.
    await set_rows("UPDATE runs SET state = 'awaiting_human' WHERE id = :id", id=run.run_id)
    with pytest.raises(Conflict) as waiting:
        await purge()
    assert "rodando" in waiting.value.message
    await set_rows(
        "UPDATE runs SET state = 'succeeded', finished_at = now(), lease_expires_at = NULL "
        "WHERE id = :id",
        id=run.run_id,
    )
    # A sessão de gravação começando, depois ativa: recusado.
    for status in ("starting", "active"):
        await set_rows(
            "UPDATE recording_sessions SET status = :s, started_at = now() WHERE id = :id",
            s=status,
            id=session_id,
        )
        with pytest.raises(Conflict) as live:
            await purge()
        assert "sessões de gravação abertas" in live.value.message, status
    assert destroyer.destroyed == []
    assert (await company_counts(superuser_engine, a.company_id))["recording_sessions"] == 1
    # Encerrada a sessão, apagar passa.
    await set_rows(
        "UPDATE recording_sessions SET status = 'stopped', stopped_at = now() WHERE id = :id",
        id=session_id,
    )
    await purge()
    assert set((await company_counts(superuser_engine, a.company_id)).values()) == {0}
    assert destroyer.destroyed == [a.company_id]


async def test_append_only_guards_accept_only_the_owner_purge(
    app_db: Database, owner_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    world = await build_world(app_db, superuser_engine)
    a, b = world.a, world.b
    delete = text("DELETE FROM audit_log WHERE company_id = :c")

    async def attempt(engine: AsyncEngine, marker: UUID | None, tenant: UUID) -> str | None:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('rapa.company_id', :c, true)"), {"c": str(tenant)}
                )
                if marker is not None:
                    await conn.execute(
                        text("SELECT set_config('rapa.purge_company', :c, true)"),
                        {"c": str(marker)},
                    )
                await conn.execute(delete, {"c": tenant})
                raise RuntimeError("rollback")  # nunca confirma: só interessa se foi aceito
        except RuntimeError:
            return None
        except DBAPIError as exc:
            return getattr(exc.orig, "sqlstate", None)

    # O dono sem a marca, o dono com a marca de outra empresa e o superusuário com a marca
    # (não é o dono): a trava recusa. O papel da aplicação nem tem permissão.
    assert await attempt(owner_engine, None, a.company_id) == "23514"
    assert await attempt(owner_engine, b.company_id, a.company_id) == "23514"
    assert await attempt(superuser_engine, a.company_id, a.company_id) == "23514"
    assert await attempt(app_db.engine, a.company_id, a.company_id) == "42501"
    # Só o dono com a marca da própria empresa passa (e a transação acima foi desfeita).
    assert await attempt(owner_engine, a.company_id, a.company_id) is None
    assert (await company_counts(superuser_engine, a.company_id))["audit_log"] > 0
    # Os registros da plataforma: nem a aplicação nem o despachante leem.
    with pytest.raises(DBAPIError) as denied:
        async with app_db.dispatcher_session() as session:
            await session.execute(text("SELECT count(*) FROM platform_events"))
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"


async def test_cli_export_and_purge(
    pg_database: PgDatabase,
    app_db: Database,
    superuser_engine: AsyncEngine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await build_world(app_db, superuser_engine)
    a = world.a
    company = str(a.company_id)
    monkeypatch.setenv("RAPA_OWNER_DATABASE_URL", pg_database.owner_url)
    monkeypatch.setenv("RAPA_PASTA_ARTEFATOS", str(tmp_path / "artefatos"))
    monkeypatch.delenv("RAPA_PASTA_ARQUIVOS", raising=False)
    output = tmp_path / "saida.tar.gz"
    code, out, err = await _cli(capsys, ["empresa", "exportar", company, "--saida", str(output)])
    assert code == 0, err
    assert str(output) in out
    code, _, err = await _cli(capsys, ["empresa", "exportar", company, "--saida", str(output)])
    assert code == 1 and "já existe" in err
    code, _, err = await _cli(capsys, ["empresa", "apagar", company, "--confirmar", "Empresa A"])
    assert code == 1 and "congelada" in err
    assert (await _cli(capsys, ["empresa", "congelar", company, "--motivo", "saída"]))[0] == 0
    code, out, err = await _cli(capsys, ["empresa", "apagar", company, "--confirmar", "Empresa A"])
    assert code == 0, err
    assert out.startswith("empresa apagada")
    assert set((await company_counts(superuser_engine, a.company_id)).values()) == {0}
    with tarfile.open(output, "r:gz") as tar:
        manifest = json.load(io.BytesIO(tar.extractfile("manifesto.json").read()))  # type: ignore[union-attr]
    assert manifest["formato"] == 1
