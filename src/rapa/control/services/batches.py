"""Lote por planilha (:class:`PgBatches`): uma execução por linha, tudo ou nada.

Regras:

- **Quem pode.** Qualquer papel na área do robô vê os lotes dele; só quem opera
  ou administra a área (:func:`rapa.core.contracts.can_automate`) confere e cria.
- **Prévia.** :meth:`PgBatches.preview_batch` lê a planilha
  (:mod:`rapa.control.services.sheets`), casa as colunas com as entradas da versão
  publicada e confere todas as linhas, sem criar nada. A planilha nunca é guardada.
- **Tudo ou nada.** :meth:`PgBatches.create_batch` lê e confere de novo; se uma
  linha tiver erro, nada é criado e a resposta diz quais linhas e por quê (sem os
  valores). Se a fila da empresa não comportar todas as linhas, também nada é
  criado. Senão, numa transação só: uma execução por linha (origem ``batch``, a
  menor prioridade), o lote, as linhas e a auditoria. Todas as execuções usam a
  mesma versão (a publicada na hora da criação); se outra versão for publicada
  entre a leitura e a transação, a criação é recusada para conferir de novo.
- **Idempotência.** Com ``idempotency_key`` (o ``Idempotency-Key`` do REST), o
  mesmo pedido de novo devolve o mesmo lote; com outro arquivo ou robô, é conflito.
- **Andamento e resultado.** O lote não tem situação própria: o andamento é a soma
  das situações das execuções dele. O resultado (CSV ou xlsx) traz uma linha por
  linha do lote com a situação, só as saídas que o robô declara e o erro; nunca as
  entradas. Uma linha que falhou não é repetida sozinha.
"""

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, and_, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import batch_rows, batches, robot_versions, robots, runs
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import PgService, ensure_company_active, visible_areas
from rapa.control.services.robots import published_robot
from rapa.control.services.runs import enqueue_many, lock_company_starts
from rapa.control.services.sheets import (
    CSV_MEDIA_TYPE,
    MAX_ROW_ERRORS_SHOWN,
    SAMPLE_ROWS,
    XLSX_MEDIA_TYPE,
    RowPlan,
    Sheet,
    SheetLimits,
    plan_rows,
    read_sheet,
    render_results,
)
from rapa.control.validation import InputValidator
from rapa.core.contracts import (
    MAX_BATCHES_LISTED,
    BatchFile,
    BatchFormat,
    BatchInfo,
    BatchPreview,
    BatchRow,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    RunState,
    RunTrigger,
    can_automate,
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = ["BATCH_SCOPE", "PgBatches", "clean_file_name"]

#: Escopo das chaves de idempotência dos lotes.
BATCH_SCOPE = "lote"
#: Quantos erros de linha a recusa da criação lista.
_MAX_ERRORS_IN_REFUSAL = 20
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]+")


def clean_file_name(name: str) -> str:
    """Só o nome do arquivo, sem pastas nem caracteres de controle (até 200 caracteres)."""
    base = re.split(r"[/\\]", name or "")[-1]
    cleaned = " ".join(_UNSAFE_NAME.sub(" ", base).split())
    return cleaned[:200] or "planilha"


def _select() -> Select[Any]:
    return select(
        batches,
        robots.c.slug.label("robot_slug"),
        robots.c.area_id,
        robot_versions.c.version_no,
        robot_versions.c.manifest,
    ).select_from(
        batches.join(
            robots,
            and_(robots.c.company_id == batches.c.company_id, robots.c.id == batches.c.robot_id),
        ).join(robot_versions, robot_versions.c.id == batches.c.version_id)
    )


