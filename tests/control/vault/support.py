"""Auxiliares dos testes do cofre: o cliente do OpenBao ligado ao OpenBao falso."""

from pathlib import Path
from typing import Any

import httpx2

from rapa.control.vault import BaoSettings, OpenBaoClient

from .fakebao import ROLE, SA_TOKEN, FakeBao, FakeClock

ADDRESS = "https://openbao.cofre.test:8200"


def sa_token_file(tmp_path: Path, value: str = SA_TOKEN) -> Path:
    path = tmp_path / "token-da-conta"
    path.write_text(value + "\n")
    return path


def bao_client(
    bao: FakeBao,
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    role: str = ROLE,
    **changes: Any,
) -> OpenBaoClient:
    """O cliente de um papel (o despachante, por padrão), com as políticas dele."""
    settings = BaoSettings(
        address=ADDRESS,
        role=role,
        sa_token_file=sa_token_file(tmp_path),
        **changes,
    )
    return OpenBaoClient(
        settings,
        transport=httpx2.ASGITransport(app=bao.app),
        clock=clock or bao.clock,
    )
