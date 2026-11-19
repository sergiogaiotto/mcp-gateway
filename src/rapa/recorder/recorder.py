"""Gravador da demonstração num contexto do Playwright.

Um script injetado em todos os quadros (``capture.js``) escuta, na fase de
captura, cliques, valores finais de campos, teclas Enter/Tab/Escape, listas,
caixas de seleção e envio de arquivos, e manda um descritor de cada ação por
uma binding do Playwright. Do lado do Python ficam as navegações do quadro
principal (e dos iframes), os downloads, as requisições de documento e XHR (para
detectar gravações) e o texto visível de cada página depois que ela assenta.

O valor de campos de senha e de código de uso único nunca entra na gravação, nem
o dos outros campos de texto preenchidos numa página que pede senha ou código
(o usuário e qualquer outro dado). O script marca esses campos (inclusive senha
em campo de texto mascarado por CSS e campo cujo ``aria-label`` ou
``aria-labelledby`` fala em senha) e manda o valor só para o gravador apagá-lo dos
outros textos. O gravador repete as regras do lado do Python
(``rapa.recorder.sensitivity``): marca de senha pelo rótulo e pelo nome
acessível e, ao fechar a gravação, os campos das páginas com senha ou código que
o script não viu (a senha que só aparece depois, numa SPA, ou noutro shadow root,
e o usuário de uma página que leva à página da senha). Campos de shadow roots
abertos chegam com o caminho dos hospedeiros (``shadow_path``) e um seletor que
atravessa o shadow root, que a conferência do Playwright usa; o valor de um campo
de texto de shadow root sai no ``change`` dele (que o script escuta em cada shadow
root que aparece no caminho de um evento), antes do blur e na ordem da pessoa, ou
no ``blur`` na fase de captura, antes do ouvinte de blur do campo; o campo que o
componente já tirou da página sai com o descritor e o último valor que o script
guardou dele, com a mesma marca de sigilo; e um campo de shadow root nunca é
credencial de login. Cada elemento leva um número (``serial``) que dois elementos
nunca têm igual: só edições do mesmo elemento viram um passo. A limpeza ignora
maiúsculas em tudo o que pode chegar ao robô ou ao modelo de linguagem: o texto
livre da página (títulos, texto, iframes) e, nos elementos, nomes acessíveis,
rótulos, placeholders, títulos, textos, seletores e o texto e as células da linha
da tabela (de onde sai o retrato do alvo). Só perdem o valor exato o que o
executor compara com a página do jeito que está: valores digitados ou
escolhidos, a chave da linha e endereços.
"""

import asyncio
import json
import re
import secrets
import time
import unicodedata
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Any, cast
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Download, Frame, Page, Request
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ValidationError

from rapa.recorder.models import (
    REDACTED,
    CheckEvent,
    ClickEvent,
    DownloadEvent,
    ElementDescriptor,
    FileEvent,
    FrameInfo,
    Heading,
    InputEvent,
    KeyEvent,
    LocatorProbe,
    NavigationCause,
    NavigationEvent,
    PageTextEvent,
    Recording,
    RecordingEvent,
    RequestEvent,
    SelectEvent,
)
from rapa.recorder.sensitivity import is_password_like_text, password_by_name, secret_page_inputs

__all__ = ["Recorder", "RecorderError"]

_PACKAGE = resources.files("rapa.recorder")
_CAPTURE_JS = _PACKAGE.joinpath("capture.js").read_text(encoding="utf-8")
_PAGE_TEXT_JS = _PACKAGE.joinpath("page_text.js").read_text(encoding="utf-8")
#: Valores sigilosos mais curtos que isto não são procurados nos textos.
MIN_REDACTED_CHARS = 4
#: Cada valor sigiloso entra com as formas que ele tem num endereço (até quatro).
_MAX_SECRET_VALUES = 256
#: Rodadas de corte de textos que a limpeza deixou maiores que o limite do campo.
_MAX_FIT_ROUNDS = 3
#: Edição mais longa que o script pode informar (um dia, em milissegundos).
_MAX_EDITING_MS = 86_400_000
#: Campos do evento que a limpeza só troca com o valor exato: o que a pessoa digitou
#: ou escolheu, endereços e marcas de estrutura (no resto ela ignora maiúsculas).
_EXACT_EVENT_FIELDS = frozenset(
    {
        "type",
        "url",
        "frame_url",
        "value",
        "typed",
        "label",
        "file_names",
        "key",
        "method",
        "resource_type",
        "cause",
    }
)
#: Campos do elemento trocados só com o valor exato: o endereço do link e as marcas de
#: estrutura (etiqueta, tipo, papel, autocomplete), que não são texto da página.
_EXACT_ELEMENT_FIELDS = frozenset({"tag", "input_type", "role", "autocomplete", "href"})
#: Campos de iframe trocados só com o valor exato (o endereço e a etiqueta).
_EXACT_FRAME_FIELDS = frozenset({"tag", "src"})
_STOP_JS = "() => { const stop = window[Symbol.for('rapa.recorder.stop')]; if (stop) stop(); }"
_FRAME_INFO_JS = """(el) => ({
  tag: el.tagName.toLowerCase(),
  id: el.id ? String(el.id).slice(0, 300) : null,
  name: el.getAttribute('name') ? el.getAttribute('name').slice(0, 300) : null,
  title: el.getAttribute('title') ? el.getAttribute('title').slice(0, 300) : null,
  src: el.getAttribute('src') ? el.getAttribute('src').slice(0, 500) : null,
})"""

