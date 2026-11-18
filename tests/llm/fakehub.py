"""Hub falso compatível com OpenAI, para testar o cliente sem rede.

Simula os comportamentos que a sonda precisa distinguir: onde vem o
raciocínio, se o hub respeita ou ignora os campos de raciocínio, se impõe
``response_format``, se converte chamadas de ferramenta, se aceita imagens e
``max_tokens`` alto, se recusa campos desconhecidos. Respostas roteirizadas
(``script``) têm prioridade sobre o comportamento padrão.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2

from rapa.llm.capabilities import CapabilityMatrix, Check, CheckStatus, ReasoningShape
from rapa.llm.client import HubClient

BASE_URL = "https://hub.exemplo.test/v1"
API_KEY = "sk-teste-NAO-VAZAR-0123456789abcdef"
DOCUMENTED = frozenset({"model", "messages", "max_tokens", "temperature", "stream"})
KNOWN_EXTRAS = frozenset(
    {
        "chat_template_kwargs",
        "reasoning_effort",
        "response_format",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "logprobs",
        "top_logprobs",
    }
)

type ScriptItem = httpx2.Response | Exception | dict[str, Any] | Callable[[dict[str, Any]], Any]


def error(status: int, message: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": message}}, headers=headers)


def completion_message(
    content: str | None, finish_reason: str = "stop", **extra: Any
) -> dict[str, Any]:
    """Item de roteiro: uma resposta com este conteúdo."""
    return {"content": content, "finish_reason": finish_reason, **extra}


def _instance(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """Instância mínima válida de um JSON Schema simples."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _instance(defs[name], defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return _instance(schema["anyOf"][0], defs)
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        return {name: _instance(props[name], defs) for name in schema.get("required", props)}
    if kind == "array":
        count = schema.get("minItems", 0)
        return [_instance(schema.get("items", {}), defs) for _ in range(count)]
    if kind == "integer":
        return 12
    if kind == "number":
        return 1.5
    if kind == "boolean":
        return True
    return "Recife"


