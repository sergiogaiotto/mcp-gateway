"""Agente do pod de execução: ``python -m rapa.runner.pod``.

No modo ``RAPA_EXECUTOR=kubernetes``, cada tentativa roda num pod de uso único
(:class:`rapa.control.launcher.KubernetesLauncher`), e este agente é o processo
principal dele. O pod não fala com o banco nem com o cofre: só com o canal do
despachante que o lançou (:mod:`rapa.control.channel`). O agente:

1. lê o endereço do canal da tentativa e o bilhete do Secret montado
   (``/var/run/rapa/canal/endereco`` e ``/var/run/rapa/canal/bilhete``);
2. busca o trabalho, os segredos e as opções do worker (uma vez só: o canal não
   entrega de novo), com espera crescente se o canal ainda não responde;
3. materializa os segredos num tmpfs (``/tmp``, pasta 0700, arquivos 0600): o
   certificado A1 chega como ``pfx_base64`` e vira um arquivo ``.pfx`` 0600, com
   ``pfx_path`` apontando para ele (um ``pfx_path`` vindo do canal não vale: é um caminho
   da máquina do despachante). O worker apaga o SECRETS.json logo depois de ler; a
   pasta inteira some no fim;
4. baixa do canal os arquivos de entrada da tentativa (os dos passos de envio) para a
   pasta da tentativa e roda o worker de sempre (``python -m rapa.runner.worker``) como
   subprocesso, com as pastas da tentativa no ``emptyDir`` do trabalho (``/trabalho``),
   ``--files-dir`` apontando para esses arquivos e a guarda da saída que as opções pedem
   (no pod, sempre);
5. faz a ponte: manda os eventos novos do EVENTS.jsonl em lotes (no máximo a cada
   100 ms); para o anúncio de um passo que grava, espera a confirmação pelo canal e só
   então escreve o ``<seq>.ack`` que o worker espera. **Nunca inventa uma
   confirmação**: sem ela, o passo que grava não começa (a regra do lançador local);
6. quando o worker termina, manda o que falta dos eventos, sobe os artefatos que o
   RESULT.json cita e, por último, o RESULT.json.

Se o canal cai, o agente tenta de novo com espera crescente até o prazo da execução
(mais a folga das opções). No prazo (ou num SIGTERM, que é como o lançador apaga o pod),
o worker recebe SIGTERM (cancela entre dois passos e escreve o resultado) e, depois
da tolerância, SIGKILL; o que estiver pronto ainda é entregue. Depois do fim (entregue
ou não, com as pastas limpas), um SIGTERM é ignorado: o processo só está saindo. O log
só leva mensagens fixas e nomes de tipos de erro, nunca corpos, bilhete ou segredos.
"""

import argparse
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent
from rapa.runner.ports import ack_path, valid_file_id, waits_for_ack
from rapa.session.egress import GUARD_BREAKING_ENV, PROXY_CREDENTIALS_ENV, UpstreamProxy

__all__ = [
    "ChannelClient",
    "PodOptions",
    "main",
    "materialize_secrets",
    "run_pod",
]

logger = logging.getLogger("rapa.runner.pod")

DEFAULT_CHANNEL_DIR = Path("/var/run/rapa/canal")
DEFAULT_WORK_ROOT = Path("/trabalho")
DEFAULT_TMP_ROOT = Path("/tmp")  # noqa: S108 - o tmpfs (emptyDir em memória) do pod
#: Intervalo máximo entre dois lotes de eventos.
BATCH_INTERVAL_S = 0.1
#: Tamanho máximo de um lote (o canal aceita 8 MiB).
MAX_BATCH_BYTES = 4 * 1024 * 1024
#: Tamanho máximo de uma linha de evento (como no lançador local e no canal).
MAX_EVENT_LINE = 1024 * 1024
#: Prazo para buscar o trabalho (o canal pode ainda não estar respondendo).
FETCH_TIMEOUT_S = 120.0
#: Espera máxima entre duas tentativas de falar com o canal.
RETRY_MAX_S = 10.0
#: Espera longa por uma confirmação.
ACK_WAIT_S = 25.0
#: Tempo entre o SIGTERM e o SIGKILL do worker.
KILL_GRACE_S = 10.0
#: Tamanho máximo do certificado A1.
MAX_PFX_BYTES = 1024 * 1024
#: Variáveis de ambiente repassadas ao worker (as do Python, do Playwright e das fontes).
_ENV_ALLOWED = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "SYSTEMROOT"})
_ENV_PREFIXES = ("PLAYWRIGHT_", "XDG_", "FONTCONFIG_")

