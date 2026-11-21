"""Hospedeiro da sessão com o Chromium e o portal de teste: canal, tela, entradas e fim."""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER, current_totp
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.recorder import InputEvent, Recording
from rapa.session.host import EndReason, HostConfig, SessionHost
from tests.session.support import LiveClient

pytestmark = pytest.mark.browser

JPEG = b"\xff\xd8"


@asynccontextmanager
async def running_host(
    chromium: str, start_url: str, tmp_path: Path, **overrides: Any
) -> AsyncIterator[tuple[SessionHost, str, asyncio.Task[EndReason]]]:
    token = secrets.token_urlsafe(32)
    config = HostConfig(
        token=token,
        start_url=start_url,
        chromium=chromium,
        browser_args=overrides.pop("browser_args", (HOST_RESOLVER_ARG,)),
        width=1024,
        height=700,
        profile_parent=str(tmp_path),
        **overrides,
    )
    host = SessionHost(config)
    ready = asyncio.Event()
    task = asyncio.create_task(host.run(lambda _port: ready.set()))
    await asyncio.wait_for(ready.wait(), timeout=30)
    try:
        yield host, token, task
    finally:
        host.finish(EndReason.SIGNAL)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)


@asynccontextmanager
async def live(host: SessionHost, token: str) -> AsyncIterator[LiveClient]:
    ws = await connect(
        f"ws://127.0.0.1:{host.port}/",
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=None,
    )
    client = LiveClient(ws)
    try:
        yield client
    finally:
        await client.close()


