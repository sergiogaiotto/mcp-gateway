"""Mensagens do canal da sessão ao vivo e o envio de quadros sem acúmulo.

O mesmo formato vale nos dois trechos: entre o navegador da pessoa e o plano de
controle (``/v1/sessoes/{id}/ao-vivo``) e entre o plano de controle e o
hospedeiro da sessão. Mensagens de texto são JSON com o campo ``tipo``; as
mensagens binárias são quadros JPEG da tela (só no sentido de quem mostra).

Entrada (de quem assiste para o navegador da nuvem):

- ``mouse``: ``acao`` (``mover``, ``apertar`` ou ``soltar``), ``x`` e ``y`` em
  pixels da página, ``botao`` e ``cliques``;
- ``roda``: ``x``, ``y``, ``dx`` e ``dy``;
- ``tecla``: ``acao`` (``descer`` ou ``subir``), ``tecla`` (o ``key`` do DOM) e
  ``codigo`` (o ``code`` do DOM), só para teclas especiais e atalhos;
- ``texto``: texto já composto (acentos, teclas mortas, IME e colar);
- ``tamanho``: ``largura`` e ``altura`` da área visível;
- ``navegar``: ``url`` (só http e https);
- ``dialogo``: resposta a um diálogo da página (``aceitar`` e ``texto``);
- ``parar`` (só o plano de controle manda): devolve a gravação e encerra.

Todas levam ``modificadores`` quando faz sentido, no formato do CDP (Alt = 1,
Ctrl = 2, Meta = 4, Shift = 8).

Saída (do navegador da nuvem para quem assiste): ``pronta``, ``tela``,
``pagina``, ``aviso``, ``dialogo``, ``fim`` e, só para o plano de controle,
``gravacao``. O plano de controle acrescenta ``sessao`` (o prazo) ao falar com
o navegador da pessoa.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

__all__ = [
    "MAX_CLIENT_MESSAGE",
    "MAX_HEIGHT",
    "MAX_WIDTH",
    "MIN_HEIGHT",
    "MIN_WIDTH",
    "RELAYED_HOST_EVENTS",
    "VIEWER_MESSAGES",
    "ClientMessage",
    "DialogAnswer",
    "FrameSender",
    "KeyMessage",
    "MouseMessage",
    "NavigateMessage",
    "ProtocolError",
    "ResizeMessage",
    "StopMessage",
    "TextMessage",
    "WheelMessage",
    "check_web_url",
    "dump_event",
    "parse_client_message",
    "parse_host_event",
]

#: Maior mensagem de texto aceita de quem assiste (colar um texto longo cabe).
MAX_CLIENT_MESSAGE = 64 * 1024
MIN_WIDTH, MAX_WIDTH = 320, 1920
MIN_HEIGHT, MAX_HEIGHT = 240, 1200
_MAX_COORD = 10_000.0
_MAX_DELTA = 5_000.0
_MAX_URL = 2_000


class ProtocolError(ValueError):
    """Mensagem fora do formato do canal (a mensagem nunca repete o conteúdo)."""


def check_web_url(value: str) -> str:
    """Só endereços http e https, com domínio e sem usuário ou senha."""
    text = value.strip()
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("o endereço precisa começar com http:// ou https://")
    if parts.username or parts.password:
        raise ValueError("o endereço não pode ter usuário nem senha")
    if len(text) > _MAX_URL:
        raise ValueError("o endereço é longo demais")
    return text


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_Modifiers = Annotated[int, Field(ge=0, le=15)]
_Coord = Annotated[float, Field(ge=0, le=_MAX_COORD)]


class MouseMessage(_Message):
    tipo: Literal["mouse"] = "mouse"
    acao: Literal["mover", "apertar", "soltar"]
    x: _Coord
    y: _Coord
    botao: Literal["esquerdo", "meio", "direito", "nenhum"] = "nenhum"
    cliques: int = Field(default=0, ge=0, le=3)
    modificadores: _Modifiers = 0


class WheelMessage(_Message):
    tipo: Literal["roda"] = "roda"
    x: _Coord
    y: _Coord
    dx: float = Field(default=0, ge=-_MAX_DELTA, le=_MAX_DELTA)
    dy: float = Field(default=0, ge=-_MAX_DELTA, le=_MAX_DELTA)
    modificadores: _Modifiers = 0


class KeyMessage(_Message):
    tipo: Literal["tecla"] = "tecla"
    acao: Literal["descer", "subir"]
    tecla: str = Field(min_length=1, max_length=32)
    codigo: str = Field(default="", max_length=32)
    modificadores: _Modifiers = 0
    repetida: bool = False


class TextMessage(_Message):
    tipo: Literal["texto"] = "texto"
    texto: str = Field(min_length=1, max_length=10_000)


class ResizeMessage(_Message):
    tipo: Literal["tamanho"] = "tamanho"
    largura: int = Field(ge=MIN_WIDTH, le=4 * MAX_WIDTH)
    altura: int = Field(ge=MIN_HEIGHT, le=4 * MAX_HEIGHT)

    def clamped(self) -> tuple[int, int]:
        """Tamanho dentro dos limites do navegador da nuvem."""
        return min(self.largura, MAX_WIDTH), min(self.altura, MAX_HEIGHT)


class NavigateMessage(_Message):
    tipo: Literal["navegar"] = "navegar"
    url: str = Field(min_length=1, max_length=_MAX_URL)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return check_web_url(value)


class DialogAnswer(_Message):
    tipo: Literal["dialogo"] = "dialogo"
    aceitar: bool
    texto: str | None = Field(default=None, max_length=2_000)


class StopMessage(_Message):
    tipo: Literal["parar"] = "parar"


type ClientMessage = Annotated[
    MouseMessage
    | WheelMessage
    | KeyMessage
    | TextMessage
    | ResizeMessage
    | NavigateMessage
    | DialogAnswer
    | StopMessage,
    Field(discriminator="tipo"),
]

_CLIENT: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

#: O que o navegador da pessoa pode mandar (parar só pelo REST, que guarda e audita).
VIEWER_MESSAGES = frozenset({"mouse", "roda", "tecla", "texto", "tamanho", "navegar", "dialogo"})
#: Eventos do hospedeiro repassados ao navegador da pessoa (a gravação, nunca).
RELAYED_HOST_EVENTS = frozenset({"pronta", "tela", "pagina", "aviso", "dialogo", "fim"})


def parse_client_message(raw: str | bytes) -> ClientMessage:
    """Valida uma mensagem de entrada. Levanta :class:`ProtocolError` sem repetir o conteúdo."""
    if isinstance(raw, bytes):
        raise ProtocolError("mensagens de entrada são texto JSON")
    if len(raw) > MAX_CLIENT_MESSAGE:
        raise ProtocolError("mensagem grande demais")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ProtocolError("mensagem não é JSON") from None
    try:
        return _CLIENT.validate_python(data)
    except ValidationError:
        raise ProtocolError("mensagem fora do formato") from None


def parse_host_event(raw: str) -> dict[str, Any]:
    """Lê um evento JSON do hospedeiro (só confere que é um objeto com ``tipo``)."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ProtocolError("evento não é JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("tipo"), str):
        raise ProtocolError("evento sem tipo")
    return data


