"""Canal do despachante: por onde o pod de uma execução fala com quem o lançou.

No modo ``RAPA_EXECUTOR=kubernetes``, cada tentativa roda num pod de uso único
(:class:`rapa.control.launcher.KubernetesLauncher`). O agente do pod
(:mod:`rapa.runner.pod`) não fala com o banco nem com o cofre: só com este canal, um
app ASGI pequeno que o despachante serve na ``RAPA_CANAL_PORTA``. As rotas são por
execução e tentativa (``/canal/<execução>/<tentativa>/...``):

- ``GET .../trabalho``: o trabalho (o JOB.json), os segredos e as opções do worker,
  **uma vez só** (a segunda chamada recebe 410). Os segredos ficam só na memória do
  despachante até essa entrega e nunca vão para o disco dele;
- ``GET .../arquivos/<nome>``: um arquivo de entrada da tentativa (os dos passos de
  envio), só os que o lançador registrou ao abrir a tentativa (os arquivos da pasta da
  empresa que as entradas de arquivo citam); qualquer outro nome recebe 404;
- ``POST .../eventos``: linhas JSONL (um lote; cada linha até 1 MiB, como no
  lançador local), cada uma um evento desta tentativa, acrescentadas na ordem ao
  EVENTS.jsonl da pasta de trabalho local da tentativa. Dali o seguimento de sempre do
  lançador grava os eventos no banco e escreve os ``<seq>.ack``;
- ``GET .../ack/<seq>?espera=25``: espera longa pelo ``<seq>.ack`` (204 quando
  existe, 408 no tempo): o agente só escreve a confirmação que o worker espera depois
  de o lançador gravar o anúncio no banco, como no lançador local;
- ``PUT .../artefatos/<nome>``: o arquivo vai para a pasta de artefatos local da
  tentativa (a de :func:`rapa.control.artifacts.attempt_dir`), com as regras de nome do
  executor, criado com ``O_EXCL`` (um nome que já chegou inteiro recebe 409; um que
  ainda está chegando por outro pedido, 429, porque esse envio ainda pode falhar) e com
  limite de tamanho; daí o despachante o registra pelo store de sempre;
- ``PUT .../resultado``: o RESULT.json, uma vez só, gravado na pasta de trabalho; o
  lançador o lê e valida como no lançador local (inválido vira ``resultado_invalido``).

Cada tentativa tem um bilhete aleatório de 32 bytes (``Authorization: Bearer``),
guardado só pelo SHA-256 na memória do despachante e comparado em tempo constante; um
bilhete só vale para a execução e a tentativa dele, e só até a tentativa acabar
(:meth:`Channel.close`). Bilhete errado, de outra execução ou de uma tentativa que já
acabou recebem a mesma resposta (401), sem dizer qual foi o caso. Todo corpo tem
limite de tamanho, nada redireciona (nem a barra no fim do endereço) e o canal nunca
registra corpo, bilhete ou segredo: os logs só levam códigos.

O pod e o que roda nele não são de confiança: tudo o que chega por aqui é conferido
(tamanho, formato, nome, a execução e a tentativa de cada evento), e o resultado
passa pela mesma validação do lançador local. Um pod com o próprio bilhete também não
toma o despachante das outras tentativas:

- no máximo :attr:`ChannelLimits.max_requests` pedidos ao mesmo tempo por tentativa
  (os demais recebem 429, que o agente repete com espera crescente), um lote de eventos
  e um resultado por vez (o corpo só é lido depois de a vez ser dele), e as cotas de
  bytes (por artefato e pela tentativa inteira) são reservadas pedaço a pedaço, antes de
  gravar: envios ao mesmo tempo nunca passam delas;
- um corpo que fica :attr:`ChannelLimits.idle_timeout_s` sem chegar nada recebe 408;
- a espera pela confirmação não usa threads (o lançador avisa quando escreve um ack, e
  o arquivo é conferido de novo a cada meio segundo), e o disco do canal roda num grupo
  de threads só dele, longe do seguimento das outras execuções;
- :meth:`Channel.close` encerra na hora os pedidos da tentativa que ainda estão no meio
  (um envio interrompido não deixa arquivo), e :meth:`Channel.drain` espera que
  terminem: o lançador fecha a tentativa antes de ler o resultado e de apagar o que o
  resultado não cita, então nada chega depois dessa decisão;
- o servidor (:class:`ChannelServer`) limita as conexões abertas ao mesmo tempo
  (:attr:`ChannelLimits.max_connections`; as demais recebem 503).
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import stat
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from rapa.core.artifacts import valid_artifact_name
from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent

__all__ = [
    "MAX_EVENT_LINE",
    "Channel",
    "ChannelAttempt",
    "ChannelLimits",
    "ChannelServer",
    "new_ticket",
]

logger = logging.getLogger("rapa.control.channel")

#: Bytes aleatórios do bilhete de cada tentativa.
TICKET_BYTES = 32
#: Tamanho máximo de uma linha de evento (o mesmo do lançador local).
MAX_EVENT_LINE = 1024 * 1024
#: Nome de artefato mais comprido que o executor produz (120 e o sufixo ``-N``).
MAX_ARTIFACT_NAME = 130
#: Intervalo em que a espera longa confere de novo o arquivo de confirmação (o lançador
#: avisa quando escreve um; isto só cobre um ack escrito sem o aviso).
_ACK_POLL_S = 0.5
_CHUNK = 64 * 1024
_NO_STORE = {"Cache-Control": "no-store"}


def new_ticket() -> str:
    """Um bilhete novo (32 bytes aleatórios, em base64 para URL)."""
    return secrets.token_urlsafe(TICKET_BYTES)


def _digest(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode("utf-8", "surrogateescape")).digest()


@dataclass(frozen=True)
class ChannelLimits:
    """Limites do que o pod pode mandar."""

    #: Corpo de um lote de eventos.
    max_events_body: int = 8 * 1024 * 1024
    #: Todos os eventos de uma tentativa juntos (o EVENTS.jsonl no disco do despachante).
    max_attempt_events_bytes: int = 512 * 1024 * 1024
    #: Uma linha de evento.
    max_event_line: int = MAX_EVENT_LINE
    #: O RESULT.json (o retrato da falha cabe em 1 MiB; as saídas, no resto).
    max_result_bytes: int = 16 * 1024 * 1024
    #: Um artefato (a tela, o arquivo baixado, o vídeo da falha).
    max_artifact_bytes: int = 1024 * 1024 * 1024
    #: Todos os artefatos de uma tentativa juntos.
    max_attempt_bytes: int = 4 * 1024 * 1024 * 1024
    #: Quantos artefatos uma tentativa guarda (o despachante registra no máximo isso).
    max_artifacts: int = 10_000
    #: Espera longa máxima por uma confirmação.
    max_ack_wait_s: float = 30.0
    #: Pedidos de uma tentativa ao mesmo tempo (o agente usa poucos: um lote de eventos,
    #: a espera pela confirmação, um envio de arquivo); os demais recebem 429.
    max_requests: int = 16
    #: Tempo máximo sem chegar nada do corpo de um pedido (408 depois disso).
    idle_timeout_s: float = 30.0
    #: Conexões abertas ao mesmo tempo no servidor do canal, de todas as tentativas.
    max_connections: int = 2048
    #: Threads do disco do canal (só dele: o seguimento das execuções usa as outras).
    io_threads: int = 4


class _Gone(Exception):
    """A tentativa acabou no meio do pedido."""


@dataclass(eq=False)
class ChannelAttempt:
    """Uma tentativa aberta no canal: o bilhete (pelo hash), o trabalho e as pastas locais."""

    run_id: UUID
    attempt: int
    company_id: UUID
    #: Pasta de trabalho local da tentativa (EVENTS.jsonl, ``ack/`` e RESULT.json).
    work_dir: Path
    #: Pasta local dos artefatos da tentativa.
    artifacts_dir: Path
    _ticket: bytes = field(repr=False)
    #: O corpo de ``trabalho`` (com os segredos): só até a entrega.
    _payload: bytes | None = field(repr=False)
    #: Os arquivos de entrada que o pod pode buscar (nome -> caminho local).
    files: Mapping[str, Path] = field(default_factory=dict, repr=False)
    #: O pod já buscou o trabalho (então o worker pode ter começado).
    fetched: bool = False
    #: Tentativa encerrada: o bilhete não vale mais.
    closed: bool = False
    result_received: asyncio.Event = field(default_factory=asyncio.Event)
    artifacts: int = 0
    #: Bytes de artefatos gravados e reservados pelos envios em andamento.
    artifact_bytes: int = 0
    #: Os nomes dos artefatos que algum pedido ainda está gravando.
    _uploading: set[str] = field(default_factory=set, repr=False)
    event_bytes: int = 0
    _events_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _result_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Pedidos em andamento, e o aviso de que não há nenhum.
    _active: int = 0
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    #: O prazo de cada pedido em andamento: :meth:`Channel.close` os vence na hora.
    _watches: set[asyncio.Timeout] = field(default_factory=set)
    #: Trocado (e o antigo disparado) a cada confirmação escrita pelo lançador.
    _ack_signal: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._idle.set()

    @property
    def events_path(self) -> Path:
        return self.work_dir / "events.jsonl"

    @property
    def ack_dir(self) -> Path:
        return self.work_dir / "ack"

    @property
    def result_path(self) -> Path:
        return self.work_dir / "result.json"

    @property
    def active(self) -> int:
        """Pedidos desta tentativa em andamento."""
        return self._active

    def wake_acks(self) -> None:
        """Acorda as esperas longas: o lançador acabou de escrever uma confirmação."""
        signal, self._ack_signal = self._ack_signal, asyncio.Event()
        signal.set()

    def _enter(self) -> None:
        self._active += 1
        self._idle.clear()

    def _leave(self) -> None:
        self._active -= 1
        if self._active <= 0:
            self._active = 0
            self._idle.set()

    def _expire_all(self) -> None:
        for watch in list(self._watches):
            with contextlib.suppress(RuntimeError):  # já vencendo ou já saiu
                watch.reschedule(-1)


class Channel:
    """As tentativas abertas e o app ASGI do canal (:attr:`app`)."""

    def __init__(self, limits: ChannelLimits | None = None) -> None:
        self.limits = limits or ChannelLimits()
        self._attempts: dict[tuple[UUID, int], ChannelAttempt] = {}
        #: Comparado quando a tentativa não existe: a resposta leva o mesmo tempo.
        self._dummy = _digest(new_ticket())
        #: O disco do canal: threads só dele, para um pod não ocupar as do despachante.
        self._io = ThreadPoolExecutor(
            max_workers=max(1, self.limits.io_threads), thread_name_prefix="canal-disco"
        )
        prefix = "/canal/{execucao}/{tentativa:int}"
        self.app = Starlette(
            routes=[
                Route(f"{prefix}/trabalho", self._job, methods=["GET"]),
                Route(f"{prefix}/arquivos/{{nome}}", self._file, methods=["GET"]),
                Route(f"{prefix}/eventos", self._events, methods=["POST"]),
                Route(f"{prefix}/ack/{{seq:int}}", self._ack, methods=["GET"]),
                Route(f"{prefix}/artefatos/{{nome}}", self._artifact, methods=["PUT"]),
                Route(f"{prefix}/resultado", self._result, methods=["PUT"]),
            ]
        )
        # Nada de redirecionamento, nem o da barra no fim do endereço.
        self.app.router.redirect_slashes = False

    def __repr__(self) -> str:
        return f"Channel(tentativas={len(self._attempts)})"

    def __len__(self) -> int:
        return len(self._attempts)

    def open(
        self,
        job: RunnerJob,
        *,
        work_dir: Path,
        artifacts_dir: Path,
        secrets: Mapping[str, Mapping[str, str]],
        options: Mapping[str, Any],
        files: Mapping[str, Path] | None = None,
    ) -> tuple[ChannelAttempt, str]:
        """Abre a tentativa e devolve o registro e o bilhete (que só o pod recebe).

        ``files`` são os arquivos de entrada que o pod pode buscar (nome -> caminho
        local, já conferido pelo lançador dentro da pasta da empresa).
        """
        key = (job.run_id, job.attempt)
        if key in self._attempts:
            raise ValueError("a tentativa já está aberta no canal")
        ticket = new_ticket()
        payload = json.dumps(
            {
                "trabalho": job.model_dump(mode="json"),
                "segredos": {name: dict(fields) for name, fields in secrets.items()},
                "opcoes": dict(options),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        attempt = ChannelAttempt(
            run_id=job.run_id,
            attempt=job.attempt,
            company_id=job.company_id,
            work_dir=work_dir,
            artifacts_dir=artifacts_dir,
            _ticket=_digest(ticket),
            _payload=payload,
            files=dict(files or {}),
        )
        self._attempts[key] = attempt
        return attempt, ticket

    def close(self, run_id: UUID, attempt: int) -> None:
        """Encerra a tentativa: o bilhete deixa de valer, os segredos saem da memória e os
        pedidos dela que ainda estão no meio acabam já (ver :meth:`drain`)."""
        registered = self._attempts.pop((run_id, attempt), None)
        if registered is not None:
            registered.closed = True
            registered._payload = None
            registered._expire_all()
            registered.wake_acks()

    async def drain(self, attempt: ChannelAttempt, *, timeout_s: float = 10.0) -> bool:
        """Espera os pedidos da tentativa (já fechada) terminarem; ``False`` se sobrou algum."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(attempt._idle.wait(), timeout_s)
        return attempt._active == 0

    def get(self, run_id: UUID, attempt: int) -> ChannelAttempt | None:
        return self._attempts.get((run_id, attempt))

    async def _disk[T](self, function: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._io, function, *args)

    # -- autenticação ------------------------------------------------------------

    def _authorize(self, request: Request) -> ChannelAttempt | None:
        try:
            run_id = UUID(str(request.path_params["execucao"]))
        except ValueError:
            run_id = None
        number = request.path_params.get("tentativa")
        attempt = (
            self._attempts.get((run_id, number))
            if run_id is not None and isinstance(number, int)
            else None
        )
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        ok = scheme.lower() == "bearer" and bool(presented)
        expected = attempt._ticket if attempt is not None else self._dummy
        # Comparação em tempo constante, também quando a tentativa não existe.
        same = hmac.compare_digest(_digest(presented.strip()), expected)
        if not (ok and same) or attempt is None or attempt.closed:
            return None
        return attempt

    async def _guarded(
        self,
        request: Request,
        handler: Callable[[Request, ChannelAttempt, asyncio.Timeout], Awaitable[Response]],
        *,
        idle: bool = True,
    ) -> Response:
        """Roda ``handler`` como um pedido da tentativa: com vaga, prazo e fim no fechamento.

        Sem vaga (:attr:`ChannelLimits.max_requests`), 429. O prazo do pedido começa em
        :attr:`ChannelLimits.idle_timeout_s` (``idle``; o handler o renova a cada pedaço
        do corpo) e :meth:`Channel.close` o vence na hora: 410.
        """
        attempt = self._authorize(request)
        if attempt is None:
            return _unauthorized()
        if attempt._active >= self.limits.max_requests:
            return _error(429, "pedidos_demais")
        attempt._enter()
        try:
            try:
                async with asyncio.timeout(self.limits.idle_timeout_s if idle else None) as watch:
                    attempt._watches.add(watch)
                    try:
                        if attempt.closed:
                            raise _Gone
                        return await handler(request, attempt, watch)
                    finally:
                        attempt._watches.discard(watch)
            except TimeoutError:
                if attempt.closed:
                    return _error(410, "tentativa_encerrada")
                return _error(408, "pedido_lento")
            except _Gone:
                return _error(410, "tentativa_encerrada")
        finally:
            attempt._leave()

    async def _stream(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout
    ) -> AsyncGenerator[bytes]:
        """Os pedaços do corpo, com o prazo renovado a cada um; para se a tentativa acabar."""
        loop = asyncio.get_running_loop()
        async for chunk in request.stream():
            if attempt.closed:
                raise _Gone
            watch.reschedule(loop.time() + self.limits.idle_timeout_s)
            if chunk:
                yield chunk

    async def _read_limited(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout, limit: int
    ) -> bytes | None:
        """O corpo inteiro, ou ``None`` se passar de ``limit`` (sem ler o resto)."""
        declared = _content_length(request)
        if declared is not None and declared > limit:
            return None
        parts: list[bytes] = []
        size = 0
        try:
            async with contextlib.aclosing(self._stream(request, attempt, watch)) as chunks:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        return None
                    parts.append(chunk)
        except ClientDisconnect:
            return None
        return b"".join(parts)

    # -- rotas -------------------------------------------------------------------

    async def _job(self, request: Request) -> Response:
        attempt = self._authorize(request)
        if attempt is None:
            return _unauthorized()
        payload = attempt._payload
        if attempt.fetched or payload is None:
            return _error(410, "trabalho_ja_entregue")
        # Uma vez só: os segredos saem da memória do despachante aqui.
        attempt.fetched = True
        attempt._payload = None
        return Response(payload, media_type="application/json", headers=_NO_STORE)

    async def _file(self, request: Request) -> Response:
        attempt = self._authorize(request)
        if attempt is None:
            return _unauthorized()
        path = attempt.files.get(str(request.path_params["nome"]))
        if path is None:
            return _error(404, "arquivo_indisponivel")
        if attempt._active >= self.limits.max_requests:
            return _error(429, "pedidos_demais")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return _error(404, "arquivo_indisponivel")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("não é um arquivo comum")
        except OSError:
            os.close(fd)
            return _error(404, "arquivo_indisponivel")
        attempt._enter()  # sai no fim do envio (_FileStream)
        return _FileStream(self, attempt, fd, info.st_size)

    async def _events(self, request: Request) -> Response:
        return await self._guarded(request, self._store_events)

    async def _store_events(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout
    ) -> Response:
        limits = self.limits
        # Um lote por vez: o corpo só é lido (e guardado na memória) na vez dele.
        if attempt._events_lock.locked():
            return _error(429, "lote_em_andamento")
        async with attempt._events_lock:
            body = await self._read_limited(request, attempt, watch, limits.max_events_body)
            if body is None:
                return _error(413, "lote_grande_demais")
            lines = [line for line in body.split(b"\n") if line.strip()]
            for line in lines:
                if len(line) > limits.max_event_line:
                    return _error(413, "evento_grande_demais")
                try:
                    event = RunEvent.model_validate_json(line)
                except ValidationError:
                    return _error(422, "evento_invalido")
                if event.run_id != attempt.run_id or event.attempt != attempt.attempt:
                    return _error(422, "evento_de_outra_tentativa")
            if lines:
                data = b"".join(line.rstrip(b"\r") + b"\n" for line in lines)
                if attempt.closed:
                    raise _Gone
                if attempt.event_bytes + len(data) > limits.max_attempt_events_bytes:
                    return _error(413, "eventos_demais")
                attempt.event_bytes += len(data)
                try:
                    await self._disk(_append, attempt.events_path, data)
                except OSError as exc:
                    attempt.event_bytes -= len(data)
                    logger.error(
                        "eventos de %s não foram gravados: %s", attempt.run_id, type(exc).__name__
                    )
                    return _error(500, "eventos_nao_gravados")
        return Response(status_code=204)

    async def _ack(self, request: Request) -> Response:
        return await self._guarded(request, self._wait_ack, idle=False)

    async def _wait_ack(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout
    ) -> Response:
        seq = request.path_params["seq"]
        try:
            wait = float(request.query_params.get("espera", "25"))
        except ValueError:
            return _error(400, "espera_invalida")
        if not 0 <= wait <= 86_400 or seq < 0:
            return _error(400, "espera_invalida")
        wait = min(wait, self.limits.max_ack_wait_s)
        marker = attempt.ack_dir / f"{seq}.ack"
        loop = asyncio.get_running_loop()
        give_up = loop.time() + wait
        while True:
            signal = attempt._ack_signal
            if attempt.closed:
                raise _Gone
            # Um stat no disco local: sem thread (uma espera longa não ocupa nenhuma).
            if marker.exists():
                return Response(status_code=204)
            remaining = give_up - loop.time()
            if remaining <= 0:
                return Response(status_code=408)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(signal.wait(), min(remaining, _ACK_POLL_S))

    async def _artifact(self, request: Request) -> Response:
        return await self._guarded(request, self._store_artifact)

    async def _store_artifact(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout
    ) -> Response:
        name = str(request.path_params["nome"])
        if not valid_artifact_name(name) or len(name) > MAX_ARTIFACT_NAME:
            return _error(400, "nome_invalido")
        limits = self.limits
        if attempt.artifacts >= limits.max_artifacts:
            return _error(413, "artefatos_demais")
        room = min(limits.max_artifact_bytes, limits.max_attempt_bytes - attempt.artifact_bytes)
        declared = _content_length(request)
        if declared is not None and declared > room:
            return _error(413, "artefato_grande_demais")
        path = attempt.artifacts_dir / name
        try:
            # Na hora (um open no disco local): um fechamento no meio nunca deixa um
            # arquivo criado por uma thread que ninguém mais acompanha.
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except FileExistsError:
            if name in attempt._uploading:
                # Outro pedido ainda está gravando este nome e pode falhar (e apagar o
                # arquivo): 409 aqui faria o agente dar por entregue o que pode sumir.
                return _error(429, "artefato_em_andamento")
            return _error(409, "artefato_repetido")
        except OSError as exc:
            logger.error(
                "artefato de %s não pôde ser criado: %s", attempt.run_id, type(exc).__name__
            )
            return _error(500, "artefato_nao_gravado")
        # Contado já na reserva: dois envios ao mesmo tempo não passam do limite.
        attempt.artifacts += 1
        attempt._uploading.add(name)
        digest = hashlib.sha256()
        size = 0
        kept = False
        try:
            with os.fdopen(fd, "wb") as handle:
                async with contextlib.aclosing(self._stream(request, attempt, watch)) as chunks:
                    async for chunk in chunks:
                        # Os bytes são reservados antes de gravar, na mesma passada do laço
                        # (sem espera no meio): envios ao mesmo tempo nunca passam das cotas.
                        if (
                            size + len(chunk) > limits.max_artifact_bytes
                            or attempt.artifact_bytes + len(chunk) > limits.max_attempt_bytes
                        ):
                            return _error(413, "artefato_grande_demais")
                        attempt.artifact_bytes += len(chunk)
                        size += len(chunk)
                        digest.update(chunk)
                        await self._disk(handle.write, chunk)
                await self._disk(handle.flush)
            if attempt.closed:
                raise _Gone
            kept = True
        except ClientDisconnect:
            return Response(status_code=400)
        except OSError as exc:
            logger.error("artefato de %s não foi gravado: %s", attempt.run_id, type(exc).__name__)
            return _error(500, "artefato_nao_gravado")
        finally:
            if not kept:
                attempt.artifacts -= 1
                attempt.artifact_bytes -= size
                with contextlib.suppress(OSError):
                    path.unlink()
            attempt._uploading.discard(name)
        return JSONResponse(
            {"nome": name, "tamanho": size, "sha256": digest.hexdigest()}, status_code=201
        )

    async def _result(self, request: Request) -> Response:
        return await self._guarded(request, self._store_result)

    async def _store_result(
        self, request: Request, attempt: ChannelAttempt, watch: asyncio.Timeout
    ) -> Response:
        if attempt._result_lock.locked():
            return _error(429, "resultado_em_andamento")
        async with attempt._result_lock:
            if attempt.result_received.is_set():
                return _error(409, "resultado_ja_entregue")
            body = await self._read_limited(request, attempt, watch, self.limits.max_result_bytes)
            if body is None:
                return _error(413, "resultado_grande_demais")
            if attempt.closed:
                raise _Gone
            try:
                await self._disk(_write_atomic, attempt.result_path, body)
            except OSError as exc:
                logger.error(
                    "resultado de %s não foi gravado: %s", attempt.run_id, type(exc).__name__
                )
                return _error(500, "resultado_nao_gravado")
            attempt.result_received.set()
        return Response(status_code=204)


