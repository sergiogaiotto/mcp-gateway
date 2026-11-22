"""Lançador local: roda cada execução num processo ``python -m rapa.runner.worker``.

Serve à Fase 0 e ao desenvolvimento; em produção o mesmo papel cabe a um pod de
uso único com gVisor. Para cada tentativa, o lançador:

1. cria uma pasta só da tentativa (permissão 0700) com JOB.json e, se o robô usa
   credenciais, SECRETS.json (0600, apagado pelo worker logo depois de ler);
2. inicia o worker num grupo de processos próprio e com um ambiente mínimo, sem
   as variáveis do plano de controle (URL do banco, chaves etc.);
3. lê EVENTS.jsonl enquanto o worker roda e entrega os eventos ao banco; quando o
   início de um passo que grava (ou o aviso de passo irreversível) foi gravado,
   cria ``<seq>.ack`` para o worker seguir (ver
   :class:`rapa.runner.ports.AckingEventSink`). Gravado esse início, a execução
   nunca mais volta para a fila (ver :mod:`rapa.control.services.queue`);
4. no fim (ou no prazo, com SIGTERM e depois SIGKILL), lê RESULT.json e o
   devolve ao despachante.

Os artefatos ficam em ``<artifacts_dir>/<empresa>/<execução>/<tentativa>``
(:func:`rapa.control.artifacts.attempt_dir`, a disposição do
:class:`rapa.control.artifacts.LocalArtifactStore`); o despachante os registra pelo
store no fim. Com ``video_on_failure``, o worker filma a execução numa pasta da
tentativa (``<trabalho>/video``, apagada no fim) e guarda o vídeo só se ela não der
certo (ver :mod:`rapa.runner.video`); o limite para fechar o vídeo
(``video_close_s``) entra na folga antes de interromper o worker.

Os arquivos de entrada também são separados por empresa: o worker só recebe a
pasta ``<files_dir>/<uuid da empresa>``, com a empresa tirada da tarefa (que vem
do token, nunca das entradas). Assim o identificador de arquivo escolhido por
quem chama nunca alcança os arquivos de outra empresa. A pasta da empresa não é
criada aqui: se não existir, o passo de envio falha com "arquivo não encontrado".

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

from rapa.control.artifacts import attempt_dir
from rapa.control.services.dispatcher import WorkerNotStarted, failure_result
from rapa.control.services.queue import StaleAttempt
from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent
from rapa.runner.ports import SECRET_FIELDS, ack_path, waits_for_ack
from rapa.runner.result import RunResult
from rapa.runner.video import VIDEO_CLOSE_TIMEOUT_S

__all__ = [
    "EventIngestor",
    "LocalLauncherSettings",
    "LocalProcessLauncher",
    "NoSecrets",
    "SecretSource",
    "StaticSecretSource",
    "child_env",
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


class _SecretsFormatError(ValueError):
    """Segredos em formato errado. A mensagem só cita nomes e tipos, nunca valores."""


def _check_credentials(company_id: UUID, credentials: object) -> dict[str, dict[str, str]]:
    """Confere as credenciais de uma empresa sem nunca pôr um valor na mensagem de erro."""
    if not isinstance(credentials, Mapping):
        raise _SecretsFormatError(f"as credenciais da empresa {company_id} precisam ser um objeto")
    checked: dict[str, dict[str, str]] = {}
    for name, fields in credentials.items():
        if not isinstance(name, str):
            raise _SecretsFormatError(
                f"as credenciais da empresa {company_id} precisam ter nomes de texto"
            )
        if not isinstance(fields, Mapping):
            # Uma senha escrita no lugar do objeto não pode aparecer na mensagem.
            raise _SecretsFormatError(
                f"a credencial '{name}' precisa ser um objeto com os campos do segredo"
            )
        if not all(isinstance(key, str) for key in fields):
            raise _SecretsFormatError(f"a credencial '{name}' precisa ter nomes de campo de texto")
        unknown = sorted(key for key in fields if key not in SECRET_FIELDS)
        if unknown:
            raise _SecretsFormatError(
                f"campos desconhecidos na credencial '{name}': {', '.join(unknown)}"
            )
        for key, value in fields.items():
            if not isinstance(value, str):
                raise _SecretsFormatError(
                    f"o campo '{key}' da credencial '{name}' precisa ser texto"
                )
        checked[name] = dict(fields)
    return checked


class StaticSecretSource:
    """Segredos fixos por empresa, para desenvolvimento e testes.

    Formato: ``{company_id: {nome_da_credencial: {campo: valor}}}``. Cada empresa
    só enxerga as próprias credenciais. As mensagens de erro citam só a empresa,
    o nome da credencial e o do campo, nunca o valor de um segredo.
    """

    def __init__(self, secrets: Mapping[UUID, Mapping[str, Mapping[str, str]]]) -> None:
        if not isinstance(secrets, Mapping):
            raise _SecretsFormatError("os segredos precisam ser um objeto por empresa")
        checked: dict[UUID, dict[str, dict[str, str]]] = {}
        for company_id, credentials in secrets.items():
            if not isinstance(company_id, UUID):
                raise _SecretsFormatError("cada empresa dos segredos precisa ser um UUID")
            checked[company_id] = _check_credentials(company_id, credentials)
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
        companies: dict[UUID, Any] = {}
        for key, value in data.items():
            try:
                companies[UUID(key)] = value
            except ValueError:
                raise ValueError(
                    "arquivo de segredos inválido: cada chave precisa ser o UUID de uma empresa"
                ) from None
        try:
            return cls(companies)
        except _SecretsFormatError as exc:
            raise ValueError(f"arquivo de segredos inválido: {exc}") from None
        except Exception:
            # Texto de um erro inesperado pode trazer um valor do arquivo: mensagem fixa.
            raise ValueError("arquivo de segredos inválido") from None

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
    #: Pasta dos arquivos de entrada, com uma subpasta por empresa
    #: (``<files_dir>/<uuid da empresa>/<arquivo>``); arquivos soltos na raiz não valem.
    files_dir: Path | None = None
    python: str = field(default_factory=lambda: sys.executable)
    #: Intervalo de leitura dos eventos do worker.
    poll_s: float = 0.1
    #: Espera do worker pela confirmação antes de cada passo que grava.
    ack_timeout_s: float = 30.0
    #: Tempo entre o SIGTERM e o SIGKILL quando o prazo acaba ou a tentativa é cancelada.
    kill_grace_s: float = 10.0
    #: Folga além do prazo da execução antes de interromper o worker.
    deadline_grace_s: float = 15.0
    #: Mantém a pasta de trabalho depois do fim (só para depuração).
    keep_work_dir: bool = False
    #: Filma a execução e guarda o vídeo só se ela não der certo (``RAPA_VIDEO_EM_FALHAS``).
    video_on_failure: bool = False
    #: Limite para o worker fechar e guardar o vídeo, somado à folga depois do prazo.
    video_close_s: float = VIDEO_CLOSE_TIMEOUT_S


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


def child_env() -> dict[str, str]:
    """Ambiente mínimo de um processo filho (executor ou sessão de gravação).

    Só o que o Python, o Playwright e as fontes precisam: nada do plano de controle
    (URL do banco, chaves, tokens) passa para o filho.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWED or key.startswith(_ENV_PREFIXES)
    }