def dump_event(tipo: str, **fields: Any) -> str:
    """Evento JSON do canal."""
    return json.dumps({"tipo": tipo, **fields}, ensure_ascii=False, separators=(",", ":"))


class FrameSender:
    """Envia quadros sem acumular: enquanto um envio está em andamento, só o mais novo espera.

    Quem mostra a tela nunca recebe uma fila de quadros velhos: sob pressão (rede
    lenta, navegador da pessoa ocupado), os quadros intermediários são descartados
    e o último sempre chega, então a imagem final da página nunca fica para trás.
    A memória usada é de no máximo dois quadros.
    """

    def __init__(self, send: Callable[[bytes], Awaitable[None]]) -> None:
        self._send = send
        self._pending: bytes | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.sent = 0
        self.dropped = 0

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def offer(self, frame: bytes) -> None:
        """Entrega um quadro: vai já, ou espera no lugar do que estava esperando."""
        if self._closed:
            return
        if self.busy:
            if self._pending is not None:
                self.dropped += 1
            self._pending = frame
            return
        self._task = asyncio.get_running_loop().create_task(self._pump(frame))

    async def _pump(self, frame: bytes | None) -> None:
        while frame is not None and not self._closed:
            try:
                await self._send(frame)
            except Exception:
                # A conexão caiu: quem é dono dela percebe e encerra; aqui só para.
                self._closed = True
                self._pending = None
                return
            self.sent += 1
            frame, self._pending = self._pending, None

    async def close(self) -> None:
        self._closed = True
        self._pending = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
