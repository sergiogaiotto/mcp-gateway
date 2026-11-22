"""Os auxiliares do CI do deploy (deploy/ci e deploy/kind), contra o worker e o serviço de verdade.

O job ``imagens`` roda o worker dentro da imagem do executor com o trabalho de
``deploy/ci/trabalho_de_teste.py``; aqui, o mesmo trabalho roda no worker local, contra o
portal de teste, e o ``conferir`` aceita o resultado certo e recusa os errados.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rapa.core.contracts import RunState
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.runner.result import RunResult
from tests.deploy.support import REPO


def load(relative: str) -> ModuleType:
    path = REPO / relative
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def job_tool() -> ModuleType:
    return load("deploy/ci/trabalho_de_teste.py")


@pytest.mark.browser
def test_test_job_runs_in_the_real_worker(
    job_tool: ModuleType, chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    folder = tmp_path / "trabalho"
    assert job_tool.main(["preparar", "--portal", portal_v1.base_url, "--pasta", str(folder)]) == 0
    secrets = json.loads((folder / "secrets.json").read_text(encoding="utf-8"))
    assert set(secrets) == {"portal", "portal_otp"}
    # A mesma linha de comando do job imagens (lá, dentro da imagem do executor).
    process = subprocess.run(  # noqa: S603 - o worker local, com argumentos fixos
        [
            sys.executable,
            "-m",
            "rapa.runner.worker",
            "--job", str(folder / "job.json"),
            "--secrets", str(folder / "secrets.json"),
            "--result", str(folder / "result.json"),
            "--events", str(folder / "events.jsonl"),
            "--artifacts", str(folder / "artefatos"),
            "--chromium", chromium_path,
            f"--browser-arg={HOST_RESOLVER_ARG}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )  # fmt: skip
    assert process.returncode == 0, process.stderr
    assert job_tool.main(["conferir", "--pasta", str(folder)]) == 0
    result = job_tool.check(folder)
    assert result.state is RunState.SUCCEEDED
    assert len(result.artifacts) >= 1


def _fake_result(job_tool: ModuleType, folder: Path, **changes: object) -> None:
    job = job_tool.prepare("http://portal-v1.rapa.test:8080", folder)
    data = {"run_id": str(job.run_id), "attempt": 1, "state": "succeeded", "artifacts": []}
    data.update(changes)
    (folder / "result.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "events.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"state": "failed", "error_code": "acao_falhou"}, "terminou failed"),
        ({}, "sem os arquivos baixados"),
        ({"run_id": "019a0000-0000-7000-8000-000000000000"}, "outra execução"),
    ],
)
def test_check_refuses_a_wrong_result(
    job_tool: ModuleType, tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    _fake_result(job_tool, tmp_path, **changes)
    (tmp_path / "secrets.json").unlink()
    with pytest.raises(AssertionError, match=message):
        job_tool.check(tmp_path)
    assert job_tool.main(["conferir", "--pasta", str(tmp_path)]) == 1


def test_check_refuses_secrets_left_on_disk(job_tool: ModuleType, tmp_path: Path) -> None:
    artifact = {"kind": "download", "name": "nota.pdf", "path": "nota.pdf", "size": 4}
    artifact["sha256"] = "0" * 64
    _fake_result(job_tool, tmp_path, artifacts=[artifact])
    RunResult.model_validate_json((tmp_path / "result.json").read_text(encoding="utf-8"))
    (tmp_path / "artefatos" / "nota.pdf").write_bytes(b"%PDF")
    # O worker apaga o arquivo de segredos logo depois de ler; se ficou, o conferir recusa.
    with pytest.raises(AssertionError, match="segredos"):
        job_tool.check(tmp_path)
    (tmp_path / "secrets.json").unlink()
    assert job_tool.check(tmp_path).state is RunState.SUCCEEDED
