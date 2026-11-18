"""Fixtures dos testes do executor."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Browser

from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.runner.browser import open_browser
from tests.runner.support import RobotRunner


@pytest.fixture
async def browser(chromium_path: str) -> AsyncIterator[Browser]:
    async with open_browser(executable_path=chromium_path, args=[HOST_RESOLVER_ARG]) as opened:
        yield opened


@pytest.fixture
def run_robot(browser: Browser, tmp_path: Path) -> RobotRunner:
    return RobotRunner(browser, tmp_path)
