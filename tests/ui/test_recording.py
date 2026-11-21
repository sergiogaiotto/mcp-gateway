"""Telas de gravação com o ``User`` simulado do NiceGUI e o backend em processo.

As sessões usam o armazenamento em memória e o hospedeiro falso (o mesmo canal,
sem navegador); o JavaScript da visão ao vivo é testado num Chromium em
``test_recording_browser.py``.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from nicegui.testing import User

from rapa.control.sessions import LiveRelay, MemorySessionStore, RecordingSessions
from rapa.core.contracts import RecordingSessionStatus, VersionStatus
from rapa.core.robot import FieldType, ForEachStep
from tests.control.sessions.support import FakeLauncher, demo_recording
from tests.ui.conftest import World, login, logout

pytestmark = pytest.mark.usefixtures("no_error_logs")

URL = "https://portal.exemplo.gov.br/login"
TWO_PEOPLE = (("Maria", "529.982.247-25"), ("João", "111.444.777-35"))


@pytest.fixture
async def recording_world(world: World) -> AsyncIterator[tuple[World, FakeLauncher]]:
    store = MemorySessionStore(reserve=3)
    store.add_area(world.company_a, world.area_a)
    store.add_area(world.company_b, world.area_b)
    launcher = FakeLauncher(recording=demo_recording(TWO_PEOPLE))
    service = RecordingSessions(store, launcher, world.platform, relay=LiveRelay(stop_timeout_s=10))
    world.sessions = service
    yield world, launcher
    await service.aclose()
    await launcher.aclose()


def _set(user: User, marker: str, value: Any) -> None:
    for element in user.find(marker=marker).elements:
        element.value = value


async def _start(user: User, world: World) -> str:
    await user.open("/gravar")
    await user.should_see(marker="nova-gravacao")
    _set(user, "endereco-inicial", URL)
    _set(user, "titulo-gravacao", "Cadastrar pessoas")
    user.find(marker="comecar-gravacao").click()
    await user.should_see(marker="tela-ao-vivo", retries=50)
    assert world.sessions is not None
    [info] = await world.sessions.list_sessions(world.principals["criadora"])
    return str(info.session_id)


async def test_only_creators_see_gravar(recording_world: tuple[World, FakeLauncher]) -> None:
    world, _ = recording_world
    async with world.user() as user:
        await login(user, "operador", "/ao-vivo")
        await user.should_see(marker="sem-permissao-gravar", retries=50)
        await user.should_not_see(marker="nav-gravar")
        await user.should_not_see(marker="nova-gravacao")
        await logout(user)
        await login(user, "criadora")
        await user.should_see(marker="nav-gravar")


async def test_start_watch_and_stop_a_recording(
    recording_world: tuple[World, FakeLauncher],
) -> None:
    world, launcher = recording_world
    async with world.user() as user:
        await login(user, "criadora", "/gravar")
        await user.should_see(marker="sem-gravacoes")
        sid = await _start(user, world)
        await user.should_see(marker="parar-e-revisar")
        await user.should_see(marker="tempo-restante")
        await user.should_see("Tempo restante: 59:")
        [job] = launcher.jobs
        assert job.start_url == URL
        # A lista mostra a gravação aberta, com o botão para voltar à tela.
        await user.open("/gravar")
        await user.should_see(marker=f"abrir-gravacao-{sid}")
        user.find(marker=f"abrir-gravacao-{sid}").click()
        await user.should_see(marker="parar-e-revisar", retries=50)
        user.find(marker="parar-e-revisar").click()
        await user.should_see(marker="titulo-revisao", retries=100)
        assert world.sessions is not None
        info = await world.sessions.get_session(world.principals["criadora"], UUID(sid))
        assert info is not None and info.status is RecordingSessionStatus.STOPPED
        # A tela ao vivo de uma gravação parada leva à revisão.
        await user.open(f"/gravar/{sid}")
        await user.should_see(marker="ir-revisar")
        # Outra empresa não vê a gravação.
        await logout(user)
        await login(user, "outra", f"/gravar/{sid}/revisar")
        await user.should_see("Gravação não encontrada.", retries=50)
        await user.open(f"/gravar/{sid}")
        await user.should_see("Gravação não encontrada.")


async def test_review_without_the_model_and_create_the_draft(
    recording_world: tuple[World, FakeLauncher],
) -> None:
    world, _ = recording_world
    async with world.user() as user:
        await login(user, "criadora", "/gravar")
        sid = await _start(user, world)
        user.find(marker="parar-e-revisar").click()
        await user.should_see(marker="titulo-revisao", retries=100)
        await user.should_see(marker="modelo-indisponivel")
        await user.should_see("O modelo de linguagem não está configurado")
        await user.should_see(marker="repeticao-repeticao_digitar_nome")
        await user.should_see(marker="parametro-digitar_cpf")
        await user.should_see("valores: Maria, João")
        # Nada entra sem aceitar: a pessoa aceita a repetição e edita a lista e um passo.
        _set(user, "decisao-repeticao-repeticao_digitar_nome", "aceitar")
        _set(user, "lista-nome-repeticao_digitar_nome", "pessoas")
        _set(user, "lista-titulo-repeticao_digitar_nome", "Pessoas")
        _set(user, "campo-tipo-digitar_cpf", "cpf")
        # Aceitar um parâmetro de um passo da repetição não vale: ele vira campo do item.
        _set(user, "decisao-parametro-digitar_nome", "aceitar")
        _set(user, "nome-passo-clicar_salvar", "Salvar a pessoa")
        _set(user, "nome-tecnico", "cadastrar_pessoas")
        user.find(marker="criar-rascunho").click()
        await user.should_see(marker="titulo-robo", retries=100)
        await user.should_see("Salvar a pessoa")
    [robot] = [r for r in world.platform.robots.values() if r.slug == "cadastrar_pessoas"]
    version = robot.versions[-1]
    assert version.status is VersionStatus.DRAFT
    assert version.created_by == world.principals["criadora"].subject
    manifest = version.manifest
    assert manifest.name == "cadastrar_pessoas"
    assert isinstance(manifest.step_by_id("repetir_digitar_nome"), ForEachStep)
    [lista] = manifest.inputs
    assert lista.name == "pessoas" and lista.type is FieldType.LIST
    assert lista.item_fields is not None
    assert [(f.name, f.type) for f in lista.item_fields] == [
        ("nome", FieldType.STRING),
        ("cpf", FieldType.CPF),
    ]
    assert world.sessions is not None
    info = await world.sessions.get_session(world.principals["criadora"], UUID(sid))
    assert info is not None and info.status is RecordingSessionStatus.DRAFTED


async def test_review_reports_invalid_names(recording_world: tuple[World, FakeLauncher]) -> None:
    world, _ = recording_world
    async with world.user() as user:
        await login(user, "criadora", "/gravar")
        await _start(user, world)
        user.find(marker="parar-e-revisar").click()
        await user.should_see(marker="titulo-revisao", retries=100)
        _set(user, "decisao-parametro-digitar_nome", "aceitar")
        _set(user, "parametro-nome-digitar_nome", "Nome com espaço")
        user.find(marker="criar-rascunho").click()
        await user.should_see("Confira os campos", retries=50)
    assert len(world.platform.robots) == 2  # só os robôs de exemplo


async def test_without_sessions_the_page_says_so(world: World) -> None:
    async with world.user() as user:
        await login(user, "criadora", "/gravar")
        await user.should_see(marker="erro", retries=50)
        await user.should_see("A gravação de demonstrações não está disponível neste servidor")
