"""Lançador local: worker de verdade, Chromium de verdade, banco simulado."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from rapa.control.launcher import (
    LocalLauncherSettings,
    LocalProcessLauncher,
    StaticSecretSource,
    _worker_env,
)
from rapa.control.services.queue import StaleAttempt
from rapa.core.contracts import RunnerJob, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal

INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
SECRETS = {
    "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
    "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
}


class RecordingIngestor:
    """Guarda os eventos como o banco guardaria (idempotente por ``seq``)."""

    def __init__(self, *, refuse_irreversible: bool = False, stale: bool = False) -> None:
        self.events: dict[int, RunEvent] = {}
        self.calls = 0
        self.refuse_irreversible = refuse_irreversible
        self.stale = stale

    async def ingest_events(
        self, run_id: UUID, attempt: int, events: Sequence[RunEvent | Mapping[str, Any]]
    ) -> int:
        self.calls += 1
        if self.stale:
            raise StaleAttempt("tentativa antiga")
        parsed = [RunEvent.model_validate(e) for e in events]
        if self.refuse_irreversible and any(e.data.get("irreversivel") for e in parsed):
            raise ConnectionError("banco fora do ar")
        new = 0
        for event in parsed:
            assert event.run_id == run_id and event.attempt == attempt
            if event.seq not in self.events:
                self.events[event.seq] = event
                new += 1
        return new


def _job(portal: RunningPortal, company_id: UUID | None = None) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=company_id or new_id(),
        attempt=1,
        manifest=emitir_protocolo(portal.base_url, portal.host),
        inputs=INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _settings(tmp_path: Path, chromium_path: str, **changes: Any) -> LocalLauncherSettings:
    return LocalLauncherSettings(
        work_dir=tmp_path / "trabalho",
        artifacts_dir=tmp_path / "artefatos",
        chromium=chromium_path,
        browser_args=(HOST_RESOLVER_ARG,),
        **changes,
    )


@pytest.mark.browser
async def test_runs_the_worker_and_forwards_events(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    job = _job(portal_v1)
    ingestor = RecordingIngestor()
    launcher = LocalProcessLauncher(
        ingestor,
        _settings(tmp_path, chromium_path),
        StaticSecretSource({job.company_id: SECRETS}),
    )

    result = await launcher(job)

    assert result["state"] == RunState.SUCCEEDED, result["error_message"]
    assert result["run_id"] == str(job.run_id) and result["attempt"] == 1
    assert len(portal_v1.state.protocolos) == 1
    assert result["outputs"] == {"protocolo": portal_v1.state.protocolos[0]["numero"]}
    seqs = sorted(ingestor.events)
    assert seqs == list(range(len(seqs)))
    kinds = [ingestor.events[s].kind for s in seqs]
    assert kinds[0] is RunEventKind.RUN_STARTED and kinds[-1] is RunEventKind.RUN_SUCCEEDED
    assert any(e.data.get("irreversivel") for e in ingestor.events.values())
    # A pasta de trabalho (com JOB.json e SECRETS.json) some; os artefatos ficam.
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()
    shots = [a for a in result["artifacts"] if a["kind"] == "screenshot"]
    assert shots and all(Path(a["path"]).is_relative_to(tmp_path / "artefatos") for a in shots)
    dump = json.dumps(result) + "".join(e.model_dump_json() for e in ingestor.events.values())
    for secret in (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert secret not in dump


@pytest.mark.browser
async def test_irreversible_step_never_runs_when_the_warning_is_not_recorded(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    job = _job(portal_v1)
    ingestor = RecordingIngestor(refuse_irreversible=True)
    launcher = LocalProcessLauncher(
        ingestor,
        _settings(tmp_path, chromium_path, ack_timeout_s=1.5),
        StaticSecretSource({job.company_id: SECRETS}),
    )

    result = await launcher(job)

    # O banco não gravou o aviso: o executor não clicou em "Emitir".
    assert result["state"] == RunState.FAILED
    assert result["error_code"] == "sem_confirmacao"
    assert result["irreversible_started"] is False
    assert portal_v1.state.protocolos == []


@pytest.mark.browser
async def test_stale_attempt_stops_the_worker(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    job = _job(portal_v1)
    launcher = LocalProcessLauncher(
        RecordingIngestor(stale=True),
        _settings(tmp_path, chromium_path, kill_grace_s=2),
        StaticSecretSource({job.company_id: SECRETS}),
    )
    with pytest.raises(StaleAttempt):
        await launcher(job)
    assert portal_v1.state.protocolos == []
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()


async def test_missing_worker_result_becomes_a_failure(
    portal_v1: RunningPortal, tmp_path: Path
) -> None:
    job = _job(portal_v1)
    launcher = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(
            work_dir=tmp_path / "trabalho", artifacts_dir=tmp_path / "artefatos", python="false"
        ),
    )
    result = await launcher(job)
    # Robô com passo irreversível, sem resultado: ninguém sabe se emitiu.
    assert result["state"] == RunState.INDETERMINATE
    assert result["error_code"] == "executor_sem_resultado"


def test_worker_environment_leaves_control_plane_variables_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAPA_DATABASE_URL", "postgresql://rapa:senha@banco/rapa")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "chave")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    env = _worker_env()
    assert "RAPA_DATABASE_URL" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw-browsers"


async def test_static_secrets_are_scoped_by_company_and_credential(
    portal_v1: RunningPortal, tmp_path: Path
) -> None:
    company_a, company_b = new_id(), new_id()
    source = StaticSecretSource(
        {company_a: {**SECRETS, "outra": {"password": "x"}}, company_b: {"portal": {}}}
    )
    got = await source.secrets_for(_job(portal_v1, company_a))
    assert sorted(got) == ["portal", "portal_otp"]  # só as declaradas no manifesto
    assert await source.secrets_for(_job(portal_v1, new_id())) == {}

    path = tmp_path / "segredos.json"
    path.write_text(json.dumps({str(company_a): SECRETS}))
    loaded = await StaticSecretSource.from_file(path).secrets_for(_job(portal_v1, company_a))
    assert loaded["portal"]["username"] == DEFAULT_USER
    path.write_text(json.dumps({"nao-e-uuid": {}}))
    with pytest.raises(ValueError, match="inválido"):
        StaticSecretSource.from_file(path)
    with pytest.raises(ValueError, match="desconhecidos"):
        StaticSecretSource({company_a: {"portal": {"pin": "1"}}})
