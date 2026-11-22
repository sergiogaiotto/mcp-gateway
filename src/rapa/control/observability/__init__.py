"""Observabilidade do plano de controle.

- :mod:`~rapa.control.observability.metrics`: métricas Prometheus numa porta própria
  (``RAPA_METRICAS_PORTA``, ``RAPA_METRICAS_POR_EMPRESA``);
- :mod:`~rapa.control.observability.logs`: logs em texto ou em JSON (``RAPA_LOG_JSON``);
- :mod:`~rapa.control.observability.tracing`: traços OpenTelemetry, só com
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` (``OTEL_SERVICE_NAME`` dá o nome do serviço);
- :mod:`~rapa.control.observability.glitchtip`: exceções não tratadas no GlitchTip, só
  com ``RAPA_GLITCHTIP_DSN``.

A saúde (``/saude`` e ``/saude/pronto``) fica em :mod:`rapa.control.health`.

Os submódulos não se importam aqui: sem o endpoint ou o DSN, o OpenTelemetry e o
``sentry_sdk`` nem são carregados.
"""
