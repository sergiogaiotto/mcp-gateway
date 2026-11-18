"""Respostas em JSON validadas com pydantic.

Quando a matriz de capacidades comprova que o hub impõe ``response_format``
``json_schema``, o esquema vai na requisição; senão, vai só nas instruções e o
primeiro objeto JSON da resposta é extraído. Em qualquer caso a resposta é
validada pelo modelo pydantic e, se falhar, o modelo de linguagem recebe o erro
e responde de novo. O código nunca conserta o JSON por conta própria.
"""

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from rapa.llm.capabilities import CapabilityMatrix
from rapa.llm.client import HubClient, Thinking
from rapa.llm.errors import StructuredOutputError
from rapa.llm.messages import Message
from rapa.llm.sanitize import sanitize

__all__ = ["ask_json", "extract_json_object", "summarize_validation_error"]

logger = logging.getLogger("rapa.llm")

_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\n?(.*?)```", re.DOTALL)
_MAX_OBJECT_STARTS = 200
_MAX_ERRORS = 12


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Primeiro objeto JSON do texto (dentro de um bloco de código, se houver)."""
    candidates = [match.group(1) for match in _FENCE.finditer(text)]
    candidates.append(text)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        starts = 0
        for match in re.finditer(r"\{", candidate):
            starts += 1
            if starts > _MAX_OBJECT_STARTS:
                break
            try:
                value, _ = decoder.raw_decode(candidate, match.start())
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def summarize_validation_error(exc: ValidationError) -> list[str]:
    """Erros de validação sem os valores recebidos (só local e motivo)."""
    summary: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]) or "(raiz)"
        summary.append(f"{location}: {error['msg']}")
        if len(summary) >= _MAX_ERRORS:
            summary.append("(há mais erros)")
            break
    return summary


def _instructions(schema: dict[str, Any], enforced: bool) -> str:
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    if enforced:
        return f"Responda apenas com um objeto JSON que siga este JSON Schema:\n{schema_text}"
    return (
        "Responda apenas com um único objeto JSON, sem nenhum texto antes ou depois e sem "
        f"blocos de código, seguindo exatamente este JSON Schema:\n{schema_text}"
    )


def _fix_request(errors: list[str]) -> str:
    lines = "\n".join(f"- {error}" for error in errors)
    return (
        "A resposta anterior não passou na validação:\n"
        f"{lines}\n"
        "Responda de novo apenas com o objeto JSON completo e corrigido, seguindo o esquema."
    )


async def ask_json[M: BaseModel](
    client: HubClient,
    *,
    system: str,
    user: str,
    model_cls: type[M],
    capabilities: CapabilityMatrix | None,
    retries: int = 2,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    thinking: Thinking = "off",
) -> M:
    """Pede ao modelo um objeto JSON válido para ``model_cls``.

    ``retries`` é quantas vezes o modelo pode corrigir a resposta depois de
    receber o erro de validação. Levanta ``StructuredOutputError`` se nenhuma
    tentativa passar.
    """
    if retries < 0:
        raise ValueError("retries não pode ser negativo")
    schema = model_cls.model_json_schema()
    enforced = capabilities is not None and capabilities.json_schema_enforced.ok
    messages: list[Message] = [
        {"role": "system", "content": f"{system.rstrip()}\n\n{_instructions(schema, enforced)}"},
        {"role": "user", "content": user},
    ]
    errors: list[str] = []
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        result = await client.chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            response_schema=schema if enforced else None,
            capabilities=capabilities,
        )
        candidate = extract_json_object(result.content)
        if candidate is None:
            errors = ["a resposta não contém um objeto JSON válido"]
        else:
            try:
                return model_cls.model_validate(candidate)
            except ValidationError as exc:
                errors = summarize_validation_error(exc)
        logger.info(
            "saída estruturada inválida para %s (tentativa %d de %d, %d erro(s))",
            model_cls.__name__,
            attempt,
            attempts,
            len(errors),
        )
        if attempt < attempts:
            previous = sanitize(result.content, budget=4000) or "(resposta vazia)"
            messages = [
                *messages,
                {"role": "assistant", "content": previous},
                {"role": "user", "content": _fix_request(errors)},
            ]
    raise StructuredOutputError(
        f"o modelo não devolveu um JSON válido para {model_cls.__name__} "
        f"depois de {attempts} tentativa{'s' if attempts > 1 else ''}",
        errors,
    )
