"""Tela "Consertar este passo" com o ``User`` simulado do NiceGUI e um conserto em memória."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from nicegui.testing import User

from rapa.core.contracts import (
    Forbidden,
    Principal,
    RepairProposalInfo,
    RepairSuggestion,
    RepairSuggestionStatus,
    RepairView,
    RunState,
    RunTrigger,
)
from rapa.core.ids import new_id
from rapa.repair import enumerate_candidates, step_info
from tests.repair.support import login_page, protocolo
from tests.ui.conftest import World, login

INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}

pytestmark = pytest.mark.usefixtures("no_error_logs")


class MemoryRepairs:
    """O conserto com os candidatos do retrato da página de login do v2."""

    def __init__(self, world: World, *, forbidden: str | None = None) -> None:
        self.world = world
        self.forbidden = forbidden
        self.created: list[tuple[str, UUID, str]] = []

    def _view(self, run_id: UUID) -> RepairView:
        step = protocolo().step_by_id("entrar")
        assert step is not None
        return RepairView(
            run_id=run_id,
            robot_id=self.world.robot_ids["emitir_protocolo"],
            robot_slug="emitir_protocolo",
            area_id=self.world.area_a,
            version_no=1,
            error_code="elemento_nao_encontrado",
            error_message="Não encontrei o elemento 'botão Entrar' no passo 'Clicar em Entrar'.",
            page_url="http://portal-v2.rapa.test:8080/login",
            page_title="Entrar · Portal de Teste",
            step=step_info(step),
            candidates=enumerate_candidates(login_page(), step),
            suggestion=RepairSuggestion(
                status=RepairSuggestionStatus.CHOSEN,
                candidate_id="c1",
                reason="Acessar é o novo nome do botão de login.",
                message="O modelo sugere o candidato c1.",
            ),
        )

    async def get_repair(
        self, principal: Principal, run_id: UUID, *, suggest: bool = True
    ) -> RepairView:
        if self.forbidden:
            raise Forbidden(self.forbidden)
        return self._view(run_id)

    async def create_repair(
        self, principal: Principal, run_id: UUID, candidate_id: str
    ) -> RepairProposalInfo:
        self.created.append((principal.subject, run_id, candidate_id))
        return RepairProposalInfo(
            proposal_id=new_id(),
            run_id=run_id,
            robot_id=self.world.robot_ids["emitir_protocolo"],
            step_id="entrar",
            base_version_no=1,
            version_no=2,
            candidate_id=candidate_id,
            model_candidate_id="c1",
            requested_by=principal.subject,
            created_at=datetime.now(UTC),
        )


async def _failed_run(world: World, code: str = "elemento_nao_encontrado") -> UUID:
    status = await world.platform.start(
        world.principals["operador"], "emitir_protocolo", INPUTS, RunTrigger.MANUAL
    )
    world.platform.set_run_state(
        status.run_id, RunState.FAILED, error_code=code, error_message="O passo falhou."
    )
    return status.run_id


def _set(user: User, marker: str, value: Any) -> None:
    for element in user.find(marker=marker).elements:
        element.value = value


async def test_repair_page_from_the_failed_run(world: World) -> None:
    repairs = MemoryRepairs(world)
    world.repairs = repairs
    run_id = await _failed_run(world)
    other = await _failed_run(world, "cpf_recusado")
    async with world.user() as user:
        await login(user, "criadora", f"/execucoes/{run_id}")
        await user.should_see(marker="consertar")
        user.find(marker="consertar").click()
        await user.should_see(marker="titulo-conserto", retries=50)
        await user.should_see(marker="passo-quebrado")
        await user.should_see("Passo que quebrou: Clicar em Entrar")
        await user.should_see(marker="sugestao-escolhido")
        await user.should_see("Motivo dado pelo modelo: Acessar é o novo nome do botão de login.")
        for candidate_id in ("c1", "c2", "c3"):
            await user.should_see(marker=f"candidato-{candidate_id}")
        await user.should_see("c1: botão “Acessar” (sugerido pelo modelo) nota", retries=5)
        # Nada vem escolhido: sem escolha, nada é criado.
        user.find(marker="criar-conserto").click()
        await user.should_see("Escolha um dos candidatos.")
        assert repairs.created == []
        _set(user, "escolha-candidato", "c2")
        user.find(marker="criar-conserto").click()
        await user.should_see(marker="conserto-criado", retries=50)
        await user.should_see("Versão 2 criada como rascunho")
        assert repairs.created == [("criadora@a", run_id, "c2")]

        # Uma falha que não é do alvo não oferece o conserto.
        await user.open(f"/execucoes/{other}")
        await user.should_see(marker="erro-execucao")
        await user.should_not_see(marker="consertar")


async def test_without_creator_role_the_page_only_shows(world: World) -> None:
    world.repairs = MemoryRepairs(world)
    run_id = await _failed_run(world)
    async with world.user() as user:
        await login(user, "aprovador", f"/execucoes/{run_id}/conserto")
        await user.should_see(marker="candidatos", retries=50)
        await user.should_see(marker="sem-permissao-conserto")
        await user.should_not_see(marker="criar-conserto")


async def test_refusals_from_the_service_are_shown(world: World) -> None:
    world.repairs = MemoryRepairs(world, forbidden="o conserto assistido fica desligado para A1")
    run_id = await _failed_run(world)
    async with world.user() as user:
        await login(user, "criadora", f"/execucoes/{run_id}/conserto")
        await user.should_see(marker="erro", retries=50)
        await user.should_see("O conserto assistido fica desligado para A1")
        await user.open(f"/execucoes/{UUID(int=3)}/conserto")
        await user.should_see("Execução não encontrada.")


async def test_without_the_service_the_page_says_so(world: World) -> None:
    run_id = await _failed_run(world)
    async with world.user() as user:
        await login(user, "criadora", f"/execucoes/{run_id}/conserto")
        await user.should_see("O conserto assistido não está disponível neste servidor", retries=50)
