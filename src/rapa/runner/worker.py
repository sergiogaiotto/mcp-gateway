"""Worker do executor: roda uma execução descrita em arquivos.

Uso::

    python -m rapa.runner.worker --job JOB.json --result RESULT.json \\
        --events EVENTS.jsonl --artifacts DIR [--secrets SECRETS.json] \\
        [--chromium CAMINHO] [--browser-arg=ARG ...] [--files-dir DIR]

- ``JOB.json`` é um :class:`rapa.core.contracts.RunnerJob` em JSON.
- ``SECRETS.json`` associa o nome de cada credencial a um objeto com os campos
  opcionais ``username``, ``password``, ``totp_secret`` (base32), ``pfx_path`` e
  ``pfx_password``. O worker apaga o arquivo logo depois de ler.
- ``EVENTS.jsonl`` recebe um :class:`rapa.core.events.RunEvent` por linha.
- ``RESULT.json`` recebe o :class:`rapa.runner.result.RunResult`.

O código de saída é 0 sempre que o RESULT.json foi escrito, qualquer que seja o
estado da execução; diferente de 0 só quando o próprio worker quebra. Argumentos
do navegador que começam com ``--`` vão na forma ``--browser-arg=--opcao``.
SIGTERM e SIGINT cancelam a execução entre dois passos.
"""

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from rapa.core.contracts import RunnerJob
from rapa.core.robot import CredentialKind
from rapa.runner.browser import ClientCertificate, open_browser, run_context
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import Interpreter
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    EventSink,
    JsonlEventSink,
    LocalFileProvider,
    SecretUnavailable,
)
from rapa.runner.result import RunResult

__all__ = ["build_parser", "main", "run_job", "write_result"]

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_JOB = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.runner.worker",
        description="Executa um robô do RaPA a partir de um trabalho em JSON.",
    )
    parser.add_argument("--job", required=True, type=Path, help="RunnerJob em JSON")
    parser.add_argument("--result", required=True, type=Path, help="onde escrever o RunResult")
    parser.add_argument(
        "--events", required=True, type=Path, help="arquivo JSONL que recebe os eventos"
    )
    parser.add_argument("--artifacts", required=True, type=Path, help="diretório dos artefatos")
    parser.add_argument(
        "--secrets", type=Path, default=None, help="SECRETS.json (apagado depois de lido)"
    )
    parser.add_argument("--chromium", default=None, help="caminho do Chromium")
    parser.add_argument(
        "--browser-arg",
        dest="browser_args",
        action="append",
        default=[],
        metavar="ARG",
        help="argumento extra do Chromium (use --browser-arg=--opcao)",
    )
    parser.add_argument(
        "--files-dir", type=Path, default=None, help="diretório dos arquivos de entrada"
    )
    parser.add_argument(
        "--screenshot-every-step",
        action="store_true",
        help="guardar a tela depois de todos os passos",
    )
    return parser


def write_result(path: Path, result: RunResult) -> None:
    """Escreve o resultado de forma atômica (arquivo temporário + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".result-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(result.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


async def _client_certificates(
    job: RunnerJob, secrets: DictSecretProvider
) -> list[ClientCertificate]:
    certificates: list[ClientCertificate] = []
    for credential in job.manifest.credentials:
        if credential.kind is not CredentialKind.PFX_A1:
            continue
        cert: ClientCertificate = {
            "origin": credential.origin,
            "pfxPath": await secrets.get(credential, "pfx_path"),
        }
        with contextlib.suppress(SecretUnavailable):
            cert["passphrase"] = await secrets.get(credential, "pfx_password")
        certificates.append(cert)
    return certificates


async def run_job(
    job: RunnerJob,
    *,
    secrets: DictSecretProvider | None,
    events: EventSink,
    artifacts_dir: Path,
    files_dir: Path | None = None,
    chromium: str | None = None,
    browser_args: Sequence[str] = (),
    screenshot_every_step: bool = False,
    cancel: asyncio.Event | None = None,
    secrets_error: str | None = None,
) -> RunResult:
    """Roda o trabalho num Chromium novo e devolve o resultado.

    Não levanta exceção por falhas da execução: navegador que não abre ou
    segredos inválidos também viram um resultado "falhou".
    """
    provider = secrets if secrets is not None else DictSecretProvider({})
    interpreter = Interpreter.from_job(
        job,
        secrets=provider,
        files=LocalFileProvider(files_dir),
        artifacts=DirectoryArtifactSink(artifacts_dir),
        events=events,
        cancel=cancel,
        screenshot_every_step=screenshot_every_step,
        redact_values=provider.sensitive_values(),
    )
    if secrets_error is not None:
        return await interpreter.fail_without_browser(
            ErrorCode.SEGREDO_INDISPONIVEL,
            f"Os segredos da execução são inválidos: {secrets_error}",
        )
    try:
        certificates = await _client_certificates(job, provider)
    except SecretUnavailable as exc:
        return await interpreter.fail_without_browser(
            ErrorCode.SEGREDO_INDISPONIVEL, f"O certificado A1 não está disponível: {exc}"
        )
    async with contextlib.AsyncExitStack() as stack:
        try:
            browser = await stack.enter_async_context(
                open_browser(executable_path=chromium, args=browser_args)
            )
            context = await stack.enter_async_context(
                run_context(browser, client_certificates=certificates)
            )
        except Exception as exc:
            return await interpreter.fail_without_browser(
                ErrorCode.NAVEGADOR_INDISPONIVEL,
                f"Não consegui abrir o navegador ({type(exc).__name__}).",
            )
        return await interpreter.run(context)


def _load_secrets(path: Path | None) -> tuple[DictSecretProvider | None, str | None]:
    if path is None:
        return None, None
    try:
        return DictSecretProvider.from_file(path, delete=True), None
    except FileNotFoundError:
        return None, "o arquivo de segredos não existe"
    except ValueError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, type(exc).__name__


async def _main_async(
    args: argparse.Namespace,
    job: RunnerJob,
    secrets: DictSecretProvider | None,
    secrets_error: str | None,
) -> RunResult:
    # SIGTERM e SIGINT cancelam a execução entre dois passos; o resultado sai mesmo assim.
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, cancel.set)
    with JsonlEventSink(args.events) as events:
        return await run_job(
            job,
            secrets=secrets,
            secrets_error=secrets_error,
            events=events,
            artifacts_dir=args.artifacts,
            files_dir=args.files_dir,
            chromium=args.chromium,
            browser_args=args.browser_args,
            screenshot_every_step=args.screenshot_every_step,
            cancel=cancel,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Os segredos saem do disco antes de qualquer outra coisa.
    secrets, secrets_error = _load_secrets(args.secrets)
    try:
        job = RunnerJob.model_validate_json(args.job.read_bytes())
    except (OSError, ValidationError) as exc:
        detail = type(exc).__name__ if isinstance(exc, OSError) else f"{exc.error_count()} erro(s)"
        print(f"rapa.runner.worker: trabalho inválido em {args.job} ({detail})", file=sys.stderr)
        return EXIT_BAD_JOB
    result = asyncio.run(_main_async(args, job, secrets, secrets_error))
    write_result(args.result, result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
