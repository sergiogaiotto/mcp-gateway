"""O navegador da sessão não sai da web: nem a rede do plano de controle, nem chrome:// e file://."""

import asyncio
import contextlib
import http.server
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.async_api import Locator

from rapa.fixtures.server import RunningPortal
from rapa.session.host import SessionHost
from tests.session.test_host_browser import live, running_host

pytestmark = pytest.mark.browser

MARK = "SEGREDO-DO-PLANO-DE-CONTROLE"


@contextmanager
def internal_server(redirect_to: str | None = None) -> Iterator[int]:
    """Um serviço "interno" em 127.0.0.1 (como a API, o banco ou o metadata da nuvem)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            data = f"<html><head><title>{MARK}</title></head><body>{MARK}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


def _urls(host: SessionHost) -> list[str]:
    context = host._context
    return [p.url for p in context.pages] if context is not None else []


async def _titles(host: SessionHost) -> list[str]:
    context = host._context
    out = []
    for page in context.pages if context is not None else []:
        with contextlib.suppress(Exception):
            out.append(await page.title())
    return out


async def _value(field: Locator, expected: str) -> None:
    for _ in range(100):
        if await field.input_value() == expected:
            return
        await asyncio.sleep(0.1)
    assert await field.input_value() == expected


async def test_navigation_never_reaches_the_control_plane_network(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    with internal_server() as port, internal_server(f"http://127.0.0.1:{port}/") as redirector:
        async with (
            running_host(chromium_path, f"{portal_v1.base_url}/", tmp_path) as (host, token, _),
            live(host, token) as client,
        ):
            await client.page("/")
            for url in (
                f"http://127.0.0.1:{port}/",
                f"http://localhost:{port}/x",
                f"http://0x7f000001:{port}/z",
                f"http://[::1]:{port}/",
                "http://169.254.169.254/latest/meta-data/",
                # Um nome que o navegador resolve (como um domínio externo) e redireciona para dentro.
                f"http://saida.rapa.test:{redirector}/",
            ):
                await client.send(tipo="navegar", url=url)
                await asyncio.sleep(1.2)
            # Um link da página (uma ação da pessoa), também.
            await client.send(tipo="navegar", url=f"{portal_v1.base_url}/")
            page = host.page
            for _ in range(100):
                page = host.page
                if page is not None and page.url.startswith(portal_v1.base_url):
                    break
                await asyncio.sleep(0.1)
            assert page is not None
            await page.wait_for_load_state()
            await page.evaluate(
                "(u) => { document.body.insertAdjacentHTML('afterbegin',"
                " '<a id=l style=\"font-size:40px\">INTERNO</a>');"
                " document.getElementById('l').href = u; }",
                f"http://127.0.0.1:{port}/link",
            )
            await client.click_on(page.locator("#l"))
            await asyncio.sleep(1.5)
            # Um fetch e um iframe da página, que não passam por navegação nenhuma.
            fetched = await page.evaluate(
                "async (u) => { try { return await (await fetch(u)).text(); }"
                " catch (e) { return 'recusado'; } }",
                f"http://127.0.0.1:{port}/fetch",
            )
            assert MARK not in fetched
            assert MARK not in await _titles(host)
            assert not [e for e in client.of("pagina") if e.get("titulo") == MARK]
            # A pessoa fica sabendo por que a página não abriu.
            await client.event("aviso", mensagem=lambda text: "endereços internos" in str(text))
            # A sessão continua utilizável.
            await client.send(tipo="navegar", url=f"{portal_v1.base_url}/login")
            await client.page("/login")


async def test_browser_shortcuts_never_open_internal_pages_or_files(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    secret = tmp_path / "segredos-do-servidor.txt"
    secret.write_text(f"DATABASE_URL=postgresql://rapa:{MARK}@db/rapa\n")
    profiles = tmp_path / "perfis"
    profiles.mkdir()
    async with (
        running_host(chromium_path, f"{portal_v1.base_url}/", profiles) as (host, token, _),
        live(host, token) as client,
    ):
        await client.page("/")
        for key, code, mods in (
            ("t", "KeyT", 2),  # nova aba (chrome://new-tab-page)
            ("n", "KeyN", 2),  # nova janela
            ("h", "KeyH", 2),  # histórico
            ("j", "KeyJ", 2),  # downloads
            ("u", "KeyU", 2),  # view-source:
            ("o", "KeyO", 2),  # abrir arquivo
            ("Home", "Home", 1),  # página inicial
            ("Delete", "Delete", 2 | 8),  # limpar dados (chrome://settings)
            ("Escape", "Escape", 8),  # gerenciador de tarefas
            ("F1", "F1", 0),  # ajuda
            ("t", "KeyT", 4),  # Meta+T
            ("i", "KeyI", 2 | 8),  # ferramentas do desenvolvedor
        ):
            await client.press(key, code, mods)
            await asyncio.sleep(0.6)
            # Na nova aba (se ela abrisse), a caixa de busca levaria a qualquer endereço.
            page = host.page
            if page is not None and page.url.startswith("chrome://new-tab-page"):
                box = page.locator("input#input").first
                with contextlib.suppress(Exception):
                    await box.wait_for(state="visible", timeout=3000)
                    await client.click_on(box)
            await client.type(f"file://{secret}")
            await client.press("Enter")
            await asyncio.sleep(0.8)
            urls = _urls(host)
            assert not any(u.startswith(("chrome:", "file:", "view-source:")) for u in urls), (
                key,
                mods,
                urls,
            )
        assert not [
            e for e in client.of("pagina") if str(e.get("url", "")).startswith(("chrome:", "file:"))
        ]
        # Os atalhos de edição continuam chegando à página.
        await client.send(tipo="navegar", url=f"{portal_v1.base_url}/login")
        await client.page("/login")
        page = host.page
        assert page is not None
        field = page.locator("input[name=usuario]")
        await client.click_on(field)
        await client.type("abc")
        await _value(field, "abc")
        await client.press("a", "KeyA", 2)  # Ctrl+A seleciona o campo
        await client.press("Backspace")
        await _value(field, "")


async def test_an_internal_page_opened_by_the_browser_is_closed_unseen(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    # Defesa em profundidade: mesmo que uma aba interna abra (por um atalho que escapasse),
    # ela fecha e a pessoa continua na página dela.
    async with (
        running_host(chromium_path, f"{portal_v1.base_url}/", tmp_path) as (host, token, _),
        live(host, token) as client,
    ):
        await client.page("/")
        context = host._context
        assert context is not None
        internal = await context.new_page()
        with contextlib.suppress(Exception):
            await internal.goto("chrome://version/")
        for _ in range(50):
            if internal.is_closed():
                break
            await asyncio.sleep(0.1)
        assert internal.is_closed()
        page = host.page
        assert page is not None and page.url.startswith(portal_v1.base_url)
        assert not [e for e in client.of("pagina") if str(e.get("url", "")).startswith("chrome:")]


async def test_webrtc_sends_no_udp_outside_the_proxy(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.setblocking(False)
    stun = udp.getsockname()[1]
    try:
        async with (
            running_host(chromium_path, f"{portal_v1.base_url}/", tmp_path) as (host, token, _),
            live(host, token) as client,
        ):
            await client.page("/")
            page = host.page
            assert page is not None
            # Um STUN no loopback: sem a política, o Chromium manda UDP direto, fora do proxy.
            result = await page.evaluate(
                """async (port) => {
                  const frame = document.createElement('iframe');
                  document.body.appendChild(frame);
                  const Peer = window.RTCPeerConnection || frame.contentWindow.RTCPeerConnection;
                  if (!Peer) return 'sem webrtc';
                  const pc = new Peer({iceServers: [{urls: 'stun:127.0.0.1:' + port}]});
                  pc.createDataChannel('x');
                  await pc.setLocalDescription(await pc.createOffer());
                  return 'com webrtc';
                }""",
                stun,
            )
            await asyncio.sleep(3)
            received = 0
            with contextlib.suppress(BlockingIOError):
                while True:
                    udp.recvfrom(2048)
                    received += 1
            assert result == "sem webrtc"
            assert received == 0
    finally:
        udp.close()
