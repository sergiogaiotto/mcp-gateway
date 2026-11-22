"""Vídeo só em falha: guardado quando a execução não dá certo, nunca muda a execução."""

import asyncio
import hashlib
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser

from rapa.core.contracts import RunnerJob, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.robots import emitir_protocolo, portal_credentials
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.runner import worker as runner_worker
from rapa.runner.browser import run_context
from rapa.runner.interpreter import Interpreter
from rapa.runner.ports import DictSecretProvider, DirectoryArtifactSink, ListEventSink
from rapa.runner.ports import LocalFileProvider as Files
from rapa.runner.result import ArtifactKind, RunResult
from rapa.runner.video import VIDEO_NAME, ffmpeg_installed, video_allowed
from rapa.runner.worker import run_job
from tests.runner.support import (
    SECRETS,
    VIDEO_REQUIRED_ENV,
    ffmpeg_or_skip,
    file_bytes,
)

#: Começo de todo arquivo WebM (o cabeçalho EBML).
_WEBM_MAGIC = bytes.fromhex("1a45dfa3")


def _target(intent: str, locator: dict[str, Any]) -> dict[str, Any]:
    return {"intent": intent, "locators": [locator]}


def _robot(portal: RunningPortal, *steps: dict[str, Any], name: str = "abrir_login") -> Manifest:
    """Robô que abre o login do portal e faz os passos dados."""
    base = portal.base_url
    return Manifest.model_validate(
        {
            "name": name,
            "title": "Abrir o login",
            "description": "Abre a página de login do portal de teste.",
            "allowed_domains": [portal.host],
            "credentials": portal_credentials(base),
            "steps": [
                {"kind": "navigate", "id": "abrir", "title": "Abrir", "url": f"{base}/login"},
                *steps,
            ],
        }
    )


_FAIL = {
    "kind": "click",
    "id": "botao_sumido",
    "title": "Clicar num botão que não existe",
    "timeout_ms": 1000,
    "target": _target("botão Sumido", {"by": "role", "value": "button", "name": "Sumido"}),
}
_READ = {
    "kind": "wait_for",
    "id": "esperar_titulo",
    "title": "Esperar o título",
    "target": _target("título Entrar", {"by": "role", "value": "heading", "name": "Entrar"}),
}


def _fill_password(label: str) -> dict[str, Any]:
    return {
        "kind": "fill",
        "id": "digitar_senha",
        "title": "Digitar a senha",
        "target": _target(f"campo {label}", {"by": "label", "value": label}),
        "value": {"source": "secret", "credential": "portal", "field": "password"},
    }


def _job(manifest: Manifest) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=manifest,
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


