"""Auxiliares dos testes das evidências: execuções que terminam com artefatos de verdade.

O despachante é o de verdade (pega a execução, registra os artefatos pelo store e grava
o resultado); só o lançador é trocado por um que escreve os arquivos na pasta da
tentativa, como o worker escreveria, e devolve o RESULT.json.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.artifacts import LocalArtifactStore, attempt_dir
from rapa.control.db import Database
from rapa.control.services import Dispatcher, PgPlatform, PgRunQueue
from rapa.core.contracts import RunnerJob, RunTrigger
from tests.control.db.support import PROTOCOLO_INPUTS, Tenant, result

#: Uma tela PNG mínima (o executor guarda PNG de verdade; aqui basta o começo).
PNG = b"\x89PNG\r\n\x1a\n" + b"tela" * 16
WEBM = bytes.fromhex("1a45dfa3") + b"video" * 16
PDF = b"%PDF-1.4\n" + b"nota" * 16

#: Os artefatos de uma execução que falhou: a tela da falha, o vídeo e um download.
FAILURE_FILES: dict[str, tuple[str, bytes]] = {
    "tela-001-entrar-falha.png": ("screenshot", PNG),
    "video-falha.webm": ("video", WEBM),
    "nota-001.pdf": ("download", PDF),
}


@dataclass
class _Plan:
    state: str
    files: Mapping[str, tuple[str, bytes]]
    #: Caminhos informados no resultado fora da pasta da tentativa (o store recusa).
    outside: Mapping[str, Path] = field(default_factory=dict)
    error_code: str | None = None
    #: Arquivos que o executor deixa na pasta da tentativa sem informar no resultado.
    unreported: Mapping[str, bytes] = field(default_factory=dict)


class FakeLauncher:
    """Escreve os arquivos de cada execução na pasta da tentativa e devolve o resultado."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.plans: dict[UUID, _Plan] = {}

    async def __call__(self, job: RunnerJob) -> dict[str, Any]:
        plan = self.plans[job.run_id]
        folder = attempt_dir(self.root, job.company_id, job.run_id, job.attempt)
        folder.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, Any]] = []
        for name, (kind, data) in plan.files.items():
            path = folder / name
            path.write_bytes(data)
            artifacts.append(_reported(kind, name, path, data))
        for name, path in plan.outside.items():
            artifacts.append(_reported("download", name, path, path.read_bytes()))
        for name, data in plan.unreported.items():
            (folder / name).write_bytes(data)
        extra: dict[str, Any] = {"artifacts": artifacts}
        if plan.state != "succeeded":
            extra |= {
                "error_code": plan.error_code or "elemento_nao_encontrado",
                "error_message": "Não encontrei o elemento 'botão Entrar'.",
                "failed_step_id": "entrar",
            }
        return result(job.run_id, job.attempt, plan.state, **extra)


def _reported(kind: str, name: str, path: Path, data: bytes) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class EvidenceWorld:
    """Execuções que terminam pelo despachante, com os artefatos no store local."""

    def __init__(self, app_db: Database, dispatcher_db: Database, root: Path) -> None:
        self.platform = PgPlatform(app_db)
        self.store = LocalArtifactStore(root)
        self.launcher = FakeLauncher(root)
        self.dispatcher = Dispatcher(
            PgRunQueue(dispatcher_db),
            self.launcher,
            concurrency=4,
            artifacts=self.store,
            artifact_retry_s=0.01,
        )

    async def run(
        self,
        tenant: Tenant,
        *,
        state: str = "failed",
        files: Mapping[str, tuple[str, bytes]] | None = None,
        outside: Mapping[str, Path] | None = None,
        unreported: Mapping[str, bytes] | None = None,
        slug: str = "emitir_protocolo",
    ) -> UUID:
        """Inicia, despacha e termina uma execução com os arquivos dados."""
        run = await self.platform.start(tenant.operator, slug, PROTOCOLO_INPUTS, RunTrigger.REST)
        self.launcher.plans[run.run_id] = _Plan(
            state,
            FAILURE_FILES if files is None else files,
            outside or {},
            unreported=unreported or {},
        )
        await self.dispatcher.run_once()
        await self.dispatcher.drain()
        status = await self.platform.get(tenant.operator, run.run_id)
        assert status is not None and status.state.finished, status
        return run.run_id


async def age_run(superuser: AsyncEngine, run_id: UUID, days: float) -> None:
    """Põe o fim da execução ``days`` dias no passado (como se o tempo tivesse passado)."""
    async with superuser.begin() as conn:
        await conn.execute(
            text("UPDATE runs SET finished_at = now() - :age WHERE id = :id"),
            {"age": timedelta(days=days), "id": run_id},
        )


async def run_row(superuser: AsyncEngine, run_id: UUID) -> dict[str, Any]:
    async with superuser.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT artifacts, artifacts_expired_at, captures_expired_at, outputs, "
                        "error_code, error_message FROM runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)
