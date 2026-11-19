"""Fixtures dos testes da interface.

O plugin de testes do NiceGUI (``nicegui.testing.user_plugin``) só pode ser
ligado por ``pytest_plugins`` no conftest da raiz, e a fixture ``user`` dele
monta a aplicação com ``ui.run`` a partir de um arquivo ``main.py``. A nossa
interface é montada com ``ui.run_with`` sobre uma aplicação FastAPI (por causa
das rotas ``/auth``), então aqui fazemos o mesmo que a fixture ``user``:
reiniciamos o estado global do NiceGUI, montamos a aplicação, ligamos o ciclo
de vida e entregamos um :class:`nicegui.testing.User` ligado a ela.
"""

import atexit
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from nicegui import app as nicegui_app
from nicegui.storage import Storage
from nicegui.testing import User
from nicegui.testing.general import nicegui_reset_globals

from rapa.core.contracts import Principal, Role
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.ui.app import create_ui
from rapa.ui.auth import DevLogin, DevProfile, MemorySessionStore, PrincipalResolver, UiAuth
from rapa.ui.backend import ContractsBackendFactory
from rapa.ui.context import UiSettings

# Mesmo isolamento do plugin oficial: o armazenamento do NiceGUI vai para uma pasta temporária.
Storage.path = Path(tempfile.mkdtemp(prefix="rapa-nicegui-test-")).resolve()
atexit.register(shutil.rmtree, Storage.path, ignore_errors=True)
nicegui_app.storage = Storage()

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


@asynccontextmanager
async def simulate(build: Callable[[FastAPI], None]) -> AsyncIterator[User]:
    """Como ``nicegui.testing.user_simulation``, mas com a aplicação montada por ``build``."""
    # Ao sair, o NiceGUI tira de sys.modules os módulos das páginas (fora de "tests.");
    # devolvemos os nossos para não haver duas cópias das mesmas classes entre os testes.
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "rapa"}
    try:
        with nicegui_reset_globals():
            os.environ["NICEGUI_USER_SIMULATION"] = "true"
            try:
                app = FastAPI()
                build(app)
                async with (
                    app.router.lifespan_context(app),
                    httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app), base_url="http://test"
                    ) as client,
                ):
                    yield User(client)
            finally:
                os.environ.pop("NICEGUI_USER_SIMULATION", None)
    finally:
        sys.modules.update(saved)


@dataclass
class World:
    """Duas empresas, uma área em cada, robôs publicados e perfis de login."""

    platform: MemoryPlatform
    company_a: UUID
    company_b: UUID
    area_a: UUID
    area_b: UUID
    principals: dict[str, Principal]
    store: MemorySessionStore = field(default_factory=MemorySessionStore)
    robot_ids: dict[str, UUID] = field(default_factory=dict)
    #: Resolvedor que a sessão consulta para conferir conta e papéis (como o banco).
    resolver: PrincipalResolver | None = None

    def auth(self) -> UiAuth:
        profiles = [
            DevProfile(key, f"Perfil {key}", principal, description=principal.subject)
            for key, principal in self.principals.items()
        ]
        return UiAuth(
            dev=DevLogin(profiles),
            store=self.store,
            cookie_secure=False,
            principals=self.resolver,
            principal_ttl=timedelta(0),
        )

    def settings(self) -> UiSettings:
        return UiSettings(
            poll_interval_s=0.05,
            area_labels={self.area_a: "Fiscal", self.area_b: "Compras"},
            company_labels={self.company_a: "Empresa A", self.company_b: "Empresa B"},
        )

    def build(self, app: FastAPI) -> None:
        create_ui(
            app,
            backend_factory=ContractsBackendFactory.from_platform(self.platform),
            auth=self.auth(),
            settings=self.settings(),
        )

    def user(self) -> AbstractAsyncContextManager[User]:
        return simulate(self.build)


async def make_world() -> World:
    platform = MemoryPlatform()
    company_a, company_b, area_a, area_b = new_id(), new_id(), new_id(), new_id()

    def person(company: UUID, area: UUID, subject: str, *roles: Role) -> Principal:
        return Principal(subject=subject, company_id=company, area_roles={area: frozenset(roles)})

    principals = {
        "criadora": person(company_a, area_a, "criadora@a", Role.CREATOR, Role.APPROVER),
        "aprovador": person(company_a, area_a, "aprovador@a", Role.APPROVER, Role.OPERATOR),
        "operador": person(company_a, area_a, "operador@a", Role.OPERATOR),
        "outra": person(company_b, area_b, "operador@b", Role.OPERATOR),
    }
    world = World(platform, company_a, company_b, area_a, area_b, principals)
    creator_b = person(company_b, area_b, "criador@b", Role.CREATOR)
    approver_b = person(company_b, area_b, "aprovador@b", Role.APPROVER)
    for creator, approver, area, manifest in (
        (principals["criadora"], principals["aprovador"], area_a, emitir_protocolo(BASE, HOST)),
        (creator_b, approver_b, area_b, baixar_notas(BASE, HOST)),
    ):
        info = await platform.create_version(creator, area, manifest)
        await platform.mark_validated(creator, info.robot_id, 1)
        await platform.publish(approver, info.robot_id, 1)
        world.robot_ids[manifest.name] = info.robot_id
    return world


@pytest.fixture
async def world() -> World:
    return await make_world()


@pytest.fixture
def no_error_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Falha o teste se houver registro de ERROR (como a fixture ``user`` do NiceGUI)."""
    caplog.set_level(logging.WARNING)
    yield
    errors = [r for r in caplog.get_records("call") if r.levelno >= logging.ERROR]
    if errors:
        pytest.fail(
            "registros de erro inesperados: " + "; ".join(r.getMessage() for r in errors),
            pytrace=False,
        )


async def login(user: User, profile: str, path: str = "/catalogo") -> None:
    """Abre ``path`` sem sessão, escolhe o perfil no login e espera voltar logado."""
    await user.open(path)
    await user.should_see(marker="pagina-login")
    user.find(marker=f"perfil-{profile}").click()
    await user.should_see(marker="usuario-logado", retries=50)


async def logout(user: User) -> None:
    user.find(marker="sair").click()
    await user.should_see(marker="pagina-login", retries=50)


def values(user: User, marker: str) -> list[Any]:
    """Valores dos elementos com o marcador (para conferir campos de formulário)."""
    return [getattr(e, "value", None) for e in user.find(marker=marker).elements]
