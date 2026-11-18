"""Testes do worker de linha de comando (interface A)."""

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rapa.core.contracts import RunMode, RunnerJob, RunState
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.robots import login_steps, portal_credentials
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.runner import browser as runner_browser
from rapa.runner.errors import ErrorCode
from rapa.runner.result import ArtifactKind, RunResult
from rapa.runner.worker import build_parser
from tests.runner.support import SECRETS, assert_no_secrets, assert_seq, file_bytes, load_jsonl

CONTEUDO = b"linha 1;valor\nlinha 2;outro valor\n"


def _upload_manifest(portal: RunningPortal) -> Manifest:
    base = portal.base_url
    return Manifest.model_validate(
        {
            "name": "enviar_documento",
            "title": "Enviar documento",
            "description": "Entra no portal e envia um arquivo.",
            "allowed_domains": [portal.host],
            "credentials": portal_credentials(base),
            "inputs": [{"name": "documento", "title": "Documento", "type": "file"}],
            "steps": [
                *login_steps(base),
                {
                    "kind": "navigate",
                    "id": "abrir_envio",
                    "title": "Abrir o envio",
                    "url": f"{base}/arquivos",
                },
                {
                    "kind": "upload",
                    "id": "escolher_arquivo",
                    "title": "Escolher o arquivo",
                    "target": {
                        "intent": "campo Arquivo",
                        "locators": [{"by": "label", "value": "Arquivo"}],
                    },
                    "file_input": "documento",
                },
                {
                    "kind": "click",
                    "id": "enviar",
                    "title": "Enviar",
                    "effect": "write",
                    "target": {
                        "intent": "botão Enviar",
                        "locators": [{"by": "role", "value": "button", "name": "Enviar"}],
                    },
                    "expect_navigation": True,
                },
                {
                    "kind": "extract_text",
                    "id": "ler_confirmacao",
                    "title": "Ler a confirmação",
                    "target": {
                        "intent": "confirmação",
                        "locators": [{"by": "test_id", "value": "arquivo-recebido"}],
                    },
                    "into": "recebido",
                },
            ],
            "outputs": [
                {
                    "name": "recebido",
                    "title": "Confirmação",
                    "type": "string",
                    "source": "vars.recebido",
                }
            ],
        }
    )


def _write_job(path: Path, manifest: Manifest, inputs: dict[str, Any], **extra: Any) -> RunnerJob:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=manifest,
        inputs=inputs,
        mode=extra.pop("mode", RunMode.FULL),
        deadline=extra.pop("deadline", datetime.now(UTC) + timedelta(minutes=5)),
    )
    path.write_text(job.model_dump_json())
    return job


