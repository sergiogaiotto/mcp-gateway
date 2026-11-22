"""Evidências na tela da execução: telas, vídeo da falha e arquivos, pela sessão logada."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from nicegui import ui

from rapa.core.artifacts import is_capture, media_type_for
from rapa.core.contracts import (
    ArtifactContent,
    ArtifactStatus,
    Gone,
    NotFound,
    Principal,
    RunArtifact,
    RunState,
    RunTrigger,
)
from rapa.ui.evidence import artifact_url
from tests.ui.conftest import World, login, logout

INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}
PNG = b"\x89PNG\r\n\x1a\n" + b"tela" * 8
WEBM = bytes.fromhex("1a45dfa3") + b"video" * 8
PDF = b"%PDF-1.4\n" + b"nota" * 8
SHOT, VIDEO, FILE = "tela-001-entrar-falha.png", "video-falha.webm", "nota-001.pdf"

pytestmark = pytest.mark.usefixtures("no_error_logs")


def _artifact(name: str, kind: str, data: bytes, *, expired: bool) -> RunArtifact:
    import hashlib

    return RunArtifact(
        name=name,
        kind=kind,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type_for(kind, name)[0],
        created_at=datetime(2026, 11, 20, 13, 0, tzinfo=UTC),
        status=ArtifactStatus.EXPIRED if expired else ArtifactStatus.AVAILABLE,
    )


class MemoryArtifacts:
    """As evidências em memória, com a mesma regra de quem vê (a da execução)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.files: dict[UUID, dict[str, tuple[str, bytes]]] = {}
        self.expired: set[UUID] = set()
        #: Execuções que deram certo cujas telas e vídeo expiraram (os downloads ficam).
        self.captures_expired: set[UUID] = set()

    def _expired(self, run_id: UUID, kind: str) -> bool:
        return run_id in self.expired or (run_id in self.captures_expired and is_capture(kind))

    async def _visible(self, principal: Principal, run_id: UUID) -> None:
        if await self.world.platform.get(principal, run_id) is None:
            raise NotFound("execução não encontrada")

    async def list_artifacts(self, principal: Principal, run_id: UUID) -> list[RunArtifact]:
        await self._visible(principal, run_id)
        return [
            _artifact(name, kind, data, expired=self._expired(run_id, kind))
            for name, (kind, data) in self.files.get(run_id, {}).items()
        ]

    async def open_artifact(self, principal: Principal, run_id: UUID, name: str) -> ArtifactContent:
        await self._visible(principal, run_id)
        found = self.files.get(run_id, {}).get(name)
        if found is None:
            raise NotFound("arquivo não encontrado nesta execução")
        kind, data = found
        if self._expired(run_id, kind):
            raise Gone("o arquivo passou do prazo de retenção e foi apagado")
        media_type, inline = media_type_for(kind, name)

        async def chunks() -> AsyncIterator[bytes]:
            yield data[:10]
            yield data[10:]

        return ArtifactContent(
            name=name, media_type=media_type, inline=inline, size=len(data), chunks=chunks()
        )


async def _failed_run(world: World, artifacts: MemoryArtifacts) -> UUID:
    run = await world.platform.start(
        world.principals["operador"], "emitir_protocolo", INPUTS, RunTrigger.MANUAL
    )
    world.platform.set_run_state(
        run.run_id, RunState.FAILED, error_code="elemento_nao_encontrado", error_message="Falhou."
    )
    artifacts.files[run.run_id] = {
        SHOT: ("screenshot", PNG),
        VIDEO: ("video", WEBM),
        FILE: ("download", PDF),
    }
    return run.run_id


async def test_failed_run_shows_screenshots_video_and_files(world: World) -> None:
    artifacts = MemoryArtifacts(world)
    world.artifacts = artifacts
    run_id = await _failed_run(world, artifacts)
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{run_id}")
        await user.should_see(marker="evidencias")
        await user.should_see("Vídeo da falha")
        image = user.find(marker=f"tela-{SHOT}").elements.pop()
        assert isinstance(image, ui.image) and image.source == artifact_url(run_id, SHOT)
        video = user.find(marker="video-falha").elements.pop()
        assert isinstance(video, ui.video) and video.props["src"] == artifact_url(run_id, VIDEO)
        link = user.find(marker=f"arquivo-{FILE}").elements.pop()
        assert isinstance(link, ui.link) and link.props["href"] == artifact_url(run_id, FILE)
        await user.should_see("campos de senha aparecem como pontos")
        # Clicar na miniatura abre a tela grande.
        user.find(marker=f"tela-{SHOT}").click()
        await user.should_see(marker="tela-grande")

        # O arquivo vem pela rota da interface, com a sessão do cookie.
        got = await user.http_client.get(artifact_url(run_id, SHOT))
        assert got.status_code == 200 and got.content == PNG
        assert got.headers["content-type"] == "image/png"
        assert got.headers["content-disposition"] == f'inline; filename="{SHOT}"'
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == "private, no-store"
        pdf = await user.http_client.get(artifact_url(run_id, FILE))
        assert pdf.status_code == 200 and pdf.content == PDF
        assert pdf.headers["content-disposition"] == f'attachment; filename="{FILE}"'
        for path, status in (
            (artifact_url(run_id, "outra.png"), 404),
            (f"/execucoes/nao-e-um-numero/artefatos/{SHOT}", 404),
        ):
            assert (await user.http_client.get(path)).status_code == status, path

        # Outra empresa não vê a execução nem os arquivos dela.
        await logout(user)
        await login(user, "outra", f"/execucoes/{run_id}")
        await user.should_see("Execução não encontrada.")
        assert (await user.http_client.get(artifact_url(run_id, SHOT))).status_code == 404
        await logout(user)
        assert (await user.http_client.get(artifact_url(run_id, SHOT))).status_code == 401


