"""Auxiliares dos testes da sessão ao vivo: um cliente do canal que simula a pessoa."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from playwright.async_api import Locator
from websockets.asyncio.client import ClientConnection


class LiveClient:
    """Fala o protocolo do canal: guarda eventos e quadros e manda entradas como a tela faria."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.events: list[dict[str, Any]] = []
        self.frames: list[bytes] = []
        self.closed = asyncio.Event()
        self._changed = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self.frames.append(message)
                else:
                    self.events.append(json.loads(message))
                self._changed.set()
        except Exception:  # noqa: S110 - a conexão fecha no fim de cada teste
            pass
        finally:
            self.closed.set()
            self._changed.set()

    async def wait_for(
        self, check: Callable[[], Any], *, within: float = 15.0, what: str = "condição"
    ) -> Any:
        deadline = asyncio.get_running_loop().time() + within
        while True:
            value = check()
            if value:
                return value
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                kinds = [e.get("tipo") for e in self.events]
                raise AssertionError(f"esperava {what}; eventos: {kinds}")
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(remaining, 0.2))
            except TimeoutError:
                pass

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("tipo") == kind]

    async def event(self, kind: str, *, within: float = 15.0, **match: Any) -> dict[str, Any]:
        def found() -> dict[str, Any] | None:
            for event in self.of(kind):
                if all(
                    (value(event.get(k)) if callable(value) else event.get(k) == value)
                    for k, value in match.items()
                ):
                    return event
            return None

        result: dict[str, Any] = await self.wait_for(found, within=within, what=kind)
        return result

    async def page(self, path_suffix: str, *, within: float = 15.0) -> dict[str, Any]:
        return await self.event(
            "pagina",
            within=within,
            url=lambda url: isinstance(url, str) and url.split("?")[0].endswith(path_suffix),
        )

    async def send(self, **message: Any) -> None:
        await self.ws.send(json.dumps(message, ensure_ascii=False))

    async def click(self, x: float, y: float) -> None:
        await self.send(tipo="mouse", acao="mover", x=x, y=y)
        await self.send(tipo="mouse", acao="apertar", x=x, y=y, botao="esquerdo", cliques=1)
        await self.send(tipo="mouse", acao="soltar", x=x, y=y, botao="esquerdo", cliques=1)

    async def click_on(self, locator: Locator) -> None:
        """Clica no centro do elemento (a posição vem de uma leitura da página, sem eventos)."""
        await locator.wait_for(state="visible")
        box = await locator.bounding_box()
        assert box is not None
        await self.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    async def type(self, text: str) -> None:
        """Um ``texto`` por caractere, como o campo escondido da tela manda a cada ``input``."""
        for char in text:
            await self.send(tipo="texto", texto=char)

    async def press(self, key: str, code: str | None = None, modifiers: int = 0) -> None:
        for action in ("descer", "subir"):
            await self.send(
                tipo="tecla", acao=action, tecla=key, codigo=code or key, modificadores=modifiers
            )

    async def close(self) -> None:
        await self.ws.close()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
