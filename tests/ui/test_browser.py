"""A interface de verdade, num Chromium, servida pelo uvicorn (login, formulário e execução)."""

import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from nicegui.testing.general import nicegui_reset_globals
from playwright.async_api import async_playwright, expect

from rapa.ui.dev import DevWorld, build_dev_ui

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def _serve_dev_ui() -> Iterator[tuple[str, DevWorld]]:
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "rapa"}
    try:
        with nicegui_reset_globals():
            app = FastAPI()
            world = build_dev_ui(app)
            port = _free_port()
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, name="rapa-ui", daemon=True)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("a interface não subiu em 15 s")
                time.sleep(0.02)
            try:
                yield f"http://127.0.0.1:{port}", world
            finally:
                server.should_exit = True
                thread.join(timeout=15)
    finally:
        sys.modules.update(saved)


async def test_login_and_run_in_real_browser(chromium_path: str) -> None:
    with _serve_dev_ui() as (base_url, world):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_path)
            try:
                page = await browser.new_page()
                page.set_default_timeout(15_000)
                await page.goto(f"{base_url}/executar/emitir_protocolo")
                await page.get_by_role("button", name="Entrar como Operador · Empresa Alfa").click()
                await expect(page.get_by_text("Executar: Emitir protocolo")).to_be_visible()
                # Cookies HttpOnly: nada de sessão ou token visível para o JavaScript da página.
                assert await page.evaluate("document.cookie") == ""

                await page.get_by_label("Nome completo *").fill("Maria da Silva")
                await page.get_by_label("CPF *").press_sequentially("52998224725")
                await expect(page.get_by_label("CPF *")).to_have_value("529.982.247-25")
                await page.get_by_label("UF *").fill("SP")
                await page.get_by_text("Entendo que esta execução").click()
                await page.get_by_role("button", name="Executar", exact=True).click()
                await expect(page.get_by_text("Na fila")).to_be_visible()
            finally:
                await browser.close()
        (run,) = world.platform.runs.values()
        assert run.inputs == {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