EXIT_OK = 0
EXIT_FAILED = 1


class PodOptions(BaseModel):
    """As opções do worker que o lançador manda junto com o trabalho."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    egress_guard: bool = Field(default=True, alias="guarda_de_saida")
    internal_networks: tuple[str, ...] = Field(
        default=(), alias="redes_internas_permitidas", max_length=256
    )
    #: A URL do proxy de saída, com usuário e senha quando há (nunca no ``repr``).
    egress_proxy: str | None = Field(default=None, alias="proxy_saida", max_length=2000, repr=False)
    video: bool = Field(default=False, alias="video")
    ack_timeout_s: float = Field(default=30.0, gt=0, le=3600, alias="espera_ack_s")
    browser_args: tuple[str, ...] = Field(default=(), alias="argumentos_navegador", max_length=256)
    #: Quanto tempo depois do prazo da execução o agente ainda tenta entregar.
    grace_s: float = Field(default=15.0, ge=0, le=3600, alias="folga_s")
    #: Os arquivos de entrada que o agente busca no canal antes de rodar o worker;
    #: ``None``: o despachante não tem pasta de arquivos (o worker fica sem, como no local).
    files: tuple[str, ...] | None = Field(default=None, alias="arquivos", max_length=256)

    @field_validator("egress_proxy")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        if value:
            UpstreamProxy.parse(value)  # ValueError sem repetir a URL
        return value or None

    def upstream_proxy(self) -> UpstreamProxy | None:
        return UpstreamProxy.parse(self.egress_proxy) if self.egress_proxy else None

    @field_validator("files")
    @classmethod
    def _check_files(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and not all(valid_file_id(name) for name in value):
            raise ValueError("identificador de arquivo inválido")
        return value

    def wire(self) -> dict[str, Any]:
        """O formato que vai pelo canal."""
        return self.model_dump(mode="json", by_alias=True)


class ChannelError(RuntimeError):
    """O canal respondeu algo que não adianta repetir."""


class AttemptClosed(ChannelError):
    """O canal encerrou a tentativa (bilhete recusado, trabalho já entregue): nada mais vai."""


class ChannelRefused(ChannelError):
    """O canal recusou o conteúdo (tamanho, nome ou formato): o envio nunca vai passar."""


def _check(response: httpx2.Response, what: str) -> None:
    status = response.status_code
    if status in (401, 404, 410):
        raise AttemptClosed(f"o canal encerrou a tentativa ({what}: {status})")
    if status in (400, 413, 422):
        raise ChannelRefused(f"o canal recusou {what} ({status})")
    response.raise_for_status()


class ChannelClient:
    """Cliente HTTP do canal de uma tentativa (sem redirecionamento, sem proxy do ambiente)."""

    def __init__(
        self,
        address: str,
        ticket: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        split = urlsplit(address)
        if split.scheme not in ("http", "https") or not split.hostname:
            raise ValueError("o endereço do canal precisa ser http(s)://host:porta/...")
        if split.username is not None or split.password is not None or split.query:
            raise ValueError("o endereço do canal não pode ter usuário, senha nem consulta")
        self.base = address.rstrip("/")
        self._http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {ticket}"},
            timeout=httpx2.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return "ChannelClient()"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ChannelClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def fetch_job(self) -> dict[str, Any]:
        response = await self._http.get(f"{self.base}/trabalho")
        _check(response, "o trabalho")
        data = response.json()
        if not isinstance(data, dict):
            raise ChannelError("o canal mandou um trabalho em formato inesperado")
        return data

    async def post_events(self, lines: Sequence[bytes]) -> None:
        response = await self._http.post(
            f"{self.base}/eventos",
            content=b"".join(line + b"\n" for line in lines),
            headers={"Content-Type": "application/x-ndjson"},
        )
        _check(response, "os eventos")

    async def wait_ack(self, seq: int, wait_s: float) -> bool:
        """Espera a confirmação de ``seq``; ``True`` quando ela existe."""
        response = await self._http.get(
            f"{self.base}/ack/{seq}",
            params={"espera": f"{wait_s:g}"},
            timeout=httpx2.Timeout(wait_s + 15.0, connect=10.0),
        )
        if response.status_code == 204:
            return True
        if response.status_code == 408:
            return False
        _check(response, "a confirmação")
        return False

    async def get_file(self, name: str, target: Path) -> bool:
        """Baixa um arquivo de entrada para ``target`` (0600); ``False`` se o canal não o tem."""
        async with self._http.stream("GET", f"{self.base}/arquivos/{name}") as response:
            if response.status_code == 404:
                return False
            _check(response, "um arquivo de entrada")
            fd = await asyncio.to_thread(
                os.open,
                target,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as handle:
                async for chunk in response.aiter_bytes(64 * 1024):
                    await asyncio.to_thread(handle.write, chunk)
        return True

    async def put_artifact(self, name: str, path: Path) -> None:
        handle = await asyncio.to_thread(path.open, "rb")

        async def body() -> AsyncIterator[bytes]:
            while chunk := await asyncio.to_thread(handle.read, 64 * 1024):
                yield chunk

        try:
            size = (await asyncio.to_thread(os.fstat, handle.fileno())).st_size
            response = await self._http.put(
                f"{self.base}/artefatos/{name}",
                content=body(),
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                },
            )
        finally:
            await asyncio.to_thread(handle.close)
        if response.status_code in (201, 409):
            # 409: já chegou inteiro numa tentativa anterior deste envio (um envio ainda em
            # andamento no canal recebe 429, e o agente tenta de novo).
            return
        _check(response, "um artefato")

    async def put_result(self, data: bytes) -> None:
        response = await self._http.put(
            f"{self.base}/resultado",
            content=data,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code in (204, 409):
            return  # 409: já chegou numa tentativa anterior deste envio
        _check(response, "o resultado")


def _transient(exc: BaseException) -> bool:
    """Um erro que vale tentar de novo (rede, prazo, 5xx, 429 ou 408 do canal).

    O 408 é o "pedido lento" do canal (o corpo ou o disco dele passou do tempo): o resultado
    e o artefato não ficaram valendo, e repetir um lote de eventos é seguro (o despachante
    grava pelo ``seq``).
    """
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code in (408, 429)
    return isinstance(exc, (httpx2.TransportError, OSError, TimeoutError))


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def materialize_secrets(secrets: object, directory: Path) -> Path:
    """Escreve o SECRETS.json do worker em ``directory`` (um tmpfs) e devolve o caminho.

    O ``pfx_base64`` de cada credencial vira um arquivo ``.pfx`` 0600 nessa pasta, com
    ``pfx_path`` apontando para ele; um ``pfx_path`` que veio do canal fica de fora. Um
    certificado que não decodifica fica de fora inteiro (o worker falha com
    ``segredo_indisponivel`` claro, sem abrir o navegador). As mensagens de erro só
    citam nomes, nunca valores.
    """
    if not isinstance(secrets, Mapping):
        raise ValueError("os segredos da execução precisam ser um objeto")
    checked: dict[str, dict[str, str]] = {}
    for index, (name, fields) in enumerate(secrets.items()):
        if not isinstance(name, str) or not isinstance(fields, Mapping):
            raise ValueError("cada credencial precisa ser um objeto com os campos do segredo")
        out: dict[str, str] = {}
        certificate: bytes | None = None
        for key, value in fields.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"os campos da credencial '{name}' precisam ser texto")
            if key == "pfx_path":
                continue  # caminho da máquina do despachante: nunca vale no pod
            if key == "pfx_base64":
                try:
                    certificate = base64.b64decode(value, validate=True)
                except binascii.Error, ValueError:
                    logger.warning("o certificado A1 de uma credencial não está em base64")
                    certificate = b""
                continue
            out[key] = value
        if certificate is not None:
            if 0 < len(certificate) <= MAX_PFX_BYTES:
                pfx = directory / f"certificado-{index}.pfx"
                _write_private(pfx, certificate)
                out["pfx_path"] = str(pfx)
            else:
                # Sem o arquivo, a senha do certificado também não serve para nada.
                out.pop("pfx_password", None)
        checked[name] = out
    path = directory / "secrets.json"
    _write_private(path, json.dumps(checked).encode())
    return path


class _LineTail:
    """As linhas completas novas do EVENTS.jsonl (como estão, sem interpretar)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[bytes]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        self._offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > MAX_EVENT_LINE:
            logger.error("linha de evento grande demais; descartada")
            self._partial = b""
        out = []
        for line in lines:
            if not line.strip():
                continue
            if len(line) > MAX_EVENT_LINE:
                logger.error("linha de evento grande demais; descartada")
                continue
            out.append(line)
        return out