class _FileStream(Response):
    """Um arquivo de entrada para o pod, em pedaços, que acaba se a tentativa acabar."""

    def __init__(self, channel: Channel, attempt: ChannelAttempt, fd: int, size: int) -> None:
        super().__init__(
            status_code=200,
            media_type="application/octet-stream",
            headers={**_NO_STORE, "Content-Length": str(size)},
        )
        self._channel = channel
        self._attempt = attempt
        self._fd = fd

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        attempt, fd = self._attempt, self._fd
        try:
            async with asyncio.timeout(None) as watch:
                attempt._watches.add(watch)
                try:
                    if attempt.closed:
                        return
                    await send(
                        {
                            "type": "http.response.start",
                            "status": self.status_code,
                            "headers": self.raw_headers,
                        }
                    )
                    while not attempt.closed:
                        chunk = await self._channel._disk(os.read, fd, _CHUNK)
                        await send(
                            {"type": "http.response.body", "body": chunk, "more_body": bool(chunk)}
                        )
                        if not chunk:
                            break
                finally:
                    attempt._watches.discard(watch)
        except TimeoutError, OSError:
            # A tentativa acabou no meio (ou o disco falhou): a resposta fica incompleta.
            return
        finally:
            os.close(fd)
            attempt._leave()


def _unauthorized() -> Response:
    return JSONResponse(
        {"erro": "bilhete_invalido"},
        status_code=401,
        headers={**_NO_STORE, "WWW-Authenticate": "Bearer"},
    )


