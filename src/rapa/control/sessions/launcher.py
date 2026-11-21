"""Lançadores das sessões de gravação: o protocolo e o lançador local (subprocesso).

Um lançador põe de pé o hospedeiro de uma sessão (:mod:`rapa.session.host`) e
devolve onde falar com ele (``ws://...``) e o token do canal. O protocolo é pequeno
de propósito (``start``, ``stop`` e ``is_alive``): na Fase 1 um lançador de pods do
Kubernetes toma o lugar do local, sem mudar o resto.

:class:`LocalSessionLauncher`, para a Fase 0 e o desenvolvimento:

- inicia ``python -m rapa.session.host`` num grupo de processos próprio, com o
  ambiente mínimo do executor (:func:`rapa.control.launcher.child_env`: nada do
  plano de controle);
- gera um token aleatório por sessão e o entrega, com o resto da configuração,
  pela entrada padrão (nunca pela linha de comando, que outros processos veem);
- espera a linha ``{"pronto": true, "porta": N}`` e devolve o endereço local;
- guarda o token só em memória; o banco nunca o vê;
- garante o prazo máximo: se o hospedeiro passar dele (com folga), recebe SIGTERM
  e depois SIGKILL.

O token e o endereço ficam neste processo: com várias réplicas da API, a sessão
só é controlada pela réplica que a abriu (o lançador de pods resolve isso).
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from rapa.control.launcher import child_env

__all__ = [
    "LocalSessionLauncher",
    "LocalSessionSettings",
    "SessionEndpoint",
    "SessionJob",
    "SessionLauncher",
    "SessionNotStarted",
]

logger = logging.getLogger("rapa.control.sessions")

_TOKEN_BYTES = 32


class SessionNotStarted(RuntimeError):
    """O hospedeiro da sessão não ficou pronto (a mensagem é fixa, em pt-BR)."""


@dataclass(frozen=True, slots=True)
class SessionJob:
    """O que o lançador precisa para abrir uma sessão."""

    session_id: UUID
    company_id: UUID
    start_url: str
    idle_timeout_s: float = 600.0
    max_duration_s: float = 3600.0
    width: int = 1280
    height: int = 800


@dataclass(frozen=True, slots=True)
class SessionEndpoint:
    """Onde falar com o hospedeiro e com que token (o token não aparece no ``repr``)."""

    url: str
    token: str = field(repr=False)


class SessionLauncher(Protocol):
    """Põe de pé, encerra e confere o hospedeiro de uma sessão."""

    async def start(self, job: SessionJob) -> SessionEndpoint: ...

    async def stop(self, session_id: UUID) -> None: ...

    async def is_alive(self, session_id: UUID) -> bool: ...


@dataclass(frozen=True)
class LocalSessionSettings:
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    #: Endereço em que cada hospedeiro escuta (o canal só aceita o plano de controle).
    listen_host: str = "127.0.0.1"
    python: str = field(default_factory=lambda: sys.executable)
    #: Pasta onde cada hospedeiro cria (e apaga) o perfil do navegador.
    profile_dir: Path | None = None
    #: Espera pelo hospedeiro pronto.
    start_timeout_s: float = 60.0
    #: Tempo entre o SIGTERM e o SIGKILL.
    kill_grace_s: float = 10.0
    #: Folga além do prazo máximo da sessão antes de interromper o hospedeiro.
    deadline_grace_s: float = 30.0


@dataclass
class _Host:
    process: asyncio.subprocess.Process
    endpoint: SessionEndpoint
    watcher: asyncio.Task[None] | None = None


class LocalSessionLauncher:
    """Hospedeiros como processos locais (``python -m rapa.session.host``)."""

    def __init__(self, settings: LocalSessionSettings | None = None) -> None:
        self.settings = settings or LocalSessionSettings()
        self._hosts: dict[UUID, _Host] = {}

    def __repr__(self) -> str:
        return f"LocalSessionLauncher(sessoes={len(self._hosts)})"

    @property
    def running(self) -> int:
        return sum(1 for host in self._hosts.values() if host.process.returncode is None)

    async def start(self, job: SessionJob) -> SessionEndpoint:
        if job.session_id in self._hosts:
            raise SessionNotStarted("esta sessão já tem um navegador")
        settings = self.settings
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        config = {
            "token": token,
            "start_url": job.start_url,
            "listen_host": settings.listen_host,
            "listen_port": 0,
            "chromium": settings.chromium,
            "browser_args": list(settings.browser_args),
            "width": job.width,
            "height": job.height,
            "idle_timeout_s": job.idle_timeout_s,
            "max_duration_s": job.max_duration_s,
            "profile_parent": str(settings.profile_dir) if settings.profile_dir else None,
        }
        if settings.profile_dir is not None:
            await asyncio.to_thread(
                settings.profile_dir.mkdir, parents=True, exist_ok=True, mode=0o700
            )
        try:
            process = await asyncio.create_subprocess_exec(
                settings.python,
                "-m",
                "rapa.session.host",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(),
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("o hospedeiro da sessão não iniciou: %s", type(exc).__name__)
            raise SessionNotStarted("não foi possível abrir o navegador da gravação") from None
        try:
            port = await asyncio.wait_for(
                self._handshake(process, config), timeout=settings.start_timeout_s
            )
        except BaseException as exc:
            await self._kill(process)
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.error("o hospedeiro da sessão não ficou pronto: %s", type(exc).__name__)
            raise SessionNotStarted("o navegador da gravação não abriu a tempo") from None
        endpoint = SessionEndpoint(
            url=f"ws://{_url_host(settings.listen_host)}:{port}/", token=token
        )
        host = _Host(process=process, endpoint=endpoint)
        self._hosts[job.session_id] = host
        host.watcher = asyncio.get_running_loop().create_task(
            self._watch(job, host), name=f"sessao-{job.session_id}"
        )
        return endpoint

    @staticmethod
    async def _handshake(process: asyncio.subprocess.Process, config: dict[str, object]) -> int:
        if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE sempre
            raise SessionNotStarted("o hospedeiro não abriu os canais")
        process.stdin.write(json.dumps(config).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        line = await process.stdout.readline()
        data = json.loads(line) if line.strip() else None
        if not isinstance(data, dict) or data.get("pronto") is not True:
            raise SessionNotStarted("resposta inesperada do hospedeiro")
        port = data.get("porta")
        if not isinstance(port, int) or not 0 < port < 65536:
            raise SessionNotStarted("porta inválida do hospedeiro")
        return port

    async def _watch(self, job: SessionJob, host: _Host) -> None:
        """Lê a saída do hospedeiro (só para não travá-lo) e garante o prazo máximo."""
        process = host.process
        drains = [
            asyncio.create_task(_drain(stream))
            for stream in (process.stdout, process.stderr)
            if stream is not None
        ]
        limit = job.max_duration_s + self.settings.deadline_grace_s
        try:
            await asyncio.wait_for(process.wait(), timeout=limit)
        except TimeoutError:
            logger.warning("a sessão %s passou do prazo máximo; encerrando", job.session_id)
            await self._kill(process)
        finally:
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)

    async def stop(self, session_id: UUID) -> None:
        host = self._hosts.pop(session_id, None)
        if host is None:
            return
        try:
            # Depois do "parar" o hospedeiro sai sozinho; dá um tempo antes de forçar.
            await asyncio.wait_for(asyncio.shield(host.process.wait()), timeout=15)
        except TimeoutError:
            await self._kill(host.process)
        if host.watcher is not None:
            host.watcher.cancel()
            await asyncio.gather(host.watcher, return_exceptions=True)

    async def is_alive(self, session_id: UUID) -> bool:
        host = self._hosts.get(session_id)
        return host is not None and host.process.returncode is None

    async def aclose(self) -> None:
        """Encerra todos os hospedeiros (no fim do serviço)."""
        for session_id in list(self._hosts):
            host = self._hosts.pop(session_id)
            await self._kill(host.process)
            if host.watcher is not None:
                host.watcher.cancel()
                await asyncio.gather(host.watcher, return_exceptions=True)

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
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


async def _drain(stream: asyncio.StreamReader) -> None:
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
    if total:
        # Só o tamanho: o que o hospedeiro escreve nunca vai para o log do plano de controle.
        logger.info("o hospedeiro da sessão escreveu %d bytes", total)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host
