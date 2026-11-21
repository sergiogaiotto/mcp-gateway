"""As telas de gravação pelos dois backends: em processo e pela API REST de verdade."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from rapa.control.auth import StaticTokenVerifier
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.core.contracts import (
    Conflict,
    DraftChoices,
    Forbidden,
    InvalidInput,
    RecordingSessionStatus,
    StepNameChoice,
)
from rapa.core.ids import new_id
from rapa.ui.auth import TokenSet, UiSession
from rapa.ui.backend import (
    SESSIONS_OFF,
    Backend,
    BackendUnavailable,
    ContractsBackendFactory,
    RestBackend,
)
from tests.control.api.support import DictResolver
from tests.control.sessions.support import SessionsWorld, build_world

URL = "https://portal.exemplo.gov.br/login"
TOKEN = "token-de-teste-da-criadora"


@pytest.fixture
async def world() -> AsyncIterator[SessionsWorld]:
    w = build_world()
    yield w
    await w.service.aclose()
    await w.launcher.aclose()


def _session(world: SessionsWorld, who: str) -> UiSession:
    now = datetime.now(UTC)
    return UiSession(
        key="k",
        principal=world.people[who],
        display_name=who,
        method="oidc",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        last_seen=now,
        tokens=TokenSet(TOKEN),
    )


def _rest(world: SessionsWorld, who: str = "criadora") -> RestBackend:
    app = create_http_app(
        catalog=world.platform,
        runs=world.platform,
        admin=world.platform,
        verifier=StaticTokenVerifier({TOKEN: world.people[who]}),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
        sessions=world.service,
    )
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.rapa.test"
    )
    return RestBackend(client, TOKEN)


@pytest.mark.parametrize("kind", ["contratos", "rest"])
async def test_recording_flow_through_the_backend(world: SessionsWorld, kind: str) -> None:
    backend: Backend = (
        ContractsBackendFactory.from_platform(world.platform, sessions=world.service)(
            _session(world, "criadora")
        )
        if kind == "contratos"
        else _rest(world)
    )
    info = await backend.create_session(world.area_a, URL, "Cadastro")
    assert info.status is RecordingSessionStatus.ACTIVE
    assert [s.session_id for s in await backend.list_sessions()] == [info.session_id]
    assert await backend.get_session(info.session_id) == info
    assert await backend.get_session(new_id()) is None
    ticket = await backend.issue_ticket(info.session_id)
    assert ticket.path.endswith("/ao-vivo") and len(ticket.ticket) >= 32
    with pytest.raises(Conflict):
        await backend.get_proposals(info.session_id)
    stopped = await backend.stop_session(info.session_id)
    assert stopped.status is RecordingSessionStatus.STOPPED
    proposals = await backend.get_proposals(info.session_id)
    assert not proposals.model_available
    choices = DraftChoices(
        name="cadastro_gravado",
        title="Cadastro",
        description="Gravado.",
        step_names=[StepNameChoice(step_id="fantasma", title="X")],
    )
    with pytest.raises(InvalidInput) as caught:
        await backend.create_draft(info.session_id, choices)
    assert caught.value.errors
    version = await backend.create_draft(
        info.session_id, choices.model_copy(update={"step_names": []})
    )
    assert version.version_no == 1 and version.manifest.name == "cadastro_gravado"
    with pytest.raises(Forbidden):
        await backend.create_session(world.area_b, URL)


async def test_without_sessions_the_backends_say_so(world: SessionsWorld) -> None:
    backend = ContractsBackendFactory.from_platform(world.platform)(_session(world, "criadora"))
    with pytest.raises(BackendUnavailable) as caught:
        await backend.list_sessions()
    assert caught.value.message == SESSIONS_OFF
    app = create_http_app(
        catalog=world.platform,
        runs=world.platform,
        admin=world.platform,
        verifier=StaticTokenVerifier({TOKEN: world.people["criadora"]}),
        resolver=DictResolver(),
        settings=HttpSettings(allow_static_tokens=True, rate_limit_per_s=None),
    )
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://api")
    with pytest.raises(BackendUnavailable) as caught:
        await RestBackend(client, TOKEN).list_sessions()
    assert caught.value.message == SESSIONS_OFF
