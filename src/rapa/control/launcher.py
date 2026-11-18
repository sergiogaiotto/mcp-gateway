"""Lançador local: roda cada execução num processo ``python -m rapa.runner.worker``.

Serve à Fase 0 e ao desenvolvimento; em produção o mesmo papel cabe a um pod de
uso único com gVisor. Para cada tentativa, o lançador:

1. cria uma pasta só da tentativa (permissão 0700) com JOB.json e, se o robô usa
   credenciais, SECRETS.json (0600, apagado pelo worker logo depois de ler);
2. inicia o worker num grupo de processos próprio e com um ambiente mínimo, sem
   as variáveis do plano de controle (URL do banco, chaves etc.);
3. lê EVENTS.jsonl enquanto o worker roda e entrega os eventos ao banco; quando o
   aviso de passo irreversível foi gravado, cria ``<seq>.ack`` para o worker
   seguir (ver :class:`rapa.runner.ports.AckingEventSink`);
4. no fim (ou no prazo, com SIGTERM e depois SIGKILL), lê RESULT.json e o
   devolve ao despachante.

Os segredos vêm de uma :class:`SecretSource`. Em produção ela consulta o OpenBao;
aqui há uma versão estática, para desenvolvimento e testes.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import UUID

from pydantic import ValidationError

from rapa.control.services.dispatcher import WorkerNotStarted, failure_result
from rapa.control.services.queue import StaleAttempt
from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent
from rapa.runner.ports import SECRET_FIELDS, ack_path, needs_ack
from rapa.runner.result import RunResult

__all__ = [
    "EventIngestor",
    "LocalLauncherSettings",
    "LocalProcessLauncher",
    "NoSecrets",
    "SecretSource",
    "StaticSecretSource",
]

logger = logging.getLogger("rapa.control.launcher")

#: Variáveis de ambiente repassadas ao worker; todo o resto fica de fora.
_ENV_ALLOWED = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT"})
_ENV_PREFIXES = ("PLAYWRIGHT_", "XDG_", "FONTCONFIG_")
#: Tamanho máximo de uma linha de evento lida do worker.
_MAX_EVENT_LINE = 1024 * 1024


class EventIngestor(Protocol):
    """Quem grava os eventos (no RaPA, :class:`rapa.control.services.PgRunQueue`)."""

    async def ingest_events(
        self, run_id: UUID, attempt: int, events: Sequence[RunEvent | Mapping[str, Any]]
    ) -> int: ...


class SecretSource(Protocol):
    """Entrega os segredos das credenciais de uma execução, no formato do SECRETS.json.

    Devolve ``{nome_da_credencial: {campo: valor}}`` só para as credenciais que
    o manifesto declara; uma credencial ausente vira erro do passo que a usar.
    """

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]: ...


class NoSecrets:
    """Nenhum segredo: robôs sem credenciais."""

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        return {}


class StaticSecretSource:
    """Segredos fixos por empresa, para desenvolvimento e testes.

    Formato: ``{company_id: {nome_da_credencial: {campo: valor}}}``. Cada empresa
    só enxerga as próprias credenciais.
    """

    def __init__(self, secrets: Mapping[UUID, Mapping[str, Mapping[str, str]]]) -> None:
        checked: dict[UUID, dict[str, dict[str, str]]] = {}
        for company_id, credentials in secrets.items():
            per_company: dict[str, dict[str, str]] = {}
            for name, fields in credentials.items():
                unknown = sorted(set(fields) - SECRET_FIELDS)
                if unknown:
                    raise ValueError(
                        f"campos desconhecidos na credencial '{name}': {', '.join(unknown)}"
                    )
                per_company[name] = dict(fields)
            checked[company_id] = per_company
        self._secrets = checked

    @classmethod
    def from_file(cls, path: Path | str) -> Self:
        """Lê o JSON ``{"<uuid da empresa>": {"credencial": {...}}}``."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("o arquivo de segredos não é um JSON válido") from None
        if not isinstance(data, dict):
            raise ValueError("o arquivo de segredos precisa ser um objeto JSON")
        try:
            return cls({UUID(key): value for key, value in data.items()})
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError(f"arquivo de segredos inválido: {exc}") from None

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        available = self._secrets.get(job.company_id, {})
        wanted = {credential.name for credential in job.manifest.credentials}
        return {name: fields for name, fields in available.items() if name in wanted}


