"""A visão ao vivo de verdade: a interface num Chromium, o JavaScript, a API e o hospedeiro.

A pessoa (um Chromium dirigido pelo Playwright) abre a gravação na interface, clica na
tela desenhada no ``<canvas>`` e digita pelo teclado: o ``ao_vivo.js`` manda as entradas
pelo websocket com o bilhete, a API repassa ao hospedeiro (um processo de verdade, com
outro Chromium e o gravador) e o portal de teste recebe o login, a senha e um nome com
acentos, inclusive por composição (IME). A posição dos campos vem de um terceiro
navegador, do mesmo tamanho, que só mede a página.
"""

import asyncio
import http.server
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from nicegui.testing.general import nicegui_reset_globals
from playwright.async_api import Browser, Dialog, Locator, Page, async_playwright, expect

from rapa.control.auth import StaticTokenVerifier
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.sessions import (
    LiveRelay,
    LocalSessionLauncher,
    LocalSessionSettings,
    MemorySessionStore,
    RecordingSessions,
)
from rapa.core.contracts import Principal, RecordingSessionStatus, Role
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER, current_totp
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.recorder import InputEvent, Recording
from rapa.ui.app import create_ui
from rapa.ui.auth import DevLogin, DevProfile, UiAuth
from rapa.ui.backend import ContractsBackendFactory
from rapa.ui.context import UiSettings
from tests.control.api.support import DictResolver

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def _serve(chromium: str, sessions_dir: Path) -> Iterator[tuple[str, MemorySessionStore]]:
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "rapa"}
    try:
        with nicegui_reset_globals():
            platform = MemoryPlatform()
            company, area = new_id(), new_id()
            creator = Principal(
                subject="criadora@a",
                company_id=company,
                area_roles={area: frozenset({Role.CREATOR})},
            )
            store = MemorySessionStore(reserve=2)
            store.add_area(company, area)
            launcher = LocalSessionLauncher(
                LocalSessionSettings(
                    chromium=chromium, browser_args=(HOST_RESOLVER_ARG,), profile_dir=sessions_dir
                )
            )
            sessions = RecordingSessions(store, launcher, platform, relay=LiveRelay())
            port = _free_port()
            app: FastAPI = create_http_app(
                catalog=platform,
                runs=platform,
                admin=platform,
                verifier=StaticTokenVerifier({"token-criadora": creator}),
                resolver=DictResolver(),
                # Com a URL pública, o websocket da visão ao vivo confere o Origin da interface.
                settings=HttpSettings(
                    allow_static_tokens=True,
                    rate_limit_per_s=None,
                    public_url=f"http://127.0.0.1:{port}",
                ),
                sessions=sessions,
            )
            create_ui(
                app,
                backend_factory=ContractsBackendFactory.from_platform(platform, sessions=sessions),
                auth=UiAuth(
                    dev=DevLogin([DevProfile("criadora", "Criadora", creator)]),
                    cookie_secure=False,
                ),
                settings=UiSettings(area_labels={area: "Fiscal"}),
            )
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            )
            thread = threading.Thread(target=server.run, name="rapa-gravacao", daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("o serviço não subiu em 15 s")
                time.sleep(0.02)
            try:
                yield f"http://127.0.0.1:{port}", store
            finally:
                server.should_exit = True
                thread.join(timeout=30)
    finally:
        sys.modules.update(saved)


class Screen:
    """Leva uma posição da página do portal para o ``<canvas>`` da interface."""

    def __init__(self, page: Page, measure: Page) -> None:
        self.page = page
        self.measure = measure
        self.canvas = page.locator("#rapa-ao-vivo canvas")

    async def click(self, target: Locator) -> None:
        await target.wait_for()
        box = await target.bounding_box()
        assert box is not None
        width, height = await self.canvas.evaluate("c => [c.width, c.height]")
        frame = await self.canvas.bounding_box()
        assert frame is not None
        x = frame["x"] + (box["x"] + box["width"] / 2) * frame["width"] / width
        y = frame["y"] + (box["y"] + box["height"] / 2) * frame["height"] / height
        await self.page.mouse.click(x, y)


async def _measuring_page(browser: Browser, width: int, height: int, url: str) -> Page:
    context = await browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()
    await page.goto(url)
    return page


async def test_live_view_in_a_real_browser_types_accents_and_secrets(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    with _serve(chromium_path, tmp_path / "sessoes") as (base_url, store):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
            )
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 900})
                page.set_default_timeout(20_000)
                await page.goto(f"{base_url}/gravar")
                await page.get_by_role("button", name="Entrar como Criadora").click()
                await page.get_by_label("Endereço inicial").fill(f"{portal_v1.base_url}/login")
                await page.get_by_role("button", name="Começar a gravar").click()
                address = page.get_by_label("Endereço da página")
                await expect(address).to_have_value(f"{portal_v1.base_url}/login", timeout=30_000)
                await expect(page.get_by_text("Tempo restante:")).to_be_visible()
                # O tamanho que a tela pediu ao navegador da nuvem é o do <canvas>.
                canvas = page.locator("#rapa-ao-vivo canvas")
                for _ in range(100):
                    width, height = await canvas.evaluate("c => [c.width, c.height]")
                    if (width, height) != (1280, 800):
                        break
                    await asyncio.sleep(0.1)
                measure = await _measuring_page(
                    browser, width, height, f"{portal_v1.base_url}/login"
                )
                screen = Screen(page, measure)
                # A imagem da página chegou (o canvas não está mais vazio).
                painted = await canvas.evaluate(
                    "c => c.getContext('2d').getImageData(c.width / 2, 40, 1, 1).data[3]"
                )
                assert painted == 255

                await screen.click(measure.get_by_label("Usuário"))
                await page.keyboard.type(DEFAULT_USER)
                await screen.click(measure.get_by_label("Senha"))
                await page.keyboard.type(DEFAULT_PASSWORD)
                await page.keyboard.press("Enter")
                await expect(address).to_have_value(f"{portal_v1.base_url}/login/codigo")
                await measure.goto(f"{portal_v1.base_url}/login/codigo")
                await screen.click(measure.get_by_label("Código de verificação"))
                await page.keyboard.type(current_totp())
                await page.keyboard.press("Enter")
                await expect(address).to_have_value(f"{portal_v1.base_url}/inicio")
                # A barra de endereço do navegador da nuvem.
                await address.fill(f"{portal_v1.base_url}/protocolo")
                await address.press("Enter")
                await expect(address).to_have_value(f"{portal_v1.base_url}/protocolo")
                await measure.goto(f"{portal_v1.base_url}/login")
                await measure.get_by_label("Usuário").fill(DEFAULT_USER)
                await measure.get_by_label("Senha").fill(DEFAULT_PASSWORD)
                await measure.get_by_role("button", name="Entrar").click()
                await measure.get_by_label("Código de verificação").fill(current_totp())
                await measure.get_by_role("button", name="Confirmar").click()
                await measure.goto(f"{portal_v1.base_url}/protocolo")
                await screen.click(measure.get_by_label("Nome completo"))
                await page.keyboard.type("João da Conceição")
                # Uma tecla morta pelo IME: a composição vira um "é" só no fim.
                cdp = await page.context.new_cdp_session(page)
                await cdp.send(
                    "Input.imeSetComposition", {"text": "´", "selectionStart": 1, "selectionEnd": 1}
                )
                await cdp.send("Input.insertText", {"text": "é"})
                await page.keyboard.press("Tab")
                await asyncio.sleep(1.5)
                await page.get_by_role("button", name="Parar e revisar").click()
                await expect(page.get_by_text("Revisar:")).to_be_visible(timeout=30_000)
                await expect(page.get_by_text("“João da Conceiçãoé”")).to_be_visible()
            finally:
                await browser.close()
    [record] = store.sessions.values()
    assert record.status is RecordingSessionStatus.STOPPED
    [(_, data)] = store.recordings.values()
    recording = Recording.model_validate(data)
    typed = [e.value for e in recording.events if isinstance(e, InputEvent)]
    assert "João da Conceiçãoé" in typed
    text = recording.model_dump_json()
    for secret in (DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert secret not in text
    # O perfil do navegador da nuvem foi apagado no fim.
    assert list((tmp_path / "sessoes").iterdir()) == []


@contextmanager
def _dialog_site() -> Iterator[int]:
    """Um site que, ao abrir, pede uma senha num ``prompt`` (fingindo ser o RaPA)."""
    page = (
        "<html><head><title>Portal</title></head><body><p>Portal</p><script>"
        "setTimeout(() => { const r = prompt('Sua sessão do RaPA expirou. Digite sua senha:',"
        " 'padrão'); location.href = '/fim?resposta=' + encodeURIComponent(r === null ?"
        " 'cancelado' : r); }, 300);</script></body></html>"
    ).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = page if self.path == "/" else b"<html><body>fim</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


async def test_page_dialogs_appear_inside_the_live_view_never_as_native_dialogs(
    chromium_path: str, tmp_path: Path
) -> None:
    with (
        _dialog_site() as site,
        _serve(chromium_path, tmp_path / "sessoes") as (base_url, store),
    ):
        start = f"http://dialogo.rapa.test:{site}/"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
            )
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 900})
                page.set_default_timeout(20_000)
                native: list[str] = []

                async def refuse(dialog: Dialog) -> None:
                    native.append(dialog.type)
                    await dialog.dismiss()

                page.on("dialog", refuse)
                await page.goto(f"{base_url}/gravar")
                await page.get_by_role("button", name="Entrar como Criadora").click()
                await page.get_by_label("Endereço inicial").fill(start)
                await page.get_by_role("button", name="Começar a gravar").click()
                overlay = page.locator("#rapa-ao-vivo [data-rapa-dialogo=prompt]")
                await expect(overlay).to_be_visible(timeout=30_000)
                # O texto é da página, dito como da página, dentro da tela da gravação.
                await expect(overlay).to_contain_text("não é do RaPA")
                await expect(overlay.locator("[data-rapa-dialogo-texto]")).to_have_text(
                    "Sua sessão do RaPA expirou. Digite sua senha:"
                )
                answer = overlay.get_by_label("Resposta para a página")
                await expect(answer).to_have_value("padrão")
                await answer.fill("resposta qualquer")
                await overlay.get_by_role("button", name="OK").click()
                await expect(overlay).to_have_count(0)
                address = page.get_by_label("Endereço da página")
                await expect(address).to_have_value(
                    f"http://dialogo.rapa.test:{site}/fim?resposta=resposta%20qualquer",
                    timeout=15_000,
                )
                assert native == []
                await page.get_by_role("button", name="Parar e revisar").click()
                await expect(page.get_by_text("Revisar:")).to_be_visible(timeout=30_000)
            finally:
                await browser.close()
    [record] = store.sessions.values()
    assert record.status is RecordingSessionStatus.STOPPED
    assert list((tmp_path / "sessoes").iterdir()) == []


