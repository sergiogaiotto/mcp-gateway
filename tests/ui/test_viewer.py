"""As ações da interface usam os papéis atuais de quem está logado, não os do login."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from rapa.core.contracts import Forbidden, Principal, Role, VersionStatus
from rapa.fixtures.robots import emitir_protocolo
from rapa.ui.auth import DevLogin, DevProfile, MemorySessionStore, UiAuth
from rapa.ui.backend import BackendUnavailable, ContractsBackendFactory, NotAuthenticated
from rapa.ui.context import UiContext, UiSettings, Viewer
from tests.ui.conftest import BASE, HOST, World

INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Database:
    """Papéis como o banco os guarda: o teste desativa contas e tira papéis."""

    def __init__(self, principals: dict[str, Principal]) -> None:
        self.by_subject = {p.subject: p for p in principals.values()}
        self.down = False

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        if self.down:
            raise ConnectionError("banco fora do ar")
        principal = self.by_subject.get(subject)
        return principal if principal is not None and principal.company_id == company_id else None


@dataclass
class Setup:
    auth: UiAuth
    store: MemorySessionStore
    ctx: UiContext
    db: Database
    clock: Clock
    #: Robô com a versão 2 validada pela criadora, esperando quem aprova.
    robot_id: UUID

    async def viewer(self, principal: Principal) -> Viewer:
        _, session = await self.auth.start_session(principal, principal.subject, "dev")
        return Viewer(self.ctx, session)


async def _setup(world: World) -> Setup:
    clock, db, store = Clock(), Database(world.principals), MemorySessionStore()
    profiles = [DevProfile(key, key, p) for key, p in world.principals.items()]
    auth = UiAuth(
        dev=DevLogin(profiles), store=store, cookie_secure=False, clock=clock, principals=db
    )
    ctx = UiContext(ContractsBackendFactory.from_platform(world.platform), auth, UiSettings())
    manifest = emitir_protocolo(BASE, HOST).model_copy(update={"title": "Versão 2"})
    creator = world.principals["criadora"]
    info = await world.platform.create_version(creator, world.area_a, manifest)
    await world.platform.mark_validated(creator, info.robot_id, info.version_no)
    assert info.version_no == 2
    return Setup(auth, store, ctx, db, clock, info.robot_id)


def _with_area_roles(principal: Principal, area_id: UUID, *roles: Role) -> Principal:
    return principal.model_copy(update={"area_roles": {area_id: frozenset(roles)}})


async def test_deactivated_users_cannot_publish_or_run_from_an_open_session(
    world: World,
) -> None:
    setup = await _setup(world)
    approver = await setup.viewer(world.principals["aprovador"])
    operator = await setup.viewer(world.principals["operador"])
    # Às 9h as duas pessoas usam a interface normalmente.
    await approver.backend()
    await (await operator.backend()).list_runs()

    # Às 9h10 o administrador desativa as duas contas (o resolvedor passa a dar None).
    del setup.db.by_subject["aprovador@a"], setup.db.by_subject["operador@a"]
    setup.clock.now += timedelta(minutes=10)

    with pytest.raises(NotAuthenticated):
        await approver.backend()
    with pytest.raises(NotAuthenticated):
        await operator.backend()
    # A sessão acabou de vez: nem a ação seguinte passa.
    with pytest.raises(NotAuthenticated):
        await approver.backend()
    assert len(setup.store) == 0
    versions = await world.platform.list_versions(world.principals["criadora"], setup.robot_id)
    assert versions[-1].status is VersionStatus.VALIDATED
    assert await world.platform.list_recent(world.principals["criadora"], 10) == []


async def test_removed_role_takes_effect_without_logging_out(world: World) -> None:
    setup = await _setup(world)
    before = world.principals["aprovador"]
    approver = await setup.viewer(before)
    await approver.backend()

    # Continua ativo, mas perde o papel de aprovador (fica só operador).
    operator_only = _with_area_roles(before, world.area_a, Role.OPERATOR)
    setup.db.by_subject[before.subject] = operator_only
    setup.clock.now += timedelta(minutes=1)

    backend = await approver.backend()
    assert approver.principal == operator_only
    with pytest.raises(Forbidden):
        await backend.publish_version(setup.robot_id, 2)
    # Ainda pode o que o papel atual permite.
    run = await (await approver.backend()).start_run("emitir_protocolo", INPUTS)
    assert run.robot_slug == "emitir_protocolo"
    assert len(setup.store) == 1


async def test_new_role_takes_effect_and_other_tabs_keep_working(world: World) -> None:
    setup = await _setup(world)
    principal = world.principals["operador"]
    _, session = await setup.auth.start_session(principal, principal.subject, "dev")
    tab_1, tab_2 = Viewer(setup.ctx, session), Viewer(setup.ctx, session)
    await tab_1.backend()

    promoted = _with_area_roles(principal, world.area_a, Role.APPROVER, Role.OPERATOR)
    setup.db.by_subject[principal.subject] = promoted
    setup.clock.now += timedelta(minutes=1)

    published = await (await tab_1.backend()).publish_version(setup.robot_id, 2)
    assert published.status is VersionStatus.PUBLISHED
    assert published.approved_by == principal.subject
    # A outra aba (aberta com os papéis antigos) segue funcionando, já com os novos.
    await tab_2.backend()
    assert tab_2.principal == promoted


async def test_unavailable_database_blocks_actions_without_logging_out(world: World) -> None:
    setup = await _setup(world)
    operator = await setup.viewer(world.principals["operador"])
    await operator.backend()
    setup.db.down = True
    setup.clock.now += timedelta(minutes=1)
    with pytest.raises(BackendUnavailable) as caught:
        await operator.backend()
    assert "fora do ar" not in caught.value.message
    assert len(setup.store) == 1
    setup.db.down = False
    await operator.backend()
