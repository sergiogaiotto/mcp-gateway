"""Rotas das evidências: ``/v1/execucoes/{id}/artefatos``.

- ``GET /v1/execucoes/{id}/artefatos`` lista os artefatos da execução (nome, tipo,
  tamanho, SHA-256, tipo do arquivo, data e situação: ``disponivel`` ou ``expirado``).
- ``GET /v1/execucoes/{id}/artefatos/{nome}`` entrega o arquivo, em pedaços, com o
  ``Content-Type`` dele; ``Content-Disposition: inline`` só para a tela PNG e o vídeo
  WebM, ``attachment`` para todo o resto; sempre ``X-Content-Type-Options: nosniff``
  e ``Cache-Control: private, no-store``.

Quem vê a execução vê os artefatos (a regra de ``GET /v1/execucoes/{id}``). Para
outra área ou outra empresa, a execução não existe (404); um nome com barra, com
``..`` ou que não está na lista da execução também não (404); um artefato que passou
do prazo de retenção responde 410. Sem o serviço configurado, 503. Nada disso entra
no MCP.
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from rapa.core.artifacts import artifact_headers
from rapa.core.contracts import (
    ArtifactContent,
    ArtifactService,
    Principal,
    RunArtifact,
    ServiceUnavailable,
)

__all__ = ["add_artifact_routes", "artifact_response"]

_NO_STORE = "private, no-store"


def artifact_response(content: ArtifactContent) -> StreamingResponse:
    """A resposta que entrega um artefato aberto, com os cabeçalhos de proteção."""
    return StreamingResponse(
        content.iterate(),
        media_type=content.media_type,
        headers=artifact_headers(content.name, inline=content.inline, size=content.size),
    )


def add_artifact_routes(
    router: APIRouter, service: ArtifactService | None, caller: Callable[..., Any]
) -> None:
    """Acrescenta as rotas das evidências ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def artifacts() -> ArtifactService:
        if service is None:
            raise ServiceUnavailable(
                "as evidências das execuções não estão disponíveis neste servidor"
            )
        return service

    @router.get("/execucoes/{run_id}/artefatos", response_model=list[RunArtifact])
    async def list_artifacts(
        run_id: UUID, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> list[RunArtifact]:
        listed = await artifacts().list_artifacts(principal, run_id)
        response.headers["Cache-Control"] = _NO_STORE
        return listed

    @router.get(
        "/execucoes/{run_id}/artefatos/{nome}",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/octet-stream": {}}}},
    )
    async def get_artifact(
        run_id: UUID, nome: str, principal: Annotated[Principal, caller_dep]
    ) -> StreamingResponse:
        content = await artifacts().open_artifact(principal, run_id, nome)
        return artifact_response(content)
