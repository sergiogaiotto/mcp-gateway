"""Lançador local: worker de verdade, Chromium de verdade, banco simulado."""

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from rapa.control.artifacts import LocalArtifactStore
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
from rapa.runner.ports import FileUnavailable, LocalFileProvider
from tests.runner.test_worker import _upload_manifest

INPUTS = {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
SECRETS = {
    "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
    "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
}


class RecordingIngestor:
    """Guarda os eventos como o banco guardaria (idempotente por ``seq``)."""

    def __init__(
        self, *, refuse_irreversible: bool = False, refuse_writes: bool = False, stale: bool = False
    ) -> None:
        self.events: dict[int, RunEvent] = {}
        self.calls = 0
        self.refuse_irreversible = refuse_irreversible
        self.refuse_writes = refuse_writes
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
        if self.refuse_writes and any(e.data.get("grava") for e in parsed):
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
async def test_step_that_writes_never_runs_when_its_start_is_not_recorded(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    files_dir = tmp_path / "arquivos"
    company = new_id()
    _company_files(files_dir, company, "planilha.csv", b"linha 1\n")
    ingestor = RecordingIngestor(refuse_writes=True)
    launcher = LocalProcessLauncher(
        ingestor,
        _settings(tmp_path, chromium_path, files_dir=files_dir, ack_timeout_s=1.5),
        StaticSecretSource({company: SECRETS}),
    )

    result = await launcher(_upload_job(portal_v1, company, "planilha.csv"))

    # O banco não gravou o início do passo "enviar" (que grava): sem a confirmação, o
    # executor não clicou, e a execução pode voltar para a fila sem gravar duas vezes.
    assert result["state"] == RunState.FAILED
    assert result["error_code"] == "sem_confirmacao"
    assert result["failed_step_id"] == "enviar"
    assert portal_v1.state.uploads == []
    assert not any(e.data.get("grava") for e in ingestor.events.values())
    assert not (tmp_path / "trabalho").exists() or not any((tmp_path / "trabalho").iterdir())


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


def _upload_job(portal: RunningPortal, company_id: UUID, file_id: str) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=company_id,
        attempt=1,
        manifest=_upload_manifest(portal),
        inputs={"documento": file_id},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _company_files(files_dir: Path, company_id: UUID, name: str, content: bytes) -> Path:
    folder = files_dir / str(company_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return folder / name


async def test_files_dir_given_to_the_worker_is_the_company_folder(
    portal_v1: RunningPortal, tmp_path: Path
) -> None:
    files_dir = tmp_path / "arquivos"
    company_a, company_b = new_id(), new_id()
    file_b = _company_files(files_dir, company_b, "notas_empresa_b.csv", b"dados da empresa B")
    (files_dir / "solto_na_raiz.csv").write_bytes(b"sem empresa")
    (files_dir / str(company_a)).mkdir()
    (files_dir / str(company_a) / "atalho.csv").symlink_to(file_b)
    launcher = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(
            work_dir=tmp_path / "trabalho",
            artifacts_dir=tmp_path / "artefatos",
            files_dir=files_dir,
        ),
    )

    def files_dir_of(job: RunnerJob) -> Path:
        work, artifacts = launcher._paths(job)
        command = launcher._command(job, work, artifacts, with_secrets=False)
        return Path(command[command.index("--files-dir") + 1])

    job_a, job_b = _upload_job(portal_v1, company_a, "x"), _upload_job(portal_v1, company_b, "x")
    assert files_dir_of(job_a) == files_dir / str(company_a)
    assert files_dir_of(job_b) == files_dir / str(company_b)

    # A execução da empresa A não alcança o arquivo da B: nem pelo nome, nem por atalho,
    # nem os arquivos soltos na raiz (que não são de empresa nenhuma).
    provider_a = LocalFileProvider(files_dir_of(job_a))
    for file_id in ("notas_empresa_b.csv", "atalho.csv", "solto_na_raiz.csv"):
        with pytest.raises(FileUnavailable, match="não foi encontrado"):
            await provider_a.path_for(file_id)
    assert await LocalFileProvider(files_dir_of(job_b)).path_for("notas_empresa_b.csv") == (
        file_b.resolve()
    )
    # Empresa sem pasta: o arquivo simplesmente não existe (a pasta não é criada).
    other = _upload_job(portal_v1, new_id(), "notas_empresa_b.csv")
    with pytest.raises(FileUnavailable, match="não foi encontrado"):
        await LocalFileProvider(files_dir_of(other)).path_for("notas_empresa_b.csv")
    assert not files_dir_of(other).exists()


@pytest.mark.browser
async def test_run_of_one_company_cannot_upload_another_company_file(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    files_dir = tmp_path / "arquivos"
    company_a, company_b = new_id(), new_id()
    own = b"planilha da empresa A\n"
    _company_files(files_dir, company_a, "planilha_a.csv", own)
    secret_b = b"dados sigilosos da empresa B"
    _company_files(files_dir, company_b, "notas_empresa_b.csv", secret_b)
    # No leiaute antigo (uma pasta só para todas as empresas), o arquivo da B ficava
    # na raiz, onde a execução de qualquer empresa o alcançava pelo nome.
    (files_dir / "notas_empresa_b.csv").write_bytes(secret_b)
    launcher = LocalProcessLauncher(
        RecordingIngestor(),
        _settings(tmp_path, chromium_path, files_dir=files_dir),
        StaticSecretSource({company_a: SECRETS, company_b: SECRETS}),
    )

    stolen = await launcher(_upload_job(portal_v1, company_a, "notas_empresa_b.csv"))

    assert stolen["state"] == RunState.FAILED
    assert stolen["error_code"] == "arquivo_indisponivel"
    assert portal_v1.state.uploads == []

    allowed = await launcher(_upload_job(portal_v1, company_a, "planilha_a.csv"))

    assert allowed["state"] == RunState.SUCCEEDED, allowed["error_message"]
    assert portal_v1.state.uploads == [{"nome": "planilha_a.csv", "tamanho": len(own)}]


def test_worker_environment_leaves_control_plane_variables_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAPA_DATABASE_URL", "postgresql://rapa:senha@banco/rapa")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "chave")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    # O Playwright tiraria o loopback do proxy da guarda: o Chromium abriria a máquina direto.
    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")
    env = _worker_env()
    assert "RAPA_DATABASE_URL" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK" not in env
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


@pytest.mark.parametrize(
    ("content", "message"),
    [
        # Senha escrita direto no lugar do objeto da credencial (erro comum).
        ({"portal": "Tr0ub4dor&3"}, "a credencial 'portal' precisa ser um objeto"),
        ({"portal": ["Tr0ub4dor&3"]}, "a credencial 'portal' precisa ser um objeto"),
        ({"portal": {"password": 12345678}}, "o campo 'password' da credencial 'portal'"),
        ({"portal": {"password": None}}, "o campo 'password' da credencial 'portal'"),
        ("Tr0ub4dor&3", "precisam ser um objeto"),
        (["Tr0ub4dor&3"], "precisam ser um objeto"),
    ],
)
def test_static_secrets_errors_never_show_secret_values(
    tmp_path: Path, content: Any, message: str
) -> None:
    company = new_id()
    path = tmp_path / "segredos.json"
    path.write_text(json.dumps({str(company): content}))
    with pytest.raises(ValueError, match=message) as from_file:
        StaticSecretSource.from_file(path)
    with pytest.raises(ValueError, match=message) as direct:
        StaticSecretSource({company: content})
    for error in (from_file.value, direct.value):
        text = str(error)
        assert "Tr0ub4dor" not in text and "12345678" not in text
        # Nem os caracteres soltos da senha (o set() de uma string os espalhava).
        assert "&" not in text and ", 0, 3" not in text
    # O erro do arquivo não carrega a exceção original (que poderia ter o valor).
    assert from_file.value.__cause__ is None and from_file.value.__suppress_context__


def test_static_secrets_file_errors_do_not_echo_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = new_id()
    path = tmp_path / "segredos.json"
    path.write_text(json.dumps({str(company): {"portal": {"password": "Tr0ub4dor&3"}}}))

    def broken(self: StaticSecretSource, secrets: Any) -> None:
        raise ValueError("falha inesperada com Tr0ub4dor&3")

    monkeypatch.setattr(StaticSecretSource, "__init__", broken)
    with pytest.raises(ValueError) as caught:
        StaticSecretSource.from_file(path)
    assert "Tr0ub4dor" not in str(caught.value)
    assert str(caught.value).startswith("arquivo de segredos inválido")


_STUBBORN_WORKER = """#!{python}
import json, os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
args = sys.argv[1:]
events = args[args.index("--events") + 1]
job = json.load(open(args[args.index("--job") + 1]))
open({pid_file!r}, "w").write(str(os.getpid()))
with open(events, "a") as out:
    out.write(json.dumps({{
        "run_id": job["run_id"], "attempt": job["attempt"], "seq": job.get("first_seq", 0),
        "at": "2026-11-19T00:00:00Z", "kind": "run_started", "step_id": None, "data": {{}},
    }}) + "\\n")
time.sleep(300)
"""


def _stubborn_worker(tmp_path: Path) -> tuple[Path, Path]:
    """Um "worker" que ignora o SIGTERM e não sai sozinho (o pior caso)."""
    pid_file = tmp_path / "worker.pid"
    script = tmp_path / "teimoso.py"
    script.write_text(_STUBBORN_WORKER.format(python=sys.executable, pid_file=str(pid_file)))
    script.chmod(0o755)
    return script, pid_file


def _gone(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("how", ["cancelada", "tentativa_antiga"])
async def test_cancelled_or_stale_launch_kills_the_worker_promptly(
    portal_v1: RunningPortal, tmp_path: Path, how: str
) -> None:
    script, pid_file = _stubborn_worker(tmp_path)
    launcher = LocalProcessLauncher(
        RecordingIngestor(stale=how == "tentativa_antiga"),
        LocalLauncherSettings(
            work_dir=tmp_path / "trabalho",
            artifacts_dir=tmp_path / "artefatos",
            python=str(script),
            kill_grace_s=1,
        ),
    )
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(launcher(_job(portal_v1)))
    for _ in range(200):  # até 10 s para o worker subir
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("o worker não subiu")
    started = loop.time()
    if how == "cancelada":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(StaleAttempt):
            await task
    # O worker morre no prazo de tolerância (SIGTERM ignorado, então SIGKILL), sem
    # esperar a saída de erro dele fechar: nada de worker antigo gravando em paralelo.
    assert loop.time() - started < 5
    assert _gone(pid_file)


def test_video_on_failure_goes_to_the_worker_and_into_the_end_budget(tmp_path: Path) -> None:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs=INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    plain = LocalProcessLauncher(RecordingIngestor(), _settings(tmp_path, "/bin/chromium"))
    filmed = LocalProcessLauncher(
        RecordingIngestor(), _settings(tmp_path, "/bin/chromium", video_on_failure=True)
    )
    work, artifacts = filmed._paths(job)
    assert "--video-dir" not in plain._command(job, work, artifacts, with_secrets=False)
    command = filmed._command(job, work, artifacts, with_secrets=False)
    # A pasta do vídeo fica dentro da pasta da tentativa, que some no fim.
    assert command[command.index("--video-dir") + 1] == str(work / "video")
    assert plain.grace_s == 15.0 and filmed.grace_s == 25.0


@pytest.mark.browser
async def test_relative_folders_are_read_from_the_server_not_the_worker(
    portal_v1: RunningPortal,
    chromium_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pastas relativas valem a partir do servidor, não da pasta de trabalho do worker.

    O worker roda com a pasta da tentativa como diretório atual; uma pasta de artefatos
    relativa lida a partir dela levaria as telas para dentro da pasta de trabalho (que
    some no fim), e o store as recusaria.
    """
    monkeypatch.chdir(tmp_path)
    job = _job(portal_v1)
    launcher = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(
            work_dir=Path("trabalho"),
            artifacts_dir=Path("dados") / "artefatos",
            files_dir=Path("dados") / "arquivos",
            chromium=chromium_path,
            browser_args=(HOST_RESOLVER_ARG,),
        ),
        StaticSecretSource({job.company_id: SECRETS}),
    )
    work, artifacts = launcher._paths(job)
    command = launcher._command(job, work, artifacts, with_secrets=False)
    for flag in ("--job", "--artifacts", "--files-dir"):
        assert Path(command[command.index(flag) + 1]).is_absolute(), flag

    result = await launcher(job)

    assert result["state"] == RunState.SUCCEEDED, result["error_message"]
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()
    store = LocalArtifactStore(Path("dados") / "artefatos")
    shots = [a for a in result["artifacts"] if a["kind"] == "screenshot"]
    assert shots
    for shot in shots:
        info = await store.put(
            job.company_id, job.run_id, job.attempt, shot["name"], shot["kind"], Path(shot["path"])
        )
        assert info.size > 0
    folder = tmp_path / "dados" / "artefatos" / str(job.company_id) / str(job.run_id) / "1"
    assert {shot["name"] for shot in shots} <= {p.name for p in folder.iterdir()}
