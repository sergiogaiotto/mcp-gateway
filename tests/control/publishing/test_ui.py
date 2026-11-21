"""Telas de Agendas, Webhooks e Lotes com o ``User`` simulado do NiceGUI e os serviços no PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.testing import User
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.ui.app import create_ui
from rapa.ui.auth import DevLogin, DevProfile, MemorySessionStore, UiAuth
from rapa.ui.backend import ContractsBackendFactory
from rapa.ui.context import UiSettings
from tests.control.db.support import PROTOCOLO_INPUTS
from tests.ui.conftest import login, logout, no_error_logs, simulate  # noqa: F401

from .support import PROTOCOL_ROWS, World, build_world, csv_bytes, protocol_sheet, xlsx_bytes

pytestmark = [pytest.mark.database, pytest.mark.usefixtures("no_error_logs")]


def _user(world: World, **settings: Any) -> AbstractAsyncContextManager[User]:
    people = {
        "operador": world.a.operator,
        "leitora": world.a.viewer,
        "outra": world.b.admin,
    }
    auth = UiAuth(
        dev=DevLogin(
            DevProfile(key, f"Perfil {key}", p, description=p.subject) for key, p in people.items()
        ),
        store=MemorySessionStore(),
        cookie_secure=False,
        principal_ttl=timedelta(0),
    )

    def build(app: FastAPI) -> None:
        create_ui(
            app,
            backend_factory=ContractsBackendFactory.from_platform(
                world.platform,
                schedules=world.schedules,
                webhooks=world.webhooks,
                batches=world.batches,
            ),
            auth=auth,
            settings=UiSettings(**{"poll_interval_s": 0.05, **settings}),
        )

    return simulate(build)


@pytest.fixture
async def world(app_db: Database, superuser_engine: AsyncEngine) -> AsyncIterator[World]:
    yield await build_world(app_db, superuser_engine)


def _set(user: User, marker: str, value: Any) -> None:
    for element in user.find(marker=marker).elements:
        element.value = value


async def test_schedule_page_creates_toggles_and_hides_from_others(world: World) -> None:
    async with _user(world) as user:
        await login(user, "operador", "/agendas")
        await user.should_see(marker="sem-agendas", retries=50)
        await user.should_see(marker="nav-agendas")
        _set(user, "robo-agenda", str(world.robots_a["emitir_protocolo"]))
        await user.should_see(marker="campo-cpf", retries=50)
        _set(user, "titulo-agenda", "Protocolo das 9h")
        _set(user, "cron-agenda", "0 9 * * 1-5")
        for name, value in PROTOCOLO_INPUTS.items():
            _set(user, f"campo-{name}", value)
        user.find(marker="criar-agenda").click()
        await user.should_see(marker="detalhes-agenda", retries=100)
        await user.should_see("Protocolo das 9h")
        [info] = await world.schedules.list_schedules(world.a.operator)
        assert info.inputs == PROTOCOLO_INPUTS and info.enabled
        user.find(marker="desligar-agenda").click()
        await user.should_see(marker="ligar-agenda", retries=100)
        assert not (await world.schedules.list_schedules(world.a.operator))[0].enabled
        await user.should_see(marker="sem-ocorrencias")
        await logout(user)
        # Quem só lê vê a agenda, mas não cria nem muda.
        await login(user, "leitora", "/agendas")
        await user.should_see(marker=f"agenda-{info.schedule_id}", retries=50)
        await user.should_see(marker="sem-permissao-agendas")
        await user.should_not_see(marker="nova-agenda")
        await user.open(f"/agendas/{info.schedule_id}")
        await user.should_see(marker="detalhes-agenda", retries=50)
        await user.should_not_see(marker="ligar-agenda")
        await logout(user)
        # Outra empresa: a agenda não existe.
        await login(user, "outra", f"/agendas/{info.schedule_id}")
        await user.should_see("Agenda não encontrada.", retries=50)
        await user.open("/agendas")
        await user.should_see(marker="sem-agendas", retries=50)


async def test_invalid_cron_shows_the_reason(world: World) -> None:
    async with _user(world) as user:
        await login(user, "operador", "/agendas")
        await user.should_see(marker="nova-agenda", retries=50)
        _set(user, "robo-agenda", str(world.robots_a["emitir_protocolo"]))
        await user.should_see(marker="campo-cpf", retries=50)
        _set(user, "cron-agenda", "0 0 31 2 *")
        for name, value in PROTOCOLO_INPUTS.items():
            _set(user, f"campo-{name}", value)
        user.find(marker="criar-agenda").click()
        await user.should_see("nunca acontece", retries=50)
    assert await world.schedules.list_schedules(world.a.operator) == []


async def test_webhook_secret_appears_only_once(world: World) -> None:
    async with _user(world) as user:
        await login(user, "operador", "/webhooks")
        await user.should_see(marker="sem-webhooks", retries=50)
        _set(user, "robo-webhook", str(world.robots_a["emitir_protocolo"]))
        _set(user, "titulo-webhook", "Pedidos do ERP")
        user.find(marker="criar-webhook").click()
        await user.should_see(marker="chave-webhook", retries=100)
        [shown] = [e.value for e in user.find(marker="valor-chave").elements]
        assert shown.startswith("whsec_")
        robot = world.robots_a["emitir_protocolo"]
        [hook] = await world.webhooks.list_webhooks(world.a.operator, robot)
        # Ao abrir de novo, a chave não aparece mais; a troca mostra outra.
        await user.open("/webhooks")
        await user.should_see(marker=f"webhook-{hook.webhook_id}", retries=50)
        await user.should_not_see(marker="chave-webhook")
        user.find(marker=f"trocar-chave-{hook.webhook_id}").click()
        await user.should_see(marker="chave-webhook", retries=100)
        [rotated] = [e.value for e in user.find(marker="valor-chave").elements]
        assert rotated != shown and rotated.startswith("whsec_")
        user.find(marker=f"ligar-webhook-{hook.webhook_id}").click()
        await user.should_see("desligado", retries=100)
        await logout(user)
        await login(user, "leitora", "/webhooks")
        await user.should_see(marker=f"webhook-{hook.webhook_id}", retries=50)
        await user.should_not_see(marker=f"trocar-chave-{hook.webhook_id}")
        await user.should_see(marker="sem-permissao-webhooks")
        await logout(user)
        await login(user, "outra", "/webhooks")
        await user.should_see(marker="sem-webhooks", retries=50)


async def _upload(user: User, name: str, data: bytes) -> None:
    [element] = list(user.find(marker="arquivo-lote").elements)
    await element.handle_uploads([SmallFileUpload(name=name, content_type="text/csv", _data=data)])


async def test_batch_upload_preview_confirm_progress_and_download(world: World) -> None:
    async with _user(world) as user:
        await login(user, "operador", "/lotes")
        await user.should_see(marker="novo-lote", retries=50)
        await user.should_see(marker="sem-lotes")
        _set(user, "robo-lote", str(world.robots_a["emitir_protocolo"]))
        # Uma linha com erro: a prévia mostra, e não há como confirmar.
        bad = csv_bytes([("Nome completo", "CPF", "UF"), PROTOCOL_ROWS[0], ("Fulano", "123", "SP")])
        await _upload(user, "lote.csv", bad)
        await user.should_see(marker="erros-linhas", retries=100)
        await user.should_see("Linha 3: cpf: valor fora do formato esperado")
        await user.should_not_see(marker="confirmar-lote")
        # Uma planilha com fórmula nem chega à prévia.
        formula = xlsx_bytes([("Nome completo", "CPF", "UF"), PROTOCOL_ROWS[0]], formula_at=(2, 1))
        await _upload(user, "lote.xlsx", formula)
        await user.should_see(marker="erros-planilha", retries=100)
        await user.should_see("fórmulas")
        await _upload(user, "lote.csv", protocol_sheet())
        await user.should_see(marker="previa-lote", retries=100)
        await user.should_see("3 linhas para o robô emitir_protocolo")
        await user.should_see(marker="amostra-previa")
        user.find(marker="confirmar-lote").click()
        await user.should_see(marker="andamento-lote", retries=100)
        await user.should_see("0 de 3 terminadas")
        await user.should_see(marker="linha-lote-4")
        [batch] = await world.batches.list_batches(world.a.operator)
        user.find(marker="baixar-csv").click()
        response = await user.download.next(timeout=5)
        text = response.content.decode("utf-8-sig")
        assert text.splitlines()[0].startswith('"linha";"execucao";"situacao"')
        assert text.count("Na fila") == 3
        await logout(user)
        # Quem só lê acompanha, mas não cria; outra empresa não vê o lote.
        await login(user, "leitora", "/lotes")
        await user.should_see(marker=f"lote-{batch.batch_id}", retries=50)
        await user.should_see(marker="sem-permissao-lotes")
        await logout(user)
        await login(user, "outra", f"/lotes/{batch.batch_id}")
        await user.should_see("Lote não encontrado.", retries=50)
    assert batch.total == 3
    assert isinstance(batch.batch_id, UUID)


async def test_batch_upload_accepts_the_batch_size_limit(world: World) -> None:
    # A tela recusa no navegador o que passa do limite dos lotes, e só isso (o servidor
    # aceita o mesmo tamanho no envio dela; veja o teste do limite do corpo na API).
    async with _user(world, batch_max_bytes=2 * 1024 * 1024) as user:
        await login(user, "operador", "/lotes")
        await user.should_see(marker="novo-lote", retries=50)
        [element] = list(user.find(marker="arquivo-lote").elements)
        assert element.props["max-file-size"] == 2 * 1024 * 1024
        await user.should_see("de até 2048 KiB")
