"""As evidências de uma execução no PostgreSQL (:class:`ArtifactService`).

Quem vê a execução vê os artefatos dela: a mesma regra de
:meth:`rapa.control.services.runs.PgRunService.get` (algum papel na área da
execução, ou um papel da empresa). Para outra empresa ou outra área, a execução não
existe (``NotFound``). Um nome com barra, com ``..``, fora da regra dos nomes ou que
não está na lista da execução também não existe. Um artefato que passou do prazo de
retenção aparece na lista como ``expirado`` e não é mais servido (``Gone``, HTTP
410): todos, com ``runs.artifacts_expired_at``; só as telas e o vídeo
(:func:`rapa.core.artifacts.is_capture`), com ``runs.captures_expired_at`` (o prazo
curto de uma execução que deu certo, em que os arquivos baixados ficam).

A lista vem de ``runs.artifacts`` (só a forma registrada pelo despachante: nome,
tipo, tamanho, SHA-256, tentativa e data; nunca o caminho). O arquivo vem do
:class:`rapa.control.artifacts.ArtifactStore`, pela empresa, a execução e a
tentativa da lista: nada que venha do pedido escolhe a pasta.
"""

import logging
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import RowMapping, select

from rapa.control.artifacts import ArtifactInfo, ArtifactMissing, ArtifactStore
from rapa.control.db.schema import runs
from rapa.control.db.session import Database
from rapa.control.services._common import PgService, visible_areas
from rapa.core.artifacts import is_capture, media_type_for, valid_artifact_name
from rapa.core.contracts import (
    ArtifactContent,
    ArtifactStatus,
    Gone,
    NotFound,
    Principal,
    RunArtifact,
)

__all__ = ["PgArtifacts"]

logger = logging.getLogger(__name__)

_RUN_NOT_FOUND = "execução não encontrada"
_FILE_NOT_FOUND = "arquivo não encontrado nesta execução"


class PgArtifacts(PgService):
    """Lista e entrega os artefatos das execuções que o principal vê."""

    def __init__(self, db: Database, store: ArtifactStore) -> None:
        super().__init__(db)
        self.store = store

    async def _run(self, principal: Principal, run_id: UUID) -> RowMapping:
        async with self.db.tenant_session(principal.company_id) as session:
            row = (
                (
                    await session.execute(
                        select(
                            runs.c.area_id,
                            runs.c.attempt,
                            runs.c.artifacts,
                            runs.c.artifacts_expired_at,
                            runs.c.captures_expired_at,
                        ).where(runs.c.id == run_id, runs.c.company_id == principal.company_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        allowed = visible_areas(principal)
        if row is None or (allowed is not None and row["area_id"] not in allowed):
            raise NotFound(_RUN_NOT_FOUND)
        return row

    @staticmethod
    def _infos(row: RowMapping) -> list[ArtifactInfo]:
        infos: list[ArtifactInfo] = []
        for raw in row["artifacts"] or []:
            try:
                info = ArtifactInfo.model_validate(raw)
            except ValidationError:
                logger.warning("artefato fora do formato numa execução; fica de fora")
                continue
            if valid_artifact_name(info.name):
                infos.append(info)
        return infos

    @staticmethod
    def _status(row: RowMapping, info: ArtifactInfo) -> ArtifactStatus:
        """Se o arquivo ainda é servido: as marcas da retenção, pelo tipo do artefato."""
        if row["artifacts_expired_at"] is not None or (
            row["captures_expired_at"] is not None and is_capture(info.kind)
        ):
            return ArtifactStatus.EXPIRED
        return ArtifactStatus.AVAILABLE

    async def list_artifacts(self, principal: Principal, run_id: UUID) -> list[RunArtifact]:
        row = await self._run(principal, run_id)
        return [
            RunArtifact(
                name=info.name,
                kind=info.kind,
                size=info.size,
                sha256=info.sha256,
                media_type=media_type_for(info.kind, info.name)[0],
                created_at=info.created_at,
                status=self._status(row, info),
            )
            for info in self._infos(row)
        ]

    async def open_artifact(self, principal: Principal, run_id: UUID, name: str) -> ArtifactContent:
        row = await self._run(principal, run_id)
        if not valid_artifact_name(name):
            raise NotFound(_FILE_NOT_FOUND)
        info = next((item for item in self._infos(row) if item.name == name), None)
        if info is None:
            raise NotFound(_FILE_NOT_FOUND)
        if self._status(row, info) is ArtifactStatus.EXPIRED:
            raise Gone("o arquivo passou do prazo de retenção e foi apagado")
        try:
            reader = await self.store.open(
                principal.company_id, run_id, info.attempt or int(row["attempt"]), info.name
            )
        except ArtifactMissing:
            raise NotFound("o arquivo desta execução não está mais guardado") from None
        media_type, inline = media_type_for(info.kind, info.name)
        return ArtifactContent(
            name=info.name,
            media_type=media_type,
            inline=inline,
            size=reader.size,
            chunks=reader.chunks(),
            close=reader.aclose,
        )
