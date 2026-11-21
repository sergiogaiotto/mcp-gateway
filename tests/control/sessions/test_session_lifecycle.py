"""Ciclo de vida das sessões: endereço guardado, encerrar sem esperar, varredura e quem segura.

Com o armazenamento em memória e o hospedeiro falso (sem navegador).
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from rapa.control.sessions import RecordingSessions, SessionLimits
from rapa.control.sessions.relay import LiveRelay
from rapa.control.sessions.service import STALE_AFTER
from rapa.core.contracts import InvalidInput, NotFound, Principal, RecordingSessionStatus, Role
from rapa.core.ids import new_id
from tests.control.sessions.support import FakeLauncher, QueueViewer, SessionsWorld, build_world

URL = "https://portal.exemplo.gov.br/login"


@pytest.fixture
async def world() -> AsyncIterator[SessionsWorld]:
    w = build_world()
    yield w
    await w.service.aclose()
    await w.launcher.aclose()


async def test_start_url_query_and_fragment_never_reach_the_store(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    secret_url = "https://portal.exemplo.gov.br/acesso?token=TOKEN-SECRETO-XYZ&senha=abc#tk=FRAG"
    info = await world.service.create_session(p, world.area_a, secret_url)
    stored = world.store.sessions[world.company_a, info.session_id]
    assert stored.start_url == "https://portal.exemplo.gov.br/acesso"
    for leaked in ("TOKEN-SECRETO-XYZ", "abc", "FRAG"):
        assert leaked not in info.model_dump_json()
        assert all(leaked not in s.start_url for s in world.store.sessions.values())
    listed = await world.service.list_sessions(p)
    assert [s.start_url for s in listed] == ["https://portal.exemplo.gov.br/acesso"]
    # O navegador da nuvem recebe o endereço inteiro.
    assert world.launcher.jobs[-1].start_url == secret_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://127.0.0.1:8000/openapi.json",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://api.localhost/",
    ],
)
async def test_an_internal_start_url_is_refused(world: SessionsWorld, url: str) -> None:
    with pytest.raises(InvalidInput) as caught:
        await world.service.create_session(world.people["criadora"], world.area_a, url)
    assert any("endereços internos" in error for error in caught.value.errors)
    assert world.launcher.jobs == []


async def test_double_stop_keeps_the_recording(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    info = await world.service.create_session(p, world.area_a, URL)
    results = await asyncio.gather(
        world.service.stop_session(p, info.session_id),
        world.service.stop_session(p, info.session_id),
        return_exceptions=True,
    )
    assert any(not isinstance(r, BaseException) for r in results)
    final = world.store.sessions[world.company_a, info.session_id]
    assert final.status is RecordingSessionStatus.STOPPED
    assert (world.company_a, info.session_id) in world.store.recordings


async def test_a_ticket_presented_for_another_session_is_consumed(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    first = await world.service.create_session(p, world.area_a, URL)
    second = await world.service.create_session(p, world.area_a, URL)
    ticket, _ = world.service.tickets.issue(first.session_id, p)
    with pytest.raises(NotFound):
        await world.service.admit_viewer(second.session_id, ticket)
    with pytest.raises(NotFound):
        await world.service.admit_viewer(first.session_id, ticket)


# -- encerrar sem esperar -----------------------------------------------------------------


async def test_only_a_stopped_host_is_waited_for(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    expired = await world.service.create_session(p, world.area_a, URL)
    stopped = await world.service.create_session(p, world.area_a, URL)
    world.launcher.hosts[expired.session_id].alive = False
    await world.service.sweep()
    await world.service.stop_session(p, stopped.session_id)
    # A sessão que ninguém mandou parar é encerrada na hora; só a parada espera o hospedeiro.
    assert world.launcher.terminated == [expired.session_id]
    assert world.launcher.stopped == [stopped.session_id]


class SlowTerminate(FakeLauncher):
    """Um hospedeiro que demora a sair (o SIGTERM até o SIGKILL)."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        self.entered = 0

    async def terminate(self, session_id: UUID) -> None:
        self.entered += 1
        self.waiting.set()
        await self.release.wait()
        await super().terminate(session_id)


def _third_company(world: SessionsWorld) -> tuple[Principal, UUID]:
    company, area = new_id(), new_id()
    world.store.add_area(company, area)
    person = Principal(
        subject="criadora@c", company_id=company, area_roles={area: frozenset({Role.CREATOR})}
    )
    return person, area


async def test_ending_a_session_never_blocks_other_companies_from_opening() -> None:
    world = build_world(reserve=2)
    launcher = SlowTerminate()
    world.service.launcher = world.launcher = launcher
    service, p = world.service, world.people
    try:
        stuck = await service.create_session(p["criadora"], world.area_a, URL)
        # A plataforma está cheia e a sessão da empresa A perdeu o navegador: quem abre a
        # próxima varre as vagas, e encerrar a sessão perdida demora.
        launcher.hosts[stuck.session_id].alive = False
        world.store.interactive_runs = 1
        sweeping = asyncio.create_task(service.create_session(p["outra"], world.area_b, URL))
        await asyncio.wait_for(launcher.waiting.wait(), 5)
        # Enquanto isso, a trava das aberturas está livre (a varredura roda fora dela) e uma
        # terceira empresa abre a sua sem esperar.
        assert not service._lock.locked()
        world.store.interactive_runs = 0
        person, area = _third_company(world)
        opened = await asyncio.wait_for(service.create_session(person, area, URL), 5)
        assert opened.status is RecordingSessionStatus.ACTIVE
        assert launcher.entered == 1  # a sessão perdida é encerrada uma vez só
        launcher.release.set()
        await asyncio.wait_for(sweeping, 5)
        assert world.store.sessions[world.company_a, stuck.session_id].end_reason == "encerrada"
    finally:
        launcher.release.set()
        await service.aclose()
        await launcher.aclose()


