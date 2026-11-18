"""Cliente do hub interno de LLM (Chat Completions compatível com OpenAI, atrás do LiteLLM).

Campos documentados pelo hub: ``model``, ``messages``, ``max_tokens`` (até
8192), ``temperature`` e ``stream``. Todo o resto (desligar o raciocínio,
saída JSON com esquema, ferramentas, imagens) precisa ser comprovado pela
sonda (``rapa.llm.probe``), que grava uma ``CapabilityMatrix``; o cliente
consulta a matriz para decidir como pedir.

O endereço, a chave e o modelo vêm de ``RAPA_LLM_BASE_URL``,
``RAPA_LLM_API_KEY`` e ``RAPA_LLM_MODEL`` ou dos argumentos do construtor.
Prompts, respostas e a chave nunca vão para logs nem para mensagens de erro.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx2

from rapa.llm.capabilities import (
    DOCUMENTED_MAX_TOKENS,
    CapabilityMatrix,
    CheckStatus,
    ReasoningShape,
)
from rapa.llm.errors import (
    CapabilityMissing,
    HubAuthError,
    HubConfigError,
    HubError,
    HubRequestRejected,
    HubUnavailable,
    IncompleteResponse,
    ModelChanged,
)
from rapa.llm.messages import Message, validate_messages

__all__ = [
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_MODEL",
    "ChatResult",
    "HubClient",
    "HubSettings",
    "Thinking",
    "ToolCall",
    "Usage",
    "json_schema_format",
    "parse_chat_response",
    "parse_retry_after",
    "split_reasoning",
    "thinking_fields",
]

logger = logging.getLogger("rapa.llm")

ENV_BASE_URL = "RAPA_LLM_BASE_URL"
ENV_API_KEY = "RAPA_LLM_API_KEY"
ENV_MODEL = "RAPA_LLM_MODEL"

type Thinking = Literal["off", "low", "default"]

_PROTECTED_FIELDS = frozenset({"model", "messages"})
_NON_RETRYABLE_5XX = frozenset({501, 505})
_KEY_CHARS = re.compile(r"[\x21-\x7e]{1,4096}")
_BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"',;]+")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}")
_EXCERPT_CHARS = 300


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HubSettings:
    """Endereço, modelo e chave do hub. A chave não aparece no ``repr``."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def resolve(
        cls,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> HubSettings:
        """Usa os argumentos e, no que faltar, as variáveis de ambiente."""
        base = base_url or os.environ.get(ENV_BASE_URL, "")
        if not base.strip():
            raise HubConfigError(f"defina o endereço do hub em {ENV_BASE_URL}")
        parts = urlsplit(base.strip())
        if parts.username or parts.password:
            raise HubConfigError("o endereço do hub não pode conter usuário ou senha")
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise HubConfigError("o endereço do hub deve começar com https:// e ter um domínio")
        if parts.query or parts.fragment:
            raise HubConfigError("o endereço do hub não pode ter consulta nem fragmento")
        chosen_model = (model or os.environ.get(ENV_MODEL, "")).strip()
        if not chosen_model:
            raise HubConfigError(f"defina o modelo em {ENV_MODEL} (por exemplo, qwen-latest)")
        key = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        key = key.strip() if key else None
        if key and not _KEY_CHARS.fullmatch(key):
            raise HubConfigError(f"a chave de acesso em {ENV_API_KEY} tem caracteres inválidos")
        return cls(base_url=base.strip().rstrip("/"), model=chosen_model, api_key=key or None)


