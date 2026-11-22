"""Rotas da administração da empresa por ela mesma: ``/v1/empresa``.

- ``GET /v1/empresa``: a empresa de quem chama (nome e situação; o motivo do
  congelamento só para quem administra ou audita).
- ``GET /v1/empresa/usuarios``: os usuários com os papéis (quem administra a empresa
  vê todos; quem administra áreas, os das áreas dele e os da empresa inteira).
- ``PUT /v1/empresa/usuarios/{id}/papeis`` com ``{"papeis": [{"papel": "creator",
  "area_id": "..."}]}``: troca os papéis que quem chama administra pelos da lista (os
  outros ficam como estão; ``area_id`` nulo é a empresa inteira).
- ``POST /v1/empresa/usuarios/{id}/desativar`` e ``.../reativar``.
- ``GET`` e ``POST /v1/empresa/areas`` (``{"nome": "Fiscal"}``) e
  ``PATCH /v1/empresa/areas/{id}`` (renomear).
- ``GET /v1/empresa/consumo?mes=AAAA-MM``: o consumo do mês (``America/Sao_Paulo``),
  por área e por origem; sem ``mes``, o mês de agora.

As regras ficam no serviço (:mod:`rapa.control.services.company`): sem o papel, 403;
usuário ou área de outra empresa, 404; tirar o último administrador ativo, desativar a
si mesmo ou repetir o nome de uma área, 409; empresa congelada, 423 (dá para tirar
papéis e desativar pessoas). Sem o serviço configurado, 503. Nada disso entra no MCP.
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from rapa.core.contracts import (
    AreaInfo,
    CompanyAdminService,
    CompanyInfo,
    CompanyUser,
    Principal,
    Role,
    RoleGrant,
    ServiceUnavailable,
    UsageReport,
)

__all__ = ["AreaBody", "RoleBody", "RolesBody", "add_company_routes"]

_MONTH = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class RoleBody(BaseModel):
    """Um papel: ``area_id`` nulo é a empresa inteira."""

    model_config = ConfigDict(extra="forbid")

    papel: Role
    area_id: UUID | None = None


class RolesBody(BaseModel):
    """Corpo de ``PUT /v1/empresa/usuarios/{id}/papeis``: a lista inteira, no escopo de quem chama."""

    model_config = ConfigDict(extra="forbid")

    papeis: list[RoleBody] = Field(max_length=200)


class AreaBody(BaseModel):
    """Corpo de ``POST /v1/empresa/areas`` e ``PATCH /v1/empresa/areas/{id}``."""

    model_config = ConfigDict(extra="forbid")

    nome: str = Field(min_length=1, max_length=200)


def add_company_routes(
    router: APIRouter, service: CompanyAdminService | None, caller: Callable[..., Any]
) -> None:
    """Acrescenta as rotas de ``/v1/empresa`` ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def company() -> CompanyAdminService:
        if service is None:
            raise ServiceUnavailable(
                "a administração da empresa não está disponível neste servidor"
            )
        return service

    @router.get("/empresa", response_model=CompanyInfo)
    async def get_company(principal: Annotated[Principal, caller_dep]) -> CompanyInfo:
        return await company().get_company(principal)

    @router.get("/empresa/usuarios", response_model=list[CompanyUser])
    async def list_users(principal: Annotated[Principal, caller_dep]) -> list[CompanyUser]:
        return await company().list_users(principal)

    @router.put("/empresa/usuarios/{user_id}/papeis", response_model=CompanyUser)
    async def set_roles(
        user_id: UUID, body: RolesBody, principal: Annotated[Principal, caller_dep]
    ) -> CompanyUser:
        grants = [RoleGrant(role=item.papel, area_id=item.area_id) for item in body.papeis]
        return await company().set_roles(principal, user_id, grants)

    @router.post("/empresa/usuarios/{user_id}/desativar", response_model=CompanyUser)
    async def deactivate_user(
        user_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> CompanyUser:
        return await company().deactivate_user(principal, user_id)

    @router.post("/empresa/usuarios/{user_id}/reativar", response_model=CompanyUser)
    async def reactivate_user(
        user_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> CompanyUser:
        return await company().reactivate_user(principal, user_id)

    @router.get("/empresa/areas", response_model=list[AreaInfo])
    async def list_areas(principal: Annotated[Principal, caller_dep]) -> list[AreaInfo]:
        return await company().list_areas(principal)

    @router.post("/empresa/areas", status_code=201, response_model=AreaInfo)
    async def create_area(
        body: AreaBody, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> AreaInfo:
        info = await company().create_area(principal, body.nome)
        response.headers["Location"] = f"/v1/empresa/areas/{info.area_id}"
        return info

    @router.patch("/empresa/areas/{area_id}", response_model=AreaInfo)
    async def rename_area(
        area_id: UUID, body: AreaBody, principal: Annotated[Principal, caller_dep]
    ) -> AreaInfo:
        return await company().rename_area(principal, area_id, body.nome)

    @router.get("/empresa/consumo", response_model=UsageReport)
    async def usage(
        principal: Annotated[Principal, caller_dep],
        mes: Annotated[str | None, Query(pattern=_MONTH)] = None,
    ) -> UsageReport:
        return await company().usage(principal, mes)