def _ack_seq(line: bytes) -> int | None:
    """O ``seq`` do evento se o worker espera a confirmação dele (senão, ``None``)."""
    try:
        event = RunEvent.model_validate_json(line)
    except ValidationError:
        return None
    return event.seq if waits_for_ack(event) else None


def _write_ack(directory: Path, seq: int) -> None:
    target = ack_path(directory, seq)
    temp = target.with_suffix(".tmp")
    temp.write_text("ok", encoding="utf-8")
    os.replace(temp, target)


@dataclass
class _Bridge:
    """A ponte entre o worker (arquivos) e o canal (HTTP) durante a tentativa."""

    client: ChannelClient
    work: Path
    stop_at: float
    tail: _LineTail = field(init=False)
    pending: list[bytes] = field(default_factory=list)
    acks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    retry_at: float = 0.0
    backoff: float = 0.25
    #: O canal encerrou a tentativa (bilhete recusado): nada mais vai.
    over: bool = False

    def __post_init__(self) -> None:
        self.tail = _LineTail(self.work / "events.jsonl")

    async def pump(self, *, final: bool = False) -> None:
        """Lê as linhas novas e manda o que estiver pendente (uma tentativa por chamada)."""
        self.pending.extend(await asyncio.to_thread(self.tail.read_new))
        loop = asyncio.get_running_loop()
        while self.pending and not self.over and loop.time() >= self.retry_at:
            batch: list[bytes] = []
            size = 0
            for line in self.pending:
                if batch and size + len(line) + 1 > MAX_BATCH_BYTES:
                    break
                batch.append(line)
                size += len(line) + 1
            try:
                await self.client.post_events(batch)
            except AttemptClosed:
                logger.error("o canal encerrou a tentativa; os eventos não vão mais")
                self.over = True
                return
            except ChannelRefused:
                # Nunca vai passar: fica de fora, e o passo que esperava a confirmação de
                # um anúncio desse lote não começa (o worker falha sem ela).
                logger.error("o canal recusou um lote de %d evento(s)", len(batch))
                del self.pending[: len(batch)]
                continue
            except Exception as exc:
                if not _transient(exc):
                    raise
                self.retry_at = loop.time() + self.backoff
                self.backoff = min(self.backoff * 2, RETRY_MAX_S)
                logger.warning(
                    "o canal não recebeu os eventos (%s); tentando de novo", type(exc).__name__
                )
                return
            self.backoff = 0.25
            del self.pending[: len(batch)]
            for line in batch:
                seq = _ack_seq(line)
                if seq is not None and seq not in self.acks:
                    self.acks[seq] = asyncio.create_task(self._ack(seq), name=f"ack-{seq}")
            if not final:
                return

    async def flush(self) -> None:
        """Manda o que falta, com espera crescente, até o prazo."""
        loop = asyncio.get_running_loop()
        while not self.over and loop.time() < self.stop_at:
            await self.pump(final=True)
            if not self.pending:
                return
            await asyncio.sleep(max(0.05, min(self.retry_at - loop.time(), RETRY_MAX_S)))
        if self.pending:
            logger.error("%d evento(s) não chegaram ao canal", len(self.pending))

    async def _ack(self, seq: int) -> None:
        """Espera a confirmação pelo canal e só então escreve o ``<seq>.ack`` do worker."""
        loop = asyncio.get_running_loop()
        backoff = 0.25
        while loop.time() < self.stop_at:
            wait = max(1.0, min(ACK_WAIT_S, self.stop_at - loop.time()))
            try:
                confirmed = await self.client.wait_ack(seq, wait)
            except AttemptClosed:
                self.over = True
                return
            except ChannelError:
                return
            except Exception as exc:
                if not _transient(exc):
                    logger.error("falha ao esperar a confirmação: %s", type(exc).__name__)
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RETRY_MAX_S)
                continue
            if confirmed:
                await asyncio.to_thread(_write_ack, self.work / "ack", seq)
                return

    async def close(self) -> None:
        for task in self.acks.values():
            task.cancel()
        await asyncio.gather(*self.acks.values(), return_exceptions=True)