def _error(status: int, code: str) -> Response:
    return JSONResponse({"erro": code}, status_code=status, headers=_NO_STORE)


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()


def _write_atomic(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


class _EmbeddedServer(uvicorn.Server):
    """O uvicorn do canal, dentro do processo do serviço: os sinais ficam com o serviço."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _listening_socket(host: str | None, port: int) -> socket.socket:
    """A porta do canal: sem ``host``, nas duas pilhas (IPv4 e IPv6) quando o sistema deixa.

    O endereço que vai para os pods leva o IP do pod do despachante, que pode ser IPv6
    num cluster só IPv6; ouvir só em ``0.0.0.0`` deixaria esses pods sem canal.
    """
    if host is None:
        if socket.has_dualstack_ipv6():
            return socket.create_server(
                ("", port), family=socket.AF_INET6, dualstack_ipv6=True, backlog=256
            )
        host = "0.0.0.0"  # noqa: S104 - os pods chegam pela rede do cluster
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=256)


class ChannelServer:
    """Serve o canal numa porta própria (``RAPA_CANAL_PORTA``) do processo do despachante.

    Sem log de acesso (os endereços têm a execução e a tentativa) e sem os cabeçalhos
    ``X-Forwarded-*`` (os pods falam direto com o despachante). Sem ``host``, ouve nas
    duas pilhas (IPv4 e IPv6) quando o sistema deixa, senão em ``0.0.0.0``.
    """

    def __init__(self, channel: Channel, *, host: str | None = None, port: int = 8081) -> None:
        self.channel = channel
        self.host = host
        self.port = port
        self._server: _EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, *, timeout_s: float = 10.0) -> int:
        """Abre a porta e devolve o número dela (útil com ``port=0``)."""
        if self._task is not None:
            raise RuntimeError("o canal já foi iniciado")
        sock = _listening_socket(self.host, self.port)
        try:
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            self.channel.app,
            lifespan="off",
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            server_header=False,
            date_header=False,
            timeout_keep_alive=30,
            # Um pod não toma todas as conexões do canal (as demais recebem 503).
            limit_concurrency=self.channel.limits.max_connections,
            # No encerramento, uma espera longa que sobrou não segura o serviço.
            timeout_graceful_shutdown=5,
        )
        server = _EmbeddedServer(config)
        self._server = server
        self._task = asyncio.create_task(self._serve(server, sock), name="canal-do-despachante")
        loop = asyncio.get_running_loop()
        give_up = loop.time() + timeout_s
        while not server.started:
            if self._task.done() or loop.time() >= give_up:
                await self.close()
                raise RuntimeError("o canal do despachante não abriu")
            await asyncio.sleep(0.02)
        self.port = port
        return port

    @staticmethod
    async def _serve(server: _EmbeddedServer, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit:
            # O uvicorn sai do processo num erro de subida: aqui, só o canal para.
            logger.error("o canal do despachante parou na subida")
        finally:
            sock.close()

    async def close(self, *, timeout_s: float = 10.0) -> None:
        server, task = self._server, self._task
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[int]:
        port = await self.start()
        try:
            yield port
        finally:
            await self.close()
