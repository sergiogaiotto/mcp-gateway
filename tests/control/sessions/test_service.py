"""Regras das sessões de gravação: quem pode, limites, bilhetes, parar, revisar e o rascunho.

Com o armazenamento em memória e um hospedeiro falso que fala o mesmo canal (sem
navegador); o PostgreSQL e o Chromium de verdade ficam nos outros testes.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rapa.control.sessions import (
    AssistantUnavailable,
    HubAssistant,
    SessionLimits,
    TicketBook,
)
from rapa.control.sessions.service import company_cap
from rapa.core.contracts import (
    Conflict,
    DraftChoices,
    Forbidden,
    InvalidInput,
    NotFound,
    ParameterChoice,
    QuotaExceeded,
    RecordingSessionStatus,
    RepetitionChoice,
    RepetitionFieldChoice,
    ServiceUnavailable,
    StepNameChoice,
    VersionStatus,
)
from rapa.core.edits import EditOp
from rapa.core.robot import FieldType, ForEachStep, Manifest
from tests.control.sessions.support import (
    FRAME,
    QueueViewer,
    SessionsWorld,
    build_world,
    demo_recording,
)
from tests.llm.fakehub import FakeHub, completion_message, make_matrix

URL = "https://portal.exemplo.gov.br/login"
TWO_PEOPLE = (("Maria", "529.982.247-25"), ("João", "111.444.777-35"))


@pytest.fixture
async def world() -> AsyncIterator[SessionsWorld]:
    w = build_world()
    yield w
    await w.service.aclose()
    await w.launcher.aclose()


async def test_only_creators_of_the_area_open_sessions(world: SessionsWorld) -> None:
    service, p = world.service, world.people
    for who in ("aprovador", "leitora", "vizinha"):
        with pytest.raises(Forbidden):
            await service.create_session(p[who], world.area_a, URL)
    # Outra empresa: a área não é dela (o papel nem vale ali).
    with pytest.raises(Forbidden):
        await service.create_session(p["outra"], world.area_a, URL)
    with pytest.raises(InvalidInput):
        await service.create_session(p["criadora"], world.area_a, "javascript:alert(1)")
    with pytest.raises(InvalidInput):
        await service.create_session(p["criadora"], world.area_a, "file:///etc/passwd")
    # O administrador da empresa cria em qualquer área dela, mas uma área inventada não existe.
    with pytest.raises(NotFound):
        await service.create_session(p["admin"], world.area_b, URL)
    info = await service.create_session(
        p["criadora"], world.area_a, URL, "  Cadastro   de pessoas "
    )
    assert info.status is RecordingSessionStatus.ACTIVE
    assert info.title == "Cadastro de pessoas"
    assert info.owner == "criadora@a" and info.company_id == world.company_a
    [job] = world.launcher.jobs
    assert job.start_url == URL and job.idle_timeout_s == 600 and job.max_duration_s == 3600
    assert [a.action for a in world.store.audit] == ["gravacao.sessao_criada"]


async def test_only_the_owner_sees_and_controls_the_session(world: SessionsWorld) -> None:
    service, p = world.service, world.people
    info = await service.create_session(p["criadora"], world.area_a, URL)
    sid = info.session_id
    assert await service.get_session(p["criadora"], sid) == info
    assert [s.session_id for s in await service.list_sessions(p["criadora"])] == [sid]
    for who in ("colega", "aprovador", "admin", "vizinha", "outra"):
        assert await service.get_session(p[who], sid) is None, who
        assert await service.list_sessions(p[who]) == [], who
        for action in (service.issue_ticket, service.stop_session, service.get_proposals):
            with pytest.raises(NotFound):
                await action(p[who], sid)
    # Quem perde o papel de criação na área perde a sessão também.
    demoted = p["criadora"].model_copy(update={"area_roles": {}})
    assert await service.get_session(demoted, sid) is None
    with pytest.raises(NotFound):
        await service.stop_session(demoted, sid)


async def test_tickets_are_single_use_short_lived_and_per_session(world: SessionsWorld) -> None:
    now = [0.0]
    world.service.tickets = TicketBook(clock=lambda: now[0])
    service, owner = world.service, world.people["criadora"]
    first = await service.create_session(owner, world.area_a, URL)
    second = await service.create_session(owner, world.area_a, URL)
    ticket = await service.issue_ticket(owner, first.session_id)
    assert ticket.path == f"/v1/sessoes/{first.session_id}/ao-vivo"
    assert "ticket" not in repr(ticket) and ticket.ticket not in repr(ticket)
    greeting = json.loads(await service.admit_viewer(first.session_id, ticket.ticket))
    assert greeting["tipo"] == "sessao" and 0 < greeting["restante_s"] <= 3600
    with pytest.raises(NotFound):  # uma vez só
        await service.admit_viewer(first.session_id, ticket.ticket)
    other = await service.issue_ticket(owner, second.session_id)
    with pytest.raises(NotFound):  # de outra sessão (e gasto na tentativa)
        await service.admit_viewer(first.session_id, other.ticket)
    with pytest.raises(NotFound):
        await service.admit_viewer(second.session_id, other.ticket)
    late = await service.issue_ticket(owner, first.session_id)
    now[0] += 61
    with pytest.raises(NotFound):
        await service.admit_viewer(first.session_id, late.ticket)
    with pytest.raises(NotFound):
        await service.admit_viewer(first.session_id, "inventado" * 5)


async def test_company_and_global_caps() -> None:
    world = build_world(limits=SessionLimits(per_company=3, global_max=4))
    service, p = world.service, world.people
    try:
        # Uma empresa nunca passa da metade das vagas da plataforma (4 // 2 = 2 < 3).
        await service.create_session(p["criadora"], world.area_a, URL)
        await service.create_session(p["colega"], world.area_a, URL)
        with pytest.raises(QuotaExceeded, match="sua empresa já tem 2"):
            await service.create_session(p["vizinha"], world.other_area_a, URL)
        await service.create_session(p["outra"], world.area_b, URL)
        # Uma validação rodando ocupa a mesma reserva: a empresa B ainda tem vaga, a plataforma não.
        world.store.interactive_runs = 1
        world.people["outra2"] = p["outra"].model_copy(update={"subject": "outro@b"})
        with pytest.raises(QuotaExceeded, match="vagas de gravação e de teste da plataforma"):
            await service.create_session(world.people["outra2"], world.area_b, URL)
        # A validação acabou: a vaga volta.
        world.store.interactive_runs = 0
        second_b = await service.create_session(world.people["outra2"], world.area_b, URL)
        # Uma sessão que termina sozinha libera a vaga.
        world.store.interactive_runs = 1
        [first, *_] = await service.list_sessions(p["criadora"])
        await world.launcher.hosts[first.session_id].end("ociosa")
        await service.stop_session(world.people["outra2"], second_b.session_id)
        await service.create_session(p["vizinha"], world.other_area_a, URL)
    finally:
        await service.aclose()
        await world.launcher.aclose()


@pytest.mark.parametrize(
    ("per_company", "platform", "expected"),
    [(2, 3, 1), (2, 4, 2), (2, 30, 2), (5, 1, 1), (5, 2, 1), (5, 10, 5)],
)
def test_one_company_never_takes_more_than_half_of_the_platform(
    per_company: int, platform: int, expected: int
) -> None:
    assert company_cap(per_company, platform) == expected


async def test_default_caps_leave_room_for_other_companies() -> None:
    # A reserva padrão (3 vagas com 30 navegadores): uma empresa fica com uma, as outras
    # ainda gravam.
    world = build_world(reserve=3)
    service, p = world.service, world.people
    try:
        await service.create_session(p["criadora"], world.area_a, URL)
        with pytest.raises(QuotaExceeded, match="sua empresa já tem 1 gravação aberta"):
            await service.create_session(p["colega"], world.area_a, URL)
        await service.create_session(p["outra"], world.area_b, URL)
    finally:
        await service.aclose()
        await world.launcher.aclose()


async def test_global_cap_defaults_to_the_interactive_reserve() -> None:
    world = build_world(reserve=1)
    try:
        await world.service.create_session(world.people["criadora"], world.area_a, URL)
        with pytest.raises(QuotaExceeded):
            await world.service.create_session(world.people["outra"], world.area_b, URL)
    finally:
        await world.service.aclose()
        await world.launcher.aclose()


async def test_launch_failure_marks_the_session_failed(world: SessionsWorld) -> None:
    world.launcher.fail = True
    with pytest.raises(ServiceUnavailable):
        await world.service.create_session(world.people["criadora"], world.area_a, URL)
    [record] = world.store.sessions.values()
    assert record.status is RecordingSessionStatus.FAILED and record.end_reason == "nao_iniciou"
    world.launcher.fail = False
    await world.service.create_session(world.people["criadora"], world.area_a, URL)


async def test_viewer_relay_frames_inputs_and_replacement(world: SessionsWorld) -> None:
    service, owner = world.service, world.people["criadora"]
    info = await service.create_session(owner, world.area_a, URL)
    host_received = None

    async def watch(viewer: QueueViewer) -> asyncio.Task[None]:
        ticket = await service.issue_ticket(owner, info.session_id)
        greeting = await service.admit_viewer(info.session_id, ticket.ticket)
        return asyncio.create_task(service.relay_viewer(info.session_id, viewer, greeting))

    first = QueueViewer()
    task = await watch(first)
    await first.wait(lambda: first.frames and "pagina" in first.kinds())
    assert first.kinds()[0] == "sessao" and first.frames[0] == FRAME
    first.inbox.put_nowait(json.dumps({"tipo": "mouse", "acao": "mover", "x": 1, "y": 2}))
    # A janela não para a sessão nem manda o que não é entrada.
    first.inbox.put_nowait(json.dumps({"tipo": "parar"}))
    first.inbox.put_nowait("lixo")
    first.inbox.put_nowait(json.dumps({"tipo": "texto", "texto": "ção"}))
    host = world.launcher.hosts[info.session_id]
    await first.wait(lambda: len(host.received) >= 2)
    host_received = [m["tipo"] for m in host.received]
    assert host_received == ["mouse", "texto"]
    await first.wait(lambda: len(first.frames) >= 2)
    # Uma segunda janela substitui a primeira (a mesma ligação com o hospedeiro).
    second = QueueViewer()
    task2 = await watch(second)
    await first.wait(lambda: first.closed is not None)
    assert first.closed == (4001, "substituida")
    assert "substituida" in [t.get("motivo") for t in first.texts]
    await second.wait(lambda: second.frames and "pronta" in second.kinds())
    assert host.connections == 1
    await asyncio.wait_for(task, timeout=5)
    stopped = await service.stop_session(owner, info.session_id)
    assert stopped.status is RecordingSessionStatus.STOPPED
    await asyncio.wait_for(task2, timeout=5)
    assert second.closed is not None
    assert "fim" in second.kinds()


async def test_stop_saves_the_recording_and_audits(world: SessionsWorld) -> None:
    service, owner = world.service, world.people["criadora"]
    info = await service.create_session(owner, world.area_a, URL)
    stopped = await service.stop_session(owner, info.session_id)
    assert stopped.status is RecordingSessionStatus.STOPPED
    assert stopped.recording_id is not None and stopped.stopped_at is not None
    saved = await world.store.get_recording(world.company_a, info.session_id)
    assert saved is not None and saved[0] == stopped.recording_id
    assert [a.action for a in world.store.audit] == [
        "gravacao.sessao_criada",
        "gravacao.sessao_parada",
    ]
    assert not await world.launcher.is_alive(info.session_id)
    with pytest.raises(Conflict):
        await service.stop_session(owner, info.session_id)
    with pytest.raises(Conflict):
        await service.issue_ticket(owner, info.session_id)
    # Sem gravação, não há revisão.
    other = await service.create_session(owner, world.area_a, URL)
    with pytest.raises(Conflict):
        await service.get_proposals(owner, other.session_id)


async def test_a_host_that_ends_alone_expires_the_session(world: SessionsWorld) -> None:
    service, owner = world.service, world.people["criadora"]
    info = await service.create_session(owner, world.area_a, URL)
    viewer = QueueViewer()
    ticket = await service.issue_ticket(owner, info.session_id)
    greeting = await service.admit_viewer(info.session_id, ticket.ticket)
    task = asyncio.create_task(service.relay_viewer(info.session_id, viewer, greeting))
    await viewer.wait(lambda: "pronta" in viewer.kinds())
    await world.launcher.hosts[info.session_id].end("ociosa")
    await asyncio.wait_for(task, timeout=5)
    assert {"tipo": "fim", "motivo": "ociosa"} in viewer.texts
    record = await service.get_session(owner, info.session_id)
    assert record is not None and record.status is RecordingSessionStatus.EXPIRED
    assert record.end_reason == "ociosa"
    with pytest.raises(Conflict):
        await service.stop_session(owner, info.session_id)


async def test_lost_browser_is_noticed_on_the_next_read_or_sweep(world: SessionsWorld) -> None:
    service, owner = world.service, world.people["criadora"]
    info = await service.create_session(owner, world.area_a, URL)
    world.launcher.hosts[info.session_id].alive = False
    assert await service.sweep() == 1
    record = await service.get_session(owner, info.session_id)
    assert record is not None and record.status is RecordingSessionStatus.EXPIRED


async def test_failed_stop_is_reported_and_the_session_fails(world: SessionsWorld) -> None:
    service, owner = world.service, world.people["criadora"]
    info = await service.create_session(owner, world.area_a, URL)
    world.launcher.hosts[info.session_id].stop_error = "Não consegui apagar um valor sigiloso."
    with pytest.raises(Conflict, match="apagar um valor sigiloso"):
        await service.stop_session(owner, info.session_id)
    record = await service.get_session(owner, info.session_id)
    assert record is not None and record.status is RecordingSessionStatus.FAILED


class _DownAssistant:
    async def propose(self, manifest: Manifest, observed_texts: set[str]) -> list[EditOp]:
        raise AssistantUnavailable("o hub do modelo de linguagem não respondeu")


async def test_hub_down_still_offers_the_rule_suggestions() -> None:
    world = build_world(assistant=_DownAssistant(), recording=demo_recording(TWO_PEOPLE))
    try:
        owner = world.people["criadora"]
        info = await world.service.create_session(owner, world.area_a, URL, "Cadastrar pessoas")
        await world.service.stop_session(owner, info.session_id)
        proposals = await world.service.get_proposals(owner, info.session_id)
        assert not proposals.model_available
        assert proposals.model_message is not None
        assert "indisponíveis" in proposals.model_message
        assert {p.name for p in proposals.parameters} >= {"nome", "cpf"}
        assert [r.proposal_id for r in proposals.repetitions] == ["repeticao_digitar_nome"]
        assert proposals.name == "cadastrar_pessoas" and proposals.title == "Cadastrar pessoas"
        # A pessoa confirma as sugestões das regras e cria o rascunho sem o modelo.
        version = await world.service.create_draft(
            owner,
            info.session_id,
            DraftChoices(
                name=proposals.name,
                title=proposals.title,
                description=proposals.description,
                repetitions=[
                    RepetitionChoice(
                        proposal_id="repeticao_digitar_nome",
                        list_name="pessoas",
                        list_title="Pessoas",
                        fields=[
                            RepetitionFieldChoice(
                                step_id="digitar_nome", name="nome", title="Nome"
                            ),
                            RepetitionFieldChoice(
                                step_id="digitar_cpf", name="cpf", title="CPF", type="cpf"
                            ),
                        ],
                    )
                ],
                step_names=[StepNameChoice(step_id="clicar_salvar", title="Salvar a pessoa")],
            ),
        )
        assert version.status is VersionStatus.DRAFT and version.created_by == owner.subject
        assert version.violations == []
        loop = version.manifest.step_by_id("repetir_digitar_nome")
        assert isinstance(loop, ForEachStep)
        assert [f.type for f in version.manifest.inputs] == [FieldType.LIST]
        record = await world.service.get_session(owner, info.session_id)
        assert record is not None and record.status is RecordingSessionStatus.DRAFTED
        assert (record.robot_id, record.version_no) == (version.robot_id, 1)
        assert world.store.audit[-1].action == "gravacao.rascunho_criado"
        # Quem criou o rascunho não o publica (a regra de sempre do serviço de robôs).
        await world.platform.mark_validated(owner, version.robot_id, 1)
        with pytest.raises(Forbidden):
            await world.platform.publish(owner, version.robot_id, 1)
    finally:
        await world.service.aclose()
        await world.launcher.aclose()


async def test_model_proposals_are_validated_and_cached(tmp_path: Path) -> None:
    answer = {
        "edits": [
            {
                "op": "parametrize",
                "step_id": "digitar_nome",
                "input_name": "nome_completo",
                "title": "Nome completo",
                "type": "string",
            },
            {"op": "rename_step", "step_id": "clicar_salvar", "title": "Salvar o cadastro"},
            {"op": "rename_step", "step_id": "passo_inventado", "title": "Nada"},
            {"op": "add_text_postcondition", "text": "Cadastro salvo com sucesso"},
            {"op": "raise_effect", "step_id": "clicar_salvar", "effect": "irreversible"},
        ]
    }
    hub = FakeHub(script=[completion_message(json.dumps(answer, ensure_ascii=False))])
    matrix = tmp_path / "matriz.json"
    make_matrix().save(matrix)
    world = build_world(assistant=HubAssistant(hub.client, str(matrix)))
    try:
        owner = world.people["criadora"]
        info = await world.service.create_session(owner, world.area_a, URL)
        await world.service.stop_session(owner, info.session_id)
        proposals = await world.service.get_proposals(owner, info.session_id)
        assert proposals.model_available and proposals.model_message is None
        nome = next(p for p in proposals.parameters if p.step_id == "digitar_nome")
        assert nome.name == "nome_completo" and nome.source.value == "modelo"
        assert [(s.step_id, s.title) for s in proposals.step_names] == [
            ("clicar_salvar", "Salvar o cadastro")
        ]
        assert [c.text for c in proposals.checks] == ["Cadastro salvo com sucesso"]
        assert [(e.step_id, e.proposed) for e in proposals.effects] == [
            ("clicar_salvar", "irreversible")
        ]
        assert any("passo_inventado" in r for r in proposals.model_rejected)
        # A senha digitada na demonstração nunca chegou ao modelo (e o pedido é um só).
        assert await world.service.get_proposals(owner, info.session_id) == proposals
        sent = json.dumps(hub.chat_requests, ensure_ascii=False)
        assert len(hub.chat_requests) == 1
        assert "Senha-Da-Demo" not in sent
        # Nada entra sem confirmação: um rascunho sem escolhas fica como o compilador fez.
        version = await world.service.create_draft(
            owner,
            info.session_id,
            DraftChoices(name="robo_gravado", title="Robô gravado", description="Gravado."),
        )
        salvar = version.manifest.step_by_id("clicar_salvar")
        assert salvar is not None and salvar.title != "Salvar o cadastro"
        assert salvar.effect.value == "write"
        assert version.manifest.postconditions == []
    finally:
        await world.service.aclose()
        await world.launcher.aclose()


async def test_draft_choices_the_code_refuses_are_errors(world: SessionsWorld) -> None:
    owner = world.people["criadora"]
    info = await world.service.create_session(owner, world.area_a, URL)
    await world.service.stop_session(owner, info.session_id)
    with pytest.raises(InvalidInput) as caught:
        await world.service.create_draft(
            owner,
            info.session_id,
            DraftChoices(
                name="robo_gravado",
                title="Robô",
                description="Gravado.",
                parameters=[ParameterChoice(step_id="clicar_salvar", name="x", title="X")],
                step_names=[StepNameChoice(step_id="fantasma", title="X")],
            ),
        )
    assert len(caught.value.errors) == 2
    record = await world.service.get_session(owner, info.session_id)
    assert record is not None and record.status is RecordingSessionStatus.STOPPED
    # A mesma gravação pode virar um rascunho depois de corrigir as escolhas.
    version = await world.service.create_draft(
        owner,
        info.session_id,
        DraftChoices(
            name="robo_gravado",
            title="Robô",
            description="Gravado.",
            step_names=[StepNameChoice(step_id="clicar_salvar", title="Salvar")],
        ),
    )
    assert version.version_no == 1
