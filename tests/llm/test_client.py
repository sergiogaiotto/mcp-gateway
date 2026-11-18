import logging
import traceback
from typing import Any

import httpx2
import pytest

from rapa.llm.capabilities import Check, CheckStatus, ReasoningShape
from rapa.llm.client import (
    HubClient,
    HubSettings,
    parse_retry_after,
    split_reasoning,
    thinking_fields,
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
from tests.llm.fakehub import API_KEY, BASE_URL, FakeHub, completion_message, error, make_matrix, no

QUESTION = [
    {"role": "system", "content": "Você é objetivo."},
    {"role": "user", "content": "Quanto é 17 + 25? Responda apenas com o número."},
]


# ---------------------------------------------------------------------------
# Raciocínio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("reasoning_content", ReasoningShape.REASONING_CONTENT),
        ("reasoning", ReasoningShape.REASONING),
        ("inline", ReasoningShape.INLINE_THINK),
        ("harmony", ReasoningShape.INLINE_HARMONY),
    ],
)
async def test_reasoning_shapes_are_normalised(shape: Any, expected: ReasoningShape) -> None:
    hub = FakeHub(reasoning_shape=shape)
    async with hub.client() as client:
        result = await client.chat(QUESTION, thinking="default")
    assert result.content == "42"
    assert result.reasoning == "Vou pensar com calma antes de responder."
    assert result.reasoning_shape is expected
    assert result.usage.reasoning_tokens == 40
    assert result.model == "Qwen/Qwen3.8-27B"
    assert result.finish_reason == "stop"


def test_split_reasoning_variants() -> None:
    assert split_reasoning({"content": "<think>\npensando\n</think>\n\nresposta"}) == (
        "resposta",
        "pensando",
        ReasoningShape.INLINE_THINK,
    )
    # Raciocínio cortado antes de fechar: não sobra resposta.
    assert split_reasoning({"content": "<think>pensando sem fim"}) == (
        "",
        "pensando sem fim",
        ReasoningShape.INLINE_THINK,
    )
    # Bloco vazio (raciocínio desligado no template).
    assert split_reasoning({"content": "<think>\n\n</think>\n\nok"}) == (
        "ok",
        None,
        ReasoningShape.NONE,
    )
    assert split_reasoning({"content": "ok", "reasoning_content": "   "}) == (
        "ok",
        None,
        ReasoningShape.NONE,
    )
    content, reasoning, shape = split_reasoning(
        {"content": "<|channel|>analysis<|message|>analisando<|end|>"}
    )
    assert (content, reasoning, shape) == ("", "analisando", ReasoningShape.INLINE_HARMONY)
    content, _, _ = split_reasoning(
        {"content": [{"type": "text", "text": "parte 1 "}, {"type": "text", "text": "parte 2"}]}
    )
    assert content == "parte 1 parte 2"


async def test_thinking_off_sends_template_kwargs() -> None:
    hub = FakeHub()
    async with hub.client() as client:
        result = await client.chat(QUESTION)
    assert hub.chat_requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in hub.chat_requests[0]
    assert result.reasoning is None
    assert not result.has_reasoning


