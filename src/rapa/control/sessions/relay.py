"""Repasse da visão ao vivo entre o navegador da pessoa e o hospedeiro da sessão.

Cada sessão aberta neste processo tem no máximo uma ligação com o hospedeiro
(:class:`HostLink`, um websocket com o token da sessão), aberta na primeira vez
que alguém assiste ou pede para parar, e que dura até o fim da sessão. O
hospedeiro aceita uma conexão por vez; a ligação é essa conexão.

- **Uma janela por vez.** Quem assiste é uma só janela: uma segunda janela da
  mesma dona *substitui* a primeira, que recebe ``fim`` com o motivo
  ``substituida`` e é fechada (o código 4001). Assim a reconexão de uma aba que
  caiu (ou a mesma sessão aberta noutra aba) funciona sem esperar a anterior
  perceber que caiu.
- **Só entradas.** Da janela, só passam as mensagens de entrada válidas
  (:data:`rapa.session.protocol.VIEWER_MESSAGES`), conferidas aqui de novo e
  regravadas no formato do canal; ``parar`` só vem pelo REST, que guarda a gravação e
  audita.
- **Sem fila.** Os quadros vão para a janela por um
  :class:`rapa.session.protocol.FrameSender`: numa rede lenta, os intermediários
  são descartados e o último sempre chega. Os eventos vão por uma caixa de saída
  da janela (:class:`_Outbox`, no máximo :data:`OUTBOX_LIMIT` esperando, e um
  ``pagina`` ou ``tela`` novo toma o lugar do que ainda não saiu): uma janela que
  para de ler (aba em segundo plano, rede lenta) nunca trava a ligação com o
  hospedeiro, que continua lendo (o ``ping``, a gravação do ``parar``). O último
  quadro e os últimos eventos ``pronta``, ``tela`` e ``pagina`` ficam guardados (só
  em memória) para a próxima janela começar com a tela certa.
- **A gravação não passa.** O evento ``gravacao`` só serve ao pedido de parar e
  nunca vai para a janela; quadros e eventos nunca vão para o log.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from rapa.control.sessions.launcher import SessionEndpoint
from rapa.session.protocol import (
    RELAYED_HOST_EVENTS,
    VIEWER_MESSAGES,
    FrameSender,
    ProtocolError,
    dump_event,
    parse_client_message,
    parse_host_event,
)

__all__ = [
    "OUTBOX_LIMIT",
    "REPLACED_CLOSE_CODE",
    "HostLink",
    "LiveRelay",
    "SessionGone",
    "StopFailed",
    "StopInProgress",
    "Viewer",
]

logger = logging.getLogger("rapa.control.sessions")

#: Código de fechamento da janela substituída por outra.
REPLACED_CLOSE_CODE = 4001
#: A gravação pode ser grande (texto das páginas); o canal com o hospedeiro aceita até isto.
MAX_HOST_MESSAGE = 64 * 1024 * 1024
_CACHED_EVENTS = ("pronta", "tela", "pagina")
#: Eventos esperando a janela, no máximo; acima disso, o mais velho cai.
OUTBOX_LIMIT = 64
#: Eventos em que só o último interessa (um novo toma o lugar do que ainda não saiu).
_COALESCED = frozenset({"pagina", "tela"})
#: Eventos que só caem se não houver outro para descartar.
_KEPT = frozenset({"fim", "dialogo", "sessao"})
#: Espera pela janela para entregar o que ficou (o ``fim``) antes de fechá-la.
_FLUSH_TIMEOUT_S = 2.0

type EndCallback = Callable[[UUID, str], Awaitable[None]]


class SessionGone(RuntimeError):
    """O hospedeiro da sessão não responde mais (fechou ou nunca abriu neste processo)."""


class StopFailed(RuntimeError):
    """O hospedeiro não devolveu a gravação; a mensagem é fixa, em pt-BR."""


class StopInProgress(StopFailed):
    """Outro pedido já está parando a sessão (este não muda nada)."""


class Viewer(Protocol):
    """A janela de quem assiste (no RaPA, o websocket da rota ``/ao-vivo``)."""

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def receive_text(self) -> str | None:
        """A próxima mensagem de texto; None quando a janela fechou."""
        ...

    async def close(self, code: int, reason: str) -> None: ...


class _Outbox:
    """Eventos para a janela, enviados por uma tarefa própria e sem acúmulo."""

    def __init__(self, send: Callable[[str], Awaitable[None]], limit: int = OUTBOX_LIMIT) -> None:
        self._send = send
        self._limit = limit
        self._items: deque[tuple[str, str]] = deque()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.dropped = 0

    @property
    def pending(self) -> int:
        return len(self._items)

    def offer(self, kind: str, raw: str) -> None:
        """Põe o evento na fila da janela; nunca espera."""
        if self._closed:
            return
        if kind in _COALESCED:
            before = len(self._items)
            self._items = deque(item for item in self._items if item[0] != kind)
            self.dropped += before - len(self._items)
        if len(self._items) >= self._limit:
            victim = next((i for i, item in enumerate(self._items) if item[0] not in _KEPT), 0)
            del self._items[victim]
            self.dropped += 1
        self._items.append((kind, raw))
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        while self._items and not self._closed:
            _, raw = self._items.popleft()
            try:
                await self._send(raw)
            except Exception:
                # A janela caiu: quem é dono dela percebe e encerra; aqui só para.
                self._closed = True
                self._items.clear()
                return

    async def flush(self) -> None:
        """Espera o que está na fila sair (com prazo: a janela pode não estar lendo)."""
        task = self._task
        if task is not None and not task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), _FLUSH_TIMEOUT_S)

    async def close(self) -> None:
        self._closed = True
        self._items.clear()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@dataclass
class _Attached:
    viewer: Viewer
    sender: FrameSender
    outbox: _Outbox
    replaced: bool = False

    async def detach(self) -> None:
        await self.sender.close()
        await self.outbox.close()


@dataclass
class HostLink:
    """A conexão deste processo com o hospedeiro de uma sessão."""

    session_id: UUID
    ws: ClientConnection
    on_end: EndCallback | None = None
    viewer: _Attached | None = None
    cached: dict[str, str] = field(default_factory=dict)
    last_frame: bytes | None = None
    recording: asyncio.Future[dict[str, Any]] | None = None
    ended: str | None = None
    reader: asyncio.Task[None] | None = None

    @property
    def open(self) -> bool:
        return self.reader is not None and not self.reader.done()

    async def read(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self.last_frame = message
                    if self.viewer is not None:
                        self.viewer.sender.offer(message)
                    continue
                await self._event(message)
        except ConnectionClosed, WebSocketException:
            pass
        finally:
            await self._closed()

    async def _event(self, raw: str) -> None:
        try:
            event = parse_host_event(raw)
        except ProtocolError:
            return
        kind = event["tipo"]
        if kind == "gravacao":
            payload = event.get("gravacao")
            if self.recording is not None and not self.recording.done():
                if isinstance(payload, dict):
                    self.recording.set_result(payload)
                else:
                    self.recording.set_exception(StopFailed("a gravação chegou incompleta"))
            return
        if kind not in RELAYED_HOST_EVENTS:
            return
        if kind in _CACHED_EVENTS:
            self.cached[kind] = raw
        if kind == "fim":
            reason = event.get("motivo")
            self.ended = reason if isinstance(reason, str) else "encerrada"
            message = event.get("mensagem")
            if self.recording is not None and not self.recording.done():
                text = message if isinstance(message, str) else "a gravação não pôde ser fechada"
                self.recording.set_exception(StopFailed(text[:300]))
        viewer = self.viewer
        if viewer is not None:
            # Nunca espera a janela: a leitura do hospedeiro segue mesmo se ela parou de ler.
            viewer.outbox.offer(kind, raw)

    async def _closed(self) -> None:
        if self.recording is not None and not self.recording.done():
            self.recording.set_exception(SessionGone("o navegador da gravação fechou"))
        viewer, self.viewer = self.viewer, None
        if viewer is not None:
            if self.ended is None:
                viewer.outbox.offer("fim", dump_event("fim", motivo="perdida"))
            await viewer.outbox.flush()
            await viewer.detach()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(viewer.viewer.close(1000, "fim"), _FLUSH_TIMEOUT_S)
        if self.on_end is not None:
            with contextlib.suppress(Exception):
                await self.on_end(self.session_id, self.ended or "perdida")

    async def send(self, text: str) -> None:
        await self.ws.send(text)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()
        reader = self.reader
        # O aviso de fim roda dentro da própria leitura: ela não espera por si mesma.
        if reader is not None and reader is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(reader, return_exceptions=True)


class LiveRelay:
    """As ligações deste processo com os hospedeiros e as janelas de quem assiste."""

    def __init__(
        self,
        *,
        on_end: EndCallback | None = None,
        connect_timeout_s: float = 10.0,
        stop_timeout_s: float = 60.0,
    ) -> None:
        self.on_end = on_end
        self.connect_timeout_s = connect_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self._endpoints: dict[UUID, SessionEndpoint] = {}
        self._links: dict[UUID, HostLink] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    def register(self, session_id: UUID, endpoint: SessionEndpoint) -> None:
        self._endpoints[session_id] = endpoint

    def knows(self, session_id: UUID) -> bool:
        return session_id in self._endpoints

    async def forget(self, session_id: UUID) -> None:
        """Esquece a sessão e fecha a ligação (as janelas recebem o ``fim`` do hospedeiro)."""
        self._endpoints.pop(session_id, None)
        self._locks.pop(session_id, None)
        link = self._links.pop(session_id, None)
        if link is not None:
            await link.close()

    async def aclose(self) -> None:
        for session_id in list(self._endpoints):
            await self.forget(session_id)

    async def _link(self, session_id: UUID) -> HostLink:
        endpoint = self._endpoints.get(session_id)
        if endpoint is None:
            raise SessionGone("a sessão não está aberta neste servidor")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            link = self._links.get(session_id)
            if link is not None and link.open:
                return link
            try:
                ws = await asyncio.wait_for(
                    connect(
                        endpoint.url,
                        additional_headers={"Authorization": f"Bearer {endpoint.token}"},
                        max_size=MAX_HOST_MESSAGE,
                        compression=None,
                        ping_interval=10,
                        ping_timeout=20,
                        open_timeout=self.connect_timeout_s,
                        logger=logging.getLogger("rapa.control.sessions.canal"),
                    ),
                    timeout=self.connect_timeout_s + 1,
                )
            except (OSError, TimeoutError, WebSocketException) as exc:
                logger.warning("sem ligação com o hospedeiro da sessão: %s", type(exc).__name__)
                raise SessionGone("o navegador da gravação não respondeu") from None
            link = HostLink(session_id=session_id, ws=ws, on_end=self.on_end)
            link.reader = asyncio.get_running_loop().create_task(
                link.read(), name=f"ligacao-{session_id}"
            )
            self._links[session_id] = link
            return link

    async def watch(self, session_id: UUID, viewer: Viewer, *, greeting: str | None = None) -> None:
        """Liga a janela à sessão até ela sair, a sessão acabar ou outra janela a substituir."""
        link = await self._link(session_id)
        attached = _Attached(
            viewer=viewer,
            sender=FrameSender(viewer.send_bytes),
            outbox=_Outbox(viewer.send_text),
        )
        # A saudação e a tela guardada entram na caixa antes de qualquer evento novo.
        if greeting is not None:
            attached.outbox.offer("sessao", greeting)
        for kind in _CACHED_EVENTS:
            if kind in link.cached:
                attached.outbox.offer(kind, link.cached[kind])
        previous, link.viewer = link.viewer, attached
        if link.last_frame is not None:
            attached.sender.offer(link.last_frame)
        if previous is not None:
            previous.replaced = True
            await previous.detach()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    previous.viewer.send_text(dump_event("fim", motivo="substituida")),
                    _FLUSH_TIMEOUT_S,
                )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    previous.viewer.close(REPLACED_CLOSE_CODE, "substituida"), _FLUSH_TIMEOUT_S
                )
        try:
            while link.open and not attached.replaced:
                raw = await viewer.receive_text()
                # Outra janela pode ter tomado o lugar enquanto esta esperava: nada dela passa.
                if raw is None or attached.replaced:
                    break
                forwarded = _viewer_message(raw)
                if forwarded is None:
                    continue
                try:
                    await link.send(forwarded)
                except ConnectionClosed, WebSocketException:
                    break
        finally:
            if link.viewer is attached:
                link.viewer = None
            await attached.detach()

    async def stop(self, session_id: UUID) -> dict[str, Any]:
        """Pede ao hospedeiro a gravação e devolve o JSON dela (já sem segredos)."""
        link = await self._link(session_id)
        if link.recording is not None:
            raise StopInProgress("a sessão já está sendo parada")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        link.recording = future
        try:
            await link.send(dump_event("parar"))
        except ConnectionClosed, WebSocketException:
            raise SessionGone("o navegador da gravação fechou") from None
        try:
            return await asyncio.wait_for(future, timeout=self.stop_timeout_s)
        except TimeoutError:
            raise StopFailed("o navegador da gravação não devolveu a gravação a tempo") from None


def _viewer_message(raw: str) -> str | None:
    """A mensagem da janela no formato do canal, se for uma entrada permitida."""
    try:
        message = parse_client_message(raw)
    except ProtocolError:
        return None
    if message.tipo not in VIEWER_MESSAGES:
        return None
    return json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