@dataclass(frozen=True)
class LocalLauncherSettings:
    """Configuração do lançador local."""

    #: Pasta onde ficam as pastas de trabalho de cada tentativa.
    work_dir: Path
    #: Pasta dos artefatos (telas e downloads), separada por empresa e execução.
    artifacts_dir: Path
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    files_dir: Path | None = None
    python: str = field(default_factory=lambda: sys.executable)
    #: Intervalo de leitura dos eventos do worker.
    poll_s: float = 0.1
    #: Espera do worker pela confirmação do aviso de passo irreversível.
    ack_timeout_s: float = 30.0
    #: Tempo entre o SIGTERM e o SIGKILL quando o prazo acaba ou a tentativa é cancelada.
    kill_grace_s: float = 10.0
    #: Folga além do prazo da execução antes de interromper o worker.
    deadline_grace_s: float = 15.0
    #: Mantém a pasta de trabalho depois do fim (só para depuração).
    keep_work_dir: bool = False


class _EventTail:
    """Lê as linhas completas novas de EVENTS.jsonl."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > _MAX_EVENT_LINE:
            logger.error("linha de evento grande demais em %s; descartada", self.path.name)
            self._partial = b""
        out: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                logger.error("linha de evento inválida em %s; ignorada", self.path.name)
                continue
            out.append(parsed)
        return out


def _worker_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWED or key.startswith(_ENV_PREFIXES)
    }


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_ack(directory: Path, seq: int) -> None:
    target = ack_path(directory, seq)
    temp = target.with_suffix(".tmp")
    temp.write_text("ok", encoding="utf-8")
    os.replace(temp, target)


class LocalProcessLauncher:
    """Lançador do despachante (``launch(job) -> dict``) com processos locais."""

    def __init__(
        self,
        events: EventIngestor,
        settings: LocalLauncherSettings,
        secrets: SecretSource | None = None,
    ) -> None:
        self.events = events
        self.settings = settings
        self.secrets: SecretSource = secrets or NoSecrets()

    async def __call__(self, job: RunnerJob) -> dict[str, Any]:
        return await self.launch(job)

    def _paths(self, job: RunnerJob) -> tuple[Path, Path]:
        work = self.settings.work_dir / f"{job.run_id}-{job.attempt}"
        artifacts = (
            self.settings.artifacts_dir / str(job.company_id) / str(job.run_id) / str(job.attempt)
        )
        return work, artifacts

    def _command(self, work: Path, artifacts: Path, *, with_secrets: bool) -> list[str]:
        settings = self.settings
        command = [
            settings.python,
            "-m",
            "rapa.runner.worker",
            "--job",
            str(work / "job.json"),
            "--result",
            str(work / "result.json"),
            "--events",
            str(work / "events.jsonl"),
            "--artifacts",
            str(artifacts),
            "--ack-dir",
            str(work / "ack"),
            f"--ack-timeout={settings.ack_timeout_s}",
        ]
        if with_secrets:
            command += ["--secrets", str(work / "secrets.json")]
        if settings.chromium:
            command += ["--chromium", settings.chromium]
        if settings.files_dir is not None:
            command += ["--files-dir", str(settings.files_dir)]
        command += [f"--browser-arg={arg}" for arg in settings.browser_args]
        return command

    async def launch(self, job: RunnerJob) -> dict[str, Any]:
        """Roda a tentativa num worker e devolve o RESULT.json.

        Falhas antes de o processo do worker existir (pasta, segredos, programa)
        levantam :class:`WorkerNotStarted`: nada pode ter rodado.
        """
        work, artifacts = self._paths(job)
        try:
            try:
                await asyncio.to_thread(self._prepare, job, work, artifacts)
                secrets = await self.secrets.secrets_for(job)
                if secrets:
                    payload = json.dumps({name: dict(fields) for name, fields in secrets.items()})
                    await asyncio.to_thread(_write_private, work / "secrets.json", payload)
                process = await asyncio.create_subprocess_exec(
                    *self._command(work, artifacts, with_secrets=bool(secrets)),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=_worker_env(),
                    cwd=work,
                    start_new_session=True,
                )
            except Exception as exc:
                raise WorkerNotStarted("o executor não pôde ser iniciado") from exc
            try:
                await self._follow(job, process, work)
            except BaseException:
                await self._stop(process)
                raise
            return await asyncio.to_thread(self._read_result, job, work, process.returncode)
        finally:
            await asyncio.to_thread(self._cleanup, work)

    def _prepare(self, job: RunnerJob, work: Path, artifacts: Path) -> None:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(mode=0o700)
        (work / "ack").mkdir(mode=0o700)
        artifacts.mkdir(parents=True, exist_ok=True)
        _write_private(work / "job.json", job.model_dump_json())

    async def _follow(
        self, job: RunnerJob, process: asyncio.subprocess.Process, work: Path
    ) -> None:
        tail = _EventTail(work / "events.jsonl")
        pending: list[dict[str, Any]] = []
        stderr = asyncio.create_task(self._drain_stderr(process))
        limit = (job.deadline - datetime.now(UTC)).total_seconds() + self.settings.deadline_grace_s
        loop = asyncio.get_running_loop()
        give_up = loop.time() + max(limit, 1.0)
        try:
            while True:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.settings.poll_s)
                    finished = True
                except TimeoutError:
                    finished = False
                if finished:
                    await self._forward(job, tail, work, pending, final=True)
                    break
                await self._forward(job, tail, work, pending)
                if loop.time() >= give_up:
                    logger.warning("execução %s passou do prazo; interrompendo", job.run_id)
                    await self._stop(process)
                    await self._forward(job, tail, work, pending, final=True)
                    break
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await stderr

    async def _forward(
        self,
        job: RunnerJob,
        tail: _EventTail,
        work: Path,
        pending: list[dict[str, Any]],
        *,
        final: bool = False,
    ) -> None:
        """Grava os eventos novos e confirma os avisos de passo irreversível.

        Gravar é idempotente (``seq``), então uma falha passageira do banco só
        adia a entrega para a próxima leitura; no fim, tenta mais algumas vezes.
        Uma tentativa antiga (:class:`StaleAttempt`) interrompe o lançamento.
        """
        pending.extend(await asyncio.to_thread(tail.read_new))
        if not pending:
            return
        tries = 4 if final else 1
        for attempt in range(1, tries + 1):
            try:
                await self.events.ingest_events(job.run_id, job.attempt, list(pending))
                break
            except StaleAttempt:
                raise
            except Exception as exc:
                logger.warning(
                    "falha ao gravar eventos de %s (tentativa %d): %s",
                    job.run_id,
                    attempt,
                    type(exc).__name__,
                )
                if attempt == tries:
                    return
                await asyncio.sleep(0.25 * attempt)
        delivered, pending[:] = list(pending), []
        for raw in delivered:
            try:
                event = RunEvent.model_validate(raw)
            except ValidationError:
                continue
            if needs_ack(event):
                await asyncio.to_thread(_write_ack, work / "ack", event.seq)

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        """Lê a saída de erro só para não travar o worker; registra apenas o tamanho."""
        if process.stderr is None:
            return
        total = 0
        while chunk := await process.stderr.read(65536):
            total += len(chunk)
        if total:
            logger.info("o worker escreveu %d bytes na saída de erro", total)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.settings.kill_grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _read_result(job: RunnerJob, work: Path, returncode: int | None) -> dict[str, Any]:
        path = work / "result.json"
        try:
            result = RunResult.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            logger.error("o worker de %s saiu (código %s) sem resultado", job.run_id, returncode)
            return failure_result(
                job, "executor_sem_resultado", "O executor terminou sem entregar o resultado."
            )
        except ValidationError:
            logger.error("o worker de %s entregou um resultado inválido", job.run_id)
            return failure_result(
                job, "resultado_invalido", "O executor devolveu um resultado inválido."
            )
        if result.run_id != job.run_id or result.attempt != job.attempt:
            logger.error("o resultado do worker não é desta tentativa de %s", job.run_id)
            return failure_result(
                job, "resultado_invalido", "O executor devolveu o resultado de outra tentativa."
            )
        return result.model_dump(mode="json")

    def _cleanup(self, work: Path) -> None:
        secrets = work / "secrets.json"
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            secrets.unlink()
        if not self.settings.keep_work_dir:
            shutil.rmtree(work, ignore_errors=True)
