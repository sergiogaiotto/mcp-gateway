"""Lançadores das sessões de gravação: o protocolo, o lançador local e o de pods.

Um lançador põe de pé o hospedeiro de uma sessão (:mod:`rapa.session.host`) e
devolve onde falar com ele (``ws://...``) e o token do canal. O protocolo é pequeno
de propósito (``start``, ``stop``, ``terminate`` e ``is_alive``): o lançador de pods
do Kubernetes (:class:`KubernetesSessionLauncher`, ``RAPA_SESSOES_EXECUTOR=kubernetes``)
toma o lugar do local sem mudar o resto. ``stop``
vem depois do ``parar`` e espera o hospedeiro sair sozinho (até 15 s);
``terminate`` encerra na hora um hospedeiro que ninguém mandou parar (o prazo, o
navegador perdido, o fim do serviço), sem fazer ninguém esperar.

:class:`LocalSessionLauncher`, para a Fase 0 e o desenvolvimento:

- inicia ``python -m rapa.session.host`` num grupo de processos próprio, com o
  ambiente mínimo do executor (:func:`rapa.control.launcher.child_env`: nada do
  plano de controle);
- gera um token aleatório por sessão e o entrega, com o resto da configuração,
  pela entrada padrão (nunca pela linha de comando, que outros processos veem);
- espera a linha ``{"pronto": true, "porta": N}`` e devolve o endereço local;
- guarda o token só em memória; o banco nunca o vê;
- garante o prazo máximo: se o hospedeiro passar dele (com folga), recebe SIGTERM
  e depois SIGKILL;
- cria uma pasta só da sessão para o perfil do navegador e a apaga quando o
  processo sai, mesmo num SIGKILL (o Chromium que ainda escreve nela recebe SIGKILL
  antes).

Com ``egress`` (``LocalSessionSettings.egress``, a mesma guarda da execução fora do
modo de desenvolvimento), a rede interna também fica fechada para o navegador da
sessão, menos as redes internas permitidas, e a internet sai pelo proxy de saída se
houver (ver :mod:`rapa.session.egress`); o hospedeiro continua sem lista de hosts.

O token e o endereço ficam neste processo: com várias réplicas da API, a sessão
só é controlada pela réplica que a abriu.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from rapa.control.kube import (
    LABEL_APP,
    LABEL_COMPANY,
    LABEL_SESSION,
    SESSION_APP,
    KubeApi,
    PodSettings,
    PodState,
    pod_manifest,
    pod_state,
    secret_manifest,
)
from rapa.control.launcher import child_env
from rapa.session.egress import EgressPolicy

__all__ = [
    "KubernetesSessionLauncher",
    "KubernetesSessionSettings",
    "LocalSessionLauncher",
    "LocalSessionSettings",
    "SessionEndpoint",
    "SessionJob",
    "SessionLauncher",
    "SessionNotStarted",
]

logger = logging.getLogger("rapa.control.sessions")

_TOKEN_BYTES = 32
_PROFILE_PREFIX = "rapa-sessao-"
#: Espera pelo hospedeiro que recebeu "parar" (ele devolve a gravação e sai sozinho).
_STOP_WAIT_S = 15.0


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

    async def stop(self, session_id: UUID) -> None:
        """Depois do ``parar``: espera o hospedeiro sair sozinho (e força se ele demorar)."""
        ...

    async def terminate(self, session_id: UUID) -> None:
        """Encerra já um hospedeiro que não recebeu ``parar`` (SIGTERM, depois SIGKILL)."""
        ...

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
    #: A guarda da saída com a rede interna fechada (menos as redes permitidas) e o
    #: proxy de saída; ``None``: só a própria máquina, o link-local e o metadata.
    egress: EgressPolicy | None = None


@dataclass
class _Host:
    process: asyncio.subprocess.Process
    endpoint: SessionEndpoint
    #: Pasta só desta sessão, com o perfil dentro; apagada quando o hospedeiro sai, mesmo morto.
    profiles: Path | None = None
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
        if settings.profile_dir is not None:
            await asyncio.to_thread(
                settings.profile_dir.mkdir, parents=True, exist_ok=True, mode=0o700
            )
        # Uma pasta só desta sessão, com o perfil dentro: um SIGKILL (o prazo de encerramento,
        # a falta de memória) não deixa o hospedeiro apagar o perfil (os cookies do portal),
        # então o lançador apaga esta pasta quando o processo sai, de qualquer jeito.
        profiles = Path(
            await asyncio.to_thread(
                tempfile.mkdtemp, prefix=_PROFILE_PREFIX, dir=settings.profile_dir
            )
        )
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
            "profile_parent": str(profiles),
        }
        if settings.egress is not None:
            proxy = settings.egress.upstream_proxy
            config["egress_policy"] = True
            config["allowed_networks"] = [str(net) for net in settings.egress.allowed_networks]
            # Pelo stdin do hospedeiro: usuário e senha do proxy, quando há, vão junto.
            config["upstream_proxy"] = proxy.url_with_credentials if proxy is not None else None
        process: asyncio.subprocess.Process | None = None
        try:
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
            except Exception as exc:
                logger.error("o hospedeiro da sessão não ficou pronto: %s", type(exc).__name__)
                raise SessionNotStarted("o navegador da gravação não abriu a tempo") from None
        except BaseException:
            if process is not None:
                await self._kill(process)
            await _remove_profiles(profiles)
            raise
        endpoint = SessionEndpoint(
            url=f"ws://{_url_host(settings.listen_host)}:{port}/", token=token
        )
        host = _Host(process=process, endpoint=endpoint, profiles=profiles)
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
            if host.profiles is not None and process.returncode is not None:
                await _remove_profiles(host.profiles)

    async def stop(self, session_id: UUID) -> None:
        host = self._hosts.pop(session_id, None)
        if host is None:
            return
        try:
            # Depois do "parar" o hospedeiro sai sozinho; dá um tempo antes de forçar.
            await asyncio.wait_for(asyncio.shield(host.process.wait()), timeout=_STOP_WAIT_S)
        except TimeoutError:
            await self._kill(host.process)
        await _finish(host)

    async def terminate(self, session_id: UUID) -> None:
        host = self._hosts.pop(session_id, None)
        if host is None:
            return
        # Sem "parar", ninguém espera a gravação: SIGTERM já (o hospedeiro fecha o navegador
        # e apaga o perfil) e SIGKILL depois de ``kill_grace_s``.
        await self._kill(host.process)
        await _finish(host)

    async def is_alive(self, session_id: UUID) -> bool:
        host = self._hosts.get(session_id)
        return host is not None and host.process.returncode is None

    async def aclose(self) -> None:
        """Encerra todos os hospedeiros ao mesmo tempo (no fim do serviço)."""
        await asyncio.gather(
            *(self.terminate(session_id) for session_id in list(self._hosts)),
            return_exceptions=True,
        )

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


async def _finish(host: _Host) -> None:
    """Espera a vigia, que apaga a pasta da sessão depois que o processo saiu."""
    if host.watcher is not None:
        await asyncio.gather(host.watcher, return_exceptions=True)


async def _remove_profiles(path: Path, *, attempts: int = 20) -> None:
    """Apaga a pasta da sessão, mesmo com o Chromium ainda de pé.

    O Chromium roda noutro grupo de processos e, quando o hospedeiro morre, ainda
    escreve no perfil (e pode até abrir um processo novo) por um instante antes de
    perceber: os que têm o perfil desta sessão na linha de comando recebem SIGKILL, e
    a pasta é apagada de novo, até ela sumir e nenhum processo usá-la.
    """
    for _ in range(attempts):
        strays = await asyncio.to_thread(_kill_strays, path)
        await asyncio.to_thread(shutil.rmtree, path, True)
        if not strays and not await asyncio.to_thread(path.exists):
            return
        await asyncio.sleep(0.5)
    logger.warning("a pasta do perfil de uma sessão não pôde ser apagada")


def _kill_strays(path: Path) -> int:
    """SIGKILL nos processos com ``--user-data-dir`` dentro de ``path`` (Linux, por /proc).

    Devolve quantos ainda estavam de pé (um processo morto e ainda não recolhido tem a
    linha de comando vazia e não conta).
    """
    marker = f"--user-data-dir={path}{os.sep}".encode()
    proc = Path("/proc")
    if not proc.is_dir():  # pragma: no cover - fora do Linux, o Chromium sai sozinho
        return 0
    found = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker in cmdline:
            found += 1
            with contextlib.suppress(OSError):
                os.kill(int(entry.name), signal.SIGKILL)
    return found


async def _drain(stream: asyncio.StreamReader) -> None:
    total = 0
    while chunk := await stream.read(65536):
        total += len(chunk)
    if total:
        # Só o tamanho: o que o hospedeiro escreve nunca vai para o log do plano de controle.
        logger.info("o hospedeiro da sessão escreveu %d bytes", total)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


# ---------------------------------------------------------------------------
# Sessões em pods do Kubernetes
# ---------------------------------------------------------------------------

#: Onde o Secret com a configuração do hospedeiro fica montado no pod da sessão.
SESSION_CONFIG_DIR = "/var/run/rapa/sessao"
#: O ``emptyDir`` do trabalho no pod: o perfil e os temporários do navegador (``TMPDIR``),
#: em disco, nunca no ``/tmp`` em memória (que conta no limite de memória do pod).
SESSION_WORK_DIR = "/trabalho"
#: Intervalo entre duas consultas do pod enquanto ele sobe.
_READY_POLL_S = 0.5
#: Folga além de ``start_timeout_s`` antes de a varredura apagar um Secret de sessão sem
#: pod: entre criar o Secret e criar o pod, o Secret fica sozinho por um instante.
_ORPHAN_SECRET_GRACE_S = 60.0


def _age_s(item: Mapping[str, Any], now: datetime) -> float | None:
    """A idade do objeto pelo ``creationTimestamp`` (``None`` se não der para ler)."""
    raw = (item.get("metadata") or {}).get("creationTimestamp")
    if not isinstance(raw, str):
        return None
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds()


@dataclass(frozen=True)
class KubernetesSessionSettings:
    """Configuração do lançador de sessões em pods (``RAPA_SESSOES_EXECUTOR=kubernetes``)."""

    #: Como os pods rodam (a mesma imagem e o mesmo namespace das execuções).
    pod: PodSettings
    browser_args: tuple[str, ...] = ()
    #: A guarda da saída do navegador da sessão (as redes internas e o proxy de saída);
    #: no pod, a rede interna sempre fica fechada, menos as redes permitidas.
    egress: EgressPolicy = field(default_factory=EgressPolicy)
    #: Porta do canal do hospedeiro no pod (o websocket que o repasse abre).
    port: int = 8765
    #: Espera pelo pod pronto (agendamento, imagem, navegador).
    start_timeout_s: float = 90.0
    #: Intervalo entre duas consultas da fase do pod pela vigia.
    status_interval_s: float = 5.0
    #: Tempo entre o SIGTERM e o SIGKILL do hospedeiro (o ``terminationGracePeriodSeconds``).
    kill_grace_s: float = 10.0
    #: Folga além do prazo máximo da sessão antes de apagar o pod.
    deadline_grace_s: float = 30.0
    #: Intervalo da varredura dos pods de sessão que terminaram.
    sweep_interval_s: float = 60.0


@dataclass
class _PodHost:
    name: str
    endpoint: SessionEndpoint
    watcher: asyncio.Task[None] | None = None
    #: O pod terminou (o hospedeiro saiu).
    finished: bool = False
    #: O pod e o Secret já foram apagados.
    removed: bool = False


def session_pod_name(session_id: UUID) -> str:
    return f"rapa-sessao-{session_id}"


class KubernetesSessionLauncher:
    """Hospedeiros das sessões em pods de uso único (``python -m rapa.session.host``).

    O hospedeiro roda num pod com a mesma imagem e as mesmas regras de segurança dos
    pods de execução (:func:`rapa.control.kube.pod_manifest`), mais a porta do
    websocket; a configuração (com o token, gerado aqui e guardado só em memória)
    chega por um Secret montado, em vez da entrada padrão. O aperto de mão é o pod
    pronto (prontidão TCP na porta do canal) mais o IP do pod: o repasse conecta em
    ``ws://<ip-do-pod>:<porta>/`` com o token de hoje. O prazo máximo da sessão também é
    garantido de fora: a vigia apaga o pod depois dele (e o ``activeDeadlineSeconds``
    do pod, se nem a API estiver de pé). O Secret é o dono do pod: apagar um apaga os
    dois. O perfil e os temporários do navegador (``TMPDIR``: os do Chromium e os
    downloads que o Playwright guarda durante a gravação) ficam no ``emptyDir`` do
    trabalho, em disco, e não no ``/tmp`` em memória, que conta no limite de memória do
    pod. Com o pod, o navegador não roda na máquina da API, mas o token e o endereço
    continuam só na réplica que abriu a sessão: com várias réplicas, só ela a controla.
    """

    def __init__(self, settings: KubernetesSessionSettings, *, api: KubeApi) -> None:
        self.settings = settings
        self.api = api
        self._hosts: dict[UUID, _PodHost] = {}
        #: Pods de sessão que este processo está criando (a varredura não os toca).
        self._starting: set[str] = set()

    def __repr__(self) -> str:
        return f"KubernetesSessionLauncher(sessoes={len(self._hosts)})"

    @property
    def running(self) -> int:
        return sum(1 for host in self._hosts.values() if not host.finished)

    def _config(self, job: SessionJob, token: str) -> dict[str, object]:
        settings = self.settings
        proxy = settings.egress.upstream_proxy
        return {
            "token": token,
            "start_url": job.start_url,
            # Todas as interfaces, nas duas pilhas: o repasse chega pela rede do cluster no
            # IP do pod, que é IPv6 num cluster IPv6 (com "0.0.0.0", só IPv4, a prontidão
            # nunca passaria e o repasse seria recusado).
            "listen_host": None,
            "listen_port": settings.port,
            "chromium": None,
            "browser_args": list(settings.browser_args),
            "width": job.width,
            "height": job.height,
            "idle_timeout_s": job.idle_timeout_s,
            "max_duration_s": job.max_duration_s,
            "profile_parent": SESSION_WORK_DIR,
            "egress_policy": True,
            "allowed_networks": [str(net) for net in settings.egress.allowed_networks],
            # No Secret montado: usuário e senha do proxy, quando há, vão junto.
            "upstream_proxy": proxy.url_with_credentials if proxy is not None else None,
        }

    def _labels(self, job: SessionJob) -> dict[str, str]:
        return {
            LABEL_APP: SESSION_APP,
            "app.kubernetes.io/part-of": "rapa",
            LABEL_SESSION: str(job.session_id),
            LABEL_COMPANY: str(job.company_id),
        }

    async def start(self, job: SessionJob) -> SessionEndpoint:
        if job.session_id in self._hosts:
            raise SessionNotStarted("esta sessão já tem um navegador")
        settings = self.settings
        namespace = settings.pod.namespace
        name = session_pod_name(job.session_id)
        labels = self._labels(job)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        created = False
        self._starting.add(name)
        try:
            try:
                secret = await self.api.create(
                    namespace,
                    "secrets",
                    secret_manifest(
                        settings.pod,
                        name=name,
                        labels=labels,
                        data={"config.json": json.dumps(self._config(job, token)).encode()},
                    ),
                )
                created = True
                await self.api.create(
                    namespace,
                    "pods",
                    pod_manifest(
                        settings.pod,
                        name=name,
                        labels=labels,
                        owner=secret,
                        args=[
                            "python",
                            "-m",
                            "rapa.session.host",
                            "--config",
                            f"{SESSION_CONFIG_DIR}/config.json",
                        ],
                        mount_path=SESSION_CONFIG_DIR,
                        # Os temporários do Chromium e do Playwright (os downloads da
                        # gravação) no disco do trabalho, não no /tmp em memória.
                        env={"TMPDIR": SESSION_WORK_DIR},
                        active_deadline_s=job.max_duration_s + settings.deadline_grace_s,
                        termination_grace_s=settings.kill_grace_s,
                        ports=[
                            {"name": "canal", "containerPort": settings.port, "protocol": "TCP"}
                        ],
                        readiness={
                            "tcpSocket": {"port": settings.port},
                            "periodSeconds": 1,
                            "timeoutSeconds": 1,
                            "failureThreshold": 1,
                        },
                    ),
                )
            except Exception as exc:
                logger.error("o pod da sessão não foi criado: %s", type(exc).__name__)
                raise SessionNotStarted("não foi possível abrir o navegador da gravação") from None
            try:
                ip = await asyncio.wait_for(self._ready(name), timeout=settings.start_timeout_s)
            except SessionNotStarted:
                raise
            except Exception as exc:
                logger.error("o pod da sessão não ficou pronto: %s", type(exc).__name__)
                raise SessionNotStarted("o navegador da gravação não abriu a tempo") from None
        except BaseException:
            self._starting.discard(name)
            if created:
                await asyncio.shield(self._remove(name, settings.kill_grace_s))
            raise
        endpoint = SessionEndpoint(url=f"ws://{_url_host(ip)}:{settings.port}/", token=token)
        host = _PodHost(name=name, endpoint=endpoint)
        self._hosts[job.session_id] = host
        self._starting.discard(name)
        host.watcher = asyncio.get_running_loop().create_task(
            self._watch(job, host), name=f"sessao-{job.session_id}"
        )
        return endpoint

    async def _state(self, name: str) -> PodState | None:
        try:
            return pod_state(await self.api.get(self.settings.pod.namespace, "pods", name))
        except Exception as exc:
            logger.warning("a API do Kubernetes não respondeu: %s", type(exc).__name__)
            return None

    async def _ready(self, name: str) -> str:
        """Espera o pod pronto (a porta do canal aceita conexões) e devolve o IP dele."""
        while True:
            state = await self._state(name)
            if state is not None:
                if state.finished or state.stuck:
                    logger.error("o pod da sessão não subiu (%s)", state.reason or state.phase)
                    raise SessionNotStarted("o navegador da gravação não abriu")
                if state.ready and state.ip:
                    return state.ip
            await asyncio.sleep(_READY_POLL_S)

    async def _watch(self, job: SessionJob, host: _PodHost) -> None:
        """Acompanha o pod e garante o prazo máximo da sessão."""
        settings = self.settings
        loop = asyncio.get_running_loop()
        give_up = loop.time() + job.max_duration_s + settings.deadline_grace_s
        try:
            while loop.time() < give_up:
                await asyncio.sleep(min(settings.status_interval_s, give_up - loop.time()))
                state = await self._state(host.name)
                if state is not None and state.finished:
                    return
            logger.warning("a sessão %s passou do prazo máximo; encerrando", job.session_id)
        finally:
            await asyncio.shield(self._discard(host))

    async def _discard(self, host: _PodHost) -> None:
        """Dá a sessão por terminada e apaga o pod e o Secret (uma vez só)."""
        host.finished = True
        if not host.removed:
            host.removed = True
            await self._remove(host.name, self.settings.kill_grace_s)

    async def _remove(self, name: str, grace_s: float) -> None:
        """Apaga o pod (SIGTERM e, depois da tolerância, SIGKILL) e o Secret."""
        namespace = self.settings.pod.namespace
        for plural, grace in (("pods", int(grace_s + 0.999)), ("secrets", None)):
            try:
                await self.api.delete(namespace, plural, name, grace_s=grace)
            except Exception as exc:
                logger.warning("falha ao apagar %s de uma sessão: %s", plural, type(exc).__name__)

    async def _end(self, host: _PodHost) -> None:
        if host.watcher is not None:
            host.watcher.cancel()
            await asyncio.gather(host.watcher, return_exceptions=True)
        # A vigia cancelada antes de rodar não apaga nada: apaga aqui.
        await self._discard(host)

    async def stop(self, session_id: UUID) -> None:
        host = self._hosts.pop(session_id, None)
        if host is None:
            return
        # Depois do "parar" o hospedeiro sai sozinho; dá um tempo antes de apagar o pod.
        loop = asyncio.get_running_loop()
        give_up = loop.time() + _STOP_WAIT_S
        while not host.finished and loop.time() < give_up:
            state = await self._state(host.name)
            if state is not None and state.finished:
                break
            await asyncio.sleep(_READY_POLL_S)
        await self._end(host)

    async def terminate(self, session_id: UUID) -> None:
        host = self._hosts.pop(session_id, None)
        if host is None:
            return
        await self._end(host)

    async def is_alive(self, session_id: UUID) -> bool:
        host = self._hosts.get(session_id)
        return host is not None and not host.finished

    async def aclose(self) -> None:
        """Encerra todos os hospedeiros ao mesmo tempo (no fim do serviço)."""
        await asyncio.gather(
            *(self.terminate(session_id) for session_id in list(self._hosts)),
            return_exceptions=True,
        )

    async def sweep(self) -> int:
        """Apaga os pods de sessão que terminaram e os Secrets sem pod, de outras réplicas.

        Um pod de sessão de outra réplica que ainda roda fica: ele termina sozinho (sem
        entrada por ``RAPA_SESSAO_OCIOSA_S`` ou no prazo máximo, com o
        ``activeDeadlineSeconds``) e sai na varredura seguinte. Um Secret sem pod só sai
        depois de ``start_timeout_s`` mais um minuto da criação: uma sessão que está
        subindo (em qualquer réplica) cria o Secret antes do pod, e apagá-lo nesse
        instante levaria o pod novo junto. As sessões que este processo está abrindo
        nunca entram.
        """
        namespace = self.settings.pod.namespace
        selector = f"{LABEL_APP}={SESSION_APP}"
        mine = {host.name for host in self._hosts.values()} | self._starting
        young = self.settings.start_timeout_s + _ORPHAN_SECRET_GRACE_S
        now = datetime.now(UTC)
        pods = await self.api.list(namespace, "pods", label_selector=selector)
        alive: set[str] = set()
        removed = 0
        for pod in pods:
            name = (pod.get("metadata") or {}).get("name")
            if not isinstance(name, str) or name in mine:
                continue
            if pod_state(pod).finished:
                await self._remove(name, 0)
                removed += 1
            else:
                alive.add(name)
        for secret in await self.api.list(namespace, "secrets", label_selector=selector):
            name = (secret.get("metadata") or {}).get("name")
            if not isinstance(name, str) or name in mine or name in alive:
                continue
            age = _age_s(secret, now)
            if age is not None and age < young:
                continue  # talvez uma sessão subindo: o pod ainda vem
            if name not in self._starting:
                with contextlib.suppress(Exception):
                    if await self.api.delete(namespace, "secrets", name):
                        removed += 1
        return removed

    async def run_sweeper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception as exc:
                logger.warning("falha na varredura dos pods de sessão: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.sweep_interval_s)
