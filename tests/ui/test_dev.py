"""Modo de desenvolvimento: 2 empresas × 2 áreas sobre a plataforma em memória."""

from fastapi import FastAPI

from rapa.ui.dev import DevWorld, build_dev_ui, seed_dev_world
from tests.ui.conftest import login, simulate


def test_seed_two_companies_two_areas() -> None:
    world = seed_dev_world()
    assert len(world.company_labels) == 2
    assert sorted(world.area_labels.values()) == ["Compras", "Compras", "Fiscal", "Fiscal"]
    assert len(world.platform.robots) == 3
    assert all(robot.current_version_no == 1 for robot in world.platform.robots.values())


async def test_dev_ui_isolates_companies() -> None:
    worlds: list[DevWorld] = []

    def build(app: FastAPI) -> None:
        worlds.append(build_dev_ui(app))

    async with simulate(build) as user:
        await login(user, "alfa-admin")
        await user.should_see(marker="robo-emitir_protocolo")
        await user.should_see(marker="robo-baixar_notas")
        await user.should_see("Empresa Alfa")
        await user.open("/robos")
        await user.should_see("Fiscal", retries=5)
        user.find(marker="sair").click()
        await user.should_see(marker="pagina-login", retries=50)
        await login(user, "beta-operadora")
        await user.should_see(marker="robo-baixar_notas")
        await user.should_not_see(marker="robo-emitir_protocolo")
    assert len(worlds) == 1