async def test_shutdown_ends_every_session_at_the_same_time() -> None:
    world = build_world(reserve=10)
    launcher = SlowTerminate()
    world.service.launcher = world.launcher = launcher
    service, p = world.service, world.people
    for who, area in (
        ("criadora", world.area_a),
        ("colega", world.area_a),
        ("outra", world.area_b),
    ):
        await service.create_session(p[who], area, URL)
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0.1)
    # Os três encerramentos começaram juntos (nenhum espera o anterior).
    assert launcher.entered == 3
    launcher.release.set()
    await asyncio.wait_for(closing, 5)
    assert sorted(map(str, launcher.terminated)) == sorted(map(str, launcher.hosts))
    assert all(s.status is RecordingSessionStatus.EXPIRED for s in world.store.sessions.values())
    await launcher.aclose()


# -- quem segura a sessão ---------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _other_process(world: SessionsWorld, clock: Clock, launcher: FakeLauncher) -> RecordingSessions:
    """Outro processo da API (uma réplica, ou o mesmo servidor depois de reiniciar)."""
    return RecordingSessions(
        world.store,
        launcher,
        world.platform,
        relay=LiveRelay(stop_timeout_s=10),
        clock=clock,
    )


async def test_sessions_of_a_crashed_api_are_expired_after_the_grace() -> None:
    world = build_world()
    clock = Clock()
    world.service._clock = clock
    p = world.people
    crashed_launcher = world.launcher
    old_a = await world.service.create_session(p["criadora"], world.area_a, URL)
    old_b = await world.service.create_session(p["outra"], world.area_b, URL)
    await world.service.sweep()  # renova as duas
    # A API cai (SIGKILL): ninguém mais renova as linhas, que continuam "abertas".
    restarted = _other_process(world, clock, FakeLauncher())
    try:
        assert await world.store.reserve_in_use(now=clock.now) == 2
        # Logo depois da queda, dentro da folga, nada muda (podia ser outra réplica viva).
        clock.now += STALE_AFTER - timedelta(seconds=5)
        assert await restarted.sweep() == 0
        assert {s.status for s in world.store.sessions.values()} == {RecordingSessionStatus.ACTIVE}
        # Passada a folga, a subida (ou a varredura) encerra as sessões de todas as empresas.
        clock.now += timedelta(seconds=10)
        assert await restarted.sweep() == 2
        for sid, company in (
            (old_a.session_id, world.company_a),
            (old_b.session_id, world.company_b),
        ):
            record = world.store.sessions[company, sid]
            assert (
                record.status is RecordingSessionStatus.EXPIRED and record.end_reason == "perdida"
            )
        assert await world.store.reserve_in_use(now=clock.now) == 0
        # A vaga da empresa voltou.
        await restarted.create_session(p["criadora"], world.area_a, URL)
    finally:
        await restarted.aclose()
        await crashed_launcher.aclose()


async def test_sessions_held_by_another_live_process_are_left_alone() -> None:
    world = build_world()
    clock = Clock()
    world.service._clock = clock
    p = world.people
    held = await world.service.create_session(p["criadora"], world.area_a, URL)
    other = _other_process(world, clock, FakeLauncher())
    try:
        for _ in range(4):
            clock.now += timedelta(minutes=1)
            await world.service.sweep()  # o dono renova a cada varredura
            assert await other.sweep() == 0
            # Ler a sessão de outro processo não a encerra (o navegador não é dele).
            assert (await other.list_sessions(p["criadora"]))[0].status is (
                RecordingSessionStatus.ACTIVE
            )
        assert world.store.sessions[world.company_a, held.session_id].status is (
            RecordingSessionStatus.ACTIVE
        )
        assert world.launcher.terminated == []
    finally:
        await other.aclose()
        await world.service.aclose()
        await world.launcher.aclose()


async def test_a_starting_session_of_a_crashed_api_fails() -> None:
    world = build_world(limits=SessionLimits(per_company=2))
    clock = Clock()
    world.service._clock = clock
    info = await world.service.create_session(world.people["criadora"], world.area_a, URL)
    key = (world.company_a, info.session_id)
    # A API caiu no meio da abertura: a linha ficou "iniciando".
    world.store.sessions[key] = replace(
        world.store.sessions[key], status=RecordingSessionStatus.STARTING, started_at=None
    )
    restarted = _other_process(world, clock, FakeLauncher())
    try:
        clock.now += STALE_AFTER + timedelta(seconds=1)
        assert await restarted.sweep() == 1
        record = world.store.sessions[key]
        assert record.status is RecordingSessionStatus.FAILED and record.end_reason == "nao_iniciou"
    finally:
        await restarted.aclose()
        await world.launcher.aclose()


async def test_the_viewer_replaced_gets_nothing_after_replacement(world: SessionsWorld) -> None:
    p = world.people["criadora"]
    info = await world.service.create_session(p, world.area_a, URL)
    sid = info.session_id
    first, second = QueueViewer(), QueueViewer()
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
    frames_before = len(first.frames)
    second.inbox.put_nowait('{"tipo":"mouse","acao":"mover","x":1,"y":1}')
    await second.wait(lambda: len(second.frames) >= 2)
    await asyncio.sleep(0.2)
    assert len(first.frames) == frames_before
    second.inbox.put_nowait(None)
    await asyncio.gather(w1, w2, return_exceptions=True)