async def test_thinking_off_falls_back_to_reasoning_effort() -> None:
    matrix = make_matrix(
        thinking_off_template_kwargs=no(), thinking_off_reasoning_effort=no(accepted=True)
    )
    hub = FakeHub(honours_template_kwargs=False, honours_reasoning_effort=True)
    async with hub.client(capabilities=matrix) as client:
        result = await client.chat(QUESTION)
    body = hub.chat_requests[0]
    assert body["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in body
    assert result.reasoning is None


def test_thinking_fields_table() -> None:
    template = {"chat_template_kwargs": {"enable_thinking": False}}
    effort = {"reasoning_effort": "low"}
    assert thinking_fields("off", None) == template
    assert thinking_fields("low", None) == effort
    assert thinking_fields("default", None) == {}
    both_rejected = make_matrix(
        thinking_off_template_kwargs=no(accepted=False),
        thinking_off_reasoning_effort=no(accepted=False),
    )
    assert thinking_fields("off", both_rejected) == {}
    assert thinking_fields("low", both_rejected) == {}
    effort_rejected = make_matrix(thinking_off_reasoning_effort=no(accepted=False))
    assert thinking_fields("low", effort_rejected) == template
    with pytest.raises(ValueError):
        thinking_fields("high", None)  # type: ignore[arg-type]


async def test_thinking_ignored_by_hub_is_visible_in_the_result() -> None:
    hub = FakeHub(honours_template_kwargs=False)
    async with hub.client() as client:
        result = await client.chat(QUESTION)
    assert result.content == "42"
    assert result.has_reasoning


# ---------------------------------------------------------------------------
# Novas tentativas
# ---------------------------------------------------------------------------


async def test_429_with_retry_after_then_success() -> None:
    hub = FakeHub(script=[error(429, "rate limited", {"Retry-After": "2"})])
    async with hub.client() as client:
        result = await client.chat(QUESTION)
    assert result.content == "42"
    assert result.attempts == 2
    assert len(hub.sleeps) == 1
    assert 2.0 <= hub.sleeps[0] <= 3.0


async def test_retry_after_ms_and_http_date() -> None:
    assert parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert parse_retry_after({"retry-after": "3"}) == 3.0
    assert parse_retry_after({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0
    assert parse_retry_after({"retry-after": "amanhã"}) is None
    assert parse_retry_after({}) is None


async def test_5xx_and_timeouts_use_exponential_backoff_with_jitter() -> None:
    request = httpx2.Request("POST", f"{BASE_URL}/chat/completions")
    hub = FakeHub(
        script=[
            error(503, "indisponível"),
            httpx2.ReadTimeout("tempo esgotado", request=request),
            httpx2.ConnectError("recusado", request=request),
        ]
    )
    async with hub.client(backoff_base_s=1.0) as client:
        result = await client.chat(QUESTION)
    assert result.attempts == 4
    assert len(hub.sleeps) == 3
    for attempt, delay in enumerate(hub.sleeps, start=1):
        base = 2.0 ** (attempt - 1)
        assert base / 2 <= delay <= base


async def test_gives_up_after_max_retries() -> None:
    hub = FakeHub(script=[error(502, "bad gateway")] * 5)
    async with hub.client(max_retries=2) as client:
        with pytest.raises(HubUnavailable, match="depois de 3 tentativas") as info:
            await client.chat(QUESTION)
    assert info.value.status_code == 502
    assert len(hub.requests) == 3


async def test_retry_after_too_long_stops_immediately() -> None:
    hub = FakeHub(script=[error(429, "devagar", {"Retry-After": "600"})])
    async with hub.client(max_retry_after_s=60) as client:
        with pytest.raises(HubUnavailable, match="esperar 600 s") as info:
            await client.chat(QUESTION)
    assert info.value.retry_after_s == 600
    assert hub.sleeps == []


async def test_empty_content_with_length_then_success_doubles_max_tokens() -> None:
    hub = FakeHub(script=[completion_message("", "length")])
    async with hub.client() as client:
        result = await client.chat(QUESTION, max_tokens=1000)
    assert result.content == "42"
    assert [body["max_tokens"] for body in hub.chat_requests] == [1000, 2000]


async def test_truncated_content_is_retried_up_to_the_ceiling() -> None:
    hub = FakeHub(script=[completion_message('{"a": ', "length")] * 4)
    async with hub.client() as client:
        with pytest.raises(IncompleteResponse, match="cortada"):
            await client.chat(QUESTION, max_tokens=3000)
    assert [body["max_tokens"] for body in hub.chat_requests] == [3000, 6000, 8192, 8192]


async def test_empty_content_stop_is_retried() -> None:
    hub = FakeHub(script=[completion_message("   ")])
    async with hub.client() as client:
        result = await client.chat(QUESTION)
    assert result.content == "42"
    assert len(hub.chat_requests) == 2


async def test_send_chat_does_not_retry_incomplete_by_default() -> None:
    hub = FakeHub(script=[completion_message("", "length")])
    async with hub.client() as client:
        result = await client.send_chat({"messages": QUESTION, "max_tokens": 10})
    assert result.content == ""
    assert result.finish_reason == "length"
    assert len(hub.requests) == 1


async def test_non_retryable_4xx() -> None:
    hub = FakeHub(rejects_unknown_fields=True)
    async with hub.client() as client:
        with pytest.raises(HubRequestRejected) as info:
            await client.chat(QUESTION, extra={"campo_inventado": 1})
    assert info.value.status_code == 400
    assert info.value.body_excerpt is not None
    assert "campo_inventado" in info.value.body_excerpt
    assert len(hub.requests) == 1


async def test_non_json_body_is_retried() -> None:
    hub = FakeHub(script=[httpx2.Response(200, text="<html>proxy</html>")])
    async with hub.client() as client:
        result = await client.chat(QUESTION)
    assert result.content == "42"


# ---------------------------------------------------------------------------
# Requisição
# ---------------------------------------------------------------------------


async def test_request_body_and_headers() -> None:
    hub = FakeHub()
    schema = {"title": "Resposta Final", "type": "object", "properties": {}}
    async with hub.client() as client:
        await client.chat(QUESTION, max_tokens=100, temperature=0.0, response_schema=schema)
    body = hub.chat_requests[0]
    assert body["model"] == "qwen-latest"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.0
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Resposta_Final", "schema": schema},
    }
    assert hub.headers_seen[0]["authorization"] == f"Bearer {API_KEY}"


async def test_parameter_limits() -> None:
    hub = FakeHub()
    async with hub.client() as client:
        with pytest.raises(ValueError, match="max_tokens"):
            await client.chat(QUESTION, max_tokens=8193)
        with pytest.raises(ValueError, match="temperature"):
            await client.chat(QUESTION, temperature=3)
        with pytest.raises(ValueError, match="model"):
            await client.chat(QUESTION, extra={"model": "outro"})
    big = make_matrix(
        max_tokens_above_documented=Check(status=CheckStatus.YES, accepted=True),
        max_tokens_ceiling=16_384,
    )
    async with hub.client(capabilities=big) as client:
        await client.chat(QUESTION, max_tokens=16_000)
    assert [body["max_tokens"] for body in hub.chat_requests] == [16_000]


async def test_matrix_of_another_model_is_refused() -> None:
    hub = FakeHub()
    async with hub.client(capabilities=make_matrix(alias="openai/gpt-oss-20b")) as client:
        with pytest.raises(CapabilityMissing, match="rode a sonda para este modelo"):
            await client.chat(QUESTION)
    assert hub.requests == []


async def test_list_models() -> None:
    hub = FakeHub()
    async with hub.client() as client:
        assert await client.list_models() == ["qwen-latest", "openai/gpt-oss-20b"]


async def test_fingerprint_change_blocks_use() -> None:
    hub = FakeHub(fingerprint="Qwen/Qwen3.9-27B")
    async with hub.client(capabilities=make_matrix()) as client:
        with pytest.raises(ModelChanged, match="rode a sonda"):
            await client.chat(QUESTION)
    async with hub.client(capabilities=make_matrix(), pin_fingerprint=False) as client:
        assert (await client.chat(QUESTION)).content == "42"


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPA_LLM_BASE_URL", "https://hub.exemplo.test/v1/")
    monkeypatch.setenv("RAPA_LLM_API_KEY", API_KEY)
    monkeypatch.setenv("RAPA_LLM_MODEL", "openai/gpt-oss-20b")
    settings = HubSettings.resolve()
    assert settings.base_url == "https://hub.exemplo.test/v1"
    assert settings.model == "openai/gpt-oss-20b"
    assert settings.api_key == API_KEY
    assert API_KEY not in repr(settings)
    # Argumentos têm prioridade sobre o ambiente.
    assert HubSettings.resolve(model="qwen-latest").model == "qwen-latest"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({}, "RAPA_LLM_BASE_URL"),
        ({"RAPA_LLM_BASE_URL": "https://hub.exemplo.test/v1"}, "RAPA_LLM_MODEL"),
        ({"RAPA_LLM_BASE_URL": "ftp://hub.exemplo.test", "RAPA_LLM_MODEL": "m"}, "https://"),
        (
            {"RAPA_LLM_BASE_URL": "https://ana:segredo@hub.exemplo.test", "RAPA_LLM_MODEL": "m"},
            "usuário ou senha",
        ),
        (
            {
                "RAPA_LLM_BASE_URL": "https://hub.exemplo.test",
                "RAPA_LLM_MODEL": "m",
                "RAPA_LLM_API_KEY": "chave com espaço\n",
            },
            "caracteres inválidos",
        ),
    ],
)
def test_settings_errors(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], fragment: str
) -> None:
    for name in ("RAPA_LLM_BASE_URL", "RAPA_LLM_API_KEY", "RAPA_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(HubConfigError, match=fragment) as info:
        HubSettings.resolve()
    assert "segredo" not in str(info.value)


def test_client_without_key_sends_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAPA_LLM_API_KEY", raising=False)
    client = HubClient(base_url=BASE_URL, model="qwen-latest")
    assert "authorization" not in client._http.headers
    assert repr(client) == f"HubClient(model='qwen-latest', base_url='{BASE_URL}')"


# ---------------------------------------------------------------------------
# A chave nunca vaza
# ---------------------------------------------------------------------------


def _exception_texts(exc: BaseException) -> list[str]:
    texts = [str(exc), repr(exc), *map(str, exc.args)]
    texts.append("".join(traceback.format_exception(exc)))
    for name in ("message", "body_excerpt"):
        value = getattr(exc, name, None)
        if value:
            texts.append(str(value))
    chained = exc.__cause__ or exc.__context__
    if chained is not None:
        texts.extend(_exception_texts(chained))
    return texts


async def test_api_key_never_appears_in_exceptions_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    prompt_secret = "PROMPT-SENTINELA-987"
    question = [{"role": "user", "content": f"Quanto é 1 + 1? {prompt_secret}"}]
    request = httpx2.Request("POST", f"{BASE_URL}/chat/completions")
    failures: list[BaseException] = []

    # 1) Chave errada: o gateway devolve no corpo a chave recebida.
    wrong = FakeHub(api_key="sk-outra-chave-correta")
    async with wrong.client() as client:
        with pytest.raises(HubAuthError) as auth:
            await client.chat(question)
    failures.append(auth.value)

    # 2) Erros 5xx que repetem a chave, até esgotar as tentativas.
    echo = error(500, f"upstream falhou com Authorization: Bearer {API_KEY} e api_key={API_KEY}")
    hub = FakeHub(script=[echo] * 4)
    async with hub.client() as client:
        with pytest.raises(HubUnavailable) as unavailable:
            await client.chat(question)
    failures.append(unavailable.value)

    # 3) Tempo esgotado e falha de rede: a exceção do httpx2 leva a requisição com os cabeçalhos.
    hub = FakeHub(script=[httpx2.ReadTimeout("t", request=request)] * 2)
    async with hub.client(max_retries=1) as client:
        with pytest.raises(HubUnavailable) as timeout:
            await client.chat(question)
    failures.append(timeout.value)
    assert timeout.value.__context__ is None and timeout.value.__cause__ is None

    # 4) Resposta cortada até o fim.
    hub = FakeHub(script=[completion_message("", "length")] * 4)
    async with hub.client() as client:
        with pytest.raises(IncompleteResponse) as incomplete:
            await client.chat(question)
    failures.append(incomplete.value)

    for failure in failures:
        assert isinstance(failure, HubError)
        for text in _exception_texts(failure):
            assert API_KEY not in text
            assert prompt_secret not in text
    assert auth.value.body_excerpt is not None and "***" in auth.value.body_excerpt
    assert "sk-outra" not in (auth.value.body_excerpt or "")
    assert caplog.records, "esperava registros de log"
    assert API_KEY not in caplog.text
    assert prompt_secret not in caplog.text
    assert "Quanto é" not in caplog.text
