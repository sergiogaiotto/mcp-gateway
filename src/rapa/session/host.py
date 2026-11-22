"""Hospedeiro de uma sessão de gravação: um Chromium, o gravador e o canal de controle.

Uso (pelo lançador do plano de controle, nunca à mão)::

    python -m rapa.session.host < config.json
    python -m rapa.session.host --config /var/run/rapa/sessao/config.json

A configuração (:class:`HostConfig`) chega numa linha JSON pela entrada padrão
(o lançador local) ou num arquivo (o Secret montado no pod da sessão), com o token
da sessão: nada sensível vai na linha de comando, que outros processos da máquina
enxergam. Quando o canal está pronto, o hospedeiro escreve uma única linha na saída
padrão, ``{"pronto": true, "porta": N}``; no pod, quem sabe que ele está pronto é a
prontidão TCP da porta do canal.

O hospedeiro:

1. abre um Chromium sem janela num perfil novo (pasta temporária 0700), com o
   gravador (:class:`rapa.recorder.Recorder`) ligado ao contexto, e abre o
   endereço inicial como se a pessoa o tivesse digitado;
2. serve um websocket em ``listen_host:listen_port`` que só aceita o plano de
   controle, com ``Authorization: Bearer <token>`` (comparação em tempo
   constante), uma conexão por vez;
3. manda as imagens da tela pelo ``Page.startScreencast`` do CDP (JPEG, do
   tamanho da área visível, cada quadro confirmado ao navegador e os
   intermediários descartados sob pressão, :class:`rapa.session.protocol.FrameSender`),
   o endereço e o título da página;
4. recebe mouse, roda, teclado (o texto pelo ``Input.insertText``, para acentos e
   teclas mortas; dos atalhos, só os de edição, :func:`rapa.session.keys.key_commands`),
   tamanho, navegação (só http e https) e respostas a diálogos;
5. no ``parar``, para o gravador e devolve a gravação (já limpa de segredos pelo
   gravador) e encerra;
6. encerra sozinho depois de ``idle_timeout_s`` sem entrada (10 min) ou de
   ``max_duration_s`` (60 min), num SIGTERM ou num erro.

O navegador nunca sai da web: toda conexão passa pela guarda da saída
(:mod:`rapa.session.egress`, que recusa a própria máquina, o link-local e o
metadata da nuvem), o QUIC fica desligado e o WebRTC só usa o que passa pelo
proxy; uma aba com página interna do Chromium (``chrome://``, ``file://``,
``view-source:``) fecha sem aparecer para a pessoa.

Em qualquer saída, o navegador fecha e a pasta do perfil é apagada (se o
hospedeiro morrer sem poder apagá-la, o lançador apaga). O hospedeiro nunca grava
quadros nem segredos em disco ou no log: o log só leva mensagens fixas e nomes de
tipos de erro.
"""

import argparse
import asyncio
import base64
import contextlib
import hmac
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    BrowserContext,
    CDPSession,
    Dialog,
    Frame,
    Page,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from rapa.recorder.recorder import Recorder, RecorderError, redact_url
from rapa.runner.browser import LOCALE, TIMEZONE, resolve_executable
from rapa.session.egress import (
    BROWSER_NETWORK_ARGS,
    PROXY_SWITCHES,
    WEBRTC_OFF_SCRIPT,
    EgressGuard,
    EgressPolicy,
    without_proxy_switches,
)
from rapa.session.keys import MouseState, key_commands, text_commands, wheel_commands
from rapa.session.protocol import (
    MAX_CLIENT_MESSAGE,
    MAX_HEIGHT,
    MAX_WIDTH,
    MIN_HEIGHT,
    MIN_WIDTH,
    ClientMessage,
    DialogAnswer,
    FrameSender,
    KeyMessage,
    MouseMessage,
    NavigateMessage,
    ProtocolError,
    ResizeMessage,
    StopMessage,
    TextMessage,
    WheelMessage,
    check_web_url,
    dump_event,
    parse_client_message,
)

__all__ = ["EndReason", "HostConfig", "SessionHost", "main"]

logger = logging.getLogger("rapa.session.host")

