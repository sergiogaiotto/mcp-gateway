"""Logs em JSON: um objeto por linha, com as mensagens de sempre e o id de rastreio."""

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from rapa.control.observability.logs import (
    TEXT_FORMAT,
    JsonFormatter,
    configure_logging,
    uvicorn_log_config,
)


def _record(message: str, *args: object, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(
        "rapa.control.dispatcher", logging.WARNING, __file__, 10, message, args, exc_info
    )


def test_json_line_has_time_level_logger_and_the_unchanged_message() -> None:
    formatter = JsonFormatter(trace_id=lambda: None)
    message = "concessões vencidas: %d de volta à fila, %d indeterminadas"
    line = formatter.format(_record(message, 2, 0))
    assert "\n" not in line
    payload = json.loads(line)
    assert set(payload) == {"hora", "nivel", "logger", "mensagem"}
    assert payload["nivel"] == "WARNING"
    assert payload["logger"] == "rapa.control.dispatcher"
    # A mensagem é exatamente a do texto: nada muda nas mensagens que já existem.
    assert payload["mensagem"] == logging.Formatter("%(message)s").format(_record(message, 2, 0))
    assert payload["mensagem"] == "concessões vencidas: 2 de volta à fila, 0 indeterminadas"
    assert payload["hora"].endswith("+00:00") and "T" in payload["hora"]


def test_multiline_messages_stay_one_json_line() -> None:
    message = (
        "erro inesperado na API: RuntimeError (mensagem omitida)\n  a.py:1 em f\n  b.py:2 em g"
    )
    line = JsonFormatter(trace_id=lambda: None).format(_record(message))
    assert line.count("\n") == 0
    assert json.loads(line)["mensagem"] == message


def test_trace_id_goes_along_when_there_is_one() -> None:
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    payload = json.loads(JsonFormatter(trace_id=lambda: trace).format(_record("oi")))
    assert payload["rastreio"] == trace

    def broken() -> str | None:
        raise RuntimeError("o rastreio falhou")

    # Um rastreio quebrado nunca impede o log.
    assert "rastreio" not in json.loads(JsonFormatter(trace_id=broken).format(_record("oi")))


def test_default_formatter_has_no_trace_without_tracing() -> None:
    # Sem o rastreio ligado, o padrão não acha id nenhum (e não carrega o OpenTelemetry).
    assert "rastreio" not in json.loads(JsonFormatter().format(_record("oi")))


def test_exception_info_goes_to_its_own_field() -> None:
    try:
        raise ValueError("problema")
    except ValueError as exc:
        error = exc
    payload = json.loads(JsonFormatter(trace_id=lambda: None).format(_record("falhou", exc=error)))
    assert payload["mensagem"] == "falhou"
    assert "Traceback" in payload["excecao"] and "ValueError" in payload["excecao"]


@contextmanager
def _bare_root() -> Iterator[logging.Logger]:
    """O logger raiz sem nenhum handler (como no começo de ``python -m rapa.server``)."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = handlers
        root.setLevel(level)


def test_configure_logging_writes_json_or_the_old_text() -> None:
    stream = io.StringIO()
    with _bare_root():
        configure_logging(json_logs=True, stream=stream)
        logging.getLogger("rapa.server").info(
            "token de desenvolvimento sem usuário ativo; ignorado"
        )
    payload = json.loads(stream.getvalue().strip())
    assert payload["mensagem"] == "token de desenvolvimento sem usuário ativo; ignorado"
    assert payload["logger"] == "rapa.server" and payload["nivel"] == "INFO"

    stream = io.StringIO()
    with _bare_root() as root:
        configure_logging(json_logs=False, stream=stream)
        formatter = root.handlers[0].formatter
        logging.getLogger("rapa.server").info("uma linha")
    assert formatter is not None and formatter._fmt == TEXT_FORMAT
    assert stream.getvalue().strip().endswith("INFO rapa.server: uma linha")


def test_uvicorn_logs_go_through_the_root_logger_only_in_json() -> None:
    from uvicorn.config import LOGGING_CONFIG

    assert uvicorn_log_config(json_logs=True) is None
    assert uvicorn_log_config(json_logs=False) is LOGGING_CONFIG
