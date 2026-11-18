"""Sonda automática das capacidades do hub para um apelido de modelo.

Cada verificação é uma requisição pequena e julga pelo efeito, não pela
ausência de erro: um gateway que descarta campos desconhecidos responde 200
para tudo, então "aceitou" não quer dizer "respeitou". Erros são registrados na
matriz e não interrompem a sonda.

Uso::

    python -m rapa.llm.probe --model qwen-latest --out matrix.json

O endereço e a chave vêm de ``RAPA_LLM_BASE_URL`` e ``RAPA_LLM_API_KEY``.
"""

import argparse
import asyncio
import base64
import json
import re
import struct
import sys
import time
import zlib
from collections.abc import Sequence
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict

from rapa.llm.capabilities import (
    Capability,
    CapabilityMatrix,
    Check,
    CheckStatus,
    ReasoningShape,
    capability_label,
    ensure_capabilities,
    matrix_changes,
)
from rapa.llm.client import ChatResult, HubClient, json_schema_format, thinking_fields
from rapa.llm.errors import (
    CapabilityMissing,
    HubAuthError,
    HubConfigError,
    HubError,
    HubRequestRejected,
)

__all__ = [
    "LARGE_MAX_TOKENS",
    "PROBE_JSON_SCHEMA",
    "CapabilityMatrix",
    "ensure_capabilities",
    "main",
    "probe",
    "render_summary",
]

#: max_tokens testado acima do teto documentado.
LARGE_MAX_TOKENS = 16_384

_SYSTEM = "Você é um assistente objetivo. Responda em português."
_UNKNOWN_FIELD = "rapa_sonda_campo_inexistente"

#: Esquema aninhado da verificação de response_format.
PROBE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cidade": {"type": "string"},
        "endereco": {
            "type": "object",
            "properties": {"rua": {"type": "string"}, "numero": {"type": "integer"}},
            "required": ["rua", "numero"],
            "additionalProperties": False,
        },
        "etiquetas": {
            "type": "array",
            "items": {"type": "string", "enum": ["residencial", "comercial"]},
            "minItems": 1,
        },
    },
    "required": ["cidade", "endereco", "etiquetas"],
    "additionalProperties": False,
}
# Pede texto corrido, número por extenso e uma etiqueta fora da lista: só um
# esquema imposto pelo servidor produz um JSON válido aqui.
_JSON_TEMPTATION = (
    "Não use JSON. Escreva um parágrafo curto, em texto corrido, sobre uma casa na "
    "Rua das Flores, número doze, em Recife, e diga que a etiqueta dela é 'histórica'."
)

_SUM_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "somar",
        "description": "Soma dois números inteiros.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
}


class _ProbeAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rua: str
    numero: int


class _ProbeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cidade: str
    endereco: _ProbeAddress
    etiquetas: list[Literal["residencial", "comercial"]]


