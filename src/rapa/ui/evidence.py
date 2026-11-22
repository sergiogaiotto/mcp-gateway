"""Evidências de uma execução na tela: as telas, o vídeo da falha e os arquivos baixados.

As telas aparecem em miniatura (clicar abre grande), o vídeo da falha toca na própria
página e os arquivos baixados viram links. Os arquivos passam pela rota
``/execucoes/{id}/artefatos/{nome}`` da própria interface, que confere a sessão a
cada pedido (o cookie da sessão, nunca um token no navegador) e lê pelo backend no
nome de quem está logado: a regra de quem vê é a do plano de controle (quem vê a
execução vê os artefatos). Para outra área ou empresa, a resposta é 404; um artefato
que passou do prazo de retenção aparece como "expirado" e não abre (410). Todo texto
entra por ``ui.label`` (escapado).
"""

import logging
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from nicegui import ui

from rapa.core.artifacts import SCREENSHOT, VIDEO, artifact_headers
from rapa.core.contracts import (
    ArtifactStatus,
    ContractError,
    Forbidden,
    Gone,
    NotFound,
    RunArtifact,
    RunStatus,
)
from rapa.ui.backend import BackendError, BackendUnavailable, NotAuthenticated, describe_error
from rapa.ui.context import UiContext, Viewer
from rapa.ui.labels import format_datetime

__all__ = ["artifact_url", "evidence_card", "evidence_router"]

logger = logging.getLogger(__name__)

_VIDEO_NOTE = (
    "O vídeo mostra a tela como ela estava: campos de senha aparecem como pontos, mas os "
    "dados digitados em campos comuns aparecem."
)


def artifact_url(run_id: UUID, name: str) -> str:
    """Endereço, na interface, de um artefato da execução."""
    return f"/execucoes/{run_id}/artefatos/{quote(name, safe='')}"


def _plain(status: int, text: str) -> Response:
    return Response(
        text,
        status_code=status,
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def evidence_router(ctx: UiContext) -> APIRouter:
    """A rota que entrega os arquivos das evidências para a sessão logada."""
    router = APIRouter()

    @router.get("/execucoes/{run_id}/artefatos/{name}", include_in_schema=False)
    async def artifact(request: Request, run_id: str, name: str) -> Response:
        try:
            run_uuid = UUID(run_id)
        except ValueError:
            return _plain(404, "Execução não encontrada.")
        try:
            viewer = await ctx.viewer(request)
            if viewer is None:
                return _plain(401, "Entre de novo para ver as evidências.")
            backend = await viewer.backend()
            content = await backend.open_artifact(run_uuid, name)
        except NotAuthenticated:
            return _plain(401, "Entre de novo para ver as evidências.")
        except BackendUnavailable as exc:
            return _plain(503, describe_error(exc)[0])
        except Gone as exc:
            return _plain(410, describe_error(exc)[0])
        except Forbidden as exc:
            return _plain(403, describe_error(exc)[0])
        except NotFound as exc:
            return _plain(404, describe_error(exc)[0])
        except (ContractError, BackendError) as exc:
            return _plain(400, describe_error(exc)[0])
        return StreamingResponse(
            content.iterate(),
            media_type=content.media_type,
            headers=artifact_headers(content.name, inline=content.inline, size=content.size),
        )

    return router


def _size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB".replace(".", ",")
    return f"{size / (1024 * 1024):.1f} MB".replace(".", ",")


def _show_large(url: str, name: str) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl").mark("tela-grande"):
        ui.label(name).classes("text-caption")
        ui.image(url).classes("w-full").props("fit=contain")
        ui.button("Fechar", on_click=dialog.close).props("flat").mark("fechar-tela")
    dialog.open()


def _screenshots(run_id: UUID, shots: list[RunArtifact]) -> None:
    ui.label("Telas").classes("text-weight-medium")
    with ui.row().classes("gap-3 flex-wrap").mark("telas"):
        for shot in shots:
            url = artifact_url(run_id, shot.name)
            with ui.column().classes("gap-1 items-center"):
                if shot.status is ArtifactStatus.EXPIRED:
                    ui.label("expirada").classes("text-caption text-grey-7").mark(
                        f"tela-expirada-{shot.name}"
                    )
                else:
                    image = (
                        ui.image(url)
                        .classes("w-48 h-32 cursor-pointer rounded border")
                        .props("fit=contain")
                        .mark(f"tela-{shot.name}")
                    )
                    image.on("click", lambda u=url, n=shot.name: _show_large(u, n))
                ui.label(shot.name).classes("text-caption break-all w-48")


def _videos(run_id: UUID, videos: list[RunArtifact]) -> None:
    ui.label("Vídeo da falha").classes("text-weight-medium")
    for video in videos:
        if video.status is ArtifactStatus.EXPIRED:
            ui.label(f"{video.name}: expirado").classes("text-caption text-grey-7").mark(
                "video-expirado"
            )
            continue
        ui.video(artifact_url(run_id, video.name), muted=True).classes("w-full max-w-3xl").mark(
            "video-falha"
        )
    ui.label(_VIDEO_NOTE).classes("text-caption")


def _files(run_id: UUID, files: list[RunArtifact]) -> None:
    ui.label("Arquivos").classes("text-weight-medium")
    with ui.column().classes("gap-1").mark("arquivos"):
        for item in files:
            with ui.row().classes("items-center gap-2"):
                if item.status is ArtifactStatus.EXPIRED:
                    ui.label(item.name)
                    ui.label("expirado").classes("text-caption text-grey-7")
                else:
                    ui.link(item.name, artifact_url(run_id, item.name)).mark(f"arquivo-{item.name}")
                ui.label(f"{_size(item.size)} · {format_datetime(item.created_at)}").classes(
                    "text-caption"
                )


async def evidence_card(viewer: Viewer, status: RunStatus) -> None:
    """O cartão das evidências de uma execução que terminou."""
    with ui.card().classes("w-full").mark("evidencias"):
        ui.label("Evidências").classes("text-subtitle1")
        try:
            backend = await viewer.backend()
            artifacts = await backend.list_artifacts(status.run_id)
        except NotAuthenticated:
            raise
        except BackendUnavailable as exc:
            ui.label(describe_error(exc)[0]).classes("text-caption").mark("sem-evidencias")
            return
        except (ContractError, BackendError) as exc:
            ui.label(describe_error(exc)[0]).classes("text-negative").mark("erro-evidencias")
            return
        if not artifacts:
            ui.label("Esta execução não guardou telas, vídeo nem arquivos.").mark("sem-evidencias")
            return
        expired = [item.status is ArtifactStatus.EXPIRED for item in artifacts]
        if all(expired):
            ui.label(
                "As evidências desta execução passaram do prazo de retenção e foram apagadas."
            ).classes("text-caption text-grey-8").mark("evidencias-expiradas")
        elif any(expired):
            # O prazo curto de uma execução que deu certo: as telas saem, os arquivos ficam.
            ui.label(
                "As telas desta execução passaram do prazo de retenção e foram apagadas; "
                "os arquivos baixados ficam até o prazo das evidências."
            ).classes("text-caption text-grey-8").mark("capturas-expiradas")
        shots = [item for item in artifacts if item.kind == SCREENSHOT]
        videos = [item for item in artifacts if item.kind == VIDEO]
        files = [item for item in artifacts if item.kind not in (SCREENSHOT, VIDEO)]
        if videos:
            _videos(status.run_id, videos)
        if shots:
            _screenshots(status.run_id, shots)
        if files:
            _files(status.run_id, files)
