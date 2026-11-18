"""Fixtures compartilhadas dos testes."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from rapa.fixtures.server import RunningPortal, run_portal

_CANDIDATES = ("/usr/bin/chromium", "/opt/pw-browsers/chromium")


def find_chromium() -> str | None:
    """Caminho do Chromium usado nos testes: RAPA_CHROMIUM_PATH ou um padrão conhecido."""
    configured = os.environ.get("RAPA_CHROMIUM_PATH")
    if configured:
        return configured
    for candidate in _CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("chromium")
    return found


@pytest.fixture(scope="session")
def chromium_path() -> str:
    path = find_chromium()
    if path is None:
        pytest.skip("Chromium não encontrado; defina RAPA_CHROMIUM_PATH")
    return path


@pytest.fixture
def portal_v1() -> Iterator[RunningPortal]:
    with run_portal("v1") as portal:
        yield portal


@pytest.fixture
def portal_v2() -> Iterator[RunningPortal]:
    with run_portal("v2") as portal:
        yield portal
