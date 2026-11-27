"""Erros inesperados no GlitchTip (``sentry-sdk``), só com ``RAPA_GLITCHTIP_DSN``.

Sem o DSN, nada daqui carrega o ``sentry_sdk``. Com ele, só vão as exceções que
nenhum código tratou: as que chegam ao último recurso da API, do MCP e da aplicação
HTTP (:func:`rapa.control.api.errors.log_unexpected`), as que derrubam o despacho de uma
execução ou o próprio despachante, e as que derrubam o processo (o ``sys.excepthook``).
Nenhum log, nenhum rastro (``breadcrumb``), nenhum traço de desempenho, nenhuma sessão.

A configuração já não manda nada de pessoal (``send_default_pii=False``), nem as
variáveis locais da pilha (``include_local_variables=False``), nem o corpo de um pedido
(``max_request_body_size="never"``), e nenhuma integração automática é ligada (elas
juntariam o pedido, os cabeçalhos e os logs); a coleta de dados do ``sentry_sdk``
(``data_collection``) também fica toda desligada. Mesmo assim, o :func:`scrub_event` de
``before_send`` só deixa sair uma lista fechada: o tipo e o módulo de cada exceção, a
pilha (arquivo, linha e função, como no log de :func:`rapa.control.api.errors.log_unexpected`:
nem o código-fonte em volta nem as variáveis), o nível, a versão, o ambiente e o nome
do servidor. Saem sempre: o pedido, os cabeçalhos, os cookies, o usuário, os extras, as
etiquetas, os rastros, os módulos instalados e a mensagem da exceção (que pode trazer
dados: no lugar dela vai ``(mensagem omitida)``).
"""

import re
import warnings
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "OMITTED_MESSAGE",
    "GlitchTip",
    "configure_glitchtip",
    "dsn_problem",
    "report_unhandled",
    "scrub_event",
]

#: O que vai no lugar da mensagem de cada exceção.
OMITTED_MESSAGE = "(mensagem omitida)"
#: As únicas chaves do evento que saem do processo (o resto é descartado).
_EVENT_KEYS = (
    "event_id",
    "timestamp",
    "level",
    "platform",
    "logger",
    "release",
    "environment",
    "server_name",
    "sdk",
    "fingerprint",
)
_FRAME_KEYS = (
    "filename",
    "abs_path",
    "module",
    "function",
    "lineno",
    "colno",
    "in_app",
)
_CONTEXTS = ("runtime", "os")
_TRACE_KEYS = ("trace_id", "span_id", "parent_span_id")
#: O número do projeto, no fim do caminho do DSN.
_PROJECT = re.compile(r"[0-9]{1,20}")

#: O GlitchTip está ligado neste processo.
_enabled = False


def dsn_problem(dsn: str) -> str | None:
    """O que falta no DSN (``https://<chave>@<servidor>/<projeto>``), ou ``None``.

    A mesma leitura que o ``sentry_sdk`` faz ao ligar, sem carregá-lo, para a
    configuração recusar o DSN na partida. A resposta nunca traz o valor (ele tem a
    chave do projeto).
    """
    try:
        parts = urlsplit(dsn)
        parts.port  # noqa: B018 - levanta ValueError se a porta não for um número válido
    except ValueError:
        return "não é um endereço válido"
    if parts.scheme not in ("http", "https"):
        return "precisa ser um endereço http:// ou https://"
    if not parts.hostname:
        return "precisa do servidor"
    if not parts.username:
        return "precisa da chave do projeto (https://<chave>@<servidor>/<projeto>)"
    if not _PROJECT.fullmatch(parts.path.rsplit("/", 1)[-1]):
        return "precisa terminar no número do projeto (https://<chave>@<servidor>/<projeto>)"
    return None


