"""Lançador local das sessões: ambiente mínimo, token fora do ``repr`` e falhas ao abrir."""

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from rapa.control.launcher import child_env
from rapa.control.sessions import LocalSessionLauncher, LocalSessionSettings, SessionJob
from rapa.control.sessions.launcher import SessionEndpoint, SessionNotStarted
from rapa.core.ids import new_id
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal


def test_child_environment_leaves_control_plane_variables_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAPA_DATABASE_URL", "postgresql://app:segredo@banco/rapa")
    monkeypatch.setenv("RAPA_LLM_API_KEY", "sk-nao-pode-passar")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    env = child_env()
    assert "RAPA_DATABASE_URL" not in env and "RAPA_LLM_API_KEY" not in env
    # Desligaria a guarda da saída para a própria máquina (o loopback sairia direto).
    assert "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK" not in env
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw-browsers"


def test_endpoint_token_is_not_in_repr() -> None:
    endpoint = SessionEndpoint(url="ws://127.0.0.1:1/", token="t" * 43)
    assert "t" * 43 not in repr(endpoint)


@pytest.mark.parametrize("program", ["/bin/true", "/caminho/que/nao/existe"])
async def test_a_host_that_never_gets_ready_is_refused(program: str, tmp_path: Path) -> None:
    # "/bin/true -m rapa.session.host" sai sem a linha de pronto; o outro nem inicia.
    profiles = tmp_path / "sessoes"
    launcher = LocalSessionLauncher(
        LocalSessionSettings(
            python=program, start_timeout_s=10, kill_grace_s=1, profile_dir=profiles
        )
    )
    job = SessionJob(session_id=new_id(), company_id=new_id(), start_url="https://x.rapa.test/")
    with pytest.raises(SessionNotStarted):
        await launcher.start(job)
    assert not await launcher.is_alive(job.session_id)
    await launcher.stop(job.session_id)  # nada a parar
    await launcher.terminate(job.session_id)
    assert launcher.running == 0
    assert list(profiles.iterdir()) == []  # a pasta da sessão não fica para trás


def _browsers_using(folder: Path) -> list[str]:
    """Processos do Chromium com um perfil dentro de ``folder`` (pelo /proc)."""
    marker = f"--user-data-dir={folder}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if marker in (entry / "cmdline").read_bytes():
                    found.append(entry.name)
    return found


def _launcher(chromium: str, profiles: Path) -> LocalSessionLauncher:
    return LocalSessionLauncher(
        LocalSessionSettings(
            chromium=chromium,
            browser_args=(HOST_RESOLVER_ARG,),
            profile_dir=profiles,
            kill_grace_s=1,
        )
    )


@pytest.mark.browser
async def test_profile_is_removed_even_when_the_host_is_killed(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    profiles = tmp_path / "sessoes"
    launcher = _launcher(chromium_path, profiles)
    job = SessionJob(session_id=new_id(), company_id=new_id(), start_url=f"{portal_v1.base_url}/")
    await launcher.start(job)
    host = launcher._hosts[job.session_id]
    try:
        # A pasta é só desta sessão, e o perfil (com os cookies do portal) fica dentro dela.
        for _ in range(100):
            if list(profiles.glob("*/*/perfil/Default")):
                break
            await asyncio.sleep(0.1)
        assert len(list(profiles.iterdir())) == 1
        assert _browsers_using(profiles)
        # O prazo de encerramento, a falta de memória: o hospedeiro não consegue limpar nada.
        os.killpg(host.process.pid, signal.SIGKILL)
        await host.process.wait()
        assert host.watcher is not None
        await asyncio.wait_for(asyncio.shield(host.watcher), timeout=20)
        assert list(profiles.iterdir()) == [], "o perfil (cookies do portal) ficou no disco"
        assert _browsers_using(profiles) == []
    finally:
        await launcher.aclose()


@pytest.mark.browser
async def test_terminate_does_not_wait_for_a_host_that_was_never_stopped(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    profiles = tmp_path / "sessoes"
    launcher = _launcher(chromium_path, profiles)
    jobs = [
        SessionJob(session_id=new_id(), company_id=new_id(), start_url=f"{portal_v1.base_url}/")
        for _ in range(3)
    ]
    for job in jobs:
        await launcher.start(job)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await launcher.terminate(jobs[0].session_id)
    # Sem "parar", nada espera os 15 s do stop: o SIGTERM vai na hora.
    assert loop.time() - started < 8
    assert not await launcher.is_alive(jobs[0].session_id)
    # O fim do serviço encerra os outros ao mesmo tempo, não um depois do outro.
    started = loop.time()
    await launcher.aclose()
    assert loop.time() - started < 8
    assert launcher.running == 0
    assert list(profiles.iterdir()) == []
