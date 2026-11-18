"""Sobe o portal de teste numa porta livre, em segundo plano, para testes com navegador.

Os testes acessam o portal por ``http://portal-v1.rapa.test:<porta>``. O
Chromium resolve ``*.rapa.test`` para 127.0.0.1 com o argumento
``HOST_RESOLVER_ARG``; assim os robôs de teste usam nomes de domínio, como em
produção, e o portão de política continua valendo.
"""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from rapa.fixtures.portal import PortalState, Variant, create_app

__all__ = ["HOST_RESOLVER_ARG", "RunningPortal", "run_portal"]

HOST_RESOLVER_ARG = "--host-resolver-rules=MAP *.rapa.test 127.0.0.1"


@dataclass(frozen=True)
class RunningPortal:
    app: FastAPI
    port: int
    host: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def state(self) -> PortalState:
        state: PortalState = self.app.state.portal
        return state


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def run_portal(variant: Variant = "v1", host: str | None = None) -> Iterator[RunningPortal]:
    """Roda o portal até o fim do bloco ``with``."""
    app = create_app(variant)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"portal-{variant}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("o portal de teste não subiu em 10 s")
        time.sleep(0.02)
    try:
        yield RunningPortal(app=app, port=port, host=host or f"portal-{variant}.rapa.test")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