def _frame(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {key: raw[key] for key in _FRAME_KEYS if key in raw}


def _exception(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {"value": OMITTED_MESSAGE}
    for key in ("type", "module"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    mechanism = raw.get("mechanism")
    if isinstance(mechanism, Mapping):
        out["mechanism"] = {key: mechanism[key] for key in ("type", "handled") if key in mechanism}
    stacktrace = raw.get("stacktrace")
    if isinstance(stacktrace, Mapping) and isinstance(stacktrace.get("frames"), list):
        frames = [frame for frame in map(_frame, stacktrace["frames"]) if frame is not None]
        out["stacktrace"] = {"frames": frames}
    return out


def scrub_event(event: Mapping[str, Any], hint: Any = None) -> dict[str, Any] | None:
    """O ``before_send``: uma cópia do evento só com a lista fechada (ver o começo)."""
    out: dict[str, Any] = {key: event[key] for key in _EVENT_KEYS if key in event}
    exception = event.get("exception")
    if isinstance(exception, Mapping) and isinstance(exception.get("values"), list):
        values = [value for value in map(_exception, exception["values"]) if value is not None]
        out["exception"] = {"values": values}
    contexts = event.get("contexts")
    if isinstance(contexts, Mapping):
        kept: dict[str, Any] = {
            name: dict(contexts[name])
            for name in _CONTEXTS
            if isinstance(contexts.get(name), Mapping)
        }
        trace = contexts.get("trace")
        if isinstance(trace, Mapping):
            kept["trace"] = {key: trace[key] for key in _TRACE_KEYS if key in trace}
        out["contexts"] = kept
    return out


def _drop_breadcrumb(crumb: Any, hint: Any) -> None:
    return None


def _quiet_exit(pending: int, timeout: float) -> None:
    """Sem a mensagem do ``sentry_sdk`` no fim do processo (o log é do serviço)."""


class GlitchTip:
    """O cliente do GlitchTip ligado no processo."""

    def close(self, timeout_s: float = 2.0) -> None:
        """Manda o que falta e desliga."""
        global _enabled
        import sentry_sdk

        _enabled = False
        sentry_sdk.get_client().close(timeout=timeout_s)


def configure_glitchtip(
    dsn: str, *, release: str | None = None, transport: Any = None
) -> GlitchTip:
    """Liga o envio das exceções não tratadas para o GlitchTip de ``dsn``.

    ``transport`` troca o envio (os testes guardam os envelopes em memória). Levanta
    ``RuntimeError`` se o ``sentry-sdk`` (extra ``observability``) não estiver instalado
    ou se o DSN for recusado (a mensagem nunca traz o DSN).
    """
    global _enabled
    if (problem := dsn_problem(dsn)) is not None:
        raise RuntimeError(f"RAPA_GLITCHTIP_DSN {problem}")
    try:
        import sentry_sdk
        from sentry_sdk.integrations.atexit import AtexitIntegration
        from sentry_sdk.integrations.dedupe import DedupeIntegration
        from sentry_sdk.integrations.excepthook import ExcepthookIntegration
        from sentry_sdk.utils import BadDsn
    except ImportError as exc:  # pragma: no cover - depende da instalação
        raise RuntimeError(
            "RAPA_GLITCHTIP_DSN está definido, mas o sentry-sdk não está instalado "
            "(instale o extra observability)"
        ) from exc
    options: dict[str, Any] = {
        "dsn": dsn,
        "release": release,
        # Só as integrações escolhidas: as automáticas juntariam pedido, cabeçalhos e logs.
        "default_integrations": False,
        "auto_enabling_integrations": False,
        "integrations": [
            ExcepthookIntegration(),
            DedupeIntegration(),
            AtexitIntegration(callback=_quiet_exit),
        ],
        "send_default_pii": False,
        "include_local_variables": False,
        "include_source_context": False,
        "max_request_body_size": "never",
        # A forma nova das mesmas escolhas (vale por cima das antigas): nada é coletado.
        "data_collection": {
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_headers": {"request": {"mode": "off"}},
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": False,
            "frame_context_lines": 0,
        },
        "max_breadcrumbs": 0,
        "before_send": scrub_event,
        "before_breadcrumb": _drop_breadcrumb,
        "traces_sample_rate": None,
        "auto_session_tracking": False,
        "enable_metrics": False,
        "enable_logs": False,
        "send_client_reports": False,
        "propagate_traces": False,
    }
    if transport is not None:
        options["transport"] = transport
    with warnings.catch_warnings():
        # O sentry_sdk avisa que send_default_pii virou data_collection; os dois vão
        # (o primeiro é o que a operação confere), e o novo vale.
        warnings.filterwarnings("ignore", r".*send_default_pii", DeprecationWarning)
        try:
            sentry_sdk.init(**options)
        except BadDsn:  # sem a mensagem do sentry_sdk, que pode citar o DSN
            raise RuntimeError("RAPA_GLITCHTIP_DSN não é um DSN válido") from None
    _enabled = True
    return GlitchTip()


def report_unhandled(exc: BaseException) -> None:
    """Manda ``exc`` para o GlitchTip, se ele estiver ligado; nunca levanta."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # o envio nunca derruba quem chamou
        return
