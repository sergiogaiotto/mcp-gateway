"""Logs do serviço: texto (o de sempre) ou JSON, um objeto por linha (``RAPA_LOG_JSON=1``).

O JSON tem a hora (UTC, ISO 8601 com milissegundos), o nível, o logger, a mensagem e,
quando há um span do OpenTelemetry corrente, o id de rastreio (``rastreio``). A mensagem
é exatamente a do texto (``record.getMessage()``): nenhuma mensagem que exista muda, e
uma mensagem de várias linhas continua uma linha só de JSON. A pilha de uma exceção
registrada com ``exc_info`` sai em ``excecao``, como o texto a mostraria.

O uvicorn configura os próprios loggers com o formato dele; no modo JSON ele não
configura nada (:func:`uvicorn_log_config` devolve ``None``) e as linhas dele também
saem em JSON, pelo logger raiz.
"""

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

__all__ = [
    "QUIET_LOGGERS",
    "TEXT_FORMAT",
    "JsonFormatter",
    "configure_logging",
    "uvicorn_log_config",
]

#: O formato do texto, o mesmo de antes do JSON.
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _no_trace() -> str | None:
    return None


class JsonFormatter(logging.Formatter):
    """Um objeto JSON por linha: ``hora``, ``nivel``, ``logger``, ``mensagem`` e ``rastreio``.

    ``trace_id`` devolve o id de rastreio corrente (32 dígitos hexadecimais) ou ``None``;
    o padrão é :func:`rapa.control.observability.tracing.current_trace_id`, que não
    carrega nada do OpenTelemetry quando o rastreio está desligado.
    """

    def __init__(self, trace_id: Callable[[], str | None] | None = None) -> None:
        super().__init__()
        if trace_id is None:
            from rapa.control.observability.tracing import current_trace_id

            trace_id = current_trace_id
        self._trace_id = trace_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "hora": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": record.getMessage(),
        }
        if record.exc_info:
            payload["excecao"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["pilha"] = self.formatStack(record.stack_info)
        try:
            trace = self._trace_id()
        except Exception:  # o rastreio nunca impede o log
            trace = None
        if trace:
            payload["rastreio"] = trace
        return json.dumps(payload, ensure_ascii=False, default=str)


#: Loggers de bibliotecas que só falam a partir de WARNING: o ``httpx2`` registra cada pedido
#: em INFO com a URL inteira (o UUID da empresa e o caminho no cofre, no OpenBao; a execução
#: e o nome dos arquivos, no canal; a sondagem de cada pod a cada 2 s, na API do Kubernetes).
QUIET_LOGGERS = ("httpx2",)


def configure_logging(
    *, json_logs: bool, level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    """Configura o logger raiz (como o ``logging.basicConfig`` de antes) em texto ou JSON."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(TEXT_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


def uvicorn_log_config(*, json_logs: bool) -> dict[str, Any] | None:
    """A configuração de log do uvicorn: a dele no texto; nenhuma no JSON (vai pelo raiz)."""
    if json_logs:
        return None
    from uvicorn.config import LOGGING_CONFIG

    return LOGGING_CONFIG