@contextmanager
def _confirm_site() -> Iterator[int]:
    """Um site que pede confirmação para uma ação destrutiva e mostra a resposta."""
    page = (
        b"<html><head><title>Portal</title></head><body><p>Portal</p><script>"
        b"setTimeout(() => { const r = confirm('Excluir todos os registros?');"
        b" location.href = '/fim?r=' + r; }, 300);</script></body></html>"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = page if self.path == "/" else b"<html><body>fim</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


async def test_enter_on_the_cancel_button_cancels_the_page_dialog(
    chromium_path: str, tmp_path: Path
) -> None:
    with _confirm_site() as site, _serve(chromium_path, tmp_path / "sessoes") as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
            )
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 900})
                page.set_default_timeout(20_000)
                await page.goto(f"{base_url}/gravar")
                await page.get_by_role("button", name="Entrar como Criadora").click()
                await page.get_by_label("Endereço inicial").fill(f"http://conf.rapa.test:{site}/")
                await page.get_by_role("button", name="Começar a gravar").click()
                overlay = page.locator("#rapa-ao-vivo [data-rapa-dialogo=confirm]")
                await expect(overlay).to_be_visible(timeout=30_000)
                # Quem usa o teclado vai até "Cancelar" e aperta Enter: a página recebe "não".
                await overlay.get_by_role("button", name="Cancelar").focus()
                await page.keyboard.press("Enter")
                await expect(page.get_by_label("Endereço da página")).to_have_value(
                    f"http://conf.rapa.test:{site}/fim?r=false", timeout=15_000
                )
                await page.get_by_role("button", name="Parar e revisar").click()
                await expect(page.get_by_text("Revisar:")).to_be_visible(timeout=30_000)
            finally:
                await browser.close()
