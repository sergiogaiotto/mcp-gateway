"""O lançador e a fonte de segredos: o certificado A1 vira um arquivo 0600 só durante a
tentativa, e um segredo que falta termina a execução sem lançar o executor."""

import base64
import json
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rapa.control.launcher import (
    LocalLauncherSettings,
    LocalProcessLauncher,
    SecretsUnavailable,
    StaticSecretSource,
    materialize_certificates,
)
from rapa.core.contracts import RunnerJob, RunState
from rapa.core.ids import new_id
from tests.control.credentials.support import a1_protocolo
from tests.control.launcher.test_launcher import RecordingIngestor

PFX = b"\x30\x82\x01\x00PFX de teste"

#: Um "worker" que guarda o que recebeu (o SECRETS.json e o arquivo do certificado).
_RECORDING_WORKER = """#!{python}
import json, os, stat, sys
args = sys.argv[1:]
seen = {{"started": True}}
if "--secrets" in args:
    secrets = json.load(open(args[args.index("--secrets") + 1]))
    seen["secrets"] = secrets
    path = secrets.get("certificado", {{}}).get("pfx_path")
    if path:
        info = os.stat(path)
        seen["pfx"] = {{
            "path": path,
            "mode": stat.S_IMODE(info.st_mode),
            "hex": open(path, "rb").read().hex(),
        }}
open({capture!r}, "w").write(json.dumps(seen))
job = json.load(open(args[args.index("--job") + 1]))
result = {{"run_id": job["run_id"], "attempt": job["attempt"], "state": "succeeded"}}
open(args[args.index("--result") + 1], "w").write(json.dumps(result))
"""


class _Source:
    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.secrets = secrets

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        if self.secrets is None:
            raise SecretsUnavailable("a credencial 'certificado' não está registrada na área")
        return self.secrets


def _job() -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=a1_protocolo(),
        inputs={"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _launcher(tmp_path: Path, source: _Source, **changes: Any) -> tuple[LocalProcessLauncher, Path]:
    capture = tmp_path / "visto.json"
    script = tmp_path / "worker.py"
    script.write_text(_RECORDING_WORKER.format(python=sys.executable, capture=str(capture)))
    script.chmod(0o755)
    settings = LocalLauncherSettings(
        work_dir=tmp_path / "trabalho",
        artifacts_dir=tmp_path / "artefatos",
        python=str(script),
        **changes,
    )
    return LocalProcessLauncher(RecordingIngestor(), settings, source), capture  # type: ignore[arg-type]


@pytest.mark.parametrize("keep_work_dir", [False, True])
async def test_the_a1_certificate_is_a_0600_file_only_during_the_attempt(
    tmp_path: Path, keep_work_dir: bool
) -> None:
    encoded = base64.b64encode(PFX).decode()
    source = _Source(
        {
            "portal": {"username": "ana", "password": "senha"},
            "certificado": {"pfx_base64": encoded, "pfx_password": "senha-do-pfx"},
        }
    )
    launcher, capture = _launcher(tmp_path, source, keep_work_dir=keep_work_dir)
    result = await launcher(_job())
    assert result["state"] == RunState.SUCCEEDED
    seen = json.loads(capture.read_text())
    # O worker recebeu o caminho de um arquivo 0600 com o PFX, nunca o base64.
    certificate = seen["secrets"]["certificado"]
    assert set(certificate) == {"pfx_path", "pfx_password"}
    assert seen["pfx"]["mode"] == 0o600 and bytes.fromhex(seen["pfx"]["hex"]) == PFX
    assert Path(certificate["pfx_path"]).name == "certificado-1.pfx"  # sem o nome da credencial
    # Depois da tentativa, o arquivo sumiu (também com a pasta guardada para depuração).
    assert _leftovers(tmp_path, seen["pfx"]["path"]) == []


def _leftovers(tmp_path: Path, certificate: str) -> list[Path]:
    work = tmp_path / "trabalho"
    found = [*work.rglob("*.pfx"), *work.rglob("secrets.json")]
    return [*found, Path(certificate)] if Path(certificate).exists() else found


async def test_a_missing_secret_ends_the_run_without_starting_the_worker(tmp_path: Path) -> None:
    launcher, capture = _launcher(tmp_path, _Source(None))
    job = _job()
    result = await launcher(job)
    assert result["state"] == RunState.FAILED  # nada rodou: nem indeterminado
    assert result["error_code"] == "segredo_indisponivel"
    assert "a credencial 'certificado' não está registrada" in result["error_message"]
    assert not capture.exists()
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()


async def test_an_invalid_pfx_is_unavailable_without_its_value(tmp_path: Path) -> None:
    bad = "não-é-base64!!" + "A" * 20
    launcher, capture = _launcher(tmp_path, _Source({"certificado": {"pfx_base64": bad}}))
    result = await launcher(_job())
    assert result["error_code"] == "segredo_indisponivel"
    assert bad not in result["error_message"] and "base64" in result["error_message"]
    assert not capture.exists()
    with pytest.raises(SecretsUnavailable, match="tamanho"):
        materialize_certificates({"c": {"pfx_base64": ""}}, tmp_path)


def test_the_dev_file_may_carry_the_certificate_in_base64(tmp_path: Path) -> None:
    path = tmp_path / "segredos.json"
    company = new_id()
    path.write_text(
        json.dumps({str(company): {"certificado": {"pfx_base64": base64.b64encode(PFX).decode()}}})
    )
    StaticSecretSource.from_file(path)
    path.write_text(json.dumps({str(company): {"certificado": {"pfx_bytes": "x"}}}))
    with pytest.raises(ValueError, match="pfx_bytes"):
        StaticSecretSource.from_file(path)
    assert stat.S_IMODE(path.stat().st_mode)  # o arquivo de teste continua legível