async def test_control_channel_requires_the_session_token_and_one_connection(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with running_host(chromium_path, f"{portal_v1.base_url}/", tmp_path) as (host, token, _):
        url = f"ws://127.0.0.1:{host.port}/"
        for headers in ({}, {"Authorization": "Bearer " + "x" * 43}, {"Authorization": token}):
            with pytest.raises(InvalidStatus) as caught:
                await connect(url, additional_headers=headers)
            assert caught.value.response.status_code == 401
        with pytest.raises(InvalidStatus) as caught:
            await connect(f"{url}outro", additional_headers={"Authorization": f"Bearer {token}"})
        assert caught.value.response.status_code == 404
        async with live(host, token) as client:
            await client.event("pronta")
            with pytest.raises(InvalidStatus) as caught:
                await connect(url, additional_headers={"Authorization": f"Bearer {token}"})
            assert caught.value.response.status_code == 409
        # Depois de a primeira sair, uma nova conexão entra.
        await asyncio.sleep(0.2)
        async with live(host, token) as client:
            await client.event("pronta")


async def test_screen_frames_page_info_and_resize(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with (
        running_host(chromium_path, f"{portal_v1.base_url}/login", tmp_path) as (host, token, _),
        live(host, token) as client,
    ):
        ready = await client.event("pronta")
        assert (ready["largura"], ready["altura"]) == (1024, 700)
        page = await client.page("/login")
        assert page["titulo"].startswith("Entrar")
        await client.wait_for(lambda: client.frames, what="um quadro")
        assert client.frames[0].startswith(JPEG)
        await client.event("tela", largura=1024, altura=700)
        # Rolagem, movimentos e um novo tamanho continuam gerando quadros (cada um confirmado).
        before = host.frames
        await client.send(tipo="tamanho", largura=800, altura=600)
        await client.event("tela", largura=800, altura=600)
        for n in range(20):
            await client.send(tipo="mouse", acao="mover", x=10 + n * 5, y=10 + n * 3)
        await client.wait_for(lambda: host.frames > before, what="mais quadros")
        assert host.page is not None and host.page.viewport_size == {"width": 800, "height": 600}
        # Mensagens fora do formato são ignoradas, sem derrubar o canal.
        await client.ws.send("{nada}")
        await client.send(tipo="navegar", url="javascript:alert(1)")
        await client.wait_for(lambda: host.rejected >= 2, what="as recusas")
        await client.send(tipo="navegar", url=f"{portal_v1.base_url}/")
        await client.event("pagina", url=f"{portal_v1.base_url}/")


async def test_slow_viewer_gets_the_latest_frame_without_queueing(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with running_host(chromium_path, f"{portal_v1.base_url}/login", tmp_path) as (
        host,
        token,
        _,
    ):
        ws = await connect(
            f"ws://127.0.0.1:{host.port}/",
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
            max_queue=1,
        )
        try:
            async with asyncio.timeout(15):
                while not isinstance(await ws.recv(), bytes):
                    pass
            page = host.page
            assert page is not None
            # Ninguém lê enquanto a página muda sem parar: o hospedeiro segue confirmando os
            # quadros ao navegador e guarda no máximo o mais novo, sem fila de imagens velhas.
            before = host.frames
            await page.evaluate(
                "() => { let n = 0; setInterval(() => { document.title = String(n);"
                " document.body.style.background = n++ % 2 ? '#fff' : '#ddd'; }, 30); }"
            )
            await asyncio.sleep(3)
            assert host.frames > before + 5
            received = 0
            async with asyncio.timeout(10):
                while received < 3:
                    message = await ws.recv()
                    if isinstance(message, bytes):
                        assert message.startswith(JPEG)
                        received += 1
        finally:
            await ws.close()


async def test_demonstration_by_input_messages_is_recorded_without_secrets(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with running_host(chromium_path, f"{portal_v1.base_url}/login", tmp_path) as (
        host,
        token,
        task,
    ):
        profile = host.profile_dir
        assert profile is not None and profile.exists()
        async with live(host, token) as client:
            await client.page("/login")
            page = host.page
            assert page is not None
            await client.click_on(page.get_by_label("Usuário"))
            await client.type(DEFAULT_USER)
            await client.click_on(page.get_by_label("Senha"))
            await client.type(DEFAULT_PASSWORD)
            await client.press("Enter")
            await client.page("/login/codigo")
            await client.click_on(page.get_by_label("Código de verificação"))
            await client.type(current_totp())
            await client.click_on(page.get_by_role("button", name="Confirmar"))
            await client.page("/inicio")
            await client.click_on(page.get_by_role("link", name="Emitir protocolo"))
            await client.page("/protocolo")
            await client.click_on(page.get_by_label("Nome completo"))
            await client.type("João da Conceição")
            await client.press("Tab")
            name = page.get_by_label("Nome completo")
            for _ in range(100):
                if await name.input_value() == "João da Conceição":
                    break
                await asyncio.sleep(0.1)
            assert await name.input_value() == "João da Conceição"
            assert await page.evaluate("() => document.activeElement.id") == "cpf"
            await client.send(tipo="parar")
            gravacao = await client.event("gravacao", within=30)
            await client.event("fim", motivo="parada")
        assert await asyncio.wait_for(task, timeout=30) is EndReason.STOPPED
        assert not profile.exists()
    recording = Recording.model_validate(gravacao["gravacao"])
    text = json.dumps(gravacao, ensure_ascii=False)
    for secret in (DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert secret not in text
    typed = [e.value for e in recording.events if isinstance(e, InputEvent)]
    assert "João da Conceição" in typed
    assert any(getattr(e, "cause", None) == "typed" for e in recording.events)


async def test_idle_session_ends_and_destroys_the_profile(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with running_host(
        chromium_path, f"{portal_v1.base_url}/", tmp_path, idle_timeout_s=1.5
    ) as (host, token, task):
        profile = host.profile_dir
        assert profile is not None
        async with live(host, token) as client:
            await client.event("pronta")
            end = await client.event("fim", within=15)
            assert end["motivo"] == "ociosa"
            await asyncio.wait_for(client.closed.wait(), timeout=10)
        assert await asyncio.wait_for(task, timeout=30) is EndReason.IDLE
        assert not profile.exists()
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_hard_cap_ends_the_session_even_with_activity(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with running_host(
        chromium_path, f"{portal_v1.base_url}/", tmp_path, max_duration_s=2
    ) as (host, token, task):
        async with live(host, token) as client:
            for n in range(30):
                await client.send(tipo="mouse", acao="mover", x=n, y=n)
                await asyncio.sleep(0.1)
                if client.of("fim"):
                    break
            end = await client.event("fim", within=10)
            assert end["motivo"] == "tempo_esgotado"
        assert await asyncio.wait_for(task, timeout=30) is EndReason.TIME_UP


async def test_page_dialogs_are_answered_by_the_person(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    async with (
        running_host(chromium_path, f"{portal_v1.base_url}/", tmp_path) as (host, token, _),
        live(host, token) as client,
    ):
        await client.page("/")
        page = host.page
        assert page is not None
        await page.evaluate(
            "() => { window.resposta = null; document.body.insertAdjacentHTML('beforeend',"
            " '<button id=b onclick=\"window.resposta = confirm(\\'Confirma a exclusão?\\')\">"
            "Excluir</button>'); }"
        )
        await client.click_on(page.locator("#b"))
        dialog = await client.event("dialogo")
        assert dialog["texto"] == "Confirma a exclusão?"
        assert dialog["tipo_dialogo"] == "confirm"
        await client.send(tipo="dialogo", aceitar=True)
        for _ in range(50):
            if await page.evaluate("() => window.resposta") is True:
                break
            await asyncio.sleep(0.1)
        assert await page.evaluate("() => window.resposta") is True
