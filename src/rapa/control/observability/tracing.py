"""Traços OpenTelemetry, só com ``OTEL_EXPORTER_OTLP_ENDPOINT``.

Sem o endpoint, nada daqui carrega o SDK, o exportador ou as instrumentações: este
módulo só importa o OpenTelemetry dentro de :func:`activate_tracing`. Com ele, o
processo ganha:

- os traços do FastAPI (``opentelemetry-instrumentation-fastapi``, por fora de tudo,
  com a rota modelo no nome do span; a saúde fica de fora) e dos clientes ``httpx2``
  (``opentelemetry-instrumentation-httpx``);
- um span por execução no despachante (:class:`RunSpans`), corrente enquanto ele cuida
  dela: os logs do lançador saem com o id de rastreio;
- o exportador OTLP por HTTP, com o nome do serviço de ``OTEL_SERVICE_NAME`` (padrão
  ``rapa``) e as demais variáveis padrão do OpenTelemetry.

O provedor não vira o global do processo: só o que o RaPA liga usa o rastreio (a
telemetria embutida do FastAPI, que se liga sozinha com um provedor global, continua
desligada).

**Nada de dados nos atributos.** Todo span passa por :class:`ScrubbingSpanExporter`
antes de sair: só ficam os atributos de uma lista fechada (método, rota modelo, código,
esquema, servidor e porta, versão do protocolo, tipo do erro) e os ``rapa.*`` que o
próprio RaPA põe (ids, tentativa, situação e código de erro). Saem sempre: caminho cru
e consulta, URL completa, cabeçalhos (inclusive se alguém ligar a captura deles pelas
variáveis do OpenTelemetry), endereço e agente do cliente, corpo, entradas e saídas.
Dos eventos, só o tipo da exceção fica (nunca a mensagem nem a pilha); a descrição do
status também sai.
"""

import asyncio
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rapa.core.contracts import RunnerJob, RunState

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.trace import Span, Tracer

__all__ = [
    "DEFAULT_SERVICE_NAME",
    "SAFE_ATTRIBUTES",
    "RunSpans",
    "ScrubbingSpanExporter",
    "Tracing",
    "activate_tracing",
    "current_trace_id",
    "scrub_span",
]

#: Nome do serviço quando ``OTEL_SERVICE_NAME`` não diz outro.
DEFAULT_SERVICE_NAME = "rapa"
#: Os únicos atributos (além dos ``rapa.*``) que um span leva para fora do processo.
SAFE_ATTRIBUTES = frozenset(
    {
        # Convenções estáveis do HTTP.
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "url.scheme",
        "server.address",
        "server.port",
        "network.protocol.version",
        "error.type",
        # Convenções antigas (o padrão das instrumentações sem OTEL_SEMCONV_STABILITY_OPT_IN).
        "http.method",
        "http.status_code",
        "http.scheme",
        "http.flavor",
        "net.host.port",
        "net.peer.name",
        "net.peer.port",
    }
)
_RAPA_PREFIX = "rapa."
#: A saúde fica de fora dos traços (as sondas batem a cada poucos segundos).
_EXCLUDED_URLS = r"/saude$,/saude/pronto$"
_STATES = frozenset(state.value for state in RunState)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: Há um provedor ligado neste processo (para o id de rastreio dos logs).
_active = 0
#: O rastreio do processo (:func:`activate_tracing`), um só.
_process: Tracing | None = None


def current_trace_id() -> str | None:
    """O id de rastreio do span corrente (32 dígitos hexadecimais), ou ``None``.

    Com o rastreio desligado, não importa nada do OpenTelemetry.
    """
    if not _active:
        return None
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def _safe(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (attributes or {}).items()
        if key in SAFE_ATTRIBUTES or key.startswith(_RAPA_PREFIX)
    }


def scrub_span(span: ReadableSpan) -> ReadableSpan:
    """Uma cópia do span só com o que pode sair (ver o começo do módulo)."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Link
    from opentelemetry.trace.status import Status

    events = []
    for event in span.events:
        if event.name != "exception":
            continue
        kind = (event.attributes or {}).get("exception.type")
        events.append(
            Event(
                "exception",
                {"exception.type": kind} if isinstance(kind, str) else {},
                timestamp=event.timestamp,
            )
        )
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_safe(span.attributes),
        events=events,
        links=[Link(link.context) for link in span.links],
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter:
    """Exportador que limpa cada span (:func:`scrub_span`) antes de passar ao de verdade."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([scrub_span(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return bool(self.inner.force_flush(timeout_millis))


