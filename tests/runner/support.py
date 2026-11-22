"""Auxiliares dos testes do executor."""

import json
import os
import shutil
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pyotp
from fastapi.testclient import TestClient
from playwright.async_api import Browser, BrowserContext

from rapa.core.contracts import RunMode
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.server import RunningPortal
from rapa.runner.browser import run_context
from rapa.runner.interpreter import Interpreter
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    EventSink,
    ListEventSink,
    LocalFileProvider,
)
from rapa.runner.result import RunResult

CPF_VALIDO = "529.982.247-25"
PROTOCOLO_INPUTS = {"nome": "Maria da Silva", "cpf": CPF_VALIDO, "uf": "SP"}

#: Segredos no formato do SECRETS.json, para os robôs de exemplo.
SECRETS: dict[str, dict[str, str]] = {
    "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
    "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
}
#: Valores que nunca podem aparecer em eventos, resultados ou nomes de artefatos.
SECRET_VALUES = (DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET)


@dataclass
class Outcome:
    result: RunResult
    events: list[RunEvent]
    artifacts_dir: Path

    def kinds(self) -> list[RunEventKind]:
        return [event.kind for event in self.events]

    def of(self, kind: RunEventKind, step_id: str | None = None) -> list[RunEvent]:
        return [
            e for e in self.events if e.kind is kind and (step_id is None or e.step_id == step_id)
        ]

    def dump(self) -> str:
        """Tudo o que sai do executor em texto: resultado, eventos e nomes de arquivos."""
        names = sorted(p.name for p in self.artifacts_dir.iterdir())
        return "\n".join(
            [
                self.result.model_dump_json(),
                *(event.model_dump_json() for event in self.events),
                *names,
            ]
        )


type ContextSetup = Callable[[BrowserContext], Awaitable[None]]


class RobotRunner:
    """Roda um manifesto num contexto novo do navegador compartilhado pelo teste."""

    def __init__(self, browser: Browser, tmp_path: Path) -> None:
        self.browser = browser
        self.tmp_path = tmp_path
        self.count = 0

    async def __call__(
        self,
        manifest: Manifest,
        inputs: dict[str, Any] | None = None,
        *,
        secrets: dict[str, dict[str, str]] | None = None,
        mode: RunMode = RunMode.FULL,
        files_dir: Path | None = None,
        deadline: datetime | None = None,
        events: EventSink | None = None,
        prepare_context: ContextSetup | None = None,
        after_run: ContextSetup | None = None,
        **options: Any,
    ) -> Outcome:
        self.count += 1
        artifacts_dir = self.tmp_path / f"artefatos-{self.count}"
        sink = events if events is not None else ListEventSink()
        provider = DictSecretProvider(SECRETS if secrets is None else secrets)
        interpreter = Interpreter(
            manifest,
            inputs or {},
            secrets=provider,
            files=LocalFileProvider(files_dir),
            artifacts=DirectoryArtifactSink(artifacts_dir),
            events=sink,
            run_id=new_id(),
            attempt=1,
            mode=mode,
            deadline=deadline,
            **options,
        )
        async with run_context(self.browser) as context:
            if prepare_context is not None:
                await prepare_context(context)
            result = await interpreter.run(context)
            if after_run is not None:
                await after_run(context)
        recorded = sink.events if isinstance(sink, ListEventSink) else []
        return Outcome(result, recorded, artifacts_dir)


def edit(manifest: Manifest, change: Callable[[dict[str, Any]], None]) -> Manifest:
    """Copia o manifesto aplicando uma alteração no dicionário (para injetar falhas)."""
    data = manifest.model_dump(mode="json")
    change(data)
    return Manifest.model_validate(data)