async def test_expired_evidence_is_shown_as_expired_and_not_served(world: World) -> None:
    artifacts = MemoryArtifacts(world)
    world.artifacts = artifacts
    run_id = await _failed_run(world, artifacts)
    artifacts.expired.add(run_id)
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{run_id}")
        await user.should_see(marker="evidencias-expiradas")
        await user.should_see(marker=f"tela-expirada-{SHOT}")
        await user.should_see(marker="video-expirado")
        await user.should_not_see(marker=f"tela-{SHOT}")
        got = await user.http_client.get(artifact_url(run_id, VIDEO))
        assert got.status_code == 410 and WEBM not in got.content


async def test_expired_captures_keep_the_downloaded_files(world: World) -> None:
    """O prazo curto de uma execução que deu certo: as telas saem, os arquivos ficam."""
    artifacts = MemoryArtifacts(world)
    world.artifacts = artifacts
    run_id = await _failed_run(world, artifacts)
    artifacts.captures_expired.add(run_id)
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{run_id}")
        await user.should_see(marker="capturas-expiradas")
        await user.should_not_see(marker="evidencias-expiradas")
        await user.should_see(marker=f"tela-expirada-{SHOT}")
        await user.should_see(marker="video-expirado")
        link = user.find(marker=f"arquivo-{FILE}").elements.pop()
        assert isinstance(link, ui.link) and link.props["href"] == artifact_url(run_id, FILE)
        pdf = await user.http_client.get(artifact_url(run_id, FILE))
        assert pdf.status_code == 200 and pdf.content == PDF
        assert (await user.http_client.get(artifact_url(run_id, SHOT))).status_code == 410


async def test_evidence_appears_when_the_run_finishes(world: World) -> None:
    artifacts = MemoryArtifacts(world)
    world.artifacts = artifacts
    run = await world.platform.start(
        world.principals["operador"], "emitir_protocolo", INPUTS, RunTrigger.MANUAL
    )
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{run.run_id}")
        await user.should_see("Na fila")
        await user.should_not_see(marker="evidencias")
        world.platform.set_run_state(run.run_id, RunState.FAILED, error_code="x")
        artifacts.files[run.run_id] = {SHOT: ("screenshot", PNG)}
        await user.should_see(marker=f"tela-{SHOT}", retries=100)


async def test_without_the_service_the_run_page_still_opens(world: World) -> None:
    run_id = await _failed_run(world, MemoryArtifacts(world))
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{run_id}")
        await user.should_see("Falhou")
        await user.should_see(marker="sem-evidencias")
        await user.should_see("não estão disponíveis neste servidor")
        await user.should_not_see(marker="erro")
        got = await user.http_client.get(artifact_url(run_id, SHOT))
        assert got.status_code == 503


async def test_rest_backend_keeps_only_png_and_webm_inline() -> None:
    from rapa.ui.backend import RestBackend

    async def api(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/pagina.html"):
            return httpx2.Response(
                200,
                content=b"<script>",
                headers={"content-type": "text/html", "content-disposition": "inline"},
            )
        return httpx2.Response(
            200,
            content=PNG,
            headers={"content-type": "image/png", "content-disposition": "inline; x"},
        )

    client = httpx2.AsyncClient(base_url="http://api.test", transport=httpx2.MockTransport(api))
    backend = RestBackend(client, "token")
    run_id = UUID(int=1)
    html = await backend.open_artifact(run_id, "pagina.html")
    assert not html.inline
    await html.aclose()
    png = await backend.open_artifact(run_id, SHOT)
    assert png.inline and await png.read() == PNG
    with pytest.raises(NotFound):
        await backend.open_artifact(run_id, "../segredo")