@dataclass
class FakeHub:
    model_ids: list[str] = field(default_factory=lambda: ["qwen-latest", "openai/gpt-oss-20b"])
    fingerprint: str = "Qwen/Qwen3.8-27B"
    api_key: str | None = API_KEY
    reasoning_shape: Literal["reasoning_content", "reasoning", "inline", "harmony", "hidden"] = (
        "reasoning_content"
    )
    thinks_by_default: bool = True
    honours_template_kwargs: bool = True
    honours_reasoning_effort: bool = False
    rejects_unknown_fields: bool = False
    known_extras: frozenset[str] = KNOWN_EXTRAS
    json_schema: Literal["enforced", "ignored", "rejected"] = "enforced"
    tools: Literal["structured", "xml", "rejected"] = "structured"
    tool_choice_required: bool = True
    parallel_tools: bool = True
    images: Literal["ok", "ignored", "rejected"] = "ok"
    max_tokens_limit: int = 32_768
    logprobs: bool = True
    reasoning_tokens: int = 40
    script: list[ScriptItem] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    # -- transporte ----------------------------------------------------------

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def client(self, **kwargs: Any) -> HubClient:
        """Cliente apontado para este hub, sem esperas reais."""

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        kwargs.setdefault("base_url", BASE_URL)
        kwargs.setdefault("api_key", API_KEY)
        kwargs.setdefault("model", "qwen-latest")
        return HubClient(transport=self.transport(), sleep=sleep, **kwargs)

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [body for body in self.requests if "messages" in body]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.headers_seen.append(dict(request.headers))
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.requests.append(body)
        auth = request.headers.get("authorization", "")
        if self.api_key is not None and auth != f"Bearer {self.api_key}":
            # Como alguns gateways, devolve a chave recebida no corpo do erro.
            return error(401, f"Authentication Error, Invalid API Key passed: {auth}")
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, httpx2.Response):
                return item
            if callable(item):
                result = item(body)
                if isinstance(result, httpx2.Response):
                    return result
                item = result
            return self.completion(body, item)
        if request.url.path.endswith("/models"):
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": m, "object": "model"} for m in self.model_ids],
                },
            )
        if not request.url.path.endswith("/chat/completions"):
            return error(404, "not found")
        return self.default(body)

    # -- comportamento padrão ------------------------------------------------

    def thinking_active(self, body: dict[str, Any]) -> bool:
        if not self.thinks_by_default:
            return False
        kwargs = body.get("chat_template_kwargs") or {}
        if self.honours_template_kwargs and kwargs.get("enable_thinking") is False:
            return False
        return not (self.honours_reasoning_effort and body.get("reasoning_effort") == "low")

    @staticmethod
    def user_text(body: dict[str, Any]) -> tuple[str, bool]:
        texts: list[str] = []
        image = False
        for message in body.get("messages", []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                image = any(p.get("type") == "image_url" for p in content)
        return "\n".join(texts), image

    def default(self, body: dict[str, Any]) -> httpx2.Response:
        extras = set(body) - DOCUMENTED
        if self.rejects_unknown_fields:
            unknown = sorted(extras - self.known_extras)
            if unknown:
                return error(400, f"Unrecognized request argument supplied: {', '.join(unknown)}")
        effort = body.get("reasoning_effort")
        if effort is not None and effort not in {"xhigh", "medium", "low"}:
            return error(400, f"chat template error: reasoning_effort {effort!r} inválido")
        if int(body.get("max_tokens", 0)) > self.max_tokens_limit:
            return error(400, "max_tokens is too large")
        if "response_format" in body and self.json_schema == "rejected":
            return error(400, "response_format is not supported")
        if "tools" in body and self.tools == "rejected":
            return error(400, "tools are not supported")
        if body.get("tool_choice") == "required" and not self.tool_choice_required:
            return error(400, "tool_choice 'required' is not supported")
        text, image = self.user_text(body)
        if image and self.images == "rejected":
            return error(400, "image input is not supported for this model")

        if "tools" in body:
            pairs = [(int(a), int(b)) for a, b in re.findall(r"(\d+)\s*\+\s*(\d+)", text)]
            if not self.parallel_tools:
                pairs = pairs[:1]
            if self.tools == "xml":
                calls = "".join(
                    f'<tool_call>\n{{"name": "somar", "arguments": {{"a": {a}, "b": {b}}}}}\n</tool_call>'
                    for a, b in pairs
                )
                return self.completion(body, completion_message(calls))
            tool_calls = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "somar", "arguments": json.dumps({"a": a, "b": b})},
                }
                for i, (a, b) in enumerate(pairs)
            ]
            return self.completion(
                body, completion_message(None, "tool_calls", tool_calls=tool_calls)
            )

        response_format = body.get("response_format")
        if response_format and self.json_schema == "enforced":
            schema = response_format["json_schema"]["schema"]
            answer = json.dumps(_instance(schema, schema.get("$defs", {})), ensure_ascii=False)
        elif image:
            answer = "Vermelho." if self.images == "ok" else "Não consigo ver imagens."
        elif match := re.search(r"Quanto é (\d+) \+ (\d+)", text):
            answer = str(int(match.group(1)) + int(match.group(2)))
        elif "Não use JSON" in text:
            answer = "A casa da Rua das Flores, número doze, em Recife, tem a etiqueta histórica."
        elif "Responda apenas: sim" in text:
            answer = "sim"
        else:
            answer = "ok"
        return self.completion(body, completion_message(answer))

    def completion(self, body: dict[str, Any], item: dict[str, Any]) -> httpx2.Response:
        """Monta a resposta, colocando o raciocínio no formato configurado."""
        item = dict(item)
        content: str | None = item.pop("content")
        finish = item.pop("finish_reason", "stop")
        message: dict[str, Any] = {"role": "assistant", "content": content, **item}
        reasoning_tokens = 0
        if self.thinking_active(body):
            reasoning = "Vou pensar com calma antes de responder."
            reasoning_tokens = self.reasoning_tokens
            if int(body.get("max_tokens", 4096)) < reasoning_tokens + 5:
                # O raciocínio consumiu o max_tokens: resposta vazia e cortada.
                content, finish = "", "length"
                message["content"] = ""
            match self.reasoning_shape:
                case "reasoning_content":
                    message["reasoning_content"] = reasoning
                case "reasoning":
                    message["reasoning"] = reasoning
                case "inline":
                    message["content"] = f"{reasoning}\n</think>\n\n{content or ''}"
                case "harmony":
                    message["content"] = (
                        f"<|channel|>analysis<|message|>{reasoning}<|end|>"
                        f"<|start|>assistant<|channel|>final<|message|>{content or ''}<|return|>"
                    )
                case "hidden":
                    pass
        choice: dict[str, Any] = {"index": 0, "message": message, "finish_reason": finish}
        if body.get("logprobs") and self.logprobs:
            choice["logprobs"] = {
                "content": [{"token": "sim", "logprob": -0.01, "top_logprobs": []}]
            }
        completion_tokens = len((content or "").split()) + reasoning_tokens + 1
        usage = {
            "prompt_tokens": 12,
            "completion_tokens": completion_tokens,
            "total_tokens": 12 + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-teste",
                "object": "chat.completion",
                "model": self.fingerprint,
                "choices": [choice],
                "usage": usage,
            },
        )


def yes(detail: str = "") -> Check:
    return Check(status=CheckStatus.YES, accepted=True, http_status=200, detail=detail)


def no(*, accepted: bool = True) -> Check:
    return Check(status=CheckStatus.NO, accepted=accepted, http_status=200 if accepted else 400)


def make_matrix(alias: str = "qwen-latest", **overrides: Any) -> CapabilityMatrix:
    """Matriz com tudo funcionando, exceto o que ``overrides`` trocar."""
    values: dict[str, Any] = {
        "alias": alias,
        "models_listed": ["qwen-latest", "openai/gpt-oss-20b"],
        "alias_listed": yes(),
        "fingerprint": "Qwen/Qwen3.8-27B",
        "baseline": yes(),
        "rejects_unknown_fields": no(),
        "thinking_off_template_kwargs": yes(),
        "thinking_off_reasoning_effort": no(),
        "reasoning_shape": ReasoningShape.REASONING_CONTENT,
        "json_schema_enforced": yes(),
        "tools_structured": yes(),
        "tool_choice_required": yes(),
        "parallel_tool_calls": yes(),
        "image_input": yes(),
        "max_tokens_above_documented": no(accepted=False),
        "logprobs": yes(),
    }
    values.update(overrides)
    return CapabilityMatrix(**values)