def _png(size: int = 64, rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    """PNG de uma cor só, gerado sem dependências."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


RED_IMAGE_URL = "data:image/png;base64," + base64.b64encode(_png()).decode()


def _has_number(text: str, number: int) -> bool:
    return re.search(rf"(?<![\d.,]){number}(?![\d])", text) is not None


def _inline_tool_call(text: str) -> bool:
    return "<tool_call>" in text or re.search(r"<function\s*=", text) is not None


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Prober:
    def __init__(self, client: HubClient) -> None:
        self.client = client
        self.matrix = CapabilityMatrix(alias=client.model)
        self.auth_failed = False

    # -- infraestrutura ------------------------------------------------------

    def body(
        self,
        content: str | list[dict[str, Any]],
        *,
        max_tokens: int,
        thinking_off: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.client.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if thinking_off:
            body.update(thinking_fields("off", self.matrix))
        body.update(extra)
        return body

    def record(self, name: str, check: Check) -> Check:
        if check.status in {CheckStatus.ERROR, CheckStatus.NO} and check.accepted is not True:
            self.matrix.errors.append(f"{name}: {check.detail}")
        return check

    async def call(self, body: dict[str, Any]) -> tuple[ChatResult | None, Check]:
        """Uma requisição; devolve o resultado (ou None) e uma verificação pré-preenchida."""
        if self.auth_failed:
            return None, Check(status=CheckStatus.SKIPPED, detail="a chave foi recusada pelo hub")
        start = time.perf_counter()
        try:
            result = await self.client.send_chat(body)
        except HubError as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            detail = exc.message
            if exc.body_excerpt:
                detail = f"{detail}: {exc.body_excerpt}"
            if isinstance(exc, HubAuthError):
                self.auth_failed = True
            rejected = isinstance(exc, HubRequestRejected) and not isinstance(exc, HubAuthError)
            return None, Check(
                status=CheckStatus.NO if rejected else CheckStatus.ERROR,
                accepted=False if exc.status_code is not None else None,
                http_status=exc.status_code,
                detail=_short(detail),
                elapsed_ms=elapsed,
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        return result, Check(
            status=CheckStatus.ERROR, accepted=True, http_status=200, elapsed_ms=elapsed
        )

    @staticmethod
    def judged(check: Check, ok: bool, detail: str) -> Check:
        status = CheckStatus.YES if ok else CheckStatus.NO
        return check.model_copy(update={"status": status, "detail": _short(detail)})

    # -- verificações --------------------------------------------------------

    async def models(self) -> None:
        start = time.perf_counter()
        try:
            listed = await self.client.list_models()
        except HubError as exc:
            if isinstance(exc, HubAuthError):
                self.auth_failed = True
            self.matrix.alias_listed = self.record(
                "modelos",
                Check(
                    status=CheckStatus.ERROR,
                    accepted=False if exc.status_code is not None else None,
                    http_status=exc.status_code,
                    detail=_short(exc.message),
                    elapsed_ms=int((time.perf_counter() - start) * 1000),
                ),
            )
            return
        self.matrix.models_listed = sorted(listed)
        found = self.client.model in listed
        self.matrix.alias_listed = Check(
            status=CheckStatus.YES if found else CheckStatus.NO,
            accepted=True,
            http_status=200,
            detail="" if found else f"'{self.client.model}' não aparece em /models",
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )

    async def baseline(self) -> None:
        body = self.body(
            "Quanto é 17 + 25? Responda apenas com o número.",
            max_tokens=4096,
            thinking_off=False,
        )
        start = time.perf_counter()
        result, check = await self.call(body)
        elapsed_s = time.perf_counter() - start
        if result is None:
            self.matrix.baseline = self.record("linha de base", check)
            return
        self.matrix.fingerprint = result.model
        self.matrix.latency_ms = int(elapsed_s * 1000)
        completion = result.usage.completion_tokens
        if completion and elapsed_s > 0:
            self.matrix.tokens_per_second = round(completion / elapsed_s, 1)
        if result.reasoning_shape is not ReasoningShape.NONE:
            self.matrix.reasoning_shape = result.reasoning_shape
        elif result.usage.reasoning_tokens:
            self.matrix.reasoning_shape = ReasoningShape.UNKNOWN
        else:
            self.matrix.reasoning_shape = ReasoningShape.NONE
        correct = _has_number(result.content, 42)
        if correct:
            detail = "resposta correta"
        elif result.finish_reason == "length":
            detail = "a resposta foi cortada pelo limite de tokens"
        else:
            detail = "resposta errada ou vazia"
        self.matrix.baseline = self.record("linha de base", self.judged(check, correct, detail))

    async def unknown_fields(self) -> None:
        body = self.body("Responda apenas: ok", max_tokens=16, thinking_off=False)
        body[_UNKNOWN_FIELD] = True
        result, check = await self.call(body)
        if result is not None:
            self.matrix.rejects_unknown_fields = self.judged(
                check,
                False,
                "o hub aceita e ignora campos desconhecidos: aceitar não prova suporte",
            )
        elif check.status is CheckStatus.NO:
            self.matrix.rejects_unknown_fields = check.model_copy(
                update={
                    "status": CheckStatus.YES,
                    "detail": f"recusa campos desconhecidos ({check.detail})",
                }
            )
        else:
            self.matrix.rejects_unknown_fields = self.record("campos desconhecidos", check)

    async def thinking(self, extra: dict[str, Any], a: int, b: int, name: str) -> Check:
        body = self.body(
            f"Quanto é {a} + {b}? Responda apenas com o número.",
            max_tokens=512,
            thinking_off=False,
            **extra,
        )
        result, check = await self.call(body)
        if result is None:
            return self.record(name, check)
        absent = not result.has_reasoning
        correct = _has_number(result.content, a + b)
        if absent and correct:
            detail = "sem raciocínio e resposta correta"
        elif not absent:
            detail = "o modelo raciocinou mesmo assim"
        else:
            detail = "sem raciocínio, mas a resposta veio errada ou vazia"
        return self.judged(check, absent and correct, detail)

    async def json_schema(self) -> None:
        body = self.body(
            _JSON_TEMPTATION,
            max_tokens=512,
            response_format=json_schema_format(PROBE_JSON_SCHEMA, "endereco_sonda"),
        )
        result, check = await self.call(body)
        if result is None:
            self.matrix.json_schema_enforced = self.record("json_schema", check)
            return
        try:
            _ProbeRecord.model_validate(json.loads(result.content))
        except ValueError:
            self.matrix.json_schema_enforced = self.judged(
                check, False, "o hub aceitou response_format, mas não impôs o esquema"
            )
            return
        self.matrix.json_schema_enforced = self.judged(check, True, "esquema imposto")

    def judge_tools(self, check: Check, result: ChatResult, minimum: int) -> Check:
        calls = [call for call in result.tool_calls if call.name == "somar"]
        valid = [
            call
            for call in calls
            if (args := call.parsed_arguments()) is not None
            and isinstance(args.get("a"), int)
            and isinstance(args.get("b"), int)
        ]
        if len(valid) >= minimum:
            return self.judged(check, True, f"{len(valid)} chamada(s) estruturada(s)")
        if _inline_tool_call(result.content):
            return self.judged(check, False, "a chamada veio como XML no texto, sem tool_calls")
        if calls:
            return self.judged(
                check, False, f"{len(valid)} chamada(s) válida(s); esperava {minimum}"
            )
        return self.judged(check, False, "o modelo não chamou a ferramenta")

    async def tools(self) -> None:
        result, check = await self.call(
            self.body(
                "Use a ferramenta somar para calcular 20 + 22.",
                max_tokens=512,
                tools=[_SUM_TOOL],
                tool_choice="auto",
            )
        )
        self.matrix.tools_structured = (
            self.judge_tools(check, result, 1) if result else self.record("ferramentas", check)
        )

        result, check = await self.call(
            self.body("Calcule 20 + 22.", max_tokens=512, tools=[_SUM_TOOL], tool_choice="required")
        )
        self.matrix.tool_choice_required = (
            self.judge_tools(check, result, 1) if result else self.record("tool_choice", check)
        )

        if self.matrix.tools_structured.status is not CheckStatus.YES:
            self.matrix.parallel_tool_calls = Check(
                status=CheckStatus.SKIPPED, detail="depende das chamadas de ferramenta estruturadas"
            )
            return
        result, check = await self.call(
            self.body(
                "Use a ferramenta somar duas vezes, em paralelo, na mesma resposta: "
                "uma para 1 + 2 e outra para 3 + 4.",
                max_tokens=512,
                tools=[_SUM_TOOL],
                tool_choice="auto",
                parallel_tool_calls=True,
            )
        )
        self.matrix.parallel_tool_calls = (
            self.judge_tools(check, result, 2) if result else self.record("paralelas", check)
        )

    async def image(self) -> None:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": "Qual é a cor desta imagem? Responda com uma palavra."},
            {"type": "image_url", "image_url": {"url": RED_IMAGE_URL}},
        ]
        result, check = await self.call(self.body(content, max_tokens=64))
        if result is None:
            self.matrix.image_input = self.record("imagem", check)
            return
        seen = re.search(r"vermelh|\bred\b|rubro", result.content, re.IGNORECASE) is not None
        self.matrix.image_input = self.judged(
            check, seen, "reconheceu a cor" if seen else "aceitou, mas não reconheceu a imagem"
        )

    async def large_max_tokens(self) -> None:
        result, check = await self.call(
            self.body("Responda apenas: ok", max_tokens=LARGE_MAX_TOKENS)
        )
        if result is None:
            self.matrix.max_tokens_above_documented = self.record("max_tokens", check)
            return
        self.matrix.max_tokens_above_documented = self.judged(
            check, True, f"max_tokens={LARGE_MAX_TOKENS} aceito"
        )
        self.matrix.max_tokens_ceiling = LARGE_MAX_TOKENS

    async def logprobs(self) -> None:
        result, check = await self.call(
            self.body("Responda apenas: sim", max_tokens=8, logprobs=True, top_logprobs=2)
        )
        if result is None:
            self.matrix.logprobs = self.record("logprobs", check)
            return
        choices = result.raw.get("choices") or [{}]
        logprobs = choices[0].get("logprobs") if isinstance(choices[0], dict) else None
        content = logprobs.get("content") if isinstance(logprobs, dict) else None
        ok = isinstance(content, list) and len(content) > 0
        self.matrix.logprobs = self.judged(
            check, ok, "logprobs devolvidos" if ok else "aceitou, mas não devolveu logprobs"
        )

    async def run(self) -> CapabilityMatrix:
        await self.models()
        await self.baseline()
        await self.unknown_fields()
        self.matrix.thinking_off_template_kwargs = await self.thinking(
            {"chat_template_kwargs": {"enable_thinking": False}}, 31, 27, "enable_thinking"
        )
        self.matrix.thinking_off_reasoning_effort = await self.thinking(
            {"reasoning_effort": "low"}, 44, 19, "reasoning_effort"
        )
        await self.json_schema()
        await self.tools()
        await self.image()
        await self.large_max_tokens()
        await self.logprobs()
        return self.matrix


async def probe(client: HubClient) -> CapabilityMatrix:
    """Mede as capacidades do apelido ``client.model`` no hub.

    O cliente não deve ter matriz anexada (a sonda decide sozinha os campos de
    cada requisição) e convém usar poucas novas tentativas.
    """
    return await _Prober(client).run()


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    CheckStatus.YES: "sim",
    CheckStatus.NO: "não",
    CheckStatus.ERROR: "erro",
    CheckStatus.SKIPPED: "não verificado",
}


def render_summary(matrix: CapabilityMatrix) -> str:
    """Resumo legível da matriz, em pt-BR."""
    lines = [
        f"Modelo: {matrix.alias} (resposta identifica: {matrix.fingerprint or 'desconhecido'})",
        f"Raciocínio na resposta: {matrix.reasoning_shape.value}",
    ]
    rows: list[tuple[str, Check]] = [
        (capability_label(cap), matrix.check_for(cap)) for cap in Capability
    ]
    rows.insert(2, ("recusa campos desconhecidos", matrix.rejects_unknown_fields))
    rows.insert(4, ("  via chat_template_kwargs", matrix.thinking_off_template_kwargs))
    rows.insert(5, ("  via reasoning_effort", matrix.thinking_off_reasoning_effort))
    for label, check in rows:
        detail = f" — {check.detail}" if check.detail else ""
        lines.append(f"- {label}: {_STATUS_LABELS[check.status]}{detail}")
    if matrix.latency_ms is not None:
        speed = f", {matrix.tokens_per_second} tokens/s" if matrix.tokens_per_second else ""
        lines.append(f"Latência da linha de base: {matrix.latency_ms} ms{speed}")
    lines.append(f"Teto de max_tokens: {matrix.max_tokens_ceiling}")
    return "\n".join(lines)


async def _run(
    model: str | None, timeout_s: float, transport: httpx2.AsyncBaseTransport | None
) -> CapabilityMatrix:
    async with HubClient(
        model=model, timeout_s=timeout_s, max_retries=1, transport=transport
    ) as client:
        return await probe(client)


def main(
    argv: Sequence[str] | None = None, *, transport: httpx2.AsyncBaseTransport | None = None
) -> int:
    """Ponto de entrada da linha de comando. Devolve o código de saída."""
    parser = argparse.ArgumentParser(
        prog="python -m rapa.llm.probe",
        description=(
            "Sonda as capacidades do hub de LLM para um apelido de modelo e grava a matriz. "
            "O endereço e a chave vêm de RAPA_LLM_BASE_URL e RAPA_LLM_API_KEY."
        ),
    )
    parser.add_argument("--model", help="apelido do modelo (padrão: RAPA_LLM_MODEL)")
    parser.add_argument("--out", required=True, help="arquivo JSON onde gravar a matriz")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        choices=[cap.value for cap in Capability],
        help="capacidade exigida; falha se faltar (pode repetir)",
    )
    parser.add_argument(
        "--compare", help="matriz anterior; falha se o modelo ou alguma capacidade mudou"
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="segundos por requisição")
    args = parser.parse_args(argv)

    try:
        matrix = asyncio.run(_run(args.model, args.timeout, transport))
    except HubConfigError as exc:
        print(f"erro: {exc.message}", file=sys.stderr)
        return 2
    matrix.save(args.out)
    print(render_summary(matrix))
    print(f"Matriz gravada em {args.out}")

    status = 0
    if args.compare:
        changes = matrix_changes(CapabilityMatrix.load(args.compare), matrix)
        if changes:
            print("Mudanças desde a sonda anterior:", file=sys.stderr)
            for change in changes:
                print(f"- {change}", file=sys.stderr)
            status = 1
    if args.require:
        try:
            ensure_capabilities(matrix, args.require, model=matrix.alias)
        except CapabilityMissing as exc:
            print(f"erro: {exc.message}", file=sys.stderr)
            status = 1
    if matrix.baseline.status is not CheckStatus.YES:
        print("erro: a linha de base falhou; o modelo não pode ser usado", file=sys.stderr)
        status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