class _RunSpan:
    def __init__(self, span: Span) -> None:
        self.span = span

    def finished(self, result: Mapping[str, Any] | None) -> None:
        if result is None:
            self.span.set_attribute("rapa.execucao.tentativa_antiga", True)
            return
        state = result.get("state")
        if isinstance(state, str) and state in _STATES:
            self.span.set_attribute("rapa.execucao.situacao", state)
        code = result.get("error_code")
        if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
            self.span.set_attribute("rapa.execucao.erro", code)


class RunSpans:
    """Um span por execução no despachante (o :class:`~rapa.control.services.dispatcher.RunTracer`)."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    @contextmanager
    def run(self, job: RunnerJob) -> Iterator[_RunSpan]:
        from opentelemetry.trace import Status, StatusCode

        attributes: dict[str, str | int] = {
            "rapa.execucao.id": str(job.run_id),
            "rapa.execucao.tentativa": job.attempt,
            "rapa.execucao.modo": job.mode.value,
            "rapa.empresa.id": str(job.company_id),
        }
        with self.tracer.start_as_current_span(
            "execucao",
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield _RunSpan(span)
            except BaseException as exc:
                # Só o tipo: a mensagem de um erro inesperado pode trazer dados.
                span.set_attribute("error.type", type(exc).__name__)
                if not isinstance(exc, asyncio.CancelledError):
                    span.set_status(Status(StatusCode.ERROR))
                raise


class Tracing:
    """O rastreio de um processo: o provedor, a instrumentação do ``httpx2`` e os spans."""

    def __init__(self, provider: TracerProvider) -> None:
        self.provider = provider
        self._httpx: Any = None

    def instrument_app(self, app: FastAPI) -> None:
        """Os traços dos pedidos de ``app`` (a saúde fica de fora)."""
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.provider,
            excluded_urls=_EXCLUDED_URLS,
            exclude_spans=["receive", "send"],
        )

    def instrument_httpx(self) -> None:
        """Os traços dos clientes ``httpx2`` do processo (uma vez só)."""
        if self._httpx is not None:
            return
        from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor

        instrumentor = HTTPX2ClientInstrumentor()
        instrumentor.instrument(tracer_provider=self.provider)
        self._httpx = instrumentor

    def run_spans(self) -> RunSpans:
        return RunSpans(self.provider.get_tracer("rapa.despachante"))

    def flush(self, timeout_s: float = 5.0) -> None:
        self.provider.force_flush(int(timeout_s * 1000))

    def shutdown(self) -> None:
        """Manda o que falta e desliga (o fim do processo)."""
        global _active, _process
        if self._httpx is not None:
            self._httpx.uninstrument()
            self._httpx = None
        self.provider.shutdown()
        _active = max(0, _active - 1)
        if _process is self:
            _process = None


def activate_tracing(
    *,
    endpoint: str,
    service_name: str | None = None,
    version: str = "0",
    exporter: SpanExporter | None = None,
    traces_endpoint: str | None = None,
) -> Tracing:
    """Liga o rastreio do processo (uma vez; as chamadas seguintes devolvem o mesmo).

    ``endpoint`` é o ``OTEL_EXPORTER_OTLP_ENDPOINT`` (a base: os traços vão para
    ``<endpoint>/v1/traces``, ou para ``traces_endpoint``, o
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``). ``exporter`` troca o exportador OTLP (os
    testes usam um em memória). Levanta ``RuntimeError`` se o extra ``observability``
    não estiver instalado.
    """
    global _active, _process
    if _process is not None:
        return _process
    try:
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - depende da instalação
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_ENDPOINT está definido, mas o OpenTelemetry não está "
            "instalado (instale o extra observability)"
        ) from exc
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=traces_endpoint or endpoint.rstrip("/") + "/v1/traces")
    resource = Resource.create(
        {SERVICE_NAME: service_name or DEFAULT_SERVICE_NAME, SERVICE_VERSION: version}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))  # type: ignore[arg-type]
    tracing = Tracing(provider)
    tracing.instrument_httpx()
    _process = tracing
    _active += 1
    return tracing