#: Espera pela resposta da pessoa a um diálogo da página antes de recusá-lo.
DIALOG_TIMEOUT_S = 60.0
#: Espera pela carga de uma página pedida pela pessoa.
NAVIGATION_TIMEOUT_S = 30.0
#: Entradas que esperam o navegador; acima disso, os movimentos do mouse são descartados.
_INPUT_QUEUE = 512
_PROFILE_PREFIX = "rapa-sessao-"
#: Esquemas que a aba da pessoa pode mostrar; o resto (file:, chrome:, view-source:...) fecha.
_WEB_SCHEMES = frozenset({"http", "https", "about", "blob", "data", "chrome-error"})
#: Uma página que falha até este tempo depois de uma conexão recusada pela guarda falhou
#: por ela (o Chromium mostra só um erro de conexão).
_REFUSAL_WINDOW_S = 3.0
#: Intervalo mínimo entre dois avisos de endereço interno recusado.
_REFUSAL_WARNING_S = 10.0
#: Os argumentos do operador que decidem por onde o navegador sai nunca valem para a
#: sessão (:data:`rapa.session.egress.PROXY_SWITCHES`): tirariam a guarda do caminho.
_PROXY_SWITCHES = PROXY_SWITCHES


def _session_args(args: tuple[str, ...]) -> list[str]:
    """Os argumentos do operador sem os que mudam o proxy do navegador."""
    return without_proxy_switches(args)


_REFUSED_TEXT = (
    "O navegador da gravação não abre endereços internos do servidor (a própria máquina, o "
    "metadata da nuvem e as redes internas não permitidas). Use o endereço público do portal."
)


class EndReason(StrEnum):
    """Por que a sessão terminou (vai no evento ``fim``)."""

    STOPPED = "parada"
    IDLE = "ociosa"
    TIME_UP = "tempo_esgotado"
    SIGNAL = "encerrada"
    ERROR = "erro"


