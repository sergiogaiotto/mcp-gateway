"""Repasse da visão ao vivo: uma janela lenta nunca trava a ligação com o hospedeiro."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from rapa.control.sessions.relay import OUTBOX_LIMIT, _Outbox
from rapa.core.contracts import RecordingSessionStatus
from tests.control.sessions.support import QueueViewer, SessionsWorld, build_world

URL = "https://portal.exemplo.gov.br/login"


@pytest.fixture
async def world() -> AsyncIterator[SessionsWorld]:
    w = build_world()
    yield w
    await w.service.aclose()
    await w.launcher.aclose()


class StuckViewer(QueueViewer):
    """Uma aba em segundo plano: recebe a primeira mensagem e depois para de ler."""

    def __init__(self) -> None:
        super().__init__()
        self.unblock = asyncio.Event()

    async def send_text(self, data: str) -> None:
        if self.texts:
            await self.unblock.wait()
        await super().send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.unblock.wait()
        await super().send_bytes(data)


async def test_a_viewer_that_stops_reading_never_stalls_the_host_link(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    info = await world.service.create_session(p, world.area_a, URL)
    sid = info.session_id
    viewer = StuckViewer()
    ticket, _ = world.service.tickets.issue(sid, p)
    greeting = await world.service.admit_viewer(sid, ticket)
    watching = asyncio.create_task(world.service.relay_viewer(sid, viewer, greeting))
    await viewer.wait(lambda: len(viewer.texts) == 1)
    host = world.launcher.hosts[sid]
    # A página no navegador da nuvem muda de título sem parar enquanto a aba não lê nada.
    for n in range(300):
        await host.emit(
            {"tipo": "pagina", "url": URL, "titulo": f"Título {n}"},
            {"tipo": "aviso", "mensagem": f"aviso {n}"},
        )
    link = world.service.relay._links[sid]
    attached = link.viewer
    assert attached is not None
    for _ in range(100):
        if attached.outbox.dropped:
            break
        await asyncio.sleep(0.01)
    # A caixa da janela não cresce, e o "parar" (pela mesma ligação) funciona na hora.
    assert attached.outbox.pending <= OUTBOX_LIMIT
    stopped = await asyncio.wait_for(world.service.stop_session(p, sid), 5)
    assert stopped.status is RecordingSessionStatus.STOPPED
    # A janela travada é fechada no fim, sem segurar ninguém.
    await asyncio.wait_for(watching, 5)
    assert viewer.closed is not None
    viewer.unblock.set()


async def test_the_outbox_keeps_the_latest_page_and_the_end() -> None:
    sent: list[str] = []
    gate = asyncio.Event()

    async def send(raw: str) -> None:
        await gate.wait()
        sent.append(json.loads(raw)["tipo"] + ":" + json.loads(raw).get("n", ""))

    outbox = _Outbox(send, limit=4)
    outbox.offer("aviso", json.dumps({"tipo": "aviso", "n": "0"}))
    await asyncio.sleep(0)  # o primeiro já está saindo
    outbox.offer("dialogo", json.dumps({"tipo": "dialogo", "n": "d"}))
    for n in range(5):
        outbox.offer("pagina", json.dumps({"tipo": "pagina", "n": str(n)}))
        outbox.offer("tela", json.dumps({"tipo": "tela", "n": str(n)}))
    for n in range(1, 4):
        outbox.offer("aviso", json.dumps({"tipo": "aviso", "n": str(n)}))
    outbox.offer("fim", json.dumps({"tipo": "fim", "n": "f"}))
    assert outbox.pending <= 4
    gate.set()
    await outbox.flush()
    assert sent[0] == "aviso:0"
    assert "dialogo:d" in sent and sent[-1] == "fim:f"
    assert [s for s in sent if s.startswith("pagina")] in ([], ["pagina:4"])
    await outbox.close()


class LateViewer(QueueViewer):
    """Uma janela cuja mensagem chega depois de outra janela tomar o lugar dela."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def receive_text(self) -> str | None:
        await self.release.wait()
        return '{"tipo":"mouse","acao":"apertar","x":5,"y":5,"botao":"esquerdo","cliques":1}'

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)
        self.changed.set()


async def test_input_received_after_replacement_never_reaches_the_host(
    world: SessionsWorld,
) -> None:
    p = world.people["criadora"]
    info = await world.service.create_session(p, world.area_a, URL)
    sid = info.session_id
    first, second = LateViewer(), QueueViewer()
    t1, _ = world.service.tickets.issue(sid, p)
    w1 = asyncio.create_task(
        world.service.relay_viewer(sid, first, await world.service.admit_viewer(sid, t1))
    )
    await first.wait(lambda: "pronta" in first.kinds())
    t2, _ = world.service.tickets.issue(sid, p)
    w2 = asyncio.create_task(
        world.service.relay_viewer(sid, second, await world.service.admit_viewer(sid, t2))
    )
    await second.wait(lambda: "pronta" in second.kinds())
    await first.wait(lambda: first.closed is not None)
    # A mensagem que a janela antiga já estava recebendo chega agora: não passa.
    first.release.set()
    await asyncio.wait_for(w1, 5)
    second.inbox.put_nowait('{"tipo":"mouse","acao":"mover","x":1,"y":1}')
    host = world.launcher.hosts[sid]
    for _ in range(100):
        if host.received:
            break
        await asyncio.sleep(0.02)
    assert [m["acao"] for m in host.received] == ["mover"]
    second.inbox.put_nowait(None)
    await asyncio.wait_for(w2, 5)
