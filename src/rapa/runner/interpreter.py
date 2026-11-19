"""Interpretador determinístico dos robôs do RaPA, sobre Playwright e sem LLM.

O interpretador percorre a lista de passos do manifesto, num contexto de
navegador novo, e devolve um :class:`~rapa.runner.result.RunResult`. Regras
principais:

- um localizador só serve se encontrar **exatamente um** elemento; passos de
  leitura podem usar localizadores alternativos (evento ``step_healed``), mas
  passos que gravam só usam o principal;
- todo endereço aberto precisa estar nos domínios permitidos do robô, e
  segredos só são digitados na origem declarada da credencial;
- antes de um passo irreversível agir, o executor avisa (evento
  ``step_started`` com ``irreversivel: true``); qualquer falha depois disso
  deixa a execução "indeterminada", nunca "falhou";
- valores de entradas, variáveis e itens nunca mudam o alvo de um passo: não
  entram em seletores CSS/XPath, cada um vira um único trecho codificado do
  endereço (no caminho, sem barras e nunca vazio), e o texto que escolhe uma
  linha precisa ser o texto inteiro de um elemento dela (uma célula, por
  exemplo), sem contar maiúsculas;
- o que faria a execução falhar no meio por causa das entradas (campo de item
  ausente, número que não pode ser digitado, lista maior que a repetição, valor
  que não pode ir no caminho do endereço, texto vazio para escolher a linha)
  falha antes do primeiro passo;
- eventos, mensagens, saídas e nomes de artefatos nunca contêm valores de
  credenciais; as chaves dos eventos e os valores que são constantes do
  executor (efeito, estado, código de erro, id de passo) nunca são mascarados.
"""

import asyncio
import math
import os
import re
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self, assert_never, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Frame,
    FrameLocator,
    Page,
    Route,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as PwLocator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from rapa.core import templates
from rapa.core.contracts import RunMode, RunnerJob, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.policy import RAW_SELECTORS, TEST_TLD
from rapa.core.robot import (
    AssertStep,
    CheckStep,
    ClickStep,
    Condition,
    CredentialRef,
    DownloadStep,
    Effect,
    EqualsCondition,
    ExtractTableStep,
    ExtractTextStep,
    FieldType,
    FillStep,
    ForEachStep,
    IfStep,
    InputField,
    Locator,
    LocatorBy,
    Manifest,
    NavigateStep,
    PressStep,
    SecretValue,
    SelectStep,
    Step,
    Target,
    TextContainsCondition,
    UploadStep,
    UrlContainsCondition,
    VisibleCondition,
    WaitForStep,
    child_blocks,
)
from rapa.runner.errors import ErrorCode, StepError
from rapa.runner.ports import (
    ArtifactSink,
    EventSink,
    FileProvider,
    FileUnavailable,
    SecretProvider,
    SecretUnavailable,
    sanitize_filename,
)
from rapa.runner.result import ArtifactKind, ArtifactRef, RunResult

__all__ = ["MASK", "Interpreter"]

#: Texto que substitui valores de credenciais em eventos, mensagens e saídas.
MASK = "***"

