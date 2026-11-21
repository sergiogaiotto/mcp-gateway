"""A sessão de gravação não sai da web, de ponta a ponta, só pela API pública e pelo websocket.

A criadora tenta, pelo canal da visão ao vivo, abrir a nova aba do Chromium (e dela ler
o ``tokens.json`` do servidor, que tem o token da outra empresa) e abrir a própria API
no loopback. Nada disso chega à tela dela.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from playwright.async_api import async_playwright
from websockets.asyncio.client import connect

from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from tests.control.api.support import auth, serve
from tests.control.db.support import Tenant
from tests.e2e.support import TOKENS
from tests.session.support import LiveClient

pytestmark = [pytest.mark.database, pytest.mark.browser]


async def _open(
    client: httpx2.AsyncClient, api: str, tenant: Tenant, base: str
) -> tuple[str, LiveClient]:
    creator = auth(TOKENS["creator"])
    body = {"area_id": str(tenant.area_id), "endereco": f"{base}/", "titulo": "x"}
    created = await client.post("/v1/sessoes", json=body, headers=creator)
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    ticket = (await client.post(f"/v1/sessoes/{sid}/bilhete", headers=creator)).json()
    ws = await connect(api.replace("http://", "ws://") + ticket["path"], max_size=None)
    live = LiveClient(ws)
    await live.send(tipo="bilhete", bilhete=ticket["ticket"])
    return sid, live


async def test_the_new_tab_page_never_opens_and_server_files_stay_unread(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
) -> None:
    app, tenant, _other = platform_app
    tokens_file = tmp_path / "tokens.json"
    assert TOKENS["other"] in tokens_file.read_text()  # o token do admin da OUTRA empresa
    with serve(app) as api:
        async with (
            httpx2.AsyncClient(base_url=api, trust_env=False, timeout=60) as client,
            async_playwright() as playwright,
        ):
            sid, live = await _open(client, api, tenant, portal_v1.base_url)
            ready = await live.event("pronta", within=30)
            await live.page("/", within=30)
            # Ctrl+T pelo canal: no navegador sem a guarda, abria chrome://new-tab-page.
            await live.press("t", "KeyT", 2)
            await asyncio.sleep(1.5)
            # Onde ficaria a caixa de busca da nova aba, medido num navegador do mesmo tamanho.
            measure = await playwright.chromium.launch_persistent_context(
                str(tmp_path / "medidor"),
                executable_path=chromium_path,
                headless=True,
                args=[HOST_RESOLVER_ARG],
                viewport={"width": ready["largura"], "height": ready["altura"]},
            )
            try:
                first = measure.pages[0] if measure.pages else await measure.new_page()
                await first.goto(f"{portal_v1.base_url}/")
                cdp = await measure.new_cdp_session(first)
                key = {
                    "key": "t",
                    "code": "KeyT",
                    "windowsVirtualKeyCode": 84,
                    "nativeVirtualKeyCode": 84,
                    "modifiers": 2,
                    "autoRepeat": False,
                    "location": 0,
                }
                async with measure.expect_page() as opened:
                    await cdp.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **key})
                    await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **key})
                page = await opened.value
                await asyncio.sleep(1.0)
                await page.set_viewport_size({"width": ready["largura"], "height": ready["altura"]})
                box = page.locator("input#input").first
                await box.wait_for(state="visible")
                await live.click_on(box)
            finally:
                await measure.close()
            await live.type(f"file://{tokens_file}")
            await live.press("Enter")
            await asyncio.sleep(3)
            shown = [
                e["url"]
                for e in live.of("pagina")
                if str(e.get("url", "")).startswith(("file:", "chrome:"))
            ]
            # A sessão continua: a página da pessoa é a do portal.
            pages = [e["url"] for e in live.of("pagina")]
            await live.close()
            stopped = await client.post(f"/v1/sessoes/{sid}/parar", headers=auth(TOKENS["creator"]))
            assert stopped.status_code == 200, stopped.text
    assert shown == []
    assert any(url.startswith(portal_v1.base_url) for url in pages)


async def test_the_api_on_loopback_is_out_of_reach(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
) -> None:
    app, tenant, _other = platform_app
    with serve(app) as api:
        async with httpx2.AsyncClient(base_url=api, trust_env=False, timeout=60) as client:
            # Como endereço inicial, é recusado na hora.
            refused = await client.post(
                "/v1/sessoes",
                json={"area_id": str(tenant.area_id), "endereco": f"{api}/openapi.json"},
                headers=auth(TOKENS["creator"]),
            )
            assert refused.status_code == 422, refused.text
            sid, live = await _open(client, api, tenant, portal_v1.base_url)
            await live.event("pronta", within=30)
            await live.page("/", within=30)
            await live.send(tipo="navegar", url=f"{api}/openapi.json")
            shown: Any = None
            try:
                shown = await live.event(
                    "pagina", url=lambda u: isinstance(u, str) and u.startswith(api), within=15
                )
            except AssertionError:
                pass
            warned = [e for e in live.of("aviso") if "endereços internos" in str(e.get("mensagem"))]
            await live.close()
            stopped = await client.post(f"/v1/sessoes/{sid}/parar", headers=auth(TOKENS["creator"]))
            assert stopped.status_code == 200, stopped.text
    assert shown is None
    assert warned