#: Tipos de requisição que entram na gravação mesmo quando são GET.
_RECORDED_TYPES = frozenset({"document", "xhr", "fetch"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Parâmetros de endereço sempre mascarados (além dos nomes de campos sensíveis vistos).
_SENSITIVE_PARAMS = frozenset(
    {
        "senha",
        "password",
        "passwd",
        "pwd",
        "otp",
        "totp",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "secret",
    }
)
#: Roles que o ``get_by_role`` do Playwright aceita e que valem a pena conferir.
_PROBE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "option",
        "radio",
        "searchbox",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
    }
)


class RecorderError(RuntimeError):
    """Uso incorreto do gravador."""


def _is_web_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _cut_to_limits(data: dict[str, Any], error: ValidationError) -> bool:
    """Corta no limite do campo os textos que a limpeza deixou longos demais.

    A troca por ``REMOVIDO`` pode deixar um texto já cortado pelo script maior que o
    limite do modelo. Devolve se cortou algum; qualquer outro erro fica como está.
    """
    cut = False
    for detail in error.errors():
        limit = (detail.get("ctx") or {}).get("max_length")
        if detail["type"] != "string_too_long" or not isinstance(limit, int):
            continue
        *path, last = detail["loc"]
        holder: Any = data
        try:
            for part in path:
                holder = holder[part]
            text = holder[last]
        except KeyError, IndexError, TypeError:
            continue
        if isinstance(text, str) and len(text) > limit:
            holder[last] = text[:limit]
            cut = True
    return cut


def redact_url(url: str, sensitive_names: set[str]) -> str:
    """Mascara valores de parâmetros sensíveis da consulta e remove o fragmento."""
    parts = urlsplit(url)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    names = _SENSITIVE_PARAMS | sensitive_names
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key.lower() in names for key, _ in pairs):
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    query = urlencode([(k, REDACTED if k.lower() in names else v) for k, v in pairs])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass
class _Action:
    index: int
    t_ms: float
    kind: str


@dataclass
class _NavRequest:
    index: int
    method: str
    has_referer: bool


@dataclass
class _PageState:
    page_id: int
    is_popup: bool = False
    popup_trigger: int | None = None
    navigations: int = 0
    expect_typed: bool = False
    pending_action: _Action | None = None
    last_click: _Action | None = None
    nav_request: _NavRequest | None = None
    #: Índice da navegação que carregou o documento atual (None: anterior à gravação).
    document_index: int | None = None
    last_text: tuple[int | None, str, tuple[str, ...]] | None = None
    sensitive_names: set[str] = field(default_factory=set)