def _worker_env() -> dict[str, str]:
    return child_env()


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

    @property
    def grace_s(self) -> float:
        """Folga além do prazo da execução antes de interromper o worker.

        Com o vídeo ligado, o limite para fechá-lo depois do fim da execução entra nela.
        """
        grace = self.settings.deadline_grace_s
        if self.settings.video_on_failure:
            grace += self.settings.video_close_s
        return grace

    def _paths(self, job: RunnerJob) -> tuple[Path, Path]:
        # Caminhos absolutos: o worker roda com a pasta da tentativa como diretório atual,
        # e uma pasta relativa da configuração seria lida a partir dela (os artefatos
        # iriam para dentro da pasta de trabalho, que some no fim).
        work = self.settings.work_dir.absolute() / f"{job.run_id}-{job.attempt}"
        artifacts = attempt_dir(
            self.settings.artifacts_dir.absolute(), job.company_id, job.run_id, job.attempt
        )
        return work, artifacts

    def _command(
        self, job: RunnerJob, work: Path, artifacts: Path, *, with_secrets: bool
    ) -> list[str]:
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
        if settings.video_on_failure:
            # Dentro da pasta da tentativa: some com ela no fim, guardado ou não.
            command += ["--video-dir", str(work / "video")]
        if settings.chromium:
            command += ["--chromium", settings.chromium]
        if settings.files_dir is not None:
            # Só a pasta da empresa da tarefa: a execução de uma empresa nunca vê os
            # arquivos de outra, seja qual for o identificador pedido nas entradas.
            command += ["--files-dir", str(settings.files_dir.absolute() / str(job.company_id))]
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
                    *self._command(job, work, artifacts, with_secrets=bool(secrets)),
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
        limit = (job.deadline - datetime.now(UTC)).total_seconds() + self.grace_s
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
        except BaseException:
            # Cancelamento (tentativa antiga, encerramento) ou erro ao gravar eventos: o
            # worker para antes de qualquer espera. A saída de erro só fecha quando ele
            # sai; esperar por ela antes deixaria um worker antigo gravando em paralelo
            # com a nova tentativa e seguraria o encerramento sem limite.
            await asyncio.shield(self._stop(process))
            raise
        finally:
            if process.returncode is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr
            else:  # pragma: no cover - _stop sempre espera o processo sair
                stderr.cancel()

    async def _forward(
        self,
        job: RunnerJob,
        tail: _EventTail,
        work: Path,
        pending: list[dict[str, Any]],
        *,
        final: bool = False,
    ) -> None:
        """Grava os eventos novos e confirma o início dos passos que gravam.

        Gravar é idempotente (``seq``), então uma falha passageira do banco só
        adia a entrega para a próxima leitura; no fim, tenta mais algumas vezes.
        Só o que foi gravado é confirmado: o worker não começa um passo que grava
        antes de o banco saber dele. Uma tentativa antiga (:class:`StaleAttempt`)
        interrompe o lançamento.
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
            if waits_for_ack(event):
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
