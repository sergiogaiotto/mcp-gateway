"""Forma única dos erros da API e o mapeamento das exceções de negócio.

Todo erro sai como JSON ``{"erro": <código>, "mensagem": <pt-BR>, "detalhes": [...]}``.
Nunca há rastro de pilha na resposta.
"""

import logging
import math
import traceback
from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from rapa.control.auth import AuthError
from rapa.core.contracts import (
    Conflict,
    ContractError,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    ServiceUnavailable,
    SignatureInvalid,
)

__all__ = [
    "ErrorBody",
    "auth_error_response",
    "contract_error_response",
    "error_response",
    "install_error_handlers",
    "log_unexpected",
    "status_for",
    "translate_validation_errors",
]

logger = logging.getLogger(__name__)

_STATUS: tuple[tuple[type[ContractError], int], ...] = (
    (NotFound, 404),
    (Forbidden, 403),
    (Conflict, 409),
    (InvalidInput, 422),
    (QuotaExceeded, 429),
    (ServiceUnavailable, 503),
    (SignatureInvalid, 401),
)

_HTTP_CODES = {
    400: ("requisicao_invalida", "requisição inválida"),
    401: ("nao_autenticado", "é preciso se autenticar"),
    403: ("proibido", "acesso negado"),
    404: ("nao_encontrado", "recurso não encontrado"),
    405: ("metodo_nao_permitido", "método não permitido neste endereço"),
    406: ("formato_nao_aceito", "formato de resposta não aceito"),
    413: ("corpo_muito_grande", "o corpo da requisição é grande demais"),
    415: ("tipo_nao_suportado", "tipo de conteúdo não suportado"),
}


class ErrorBody(BaseModel):
    """Corpo de erro de toda resposta 4xx e 5xx."""

    model_config = ConfigDict(extra="forbid")

    erro: str
    mensagem: str
    detalhes: list[str] = Field(default_factory=list)


def log_unexpected(log: logging.Logger, what: str, exc: BaseException) -> None:
    """Registra um erro inesperado com o tipo e a pilha, mas sem a mensagem.

    A mensagem de uma exceção de outra camada pode trazer dados da requisição ou
    da conexão; o tipo e os quadros da pilha (arquivo, linha e função, sem o
    código-fonte) bastam para investigar.
    """
    frames = "\n".join(
        f"  {frame.filename}:{frame.lineno} em {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )
    log.error("%s: %s (mensagem omitida)\n%s", what, type(exc).__name__, frames)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Sequence[str] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorBody(erro=code, mensagem=message, detalhes=list(details))
    return JSONResponse(body.model_dump(), status_code=status_code, headers=dict(headers or {}))


def status_for(exc: ContractError) -> int:
    for cls, status in _STATUS:
        if isinstance(exc, cls):
            return status
    return 400


def contract_error_response(exc: ContractError) -> JSONResponse:
    details: list[str] = []
    headers: dict[str, str] = {}
    if isinstance(exc, InvalidInput):
        details = list(exc.errors)
    if isinstance(exc, QuotaExceeded):
        headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_s)))
    if isinstance(exc, ServiceUnavailable):
        headers["Retry-After"] = "5"
    return error_response(status_for(exc), exc.code, exc.message, details, headers)


def www_authenticate(exc: AuthError, resource_metadata_url: str | None = None) -> str:
    parts = ['realm="rapa"']
    if exc.bearer_error:
        parts.append(f'error="{exc.bearer_error}"')
    if resource_metadata_url:
        parts.append(f'resource_metadata="{resource_metadata_url}"')
    return "Bearer " + ", ".join(parts)


def auth_error_response(exc: AuthError, resource_metadata_url: str | None = None) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = www_authenticate(exc, resource_metadata_url)
    elif exc.status_code == 503:
        headers["Retry-After"] = "5"
    return error_response(exc.status_code, exc.code, exc.message, headers=headers)


_PYDANTIC_MESSAGES = {
    "missing": "campo obrigatório ausente",
    "extra_forbidden": "campo não permitido",
    "string_type": "esperado texto",
    "int_type": "esperado número inteiro",
    "int_parsing": "esperado número inteiro",
    "float_type": "esperado número",
    "bool_type": "esperado verdadeiro ou falso",
    "dict_type": "esperado objeto",
    "list_type": "esperada lista",
    "uuid_type": "esperado identificador UUID",
    "uuid_parsing": "identificador UUID inválido",
    "string_too_short": "texto curto demais",
    "string_too_long": "texto longo demais",
    "string_pattern_mismatch": "formato inválido",
    "too_short": "itens de menos",
    "too_long": "itens demais",
    "greater_than_equal": "valor abaixo do mínimo",
    "less_than_equal": "valor acima do máximo",
    "literal_error": "valor fora das opções permitidas",
    "enum": "valor fora das opções permitidas",
    "union_tag_invalid": "tipo de passo ou valor desconhecido",
    "union_tag_not_found": "falta o campo que identifica o tipo",
    "json_invalid": "JSON inválido",
    "model_attributes_type": "esperado objeto",
}


def _location(loc: Sequence[Any]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "corpo"


def translate_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """Erros do pydantic em pt-BR, sem repetir o valor recebido."""
    out: list[str] = []
    for error in errors[:50]:
        loc = list(error.get("loc", ()))
        if loc and loc[0] in {"body", "query", "path", "header"}:
            loc = loc[1:]
        kind = str(error.get("type", ""))
        if kind == "value_error":
            ctx = error.get("ctx") or {}
            cause = ctx.get("error")
            message = str(cause) if cause is not None else "valor inválido"
        else:
            message = _PYDANTIC_MESSAGES.get(kind, "valor inválido")
        out.append(f"{_location(loc)}: {message}")
    return out


def install_error_handlers(app: FastAPI, *, resource_metadata_url: str | None = None) -> None:
    """Liga o mapeamento de exceções → respostas JSON de erro."""

    async def on_contract_error(_: Request, exc: Exception) -> JSONResponse:
        return contract_error_response(cast(ContractError, exc))

    async def on_auth_error(_: Request, exc: Exception) -> JSONResponse:
        return auth_error_response(cast(AuthError, exc), resource_metadata_url)

    async def on_validation_error(_: Request, exc: Exception) -> JSONResponse:
        errors = cast(RequestValidationError, exc).errors()
        return error_response(
            422,
            InvalidInput.code,
            "requisição inválida",
            translate_validation_errors(list(errors)),
        )

    async def on_http_error(_: Request, exc: Exception) -> JSONResponse:
        exc = cast(StarletteHTTPException, exc)
        code, message = _HTTP_CODES.get(exc.status_code, ("erro_http", "erro na requisição"))
        return error_response(exc.status_code, code, message, headers=exc.headers)

    async def on_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log_unexpected(logger, "erro inesperado na API", exc)
        return error_response(500, "erro_interno", "erro interno; tente de novo mais tarde")

    app.add_exception_handler(ContractError, on_contract_error)
    app.add_exception_handler(AuthError, on_auth_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(Exception, on_unexpected)
