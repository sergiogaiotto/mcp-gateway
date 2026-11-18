"""Erros inesperados: página genérica em pt-BR, sem detalhes da exceção."""

from collections.abc import Callable

from fastapi import FastAPI

from rapa.core.contracts import RobotSummary
from rapa.core.ids import new_id
from rapa.ui.app import create_ui
from rapa.ui.auth import UiSession
from rapa.ui.backend import Backend, BackendUnavailable, ContractsBackend
from tests.ui.conftest import World, login, simulate

SECRET_DETAIL = "detalhe-interno-que-nao-pode-vazar"


class Exploding(ContractsBackend):
    async def list_catalog(self) -> list[RobotSummary]:
        raise RuntimeError(SECRET_DETAIL)


class Unavailable(ContractsBackend):
    async def list_catalog(self) -> list[RobotSummary]:
        raise BackendUnavailable("o serviço do RaPA não respondeu; tente de novo em instantes")


def _build(world: World, backend_cls: type[ContractsBackend]) -> Callable[[FastAPI], None]:
    def factory(session: UiSession) -> Backend:
        return backend_cls(world.platform, world.platform, world.platform, session.principal)

    def build(app: FastAPI) -> None:
        create_ui(app, backend_factory=factory, auth=world.auth(), settings=world.settings())

    return build


async def test_unexpected_error_page_hides_details(world: World) -> None:
    async with simulate(_build(world, Exploding)) as user:
        # Entra por uma página que não usa o catálogo (detalhe de execução inexistente).
        await login(user, "operador", f"/execucoes/{new_id()}")
        response = await user.http_client.get("/catalogo")
        assert response.status_code == 500
        assert "Algo deu errado ao montar esta página" in response.text
        assert SECRET_DETAIL not in response.text
        assert "RuntimeError" not in response.text


async def test_backend_unavailable_shows_friendly_message(world: World) -> None:
    async with simulate(_build(world, Unavailable)) as user:
        await login(user, "operador")
        await user.should_see(marker="erro")
        await user.should_see("O serviço do RaPA não respondeu; tente de novo em instantes")