def _info(row: RowMapping, counts: Mapping[RunState, int]) -> BatchInfo:
    return BatchInfo(
        batch_id=row["id"],
        company_id=row["company_id"],
        area_id=row["area_id"],
        robot_id=row["robot_id"],
        robot_slug=row["robot_slug"],
        version_no=row["version_no"],
        file_name=row["file_name"],
        format=BatchFormat(row["file_format"]),
        total=row["total_rows"],
        counts=dict(counts),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _refusal(plan: RowPlan) -> InvalidInput:
    details = list(plan.file_errors)
    for error in plan.errors[:_MAX_ERRORS_IN_REFUSAL]:
        details += [f"linha {error.line}: {message}" for message in error.errors]
    if len(plan.errors) > _MAX_ERRORS_IN_REFUSAL:
        details.append(f"e mais {len(plan.errors) - _MAX_ERRORS_IN_REFUSAL} linhas com erro")
    return InvalidInput("o lote tem erros; nada foi criado", details)


class PgBatches(PgService):
    """Lotes por planilha no PostgreSQL (:class:`rapa.core.contracts.BatchService`)."""

    def __init__(
        self,
        db: Database,
        *,
        limits: SheetLimits | None = None,
        validator: InputValidator | None = None,
    ) -> None:
        super().__init__(db)
        self.limits = limits or SheetLimits()
        self.validator = validator or InputValidator()

    async def _robot(
        self, session: AsyncSession, principal: Principal, robot_id: UUID
    ) -> RowMapping:
        row: RowMapping | None = (
            (
                await session.execute(
                    select(robots.c.id, robots.c.area_id, robots.c.slug).where(
                        robots.c.company_id == principal.company_id, robots.c.id == robot_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("robô não encontrado")
        return row

    async def _published_for(
        self, session: AsyncSession, principal: Principal, robot_id: UUID
    ) -> RowMapping:
        robot = await self._robot(session, principal, robot_id)
        if not can_automate(principal, robot["area_id"]):
            raise Forbidden("só quem opera robôs nesta área pode criar lotes")
        published = await published_robot(session, principal.company_id, robot_id)
        if published is None:
            raise Conflict("o robô precisa estar publicado para rodar em lote")
        return published

    async def _plan(
        self, sheet: Sheet, manifest: Manifest, columns: Mapping[str, str] | None
    ) -> RowPlan:
        return await asyncio.to_thread(plan_rows, sheet, manifest, self.validator, columns)

    async def _read(self, file_name: str, data: bytes) -> Sheet:
        return await asyncio.to_thread(read_sheet, file_name, data, self.limits)

    async def preview_batch(
        self,
        principal: Principal,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
    ) -> BatchPreview:
        async with self.db.tenant_session(principal.company_id) as session:
            robot = await self._published_for(session, principal, robot_id)
        sheet = await self._read(file_name, data)
        plan = await self._plan(sheet, Manifest.model_validate(robot["manifest"]), columns)
        return BatchPreview(
            robot_id=robot_id,
            robot_slug=robot["slug"],
            version_no=robot["version_no"],
            file_name=clean_file_name(file_name),
            format=sheet.format,
            file_sha256=hashlib.sha256(data).hexdigest(),
            rows=len(sheet.rows),
            columns=plan.columns,
            file_errors=plan.file_errors,
            errors=plan.errors[:MAX_ROW_ERRORS_SHOWN],
            error_rows=len(plan.errors),
            sample=[inputs for _, inputs in plan.rows[:SAMPLE_ROWS]],
        )

    async def create_batch(
        self,
        principal: Principal,
        robot_id: UUID,
        file_name: str,
        data: bytes,
        columns: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> BatchInfo:
        company_id = principal.company_id
        digest = hashlib.sha256(data).hexdigest()
        if idempotency_key is not None:
            done = await self._existing(principal, idempotency_key, robot_id, digest)
            if done is not None:
                return done
        async with self.db.tenant_session(company_id) as session:
            # Empresa congelada: recusa antes de ler a planilha.
            await ensure_company_active(session, company_id)
            robot = await self._published_for(session, principal, robot_id)
        sheet = await self._read(file_name, data)
        manifest = Manifest.model_validate(robot["manifest"])
        plan = await self._plan(sheet, manifest, columns)
        if not plan.ok:
            raise _refusal(plan)
        batch_id = new_id()
        name = clean_file_name(file_name)
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            await lock_company_starts(session, company_id)
            if idempotency_key is not None:
                done = await self._existing_in(
                    session, principal, idempotency_key, robot_id, digest
                )
                if done is not None:
                    return done
            current = await self._published_for(session, principal, robot_id)
            if current["version_id"] != robot["version_id"]:
                raise Conflict(
                    "o robô ganhou uma versão nova enquanto o lote era conferido; confira de novo"
                )
            run_ids = await enqueue_many(
                session,
                company_id=company_id,
                requested_by=principal.subject,
                robot=current,
                slug=current["slug"],
                inputs=[inputs for _, inputs in plan.rows],
                trigger=RunTrigger.BATCH,
                audit_extra={"lote": str(batch_id)},
            )
            await session.execute(
                insert(batches).values(
                    company_id=company_id,
                    id=batch_id,
                    robot_id=robot_id,
                    version_id=current["version_id"],
                    file_name=name,
                    file_format=sheet.format.value,
                    file_sha256=digest,
                    total_rows=len(run_ids),
                    columns={c.header: c.input for c in plan.columns if c.input is not None},
                    request_key=idempotency_key,
                    created_by=principal.subject,
                )
            )
            await session.execute(
                insert(batch_rows),
                [
                    {
                        "company_id": company_id,
                        "batch_id": batch_id,
                        "row_no": number,
                        "line_no": line,
                        "run_id": run_id,
                    }
                    for number, ((line, _), run_id) in enumerate(
                        zip(plan.rows, run_ids, strict=True), start=1
                    )
                ],
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="lote.criado",
                target=f"lote:{batch_id}",
                payload={
                    "robo": current["slug"],
                    "versao": current["version_no"],
                    "arquivo": digest,
                    "formato": sheet.format.value,
                    "linhas": len(run_ids),
                },
            )
            info = await self._get(session, principal, batch_id)
        if info is None:  # pragma: no cover - acabou de ser incluído
            raise NotFound("lote não encontrado")
        return info

    async def _existing(
        self, principal: Principal, key: str, robot_id: UUID, digest: str
    ) -> BatchInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            return await self._existing_in(session, principal, key, robot_id, digest)

    async def _existing_in(
        self, session: AsyncSession, principal: Principal, key: str, robot_id: UUID, digest: str
    ) -> BatchInfo | None:
        row = (
            await session.execute(
                select(batches.c.id, batches.c.robot_id, batches.c.file_sha256).where(
                    batches.c.company_id == principal.company_id, batches.c.request_key == key
                )
            )
        ).one_or_none()
        if row is None:
            return None
        if row[1] != robot_id or row[2] != digest:
            raise Conflict("a chave de idempotência já foi usada com outro lote")
        return await self._get(session, principal, row[0])

    async def _counts(
        self, session: AsyncSession, company_id: UUID, batch_ids: Sequence[UUID]
    ) -> dict[UUID, dict[RunState, int]]:
        out: dict[UUID, dict[RunState, int]] = defaultdict(dict)
        if not batch_ids:
            return out
        rows = (
            await session.execute(
                select(batch_rows.c.batch_id, runs.c.state, func.count())
                .select_from(
                    batch_rows.join(
                        runs,
                        and_(
                            runs.c.company_id == batch_rows.c.company_id,
                            runs.c.id == batch_rows.c.run_id,
                        ),
                    )
                )
                .where(batch_rows.c.company_id == company_id, batch_rows.c.batch_id.in_(batch_ids))
                .group_by(batch_rows.c.batch_id, runs.c.state)
            )
        ).all()
        for batch_id, state, count in rows:
            out[batch_id][RunState(state)] = int(count)
        return out

    async def _row(
        self, session: AsyncSession, principal: Principal, batch_id: UUID
    ) -> RowMapping | None:
        row: RowMapping | None = (
            (
                await session.execute(
                    _select().where(
                        batches.c.company_id == principal.company_id, batches.c.id == batch_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not principal.roles_in(row["area_id"]):
            return None
        return row

    async def _get(
        self, session: AsyncSession, principal: Principal, batch_id: UUID
    ) -> BatchInfo | None:
        row = await self._row(session, principal, batch_id)
        if row is None:
            return None
        counts = await self._counts(session, principal.company_id, [batch_id])
        return _info(row, counts.get(batch_id, {}))

    async def list_batches(self, principal: Principal, limit: int = 50) -> list[BatchInfo]:
        limit = max(1, min(limit, MAX_BATCHES_LISTED))
        query = (
            _select()
            .where(batches.c.company_id == principal.company_id)
            .order_by(batches.c.created_at.desc(), batches.c.id.desc())
            .limit(limit)
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(robots.c.area_id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
            counts = await self._counts(session, principal.company_id, [row["id"] for row in rows])
        return [_info(row, counts.get(row["id"], {})) for row in rows]

    async def get_batch(self, principal: Principal, batch_id: UUID) -> BatchInfo | None:
        async with self.db.tenant_session(principal.company_id) as session:
            return await self._get(session, principal, batch_id)

    async def _rows_and_manifest(
        self, principal: Principal, batch_id: UUID
    ) -> tuple[RowMapping, list[BatchRow]]:
        async with self.db.tenant_session(principal.company_id) as session:
            batch = await self._row(session, principal, batch_id)
            if batch is None:
                raise NotFound("lote não encontrado")
            records = (
                (
                    await session.execute(
                        select(
                            batch_rows.c.row_no,
                            batch_rows.c.line_no,
                            batch_rows.c.run_id,
                            runs.c.state,
                            runs.c.started_at,
                            runs.c.finished_at,
                            runs.c.outputs,
                            runs.c.error_code,
                            runs.c.error_message,
                        )
                        .select_from(
                            batch_rows.join(
                                runs,
                                and_(
                                    runs.c.company_id == batch_rows.c.company_id,
                                    runs.c.id == batch_rows.c.run_id,
                                ),
                            )
                        )
                        .where(
                            batch_rows.c.company_id == principal.company_id,
                            batch_rows.c.batch_id == batch_id,
                        )
                        .order_by(batch_rows.c.row_no)
                    )
                )
                .mappings()
                .all()
            )
        declared = {output.name for output in Manifest.model_validate(batch["manifest"]).outputs}
        rows = [
            BatchRow(
                row_no=record["row_no"],
                line=record["line_no"],
                run_id=record["run_id"],
                state=RunState(record["state"]),
                started_at=record["started_at"],
                finished_at=record["finished_at"],
                outputs=(
                    {k: v for k, v in record["outputs"].items() if k in declared}
                    if isinstance(record["outputs"], dict)
                    else None
                ),
                error_code=record["error_code"],
                error_message=record["error_message"],
            )
            for record in records
        ]
        return batch, rows

    async def list_batch_rows(self, principal: Principal, batch_id: UUID) -> list[BatchRow]:
        _, rows = await self._rows_and_manifest(principal, batch_id)
        return rows

    async def batch_results(
        self, principal: Principal, batch_id: UUID, format: BatchFormat
    ) -> BatchFile:
        batch, rows = await self._rows_and_manifest(principal, batch_id)
        outputs = Manifest.model_validate(batch["manifest"]).outputs
        content = await asyncio.to_thread(render_results, rows, outputs, format)
        stem = f"lote-{str(batch_id)[:8]}-resultado"
        if format is BatchFormat.XLSX:
            return BatchFile(file_name=f"{stem}.xlsx", media_type=XLSX_MEDIA_TYPE, content=content)
        return BatchFile(file_name=f"{stem}.csv", media_type=CSV_MEDIA_TYPE, content=content)