class Recorder:
    """Grava a demonstração feita num ``BrowserContext`` do Playwright.

    Uso::

        recorder = Recorder()
        await recorder.start(context)
        ...  # a pessoa trabalha no navegador
        recording = await recorder.stop()
    """

    def __init__(
        self,
        *,
        settle_ms: int = 400,
        capture_text: bool = True,
        probe_locators: bool = True,
        max_text_chars: int = 20_000,
        action_window_ms: int = 10_000,
    ) -> None:
        self._settle_s = settle_ms / 1000
        self._capture_text = capture_text
        self._probe_locators = probe_locators
        self._max_text = max(1_000, min(max_text_chars, 100_000))
        self._action_window_ms = action_window_ms
        self._context: BrowserContext | None = None
        self._binding = f"__rapa_gravador_{secrets.token_hex(6)}"
        self._nonce = secrets.token_hex(16)
        self._events: list[RecordingEvent] = []
        self._pages: dict[Page, _PageState] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._disposables: list[Any] = []
        self._other_hosts: set[str] = set()
        self._dropped = 0
        self._t0 = 0.0
        self._started_at: datetime | None = None
        self._stopping = False
        self._closed = False
        self._recording: Recording | None = None
        #: Valores de campos sigilosos (usuário, senha, código), só em memória, para
        #: apagar dos textos capturados ao fechar a gravação. Nunca entram nela.
        self._secret_values: set[str] = set()
        #: Campos sigilosos descobertos só ao fechar: os endereços já gravados são
        #: mascarados de novo com os nomes desses campos.
        self._redact_again = False
        #: Buscas dos valores sigilosos (exata e sem caixa), montadas uma vez para o
        #: conjunto atual.
        self._patterns: dict[bool, re.Pattern[str] | None] = {}
        self._pattern_for: set[str] = set()

    # -- ciclo de vida ---------------------------------------------------------

    async def start(self, context: BrowserContext) -> None:
        """Começa a gravar todas as abas, atuais e futuras, do contexto."""
        if self._context is not None:
            raise RecorderError("O gravador já foi iniciado.")
        self._context = context
        self._t0 = time.monotonic()
        self._started_at = datetime.now(UTC)
        script = _CAPTURE_JS.replace(
            "__RAPA_CONFIG__", json.dumps({"binding": self._binding, "nonce": self._nonce})
        )
        self._disposables.append(await context.expose_binding(self._binding, self._on_binding))
        self._disposables.append(await context.add_init_script(script))
        context.on("page", self._on_page)
        context.on("framenavigated", self._on_frame_navigated)
        context.on("request", self._on_request)
        context.on("download", self._on_download)
        for page in list(context.pages):
            state = self._register_page(page)
            for frame in page.frames:
                with suppress(PlaywrightError):
                    await frame.evaluate(script)
            if _is_web_url(page.url):
                event = self._append(
                    NavigationEvent,
                    page_id=state.page_id,
                    frame_url=None,
                    url=redact_url(page.url, state.sensitive_names),
                    cause=NavigationCause.INITIAL,
                )
                state.navigations += 1
                if event is not None:
                    state.document_index = event.index
                    self._schedule_text(page, event.index, 0)

    async def stop(self) -> Recording:
        """Para a gravação e devolve o que foi gravado. Pode ser chamado de novo."""
        if self._recording is not None:
            return self._recording
        context = self._context
        if context is None or self._started_at is None:
            raise RecorderError("O gravador não foi iniciado.")
        # Valores digitados que ainda não saíram (campo com foco) e paradas do script.
        for page in list(context.pages):
            for frame in page.frames:
                with suppress(PlaywrightError):
                    await frame.evaluate(_STOP_JS)
        await asyncio.sleep(0.15)
        self._stopping = True
        if self._capture_text:
            for page in list(context.pages):
                await self._snapshot(page, self._last_index())
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=5)
            for task in pending:
                task.cancel()
        self._closed = True
        for event_name, handler in (
            ("page", self._on_page),
            ("framenavigated", self._on_frame_navigated),
            ("request", self._on_request),
            ("download", self._on_download),
        ):
            with suppress(Exception):
                context.remove_listener(event_name, handler)
        for page in self._pages:
            with suppress(Exception):
                page.remove_listener("popup", self._on_popup)
            with suppress(Exception):
                page.remove_listener("load", self._on_load)
        for disposable in reversed(self._disposables):
            with suppress(Exception):
                await disposable.close()
        self._hide_secret_page_fields()
        events = [self._scrub(event) for event in self._events]
        self._secret_values.clear()
        self._patterns.clear()
        self._pattern_for.clear()
        self._recording = Recording(
            started_at=self._started_at,
            finished_at=datetime.now(UTC),
            events=events,
            other_hosts=sorted(self._other_hosts),
            dropped_messages=self._dropped,
        )
        return self._recording

    def _remember_secret(self, value: object) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < MIN_REDACTED_CHARS:
            return
        # Também como o valor aparece num endereço: percent-encoding estrito, o do
        # encodeURIComponent e o de formulário (espaço vira "+").
        for variant in dict.fromkeys(
            (
                value,
                quote(value, safe=""),
                quote(value, safe="!~*'()"),
                quote_plus(value, safe="*"),
            )
        ):
            if len(self._secret_values) < _MAX_SECRET_VALUES:
                self._secret_values.add(variant)

    def _hide_secret_page_fields(self) -> None:
        """Tira da gravação os campos de páginas com senha que o script não viu (segunda camada).

        O script só vê o grupo de campos da página no momento em que manda o valor:
        no login em duas etapas de uma SPA a senha ainda está escondida, e no login
        em duas páginas ela está na página seguinte. Aqui a mesma regra do compilador
        (``secret_page_inputs``) apaga, depois do fato, o valor dos campos de texto
        preenchidos numa página que pede senha ou código (desde a última navegação
        de documento ou o último login confirmado) e o do usuário de uma página que
        leva à página da senha: o usuário (pelo próprio campo) com a marca de
        usuário, os outros sem ela (o compilador os transforma em entradas). O campo
        fica sensível, o valor fica guardado para ``_scrub`` apagá-lo dos textos e os
        endereços já gravados são mascarados de novo com o nome do campo.
        """
        found = secret_page_inputs(self._events)
        if not found.usernames and not found.others:
            return
        states = {state.page_id: state for state in self._pages.values()}
        for event, is_username in [
            *((e, True) for e in found.usernames),
            *((e, False) for e in found.others),
        ]:
            self._remember_secret(event.value)
            self._remember_secret(event.typed)
            event.value = None
            event.typed = None
            event.script_modified = False
            event.element.is_username = is_username
            event.element.sensitive = True
            state = states.get(event.page_id)
            if state is not None and event.element.name_attr:
                state.sensitive_names.add(event.element.name_attr.lower())
        self._redact_again = True

    def _secret_pattern(self, *, ignore_case: bool) -> re.Pattern[str] | None:
        """Os valores sigilosos numa busca só, exata ou sem caixa.

        As duas cobrem as normalizações Unicode do valor (é composto ou "e" +
        acento). A busca sem caixa, para tudo o que não é valor digitado, chave da
        linha ou endereço, também leva as formas em maiúsculas e minúsculas, que
        cobrem o que muda de tamanho ao trocar de caixa ("ß" → "SS"). Os mais longos
        vêm primeiro.
        """
        if self._pattern_for != self._secret_values:
            self._patterns.clear()
            self._pattern_for = set(self._secret_values)
        if ignore_case in self._patterns:
            return self._patterns[ignore_case]
        variants: set[str] = set()
        for secret in self._secret_values:
            forms = (
                (secret, secret.upper(), secret.lower(), secret.casefold())
                if ignore_case
                else (secret,)
            )
            for form in forms:
                for normalized in ("NFC", "NFD"):
                    variants.add(unicodedata.normalize(normalized, form))
        ordered = sorted((v for v in variants if v), key=len, reverse=True)
        flags = re.IGNORECASE if ignore_case else 0
        pattern = re.compile("|".join(re.escape(v) for v in ordered), flags) if ordered else None
        self._patterns[ignore_case] = pattern
        return pattern

    def _scrub(self, event: RecordingEvent) -> RecordingEvent:
        """Troca por ``REMOVIDO`` os valores sigilosos que aparecerem em qualquer texto do evento.

        O nome de usuário costuma aparecer na página depois do login ("Olá, ana.souza",
        um menu "Menu de ANA.SOUZA") e chegaria às pós-condições, aos localizadores e
        ao prompt do modelo de linguagem. Em tudo o que pode chegar ao robô ou ao
        prompt a busca não olha maiúsculas e cobre as formas Unicode do valor ("weiß"
        em maiúsculas é "WEISS"): o texto livre da página (título, cabeçalhos, texto e
        iframes), o nome do arquivo baixado e, no descritor do elemento, o nome
        acessível, o rótulo, o placeholder, o título, o texto, o id, o atributo
        ``name``, o seletor, a conferência do Playwright, os iframes e o texto e as
        células da linha da tabela (de onde sai o retrato do alvo). Um localizador com
        ``REMOVIDO`` (que não seja CSS) não entra no robô. Só perdem o valor exato o
        que o executor compara com a página como está (valores digitados ou
        escolhidos, a chave da linha e endereços) e as marcas de estrutura do
        elemento: o usuário "financeiro" não apaga o departamento "Financeiro
        Corporativo" digitado. Quando um campo sigiloso só foi descoberto ao fechar a
        gravação, os endereços do evento são mascarados de novo com os nomes dos
        campos sigilosos da aba. Um texto que a troca deixou maior que o limite do
        campo é cortado no limite.
        """
        if not self._secret_values and not self._redact_again:
            return event
        exact = self._secret_pattern(ignore_case=False)
        loose = self._secret_pattern(ignore_case=True)
        names: set[str] = set()
        for state in self._pages.values():
            if state.page_id == event.page_id:
                names = state.sensitive_names

        def clean(value: Any, pattern: re.Pattern[str] | None) -> Any:
            if isinstance(value, str):
                return pattern.sub(REDACTED, value) if pattern is not None else value
            if isinstance(value, list):
                return [clean(item, pattern) for item in value]
            if isinstance(value, dict):
                return {key: clean(item, pattern) for key, item in value.items()}
            return value

        def clean_frames(value: Any) -> Any:
            if not isinstance(value, list):
                return clean(value, exact)
            return [
                {
                    key: clean(item, exact if key in _EXACT_FRAME_FIELDS else loose)
                    for key, item in frame.items()
                }
                if isinstance(frame, dict)
                else clean(frame, exact)
                for frame in value
            ]

        def clean_element(value: Any) -> Any:
            if not isinstance(value, dict):
                return clean(value, loose)
            cleaned_element: dict[str, Any] = {}
            for key, item in value.items():
                if key in _EXACT_ELEMENT_FIELDS or key == "form":
                    # O formulário só tem o endereço, o método e contagens.
                    cleaned_element[key] = clean(item, exact)
                elif key == "row" and isinstance(item, dict):
                    # A chave da linha o executor compara inteira, com maiúsculas.
                    cleaned_element[key] = {
                        part: clean(text, exact if part == "key" else loose)
                        for part, text in item.items()
                    }
                elif key == "frame_path":
                    cleaned_element[key] = clean_frames(item)
                else:
                    cleaned_element[key] = clean(item, loose)
            return cleaned_element

        data = event.model_dump(mode="json")
        redacted = dict(data)
        if self._redact_again:
            for key in ("url", "frame_url"):
                url = redacted.get(key)
                if isinstance(url, str) and url:
                    redacted[key] = redact_url(url, names)
        cleaned: dict[str, Any] = {}
        for key, item in redacted.items():
            if key == "element":
                cleaned[key] = clean_element(item)
            elif key == "frame_path":
                cleaned[key] = clean_frames(item)
            else:
                cleaned[key] = clean(item, exact if key in _EXACT_EVENT_FIELDS else loose)
        if cleaned == data:
            return event
        for _ in range(_MAX_FIT_ROUNDS):
            try:
                return type(event).model_validate(cleaned)
            except ValidationError as exc:
                if not _cut_to_limits(cleaned, exc):
                    break
        raise RecorderError("Não consegui apagar um valor sigiloso da gravação; grave de novo.")

    def expect_typed_navigation(self, page: Page) -> None:
        """Avisa que a próxima navegação da aba foi digitada pela pessoa (barra de endereço)."""
        self._register_page(page).expect_typed = True

    async def goto(self, page: Page, url: str) -> None:
        """Navega como se a pessoa tivesse digitado o endereço."""
        self.expect_typed_navigation(page)
        await page.goto(url)

    # -- eventos ---------------------------------------------------------------

    def _now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000

    def _last_index(self) -> int | None:
        return self._events[-1].index if self._events else None

    def _append[E: BaseModel](self, cls: type[E], **data: Any) -> E | None:
        if self._closed:
            return None
        try:
            event = cls.model_validate({"index": len(self._events), "t_ms": self._now_ms(), **data})
        except ValidationError:
            # Nunca registrar a mensagem do pydantic: ela repete os valores recebidos.
            self._dropped += 1
            return None
        self._events.append(cast(RecordingEvent, event))
        return event

    def _register_page(self, page: Page) -> _PageState:
        state = self._pages.get(page)
        if state is None:
            state = _PageState(page_id=len(self._pages))
            self._pages[page] = state
            page.on("popup", self._on_popup)
            page.on("load", self._on_load)
        return state

    def _spawn(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self._closed:
            return
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(factory())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # Uma falha numa captura em segundo plano não derruba a gravação, e a
        # mensagem (que pode repetir conteúdo da página) não vai para o log.
        if not task.cancelled() and task.exception() is not None:
            self._dropped += 1

    def _on_page(self, page: Page) -> None:
        self._register_page(page)

    def _on_load(self, page: Page) -> None:
        # Uma captura logo no load (a automação pode sair da página em seguida) e
        # outra depois que a página assenta (conteúdo que aparece com atraso).
        last = self._last_index()
        if last is not None:
            self._schedule_text(page, last, 0)
            self._schedule_text(page, last, self._settle_s)

    def _on_popup(self, popup: Page) -> None:
        state = self._register_page(popup)
        if state.navigations == 0:
            state.is_popup = True
            clicks = [s.last_click for s in self._pages.values() if s.last_click is not None]
            if clicks:
                last = max(clicks, key=lambda action: action.index)
                if self._now_ms() - last.t_ms <= self._action_window_ms:
                    state.popup_trigger = last.index

    def _on_binding(self, source: dict[str, Any], payload: object) -> None:
        # Nada volta para a página nem para o log: um erro aqui só descarta a mensagem.
        try:
            self._handle_binding(source, payload)
        except Exception:
            self._dropped += 1

    def _handle_binding(self, source: dict[str, Any], payload: object) -> None:
        if self._closed:
            return
        if not isinstance(payload, dict) or not secrets.compare_digest(
            str(payload.get("nonce", "")), self._nonce
        ):
            self._dropped += 1
            return
        frame = cast(Frame | None, source.get("frame"))
        page = cast(Page | None, source.get("page"))
        if frame is None or page is None:
            return
        state = self._register_page(page)
        try:
            raw_element = payload.get("element")
            element = (
                ElementDescriptor.model_validate(raw_element) if raw_element is not None else None
            )
        except ValidationError:
            self._dropped += 1
            return
        if element is not None and not element.sensitive and is_password_like_text(element):
            # Mesma regra do script (que também olha a máscara por CSS): senha em
            # campo de texto, pelo autocomplete ou pelo rótulo. Um campo que o
            # script já marcou (usuário, por exemplo) fica como ele marcou: o cofre
            # nunca digita a senha num campo de texto que é o usuário.
            element.is_password = True
        elif element is not None and not element.sensitive and password_by_name(element):
            # Só o aria-label ou o aria-labelledby fala em senha: o valor sai da
            # gravação, sem a marca de senha (o compilador pede o valor numa entrada).
            element.sensitive = True
        if element is not None and (
            element.is_password or element.is_one_time_code or element.is_username
        ):
            element.sensitive = True
        if element is not None and element.sensitive and element.name_attr:
            state.sensitive_names.add(element.name_attr.lower())
        common: dict[str, Any] = {
            "page_id": state.page_id,
            "frame_url": redact_url(frame.url, state.sensitive_names),
        }
        kind = payload.get("kind")
        created: BaseModel | None
        if kind == "click" and element is not None:
            click = self._append(
                ClickEvent, element=element, detail=payload.get("detail", 1), **common
            )
            if click is not None:
                action = _Action(click.index, click.t_ms, "click")
                state.pending_action = action
                state.last_click = action
                self._schedule_text(page, click.index, self._settle_s)
            created = click
        elif kind == "input" and element is not None:
            sensitive = element.sensitive
            if sensitive:
                # O valor só chega aqui se o script não marcou o campo; não é gravado.
                for part in ("secret", "value", "typed"):
                    self._remember_secret(payload.get(part))
            created = self._append(
                InputEvent,
                element=element,
                value=None if sensitive else payload.get("value"),
                typed=None if sensitive else payload.get("typed"),
                script_modified=False if sensitive else bool(payload.get("script_modified")),
                editing_since_ms=self._editing_since(payload.get("editing_ms")),
                **common,
            )
        elif kind == "select" and element is not None:
            created = self._append(
                SelectEvent,
                element=element,
                label=payload.get("label", ""),
                value=payload.get("value", ""),
                multiple=bool(payload.get("multiple")),
                **common,
            )
        elif kind == "check" and element is not None:
            created = self._append(
                CheckEvent, element=element, checked=bool(payload.get("checked")), **common
            )
        elif kind == "file" and element is not None:
            created = self._append(
                FileEvent, element=element, file_names=payload.get("file_names", []), **common
            )
        elif kind == "key":
            key = self._append(KeyEvent, key=payload.get("key"), element=element, **common)
            if key is not None and key.key == "Enter":
                state.pending_action = _Action(key.index, key.t_ms, "key")
            created = key
        else:
            self._dropped += 1
            return
        if created is None or element is None:
            return
        if self._probe_locators:
            self._spawn(lambda: self._probe(element, frame, state))
        if element.frame_path is None and frame.parent_frame is not None:
            self._spawn(lambda: self._fill_frame_path(element, frame))

    def _editing_since(self, editing_ms: object) -> float | None:
        """Começo da edição do campo no relógio da gravação (o script manda a duração)."""
        if isinstance(editing_ms, bool) or not isinstance(editing_ms, int | float):
            return None
        if not 0 <= editing_ms <= _MAX_EDITING_MS:  # também descarta NaN
            return None
        return max(0.0, self._now_ms() - float(editing_ms))

    def _on_frame_navigated(self, frame: Frame) -> None:
        if self._closed:
            return
        url = frame.url
        if not _is_web_url(url):
            return
        page = frame.page
        state = self._register_page(page)
        if frame.parent_frame is not None:
            self._append(
                NavigationEvent,
                page_id=state.page_id,
                frame_url=None,
                url=redact_url(url, state.sensitive_names),
                main_frame=False,
                cause=NavigationCause.SCRIPT,
            )
            return
        same_document = state.nav_request is None
        cause, trigger = self._navigation_cause(state, same_document)
        event = self._append(
            NavigationEvent,
            page_id=state.page_id,
            frame_url=None,
            url=redact_url(url, state.sensitive_names),
            cause=cause,
            trigger_index=trigger,
            same_document=same_document,
        )
        state.navigations += 1
        state.nav_request = None
        state.pending_action = None
        state.expect_typed = False
        state.document_index = event.index if event is not None else None
        if event is not None and same_document:
            self._schedule_text(page, event.index, self._settle_s)

    def _navigation_cause(
        self, state: _PageState, same_document: bool
    ) -> tuple[NavigationCause, int | None]:
        if state.expect_typed:
            return NavigationCause.TYPED, None
        if state.is_popup and state.navigations == 0:
            return NavigationCause.POPUP, state.popup_trigger
        request = state.nav_request
        action = state.pending_action
        if (
            action is not None
            and self._now_ms() - action.t_ms <= self._action_window_ms
            and (request is None or request.index > action.index)
        ):
            cause = NavigationCause.CLICK if action.kind == "click" else NavigationCause.KEY
            # GET sem Referer depois de uma ação: endereço digitado logo em seguida.
            if same_document or request is None or request.method != "GET" or request.has_referer:
                return cause, action.index
            return NavigationCause.TYPED, None
        if same_document or request is None:
            return NavigationCause.SCRIPT, None
        if request.has_referer or request.method != "GET":
            return NavigationCause.SCRIPT, None
        return NavigationCause.TYPED, None

    def _on_request(self, request: Request) -> None:
        if self._closed:
            return
        url = request.url
        if not _is_web_url(url):
            return
        method = request.method.upper()
        resource_type = request.resource_type
        if resource_type not in _RECORDED_TYPES and method in _SAFE_METHODS:
            host = urlsplit(url).hostname
            if host:
                self._other_hosts.add(host.lower())
            return
        frame: Frame | None
        try:
            frame = request.frame
        except PlaywrightError:
            frame = None
        state = self._register_page(frame.page) if frame is not None else None
        main_frame = frame is not None and frame.parent_frame is None
        has_referer = "referer" in request.headers
        redirected = request.redirected_from is not None
        is_navigation = request.is_navigation_request()
        event = self._append(
            RequestEvent,
            page_id=state.page_id if state is not None else 0,
            frame_url=None,
            method=method,
            url=_without_query(url),
            resource_type=resource_type,
            main_frame=main_frame,
            is_navigation=is_navigation,
            has_referer=has_referer,
            redirected=redirected,
        )
        if event is not None and state is not None and main_frame and is_navigation:
            if not redirected or state.nav_request is None:
                state.nav_request = _NavRequest(event.index, method, has_referer)

    def _on_download(self, download: Download) -> None:
        if self._closed:
            return
        state = self._register_page(download.page)
        # A requisição de documento virou download: não houve navegação.
        state.nav_request = None
        trigger: int | None = None
        click = state.last_click
        if click is not None and self._now_ms() - click.t_ms <= self._action_window_ms:
            trigger = click.index
            if state.pending_action is not None and state.pending_action.index == click.index:
                # O clique virou download: não espera navegação por causa dele.
                state.pending_action = None
        self._append(
            DownloadEvent,
            page_id=state.page_id,
            frame_url=None,
            url=_without_query(download.url),
            suggested_filename=download.suggested_filename[:255],
            trigger_index=trigger,
        )

    # -- tarefas em segundo plano ----------------------------------------------

    def _schedule_text(self, page: Page, after_index: int, delay_s: float) -> None:
        if not self._capture_text or self._stopping:
            return
        self._spawn(lambda: self._capture_later(page, after_index, delay_s))

    async def _capture_later(self, page: Page, after_index: int, delay_s: float) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        await self._snapshot(page, after_index)

    async def _snapshot(self, page: Page, after_index: int | None) -> None:
        """Texto visível do documento atual da aba.

        A captura só vale se nenhuma navegação aconteceu enquanto ela rodava:
        assim o texto fica ligado ao documento certo mesmo quando a automação é
        mais rápida que a página.
        """
        if page.is_closed():
            return
        state = self._register_page(page)
        seq = state.navigations
        document_index = state.document_index
        try:
            main = await page.main_frame.evaluate(_PAGE_TEXT_JS, self._max_text)
            frame_texts: list[str] = []
            for frame in page.frames:
                if frame is page.main_frame or frame.is_detached() or not _is_web_url(frame.url):
                    continue
                with suppress(PlaywrightError):
                    sub = await frame.evaluate(_PAGE_TEXT_JS, self._max_text)
                    if isinstance(sub, dict):
                        frame_texts.append(str(sub.get("text", ""))[: self._max_text])
                if len(frame_texts) >= 20:
                    break
        except PlaywrightError:
            return
        if state.navigations != seq or not isinstance(main, dict):
            return
        url = str(main.get("url", ""))
        if not _is_web_url(url):
            return
        text = str(main.get("text", ""))[: self._max_text]
        key = (document_index, text, tuple(frame_texts))
        if state.last_text == key:
            return
        state.last_text = key
        try:
            headings = [Heading.model_validate(h) for h in list(main.get("headings") or [])[:50]]
        except ValidationError:
            headings = []
        self._append(
            PageTextEvent,
            page_id=state.page_id,
            frame_url=None,
            url=redact_url(url, state.sensitive_names),
            title=str(main.get("title", ""))[:500],
            headings=headings,
            text=text,
            frame_texts=frame_texts,
            after_index=after_index,
            navigation_index=document_index,
        )

    async def _probe(self, element: ElementDescriptor, frame: Frame, state: _PageState) -> None:
        """Confere com o próprio Playwright quantos elementos casam com cada localizador.

        O resultado só vale se a aba não navegou durante a conferência.
        """
        seq = state.navigations
        try:
            if frame.is_detached():
                return
            located = frame.locator(element.css)
            if await located.count() != 1:
                element.probe = LocatorProbe(found=False)
                return
            normalized: str | None = None
            if not element.sensitive:
                with suppress(PlaywrightError, TimeoutError):
                    normalized = str(await asyncio.wait_for(located.normalize(), 2))[:1000]
            role_name: int | None = None
            if element.role in _PROBE_ROLES and element.name:
                role_name = await frame.get_by_role(
                    cast(Any, element.role), name=element.name, exact=True
                ).count()
            label = (
                await frame.get_by_label(element.label, exact=True).count()
                if element.label
                else None
            )
            placeholder = (
                await frame.get_by_placeholder(element.placeholder, exact=True).count()
                if element.placeholder
                else None
            )
            text = (
                await frame.get_by_text(element.text, exact=True).count()
                if element.text and len(element.text) <= 100
                else None
            )
            # O elemento continua lá e a aba não navegou: as contagens valem.
            still_there = await located.count() == 1
        except PlaywrightError:
            element.probe = LocatorProbe(found=False)
            return
        if not still_there or state.navigations != seq or frame.is_detached():
            element.probe = LocatorProbe(found=False)
            return
        element.probe = LocatorProbe(
            found=True,
            normalized=normalized,
            role_name=role_name,
            label=label,
            placeholder=placeholder,
            text=text,
        )

    async def _fill_frame_path(self, element: ElementDescriptor, frame: Frame) -> None:
        """Caminho dos iframes de outra origem, que o script não consegue ver."""
        path: list[FrameInfo] = []
        current = frame
        try:
            while current.parent_frame is not None:
                handle = await current.frame_element()
                info = await handle.evaluate(_FRAME_INFO_JS)
                path.insert(0, FrameInfo.model_validate(info))
                current = current.parent_frame
        except PlaywrightError, ValidationError:
            return
        element.frame_path = path