async def _retrying[T](action: Callable[[], Awaitable[T]], stop_at: float, what: str) -> T | None:
    """Repete ``action()`` com espera crescente nos erros passageiros, até ``stop_at``."""
    loop = asyncio.get_running_loop()
    backoff = 0.25
    while True:
        try:
            result: T = await action()
            return result
        except ChannelError:
            raise
        except Exception as exc:
            if not _transient(exc):
                raise
            if loop.time() + backoff >= stop_at:
                logger.error("o canal não respondeu a tempo (%s): %s", what, type(exc).__name__)
                return None
            logger.warning("o canal não respondeu (%s): %s", what, type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX_S)


def _prepare_work(work: Path, job: RunnerJob, *, with_files: bool = False) -> None:
    """A pasta da tentativa no ``emptyDir`` do trabalho, com o JOB.json (0600)."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(mode=0o700, parents=True)
    for name in ("ack", "artefatos", "tmp", *(("arquivos",) if with_files else ())):
        (work / name).mkdir(mode=0o700)
    _write_private(work / "job.json", job.model_dump_json().encode())


async def _fetch_files(
    client: ChannelClient, names: Sequence[str], folder: Path, stop_at: float
) -> None:
    """Baixa os arquivos de entrada do canal, com espera crescente nas falhas passageiras.

    Um arquivo que o canal não tem (ou recusa) fica de fora: o passo de envio que o usar
    falha com "arquivo não encontrado", como no lançador local.
    """
    for name in names:
        try:
            if not await _retrying(_downloader(client, name, folder / name), stop_at, "arquivo"):
                logger.warning("um arquivo de entrada não veio do canal")
        except ChannelRefused as exc:
            logger.warning("%s", exc)


def _downloader(client: ChannelClient, name: str, target: Path) -> Callable[[], Awaitable[bool]]:
    async def download() -> bool:
        try:
            found = await client.get_file(name, target)
        except BaseException:
            # Um pedaço baixado não vale: a próxima tentativa começa do zero.
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
        return found

    return download


def _worker_env(work: Path, options: PodOptions | None = None) -> dict[str, str]:
    """O ambiente do worker: só o que o Python, o Playwright e as fontes precisam, mais
    usuário e senha do proxy de saída (nunca na linha de comando)."""
    env = {
        key: value
        for key, value in os.environ.items()
        if (key in _ENV_ALLOWED or key.startswith(_ENV_PREFIXES)) and key not in GUARD_BREAKING_ENV
    }
    # Os temporários do Chromium (o perfil) vão para o disco do trabalho, não para o tmpfs.
    env["TMPDIR"] = str(work / "tmp")
    proxy = options.upstream_proxy() if options is not None and options.egress_guard else None
    if proxy is not None and proxy.credentials is not None:
        env[PROXY_CREDENTIALS_ENV] = proxy.credentials
    return env


def _worker_command(
    python: str,
    work: Path,
    options: PodOptions,
    *,
    secrets: Path | None,
    chromium: str | None,
    browser_args: Sequence[str],
    files_dir: Path | None = None,
) -> list[str]:
    command = [
        python,
        "-m",
        "rapa.runner.worker",
        "--job",
        str(work / "job.json"),
        "--result",
        str(work / "result.json"),
        "--events",
        str(work / "events.jsonl"),
        "--artifacts",
        str(work / "artefatos"),
        "--ack-dir",
        str(work / "ack"),
        f"--ack-timeout={options.ack_timeout_s}",
    ]
    if secrets is not None:
        command += ["--secrets", str(secrets)]
    if options.video:
        command += ["--video-dir", str(work / "video")]
    if chromium:
        command += ["--chromium", chromium]
    if files_dir is not None:
        command += ["--files-dir", str(files_dir)]
    if options.egress_guard:
        command.append("--guarda-de-saida")
        command += [f"--rede-interna-permitida={net}" for net in options.internal_networks]
        proxy = options.upstream_proxy()
        if proxy is not None:
            command.append(f"--proxy-saida={proxy.url}")  # usuário e senha, no ambiente
    command += [f"--browser-arg={arg}" for arg in browser_args]
    return command


@dataclass
class _Worker:
    process: asyncio.subprocess.Process
    stopping: bool = False

    async def stop(self, grace_s: float) -> None:
        """SIGTERM (o worker cancela entre dois passos e escreve o resultado), depois SIGKILL."""
        process = self.process
        if process.returncode is not None:
            return
        self.stopping = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
    if total:
        logger.info("o worker escreveu %d bytes na saída de erro", total)


def _artifacts_to_upload(result: bytes, folder: Path) -> list[tuple[str, Path]]:
    """Os artefatos que o RESULT.json cita e que estão mesmo na pasta da tentativa."""
    try:
        data = json.loads(result)
    except ValueError:
        return []
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, list):
        return []
    base = folder.resolve()
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        name, raw = item.get("name"), item.get("path")
        if not isinstance(name, str) or not isinstance(raw, str) or name in seen:
            continue
        path = Path(raw)
        if path.name != name or path.parent.resolve() != base or path.is_symlink():
            continue
        if path.is_file():
            out.append((name, path))
            seen.add(name)
    return out


async def run_pod(
    channel_dir: Path = DEFAULT_CHANNEL_DIR,
    work_root: Path = DEFAULT_WORK_ROOT,
    tmp_root: Path = DEFAULT_TMP_ROOT,
    *,
    python: str = sys.executable,
    transport: httpx2.AsyncBaseTransport | None = None,
    kill_grace_s: float = KILL_GRACE_S,
    fetch_timeout_s: float = FETCH_TIMEOUT_S,
) -> int:
    """Roda a tentativa do pod (ver o módulo); devolve o código de saída do agente."""
    try:
        address = (channel_dir / "endereco").read_text(encoding="utf-8").strip()
        ticket = (channel_dir / "bilhete").read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error(
            "o endereço ou o bilhete do canal não puderam ser lidos: %s", type(exc).__name__
        )
        return EXIT_FAILED
    loop = asyncio.get_running_loop()
    try:
        client = ChannelClient(address, ticket, transport=transport)
    except ValueError:
        logger.error("o endereço do canal é inválido")
        return EXIT_FAILED
    del ticket
    async with client:
        try:
            payload = await _retrying(client.fetch_job, loop.time() + fetch_timeout_s, "trabalho")
        except ChannelError as exc:
            logger.error("%s", exc)
            return EXIT_FAILED
        except Exception as exc:
            logger.error("o trabalho não pôde ser buscado: %s", type(exc).__name__)
            return EXIT_FAILED
        if payload is None:
            return EXIT_FAILED
        try:
            job = RunnerJob.model_validate(payload.get("trabalho"))
            options = PodOptions.model_validate(payload.get("opcoes") or {})
        except ValidationError:
            logger.error("o canal mandou um trabalho inválido")
            return EXIT_FAILED
        secrets = payload.pop("segredos", None) or {}
        del payload
        remaining = (job.deadline - datetime.now(UTC)).total_seconds()
        stop_at = loop.time() + max(remaining, 1.0) + options.grace_s
        # O pod é de uso único (uma tentativa só), e o caminho fica curto: o Chromium põe
        # um socket Unix nos temporários (``TMPDIR``), e o caminho dele tem limite de 108 bytes.
        work = work_root / "tentativa"
        private = Path(tempfile.mkdtemp(prefix="rapa-segredos-", dir=tmp_root))
        # Daqui em diante, um SIGTERM (o pod apagado) para o worker e o agente ainda entrega
        # o que tiver e limpa as pastas, em vez de morrer no meio.
        terminated = asyncio.Event()

        # signal.signal, não loop.add_signal_handler: no fim, o SIGTERM passa direto a ser
        # ignorado. O remove_signal_handler do asyncio devolve o SIGTERM ao padrão (que mata o
        # processo) antes de o agente o ignorar, e o pod apagado logo depois do resultado
        # mataria o agente nesse instante (saída -15, sem ter limpado tudo).
        def on_sigterm(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(terminated.set)

        previous = signal.signal(signal.SIGTERM, on_sigterm)
        try:
            return await _run_attempt(
                client,
                job,
                options,
                secrets,
                work=work,
                private=private,
                python=python,
                stop_at=stop_at,
                kill_grace_s=kill_grace_s,
                terminated=terminated,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, private, True)
            await asyncio.to_thread(shutil.rmtree, work, True)
            # A tentativa acabou e as pastas estão limpas: marca o fim antes de devolver o
            # tratamento de antes (o do agente, em _main, que a partir daqui ignora o sinal).
            _ATTEMPT_OVER.set()
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


async def _run_attempt(
    client: ChannelClient,
    job: RunnerJob,
    options: PodOptions,
    secrets: object,
    *,
    work: Path,
    private: Path,
    python: str,
    stop_at: float,
    kill_grace_s: float,
    terminated: asyncio.Event,
) -> int:
    loop = asyncio.get_running_loop()
    if terminated.is_set():
        return EXIT_FAILED  # o pod foi apagado antes de o worker começar: nada roda
    with_files = options.files is not None
    await asyncio.to_thread(partial(_prepare_work, work, job, with_files=with_files))
    files_dir = work / "arquivos" if with_files else None
    if files_dir is not None and options.files:
        try:
            await _fetch_files(client, options.files, files_dir, stop_at)
        except AttemptClosed as exc:
            logger.error("%s", exc)
            return EXIT_FAILED  # a tentativa acabou antes de o worker começar: nada roda
        if terminated.is_set():
            return EXIT_FAILED
    secrets_path: Path | None = None
    if secrets:
        try:
            secrets_path = await asyncio.to_thread(materialize_secrets, secrets, private)
        except ValueError as exc:
            # Só nomes na mensagem. Sem as credenciais, o passo que usar uma falha com
            # segredo_indisponivel, sem digitar nada.
            logger.error("segredos inválidos: %s", exc)
            secrets_path = private / "secrets.json"
            await asyncio.to_thread(_write_private, secrets_path, b"{}")
    del secrets
    browser_args = [
        *shlex.split(os.environ.get("RAPA_ARGS_NAVEGADOR", "")),
        *options.browser_args,
    ]
    command = _worker_command(
        python,
        work,
        options,
        secrets=secrets_path,
        chromium=os.environ.get("RAPA_CHROMIUM_PATH") or None,
        browser_args=browser_args,
        files_dir=files_dir,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=_worker_env(work, options),
            cwd=work,
            start_new_session=True,
        )
    except OSError as exc:
        logger.error("o worker não iniciou: %s", type(exc).__name__)
        return EXIT_FAILED
    worker = _Worker(process)
    stderr = asyncio.create_task(_drain(process.stderr))
    bridge = _Bridge(client, work, stop_at)
    try:
        while process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=BATCH_INTERVAL_S)
            await bridge.pump()
            if terminated.is_set() or bridge.over or loop.time() >= stop_at:
                # O lançador apagou o pod, o canal encerrou a tentativa ou o prazo acabou: o
                # worker cancela e escreve o resultado; a ponte continua enquanto ele termina.
                stopping = asyncio.create_task(worker.stop(kill_grace_s))
                while not stopping.done():
                    await asyncio.wait({stopping}, timeout=BATCH_INTERVAL_S)
                    await bridge.pump()
                await stopping
        await bridge.flush()
    finally:
        await bridge.close()
        if process.returncode is None:
            await worker.stop(kill_grace_s)
        with contextlib.suppress(asyncio.CancelledError):
            await stderr
    if bridge.over:
        return EXIT_FAILED
    return await _deliver(client, work, stop_at)


async def _deliver(client: ChannelClient, work: Path, stop_at: float) -> int:
    """Sobe os artefatos que o RESULT.json cita e, por último, o RESULT.json."""
    try:
        result = await asyncio.to_thread((work / "result.json").read_bytes)
    except FileNotFoundError:
        logger.error("o worker terminou sem resultado")
        return EXIT_FAILED
    loop = asyncio.get_running_loop()
    # O que sobrar de prazo para entregar: pelo menos um pouco, mesmo depois do prazo.
    deliver_by = max(stop_at, loop.time() + 30.0)
    try:
        for name, path in await asyncio.to_thread(_artifacts_to_upload, result, work / "artefatos"):
            try:
                await _retrying(_uploader(client, name, path), deliver_by, "artefato")
            except ChannelRefused as exc:
                logger.warning("%s", exc)  # fica de fora; o resultado vai do mesmo jeito
        delivered = await _retrying(lambda: _put(client, result), deliver_by, "resultado")
    except AttemptClosed as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    except Exception as exc:
        logger.error("falha ao entregar o resultado: %s", type(exc).__name__)
        return EXIT_FAILED
    return EXIT_OK if delivered else EXIT_FAILED


def _uploader(client: ChannelClient, name: str, path: Path) -> Callable[[], Awaitable[None]]:
    return lambda: client.put_artifact(name, path)


async def _put(client: ChannelClient, result: bytes) -> bool:
    await client.put_result(result)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.runner.pod",
        description="Agente do pod de execução do RaPA (fala com o canal do despachante).",
    )
    parser.add_argument(
        "--canal", type=Path, default=DEFAULT_CHANNEL_DIR, help="pasta do Secret do canal"
    )
    parser.add_argument(
        "--trabalho", type=Path, default=DEFAULT_WORK_ROOT, help="pasta do trabalho"
    )
    parser.add_argument("--tmp", type=Path, default=DEFAULT_TMP_ROOT, help="tmpfs dos segredos")
    return parser


#: Marcado quando a tentativa acabou (entregue ou não) e as pastas já foram limpas.
_ATTEMPT_OVER = threading.Event()


def _outside_attempt(_signum: int, _frame: object) -> None:
    """O SIGTERM fora da tentativa (no processo do agente; durante ela, quem cuida é run_pod).

    Antes dela (buscando o trabalho), nada começou: o agente sai. Depois dela, o processo só
    está saindo (o lançador apaga o pod logo depois do resultado): o sinal não muda nada e
    não mata o agente no meio do fim. Nunca o tratamento padrão, que mataria o processo.
    """
    if _ATTEMPT_OVER.is_set():
        return
    raise SystemExit(EXIT_FAILED)


async def _main(channel_dir: Path, work_root: Path, tmp_root: Path) -> int:
    signal.signal(signal.SIGTERM, _outside_attempt)
    try:
        return await run_pod(channel_dir, work_root, tmp_root)
    finally:
        # Também quando a tentativa nem começou: daqui em diante, o processo só está saindo.
        _ATTEMPT_OVER.set()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # O httpx2 registra cada pedido em INFO com a URL inteira (a execução, a tentativa e o
    # nome de cada arquivo e artefato): o log só leva mensagens fixas.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        return asyncio.run(_main(args.canal, args.trabalho, args.tmp))
    except Exception as exc:
        # Só o tipo: a mensagem de um erro inesperado pode trazer dados da execução.
        logger.error("o agente do pod falhou: %s", type(exc).__name__)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