@pytest.fixture
def ffmpeg(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """O ``ffmpeg`` do vídeo do Playwright para este teste.

    Sem nenhum na máquina, o teste é pulado; no CI (``RAPA_TEST_VIDEO_OBRIGATORIO=1``,
    com o ``ffmpeg`` do Debian), falha.
    """
    browsers = ffmpeg_or_skip(tmp_path_factory.mktemp("navegadores"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    return browsers


@pytest.fixture
def no_ffmpeg(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uma pasta de navegadores sem ``ffmpeg``: o vídeo não abre."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path_factory.mktemp("sem-ffmpeg")))


class _Runs:
    """Roda trabalhos pelo :func:`run_job` do worker, com ou sem vídeo."""

    def __init__(self, tmp_path: Path, chromium: str) -> None:
        self.tmp_path = tmp_path
        self.chromium = chromium
        self.count = 0

    async def __call__(
        self,
        manifest: Manifest,
        *,
        video: bool = True,
        events: ListEventSink | None = None,
        cancel: asyncio.Event | None = None,
    ) -> tuple[RunResult, Path, Path]:
        self.count += 1
        artifacts = self.tmp_path / f"artefatos-{self.count}"
        video_dir = self.tmp_path / f"video-{self.count}"
        result = await run_job(
            _job(manifest),
            secrets=DictSecretProvider(SECRETS),
            events=events if events is not None else ListEventSink(),
            artifacts_dir=artifacts,
            chromium=self.chromium,
            browser_args=[HOST_RESOLVER_ARG],
            video_dir=video_dir if video else None,
            cancel=cancel,
        )
        return result, artifacts, video_dir


@pytest.fixture
def runs(tmp_path: Path, chromium_path: str) -> _Runs:
    return _Runs(tmp_path, chromium_path)


def _videos(result: RunResult) -> list[Any]:
    return [a for a in result.artifacts if a.kind == ArtifactKind.VIDEO]


# -- quando o vídeo pode ser guardado -----------------------------------------------------


def test_robot_typing_the_user_or_the_code_from_the_vault_is_never_filmed(
    portal_v1: RunningPortal,
) -> None:
    # O usuário e o código TOTP vão para campos comuns: o vídeo os mostraria.
    assert not video_allowed(emitir_protocolo(portal_v1.base_url, portal_v1.host))
    assert video_allowed(_robot(portal_v1, _FAIL))
    assert video_allowed(_robot(portal_v1, _fill_password("Senha"), _FAIL))


# -- os testes que filmam rodam no CI --------------------------------------------------------


def test_ci_installs_ffmpeg_and_never_skips_the_video_tests() -> None:
    """Sem o ``ffmpeg`` no CI, todo teste que filma de verdade seria pulado em silêncio."""
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    [install] = [line for line in workflow.splitlines() if "apt-get install" in line]
    assert "ffmpeg" in install.split()
    assert f'{VIDEO_REQUIRED_ENV}: "1"' in workflow


def test_without_ffmpeg_the_video_tests_fail_when_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "sem-programas"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "sem-navegadores"))
    monkeypatch.delenv(VIDEO_REQUIRED_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        ffmpeg_or_skip(tmp_path / "a")
    monkeypatch.setenv(VIDEO_REQUIRED_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        ffmpeg_or_skip(tmp_path / "b")


def _ffmpeg_revision() -> str:
    import json

    import playwright

    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
    return next(str(b["revision"]) for b in browsers if b["name"] == "ffmpeg")


@pytest.mark.skipif(sys.platform != "linux", reason="o nome do ffmpeg muda em cada sistema")
def test_ffmpeg_installed_looks_where_playwright_looks(tmp_path: Path) -> None:
    env = {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "navegadores")}
    assert ffmpeg_installed(env) is False
    executable = tmp_path / "navegadores" / f"ffmpeg-{_ffmpeg_revision()}" / "ffmpeg-linux"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    assert ffmpeg_installed(env) is True
    # Sem a variável, o cache do usuário.
    assert ffmpeg_installed({"XDG_CACHE_HOME": str(tmp_path / "cache")}) is False
    cached = tmp_path / "cache" / "ms-playwright" / executable.relative_to(tmp_path / "navegadores")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"")
    assert ffmpeg_installed({"XDG_CACHE_HOME": str(tmp_path / "cache")}) is True


# -- sucesso apaga, falha guarda -----------------------------------------------------------


@pytest.mark.browser
@pytest.mark.usefixtures("portal_v1", "ffmpeg")
async def test_failed_run_keeps_the_video_and_a_successful_one_does_not(
    runs: _Runs, portal_v1: RunningPortal
) -> None:
    failed, artifacts, video_dir = await runs(_robot(portal_v1, _FAIL))
    assert failed.state is RunState.FAILED
    assert failed.error_code == "elemento_nao_encontrado"
    [video] = _videos(failed)
    assert video.name == VIDEO_NAME
    data = file_bytes(video.path)
    assert data.startswith(_WEBM_MAGIC) and video.size == len(data) > 0
    assert hashlib.sha256(data).hexdigest() == video.sha256
    assert Path(video.path).parent == artifacts.resolve()
    # A pasta temporária do vídeo some; ficam só a tela da falha e o vídeo.
    assert not video_dir.exists()
    assert sorted(p.name for p in artifacts.iterdir()) == sorted(a.name for a in failed.artifacts)

    succeeded, artifacts, video_dir = await runs(_robot(portal_v1, _READ))
    assert succeeded.state is RunState.SUCCEEDED, succeeded.error_message
    assert _videos(succeeded) == []
    assert not video_dir.exists()
    assert not any(p.suffix == ".webm" for p in artifacts.iterdir())


@pytest.mark.browser
@pytest.mark.usefixtures("ffmpeg")
async def test_password_only_in_a_password_field_keeps_the_video(
    runs: _Runs, portal_v1: RunningPortal
) -> None:
    kept, _, _ = await runs(_robot(portal_v1, _fill_password("Senha"), _FAIL))
    assert kept.state is RunState.FAILED
    assert len(_videos(kept)) == 1
    # A mesma senha num campo comum apareceria no vídeo: ele não é guardado.
    dropped, artifacts, video_dir = await runs(_robot(portal_v1, _fill_password("Usuário"), _FAIL))
    assert dropped.state is RunState.FAILED
    assert dropped.error_code == kept.error_code
    assert _videos(dropped) == []
    assert not video_dir.exists()
    assert not any(p.suffix == ".webm" for p in artifacts.iterdir())


@pytest.mark.browser
@pytest.mark.usefixtures("ffmpeg")
async def test_robot_with_the_user_from_the_vault_is_not_even_filmed(
    runs: _Runs, portal_v1: RunningPortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    real = runner_worker._video_context

    async def spy(browser: Browser, certificates: Any, directory: Path) -> Any:
        opened.append(directory)
        return await real(browser, certificates, directory)

    monkeypatch.setattr(runner_worker, "_video_context", spy)
    manifest = _robot(
        portal_v1,
        {
            "kind": "fill",
            "id": "digitar_usuario",
            "title": "Digitar o usuário",
            "target": _target("campo Usuário", {"by": "label", "value": "Usuário"}),
            "value": {"source": "secret", "credential": "portal", "field": "username"},
        },
        _FAIL,
    )
    result, _, video_dir = await runs(manifest)
    assert result.state is RunState.FAILED
    assert _videos(result) == [] and opened == [] and not video_dir.exists()


class _StopAfter(ListEventSink):
    """Para o worker (como o SIGTERM do lançador no fim do prazo) depois de um passo."""

    def __init__(self, step_id: str, cancel: asyncio.Event) -> None:
        super().__init__()
        self.step_id = step_id
        self.cancel = cancel

    async def emit(self, event: RunEvent) -> None:
        await super().emit(event)
        if event.kind is RunEventKind.STEP_SUCCEEDED and event.step_id == self.step_id:
            self.cancel.set()


_IRREVERSIBLE = {
    "kind": "click",
    "id": "enviar",
    "title": "Clicar em Entrar",
    "effect": "irreversible",
    "target": _target("botão Entrar", {"by": "role", "value": "button", "name": "Entrar"}),
}


@pytest.mark.browser
@pytest.mark.usefixtures("ffmpeg")
async def test_a_run_stopped_after_an_irreversible_step_keeps_the_video(
    runs: _Runs, portal_v1: RunningPortal
) -> None:
    """O lançador interrompe o worker (SIGTERM) depois de um passo irreversível: a execução
    fica indeterminada, e é aí que o vídeo mais ajuda."""
    cancel = asyncio.Event()
    stopped, _, video_dir = await runs(
        _robot(portal_v1, _IRREVERSIBLE, _READ), events=_StopAfter("enviar", cancel), cancel=cancel
    )
    assert stopped.state is RunState.INDETERMINATE, stopped.error_message
    [video] = _videos(stopped)
    assert file_bytes(video.path).startswith(_WEBM_MAGIC)
    assert not video_dir.exists()
    # Parada antes de qualquer passo que grava, a execução é cancelada: sem vídeo.
    cancel = asyncio.Event()
    cancelled, artifacts, video_dir = await runs(
        _robot(portal_v1, _READ), events=_StopAfter("abrir", cancel), cancel=cancel
    )
    assert cancelled.state is RunState.CANCELLED
    assert _videos(cancelled) == [] and not video_dir.exists()
    assert not any(p.suffix == ".webm" for p in artifacts.iterdir())


# -- a gravação nunca muda a execução --------------------------------------------------------


@pytest.mark.browser
@pytest.mark.usefixtures("no_ffmpeg")
async def test_without_ffmpeg_the_run_is_the_same_without_video(
    runs: _Runs, portal_v1: RunningPortal
) -> None:
    for steps, state in (((_FAIL,), RunState.FAILED), ((_READ,), RunState.SUCCEEDED)):
        filmed, _, video_dir = await runs(_robot(portal_v1, *steps))
        plain, _, _ = await runs(_robot(portal_v1, *steps), video=False)
        assert filmed.state is plain.state is state
        assert filmed.error_code == plain.error_code
        assert filmed.steps_done == plain.steps_done
        assert _videos(filmed) == [] and not video_dir.exists()


@pytest.mark.browser
@pytest.mark.usefixtures("ffmpeg")
@pytest.mark.parametrize("problem", ["erro", "demora"])
async def test_error_or_delay_when_saving_the_video_never_changes_the_result(
    runs: _Runs, portal_v1: RunningPortal, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    async def broken(self: Any, context: Any, sink: Any, *, keep: bool) -> Any:
        if problem == "erro":
            raise OSError("disco cheio")
        import asyncio

        await asyncio.sleep(5)

    monkeypatch.setattr(runner_worker._Video, "_close_and_save", broken)
    monkeypatch.setattr(runner_worker, "VIDEO_CLOSE_TIMEOUT_S", 0.5)
    result, _, video_dir = await runs(_robot(portal_v1, _FAIL))
    assert result.state is RunState.FAILED
    assert result.error_code == "elemento_nao_encontrado"
    assert result.error_message and "Sumido" in result.error_message
    assert _videos(result) == [] and not video_dir.exists()


# -- o executor confere o campo da senha só com o vídeo ligado ------------------------------


@pytest.fixture
def interpreted(browser: Browser, tmp_path: Path) -> Iterator[Any]:
    async def run(
        manifest: Manifest, *, record_video: bool, page_script: str | None = None
    ) -> tuple[RunResult, Interpreter]:
        interpreter = Interpreter(
            manifest,
            {},
            secrets=DictSecretProvider(SECRETS),
            files=Files(None),
            artifacts=DirectoryArtifactSink(tmp_path / f"artefatos-{new_id()}"),
            events=ListEventSink(),
            run_id=new_id(),
            attempt=1,
            record_video=record_video,
        )
        async with run_context(browser) as context:
            if page_script is not None:
                # O script da própria página, como se o portal o tivesse.
                await context.add_init_script(page_script)
            result = await interpreter.run(context)
        return result, interpreter

    yield run


@pytest.mark.browser
async def test_interpreter_marks_the_video_unsafe_for_a_secret_outside_a_password_field(
    interpreted: Any, portal_v1: RunningPortal
) -> None:
    safe = _robot(portal_v1, _fill_password("Senha"), _READ)
    unsafe = _robot(portal_v1, _fill_password("Usuário"), _READ)
    result, interpreter = await interpreted(safe, record_video=True)
    assert result.state is RunState.SUCCEEDED and not interpreter.video_unsafe
    result, interpreter = await interpreted(unsafe, record_video=True)
    # O passo dá certo do mesmo jeito: a conferência só lê o campo.
    assert result.state is RunState.SUCCEEDED and interpreter.video_unsafe
    # Sem o vídeo ligado, nada é conferido.
    result, interpreter = await interpreted(unsafe, record_video=False)
    assert result.state is RunState.SUCCEEDED and not interpreter.video_unsafe


#: "Mostrar senha" ligado: o campo é comum enquanto a senha é digitada, e a página o
#: esconde de novo logo depois (no ``input``), antes da conferência de depois.
_SHOW_PASSWORD_THEN_HIDE = """
document.addEventListener('DOMContentLoaded', () => {
    const field = document.getElementById('senha');
    field.type = 'text';
    field.addEventListener('input', () => { field.type = 'password'; });
});
"""
#: Campo de senha que mostra o que é digitado enquanto a tecla está apertada.
_REVEAL_WHILE_TYPING = """
document.addEventListener('DOMContentLoaded', () => {
    const field = document.getElementById('senha');
    field.addEventListener('keydown', () => { field.type = 'text'; });
    field.addEventListener('keyup', () => { field.type = 'password'; });
});
"""


@pytest.mark.browser
@pytest.mark.parametrize(
    ("script", "sequential"),
    [(_SHOW_PASSWORD_THEN_HIDE, False), (_REVEAL_WHILE_TYPING, True)],
    ids=["mostrar-senha-ligado", "mostra-ao-digitar"],
)
async def test_a_password_field_that_is_plain_while_typing_marks_the_video_unsafe(
    interpreted: Any, portal_v1: RunningPortal, script: str, sequential: bool
) -> None:
    """O campo é de senha na conferência de depois, mas foi comum enquanto a senha era
    digitada: o vídeo a mostraria, então não pode ser guardado."""
    fill = {**_fill_password("Senha"), "sequential": sequential}
    manifest = _robot(portal_v1, fill, _READ)
    result, interpreter = await interpreted(manifest, record_video=True, page_script=script)
    # O passo dá certo do mesmo jeito: a conferência só lê o campo.
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert interpreter.video_unsafe
    # O mesmo campo, sem a página mexer no tipo dele, deixa o vídeo ser guardado.
    result, interpreter = await interpreted(manifest, record_video=True)
    assert result.state is RunState.SUCCEEDED and not interpreter.video_unsafe