# ---------------------------------------------------------------------------
# Resultado de uma chamada
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: Tokens de raciocínio (contam no max_tokens), quando o hub informa.
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str | None
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any] | None:
        """Argumentos como objeto JSON, ou None se não forem um objeto válido."""
        try:
            value = json.loads(self.arguments)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Resposta normalizada: o raciocínio sai do conteúdo, venha no formato que vier."""

    content: str
    reasoning: str | None
    finish_reason: str | None
    usage: Usage
    model: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_shape: ReasoningShape = ReasoningShape.NONE
    latency_ms: int = 0
    attempts: int = 1
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_reasoning(self) -> bool:
        return bool(self.reasoning) or bool(self.usage.reasoning_tokens)


# ---------------------------------------------------------------------------
# Normalização da resposta
# ---------------------------------------------------------------------------

_HARMONY_ANALYSIS = re.compile(
    r"<\|channel\|>\s*analysis\s*<\|message\|>(.*?)(?=<\|end\|>|<\|start\|>|<\|channel\|>|$)",
    re.DOTALL,
)
_HARMONY_FINAL = re.compile(
    r"<\|channel\|>\s*final\s*(?:<\|constrain\|>[^<]*)?<\|message\|>", re.DOTALL
)
_HARMONY_TAIL = re.compile(r"(?:<\|(?:end|return|call)\|>\s*)+$")


def _content_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        texts = [
            part["text"]
            for part in raw
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        return "".join(texts)
    return ""


def split_reasoning(message: Mapping[str, Any]) -> tuple[str, str | None, ReasoningShape]:
    """Separa resposta e raciocínio de uma mensagem do modelo.

    Aceita os formatos ``message.reasoning_content``, ``message.reasoning``,
    raciocínio no texto terminado em ``</think>`` (com ou sem ``<think>`` no
    começo) e o formato harmony cru (``<|channel|>analysis<|message|>…``).
    """
    content = _content_text(message.get("content"))
    parts: list[str] = []
    shape = ReasoningShape.NONE
    for key, kind in (
        ("reasoning_content", ReasoningShape.REASONING_CONTENT),
        ("reasoning", ReasoningShape.REASONING),
    ):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
            shape = kind
            break

    if "<|channel|>" in content or "<|message|>" in content:
        final = _HARMONY_FINAL.search(content)
        analysis = " ".join(m.strip() for m in _HARMONY_ANALYSIS.findall(content) if m.strip())
        content = _HARMONY_TAIL.sub("", content[final.end() :]) if final else ""
        if analysis:
            parts.append(analysis)
            if shape is ReasoningShape.NONE:
                shape = ReasoningShape.INLINE_HARMONY

    if "</think>" in content:
        before, _, after = content.partition("</think>")
        inline = before.replace("<think>", "").strip()
        content = after
        if inline:
            parts.append(inline)
            if shape is ReasoningShape.NONE:
                shape = ReasoningShape.INLINE_THINK
    elif content.lstrip().startswith("<think>"):
        # Raciocínio cortado antes de fechar: não há resposta.
        inline = content.lstrip().removeprefix("<think>").strip()
        content = ""
        if inline:
            parts.append(inline)
            if shape is ReasoningShape.NONE:
                shape = ReasoningShape.INLINE_THINK

    return content.strip(), ("\n\n".join(parts) or None), shape


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    details = raw.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
    return Usage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
        total_tokens=_as_int(raw.get("total_tokens")),
        reasoning_tokens=_as_int(reasoning),
    )


def _tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, ensure_ascii=False)
        elif not isinstance(arguments, str):
            arguments = ""
        call_id = item.get("id")
        calls.append(
            ToolCall(
                id=call_id if isinstance(call_id, str) else None,
                name=function["name"],
                arguments=arguments,
            )
        )
    return tuple(calls)


def parse_chat_response(
    data: Mapping[str, Any], *, latency_ms: int = 0, attempts: int = 1
) -> ChatResult:
    """Converte o JSON de Chat Completions num ``ChatResult``.

    Levanta ``ValueError`` se a resposta não tiver o formato esperado.
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ValueError("a resposta do hub não tem 'choices'")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("a resposta do hub não tem 'message'")
    content, reasoning, shape = split_reasoning(message)
    finish = choice.get("finish_reason")
    model = data.get("model")
    return ChatResult(
        content=content,
        reasoning=reasoning,
        finish_reason=finish if isinstance(finish, str) else None,
        usage=_usage(data.get("usage")),
        model=model if isinstance(model, str) else None,
        tool_calls=_tool_calls(message.get("tool_calls")),
        reasoning_shape=shape,
        latency_ms=latency_ms,
        attempts=attempts,
        raw=data,
    )