def find_step(data: dict[str, Any], step_id: str) -> dict[str, Any]:
    def walk(steps: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for step in steps:
            yield step
            for key in ("steps", "then", "otherwise"):
                yield from walk(step.get(key) or [])

    for step in walk(data["steps"]):
        if step["id"] == step_id:
            return step
    raise KeyError(step_id)


def set_step(manifest: Manifest, step_id: str, **changes: Any) -> Manifest:
    def change(data: dict[str, Any]) -> None:
        find_step(data, step_id).update(changes)

    return edit(manifest, change)


def assert_seq(events: list[RunEvent]) -> None:
    assert [e.seq for e in events] == list(range(len(events)))
    assert events[0].kind is RunEventKind.RUN_STARTED
    assert events[-1].kind in (RunEventKind.RUN_SUCCEEDED, RunEventKind.RUN_FAILED)


def assert_no_secrets(text: str) -> None:
    for value in SECRET_VALUES:
        assert value not in text


def portal_download(portal: RunningPortal, numero: str) -> bytes:
    """Baixa o arquivo de uma nota direto do portal (sem navegador), para comparar."""
    client = TestClient(portal.app)
    client.post("/login", data={"usuario": DEFAULT_USER, "senha": DEFAULT_PASSWORD})
    client.post("/login/codigo", data={"codigo": pyotp.TOTP(DEFAULT_TOTP_SECRET).now()})
    response = client.get(f"/notas/{numero}/download")
    assert response.status_code == 200
    return response.content


def file_bytes(path: str | Path) -> bytes:
    """Conteúdo de um arquivo (síncrono, fora do laço de eventos dos testes)."""
    return Path(path).read_bytes()


def load_jsonl(path: Path) -> list[RunEvent]:
    return [
        RunEvent.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def ffmpeg_browsers_path(base: Path) -> Path | None:
    """Pasta para ``PLAYWRIGHT_BROWSERS_PATH`` com o ``ffmpeg`` que o vídeo do Playwright pede.

    O Playwright procura ``<pasta>/ffmpeg-<revisão>/ffmpeg-linux``. Sem a revisão certa
    instalada (no CI, o Chromium é o do Debian), a pasta aponta para outro ``ffmpeg``
    da máquina: o do Playwright em outra revisão ou o do sistema, que também grava
    VP8 (no CI, o ``ffmpeg`` do Debian). Sem nenhum, ``None`` (o teste do vídeo é
    pulado, ou falha no CI: :func:`ffmpeg_or_skip`).
    """
    import playwright

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "ffmpeg")
    except OSError, ValueError, KeyError, StopIteration:
        return None
    # A revisão certa; senão o do sistema; por último, outra revisão do Playwright (uma
    # antiga pode não gravar nada, e o teste do vídeo falharia sem motivo).
    installed = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/nenhum")
    candidates = [installed / f"ffmpeg-{revision}" / "ffmpeg-linux"]
    if system := shutil.which("ffmpeg"):
        candidates.append(Path(system))
    candidates += sorted(installed.glob("ffmpeg-*/ffmpeg-linux"), reverse=True)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            target = base / f"ffmpeg-{revision}"
            target.mkdir(parents=True, exist_ok=True)
            link = target / "ffmpeg-linux"
            if not link.exists():
                link.symlink_to(candidate)
            return base
    return None


#: Com ``1`` (no CI, que instala o ``ffmpeg`` do Debian), os testes que filmam de verdade
#: falham em vez de serem pulados quando não há ``ffmpeg``.
VIDEO_REQUIRED_ENV = "RAPA_TEST_VIDEO_OBRIGATORIO"


def ffmpeg_or_skip(base: Path) -> Path:
    """:func:`ffmpeg_browsers_path`, ou pula o teste (falha, se ``VIDEO_REQUIRED_ENV=1``)."""
    import pytest

    browsers = ffmpeg_browsers_path(base)
    if browsers is not None:
        return browsers
    message = "sem ffmpeg para o vídeo do Playwright"
    if os.environ.get(VIDEO_REQUIRED_ENV) == "1":
        pytest.fail(f"{message}, e {VIDEO_REQUIRED_ENV}=1 pede os testes do vídeo")
    pytest.skip(message)
