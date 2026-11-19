"""Worker com ``--ack-dir``: nenhum passo que grava começa sem a confirmação do plano de controle.

O plano de controle só confirma (``<seq>.ack``) o início de um passo que grava depois
de gravá-lo no banco. Sem a confirmação, o passo não acontece; com ela, a execução
que perder o executor dali em diante nunca volta para a fila (ver
:mod:`rapa.control.services.queue`).
"""

import json
from pathlib import Path

import pytest

from rapa.core.contracts import RunMode, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.runner.errors import ErrorCode
from rapa.runner.ports import ack_path, needs_ack, waits_for_ack
from rapa.runner.result import RunResult
from tests.runner.support import SECRETS, assert_no_secrets, load_jsonl
from tests.runner.test_worker import CONTEUDO, _read, _upload_manifest, _worker, _write_job

pytestmark = pytest.mark.browser


def _arguments(tmp_path: Path, chromium_path: str, *, ack_timeout: float) -> list[str]:
    files_dir = tmp_path / "entradas"
    files_dir.mkdir()
    (files_dir / "relatorio.csv").write_bytes(CONTEUDO)
    secrets_path = tmp_path / "segredos.json"
    secrets_path.write_text(json.dumps(SECRETS))
    (tmp_path / "ack").mkdir()
    return [
        "--job", str(tmp_path / "job.json"),
        "--result", str(tmp_path / "resultado.json"),
        "--events", str(tmp_path / "eventos.jsonl"),
        "--artifacts", str(tmp_path / "artefatos"),
        "--secrets", str(secrets_path),
        "--chromium", chromium_path,
        f"--browser-arg={HOST_RESOLVER_ARG}",
        "--files-dir", str(files_dir),
        "--ack-dir", str(tmp_path / "ack"),
        f"--ack-timeout={ack_timeout}",
    ]  # fmt: skip


def _outcome(tmp_path: Path) -> tuple[RunResult, list[RunEvent]]:
    result = RunResult.model_validate_json(_read(tmp_path / "resultado.json"))
    return result, load_jsonl(tmp_path / "eventos.jsonl")


def _started(events: list[RunEvent], step_id: str) -> list[RunEvent]:
    return [e for e in events if e.kind is RunEventKind.STEP_STARTED and e.step_id == step_id]


async def test_step_that_writes_never_runs_without_the_ack(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    job = _write_job(
        tmp_path / "job.json", _upload_manifest(portal_v1), {"documento": "relatorio.csv"}
    )

    code, out, err = await _worker(*_arguments(tmp_path, chromium_path, ack_timeout=1.0))

    assert code == 0, err
    result, events = _outcome(tmp_path)
    assert result.run_id == job.run_id
    # O início do passo "enviar" (que grava) não foi confirmado: o botão não foi clicado.
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.SEM_CONFIRMACAO
    assert result.failed_step_id == "enviar"
    assert "passo que grava" in (result.error_message or "")
    assert not result.irreversible_started
    assert portal_v1.state.uploads == []
    [announced] = _started(events, "enviar")
    assert announced.data["grava"] is True and announced.data["efeito"] == "write"
    assert waits_for_ack(announced) and not needs_ack(announced)
    assert not [
        e for e in events if e.kind is RunEventKind.STEP_SUCCEEDED and e.step_id == "enviar"
    ]
    # Os passos de leitura de antes não esperam confirmação nenhuma.
    assert [e.step_id for e in events if waits_for_ack(e)] == ["enviar"]
    assert_no_secrets(_read(tmp_path / "resultado.json") + _read(tmp_path / "eventos.jsonl") + out)


async def test_step_that_writes_runs_once_after_the_ack(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    _write_job(tmp_path / "job.json", _upload_manifest(portal_v1), {"documento": "relatorio.csv"})
    arguments = _arguments(tmp_path, chromium_path, ack_timeout=10.0)
    for seq in range(100):  # o plano de controle já confirmou tudo
        ack_path(tmp_path / "ack", seq).write_text("ok")

    code, _, err = await _worker(*arguments)

    assert code == 0, err
    result, events = _outcome(tmp_path)
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert portal_v1.state.uploads == [{"nome": "relatorio.csv", "tamanho": len(CONTEUDO)}]
    assert [e.step_id for e in events if waits_for_ack(e)] == ["enviar"]


async def test_validation_stops_before_the_step_that_writes_without_waiting(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    # A validação segura para antes do primeiro passo que grava: nada a confirmar, e
    # nenhuma espera (com a espera, o resultado seria "sem_confirmacao").
    _write_job(
        tmp_path / "job.json",
        _upload_manifest(portal_v1),
        {"documento": "relatorio.csv"},
        mode=RunMode.UNTIL_FIRST_WRITE,
    )

    code, _, err = await _worker(*_arguments(tmp_path, chromium_path, ack_timeout=0.5))

    assert code == 0, err
    result, events = _outcome(tmp_path)
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.error_code == ErrorCode.PAROU_ANTES_DE_GRAVAR
    assert portal_v1.state.uploads == []
    assert not [e for e in events if waits_for_ack(e)]
    assert not _started(events, "enviar")
    assert events[-2].kind is RunEventKind.RUN_STOPPED_BEFORE_WRITE
    assert events[-2].step_id == "enviar"