class HostConfig(BaseModel):
    """Configuração do hospedeiro, recebida pela entrada padrão."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Token da sessão (pelo menos 32 caracteres); nunca vai para o log nem para o ``repr``.
    token: str = Field(min_length=32, max_length=512, repr=False)
    start_url: str = Field(min_length=1, max_length=2000)
    #: ``None``: todas as interfaces, em IPv4 e em IPv6 (o pod da sessão, cujo IP pode ser
    #: IPv6); o lançador local usa ``127.0.0.1``.
    listen_host: str | None = "127.0.0.1"
    listen_port: int = Field(default=0, ge=0, le=65535)
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    width: int = Field(default=1280, ge=MIN_WIDTH, le=MAX_WIDTH)
    height: int = Field(default=800, ge=MIN_HEIGHT, le=MAX_HEIGHT)
    idle_timeout_s: float = Field(default=600.0, gt=0, le=86_400)
    max_duration_s: float = Field(default=3600.0, gt=0, le=86_400)
    jpeg_quality: int = Field(default=60, ge=20, le=95)
    #: Onde criar a pasta temporária do perfil (padrão: a pasta temporária do sistema).
    profile_parent: str | None = None
    #: Rede interna fechada (menos ``allowed_networks``) e o proxy de saída; sem ela, só a
    #: própria máquina, o link-local e o metadata ficam fechados (ver :mod:`rapa.session.egress`).
    egress_policy: bool = False
    #: Redes internas que o navegador alcança, com ``egress_policy`` (``10.20.0.0/16``).
    allowed_networks: tuple[str, ...] = Field(default=(), max_length=256)
    #: Proxy HTTP CONNECT de saída, com ``egress_policy`` (``http://proxy.interno:3128``, ou
    #: com usuário e senha); nunca no ``repr``.
    upstream_proxy: str | None = Field(default=None, max_length=2000, repr=False)

    @field_validator("start_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_web_url(value)

    @model_validator(mode="after")
    def _check_egress(self) -> HostConfig:
        if (self.allowed_networks or self.upstream_proxy) and not self.egress_policy:
            raise ValueError("as redes internas e o proxy de saída só valem com egress_policy")
        self.policy()  # redes e proxy no formato certo (ValueError, sem repetir segredo)
        return self

    def policy(self) -> EgressPolicy | None:
        """A política da guarda da saída, se houver."""
        if not self.egress_policy:
            return None
        return EgressPolicy.parse(self.allowed_networks, self.upstream_proxy)


type _Factory = Callable[[], Coroutine[Any, Any, None]]


class SessionHost:
    """Uma sessão: o navegador, o gravador e o canal de controle."""

    def __init__(self, config: HostConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._expected = f"Bearer {config.token}".encode()
        self._recorder = Recorder()
        self._context: BrowserContext | None = None
        self._pages: list[Page] = []
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._casting = False
        self._size = (config.width, config.height)
        self._screen: tuple[int, int] | None = None
        self._mouse = MouseState()
        self._client: ServerConnection | None = None
        self._sender: FrameSender | None = None
        self._inputs: asyncio.Queue[ClientMessage] | None = None
        self._dialog: asyncio.Future[DialogAnswer] | None = None
        self._ended: asyncio.Future[EndReason] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = 0.0
        self._last_input = 0.0
        self._warning: str | None = None
        self._last_refusal = float("-inf")
        self._last_refusal_warning = float("-inf")
        self._stopping = False
        #: O processo que abriu este (a API, pelo lançador): se ele morrer, a sessão acaba.
        self._parent = os.getppid()
        #: Pasta do perfil (apagada no fim); exposta para os testes conferirem.
        self.profile_dir: Path | None = None
        self.port: int | None = None
        self.frames = 0
        self.rejected = 0

    @property
    def page(self) -> Page | None:
        """A aba que a pessoa vê agora (para diagnóstico e testes; só leitura)."""
        return self._page

    # -- ciclo de vida ---------------------------------------------------------

    def finish(self, reason: EndReason) -> None:
        """Pede o fim da sessão (pode ser chamado de um sinal)."""
        if self._ended is not None and not self._ended.done():
            self._ended.set_result(reason)

    async def run(self, ready: Callable[[int], None]) -> EndReason:
        """Roda a sessão até o fim e devolve o motivo; ``ready`` recebe a porta do canal."""
        loop = asyncio.get_running_loop()
        self._ended = loop.create_future()
        self._started = self._last_input = self._clock()
        parent = self.config.profile_parent
        self.profile_dir = Path(tempfile.mkdtemp(prefix=_PROFILE_PREFIX, dir=parent))
        try:
            return await self._run(ready)
        finally:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            shutil.rmtree(self.profile_dir, ignore_errors=True)

    async def _run(self, ready: Callable[[int], None]) -> EndReason:
        if self._ended is None or self.profile_dir is None:  # pragma: no cover - só por run()
            raise RuntimeError("a sessão não foi iniciada por run()")
        egress = EgressGuard.from_browser_args(
            self.config.browser_args, on_refused=self._refused, policy=self.config.policy()
        )
        egress_port = await egress.start()
        try:
            return await self._run_browser(ready, egress_port, self.profile_dir, self._ended)
        finally:
            await egress.close()

    async def _run_browser(
        self,
        ready: Callable[[int], None],
        egress_port: int,
        profile_dir: Path,
        ended: asyncio.Future[EndReason],
    ) -> EndReason:
        config = self.config
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile_dir / "perfil"),
                executable_path=resolve_executable(config.chromium),
                headless=True,
                # Os do operador primeiro: os que tiram o UDP do caminho não são desfeitos.
                args=[*_session_args(config.browser_args), *BROWSER_NETWORK_ARGS],
                viewport={"width": config.width, "height": config.height},
                locale=LOCALE,
                timezone_id=TIMEZONE,
                accept_downloads=True,
                downloads_path=str(profile_dir / "downloads"),
                # Toda conexão TCP do navegador (também os saltos de um redirecionamento,
                # iframes, workers e websockets; o Playwright põe <-loopback>) passa pela
                # guarda da saída (rapa.session.egress).
                proxy={"server": f"socks5://127.0.0.1:{egress_port}"},
            )
            self._context = context
            try:
                await context.add_init_script(WEBRTC_OFF_SCRIPT)
                await self._recorder.start(context)
                context.on("page", self._on_page)
                first = context.pages[0] if context.pages else await context.new_page()
                self._register(first)
                await self._activate(first)
                async with serve(
                    self._handle,
                    config.listen_host,
                    config.listen_port,
                    process_request=self._authorize,
                    max_size=MAX_CLIENT_MESSAGE,
                    compression=None,
                    server_header=None,
                    ping_interval=10,
                    ping_timeout=20,
                    close_timeout=5,
                    logger=logging.getLogger("rapa.session.host.canal"),
                ) as server:
                    self.port = _port(server)
                    ready(self.port)
                    self._spawn(lambda: self._navigate(config.start_url, first=True))
                    self._spawn(self._watchdog)
                    reason = await ended
                    await self._say_goodbye(reason)
            finally:
                self._stopping = True
                await self._resolve_dialog(None)
                with contextlib.suppress(Exception):
                    await context.close()
        return reason

    async def _say_goodbye(self, reason: EndReason) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(Exception):
            if reason is not EndReason.STOPPED:
                await client.send(dump_event("fim", motivo=reason.value))
            await client.close(1000, "fim")

    async def _watchdog(self) -> None:
        if self._ended is None:  # pragma: no cover - só por run()
            return
        config = self.config
        while not self._ended.done():
            if os.getppid() != self._parent:
                # A API que abriu a sessão caiu: ninguém mais tem o token, e o banco já vai
                # dar a sessão por encerrada; o navegador e o perfil não ficam para trás.
                self.finish(EndReason.SIGNAL)
                return
            now = self._clock()
            if now - self._last_input >= config.idle_timeout_s:
                self.finish(EndReason.IDLE)
                return
            if now - self._started >= config.max_duration_s:
                self.finish(EndReason.TIME_UP)
                return
            await asyncio.sleep(min(1.0, config.idle_timeout_s / 4, config.max_duration_s / 4))

    def _spawn(self, factory: _Factory) -> None:
        task = asyncio.get_running_loop().create_task(factory())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Só o tipo: a mensagem pode repetir conteúdo da página.
            logger.warning("tarefa da sessão falhou: %s", type(task.exception()).__name__)

    # -- páginas ---------------------------------------------------------------

    def _register(self, page: Page) -> None:
        if page in self._pages:
            return
        self._pages.append(page)
        page.on("close", self._on_close)
        page.on("dialog", self._on_dialog)
        page.on("framenavigated", self._on_navigated)
        page.on("load", self._on_load)

    @staticmethod
    def _is_web(page: Page) -> bool:
        return urlsplit(page.url).scheme in _WEB_SCHEMES

    def _on_page(self, page: Page) -> None:
        # Uma aba nova (um link com target=_blank) passa a ser a que a pessoa vê; uma página
        # interna do Chromium (chrome://, file://) nunca: fecha sem aparecer.
        if not self._is_web(page):
            self._spawn(page.close)
            return
        self._register(page)
        self._spawn(lambda: self._activate(page))

    def _on_close(self, page: Page) -> None:
        if page in self._pages:
            self._pages.remove(page)
        if page is self._page and not self._stopping:
            self._page = None
            self._cdp = None
            self._spawn(self._activate_last)

    async def _activate_last(self) -> None:
        if self._pages:
            await self._activate(self._pages[-1])
        elif self._context is not None and not self._stopping:
            # A pessoa fechou a última aba: abre uma vazia para a sessão continuar.
            page = await self._context.new_page()
            self._register(page)
            await self._activate(page)

    async def _activate(self, page: Page) -> None:
        if self._stopping or page.is_closed() or self._context is None or not self._is_web(page):
            return
        old = self._cdp
        self._cdp, self._casting, self._screen = None, False, None
        if old is not None:
            with contextlib.suppress(Exception):
                await old.send("Page.stopScreencast")
            with contextlib.suppress(Exception):
                await old.detach()
        self._page = page
        with contextlib.suppress(PlaywrightError):
            if page.viewport_size != {"width": self._size[0], "height": self._size[1]}:
                await page.set_viewport_size({"width": self._size[0], "height": self._size[1]})
        try:
            cdp = await self._context.new_cdp_session(page)
        except PlaywrightError:
            return
        cdp.on("Page.screencastFrame", lambda params: self._on_frame(cdp, params))
        self._cdp = cdp
        if self._client is not None:
            await self._start_screencast()
            await self._send_page_info()

    def _on_navigated(self, frame: Frame) -> None:
        page = frame.page
        if frame == page.main_frame and not self._is_web(page):
            if page is self._page:
                # Nenhum quadro dela sai; ao fechar, a aba anterior volta a ser a da pessoa.
                self._cdp, self._casting = None, False
            self._spawn(page.close)
            return
        if self._page is not None and frame == self._page.main_frame:
            if page.url.startswith("chrome-error:"):
                # Um link, um redirecionamento ou o "navegar" para um endereço interno.
                self._warn_refused()
            self._spawn(self._send_page_info)

    def _on_load(self, page: Page) -> None:
        if page is self._page:
            self._spawn(self._send_page_info)

    async def _send_page_info(self) -> None:
        page, client = self._page, self._client
        if page is None or client is None or not self._is_web(page):
            return
        url = page.url
        shown = redact_url(url, set()) if url.startswith(("http://", "https://")) else url
        title = ""
        with contextlib.suppress(Exception):
            title = await asyncio.wait_for(page.title(), timeout=2)
        with contextlib.suppress(Exception):
            await client.send(dump_event("pagina", url=shown[:2000], titulo=title[:300]))

    async def _navigate(self, url: str, *, first: bool = False) -> None:
        page = self._page
        if page is None:
            return
        try:
            await asyncio.wait_for(self._recorder.goto(page, url), timeout=NAVIGATION_TIMEOUT_S)
        except (PlaywrightError, TimeoutError) as exc:
            logger.info("a página pedida não abriu: %s", type(exc).__name__)
            if self._refused_recently():
                await self._warn(_REFUSED_TEXT)
            else:
                await self._warn("Não foi possível abrir o endereço. Confira e tente de novo.")
            if first:
                self._warning = "O endereço inicial não abriu. Digite outro endereço."

    def _refused(self, _host: str) -> None:
        """A guarda da saída recusou uma conexão (de uma página ou do próprio Chromium)."""
        self._last_refusal = self._clock()

    def _refused_recently(self) -> bool:
        return self._clock() - self._last_refusal < _REFUSAL_WINDOW_S

    def _warn_refused(self) -> None:
        """A página da pessoa falhou logo depois de uma recusa: diz por quê (no máximo a cada 10 s)."""
        now = self._clock()
        if not self._refused_recently() or now - self._last_refusal_warning < _REFUSAL_WARNING_S:
            return
        self._last_refusal_warning = now
        self._spawn(lambda: self._warn(_REFUSED_TEXT))

    async def _warn(self, text: str) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.send(dump_event("aviso", mensagem=text))

    # -- tela ------------------------------------------------------------------

    async def _start_screencast(self) -> None:
        cdp = self._cdp
        if cdp is None or self._casting or self._client is None:
            return
        width, height = self._size
        try:
            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": self.config.jpeg_quality,
                    "maxWidth": width,
                    "maxHeight": height,
                    "everyNthFrame": 1,
                },
            )
        except PlaywrightError:
            return
        self._casting = cdp is self._cdp

    async def _stop_screencast(self) -> None:
        cdp, self._casting = self._cdp, False
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.send("Page.stopScreencast")

    def _on_frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        self._spawn(lambda: self._frame(cdp, params))

    async def _frame(self, cdp: CDPSession, params: dict[str, Any]) -> None:
        try:
            data = params.get("data")
            sender, client = self._sender, self._client
            if cdp is not self._cdp or sender is None or client is None:
                return
            if not isinstance(data, str):
                return
            metadata = params.get("metadata")
            if isinstance(metadata, dict):
                screen = (
                    int(metadata.get("deviceWidth") or self._size[0]),
                    int(metadata.get("deviceHeight") or self._size[1]),
                )
                if screen != self._screen:
                    self._screen = screen
                    with contextlib.suppress(Exception):
                        await client.send(dump_event("tela", largura=screen[0], altura=screen[1]))
            sender.offer(base64.b64decode(data))
            self.frames += 1
        finally:
            # Todo quadro é confirmado, também o descartado: o navegador manda o próximo.
            with contextlib.suppress(Exception):
                await cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})

    # -- canal de controle -----------------------------------------------------

    def _authorize(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path != "/":
            return connection.respond(404, "nao encontrado\n")
        header = request.headers.get("Authorization", "")
        if not hmac.compare_digest(header.encode(), self._expected):
            return connection.respond(401, "nao autorizado\n")
        if self._client is not None or self._stopping:
            return connection.respond(409, "a sessao ja tem uma conexao\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        if self._client is not None or self._stopping:
            await connection.close(1013, "ocupada")
            return
        self._client = connection
        self._sender = FrameSender(connection.send)
        self._inputs = asyncio.Queue(_INPUT_QUEUE)
        worker = asyncio.get_running_loop().create_task(self._input_worker(self._inputs))
        try:
            width, height = self._size
            await connection.send(dump_event("pronta", largura=width, altura=height))
            if self._warning is not None:
                await connection.send(dump_event("aviso", mensagem=self._warning))
                self._warning = None
            await self._start_screencast()
            await self._send_page_info()
            async for raw in connection:
                try:
                    message = parse_client_message(raw)
                except ProtocolError:
                    self.rejected += 1
                    continue
                if isinstance(message, StopMessage):
                    await self._stop(connection)
                    break
                if not isinstance(message, ResizeMessage):
                    self._last_input = self._clock()
                if isinstance(message, DialogAnswer):
                    await self._resolve_dialog(message)
                    continue
                self._enqueue(message)
        except ConnectionClosed:
            pass
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            sender, self._sender, self._client, self._inputs = self._sender, None, None, None
            if sender is not None:
                await sender.close()
            if not self._stopping:
                await self._stop_screencast()

    def _enqueue(self, message: ClientMessage) -> None:
        queue = self._inputs
        if queue is None:
            return
        if queue.full():
            if isinstance(message, MouseMessage) and message.acao == "mover":
                return  # o próximo movimento leva a posição certa
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait(message)

    async def _input_worker(self, queue: asyncio.Queue[ClientMessage]) -> None:
        # As entradas vão em ordem, fora do laço do canal: enquanto um clique espera um
        # diálogo da página, a resposta da pessoa ao diálogo ainda chega.
        while True:
            message = await queue.get()
            try:
                await self._apply(message)
            except PlaywrightError as exc:
                logger.info("entrada não aplicada: %s", type(exc).__name__)

    async def _apply(self, message: ClientMessage) -> None:
        width, height = self._size
        match message:
            case MouseMessage():
                await self._send_all(self._mouse.commands(message, width=width, height=height))
            case WheelMessage():
                await self._send_all(wheel_commands(message, width=width, height=height))
            case KeyMessage():
                await self._send_all(key_commands(message))
            case TextMessage():
                await self._send_all(text_commands(message))
            case ResizeMessage():
                await self._resize(*message.clamped())
            case NavigateMessage(url=url):
                self._spawn(lambda: self._navigate(url))
            case _:
                pass

    async def _send_all(self, commands: list[tuple[str, dict[str, Any]]]) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        for method, params in commands:
            await cdp.send(method, params)

    async def _resize(self, width: int, height: int) -> None:
        if (width, height) == self._size:
            return
        self._size = (width, height)
        page = self._page
        if page is not None:
            await page.set_viewport_size({"width": width, "height": height})
        await self._stop_screencast()
        await self._start_screencast()

    async def _stop(self, connection: ServerConnection) -> None:
        """Para o gravador, devolve a gravação limpa e encerra a sessão."""
        self._stopping = True
        await self._resolve_dialog(None)
        await self._stop_screencast()
        try:
            recording = await self._recorder.stop()
        except RecorderError as exc:
            # Mensagens fixas do gravador, em pt-BR, sem conteúdo gravado.
            await connection.send(
                dump_event("fim", motivo=EndReason.ERROR.value, mensagem=str(exc))
            )
            self.finish(EndReason.ERROR)
            return
        except Exception as exc:
            logger.error("falha ao parar o gravador: %s", type(exc).__name__)
            await connection.send(
                dump_event(
                    "fim",
                    motivo=EndReason.ERROR.value,
                    mensagem="Não foi possível fechar a gravação; grave de novo.",
                )
            )
            self.finish(EndReason.ERROR)
            return
        await connection.send(dump_event("gravacao", gravacao=recording.model_dump(mode="json")))
        await connection.send(dump_event("fim", motivo=EndReason.STOPPED.value))
        self.finish(EndReason.STOPPED)

    # -- diálogos da página ----------------------------------------------------

    def _on_dialog(self, dialog: Dialog) -> None:
        self._spawn(lambda: self._dialog_flow(dialog))

    async def _dialog_flow(self, dialog: Dialog) -> None:
        client = self._client
        answer: DialogAnswer | None = None
        if client is not None and not self._stopping and self._dialog is None:
            future: asyncio.Future[DialogAnswer] = asyncio.get_running_loop().create_future()
            self._dialog = future
            try:
                await client.send(
                    dump_event(
                        "dialogo",
                        tipo_dialogo=dialog.type,
                        texto=dialog.message[:2000],
                        padrao=(dialog.default_value or "")[:2000],
                    )
                )
                answer = await asyncio.wait_for(future, timeout=DIALOG_TIMEOUT_S)
            except Exception:
                answer = None
            finally:
                self._dialog = None
        with contextlib.suppress(PlaywrightError):
            if answer is not None and answer.aceitar:
                if dialog.type == "prompt":
                    await dialog.accept(answer.texto or "")
                else:
                    await dialog.accept()
            elif dialog.type == "alert":
                await dialog.accept()
            else:
                await dialog.dismiss()

    async def _resolve_dialog(self, answer: DialogAnswer | None) -> None:
        future = self._dialog
        if future is None or future.done():
            return
        if answer is None:
            future.cancel()
        else:
            future.set_result(answer)


def _port(server: Server) -> int:
    for sock in server.sockets:
        name = sock.getsockname()
        if isinstance(name, tuple) and len(name) >= 2:
            return int(name[1])
    raise RuntimeError("o canal da sessão não abriu porta")


#: Tamanho máximo da configuração (pela entrada padrão ou pelo arquivo).
_MAX_CONFIG = 64 * 1024


def _parse_config(raw: str | bytes) -> HostConfig:
    try:
        return HostConfig.model_validate(json.loads(raw))
    except ValueError, ValidationError:
        # Nunca repetir a configuração: ela tem o token da sessão.
        raise SystemExit("rapa.session.host: configuração inválida") from None


def _read_config(stream: Any) -> HostConfig:
    return _parse_config(stream.readline(_MAX_CONFIG))


def _read_config_file(path: Path) -> HostConfig:
    """A configuração num arquivo (o Secret montado no pod da sessão)."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG + 1)
    except OSError:
        raise SystemExit("rapa.session.host: o arquivo de configuração não pôde ser lido") from None
    if len(raw) > _MAX_CONFIG:
        raise SystemExit("rapa.session.host: configuração grande demais")
    return _parse_config(raw)


