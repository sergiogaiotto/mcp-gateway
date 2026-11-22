"""Rotas das credenciais e das regras do certificado A1.

- ``GET /v1/credenciais`` (``?area_id=``) lista as credenciais registradas das áreas
  que a pessoa vê: nome, tipo, origem, custodiante, validade e situação. O caminho no
  cofre só vem para quem administra a área e para o custodiante. ``POST`` registra
  (só quem administra a área; o valor nunca passa pelo RaPA, só a referência).
- ``GET /v1/credenciais/vencimentos`` (``?dias=``, padrão ``RAPA_A1_AVISO_DIAS``): os
  certificados A1 que vencem dentro do prazo ou já venceram.
- ``PATCH /v1/credenciais/{id}`` renova a validade (só o custodiante); uma impressão
  nova revoga as autorizações da anterior, e sem impressão só renova um certificado
  registrado sem ela (com uma registrada, 422). ``POST .../revogar`` revoga a
  credencial e as autorizações dela (custodiante ou administrador da área).
- ``GET/POST /v1/robos/{id}/autorizacoes-a1``: as autorizações do custodiante para o
  robô usar um certificado A1, e a autorização nova (só o custodiante);
  ``POST .../{aid}/revogar`` revoga (custodiante ou administrador da área).
- ``GET /v1/robos/{id}/versoes/{n}/aprovacoes``: quem aprovou, quantas faltam e o que
  falta no certificado A1.

Outra área e outra empresa: 404. As respostas com credenciais levam
``Cache-Control: no-store``. Sem o serviço configurado, 503. Nada disso entra no MCP.
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rapa.core.contracts import (
    A1AuthorizationInfo,
    CredentialInfo,
    CredentialService,
    Principal,
    ServiceUnavailable,
    VersionApprovals,
)
from rapa.core.robot import NAME_PATTERN, CredentialKind

__all__ = [
    "AuthorizeBody",
    "RegisterCredentialBody",
    "RenewCredentialBody",
    "add_credential_routes",
]

_NO_STORE = "no-store"


class RegisterCredentialBody(BaseModel):
    """Corpo de ``POST /v1/credenciais``: só a referência ao cofre, nunca o valor."""

    model_config = ConfigDict(extra="forbid")

    area_id: UUID
    nome: str = Field(min_length=1, max_length=64)
    tipo: CredentialKind
    origem: str = Field(min_length=1, max_length=300, description="https://host[:porta]")
    caminho_cofre: str = Field(min_length=1, max_length=500)
    custodiante: str | None = Field(
        default=None, max_length=255, description="Sujeito do custodiante (obrigatório no A1)"
    )
    validade: AwareDatetime | None = Field(
        default=None, description="Fim da validade do certificado A1"
    )
    impressao: str | None = Field(
        default=None, max_length=200, description="Impressão SHA-256 do certificado"
    )


class RenewCredentialBody(BaseModel):
    """Corpo de ``PATCH /v1/credenciais/{id}`` (o certificado renovado no cofre)."""

    model_config = ConfigDict(extra="forbid")

    validade: AwareDatetime
    impressao: str | None = Field(default=None, max_length=200)


class AuthorizeBody(BaseModel):
    """Corpo de ``POST /v1/robos/{id}/autorizacoes-a1``."""

    model_config = ConfigDict(extra="forbid")

    credencial: str = Field(pattern=NAME_PATTERN, description="Nome da credencial no robô")


def add_credential_routes(
    router: APIRouter, service: CredentialService | None, caller: Callable[..., Any]
) -> None:
    """Acrescenta as rotas das credenciais e do certificado A1 ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def credentials() -> CredentialService:
        if service is None:
            raise ServiceUnavailable("as credenciais não estão disponíveis neste servidor")
        return service

    @router.get("/credenciais", response_model=list[CredentialInfo])
    async def list_credentials(
        response: Response,
        principal: Annotated[Principal, caller_dep],
        area_id: Annotated[UUID | None, Query()] = None,
    ) -> list[CredentialInfo]:
        response.headers["Cache-Control"] = _NO_STORE
        return await credentials().list(principal, area_id)

    @router.post("/credenciais", status_code=201, response_model=CredentialInfo)
    async def register_credential(
        body: RegisterCredentialBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> CredentialInfo:
        info = await credentials().register(
            principal,
            body.area_id,
            name=body.nome,
            kind=body.tipo,
            origin=body.origem,
            vault_path=body.caminho_cofre,
            custodian_subject=body.custodiante,
            not_after=body.validade,
            fingerprint=body.impressao,
        )
        response.headers["Cache-Control"] = _NO_STORE
        response.headers["Location"] = f"/v1/credenciais/{info.credential_id}"
        return info

    @router.get("/credenciais/vencimentos", response_model=list[CredentialInfo])
    async def expiring_credentials(
        response: Response,
        principal: Annotated[Principal, caller_dep],
        dias: Annotated[int | None, Query(ge=0, le=3650)] = None,
    ) -> list[CredentialInfo]:
        response.headers["Cache-Control"] = _NO_STORE
        return await credentials().expiring(principal, dias)

    @router.patch("/credenciais/{credential_id}", response_model=CredentialInfo)
    async def renew_credential(
        credential_id: UUID,
        body: RenewCredentialBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> CredentialInfo:
        response.headers["Cache-Control"] = _NO_STORE
        return await credentials().renew(
            principal, credential_id, not_after=body.validade, fingerprint=body.impressao
        )

    @router.post("/credenciais/{credential_id}/revogar", response_model=CredentialInfo)
    async def revoke_credential(
        credential_id: UUID, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> CredentialInfo:
        response.headers["Cache-Control"] = _NO_STORE
        return await credentials().revoke(principal, credential_id)

    @router.get("/robos/{robot_id}/autorizacoes-a1", response_model=list[A1AuthorizationInfo])
    async def list_authorizations(
        robot_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> list[A1AuthorizationInfo]:
        return await credentials().list_authorizations(principal, robot_id)

    @router.post(
        "/robos/{robot_id}/autorizacoes-a1", status_code=201, response_model=A1AuthorizationInfo
    )
    async def authorize_robot(
        robot_id: UUID, body: AuthorizeBody, principal: Annotated[Principal, caller_dep]
    ) -> A1AuthorizationInfo:
        return await credentials().authorize(principal, robot_id, body.credencial)

    @router.post(
        "/robos/{robot_id}/autorizacoes-a1/{authorization_id}/revogar",
        response_model=A1AuthorizationInfo,
    )
    async def revoke_authorization(
        robot_id: UUID, authorization_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> A1AuthorizationInfo:
        return await credentials().revoke_authorization(
            principal, authorization_id, robot_id=robot_id
        )

    @router.get("/robos/{robot_id}/versoes/{n}/aprovacoes", response_model=VersionApprovals)
    async def version_approvals(
        robot_id: UUID,
        n: Annotated[int, Path(ge=1)],
        principal: Annotated[Principal, caller_dep],
    ) -> VersionApprovals:
        return await credentials().approvals(principal, robot_id, n)