_TIMEOUT_GRACE_S = 2.0
_SCREENSHOT_TIMEOUT_MS = 5_000
_DETAIL_MAX = 300
_MIN_MASKED_LENGTH = 4
#: Campos de evento com hashes que o próprio executor calcula (nunca dados da página).
_HASH_KEYS = frozenset({"sha256", "fingerprint"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
#: Campos de evento cujo valor é uma constante do executor, quando é um destes.
_EVENT_CONSTANTS: dict[str, frozenset[str]] = {
    "efeito": frozenset(effect.value for effect in Effect),
    "estado": frozenset(state.value for state in RunState),
    "codigo": frozenset(code.value for code in ErrorCode),
    "modo": frozenset(mode.value for mode in RunMode),
    "estrategia": frozenset(by.value for by in LocatorBy),
    "estrategias": frozenset(by.value for by in LocatorBy),
    "ramo": frozenset({"entao", "senao"}),
    "origem": frozenset({"texto", "segredo"}),
}
#: Campos de evento com o id de um passo do robô.
_STEP_ID_KEYS = ("passo", "passo_irreversivel", "passo_interno")
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: Problemas de entrada listados na mensagem de erro (o resto só é contado).
_MAX_PROBLEMS = 20
#: Maior inteiro que um float guarda sem perder precisão.
_MAX_EXACT_FLOAT_INT = 2**53

#: Brancos do JavaScript (os de ``trim()`` e de ``\s``), que o Playwright usa ao comparar
#: textos. Não são os do Python: U+FEFF entra, U+001C a U+001F e U+0085 não.
_JS_SPACES = re.compile(
    "[\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)
#: Caracteres que o Playwright descarta antes de comparar textos.
_IGNORED_IN_TEXT = re.compile("[\u200b\u00ad]")
# Trechos da expressão do texto inteiro de um elemento (avaliada no navegador).
_TEXT_EDGE = r"[\s\u200b\u00ad]*"
_INSIDE_WORD = r"[\u200b\u00ad]*"
_BETWEEN_WORDS = r"[\u200b\u00ad]*\s[\s\u200b\u00ad]*"

type _Root = Page | FrameLocator | PwLocator
type _Action = Callable[[], Awaitable[dict[str, Any]]]
#: Onde uma referência cai no endereço de um ``navigate``.
type _UrlSlot = Literal["autoridade", "caminho", "consulta", "fragmento"]
#: Itens de uma repetição sobre uma entrada de lista: o nome da lista e as linhas.
type _Rows = tuple[str, list[Mapping[str, object]]]

#: Contextos já usados por alguma execução: nunca se reaproveita um contexto.
_claimed_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()

# Lê as linhas de dados de uma tabela HTML (ou de uma grade ARIA), já com o
# texto de cada célula aparado. Uma ida e volta ao navegador por tabela.
_READ_TABLE_JS = """
(el, maxRows) => {
  const text = (node) => (node.innerText ?? node.textContent ?? '').trim();
  if (el.tagName === 'TABLE') {
    let headers = [];
    const head = el.tHead;
    if (head && head.rows.length) headers = Array.from(head.rows[head.rows.length - 1].cells);
    let rows = el.tBodies.length
      ? Array.from(el.tBodies).flatMap((body) => Array.from(body.rows))
      : Array.from(el.rows).filter((row) => !row.closest('thead') && !row.closest('tfoot'));
    if (!headers.length && rows.length && Array.from(rows[0].cells).every((c) => c.tagName === 'TH')) {
      headers = Array.from(rows[0].cells);
      rows = rows.slice(1);
    }
    rows = rows.filter((row) => row.cells.length > 0);
    return {
      headers: headers.map(text),
      rows: rows.slice(0, maxRows).map((row) => Array.from(row.cells).map(text)),
      total: rows.length,
    };
  }
  const all = Array.from(el.querySelectorAll('[role="row"]'));
  const isHeader = (row) => row.querySelector('[role="columnheader"]') !== null;
  const headerRow = all.find(isHeader);
  const cells = (row) =>
    Array.from(row.querySelectorAll('[role="cell"],[role="gridcell"],[role="rowheader"]'));
  const rows = all.filter((row) => !isHeader(row) && cells(row).length > 0);
  return {
    headers: headerRow
      ? Array.from(headerRow.querySelectorAll('[role="columnheader"]')).map(text)
      : [],
    rows: rows.slice(0, maxRows).map((row) => cells(row).map(text)),
    total: rows.length,
  };
}
"""


class _StopBeforeWrite(Exception):
    def __init__(self, step: Step) -> None:
        super().__init__(step.id)
        self.step = step


class _Cancelled(Exception):
    def __init__(self, step: Step) -> None:
        super().__init__(step.id)
        self.step = step


class _Redactor:
    """Mascara valores de credenciais em qualquer texto que saia do executor."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str) -> None:
        # Valores muito curtos mascarariam texto comum demais.
        if len(value) >= _MIN_MASKED_LENGTH:
            self._values.add(value)

    def text(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, MASK)
        return text

    def value(self, value: Any) -> Any:
        """Mascara textos em qualquer profundidade, inclusive as chaves dos dicionários
        (cabeçalhos de tabela viram chaves). Nenhuma chave fica de fora."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            masked: dict[Any, Any] = {}
            for key, item in value.items():
                name = self._free_key(self.text(key), masked) if isinstance(key, str) else key
                masked[name] = self.value(item)
            return masked
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    @staticmethod
    def _free_key(key: str, taken: Mapping[Any, Any]) -> str:
        # Duas chaves que só diferem por um segredo ficariam iguais depois da máscara.
        candidate, n = key, 1
        while candidate in taken:
            n += 1
            candidate = f"{key}_{n}"
        return candidate


class _StepCtx:
    __slots__ = ("deadline", "step")

    def __init__(self, step: Step, deadline: float) -> None:
        self.step = step
        self.deadline = deadline

    def remaining_ms(self) -> float:
        """Tempo que resta ao passo, em ms (nunca 0, que no Playwright é 'sem limite')."""
        loop = asyncio.get_running_loop()
        return max(1.0, (self.deadline - loop.time()) * 1000)


def _normalize_origin(url: str) -> str:
    """``esquema://host[:porta]`` em minúsculas, sem a porta padrão; vazio se não for web."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return ""
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _number_source(kind: FieldType, text: str) -> str:
    """O texto que vira número: sem separadores de milhar e com ponto decimal."""
    if kind is FieldType.INTEGER:
        return text.replace(".", "").replace(" ", "")
    number = text.replace(" ", "")
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    return number


def _number_texts(kind: FieldType, raw: str, converted: int | float) -> list[str]:
    """Textos em que um número convertido pode mostrar um segredo.

    Os dígitos de onde ele veio (um float arredonda o último dígito de um número
    longo) e as suas formas decimais exatas: ``str()`` de um float grande usa
    expoente (``1.2345678901234568e+16``) e esconderia os dígitos da conferência.
    """
    texts = [_number_source(kind, raw.strip()), str(converted)]
    if isinstance(converted, float):
        texts.append(format(Decimal(repr(converted)), "f"))
        if converted.is_integer():
            texts.append(str(int(converted)))
    return texts


def _coerce_output(kind: FieldType, value: Any) -> Any:
    """Converte o texto extraído no tipo declarado da saída, quando possível."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if kind is FieldType.INTEGER:
        digits = _number_source(kind, text)
        try:
            return int(digits)
        except ValueError:
            return value
    if kind is FieldType.NUMBER:
        number = _number_source(kind, text)
        try:
            converted = float(number)
        except ValueError:
            return value
        return converted if math.isfinite(converted) else value
    if kind is FieldType.BOOLEAN:
        folded = text.casefold()
        if folded in {"sim", "true", "verdadeiro", "1"}:
            return True
        if folded in {"não", "nao", "false", "falso", "0"}:
            return False
    return value


def _prepare_simple(spec: InputField, value: object, where: str, problems: list[str]) -> object:
    """Normaliza um valor simples de entrada e confere que ele pode ser digitado."""
    if spec.type is FieldType.INTEGER and isinstance(value, float):
        # JSON e formulários mandam 5 como 5.0; digitar "5.0" seria outro número
        # num portal em pt-BR. Um float grande demais já perdeu dígitos.
        if not (value.is_integer() and abs(value) <= _MAX_EXACT_FLOAT_INT):
            problems.append(f"a entrada '{where}' precisa ser um número inteiro")
            return value
        value = int(value)
    try:
        templates.format_value(value)
    except templates.TemplateError as exc:
        problems.append(f"a entrada '{where}' não pode ser digitada: {exc}")
    return value


def _url_slots(parts: list[templates.Part]) -> dict[int, _UrlSlot]:
    """Em que trecho do endereço cai cada referência, pela posição dela em ``parts``.

    Os valores são codificados e nunca trazem ``/``, ``?`` nem ``#``: só os trechos
    literais antes de cada referência decidem onde ela cai. Sem ``//`` no começo,
    vale a regra mais estrita (caminho).
    """
    slots: dict[int, _UrlSlot] = {}
    before = ""
    for index, part in enumerate(parts):
        if isinstance(part, str):
            before += part
            continue
        if "#" in before:
            slots[index] = "fragmento"
        elif "?" in before:
            slots[index] = "consulta"
        elif (start := before.find("//")) != -1 and not any(
            separator in before[start + 2 :] for separator in "/\\"
        ):
            slots[index] = "autoridade"
        else:
            slots[index] = "caminho"
        before += "x"  # o valor ocupa o lugar, mas não muda o trecho
    return slots


def _path_value_problem(text: str) -> str | None:
    """Por que um valor não pode ser um trecho do caminho do endereço (``None`` se pode).

    Codificar não basta: muitos servidores (ASGI, WSGI, Laravel...) decodificam o
    caminho antes de escolher a rota, e ``%2F`` volta a ser uma barra. Um valor
    vazio some e junta os trechos vizinhos (``/notas//cancelar``), e ``.`` e
    ``..`` mudam de pasta mesmo codificados.
    """
    if not text:
        return "está vazio"
    if text in (".", ".."):
        return f"é '{text}', que mudaria de pasta"
    if "/" in text or "\\" in text:
        return "contém '/' ou '\\', que mudariam o caminho"
    return None


def _encode_url_value(text: str, slot: _UrlSlot) -> str:
    """Codifica um valor como um único trecho do endereço.

    No caminho, recusa os valores que mudariam a rota mesmo codificados.
    """
    if slot == "caminho" and (problem := _path_value_problem(text)) is not None:
        raise templates.TemplateError(f"não pode ir no caminho do endereço: {problem}")
    return quote(text, safe="")


def _normalize_text(text: str) -> str:
    """O texto como o Playwright o compara.

    Sem U+200B e U+00AD, sem brancos do JavaScript nas pontas (U+FEFF inclusive) e
    com cada sequência deles virando um espaço. Uma chave que fica vazia assim
    escolheria qualquer elemento sem texto (uma célula vazia, um botão só com ícone).
    """
    return _JS_SPACES.sub(" ", _IGNORED_IN_TEXT.sub("", text)).strip(" ")


def _js_escape(char: str) -> str:
    # Letras e dígitos ASCII vão como estão; o resto do ASCII vira \xHH, que vale
    # igual no Python e no JavaScript e nunca é aspas, barra ou ">>" para o seletor.
    if char.isascii() and not char.isalnum():
        return f"\\x{ord(char):02x}"
    return char


def _whole_text(key: str) -> re.Pattern[str]:
    """Expressão, avaliada no navegador, do texto inteiro de um elemento igual a ``key``.

    ``key`` já vem normalizada. O Playwright testa expressões no texto cru
    (``textContent``), então a expressão aceita o que ele ignora ao comparar
    textos: brancos nas pontas e repetidos, U+200B e U+00AD. Os caracteres
    precisam ser os mesmos, mas maiúsculas e minúsculas não contam: a chave
    gravada vem de ``innerText``, que já aplicou o ``text-transform`` do CSS.
    """
    words = (_INSIDE_WORD.join(_js_escape(char) for char in word) for word in key.split(" "))
    body = _BETWEEN_WORDS.join(words)
    return re.compile(f"^{_TEXT_EDGE}{body}{_TEXT_EDGE}$", re.IGNORECASE)


def _step_targets(step: Step) -> list[Target]:
    """Alvos de um passo, inclusive o da condição de ``if`` e ``assert``."""
    targets: list[Target] = []
    for holder in (step, getattr(step, "condition", None)):
        target = getattr(holder, "target", None)
        if isinstance(target, Target):
            targets.append(target)
    return targets


def _has_references(template: str) -> bool:
    try:
        return templates.has_references(template)
    except templates.TemplateError:
        return False  # o template inválido é recusado ao ser montado


def _describe_condition(condition: Condition) -> str:
    match condition:
        case VisibleCondition(target=target, negate=negate):
            text = f"'{target.intent}' visível"
        case TextContainsCondition(target=target, text=needle, negate=negate):
            where = f"em '{target.intent}'" if target is not None else "na página"
            text = f"texto '{needle}' {where}"
        case UrlContainsCondition(value=needle, negate=negate):
            text = f"endereço contendo '{needle}'"
        case EqualsCondition(left=left, right=right, negate=negate):
            text = f"'{left}' igual a '{right}'"
        case _:  # pragma: no cover - conjunto fechado
            assert_never(condition)
    return f"não ({text})" if negate else text


class Interpreter:
    """Executa um manifesto num contexto de navegador novo.

    Cada instância executa uma única vez. O chamador cria e fecha o contexto
    (ver :func:`rapa.runner.browser.run_context`); o interpretador abre nele a
    sua página e recusa contextos já usados por outra execução.
    """

    def __init__(
        self,
        manifest: Manifest,
        inputs: Mapping[str, Any],
        *,
        secrets: SecretProvider,
        files: FileProvider,
        artifacts: ArtifactSink,
        events: EventSink,
        run_id: UUID,
        attempt: int,
        first_seq: int = 0,
        mode: RunMode = RunMode.FULL,
        deadline: datetime | None = None,
        cancel: asyncio.Event | None = None,
        screenshot_every_step: bool = False,
        block_other_domains: bool = True,
        redact_values: Iterable[str] = (),
        postcondition_timeout_ms: int = 5_000,
        heal_after_ms: int = 1_000,
        poll_interval_ms: int = 100,
    ) -> None:
        if attempt < 1:
            raise ValueError("attempt começa em 1")
        if first_seq < 0:
            raise ValueError("first_seq não pode ser negativo")
        self._manifest = manifest
        self._raw_inputs = dict(inputs)
        self._secrets = secrets
        self._files = files
        self._artifact_sink = artifacts
        self._events = events
        self._run_id = run_id
        self._attempt = attempt
        self._mode = mode
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        self._deadline_at = deadline
        self._cancel = cancel
        self._screenshot_every_step = screenshot_every_step
        self._block_other_domains = block_other_domains
        self._redactor = _Redactor(redact_values)
        self._postcondition_timeout_s = postcondition_timeout_ms / 1000
        self._heal_after_s = heal_after_ms / 1000
        self._poll_s = poll_interval_ms / 1000

        self._credentials: dict[str, CredentialRef] = {c.name: c for c in manifest.credentials}
        self._allowed_hosts = frozenset(manifest.allowed_domains)
        steps = [step for step, _ in manifest.iter_steps()]
        step_ids = frozenset(step.id for step in steps)
        self._event_constants: dict[str, frozenset[str]] = {
            **_EVENT_CONSTANTS,
            **dict.fromkeys(_STEP_ID_KEYS, step_ids),
            "tipo": frozenset(step.kind for step in steps) | {kind.value for kind in ArtifactKind},
        }
        # A numeração dos eventos é da execução: cada tentativa continua a anterior.
        self._seq = first_seq
        self._used = False
        self._page: Page | None = None
        self._deadline = math.inf
        self._started_at = 0.0
        self._inputs: dict[str, Any] = {}
        self._vars: dict[str, Any] = {}
        self._items: list[tuple[Mapping[str, object], int]] = []
        self._steps_done = 0
        self._irreversible_step: Step | None = None
        self._artifacts: list[ArtifactRef] = []
        self._secret_locators: list[PwLocator] = []
        self._blocked_navigation: str | None = None
        self._screenshots = 0

    @classmethod
    def from_job(
        cls,
        job: RunnerJob,
        *,
        secrets: SecretProvider,
        files: FileProvider,
        artifacts: ArtifactSink,
        events: EventSink,
        **options: Any,
    ) -> Self:
        """Monta o interpretador a partir do trabalho entregue pelo plano de controle."""
        return cls(
            job.manifest,
            job.inputs,
            secrets=secrets,
            files=files,
            artifacts=artifacts,
            events=events,
            run_id=job.run_id,
            attempt=job.attempt,
            first_seq=job.first_seq,
            mode=job.mode,
            deadline=job.deadline,
            **options,
        )

    @property
    def irreversible_started(self) -> bool:
        return self._irreversible_step is not None

    # ------------------------------------------------------------------
    # Execução
    # ------------------------------------------------------------------

    async def run(self, context: BrowserContext) -> RunResult:
        """Executa o robô e devolve o resultado. Só levanta exceção se os
        eventos não puderem ser registrados ou por uso indevido da API."""
        self._claim(context)
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        self._deadline = self._loop_deadline()
        await self._emit_run_started()

        state = RunState.SUCCEEDED
        code: str | None = None
        message: str | None = None
        failed_step: str | None = None
        try:
            if loop.time() >= self._deadline:
                raise StepError(
                    ErrorCode.TEMPO_ESGOTADO, "O prazo da execução acabou antes de começar."
                )
            self._inputs = self._prepare_inputs()
            self._page = await self._open_page(context)
            await self._run_block(self._manifest.steps)
            await self._check_postconditions()
        except _StopBeforeWrite as stop:
            await self._emit(
                RunEventKind.RUN_STOPPED_BEFORE_WRITE,
                stop.step.id,
                {"titulo": stop.step.title, "efeito": stop.step.effect.value},
            )
            code = ErrorCode.PAROU_ANTES_DE_GRAVAR
            message = (
                f"Validação segura: a execução parou antes do passo '{stop.step.title}', "
                "o primeiro que grava no sistema de destino."
            )
        except _Cancelled as cancelled:
            state = RunState.CANCELLED
            code = ErrorCode.CANCELADA
            message = f"A execução foi cancelada antes do passo '{cancelled.step.title}'."
            failed_step = cancelled.step.id
        except StepError as err:
            if not err.reported:
                err.reported = True
                await self._screenshot(err.step_id, "falha")
            state = RunState.FAILED
            code = err.code
            message = err.message
            failed_step = err.step_id

        if state is not RunState.SUCCEEDED and self._irreversible_step is not None:
            state = RunState.INDETERMINATE
            message = (
                f"Resultado indeterminado: o passo irreversível '{self._irreversible_step.title}' "
                f"({self._irreversible_step.id}) já tinha começado quando a execução parou. "
                f"Confira no sistema de destino antes de repetir. Motivo: {message}"
            )
        return await self._finish(state, code, message, failed_step)

    async def fail_without_browser(self, code: ErrorCode, message: str) -> RunResult:
        """Registra uma execução que nem chegou a abrir o navegador."""
        if self._used:
            raise RuntimeError("este interpretador já executou")
        self._used = True
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        await self._emit_run_started()
        return await self._finish(RunState.FAILED, code, message, None)

    async def _open_page(self, context: BrowserContext) -> Page:
        try:
            if self._block_other_domains:
                await context.route("**/*", self._guard_request)
            return await context.new_page()
        except PlaywrightError as exc:
            raise StepError(
                ErrorCode.NAVEGADOR_INDISPONIVEL,
                "Não consegui abrir uma página no navegador.",
                details={"detalhe": self._detail(exc)},
            ) from None

    def _claim(self, context: BrowserContext) -> None:
        if self._used:
            raise RuntimeError("cada Interpreter executa uma única vez; crie outro")
        if context in _claimed_contexts:
            raise RuntimeError(
                "este contexto do navegador já foi usado por outra execução; crie um novo"
            )
        self._used = True
        _claimed_contexts.add(context)

    def _loop_deadline(self) -> float:
        if self._deadline_at is None:
            return math.inf
        remaining = (self._deadline_at - datetime.now(UTC)).total_seconds()
        return asyncio.get_running_loop().time() + remaining

    async def _emit_run_started(self) -> None:
        await self._emit(
            RunEventKind.RUN_STARTED,
            None,
            {
                "robo": self._manifest.name,
                "fingerprint": self._manifest.fingerprint(),
                "modo": self._mode.value,
                "passos": sum(1 for _ in self._manifest.iter_steps()),
            },
        )

    async def _finish(
        self, state: RunState, code: str | None, message: str | None, failed_step: str | None
    ) -> RunResult:
        loop = asyncio.get_running_loop()
        message = self._redactor.text(message) if message is not None else None
        outputs = (
            self._collect_outputs()
            if state in (RunState.SUCCEEDED, RunState.INDETERMINATE)
            else None
        )
        data: dict[str, Any] = {
            "estado": state.value,
            "passos_concluidos": self._steps_done,
            "duracao_ms": round((loop.time() - self._started_at) * 1000),
            "irreversivel_iniciado": self.irreversible_started,
        }
        if code is not None:
            data["codigo"] = str(code)
        if message is not None:
            data["mensagem"] = message
        if failed_step is not None:
            data["passo"] = failed_step
        if self._irreversible_step is not None:
            data["passo_irreversivel"] = self._irreversible_step.id
        kind = (
            RunEventKind.RUN_SUCCEEDED if state is RunState.SUCCEEDED else RunEventKind.RUN_FAILED
        )
        await self._emit(kind, failed_step, data)
        return RunResult(
            run_id=self._run_id,
            attempt=self._attempt,
            state=state,
            outputs=outputs,
            error_code=str(code) if code is not None else None,
            error_message=message,
            failed_step_id=failed_step,
            irreversible_started=self.irreversible_started,
            steps_done=self._steps_done,
            artifacts=list(self._artifacts),
        )

    def _prepare_inputs(self) -> dict[str, Any]:
        """Confere e normaliza as entradas antes do primeiro passo.

        O que faria a execução falhar no meio por causa das entradas (campo de
        item ausente, número que não pode ser digitado, lista maior que a
        repetição aceita, valor que não pode ir no caminho de um endereço, texto
        vazio para escolher um contêiner) falha aqui, antes de qualquer passo
        gravar. As entradas recebidas não são alteradas.
        """
        values = dict(self._raw_inputs)
        problems: list[str] = []
        for spec in self._manifest.inputs:
            value = values.get(spec.name)
            if value is None:
                if spec.required:
                    problems.append(f"entrada obrigatória ausente: {spec.name}")
                elif spec.type is FieldType.LIST:
                    values[spec.name] = []
                elif spec.type is FieldType.FILE:
                    values.pop(spec.name, None)
                else:
                    values[spec.name] = ""
                continue
            if spec.type is FieldType.LIST:
                if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
                    values[spec.name] = self._prepare_items(spec, value, problems)
                else:
                    problems.append(f"a entrada '{spec.name}' precisa ser uma lista de objetos")
            elif spec.type is FieldType.FILE:
                if not isinstance(value, str):
                    problems.append(
                        f"a entrada '{spec.name}' precisa ser o identificador do arquivo"
                    )
            else:
                values[spec.name] = _prepare_simple(spec, value, spec.name, problems)
        self._check_input_uses(values, problems)
        if problems:
            shown = problems[:_MAX_PROBLEMS]
            if len(problems) > _MAX_PROBLEMS:
                shown.append(f"e mais {len(problems) - _MAX_PROBLEMS} problemas")
            raise StepError(
                ErrorCode.ENTRADA_INVALIDA,
                "Entradas inválidas: " + "; ".join(shown) + ".",
                details={"problemas": shown},
            )
        self._check_list_limits(values)
        return values

    @staticmethod
    def _prepare_items(
        spec: InputField, items: list[Mapping[str, object]], problems: list[str]
    ) -> list[dict[str, object]]:
        """Itens de uma lista com os mesmos cuidados das entradas de cima."""
        prepared: list[dict[str, object]] = []
        for index, item in enumerate(items):
            row = dict(item)
            for field in spec.item_fields or ():
                where = f"{spec.name}[{index}].{field.name}"
                current = row.get(field.name)
                if current is None:
                    if field.required:
                        problems.append(f"campo obrigatório ausente: {where}")
                    else:
                        # Campo opcional sem valor é digitado vazio (campos de item
                        # nunca são listas nem arquivos).
                        row[field.name] = ""
                    continue
                row[field.name] = _prepare_simple(field, current, where, problems)
            prepared.append(row)
        return prepared

    def _check_input_uses(self, values: Mapping[str, Any], problems: list[str]) -> None:
        """Confere os usos que só dependem das entradas e dos itens de listas de entrada.

        São os valores que vão no caminho de um endereço e o texto que escolhe um
        contêiner. Se um deles não serve, a execução pararia nesse passo, talvez
        depois de outros já terem gravado. Variáveis só existem durante a
        execução: essas são conferidas quando o passo roda. Vale para todos os
        passos, inclusive os de ramos de ``if`` que podem nem rodar.
        """
        found: list[str] = []

        def walk(steps: Iterable[Step], rows: _Rows | None) -> None:
            for step in steps:
                if isinstance(step, NavigateStep):
                    found.extend(self._url_value_problems(step, values, rows))
                for target in _step_targets(step):
                    found.extend(self._row_key_problems(step, target, values, rows))
                if isinstance(step, ForEachStep):
                    walk(step.steps, self._loop_rows(step, values))
                else:
                    for block in child_blocks(step):
                        walk(block, rows)

        walk(self._manifest.steps, None)
        for condition in self._manifest.postconditions:
            target = getattr(condition, "target", None)
            if isinstance(target, Target):
                found.extend(self._row_key_problems(None, target, values, None))
        seen = set(problems)
        for problem in found:
            if problem not in seen:  # a mesma entrada pode aparecer em vários passos
                seen.add(problem)
                problems.append(problem)

    @staticmethod
    def _loop_rows(step: ForEachStep, values: Mapping[str, Any]) -> _Rows | None:
        """Os itens de uma repetição, quando ela percorre uma entrada de lista."""
        scope, name = step.items.split(".", 1)
        rows = values.get(name) if scope == "inputs" else None
        if isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows):
            return name, rows
        return None

    @staticmethod
    def _ref_values(
        ref: templates.Ref, values: Mapping[str, Any], rows: _Rows | None
    ) -> list[tuple[str, object]]:
        """Os valores que uma referência pode ter, com a descrição de cada um."""
        if ref.scope is templates.Scope.INPUTS and ref.name in values:
            return [(f"a entrada '{ref.name}'", values[ref.name])]
        if ref.scope is templates.Scope.ITEM and rows is not None:
            name, items = rows
            return [
                (f"o campo '{name}[{index}].{ref.name}'", row[ref.name])
                for index, row in enumerate(items)
                if ref.name in row
            ]
        return []  # variáveis (e referências sem valor, recusadas ao montar o passo)

    def _url_value_problems(
        self, step: NavigateStep, values: Mapping[str, Any], rows: _Rows | None
    ) -> list[str]:
        try:
            parts = templates.parse(step.url)
        except templates.TemplateError:
            return []  # recusado ao montar o passo
        found: list[str] = []
        for index, slot in _url_slots(parts).items():
            ref = parts[index]
            if slot != "caminho" or not isinstance(ref, templates.Ref):
                continue
            for where, value in self._ref_values(ref, values, rows):
                try:
                    text = templates.format_value(value)
                except templates.TemplateError:
                    continue  # já recusado como valor que não pode ser digitado
                problem = _path_value_problem(text)
                if problem is not None:
                    found.append(
                        f"{where} não pode ir no caminho do endereço do passo "
                        f"'{step.id}': {problem}"
                    )
        return found

    @staticmethod
    def _row_key_problems(
        step: Step | None, target: Target, values: Mapping[str, Any], rows: _Rows | None
    ) -> list[str]:
        template = target.within_has_text
        if template is None:
            return []
        try:
            scopes = {ref.scope for ref in templates.references(template)}
        except templates.TemplateError:
            return []  # recusado ao montar o passo
        if not scopes or templates.Scope.VARS in scopes:
            return []  # texto fixo, ou que só existe durante a execução
        contexts: list[tuple[str, dict[templates.Scope, Mapping[str, object]]]]
        if templates.Scope.ITEM in scopes:
            if rows is None:
                return []  # itens de uma tabela lida da página
            name, items = rows
            contexts = [
                (
                    f" para '{name}[{index}]'",
                    {templates.Scope.INPUTS: values, templates.Scope.ITEM: row},
                )
                for index, row in enumerate(items)
            ]
        else:
            contexts = [("", {templates.Scope.INPUTS: values})]
        where = f"no passo '{step.id}'" if step is not None else "nas pós-condições"
        found: list[str] = []
        for suffix, context in contexts:
            try:
                key = templates.render(template, context)
            except templates.TemplateError:
                continue
            if not _normalize_text(key):
                found.append(
                    f"o texto que escolhe o contêiner de '{target.intent}' {where} "
                    f"ficaria vazio{suffix}"
                )
        return found

    def _check_list_limits(self, values: Mapping[str, Any]) -> None:
        """Recusa, antes do primeiro passo, uma lista maior que a repetição que a percorre."""
        for step, _ in self._manifest.iter_steps():
            if not isinstance(step, ForEachStep):
                continue
            scope, name = step.items.split(".", 1)
            items = values.get(name) if scope == "inputs" else None
            if isinstance(items, list) and len(items) > step.max_items:
                err = self._too_many_items(step, len(items))
                err.step_id = step.id
                err.reported = True  # o passo nem começou: não há evento nem tela
                raise err

    @staticmethod
    def _too_many_items(step: ForEachStep, count: int) -> StepError:
        return StepError(
            ErrorCode.LIMITE_DE_ITENS_EXCEDIDO,
            f"'{step.items}' tem {count} itens, mais que o limite de "
            f"{step.max_items} do passo '{step.title}'.",
            details={"itens": count, "limite": step.max_items},
        )

    def _collect_outputs(self) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for spec in self._manifest.outputs:
            name = spec.source.split(".", 1)[1]
            outputs[spec.name] = (
                self._output_value(spec.type, self._vars[name]) if name in self._vars else None
            )
        return outputs

    def _output_value(self, kind: FieldType, raw: Any) -> Any:
        """Valor de uma saída, já mascarado.

        A máscara vem antes da conversão de tipo: um segredo só de dígitos viraria
        número e escaparia dela. Se a máscara mudou algo, fica o texto mascarado.
        """
        if isinstance(raw, ArtifactRef):
            # Só o hash, calculado pelo próprio executor, fica fora da máscara.
            data = raw.model_dump(mode="json")
            return {
                key: item if key == "sha256" else self._redactor.value(item)
                for key, item in data.items()
            }
        masked = self._redactor.value(raw)
        if masked != raw:
            return masked
        converted = _coerce_output(kind, raw)
        if (
            isinstance(raw, str)
            and isinstance(converted, (int, float))
            and not isinstance(converted, bool)
            and any(
                self._redactor.text(text) != text for text in _number_texts(kind, raw, converted)
            )
        ):
            # A conversão juntou um segredo (por exemplo, tirando os pontos de um número).
            return MASK
        return converted

    async def _check_postconditions(self) -> None:
        conditions = self._manifest.postconditions
        if not conditions:
            return
        loop = asyncio.get_running_loop()
        if loop.time() >= self._deadline:
            raise StepError(
                ErrorCode.TEMPO_ESGOTADO,
                "O prazo da execução acabou antes de conferir as pós-condições.",
            )
        deadline = min(loop.time() + self._postcondition_timeout_s, self._deadline)
        for index, condition in enumerate(conditions, start=1):
            if not await self._wait_condition(condition, deadline):
                if deadline >= self._deadline:
                    raise StepError(
                        ErrorCode.TEMPO_ESGOTADO,
                        "O prazo da execução acabou enquanto as pós-condições eram conferidas.",
                    )
                raise StepError(
                    ErrorCode.POS_CONDICAO_FALHOU,
                    f"A pós-condição nº {index} ({_describe_condition(condition)}) "
                    "não foi satisfeita no fim da execução.",
                    details={"pos_condicao": index},
                )

    # ------------------------------------------------------------------
    # Eventos, telas e segurança
    # ------------------------------------------------------------------

    async def _emit(
        self, kind: RunEventKind, step_id: str | None, data: Mapping[str, Any] | None = None
    ) -> None:
        event = RunEvent(
            run_id=self._run_id,
            attempt=self._attempt,
            seq=self._seq,
            at=datetime.now(UTC),
            kind=kind,
            step_id=step_id,
            data=self._mask_event_data(data or {}),
        )
        self._seq += 1
        await self._events.emit(event)

    def _mask_event_data(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Mascara os dados de um evento.

        As chaves de primeiro nível são constantes do executor e nunca são
        mascaradas: o plano de controle decide por elas, e um segredo que fosse um
        pedaço de uma delas (``vers`` em ``irreversivel``) apagaria o aviso do passo
        irreversível, e o executor agiria sem esperar a confirmação. Também ficam
        intactos os valores que são constantes do executor (efeito, estado, código
        de erro, id de passo...) e os hashes que ele mesmo calcula, onde um segredo
        curto poderia aparecer por acaso. O resto, inclusive as chaves de dentro,
        que podem vir da página, é mascarado.
        """
        return {
            key: item if self._is_event_constant(key, item) else self._redactor.value(item)
            for key, item in data.items()
        }

    def _is_event_constant(self, key: str, item: Any) -> bool:
        if key in _HASH_KEYS:
            return isinstance(item, str) and _HEX_DIGEST.fullmatch(item) is not None
        allowed = self._event_constants.get(key)
        if allowed is None:
            return False
        if isinstance(item, str):
            return item in allowed
        return isinstance(item, list) and all(
            isinstance(value, str) and value in allowed for value in item
        )

    async def _emit_step_started(self, step: Step, *, irreversible: bool = False) -> None:
        data: dict[str, Any] = {
            "tipo": step.kind,
            "titulo": step.title,
            "efeito": step.effect.value,
        }
        if self._items:
            data["iteracao"] = self._items[-1][1]
        if irreversible:
            data["irreversivel"] = True
        await self._emit(RunEventKind.STEP_STARTED, step.id, data)

    async def _screenshot(self, step_id: str | None, reason: Literal["depois", "falha"]) -> None:
        """Guarda a tela atual (melhor esforço: uma falha aqui não derruba a execução)."""
        page = self._page
        if page is None or page.is_closed():
            return
        self._screenshots += 1
        name = f"tela-{self._screenshots:03d}-{step_id or 'execucao'}-{reason}.png"
        try:
            data = await page.screenshot(
                type="png",
                full_page=reason == "falha",
                timeout=_SCREENSHOT_TIMEOUT_MS,
                animations="disabled",
                caret="hide",
                mask=list(self._secret_locators),
            )
            ref = await self._artifact_sink.save_bytes(ArtifactKind.SCREENSHOT, name, data)
        except Exception as exc:
            await self._emit(
                RunEventKind.ARTIFACT,
                step_id,
                {"tipo": ArtifactKind.SCREENSHOT.value, "erro": type(exc).__name__},
            )
            return
        await self._register_artifact(step_id, ref)

    async def _register_artifact(self, step_id: str | None, ref: ArtifactRef) -> None:
        self._artifacts.append(ref)
        await self._emit(
            RunEventKind.ARTIFACT,
            step_id,
            {"tipo": ref.kind, "nome": ref.name, "tamanho": ref.size, "sha256": ref.sha256},
        )

    async def _guard_request(self, route: Route) -> None:
        """Bloqueia requisições a domínios fora da lista do robô (como o proxy de saída)."""
        request = route.request
        url = request.url
        try:
            scheme = urlsplit(url).scheme
        except ValueError:
            scheme = ""
        host = _host(url)
        try:
            if scheme not in _DEFAULT_PORTS or host in self._allowed_hosts:
                await route.fallback()
                return
            if request.is_navigation_request():
                try:
                    main_frame = request.frame.parent_frame is None
                except PlaywrightError:
                    main_frame = False
                if main_frame:
                    self._blocked_navigation = host
            await route.abort("blockedbyclient")
        except PlaywrightError:
            # O contexto pode ter sido fechado no meio do roteamento.
            return

    def _check_page_domain(self, step: Step) -> None:
        blocked = self._blocked_navigation
        if blocked is not None:
            self._blocked_navigation = None
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' tentou abrir '{blocked}', que não está nos domínios "
                "permitidos do robô.",
                details={"dominio": blocked},
            )
        page = self._page
        if page is None:
            return
        url = page.url
        host = _host(url)
        if urlsplit(url).scheme in _DEFAULT_PORTS and host not in self._allowed_hosts:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"Depois do passo '{step.title}', a página está em '{host}', fora dos domínios "
                "permitidos do robô.",
                details={"dominio": host},
            )

    def _raise_if_run_deadline(self, ctx: _StepCtx) -> None:
        """Se a espera acabou pelo prazo total da execução, o erro é ``tempo_esgotado``."""
        if ctx.deadline >= self._deadline:
            raise StepError(
                ErrorCode.TEMPO_ESGOTADO,
                f"O prazo da execução acabou durante o passo '{ctx.step.title}'.",
            )

    def _detail(self, exc: BaseException) -> str:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return self._redactor.text(first)[:_DETAIL_MAX]

    def _convert(self, step: Step, exc: Exception) -> StepError:
        """Traduz uma exceção do navegador (ou inesperada) num erro do passo."""
        if self._blocked_navigation is not None:
            try:
                self._check_page_domain(step)
            except StepError as err:
                return err
        loop = asyncio.get_running_loop()
        if isinstance(exc, (PlaywrightTimeoutError, TimeoutError)):
            if loop.time() >= self._deadline - 0.05:
                return StepError(
                    ErrorCode.TEMPO_ESGOTADO,
                    f"O prazo da execução acabou durante o passo '{step.title}'.",
                )
            return StepError(
                ErrorCode.TEMPO_DO_PASSO_ESGOTADO,
                f"O passo '{step.title}' passou de {step.timeout_ms} ms sem terminar.",
                details={"detalhe": self._detail(exc)},
            )
        if isinstance(exc, PlaywrightError):
            return StepError(
                ErrorCode.ACAO_FALHOU,
                f"O navegador não conseguiu concluir o passo '{step.title}'.",
                details={"detalhe": self._detail(exc)},
            )
        return StepError(
            ErrorCode.ERRO_INTERNO,
            f"Erro interno do executor no passo '{step.title}' ({type(exc).__name__}).",
        )

    # ------------------------------------------------------------------
    # Passos
    # ------------------------------------------------------------------

    async def _run_block(self, steps: list[Step]) -> None:
        for step in steps:
            await self._run_step(step)

    async def _run_step(self, step: Step) -> None:
        loop = asyncio.get_running_loop()
        if self._cancel is not None and self._cancel.is_set():
            raise _Cancelled(step)
        if loop.time() >= self._deadline:
            err = StepError(
                ErrorCode.TEMPO_ESGOTADO,
                f"O prazo da execução acabou antes do passo '{step.title}'.",
            )
            err.step_id = step.id
            err.reported = True  # o passo nem começou: não há evento nem tela
            raise err
        if self._mode is RunMode.UNTIL_FIRST_WRITE and step.effect is not Effect.READ:
            raise _StopBeforeWrite(step)
        if isinstance(step, (ForEachStep, IfStep)):
            await self._run_container(step)
        else:
            await self._run_leaf(step)

    async def _run_leaf(self, step: Step) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        ctx = _StepCtx(step, min(started + step.timeout_ms / 1000, self._deadline))
        irreversible = step.effect is Effect.IRREVERSIBLE
        announced = False
        if not irreversible:
            await self._emit_step_started(step)
            announced = True
        try:
            try:
                async with asyncio.timeout_at(ctx.deadline + _TIMEOUT_GRACE_S):
                    action = await self._prepare(step, ctx)
                    if irreversible:
                        # O aviso vai antes: com confirmação, ele pode esperar o plano de
                        # controle gravá-lo. Se falhar, nada foi enviado ao portal.
                        await self._emit_step_started(step, irreversible=True)
                        announced = True
                        # Daqui em diante, qualquer falha deixa a execução indeterminada.
                        self._irreversible_step = step
                    data = await action()
                self._check_page_domain(step)
            except StepError:
                raise
            except Exception as exc:
                raise self._convert(step, exc) from None
        except StepError as err:
            if not announced:
                await self._emit_step_started(step)
            await self._report_failure(step, err, started)
            raise
        self._steps_done += 1
        await self._emit(
            RunEventKind.STEP_SUCCEEDED,
            step.id,
            {"duracao_ms": round((loop.time() - started) * 1000), **data},
        )
        if self._screenshot_every_step or step.effect is not Effect.READ:
            await self._screenshot(step.id, "depois")

    async def _report_failure(self, step: Step, err: StepError, started: float) -> None:
        loop = asyncio.get_running_loop()
        duration = round((loop.time() - started) * 1000)
        if err.reported:
            # Falhou num passo de dentro: o contêiner também registra a falha.
            await self._emit(
                RunEventKind.STEP_FAILED,
                step.id,
                {
                    "codigo": str(err.code),
                    "mensagem": err.message,
                    "duracao_ms": duration,
                    "passo_interno": err.step_id,
                },
            )
            return
        err.step_id = step.id
        err.reported = True
        await self._emit(
            RunEventKind.STEP_FAILED,
            step.id,
            {
                "codigo": str(err.code),
                "mensagem": err.message,
                "duracao_ms": duration,
                **err.details,
            },
        )
        await self._screenshot(step.id, "falha")

    async def _run_container(self, step: ForEachStep | IfStep) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        # O prazo do contêiner não limita os passos de dentro: cada um tem o seu.
        irreversible = step.effect is Effect.IRREVERSIBLE
        await self._emit_step_started(step, irreversible=irreversible)
        if irreversible:
            self._irreversible_step = step
        try:
            if isinstance(step, ForEachStep):
                items = self._loop_items(step)
                for index, item in enumerate(items):
                    self._items.append((item, index))
                    try:
                        await self._run_block(step.steps)
                    finally:
                        self._items.pop()
                data: dict[str, Any] = {"itens": len(items)}
            else:
                holds = await self._holds(step.condition)
                await self._run_block(step.then if holds else step.otherwise)
                data = {"ramo": "entao" if holds else "senao"}
        except StepError as err:
            await self._report_failure(step, err, started)
            raise
        self._steps_done += 1
        await self._emit(
            RunEventKind.STEP_SUCCEEDED,
            step.id,
            {"duracao_ms": round((loop.time() - started) * 1000), **data},
        )

    def _loop_items(self, step: ForEachStep) -> list[Mapping[str, object]]:
        scope, name = step.items.split(".", 1)
        source = self._inputs if scope == "inputs" else self._vars
        value = source.get(name)
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise StepError(
                ErrorCode.LISTA_INVALIDA,
                f"'{step.items}' não é uma lista de itens no passo '{step.title}'.",
            )
        if len(value) > step.max_items:
            # Listas de entradas já foram conferidas antes do primeiro passo; aqui
            # sobram as tabelas extraídas da página.
            raise self._too_many_items(step, len(value))
        return list(value)

    async def _prepare(self, step: Step, ctx: _StepCtx) -> _Action:
        """Prepara o passo sem efeitos colaterais e devolve a ação que o executa."""
        match step:
            case NavigateStep():
                return self._prepare_navigate(step, ctx)
            case ClickStep():
                return await self._prepare_click(step, ctx)
            case FillStep():
                return await self._prepare_fill(step, ctx)
            case SelectStep():
                return await self._prepare_select(step, ctx)
            case CheckStep():
                return await self._prepare_check(step, ctx)
            case PressStep():
                return await self._prepare_press(step, ctx)
            case WaitForStep():
                return self._prepare_wait_for(step, ctx)
            case ExtractTextStep():
                return await self._prepare_extract_text(step, ctx)
            case ExtractTableStep():
                return await self._prepare_extract_table(step, ctx)
            case AssertStep():
                return self._prepare_assert(step, ctx)
            case DownloadStep():
                return await self._prepare_download(step, ctx)
            case UploadStep():
                return await self._prepare_upload(step, ctx)
            case ForEachStep() | IfStep():  # pragma: no cover - tratados como contêiner
                raise AssertionError("passo de controle tratado como passo simples")
            case _:  # pragma: no cover - conjunto fechado
                assert_never(step)

    @property
    def page(self) -> Page:
        if self._page is None:  # pragma: no cover - a página é aberta antes do 1º passo
            raise RuntimeError("a página ainda não foi aberta")
        return self._page

    def _prepare_navigate(self, step: NavigateStep, ctx: _StepCtx) -> _Action:
        url = self._render_url(step)
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            parts, host = urlsplit(""), ""
        if not host or host not in self._allowed_hosts:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' quer abrir '{host or 'um endereço sem domínio'}', "
                "que não está nos domínios permitidos do robô.",
                details={"dominio": host},
            )
        if parts.scheme != "https" and not (parts.scheme == "http" and host.endswith(TEST_TLD)):
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' quer abrir um endereço sem https.",
                details={"dominio": host},
            )
        if parts.username or parts.password:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O endereço do passo '{step.title}' não pode conter usuário ou senha.",
                details={"dominio": host},
            )

        async def act() -> dict[str, Any]:
            try:
                response = await self.page.goto(url, timeout=ctx.remaining_ms(), wait_until="load")
            except PlaywrightTimeoutError:
                raise
            except PlaywrightError as exc:
                if self._blocked_navigation is not None:
                    self._check_page_domain(step)
                raise StepError(
                    ErrorCode.NAVEGACAO_FALHOU,
                    f"Não consegui abrir a página do passo '{step.title}'.",
                    details={"detalhe": self._detail(exc)},
                ) from None
            return {"dominio": host, "status": response.status if response is not None else None}

        return act

    def _render_url(self, step: NavigateStep) -> str:
        """Monta o endereço codificando cada valor como um único trecho.

        ``?``, ``#`` ou ``&`` num valor nunca mudam o caminho nem a consulta; no
        caminho, valores vazios, com ``/`` ou ``\\`` ou iguais a ``.`` e ``..``
        são recusados. Domínio, porta e usuário vêm só do trecho literal do começo.
        Valores de entradas e itens já foram conferidos antes do primeiro passo;
        aqui a conferência pega os de variáveis.
        """
        try:
            slots = _url_slots(templates.parse(step.url))
        except templates.TemplateError:
            slots = {}  # _render recusa o template antes de codificar qualquer valor
        url = self._render(
            step.url,
            encode=lambda index, text: _encode_url_value(text, slots.get(index, "caminho")),
        )
        parts = templates.parse(step.url)  # já conferido por _render
        prefix = parts[0] if parts and isinstance(parts[0], str) else ""
        try:
            fixed = urlsplit(prefix).netloc.lower() == urlsplit(url).netloc.lower()
        except ValueError:
            fixed = False
        if not fixed:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O domínio do endereço do passo '{step.title}' precisa ser fixo; só o "
                "caminho e a consulta podem usar valores.",
                details={"dominio": _host(url)},
            )
        return url

    async def _prepare_click(self, step: ClickStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)
        navigation_owner: Page | Frame = self.page
        if step.expect_navigation and step.target.frames:
            frame = await self._owner_frame(locator, ctx)
            if frame is not None:
                navigation_owner = frame

        async def act() -> dict[str, Any]:
            if step.expect_navigation:
                async with navigation_owner.expect_navigation(
                    timeout=ctx.remaining_ms(), wait_until="load"
                ):
                    await locator.click(timeout=ctx.remaining_ms())
            else:
                await locator.click(timeout=ctx.remaining_ms())
            return {}

        return act

    async def _prepare_fill(self, step: FillStep, ctx: _StepCtx) -> _Action:
        value = step.value
        if not isinstance(value, SecretValue):
            text = self._render(value.template)
            locator = await self._resolve(step.target, step, ctx)

            async def act_text() -> dict[str, Any]:
                if step.sequential:
                    await locator.clear(timeout=ctx.remaining_ms())
                    await locator.press_sequentially(text, timeout=ctx.remaining_ms())
                else:
                    await locator.fill(text, timeout=ctx.remaining_ms())
                return {"origem": "texto", "tamanho": len(text)}

            return act_text

        credential = self._credentials.get(value.credential)
        if credential is None:
            raise StepError(
                ErrorCode.CREDENCIAL_DESCONHECIDA,
                f"A credencial '{value.credential}' do passo '{step.title}' não foi declarada.",
            )
        locator = await self._resolve(step.target, step, ctx)
        # O segredo vai para o elemento exato que foi conferido: se a página
        # navegar entre a conferência e a digitação, o elemento deixa de existir.
        handle = await self._element_handle(locator, ctx)
        frame = await handle.owner_frame()
        origin = _normalize_origin(frame.url) if frame is not None else ""
        expected = _normalize_origin(credential.origin)
        if not origin or origin != expected:
            await handle.dispose()
            raise StepError(
                ErrorCode.CREDENCIAL_ORIGEM_INVALIDA,
                f"O passo '{step.title}' digitaria a credencial '{credential.name}' em "
                f"'{origin or 'uma página sem origem'}', mas ela só pode ser usada em '{expected}'.",
                details={"origem": origin, "origem_da_credencial": expected},
            )
        try:
            secret = await self._secrets.get(credential, value.field)
        except SecretUnavailable as exc:
            await handle.dispose()
            raise StepError(
                ErrorCode.SEGREDO_INDISPONIVEL,
                f"O segredo do passo '{step.title}' não está disponível: {exc}",
            ) from None
        if value.field != "totp":
            # O código TOTP vale por segundos e nunca entra em eventos; mascarar
            # seis dígitos estragaria hashes e números legítimos.
            self._redactor.add(secret)
        self._secret_locators.append(locator)

        async def act_secret() -> dict[str, Any]:
            try:
                if step.sequential:
                    await handle.fill("", timeout=ctx.remaining_ms())
                    await handle.type(secret, timeout=ctx.remaining_ms())
                else:
                    await handle.fill(secret, timeout=ctx.remaining_ms())
            finally:
                await self._dispose(handle)
            return {"origem": "segredo", "tamanho": len(secret)}

        return act_secret

    async def _prepare_select(self, step: SelectStep, ctx: _StepCtx) -> _Action:
        option = self._render(step.option)
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await locator.select_option(label=option, timeout=ctx.remaining_ms())
            return {}

        return act

    async def _prepare_check(self, step: CheckStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await locator.set_checked(step.checked, timeout=ctx.remaining_ms())
            return {"marcado": step.checked}

        return act

    async def _prepare_press(self, step: PressStep, ctx: _StepCtx) -> _Action:
        if step.target is None:

            async def act_page() -> dict[str, Any]:
                await self.page.keyboard.press(step.key)
                return {"tecla": step.key}

            return act_page
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await locator.press(step.key, timeout=ctx.remaining_ms())
            return {"tecla": step.key}

        return act

    def _prepare_wait_for(self, step: WaitForStep, ctx: _StepCtx) -> _Action:
        needle = self._render(step.url_contains) if step.url_contains is not None else None

        async def act() -> dict[str, Any]:
            if needle is not None:
                await self.page.wait_for_url(
                    lambda url: needle in url, timeout=ctx.remaining_ms(), wait_until="load"
                )
            if step.target is not None:
                if step.state == "visible":
                    locator = await self._resolve(step.target, step, ctx)
                    await locator.wait_for(state="visible", timeout=ctx.remaining_ms())
                else:
                    await self._wait_hidden(step.target, ctx)
            return {}

        return act

    async def _prepare_extract_text(self, step: ExtractTextStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            text = (await locator.inner_text(timeout=ctx.remaining_ms())).strip()
            self._vars[step.into] = text
            return {"tamanho": len(text)}

        return act

    async def _prepare_extract_table(self, step: ExtractTableStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            raw = await locator.evaluate(_READ_TABLE_JS, step.max_rows, timeout=ctx.remaining_ms())
            # Cabeçalhos viram chaves: mascarados antes, os repetidos viram coluna_N.
            headers = [self._redactor.text(str(h)) for h in raw.get("headers", [])]
            rows = [[str(cell) for cell in row] for row in raw.get("rows", [])]
            keys = self._table_keys(step.columns, headers, rows)
            table = [
                {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(keys)}
                for cells in rows
            ]
            self._vars[step.into] = table
            total = int(raw.get("total", len(table)))
            return {"linhas": len(table), "colunas": len(keys), "truncada": total > len(table)}

        return act

    @staticmethod
    def _table_keys(
        columns: list[str] | None, headers: list[str], rows: list[list[str]]
    ) -> list[str]:
        if columns:
            return list(columns)
        width = max([len(headers), *(len(row) for row in rows)], default=0)
        keys: list[str] = []
        for index in range(width):
            key = headers[index] if index < len(headers) and headers[index] else ""
            if not key or key in keys:
                key = f"coluna_{index + 1}"
            keys.append(key)
        return keys

    def _prepare_assert(self, step: AssertStep, ctx: _StepCtx) -> _Action:
        async def act() -> dict[str, Any]:
            if not await self._wait_condition(step.condition, ctx.deadline):
                self._raise_if_run_deadline(ctx)
                raise StepError(
                    ErrorCode.VERIFICACAO_FALHOU,
                    step.message,
                    details={"condicao": _describe_condition(step.condition)},
                )
            return {}

        return act

    async def _prepare_download(self, step: DownloadStep, ctx: _StepCtx) -> _Action:
        name = self._artifact_name(self._render(step.save_as), default=f"{step.id}.bin")
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            async with self.page.expect_download(timeout=ctx.remaining_ms()) as info:
                await locator.click(timeout=ctx.remaining_ms())
            download = await info.value
            try:
                failure = await download.failure()
                if failure is not None:
                    raise StepError(
                        ErrorCode.DOWNLOAD_FALHOU,
                        f"O download do passo '{step.title}' falhou.",
                        details={"detalhe": self._redactor.text(failure)[:_DETAIL_MAX]},
                    )
                source = await download.path()
                try:
                    ref = await self._artifact_sink.save_file(ArtifactKind.DOWNLOAD, name, source)
                except OSError as exc:
                    raise StepError(
                        ErrorCode.ARTEFATO_FALHOU,
                        f"Não consegui guardar o arquivo baixado no passo '{step.title}'.",
                        details={"detalhe": type(exc).__name__},
                    ) from None
            finally:
                try:
                    await download.delete()
                except PlaywrightError:
                    pass
            await self._register_artifact(step.id, ref)
            # A própria referência: na saída, só o hash dela fica fora da máscara.
            self._vars[step.into] = ref
            return {"arquivo": ref.name, "tamanho": ref.size, "sha256": ref.sha256}

        return act

    def _artifact_name(self, rendered: str, *, default: str) -> str:
        """Nome saneado e sem segredos: vira o nome e o caminho do artefato."""
        name = sanitize_filename(self._redactor.text(rendered), default=default)
        # O saneamento tira acentos e separadores e poderia remontar um segredo.
        return name if self._redactor.text(name) == name else default

    async def _prepare_upload(self, step: UploadStep, ctx: _StepCtx) -> _Action:
        file_id = self._inputs.get(step.file_input)
        if not isinstance(file_id, str) or not file_id:
            raise StepError(
                ErrorCode.ARQUIVO_INDISPONIVEL,
                f"A entrada '{step.file_input}' não traz um arquivo para o passo '{step.title}'.",
            )
        try:
            path = await self._files.path_for(file_id)
        except FileUnavailable as exc:
            raise StepError(
                ErrorCode.ARQUIVO_INDISPONIVEL,
                f"O arquivo da entrada '{step.file_input}' não está disponível: {exc}",
            ) from None
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await locator.set_input_files(path, timeout=ctx.remaining_ms())
            size = await asyncio.to_thread(os.path.getsize, path)
            return {"tamanho": size}

        return act

    # ------------------------------------------------------------------
    # Templates, localizadores e condições
    # ------------------------------------------------------------------

    def _render(self, template: str, *, encode: Callable[[int, str], str] | None = None) -> str:
        scopes: dict[templates.Scope, Mapping[str, object]] = {
            templates.Scope.INPUTS: self._inputs,
            templates.Scope.VARS: self._vars,
        }
        if self._items:
            scopes[templates.Scope.ITEM] = self._items[-1][0]
        try:
            return templates.render(template, scopes, encode=encode)
        except templates.TemplateError as exc:
            raise StepError(
                ErrorCode.TEMPLATE_INVALIDO, f"Não consegui montar um valor do passo: {exc}."
            ) from None

    def _build(self, root: _Root, locator: Locator) -> PwLocator:
        value = self._render(locator.value)
        match locator.by:
            case LocatorBy.ROLE:
                role = cast(Any, value)
                if locator.name is None:
                    return root.get_by_role(role)
                return root.get_by_role(role, name=self._render(locator.name), exact=locator.exact)
            case LocatorBy.LABEL:
                return root.get_by_label(value, exact=locator.exact)
            case LocatorBy.PLACEHOLDER:
                return root.get_by_placeholder(value, exact=locator.exact)
            case LocatorBy.TEXT:
                return root.get_by_text(value, exact=locator.exact)
            case LocatorBy.TEST_ID:
                return root.get_by_test_id(value)
            case LocatorBy.CSS:
                return root.locator(f"css={value}")
            case LocatorBy.XPATH:
                return root.locator(f"xpath={value}")
            case _:  # pragma: no cover - conjunto fechado
                assert_never(locator.by)

    def _candidates(self, target: Target) -> list[PwLocator]:
        self._refuse_dynamic_selectors(target)
        root: Page | FrameLocator = self.page
        for selector in target.frames:
            root = root.frame_locator(selector)
        container: _Root = root
        if target.within is not None:
            within = self._build(root, target.within)
            if target.within_has_text is not None:
                # Normalizada como o Playwright normaliza: um texto só de U+200B,
                # U+00AD ou U+FEFF (a célula vazia de um CSV do Excel) fica vazio.
                key = _normalize_text(self._render(target.within_has_text))
                if not key:
                    # Sem texto, qualquer elemento vazio (uma célula, um botão só com
                    # ícone) serviria, e qualquer contêiner seria escolhido.
                    raise StepError(
                        ErrorCode.TEMPLATE_INVALIDO,
                        f"O texto que escolhe o contêiner de '{target.intent}' ficou vazio; "
                        "sem ele não há como saber qual usar.",
                    )
                # O texto precisa ser o texto inteiro de um elemento do contêiner (uma
                # célula, por exemplo), com os mesmos caracteres: "12" não escolhe a
                # linha da nota "120" nem a do valor "12,00". Maiúsculas não contam,
                # porque a chave gravada já passou pelo text-transform do CSS. Mais de
                # um contêiner com o texto continua ambíguo.
                within = within.filter(has=self.page.get_by_text(_whole_text(key)))
            container = within
        return [self._build(container, locator) for locator in target.locators]

    @staticmethod
    def _refuse_dynamic_selectors(target: Target) -> None:
        """Seletores CSS/XPath não escapam valores: um valor com aspas ou colchetes
        escolheria outro elemento. Vale também para robôs publicados antes de o
        portão de política recusar isso."""
        locators = [*target.locators, *([target.within] if target.within is not None else [])]
        for locator in locators:
            if locator.by in RAW_SELECTORS and _has_references(locator.value):
                raise StepError(
                    ErrorCode.TEMPLATE_INVALIDO,
                    f"O alvo '{target.intent}' usa uma entrada, variável ou item num "
                    f"localizador {locator.by.value}, onde o valor entraria cru no seletor. "
                    "O robô precisa de conserto.",
                    details={"estrategia": locator.by.value},
                )

    @staticmethod
    async def _count(locator: PwLocator) -> int:
        try:
            return await locator.count()
        except PlaywrightError:
            # A página pode estar no meio de uma navegação.
            return 0

    async def _resolve(self, target: Target, step: Step, ctx: _StepCtx) -> PwLocator:
        """Encontra o elemento-alvo, que precisa ser único.

        O localizador principal tem preferência. Em passos de leitura, os
        alternativos entram depois de ``heal_after_ms`` (ou no fim do prazo) e
        geram o evento ``step_healed``; em passos que gravam, só o principal vale.
        """
        candidates = self._candidates(target)
        allow_fallback = step.effect is Effect.READ
        loop = asyncio.get_running_loop()
        start = loop.time()
        heal_at = start + min(self._heal_after_s, max(0.0, (ctx.deadline - start) / 4))
        counts: list[int | None] = [None] * len(candidates)
        while True:
            last_round = loop.time() + self._poll_s >= ctx.deadline
            counts[0] = await self._count(candidates[0])
            if counts[0] == 1:
                return candidates[0]
            if allow_fallback and (last_round or loop.time() >= heal_at):
                for index in range(1, len(candidates)):
                    counts[index] = await self._count(candidates[index])
                    if counts[index] == 1:
                        await self._emit(
                            RunEventKind.STEP_HEALED,
                            step.id,
                            {
                                "localizador": index,
                                "estrategia": target.locators[index].by.value,
                                "contagens": list(counts),
                            },
                        )
                        return candidates[index]
            if last_round:
                break
            await asyncio.sleep(self._poll_s)

        self._raise_if_run_deadline(ctx)
        for index in range(1, len(candidates)):
            if counts[index] is None:
                counts[index] = await self._count(candidates[index])
        details: dict[str, object] = {
            "alvo": target.intent,
            "estrategias": [locator.by.value for locator in target.locators],
            "contagens": list(counts),
        }
        if not allow_fallback:
            fallback = next((i for i in range(1, len(counts)) if counts[i] == 1), None)
            if fallback is not None:
                raise StepError(
                    ErrorCode.LOCALIZADOR_ALTERNATIVO_EM_GRAVACAO,
                    f"O elemento '{target.intent}' só foi encontrado pelo localizador "
                    f"alternativo nº {fallback + 1}; em passos que gravam, só o localizador "
                    "principal pode ser usado. O robô precisa de conserto.",
                    details={**details, "localizador": fallback},
                )
        considered = counts if allow_fallback else counts[:1]
        most = max((count or 0) for count in considered)
        if most > 1:
            raise StepError(
                ErrorCode.ELEMENTO_AMBIGUO,
                f"O elemento '{target.intent}' corresponde a {most} elementos na página; "
                "o localizador precisa encontrar exatamente um.",
                details=details,
            )
        raise StepError(
            ErrorCode.ELEMENTO_NAO_ENCONTRADO,
            f"Não encontrei o elemento '{target.intent}' no passo '{step.title}'.",
            details=details,
        )

    async def _element_handle(self, locator: PwLocator, ctx: _StepCtx) -> ElementHandle:
        return await locator.element_handle(timeout=ctx.remaining_ms())

    async def _owner_frame(self, locator: PwLocator, ctx: _StepCtx) -> Frame | None:
        handle = await self._element_handle(locator, ctx)
        try:
            return await handle.owner_frame()
        finally:
            await self._dispose(handle)

    @staticmethod
    async def _dispose(handle: ElementHandle) -> None:
        try:
            await handle.dispose()
        except PlaywrightError:
            pass

    async def _wait_hidden(self, target: Target, ctx: _StepCtx) -> None:
        candidates = self._candidates(target)
        loop = asyncio.get_running_loop()
        while True:
            visible = 0
            for candidate in candidates:
                visible += await self._count(candidate.filter(visible=True))
            if visible == 0:
                return
            if loop.time() + self._poll_s >= ctx.deadline:
                self._raise_if_run_deadline(ctx)
                raise StepError(
                    ErrorCode.TEMPO_DO_PASSO_ESGOTADO,
                    f"O elemento '{target.intent}' continuou visível no passo '{ctx.step.title}'.",
                )
            await asyncio.sleep(self._poll_s)

    async def _first_unique(self, target: Target) -> PwLocator | None:
        for candidate in self._candidates(target):
            if await self._count(candidate) == 1:
                return candidate
        return None

    async def _holds(self, condition: Condition) -> bool:
        """Avalia a condição uma vez. Erros do navegador contam como 'falso'."""
        try:
            match condition:
                case VisibleCondition(target=target):
                    found = await self._first_unique(target)
                    result = found is not None and await found.is_visible()
                case TextContainsCondition(target=target, text=text):
                    needle = self._render(text)
                    if target is None:
                        haystack = await self.page.locator("body").inner_text(timeout=2_000)
                    else:
                        found = await self._first_unique(target)
                        haystack = await found.inner_text(timeout=2_000) if found else ""
                    result = needle in haystack
                case UrlContainsCondition(value=value):
                    result = self._render(value) in self.page.url
                case EqualsCondition(left=left, right=right):
                    result = self._render(left) == self._render(right)
                case _:  # pragma: no cover - conjunto fechado
                    assert_never(condition)
        except PlaywrightError:
            result = False
        return result != condition.negate

    async def _wait_condition(self, condition: Condition, deadline: float) -> bool:
        loop = asyncio.get_running_loop()
        while True:
            if await self._holds(condition):
                return True
            if loop.time() + self._poll_s >= deadline:
                return False
            await asyncio.sleep(self._poll_s)