async def _worker(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "rapa.runner.worker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    assert process.returncode is not None
    return process.returncode, out.decode(), err.decode()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _has_step_succeeded(path: Path) -> bool:
    return path.exists() and '"step_succeeded"' in _read(path)


@pytest.mark.browser
async def test_worker_runs_job_end_to_end_with_files(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    files_dir = tmp_path / "entradas"
    files_dir.mkdir()
    (files_dir / "relatorio.csv").write_bytes(CONTEUDO)
    job = _write_job(
        tmp_path / "job.json", _upload_manifest(portal_v1), {"documento": "relatorio.csv"}
    )
    secrets_path = tmp_path / "segredos.json"
    secrets_path.write_text(json.dumps(SECRETS))
    result_path = tmp_path / "saida" / "resultado.json"
    events_path = tmp_path / "eventos.jsonl"
    artifacts_dir = tmp_path / "artefatos"

    code, out, err = await _worker(
        "--job", str(tmp_path / "job.json"),
        "--result", str(result_path),
        "--events", str(events_path),
        "--artifacts", str(artifacts_dir),
        "--secrets", str(secrets_path),
        "--chromium", chromium_path,
        f"--browser-arg={HOST_RESOLVER_ARG}",
        "--files-dir", str(files_dir),
    )  # fmt: skip

    assert code == 0, err
    assert not secrets_path.exists()
    result = RunResult.model_validate_json(_read(result_path))
    assert result.run_id == job.run_id
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.outputs == {
        "recebido": f"Arquivo recebido: relatorio.csv ({len(CONTEUDO)} bytes)"
    }
    assert portal_v1.state.uploads == [{"nome": "relatorio.csv", "tamanho": len(CONTEUDO)}]

    events = load_jsonl(events_path)
    assert_seq(events)
    assert {e.run_id for e in events} == {job.run_id}
    assert events[-1].kind is RunEventKind.RUN_SUCCEEDED
    upload = next(
        e
        for e in events
        if e.kind is RunEventKind.STEP_SUCCEEDED and e.step_id == "escolher_arquivo"
    )
    assert upload.data["tamanho"] == len(CONTEUDO)

    # Tela depois do passo que grava, com hash conferido.
    shots = [a for a in result.artifacts if a.kind == ArtifactKind.SCREENSHOT]
    assert [a.name for a in shots] == ["tela-001-enviar-depois.png"]
    shot = Path(shots[0].path)
    assert shot.parent == artifacts_dir.resolve()
    assert hashlib.sha256(file_bytes(shot)).hexdigest() == shots[0].sha256

    names = "\n".join(p.name for p in artifacts_dir.iterdir())
    assert_no_secrets("\n".join([_read(result_path), _read(events_path), out, err, names]))


@pytest.mark.browser
async def test_worker_sigterm_cancels_between_steps(
    chromium_path: str, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    manifest = Manifest.model_validate(
        {
            "name": "abrir_muitas_vezes",
            "title": "Abrir muitas vezes",
            "description": "Abre a mesma página para cada item.",
            "allowed_domains": [portal_v1.host],
            "inputs": [
                {
                    "name": "itens",
                    "title": "Itens",
                    "type": "list",
                    "item_fields": [{"name": "n", "title": "N", "type": "string"}],
                }
            ],
            "steps": [
                {
                    "kind": "for_each",
                    "id": "cada",
                    "title": "Para cada item",
                    "items": "inputs.itens",
                    "steps": [
                        {
                            "kind": "navigate",
                            "id": "abrir",
                            "title": "Abrir a página",
                            "url": f"{portal_v1.base_url}/?n={{{{ item.n }}}}",
                        }
                    ],
                }
            ],
        }
    )
    job = _write_job(
        tmp_path / "job.json", manifest, {"itens": [{"n": str(i)} for i in range(500)]}
    )
    result_path = tmp_path / "resultado.json"
    events_path = tmp_path / "eventos.jsonl"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "rapa.runner.worker",
        "--job", str(tmp_path / "job.json"),
        "--result", str(result_path),
        "--events", str(events_path),
        "--artifacts", str(tmp_path / "artefatos"),
        "--chromium", chromium_path,
        f"--browser-arg={HOST_RESOLVER_ARG}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    for _ in range(600):  # até 30 s para o primeiro passo terminar
        if _has_step_succeeded(events_path):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("o worker não concluiu nenhum passo a tempo")
    process.terminate()
    _, err = await process.communicate()

    assert process.returncode == 0, err.decode()
    result = RunResult.model_validate_json(_read(result_path))
    assert result.run_id == job.run_id
    assert result.state is RunState.CANCELLED
    assert result.error_code == ErrorCode.CANCELADA
    assert 0 < result.steps_done < 500
    assert load_jsonl(events_path)[-1].kind is RunEventKind.RUN_FAILED


async def test_worker_with_invalid_secrets_writes_failed_result(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(
        {
            "name": "robo_minimo",
            "title": "Robô mínimo",
            "description": "Só abre uma página.",
            "allowed_domains": ["portal.rapa.test"],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir",
                    "title": "Abrir",
                    "url": "http://portal.rapa.test/",
                }
            ],
        }
    )
    job = _write_job(tmp_path / "job.json", manifest, {})
    secrets_path = tmp_path / "segredos.json"
    secrets_path.write_text('{"portal": {"password": "Senha-Forte-123", "extra": "x"}}')
    result_path = tmp_path / "resultado.json"
    events_path = tmp_path / "eventos.jsonl"

    code, out, err = await _worker(
        "--job", str(tmp_path / "job.json"),
        "--result", str(result_path),
        "--events", str(events_path),
        "--artifacts", str(tmp_path / "artefatos"),
        "--secrets", str(secrets_path),
    )  # fmt: skip

    assert code == 0, err
    assert not secrets_path.exists()
    result = RunResult.model_validate_json(_read(result_path))
    assert (result.run_id, result.state) == (job.run_id, RunState.FAILED)
    assert result.error_code == ErrorCode.SEGREDO_INDISPONIVEL
    events = load_jsonl(events_path)
    assert [e.kind for e in events] == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]
    assert "Senha-Forte-123" not in _read(result_path) + _read(events_path) + out + err


async def test_worker_with_invalid_job_exits_non_zero(tmp_path: Path) -> None:
    (tmp_path / "job.json").write_text('{"run_id": "nao-e-uuid"}')
    secrets_path = tmp_path / "segredos.json"
    secrets_path.write_text(json.dumps(SECRETS))
    result_path = tmp_path / "resultado.json"
    code, _, err = await _worker(
        "--job", str(tmp_path / "job.json"),
        "--result", str(result_path),
        "--events", str(tmp_path / "eventos.jsonl"),
        "--artifacts", str(tmp_path / "artefatos"),
        "--secrets", str(secrets_path),
    )  # fmt: skip
    assert code != 0
    assert not result_path.exists()
    assert not secrets_path.exists()
    assert "trabalho inválido" in err
    assert_no_secrets(err)


def test_parser_accepts_repeated_browser_args() -> None:
    args = build_parser().parse_args(
        [
            "--job", "j.json", "--result", "r.json", "--events", "e.jsonl", "--artifacts", "a",
            f"--browser-arg={HOST_RESOLVER_ARG}", "--browser-arg=--lang=pt-BR",
        ]
    )  # fmt: skip
    assert args.browser_args == [HOST_RESOLVER_ARG, "--lang=pt-BR"]
    assert args.secrets is None and args.chromium is None and args.files_dir is None


def test_chromium_path_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RAPA_CHROMIUM_PATH", "/opt/chromium-da-variavel")
    assert runner_browser.resolve_executable("/explicito") == "/explicito"
    assert runner_browser.resolve_executable() == "/opt/chromium-da-variavel"
    monkeypatch.delenv("RAPA_CHROMIUM_PATH")
    fake = tmp_path / "chromium"
    monkeypatch.setattr(runner_browser, "DEFAULT_CHROMIUM", str(fake))
    assert runner_browser.resolve_executable() is None
    fake.write_text("")
    assert runner_browser.resolve_executable() == str(fake)
