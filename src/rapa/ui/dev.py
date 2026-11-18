"""Interface em modo de desenvolvimento, num processo só, sobre a plataforma em memória.

Sobe com ``python -m rapa.ui`` (precisa dos extras ``ui`` e ``fixtures``) e
abre em http://127.0.0.1:8080. Há duas empresas com duas áreas cada, os robôs de
exemplo publicados e um login de desenvolvimento com um perfil por papel.
As execuções ficam na fila: não há executor neste modo.

Nunca use este modo em produção: o login de desenvolvimento não pede senha.
"""

import argparse
import asyncio
import logging
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI

from rapa.core.contracts import Principal, Role
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.ui.app import create_ui
from rapa.ui.auth import DevLogin, DevProfile, UiAuth
from rapa.ui.backend import ContractsBackendFactory
from rapa.ui.context import UiSettings

__all__ = ["DevWorld", "build_dev_ui", "create_dev_app", "main", "seed_dev_world"]

logger = logging.getLogger(__name__)

DEFAULT_PORTAL_URL = "http://portal-v1.rapa.test:8080"


@dataclass(frozen=True)
class DevWorld:
    platform: MemoryPlatform
    profiles: list[DevProfile]
    area_labels: dict[UUID, str]
    company_labels: dict[UUID, str]


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Roda a corrotina até o fim, mesmo se já houver um laço de eventos rodando."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _seed(portal_url: str) -> DevWorld:
    platform = MemoryPlatform()
    host = urlsplit(portal_url).hostname or "portal-v1.rapa.test"
    alfa, beta = new_id(), new_id()
    areas = {
        (company, name): new_id() for company in (alfa, beta) for name in ("Fiscal", "Compras")
    }
    fiscal_a, compras_a = areas[(alfa, "Fiscal")], areas[(alfa, "Compras")]
    fiscal_b = areas[(beta, "Fiscal")]

    def person(company: UUID, subject: str, roles: dict[UUID, set[Role]]) -> Principal:
        return Principal(
            subject=subject,
            company_id=company,
            area_roles={area: frozenset(r) for area, r in roles.items()},
        )

    criadora = person(alfa, "criadora@alfa", {fiscal_a: {Role.CREATOR}, compras_a: {Role.CREATOR}})
    aprovador = person(
        alfa,
        "aprovador@alfa",
        {fiscal_a: {Role.APPROVER, Role.OPERATOR}, compras_a: {Role.APPROVER, Role.OPERATOR}},
    )
    operador = person(alfa, "operador@alfa", {fiscal_a: {Role.OPERATOR}})
    admin = Principal(
        subject="admin@alfa", company_id=alfa, company_roles=frozenset({Role.COMPANY_ADMIN})
    )
    beta_criador = person(beta, "criador@beta", {fiscal_b: {Role.CREATOR}})
    beta_operadora = person(beta, "operadora@beta", {fiscal_b: {Role.OPERATOR, Role.APPROVER}})

    for creator, approver, area, manifest in (
        (criadora, aprovador, fiscal_a, emitir_protocolo(portal_url, host)),
        (criadora, aprovador, compras_a, baixar_notas(portal_url, host)),
        (beta_criador, beta_operadora, fiscal_b, baixar_notas(portal_url, host)),
    ):
        info = await platform.create_version(creator, area, manifest)
        await platform.mark_validated(creator, info.robot_id, 1)
        await platform.publish(approver, info.robot_id, 1)

    profiles = [
        DevProfile(
            "alfa-criadora",
            "Criadora · Empresa Alfa",
            criadora,
            "Cria robôs no Fiscal e em Compras",
        ),
        DevProfile(
            "alfa-aprovador", "Aprovador · Empresa Alfa", aprovador, "Aprova e executa robôs"
        ),
        DevProfile("alfa-operador", "Operador · Empresa Alfa", operador, "Executa robôs do Fiscal"),
        DevProfile(
            "alfa-admin", "Administradora · Empresa Alfa", admin, "Administra a empresa toda"
        ),
        DevProfile(
            "beta-operadora",
            "Operadora · Empresa Beta",
            beta_operadora,
            "Executa e aprova no Fiscal",
        ),
    ]
    labels = {area: name for (_, name), area in areas.items()}
    return DevWorld(platform, profiles, labels, {alfa: "Empresa Alfa", beta: "Empresa Beta"})


def seed_dev_world(portal_url: str = DEFAULT_PORTAL_URL) -> DevWorld:
    """Plataforma em memória com 2 empresas × 2 áreas, robôs publicados e perfis."""
    return _run_sync(_seed(portal_url))


def build_dev_ui(app: FastAPI, *, portal_url: str = DEFAULT_PORTAL_URL) -> DevWorld:
    """Monta a interface de desenvolvimento em ``app`` e devolve o mundo criado."""
    world = seed_dev_world(portal_url)
    logger.warning("login de desenvolvimento ligado: não use este modo em produção")
    create_ui(
        app,
        backend_factory=ContractsBackendFactory.from_platform(world.platform),
        auth=UiAuth(dev=DevLogin(world.profiles), cookie_secure=False),
        settings=UiSettings(area_labels=world.area_labels, company_labels=world.company_labels),
    )
    return world


def create_dev_app(*, portal_url: str = DEFAULT_PORTAL_URL) -> FastAPI:
    app = FastAPI(title="RaPA (desenvolvimento)", docs_url=None, redoc_url=None, openapi_url=None)
    build_dev_ui(app, portal_url=portal_url)
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Interface do RaPA em modo de desenvolvimento")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--portal-url", default=DEFAULT_PORTAL_URL)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(create_dev_app(portal_url=args.portal_url), host=args.host, port=args.port)