def _with_image_args(config: HostConfig, env: Mapping[str, str]) -> HostConfig:
    """Os argumentos do navegador da imagem (``RAPA_ARGS_NAVEGADOR`` do pod) antes dos da sessão.

    O lançador local passa só o ambiente mínimo (sem ``RAPA_*``), então isto só vale no
    pod da sessão, onde a imagem do executor diz como o Chromium roda nela.
    """
    image = tuple(shlex.split(env.get("RAPA_ARGS_NAVEGADOR", "")))
    if not image:
        return config
    return config.model_copy(update={"browser_args": (*image, *config.browser_args)})


def main(argv: list[str] | None = None) -> int:
    """Lê a configuração, roda a sessão e devolve o código de saída.

    A configuração chega pela entrada padrão (o lançador local) ou, com ``--config``,
    de um arquivo (o Secret montado no pod da sessão, :mod:`rapa.control.sessions.launcher`).
    No pod, o lançador sabe que o hospedeiro está pronto quando a porta do canal aceita
    conexões (a prontidão TCP do pod); a linha de pronto sai do mesmo jeito.
    """
    parser = argparse.ArgumentParser(prog="python -m rapa.session.host")
    parser.add_argument("--config", type=Path, default=None, help="arquivo da configuração")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = _read_config(sys.stdin) if args.config is None else _read_config_file(args.config)
    config = _with_image_args(config, os.environ)
    host = SessionHost(config)

    def ready(port: int) -> None:
        sys.stdout.write(json.dumps({"pronto": True, "porta": port}) + "\n")
        sys.stdout.flush()

    async def run() -> EndReason:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, host.finish, EndReason.SIGNAL)
        return await host.run(ready)

    try:
        reason = asyncio.run(run())
    except Exception as exc:
        logger.error("a sessão falhou: %s", type(exc).__name__)
        return 1
    return 0 if reason is not EndReason.ERROR else 1


if __name__ == "__main__":
    sys.exit(main())