def _incomplete_reason(result: ChatResult) -> str | None:
    if result.finish_reason == "length":
        return "a resposta do modelo foi cortada pelo limite de tokens"
    if not result.content and not result.tool_calls:
        return "o modelo devolveu uma resposta vazia"
    return None


# ---------------------------------------------------------------------------
# Campos opcionais
# ---------------------------------------------------------------------------


def thinking_fields(thinking: Thinking, capabilities: CapabilityMatrix | None) -> dict[str, Any]:
    """Campos que controlam o raciocínio, segundo a matriz de capacidades.

    ``off`` usa ``chat_template_kwargs.enable_thinking=false``; se a matriz
    disser que isso não é respeitado, cai para ``reasoning_effort="low"``
    (o template do Qwen só aceita ``xhigh``, ``medium`` e ``low``). ``low``
    usa ``reasoning_effort="low"``. ``default`` não manda nada (o Qwen pensa no
    esforço máximo).
    """
    if thinking == "default":
        return {}
    template_ok = (
        capabilities is None or capabilities.thinking_off_template_kwargs.status is CheckStatus.YES
    )
    effort_usable = (
        capabilities is None or capabilities.thinking_off_reasoning_effort.accepted is not False
    )
    template = {"chat_template_kwargs": {"enable_thinking": False}}
    effort = {"reasoning_effort": "low"}
    if thinking == "off":
        if template_ok:
            return template
        return effort if effort_usable else {}
    if thinking == "low":
        if effort_usable:
            return effort
        return template if template_ok else {}
    raise ValueError(f"modo de raciocínio inválido: {thinking!r}; use off, low ou default")


