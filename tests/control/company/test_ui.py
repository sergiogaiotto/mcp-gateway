"""Tela Empresa e a tela de quem ainda não tem papel, com o ``User`` do NiceGUI e o PostgreSQL."""

import secrets
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from nicegui.testing import User
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgCompanyAdmin, PgPlatform, PgPrincipalResolver
from rapa.core.contracts import Principal, Role
from rapa.ui.app import create_ui
from rapa.ui.auth import DevLogin, DevProfile, MemorySessionStore, UiAuth
from rapa.ui.backend import ContractsBackendFactory
from rapa.ui.context import UiSettings
from tests.control.db.support import Tenant, make_tenant
from tests.ui.conftest import login, no_error_logs, simulate  # noqa: F401

from .support import freeze, person

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("no_error_logs")]


@dataclass
class Ui:
    a: Tenant
    people: dict[str, Principal]
    app_db: Database

    def user(self) -> AbstractAsyncContextManager[User]:
        auth = UiAuth(
            dev=DevLogin(
                DevProfile(key, f"Perfil {key}", p, description=p.subject)
                for key, p in self.people.items()
            ),
            store=MemorySessionStore(),
            cookie_secure=False,
            principals=PgPrincipalResolver(self.app_db),
            principal_ttl=timedelta(0),
        )

        def build(app: FastAPI) -> None:
            create_ui(
                app,
                backend_factory=ContractsBackendFactory.from_platform(
                    PgPlatform(self.app_db), company=PgCompanyAdmin(self.app_db)
                ),
                auth=auth,
                settings=UiSettings(poll_interval_s=0.05),
            )

        return simulate(build)


@pytest.fixture
async def ui_world(app_db: Database) -> AsyncIterator[Ui]:
    a = await make_tenant(app_db, "Empresa A")
    tag = secrets.token_hex(4)
    boss = await person(app_db, a.company_id, f"chefe-{tag}@a", (Role.AREA_ADMIN, a.area_id))
    auditor = await person(app_db, a.company_id, f"auditora-{tag}@a", (Role.AUDITOR, None))
    newcomer = await person(app_db, a.company_id, f"nova-{tag}@a")
    yield Ui(
        a,
        {
            "admin": a.admin,
            "chefe": boss,
            "auditora": auditor,
            "operador": a.operator,
            "nova": newcomer,
        },
        app_db,
    )


def _set(user: User, marker: str, value: Any) -> None:
    for element in user.find(marker=marker).elements:
        element.value = value


async def test_company_admin_gives_a_role_and_creates_an_area(ui_world: Ui) -> None:
    a = ui_world.a
    async with ui_world.user() as user:
        await login(user, "admin", "/empresa")
        await user.should_see(marker="empresa-situacao", retries=50)
        await user.should_see(marker="lista-usuarios")
        await user.should_see(marker="areas-empresa")
        await user.should_see(marker="consumo")
        operator = a.operator.user_id
        assert operator is not None
        await user.should_see(marker=f"papel-{operator}-operator-{a.area_id}")
        _set(user, f"papel-novo-{operator}", "creator")
        _set(user, f"area-nova-{operator}", str(a.other_area_id))
        user.find(marker=f"dar-papel-{operator}").click()
        await user.should_see(marker=f"papel-{operator}-creator-{a.other_area_id}", retries=50)
        _set(user, "nova-area", "Compras")
        user.find(marker="criar-area").click()
        await user.should_see("Área criada.", retries=50)
    resolved = await PgPrincipalResolver(ui_world.app_db).resolve(a.operator.subject, a.company_id)
    assert resolved is not None and resolved.area_roles[a.other_area_id] == {Role.CREATOR}
    names = {area.name for area in await PgCompanyAdmin(ui_world.app_db).list_areas(a.admin)}
    assert "Compras" in names


async def test_area_admin_and_auditor_see_only_their_part(ui_world: Ui) -> None:
    async with ui_world.user() as user:
        await login(user, "chefe", "/empresa")
        await user.should_see(marker="so-minhas-areas", retries=50)
        await user.should_see(marker="nav-empresa")
        await user.should_not_see(marker="areas-empresa")
        await user.should_not_see(marker="consumo")
    async with ui_world.user() as user:
        await login(user, "auditora", "/empresa")
        await user.should_see(marker="consumo", retries=50)
        await user.should_see(marker="consumo-total")
        await user.should_not_see(marker="lista-usuarios")
    async with ui_world.user() as user:
        await login(user, "operador", "/catalogo")
        await user.should_not_see(marker="nav-empresa")
        await user.open("/empresa")
        await user.should_see(marker="sem-permissao-empresa", retries=50)


async def test_newcomer_without_roles_only_sees_the_request(ui_world: Ui) -> None:
    async with ui_world.user() as user:
        await login(user, "nova", "/catalogo")
        await user.should_see(marker="sem-papel", retries=50)
        await user.should_see("Peça a um administrador da empresa para dar um papel.")
        await user.should_not_see(marker="nav-catalogo")
        await user.should_not_see(marker="lista-catalogo")
        await user.open("/empresa")
        await user.should_see(marker="sem-papel", retries=50)


async def test_frozen_company_shows_the_banner(ui_world: Ui, owner_engine: AsyncEngine) -> None:
    await freeze(owner_engine, ui_world.a.company_id, "fim do contrato")
    async with ui_world.user() as user:
        await login(user, "admin", "/empresa")
        await user.should_see(marker="empresa-congelada", retries=50)
        await user.should_see("Motivo: fim do contrato")
        _set(user, "nova-area", "Outra")
        user.find(marker="criar-area").click()
        await user.should_see("A empresa está congelada", retries=50)
