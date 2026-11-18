import pytest
from playwright.async_api import async_playwright

from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal

pytestmark = pytest.mark.browser


async def test_chromium_reaches_portal_by_test_domain(
    chromium_path: str, portal_v1: RunningPortal
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=chromium_path, args=[HOST_RESOLVER_ARG]
        )
        try:
            page = await browser.new_page()
            await page.goto(f"{portal_v1.base_url}/login")
            await page.get_by_label("Usuário").fill("ana.souza")
            assert await page.get_by_role("button", name="Entrar").count() == 1
            snapshot = await page.aria_snapshot(mode="ai")
            assert "[ref=" in snapshot
        finally:
            await browser.close()
