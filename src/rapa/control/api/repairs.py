"""Rotas do conserto: ``/v1/execucoes/{id}/conserto``.

- ``GET`` devolve o passo que quebrou, os candidatos que o código enumerou a partir
  do retrato da página (nada é executado de novo) e a sugestão do modelo de
  linguagem, que só escolhe um deles (``?sugestao=false`` não pergunta ao modelo).
  Com o hub fora do ar, a resposta vem do mesmo jeito, com a sugestão
  ``indisponivel``: a pessoa escolhe sozinha.
- ``POST`` com ``{"candidato": "c2"}`` cria o rascunho da próxima versão do robô,
  com o alvo daquele passo trocado, pelo serviço de robôs (validação, portão de
  política e auditoria). Responde 201 com o conserto e ``Location`` da versão nova,
  que outra pessoa, com o papel de aprovação, valida e publica como qualquer outra.

Para outra área e outra empresa, a execução não existe (404); sem o papel de criação
ou de aprovação na área, 403; robôs com certificado A1, 403 (o conserto assistido
fica desligado); uma execução que não falhou por causa do alvo, sem retrato, de uma
versão antiga ou já consertada, 409. Sem o serviço configurado, 503. Nada disso entra
no MCP.
"""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from rapa.core.contracts import (
    Principal,
    RepairProposalInfo,
    RepairService,
    RepairView,
    ServiceUnavailable,
)

__all__ = ["CreateRepairBody", "add_repair_routes"]


class CreateRepairBody(BaseModel):
    """Corpo de ``POST /v1/execucoes/{id}/conserto``."""

    model_config = ConfigDict(extra="forbid")

    candidato: str = Field(pattern=r"^c[0-9]{1,3}$", description="Um dos candidatos listados")


def add_repair_routes(
    router: APIRouter, service: RepairService | None, caller: Callable[..., Any]
) -> None:
    """Acrescenta as rotas do conserto ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def repairs() -> RepairService:
        if service is None:
            raise ServiceUnavailable("o conserto assistido não está disponível neste servidor")
        return service

    @router.get("/execucoes/{run_id}/conserto", response_model=RepairView)
    async def get_repair(
        run_id: UUID,
        principal: Annotated[Principal, caller_dep],
        sugestao: Annotated[bool, Query()] = True,
    ) -> RepairView:
        return await repairs().get_repair(principal, run_id, suggest=sugestao)

    @router.post("/execucoes/{run_id}/conserto", status_code=201, response_model=RepairProposalInfo)
    async def create_repair(
        run_id: UUID,
        body: CreateRepairBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> RepairProposalInfo:
        proposal = await repairs().create_repair(principal, run_id, body.candidato)
        response.headers["Location"] = (
            f"/v1/robos/{proposal.robot_id}/versoes/{proposal.version_no}"
        )
        return proposal
