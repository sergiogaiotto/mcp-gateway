"""Evidências de ponta a ponta: o vídeo da falha e as telas, pelo serviço inteiro.

O robô roda de verdade (despachante, lançador local, worker e Chromium) contra o portal
de teste; os artefatos são registrados pelo store e lidos pelo REST, com o vídeo só na
execução que falhou.
"""

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from rapa.control.artifacts import LocalArtifactStore
from rapa.control.services import Dispatcher, Retention
from rapa.core.robot import Manifest
from rapa.fixtures.server import RunningPortal
from rapa.server import ServerSettings, create_server_app
from tests.control.api.support import asgi_client, auth, running
from tests.control.db.support import Tenant
from tests.e2e.support import TOKENS, publish, wait_finished
from tests.runner.support import ffmpeg_or_skip

_WEBM_MAGIC = bytes.fromhex("1a45dfa3")


def _robot(portal: RunningPortal, name: str, last: dict[str, Any]) -> dict[str, Any]:
    return Manifest.model_validate(
        {
            "name": name,
            "title": "Abrir o login",
            "description": "Abre a página de login do portal de teste.",
            "allowed_domains": [portal.host],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir",
                    "title": "Abrir",
                    "url": f"{portal.base_url}/login",
                },
                last,
            ],
        }
    ).model_dump(mode="json")


_MISSING = {
    "kind": "click",
    "id": "botao_sumido",
    "title": "Clicar num botão que não existe",
    "timeout_ms": 1000,
    "target": {
        "intent": "botão Sumido",
        "locators": [{"by": "role", "value": "button", "name": "Sumido"}],
    },
}
_HEADING = {
    "kind": "wait_for",
    "id": "esperar_titulo",
    "title": "Esperar o título",
    "target": {
        "intent": "título Entrar",
        "locators": [{"by": "role", "value": "heading", "name": "Entrar"}],
    },
}