def json_schema_format(schema: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    """``response_format`` do tipo ``json_schema`` para um JSON Schema."""
    title = name or schema.get("title") or "resposta"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(title))[:64] or "resposta"
    return {"type": "json_schema", "json_schema": {"name": safe, "schema": dict(schema)}}


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Segundos pedidos em ``retry-after-ms`` ou ``Retry-After`` (número ou data HTTP)."""
    millis = headers.get("retry-after-ms")
    if millis:
        try:
            return max(0.0, float(millis) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError, IndexError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Retry:
    """Falha que vale nova tentativa."""

    error: HubError
    retry_after: float | None = None


type _Outcome = tuple[dict[str, Any], int] | _Retry | HubError


class HubClient:
    """Cliente assíncrono do hub, sobre ``httpx2.AsyncClient``.

    Repete com espera exponencial e variação aleatória em 429, 5xx, tempo
    esgotado e falha de rede, respeitando ``Retry-After``. Em ``chat``,
    resposta vazia ou cortada (``finish_reason == "length"``) também é
    repetida, dobrando ``max_tokens`` até o teto.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        capabilities: CapabilityMatrix | None = None,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        max_backoff_s: float = 30.0,
        max_retry_after_s: float = 60.0,
        pin_fingerprint: bool = True,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries não pode ser negativo")
        self._settings = HubSettings.resolve(base_url=base_url, api_key=api_key, model=model)
        self.capabilities = capabilities
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._max_backoff_s = max_backoff_s
        self._max_retry_after_s = max_retry_after_s
        self._pin_fingerprint = pin_fingerprint
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()  # noqa: S311 - variação da espera, não criptografia
        headers = {"Accept": "application/json", "User-Agent": "rapa-llm"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        self._http = httpx2.AsyncClient(
            base_url=self._settings.base_url + "/",
            headers=headers,
            timeout=httpx2.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            transport=transport,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"HubClient(model={self.model!r}, base_url={self.base_url!r})"

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def base_url(self) -> str:
        return self._settings.base_url

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- API pública ---------------------------------------------------------

    async def list_models(self) -> list[str]:
        """Ids dos modelos em GET /models."""

        def interpret(data: dict[str, Any], latency_ms: int, attempt: int) -> list[str] | _Retry:
            items = data.get("data")
            if not isinstance(items, list):
                return _Retry(HubUnavailable("a listagem de modelos veio sem 'data'"))
            return [
                item["id"]
                for item in items
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            ]

        return await self._with_retries("GET", "models", None, interpret)

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        thinking: Thinking = "off",
        response_schema: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        capabilities: CapabilityMatrix | None = None,
    ) -> ChatResult:
        """Uma conversa de Chat Completions.

        ``capabilities`` substitui a matriz do cliente nesta chamada.
        ``response_schema`` pede ``response_format`` ``json_schema`` (quem
        chama decide, pela matriz, se o hub o respeita). ``extra`` acrescenta
        campos ao corpo, mas não pode trocar ``model`` nem ``messages``.
        """
        caps = capabilities if capabilities is not None else self.capabilities
        if caps is not None and caps.alias != self.model:
            raise CapabilityMissing(
                f"a matriz de capacidades é do modelo '{caps.alias}', não de '{self.model}'; "
                "rode a sonda para este modelo"
            )
        checked = validate_messages(messages)
        ceiling = DOCUMENTED_MAX_TOKENS
        if caps is not None and caps.max_tokens_above_documented.ok:
            ceiling = max(ceiling, caps.max_tokens_ceiling)
        if not 1 <= max_tokens <= ceiling:
            raise ValueError(f"max_tokens deve ficar entre 1 e {ceiling}")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature deve ficar entre 0 e 2")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": checked,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body.update(thinking_fields(thinking, caps))
        if response_schema is not None:
            body["response_format"] = json_schema_format(response_schema)
        if extra:
            protected = _PROTECTED_FIELDS & set(extra)
            if protected:
                raise ValueError(f"'extra' não pode trocar {', '.join(sorted(protected))}")
            body.update(extra)
        return await self.send_chat(
            body, retry_incomplete=True, max_tokens_ceiling=ceiling, capabilities=caps
        )

    async def send_chat(
        self,
        body: Mapping[str, Any],
        *,
        retry_incomplete: bool = False,
        max_tokens_ceiling: int = DOCUMENTED_MAX_TOKENS,
        capabilities: CapabilityMatrix | None = None,
    ) -> ChatResult:
        """Manda um corpo de Chat Completions já montado (usado pela sonda).

        Só repete falhas de transporte e de HTTP, a menos que
        ``retry_incomplete`` peça para repetir também respostas vazias ou
        cortadas. Não confere as mensagens: use ``chat`` para isso.
        """
        payload = dict(body)
        payload.setdefault("model", self.model)

        def interpret(data: dict[str, Any], latency_ms: int, attempt: int) -> ChatResult | _Retry:
            try:
                result = parse_chat_response(data, latency_ms=latency_ms, attempts=attempt)
            except ValueError as exc:
                return _Retry(HubUnavailable(str(exc)))
            self._check_fingerprint(result, capabilities)
            usage = result.usage
            logger.debug(
                "hub: resposta de %s em %d ms (entrada=%s saída=%s raciocínio=%s, fim=%s)",
                result.model,
                latency_ms,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.reasoning_tokens,
                result.finish_reason,
            )
            if retry_incomplete and (reason := _incomplete_reason(result)) is not None:
                if result.finish_reason == "length":
                    current = int(payload.get("max_tokens") or DOCUMENTED_MAX_TOKENS)
                    payload["max_tokens"] = min(max_tokens_ceiling, current * 2)
                return _Retry(IncompleteResponse(reason))
            return result

        return await self._with_retries("POST", "chat/completions", payload, interpret)

    # -- internos ------------------------------------------------------------

    def _check_fingerprint(self, result: ChatResult, caps: CapabilityMatrix | None) -> None:
        if not self._pin_fingerprint or caps is None or caps.fingerprint is None:
            return
        if result.model is not None and result.model != caps.fingerprint:
            raise ModelChanged(
                f"o modelo por trás de '{caps.alias}' mudou desde a sonda "
                f"('{caps.fingerprint}' → '{result.model}'); rode a sonda de novo"
            )

    def _redact(self, text: str) -> str:
        key = self._settings.api_key
        if key:
            text = text.replace(key, "***")
        text = _BEARER.sub(r"\1***", text)
        return _SK_TOKEN.sub("sk-***", text)

    def _excerpt(self, response: httpx2.Response) -> str | None:
        try:
            text = response.text[:4000]
        except UnicodeDecodeError, httpx2.ResponseNotRead:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, Mapping):
            error = data.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(error, str):
                text = error
            elif isinstance(data.get("detail"), str):
                text = data["detail"]
        excerpt = self._redact(text).strip()[:_EXCERPT_CHARS]
        return excerpt or None

    async def _once(
        self, method: str, path: str, body: Mapping[str, Any] | None, attempt: int
    ) -> _Outcome:
        """Uma requisição. Nunca deixa escapar exceções do httpx2 (elas levam a requisição)."""
        start = time.perf_counter()
        response: httpx2.Response | None = None
        failure: str | None = None
        try:
            if body is None:
                response = await self._http.request(method, path)
            else:
                response = await self._http.request(method, path, json=body)
        except httpx2.TimeoutException:
            failure = "o hub não respondeu dentro do tempo limite"
        except httpx2.RequestError as exc:
            failure = f"falha de rede ao falar com o hub ({type(exc).__name__})"
        latency_ms = int((time.perf_counter() - start) * 1000)
        if response is None:
            logger.warning("hub: %s %s falhou: %s (tentativa %d)", method, path, failure, attempt)
            return _Retry(HubUnavailable(failure or "falha ao falar com o hub"))

        status = response.status_code
        logger.info(
            "hub: %s %s → HTTP %d em %d ms (tentativa %d)",
            method,
            path,
            status,
            latency_ms,
            attempt,
        )
        if 200 <= status < 300:
            parsed: Any = None
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                return _Retry(HubUnavailable("o hub devolveu uma resposta que não é JSON"))
            return parsed, latency_ms
        excerpt = self._excerpt(response)
        retry_after = parse_retry_after(response.headers)
        if status in {401, 403}:
            return HubAuthError(
                f"o hub recusou a chave de acesso (HTTP {status}); confira {ENV_API_KEY}",
                status_code=status,
                body_excerpt=excerpt,
            )
        if status in {408, 425, 429} or (status >= 500 and status not in _NON_RETRYABLE_5XX):
            return _Retry(
                HubUnavailable(
                    f"o hub respondeu HTTP {status}",
                    status_code=status,
                    body_excerpt=excerpt,
                    retry_after_s=retry_after,
                ),
                retry_after,
            )
        if 300 <= status < 400:
            return HubRequestRejected(
                f"o hub respondeu com redirecionamento (HTTP {status}); confira {ENV_BASE_URL}",
                status_code=status,
            )
        return HubRequestRejected(
            f"o hub recusou a requisição (HTTP {status})", status_code=status, body_excerpt=excerpt
        )

    def _delay(self, attempt: int, retry_after: float | None) -> float | None:
        if retry_after is not None:
            if retry_after > self._max_retry_after_s:
                return None
            return retry_after + self._rng.uniform(0.0, min(1.0, self._backoff_base_s))
        base = min(self._max_backoff_s, self._backoff_base_s * 2.0 ** (attempt - 1))
        return base / 2 + self._rng.uniform(0.0, base / 2)

    async def _with_retries[T](
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        interpret: Callable[[dict[str, Any], int, int], T | _Retry],
    ) -> T:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            outcome = await self._once(method, path, body, attempt)
            if isinstance(outcome, HubError):
                raise outcome
            if isinstance(outcome, _Retry):
                retry = outcome
            else:
                data, latency_ms = outcome
                interpreted = interpret(data, latency_ms, attempt)
                if not isinstance(interpreted, _Retry):
                    return interpreted
                retry = interpreted
            error = retry.error
            delay = self._delay(attempt, retry.retry_after) if attempt < attempts else None
            if delay is None:
                suffix = (
                    f" (pediu para esperar {retry.retry_after:.0f} s)"
                    if attempt < attempts and retry.retry_after is not None
                    else f" (depois de {attempt} tentativa{'s' if attempt > 1 else ''})"
                )
                raise type(error)(
                    error.message + suffix,
                    status_code=error.status_code,
                    body_excerpt=error.body_excerpt,
                    retry_after_s=error.retry_after_s,
                )
            logger.warning(
                "hub: %s; nova tentativa em %.1f s (%d de %d)",
                error.message,
                delay,
                attempt + 1,
                attempts,
            )
            await self._sleep(delay)
        raise AssertionError("inalcançável")
