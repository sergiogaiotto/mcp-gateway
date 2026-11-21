"""Lançador local das sessões: ambiente mínimo, token fora do ``repr`` e falhas ao abrir."""

from pathlib import Path

import pytest

from rapa.control.launcher import child_env
from rapa.control.sessions import LocalSessionLauncher, LocalSessionSettings, SessionJob
from rapa.control.sessions.launcher import SessionEndpoint, SessionNotStarted
from rapa.core.ids import new_id


def test_child_environment_leaves_control_plane_variables_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAPA_DATABASE_URL", "postgresql://app:segredo@banco/rapa")
    monkeypatch.setenv("RAPA_LLM_API_KEY", "sk-nao-pode-passar")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    env = child_env()
    assert "RAPA_DATABASE_URL" not in env and "RAPA_LLM_API_KEY" not in env
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw-browsers"


def test_endpoint_token_is_not_in_repr() -> None:
    endpoint = SessionEndpoint(url="ws://127.0.0.1:1/", token="t" * 43)
    assert "t" * 43 not in repr(endpoint)


@pytest.mark.parametrize("program", ["/bin/true", "/caminho/que/nao/existe"])
async def test_a_host_that_never_gets_ready_is_refused(program: str, tmp_path: Path) -> None:
    # "/bin/true -m rapa.session.host" sai sem a linha de pronto; o outro nem inicia.
    launcher = LocalSessionLauncher(
        LocalSessionSettings(python=program, start_timeout_s=10, kill_grace_s=1)
    )
    job = SessionJob(session_id=new_id(), company_id=new_id(), start_url="https://x.rapa.test/")
    with pytest.raises(SessionNotStarted):
        await launcher.start(job)
    assert not await launcher.is_alive(job.session_id)
    await launcher.stop(job.session_id)  # nada a parar
    assert launcher.running == 0