@pytest.mark.database
@pytest.mark.browser
async def test_failed_run_has_video_and_screenshot_and_successful_run_has_no_video(
    platform_app: tuple[FastAPI, Tenant, Tenant],
    portal_v1: RunningPortal,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browsers = ffmpeg_or_skip(tmp_path_factory.mktemp("navegadores"))
    # O lançador passa as variáveis PLAYWRIGHT_* ao worker.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    app, tenant, _other = platform_app
    async with running(app), asgi_client(app) as client:
        await publish(client, tenant, _robot(portal_v1, "botao_sumido", _MISSING))
        await publish(client, tenant, _robot(portal_v1, "ler_titulo", _HEADING))
        headers = auth(TOKENS["operator"])
        runs = {}
        for slug in ("botao_sumido", "ler_titulo"):
            started = await client.post(
                f"/v1/catalogo/{slug}/execucoes", json={"entradas": {}}, headers=headers
            )
            assert started.status_code == 202, started.text
            runs[slug] = started.json()["run_id"]
        failed = await wait_finished(client, runs["botao_sumido"], TOKENS["operator"])
        succeeded = await wait_finished(client, runs["ler_titulo"], TOKENS["operator"])
        assert failed["state"] == "failed" and failed["error_code"] == "elemento_nao_encontrado"
        assert succeeded["state"] == "succeeded", succeeded

        listed = await client.get(
            f"/v1/execucoes/{runs['botao_sumido']}/artefatos", headers=headers
        )
        assert listed.status_code == 200, listed.text
        kinds = {item["kind"]: item for item in listed.json()}
        assert set(kinds) == {"screenshot", "video"}
        video = kinds["video"]
        assert video["name"] == "video-falha.webm" and video["media_type"] == "video/webm"
        got = await client.get(
            f"/v1/execucoes/{runs['botao_sumido']}/artefatos/{video['name']}", headers=headers
        )
        assert got.status_code == 200
        assert got.content.startswith(_WEBM_MAGIC) and len(got.content) == video["size"]
        assert got.headers["content-disposition"] == 'inline; filename="video-falha.webm"'
        shot = kinds["screenshot"]
        png = await client.get(
            f"/v1/execucoes/{runs['botao_sumido']}/artefatos/{shot['name']}", headers=headers
        )
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"

        # A execução que deu certo não guarda vídeo.
        ok = await client.get(f"/v1/execucoes/{runs['ler_titulo']}/artefatos", headers=headers)
        assert all(item["kind"] != "video" for item in ok.json())
        # Quem não vê a execução não vê os artefatos.
        hidden = await client.get(
            f"/v1/execucoes/{runs['botao_sumido']}/artefatos", headers=auth(TOKENS["other"])
        )
        assert hidden.status_code == 404


def test_evidence_configuration(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.json"
    tokens.write_text('{"t": {"sujeito": "s", "empresa": "00000000-0000-0000-0000-000000000001"}}')
    base = {
        "RAPA_DATABASE_URL": "postgresql://rapa_app:x@banco:5432/rapa",
        "RAPA_DEV": "1",
        "RAPA_TOKENS_DEV": str(tokens),
    }
    defaults = ServerSettings.from_env(base)
    assert defaults.video_on_failure is True
    assert (
        defaults.retention_success_days,
        defaults.retention_failure_days,
        defaults.retention_events_days,
    ) == (7, 90, 400)
    changed = ServerSettings.from_env(
        {
            **base,
            "RAPA_VIDEO_EM_FALHAS": "0",
            "RAPA_RETENCAO_SUCESSO_DIAS": "1",
            "RAPA_RETENCAO_FALHAS_DIAS": "30",
            "RAPA_RETENCAO_EVENTOS_DIAS": "800",
        }
    )
    assert changed.video_on_failure is False
    assert (changed.retention_success_days, changed.retention_failure_days) == (1, 30)
    assert changed.retention_events_days == 800
    for name in (
        "RAPA_RETENCAO_SUCESSO_DIAS",
        "RAPA_RETENCAO_FALHAS_DIAS",
        "RAPA_RETENCAO_EVENTOS_DIAS",
    ):
        with pytest.raises(ValueError):
            ServerSettings.from_env({**base, name: "0"})
    # As capturas das que deram certo não ficam mais que as evidências (o servidor não sobe).
    with pytest.raises(ValueError, match="RAPA_RETENCAO_SUCESSO_DIAS"):
        ServerSettings.from_env(
            {**base, "RAPA_RETENCAO_SUCESSO_DIAS": "120", "RAPA_RETENCAO_FALHAS_DIAS": "90"}
        )
    with pytest.raises(ValueError, match="não pode passar"):
        ServerSettings.from_env({**base, "RAPA_RETENCAO_FALHAS_DIAS": "6"})
    equal = ServerSettings.from_env(
        {**base, "RAPA_RETENCAO_SUCESSO_DIAS": "30", "RAPA_RETENCAO_FALHAS_DIAS": "30"}
    )
    assert (equal.retention_success_days, equal.retention_failure_days) == (30, 30)
    app = create_server_app(
        ServerSettings.from_env(
            {
                **base,
                "RAPA_UI": "0",
                "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:x@banco:5432/rapa",
                "RAPA_PASTA_ARTEFATOS": str(tmp_path / "artefatos"),
                "RAPA_RETENCAO_FALHAS_DIAS": "30",
            }
        )
    )
    dispatcher = app.state.dispatcher
    assert isinstance(dispatcher, Dispatcher)
    assert isinstance(dispatcher.artifacts, LocalArtifactStore)
    assert dispatcher.artifacts.root == tmp_path / "artefatos"
    assert isinstance(dispatcher.retention, Retention)
    assert dispatcher.retention.policy.failure_days == 30
    assert dispatcher.launch.settings.video_on_failure is True  # type: ignore[attr-defined]
    assert app.state.artifacts.store.root == tmp_path / "artefatos"


def test_server_warns_when_the_video_is_on_but_nothing_can_be_filmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sem o ffmpeg do Playwright, o worker roda sem vídeo em silêncio: o servidor avisa."""
    tokens = tmp_path / "tokens.json"
    tokens.write_text('{"t": {"sujeito": "s", "empresa": "00000000-0000-0000-0000-000000000001"}}')
    env = {
        "RAPA_DATABASE_URL": "postgresql://rapa_app:x@banco:5432/rapa",
        "RAPA_DISPATCHER_DATABASE_URL": "postgresql://rapa_dispatcher:x@banco:5432/rapa",
        "RAPA_DEV": "1",
        "RAPA_UI": "0",
        "RAPA_TOKENS_DEV": str(tokens),
        "RAPA_PASTA_ARTEFATOS": str(tmp_path / "artefatos"),
    }
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "sem-ffmpeg"))
    warning = "o ffmpeg do Playwright não está instalado"
    with caplog.at_level(logging.WARNING, logger="rapa.server"):
        create_server_app(ServerSettings.from_env(env))
    assert warning in caplog.text and "RAPA_VIDEO_EM_FALHAS=0" in caplog.text
    caplog.clear()
    # Com o vídeo desligado, nada a avisar.
    with caplog.at_level(logging.WARNING, logger="rapa.server"):
        create_server_app(ServerSettings.from_env({**env, "RAPA_VIDEO_EM_FALHAS": "0"}))
    assert warning not in caplog.text
