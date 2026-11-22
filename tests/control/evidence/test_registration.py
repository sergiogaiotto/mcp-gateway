"""O despachante registra os artefatos pelo store: só o que está na pasta da tentativa."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.artifacts import ArtifactInfo
from rapa.control.db import Database
from rapa.core.contracts import RunState
from rapa.runner.ports import sanitize_filename
from tests.control.db.support import make_tenant, protocolo, publish, reset_queue
from tests.control.evidence.support import PDF, PNG, EvidenceWorld, run_row

pytestmark = pytest.mark.database


@pytest.fixture
async def world(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> EvidenceWorld:
    await reset_queue(superuser_engine, total_slots=10)
    return EvidenceWorld(app_db, dispatcher_db, tmp_path / "artefatos")


async def test_only_files_in_the_attempt_folder_become_evidence(
    world: EvidenceWorld, app_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Registro")
    await publish(world.platform, tenant, protocolo())
    secret = tmp_path / "senhas.txt"
    secret.write_text("não é evidência")
    run_id = await world.run(
        tenant,
        state="failed",
        files={"tela-001-entrar-falha.png": ("screenshot", PNG), "nota.pdf": ("download", PDF)},
        # O resultado aponta um arquivo de fora da pasta: o store recusa, o resto vale.
        outside={"senhas.txt": secret},
    )
    row = await run_row(superuser_engine, run_id)
    names = sorted(item["name"] for item in row["artifacts"])
    assert names == ["nota.pdf", "tela-001-entrar-falha.png"]
    for item in row["artifacts"]:
        info = ArtifactInfo.model_validate(item)
        assert info.attempt == 1 and info.created_at is not None
        assert set(item) == {"kind", "name", "size", "sha256", "attempt", "created_at"}
    assert str(tmp_path) not in json.dumps(row, default=str)
    status = await world.platform.get(tenant.operator, run_id)
    assert status is not None and status.state is RunState.FAILED


async def test_names_the_runner_makes_are_names_the_store_accepts(
    world: EvidenceWorld, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    """Um arquivo baixado com pontos seguidos no nome vira evidência com o nome que a
    saída do download aponta (o executor saneia como o plano de controle confere)."""
    tenant = await make_tenant(app_db, "Nomes do executor")
    await publish(world.platform, tenant, protocolo())
    names = [sanitize_filename(raw) for raw in ("nota..pdf", "Relatório 2026...xlsx")]
    assert names == ["nota.pdf", "Relatorio_2026.xlsx"]
    run_id = await world.run(
        tenant, state="succeeded", files={name: ("download", PDF) for name in names}
    )
    row = await run_row(superuser_engine, run_id)
    assert sorted(item["name"] for item in row["artifacts"]) == sorted(names)


class _BrokenStore:
    async def put(self, *args: Any) -> Any:
        raise RuntimeError("armazenamento fora do ar")

    async def open(self, *args: Any) -> Any:  # pragma: no cover - não é usado aqui
        raise AssertionError

    async def delete_run(
        self, company_id: UUID, run_id: UUID, **_: Any
    ) -> None:  # pragma: no cover
        raise AssertionError


async def test_a_store_that_fails_never_stops_the_result(
    world: EvidenceWorld, app_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(app_db, "Store fora")
    await publish(world.platform, tenant, protocolo())
    world.dispatcher.artifacts = _BrokenStore()
    run_id = await world.run(tenant, state="succeeded")
    status = await world.platform.get(tenant.operator, run_id)
    assert status is not None and status.state is RunState.SUCCEEDED
    assert (await run_row(superuser_engine, run_id))["artifacts"] == []
