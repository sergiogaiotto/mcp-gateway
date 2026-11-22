"""Conserto assistido no PostgreSQL: permissões, modelo, rascunho novo e aprovação por outra pessoa."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import HubRepairAssistant, PgPlatform, PgRepairs, verify_chain
from rapa.control.services.repairs import (
    A1_DISABLED,
    RepairAssistantUnavailable,
    RepairChoiceRefused,
)
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    RepairCandidate,
    RepairStepInfo,
    RepairSuggestionStatus,
    VersionStatus,
)
from rapa.core.robot import ClickStep, LocatorBy, Manifest
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_USER
from rapa.llm.repair import RepairAnswer
from rapa.repair import enumerate_candidates, step_info
from tests.control.db.support import (
    Tenant,
    failed_run,
    make_tenant,
    protocolo,
    publish,
    reset_queue,
)
from tests.llm.fakehub import FakeHub, completion_message, error, make_matrix
from tests.repair.support import login_page

pytestmark = pytest.mark.database


class FakeAssistant:
    """O modelo, sem hub: devolve as respostas na ordem (ou levanta)."""

    def __init__(self, *answers: RepairAnswer | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    async def choose(
        self,
        step: RepairStepInfo,
        candidates: Sequence[RepairCandidate],
        *,
        page_url: str,
        page_title: str,
        error_code: str,
    ) -> RepairAnswer:
        self.calls.append([c.candidate_id for c in candidates])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _world(app_db: Database, superuser: AsyncEngine) -> tuple[Tenant, PgPlatform]:
    await reset_queue(superuser, total_slots=10)
    tenant = await make_tenant(app_db, "Conserto")
    platform = PgPlatform(app_db)
    await publish(platform, tenant, protocolo())
    return tenant, platform


async def _failed(app_db: Database, dispatcher_db: Database, tenant: Tenant, **extra: Any) -> UUID:
    extra.setdefault("snapshot", login_page().model_dump(mode="json"))
    return await failed_run(app_db, dispatcher_db, tenant, **extra)


async def test_candidates_suggestion_draft_and_approval_by_someone_else(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, platform = await _world(app_db, superuser_engine)
    run_id = await _failed(app_db, dispatcher_db, tenant)
    assistant = FakeAssistant(RepairAnswer(candidate_id="c1", reason="Acessar é o login."))
    repairs = PgRepairs(app_db, assistant=assistant)

    view = await repairs.get_repair(tenant.creator, run_id)
    assert view.step.step_id == "entrar" and view.step.kind == "click"
    assert view.version_no == 1 and view.robot_slug == "emitir_protocolo"
    assert [(c.candidate_id, c.name) for c in view.candidates] == [
        ("c1", "Acessar"),
        ("c2", "Início"),
        ("c3", "Sair"),
    ]
    assert view.suggestion.status is RepairSuggestionStatus.CHOSEN
    assert view.suggestion.candidate_id == "c1"
    assert view.blocked_reason is None and view.proposal is None
    # A sugestão fica guardada: abrir de novo não pergunta outra vez ao modelo.
    again = await repairs.get_repair(tenant.approver, run_id)
    assert again.suggestion == view.suggestion
    assert len(assistant.calls) == 1
    # Sem pedir a sugestão, o modelo nem é consultado.
    quiet = await repairs.get_repair(tenant.creator, run_id, suggest=False)
    assert quiet.suggestion.status is RepairSuggestionStatus.OFF

    # Quem aprova não pede o conserto; quem cria, sim (a pessoa escolhe, não o modelo).
    with pytest.raises(Forbidden):
        await repairs.create_repair(tenant.approver, run_id, "c1")
    with pytest.raises(InvalidInput):
        await repairs.create_repair(tenant.creator, run_id, "c9")
    proposal = await repairs.create_repair(tenant.creator, run_id, "c1")
    assert (proposal.base_version_no, proposal.version_no) == (1, 2)
    assert proposal.model_candidate_id == "c1"
    assert proposal.requested_by == tenant.creator.subject

    draft = await platform.get_version(tenant.creator, proposal.robot_id, 2)
    assert draft is not None and draft.status is VersionStatus.DRAFT
    assert draft.created_by == tenant.creator.subject
    assert draft.violations == []
    entrar = draft.manifest.step_by_id("entrar")
    assert isinstance(entrar, ClickStep)
    assert (entrar.target.locators[0].by, entrar.target.locators[0].name) == (
        LocatorBy.ROLE,
        "Acessar",
    )
    # A versão publicada não muda: o conserto sempre é um rascunho novo.
    published = await platform.get_published(tenant.operator, "emitir_protocolo")
    assert published is not None and published.version_no == 1

    # Um conserto por execução.
    with pytest.raises(Conflict, match="já tem um conserto"):
        await repairs.create_repair(tenant.creator, run_id, "c2")
    after = await repairs.get_repair(tenant.creator, run_id)
    assert after.proposal == proposal
    assert after.blocked_reason is not None and "versão 2" in after.blocked_reason
    assert after.suggestion.status is RepairSuggestionStatus.OFF

    # Outra falha da versão 1 já não pode virar conserto: o robô já tem a versão 2.
    old = await _failed(app_db, dispatcher_db, tenant)
    stale = await repairs.get_repair(tenant.creator, old)
    assert stale.version_no == 1
    assert stale.blocked_reason is not None and "versão 2" in stale.blocked_reason
    assert stale.suggestion.status is RepairSuggestionStatus.OFF
    with pytest.raises(Conflict):
        await repairs.create_repair(tenant.creator, old, "c1")

    # Ninguém aprova a própria versão: quem pediu o conserto não publica.
    await platform.mark_validated(tenant.creator, proposal.robot_id, 2)
    with pytest.raises(Forbidden):
        await platform.publish(tenant.creator, proposal.robot_id, 2)
    released = await platform.publish(tenant.approver, proposal.robot_id, 2)
    assert released.approved_by == tenant.approver.subject != released.created_by

    # Uma falha da versão 2, publicada, pode virar o conserto seguinte.
    newer = await _failed(app_db, dispatcher_db, tenant)
    current = await repairs.get_repair(tenant.creator, newer, suggest=False)
    assert current.version_no == 2 and current.blocked_reason is None

    async with app_db.tenant_session(tenant.company_id) as session:
        chain = await verify_chain(session, tenant.company_id)
        actions = (await session.scalars(text("SELECT action FROM audit_log ORDER BY seq"))).all()
        stored = (
            await session.execute(
                text("SELECT candidate, model_candidate_id FROM repair_proposals")
            )
        ).one()
    assert chain.ok, chain.problems
    assert actions.count("conserto.sugestao_pedida") == 1
    assert actions.count("conserto.pedido") == 1
    assert actions[-4:].count("robo.versao_publicada") == 1
    assert stored.model_candidate_id == "c1"
    assert stored.candidate["candidate_id"] == "c1"


async def test_who_sees_the_repair(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, _ = await _world(app_db, superuser_engine)
    run_id = await _failed(app_db, dispatcher_db, tenant)
    repairs = PgRepairs(app_db)
    for principal in (tenant.operator, tenant.viewer):
        with pytest.raises(Forbidden):
            await repairs.get_repair(principal, run_id)
        with pytest.raises(Forbidden):
            await repairs.create_repair(principal, run_id, "c1")
    # Para outra área, a execução não existe.
    with pytest.raises(NotFound):
        await repairs.get_repair(tenant.outsider, run_id)
    with pytest.raises(NotFound):
        await repairs.create_repair(tenant.outsider, run_id, "c1")
    with pytest.raises(NotFound):
        await repairs.get_repair(tenant.creator, UUID(int=1))
    # O administrador da empresa vê; sem hub, a pessoa escolhe sozinha.
    view = await repairs.get_repair(tenant.admin, run_id)
    assert view.suggestion.status is RepairSuggestionStatus.OFF
    assert "escolha o candidato você mesmo" in view.suggestion.message


async def test_model_down_or_refused_never_blocks_the_person(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, _ = await _world(app_db, superuser_engine)
    run_id = await _failed(app_db, dispatcher_db, tenant)
    assistant = FakeAssistant(
        RepairAssistantUnavailable("fora do ar"),
        RepairChoiceRefused("inválida"),
    )
    repairs = PgRepairs(app_db, assistant=assistant)
    down = await repairs.get_repair(tenant.creator, run_id)
    assert down.suggestion.status is RepairSuggestionStatus.UNAVAILABLE
    assert len(down.candidates) == 3
    # Fora do ar não fica guardado: a próxima abertura pergunta de novo.
    refused = await repairs.get_repair(tenant.creator, run_id)
    assert refused.suggestion.status is RepairSuggestionStatus.REFUSED
    assert refused.suggestion.candidate_id is None
    assert len(assistant.calls) == 2
    # Mesmo assim, a pessoa escolhe e o rascunho sai, sem a sugestão do modelo.
    proposal = await repairs.create_repair(tenant.creator, run_id, "c1")
    assert proposal.model_candidate_id is None


async def test_hub_assistant_maps_hub_down_and_invalid_answers(tmp_path: Path) -> None:
    """Pelo hub (falso): fora do ar, fora da lista e uma escolha válida, sem segredos."""
    matrix = tmp_path / "capacidades.json"
    make_matrix().save(matrix)
    step = protocolo().step_by_id("entrar")
    assert step is not None
    info = step_info(step)
    candidates = enumerate_candidates(login_page(), step)
    scripts: list[list[Any]] = [
        [error(503, "fora")] * 3,
        [completion_message(json.dumps({"candidate_id": "c7", "reason": "x"}))] * 3,
        [completion_message(json.dumps({"candidate_id": "c2", "reason": "Início."}))],
    ]
    seen: list[str | None] = []
    for script in scripts:
        hub = FakeHub(json_schema="ignored", script=list(script))
        assistant = HubRepairAssistant(lambda h=hub: h.client(max_retries=0), str(matrix))
        try:
            answer = await assistant.choose(
                info, candidates, page_url="", page_title="", error_code="elemento_nao_encontrado"
            )
            seen.append(answer.candidate_id)
        except RepairAssistantUnavailable:
            seen.append("indisponivel")
        except RepairChoiceRefused:
            seen.append("recusada")
        sent = json.dumps(hub.chat_requests, ensure_ascii=False)
        assert DEFAULT_PASSWORD not in sent and DEFAULT_USER not in sent
    assert seen == ["indisponivel", "recusada", "c2"]
    missing = HubRepairAssistant(lambda: FakeHub().client(), None)
    with pytest.raises(RepairAssistantUnavailable):
        await missing.choose(info, candidates, page_url="", page_title="", error_code="x")


async def test_a1_robots_have_no_assisted_repair(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    await reset_queue(superuser_engine, total_slots=10)
    tenant = await make_tenant(app_db, "Conserto A1")
    platform = PgPlatform(app_db)
    data = protocolo().model_dump(mode="json")
    data["credentials"].append(
        {"name": "certificado", "kind": "pfx_a1", "origin": data["credentials"][0]["origin"]}
    )
    await publish(platform, tenant, Manifest.model_validate(data))
    run_id = await _failed(app_db, dispatcher_db, tenant)
    assistant = FakeAssistant()
    repairs = PgRepairs(app_db, assistant=assistant)
    for call in (
        repairs.get_repair(tenant.creator, run_id),
        repairs.create_repair(tenant.creator, run_id, "c1"),
        repairs.get_repair(tenant.admin, run_id),
    ):
        with pytest.raises(Forbidden) as caught:
            await call
        assert caught.value.message == A1_DISABLED
    # Nem o modelo foi perguntado, nem rascunho foi criado.
    assert assistant.calls == []
    versions = await platform.list_versions(tenant.creator, (await _robot_id(platform, tenant)))
    assert [v.version_no for v in versions] == [1]


async def _robot_id(platform: PgPlatform, tenant: Tenant) -> UUID:
    robots = await platform.list_robots(tenant.creator)
    return robots[0].robot_id


async def test_only_failures_of_the_target_with_a_snapshot_are_repairable(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, _ = await _world(app_db, superuser_engine)
    repairs = PgRepairs(app_db)
    other_code = await _failed(app_db, dispatcher_db, tenant, error_code="tempo_esgotado")
    with pytest.raises(Conflict, match="não é de um elemento"):
        await repairs.get_repair(tenant.creator, other_code)
    without = await _failed(app_db, dispatcher_db, tenant, snapshot=None)
    with pytest.raises(Conflict, match="retrato"):
        await repairs.get_repair(tenant.creator, without)
    indeterminate = await _failed(app_db, dispatcher_db, tenant, state="indeterminate")
    with pytest.raises(Conflict, match="indeterminada"):
        await repairs.get_repair(tenant.creator, indeterminate)
    succeeded = await _failed(
        app_db, dispatcher_db, tenant, state="succeeded", error_code=None, step_id=None
    )
    with pytest.raises(Conflict, match="falhou"):
        await repairs.get_repair(tenant.creator, succeeded)
    # Um retrato fora do contrato fica de fora; o resultado é gravado do mesmo jeito.
    broken = login_page().model_dump(mode="json")
    broken["elements"] = [{"tag": "x" * 500}]
    invalid = await _failed(app_db, dispatcher_db, tenant, snapshot=broken)
    with pytest.raises(Conflict, match="retrato"):
        await repairs.get_repair(tenant.creator, invalid)
    async with app_db.tenant_session(tenant.company_id) as session:
        states = dict(
            (
                await session.execute(
                    text("SELECT id::text, state FROM runs WHERE id = ANY(:ids)"),
                    {"ids": [invalid, succeeded]},
                )
            ).all()
        )
        snapshots = await session.scalar(text("SELECT count(*) FROM run_snapshots"))
    assert states == {str(invalid): "failed", str(succeeded): "succeeded"}
    # Só a de código de outro tipo e a indeterminada guardaram retrato (sem uso aqui).
    assert snapshots == 2


async def test_snapshots_are_append_only_and_written_only_by_the_dispatcher(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine
) -> None:
    tenant, _ = await _world(app_db, superuser_engine)
    run_id = await _failed(app_db, dispatcher_db, tenant)
    async with app_db.tenant_session(tenant.company_id) as session:
        stored = (
            await session.execute(
                text("SELECT step_id, error_code, snapshot FROM run_snapshots WHERE run_id = :r"),
                {"r": run_id},
            )
        ).one()
    assert (stored.step_id, stored.error_code) == ("entrar", "elemento_nao_encontrado")
    assert stored.snapshot["elements"][-1]["name"] == "Acessar"
    for statement in (
        "UPDATE run_snapshots SET step_id = 'outro'",
        "DELETE FROM run_snapshots",
        "INSERT INTO run_snapshots (company_id, run_id, attempt, step_id, error_code, snapshot, "
        "snapshot_sha256) VALUES (:c, :r, 9, 'x', 'x', '{}', repeat('0', 64))",
    ):
        with pytest.raises(DBAPIError) as caught:
            async with app_db.tenant_session(tenant.company_id) as session:
                await session.execute(text(statement), {"c": tenant.company_id, "r": run_id})
        assert getattr(caught.value.orig, "sqlstate", None) == "42501", statement
    await PgRepairs(app_db).create_repair(tenant.creator, run_id, "c1")
    for statement in (
        "UPDATE repair_proposals SET candidate_id = 'c2'",
        "DELETE FROM repair_proposals",
    ):
        with pytest.raises(DBAPIError):
            async with app_db.tenant_session(tenant.company_id) as session:
                await session.execute(text(statement))
