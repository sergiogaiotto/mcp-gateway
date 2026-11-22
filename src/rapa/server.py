"""O RaPA num processo só: REST, MCP, interface e despachante sobre o PostgreSQL.

Uso::

    python -m rapa.server

A configuração vem de variáveis de ambiente (veja :meth:`ServerSettings.from_env`).
Em produção, o despachante roda em réplicas próprias (``RAPA_HTTP=0``, com
``RAPA_DISPATCHER_DATABASE_URL``) e o lançador local dá lugar aos pods de uso único
(``RAPA_EXECUTOR=kubernetes``, com o canal do despachante na ``RAPA_CANAL_PORTA``); na
Fase 0 e em desenvolvimento, tudo cabe num processo.

O esquema do banco é criado antes, com ``python -m rapa.control.db.migrate``.

Observabilidade e saúde (:mod:`rapa.control.observability` e :mod:`rapa.control.health`):
métricas Prometheus numa porta própria (``RAPA_METRICAS_PORTA``), logs em JSON
(``RAPA_LOG_JSON``), traços OpenTelemetry (``OTEL_EXPORTER_OTLP_ENDPOINT``), exceções
não tratadas no GlitchTip (``RAPA_GLITCHTIP_DSN``), ``/saude`` (vivo) e
``/saude/pronto`` (pronto).
"""

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import shlex
import sys
import tempfile
from collections.abc import AsyncIterator, Coroutine, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from rapa.control.api.publishing import PublishingServices
from rapa.control.artifacts import ArtifactStore
from rapa.control.auth import (
    Authenticator,
    AuthError,
    BearerVerifier,
    CachedOrganizationDirectory,
    DevTokenVerifier,
    JwtSettings,
    TokenVerifier,
)
from rapa.control.backends import Backends, BackendSettings, VaultKind
from rapa.control.channel import Channel, ChannelServer
from rapa.control.db import Database
from rapa.control.health import (
    READY_PATH,
    DatabaseCheck,
    DispatcherCheck,
    Readiness,
    ReadinessCheck,
)
from rapa.control.http_app import HttpSettings, create_http_app
from rapa.control.kube import (
    KubeApi,
    KubeConfig,
    PodSettings,
    channel_address,
    parse_node_selector,
    parse_tolerations,
)
from rapa.control.launcher import (
    KubernetesLauncher,
    KubernetesLauncherSettings,
    LocalLauncherSettings,
    LocalProcessLauncher,
)
from rapa.control.observability.glitchtip import (
    GlitchTip,
    configure_glitchtip,
    dsn_problem,
    report_unhandled,
)
from rapa.control.observability.logs import configure_logging, uvicorn_log_config
from rapa.control.observability.metrics import (
    DEFAULT_METRICS_PORT,
    MeteredWebhooks,
    Metrics,
    MetricsServer,
    instrument_http,
    serve_metrics,
    watch_platform,
)
from rapa.control.observability.tracing import Tracing, activate_tracing
from rapa.control.services import (
    CredentialAlerts,
    Dispatcher,
    HubRepairAssistant,
    PgArtifacts,
    PgBatches,
    PgCompanyAdmin,
    PgCredentials,
    PgOrganizationDirectory,
    PgPlatform,
    PgPrincipalResolver,
    PgRepairs,
    PgRunQueue,
    PgSchedules,
    PgWebhooks,
    Retention,
    RetentionPolicy,
    Scheduler,
    WebhookKeys,
)
from rapa.control.services._common import now_utc
from rapa.control.services.queue import DEFAULT_MAX_DURATION
from rapa.control.services.retention import SUCCESS_AFTER_FAILURE
from rapa.control.services.sessions import PgSessionStore
from rapa.control.services.sheets import SheetLimits
from rapa.control.services.webhooks import WebhookGate
from rapa.control.sessions import (
    GeneralizationAssistant,
    HubAssistant,
    LocalSessionLauncher,
    LocalSessionSettings,
    RecordingSessions,
    SessionLimits,
)
from rapa.control.sessions.launcher import KubernetesSessionLauncher, KubernetesSessionSettings
from rapa.core.contracts import Principal
from rapa.runner.video import ffmpeg_installed
from rapa.session.egress import EgressPolicy

__all__ = [
    "AuthenticatorLoginResolver",
    "DispatcherSupervisor",
    "ServerSettings",
    "build_artifact_store",
    "build_artifacts",
    "build_credentials",
    "build_pod_launcher",
    "build_publishing",
    "build_readiness_checks",
    "build_repairs",
    "build_scheduler",
    "build_sessions",
    "create_server_app",
    "dispatcher_pool_size",
    "main",
]

logger = logging.getLogger("rapa.server")

_HTTP_URL_PREFIXES = ("http://", "https://")

_TRUE = {"1", "true", "sim", "yes", "on"}
_FALSE = {"0", "false", "nao", "não", "no", "off", ""}


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} precisa ser 1 ou 0")


def _path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    return Path(raw) if raw else None


def _json(env: Mapping[str, str], name: str) -> Any:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Sem repetir o valor: só o nome da variável.
        raise ValueError(f"{name} precisa ser um JSON válido") from None


def _version() -> str:
    try:
        return importlib.metadata.version("rapa")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - sempre instalado
        return "0"


type Executor = Literal["local", "kubernetes"]


class ServerSettings(BaseModel):
    """Configuração do serviço."""

    # Sem a entrada nas mensagens de erro: o pydantic poria nelas o valor recusado (ou um
    # pedaço do dicionário inteiro, numa regra do modelo), e ali estão as senhas dos
    # bancos, as chaves dos webhooks e do hub e o DSN do GlitchTip.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    #: Banco com o papel da aplicação (``rapa_app``, sujeito ao isolamento por linha).
    database_url: str = Field(min_length=1, repr=False)
    #: Banco com o papel do despachante; sem ele, este processo não despacha.
    dispatcher_database_url: str | None = Field(default=None, repr=False)
    http: bool = True
    ui: bool = True
    public_url: str | None = None
    #: Modo de desenvolvimento: tokens fixos, login sem senha e segredos em arquivo.
    dev: bool = False
    dev_tokens_file: Path | None = None
    dev_secrets_file: Path | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_url: str | None = None
    organization_aliases: dict[str, UUID] = Field(default_factory=dict)
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = Field(default=None, repr=False)
    oidc_redirect_uri: str | None = None
    cookie_secure: bool = True
    mcp_allowed_origins: frozenset[str] = frozenset()
    #: Origens da interface aceitas no websocket da visão ao vivo, além da de ``public_url``.
    live_allowed_origins: tuple[str, ...] = ()
    rate_limit_per_s: float | None = Field(default=20.0, gt=0)
    concurrency: int = Field(default=4, ge=1, le=256)
    poll_interval_s: float = Field(default=1.0, gt=0)
    work_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "rapa-trabalho")
    artifacts_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "rapa-artefatos"
    )
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    #: Arquivos de entrada, numa subpasta por empresa (``<pasta>/<uuid da empresa>/``).
    files_dir: Path | None = None
    #: Sessões de gravação (o navegador na nuvem da demonstração) neste processo.
    sessions: bool = True
    sessions_per_company: int = Field(default=2, ge=1, le=1000)
    #: Sessões abertas ao mesmo tempo neste processo; None usa a reserva interativa.
    sessions_max: int | None = Field(default=None, ge=1, le=10_000)
    session_idle_s: float = Field(default=600.0, gt=0, le=86_400)
    session_max_s: float = Field(default=3600.0, gt=0, le=86_400)
    #: Pasta dos perfis temporários dos navegadores das sessões (apagados no fim de cada uma).
    sessions_dir: Path | None = None
    #: Hub de LLM para as propostas da revisão (sem ele, só as sugestões das regras).
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_capabilities_file: Path | None = None
    #: Chave do servidor de que se deriva a chave de cada webhook; sem ela, não há webhooks.
    webhook_key: str | None = Field(default=None, min_length=32, repr=False)
    #: Até quantos segundos depois do horário uma ocorrência de agenda ainda roda.
    schedule_catch_up_s: float = Field(default=600.0, ge=0, le=7 * 86_400)
    #: De quantos em quantos segundos o despachante procura agendas vencidas.
    schedule_interval_s: float = Field(default=5.0, ge=0, le=3600)
    #: Agendas ligadas, no máximo, por empresa.
    schedules_per_company: int = Field(default=200, ge=1, le=100_000)
    #: Leituras de webhook por segundo, neste processo, de todas as entregas juntas
    #: (antes de conferir a assinatura; acima disso, 429 sem ir ao banco).
    webhook_lookups_per_s: float = Field(default=200.0, gt=0, le=100_000)
    #: Linhas e tamanho máximos da planilha de um lote.
    batch_max_rows: int = Field(default=1000, ge=1, le=100_000)
    batch_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    #: Quantos candidatos o conserto assistido mostra (e o modelo vê) para o passo que quebrou.
    repair_candidates: int = Field(default=8, ge=1, le=20)
    #: Quantos dias antes do vencimento de um certificado A1 o aviso começa.
    a1_warn_days: int = Field(default=30, ge=1, le=3650)
    #: Filma cada execução e guarda o vídeo só quando ela falha ou fica indeterminada.
    video_on_failure: bool = True
    #: Prazos de retenção, em dias (a validar com o jurídico): telas e vídeos de execuções
    #: que deram certo; evidências (todos os artefatos de falhas e indeterminadas e os
    #: arquivos baixados das que deram certo) e retratos das falhas; partições de eventos.
    retention_success_days: int = Field(default=7, ge=1, le=36_500)
    retention_failure_days: int = Field(default=90, ge=1, le=36_500)
    retention_events_days: int = Field(default=400, ge=1, le=36_500)
    #: Onde rodam as execuções: ``local`` (um processo) ou ``kubernetes`` (um pod cada).
    executor: Executor = "local"
    #: Onde rodam as sessões de gravação; ``None``: o mesmo das execuções.
    sessions_executor: Executor | None = None
    k8s_namespace: str = "rapa-execucoes"
    k8s_image: str | None = None
    #: ``None`` (``RAPA_K8S_RUNTIME_CLASS`` vazia) só para kind e desenvolvimento.
    k8s_runtime_class: str | None = "gvisor"
    k8s_cpu: str = "1"
    k8s_memory: str = "2Gi"
    k8s_service_account: str | None = None
    k8s_pull_secret: str | None = None
    k8s_node_selector: dict[str, str] = Field(default_factory=dict)
    k8s_tolerations: tuple[dict[str, Any], ...] = ()
    #: Endereço do canal que os pods usam (``{ip}`` vira o IP deste pod).
    channel_address: str | None = None
    channel_port: int = Field(default=8081, ge=1, le=65535)
    #: Proxy HTTP CONNECT de saída para a internet dos navegadores.
    egress_proxy: str | None = Field(default=None, max_length=2000)
    #: Redes internas (CIDR) que os navegadores alcançam além da internet.
    internal_networks: tuple[str, ...] = ()

    @property
    def sessions_runner(self) -> Executor:
        """Onde rodam as sessões de gravação (``RAPA_SESSOES_EXECUTOR``, ou o das execuções)."""
        return self.sessions_executor or self.executor

    @property
    def egress_guard(self) -> bool:
        """Se o navegador das execuções (e das sessões) sai pela guarda com a rede interna
        fechada: nos pods, com redes internas permitidas ou proxy de saída, e sempre fora
        do modo de desenvolvimento."""
        return (
            self.executor == "kubernetes"
            or bool(self.internal_networks)
            or self.egress_proxy is not None
            or not self.dev
        )

    def egress_policy(self) -> EgressPolicy:
        """A política da guarda da saída (as redes internas permitidas e o proxy)."""
        return EgressPolicy.parse(self.internal_networks, self.egress_proxy)

    def local_egress(self) -> EgressPolicy | None:
        """A guarda dos navegadores locais: ``None`` só no desenvolvimento sem redes nem proxy."""
        return self.egress_policy() if self.egress_guard else None

    def pod_settings(self) -> PodSettings:
        """Como rodam os pods de execução e de sessão (``RAPA_K8S_*``)."""
        return PodSettings(
            image=self.k8s_image or "",
            namespace=self.k8s_namespace,
            runtime_class=self.k8s_runtime_class,
            cpu=self.k8s_cpu,
            memory=self.k8s_memory,
            service_account=self.k8s_service_account,
            pull_secret=self.k8s_pull_secret,
            node_selector=dict(self.k8s_node_selector),
            tolerations=tuple(self.k8s_tolerations),
        )

    #: O cofre, o armazenamento dos artefatos e as chaves (``RAPA_COFRE``,
    #: ``RAPA_ARMAZENAMENTO``, ``RAPA_CHAVES`` e os seus; :mod:`rapa.control.backends`).
    backends: BackendSettings = Field(default_factory=BackendSettings)

    @model_validator(mode="before")
    @classmethod
    def _backends_dev(cls, data: Any) -> Any:
        # Sem a configuração do cofre dada, ela segue o modo de desenvolvimento do serviço.
        if isinstance(data, dict) and "backends" not in data:
            return {**data, "backends": BackendSettings(dev=bool(data.get("dev", False)))}
        return data

    #: Porta do servidor de métricas (só ``GET /metrics``, fora do gateway); 0 desliga.
    metrics_port: int = Field(default=DEFAULT_METRICS_PORT, ge=0, le=65_535)
    #: Rótulo ``empresa`` nas execuções concluídas, na fila e nos avisos do A1.
    metrics_per_company: bool = False
    #: Logs em JSON, um objeto por linha.
    log_json: bool = False
    #: Base do OTLP (``OTEL_EXPORTER_OTLP_ENDPOINT``); sem ela, nada de rastreio.
    otel_endpoint: str | None = None
    #: Endereço só dos traços (``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``), se for outro.
    otel_traces_endpoint: str | None = None
    #: Nome do serviço nos traços (``OTEL_SERVICE_NAME``; padrão ``rapa``).
    otel_service_name: str | None = None
    #: GlitchTip das exceções não tratadas (o DSN tem a chave do projeto).
    glitchtip_dsn: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _check(self) -> ServerSettings:
        if not self.http and self.dispatcher_database_url is None:
            raise ValueError("sem HTTP e sem despachante, o processo não teria o que fazer")
        jwt = (self.jwt_issuer, self.jwt_audience, self.jwks_url)
        if any(jwt) and not all(jwt):
            raise ValueError("para tokens do Keycloak, informe emissor, público e URL das chaves")
        if self.http and not all(jwt) and not (self.dev and self.dev_tokens_file):
            raise ValueError(
                "configure a verificação de tokens do Keycloak (RAPA_JWT_*) ou, só em "
                "desenvolvimento, RAPA_DEV=1 com RAPA_TOKENS_DEV"
            )
        if (self.dev_tokens_file or self.dev_secrets_file) and not self.dev:
            raise ValueError("tokens e segredos em arquivo só valem com RAPA_DEV=1")
        if self.backends.dev != self.dev:
            raise ValueError("a configuração do cofre e a do serviço discordam sobre RAPA_DEV")
        if self.dev_secrets_file is not None and self.backends.vault not in (
            None,
            VaultKind.DEV,
        ):
            raise ValueError("RAPA_SEGREDOS_DEV só vale com RAPA_COFRE=dev")
        if self.backends.vault is VaultKind.DEV and self.dev_secrets_file is None:
            raise ValueError("RAPA_COFRE=dev precisa do arquivo RAPA_SEGREDOS_DEV")
        llm = (self.llm_base_url, self.llm_model)
        if any(llm) and not all(llm):
            raise ValueError("para o hub de LLM, informe o endereço e o modelo (RAPA_LLM_*)")
        if self.llm_base_url and self.llm_capabilities_file is None:
            raise ValueError(
                "para usar o hub de LLM, informe a matriz da sonda em RAPA_LLM_CAPACIDADES"
            )
        if self.session_idle_s > self.session_max_s:
            raise ValueError("o tempo sem atividade de uma sessão não pode passar do prazo máximo")
        if self.retention_success_days > self.retention_failure_days:
            raise ValueError(SUCCESS_AFTER_FAILURE)
        oidc = (self.oidc_client_id, self.oidc_redirect_uri)
        if any(oidc) and not all(oidc):
            raise ValueError("para o login OIDC, informe o cliente e o endereço de retorno")
        if self.ui and self.http and self.oidc_client_id and not self.jwt_issuer:
            raise ValueError("o login OIDC da interface precisa do emissor (RAPA_JWT_ISSUER)")
        try:
            self.egress_policy()
        except ValueError as exc:
            raise ValueError(f"RAPA_REDES_INTERNAS_PERMITIDAS ou RAPA_PROXY_SAIDA: {exc}") from None
        if "kubernetes" in (self.executor, self.sessions_runner):
            if not self.k8s_image:
                raise ValueError(
                    "no modo kubernetes, informe a imagem do executor (RAPA_K8S_IMAGEM_EXECUTOR)"
                )
            self.pod_settings()  # nomes, quantidades e rótulos no formato do Kubernetes
        if self.executor == "kubernetes":
            # Só o formato: o IP deste pod é descoberto ao montar o despachante.
            channel_address(self.channel_address, self.channel_port, resolve_ip=lambda: "10.0.0.1")
        for url, name in (
            (self.otel_endpoint, "OTEL_EXPORTER_OTLP_ENDPOINT"),
            (self.otel_traces_endpoint, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
        ):
            if url is not None and not url.startswith(_HTTP_URL_PREFIXES):
                raise ValueError(f"{name} precisa ser um endereço http:// ou https://")
        if self.otel_traces_endpoint and not self.otel_endpoint:
            raise ValueError(
                "o rastreio só liga com OTEL_EXPORTER_OTLP_ENDPOINT (o endereço dos traços "
                "sozinho não basta)"
            )
        if self.glitchtip_dsn is not None and (problem := dsn_problem(self.glitchtip_dsn)):
            # A mesma leitura do sentry_sdk, na partida; sem o valor na mensagem (o DSN
            # tem a chave do projeto).
            raise ValueError(f"RAPA_GLITCHTIP_DSN {problem}")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        """Lê a configuração das variáveis ``RAPA_*``.

        - ``RAPA_DATABASE_URL`` (obrigatória) e ``RAPA_DISPATCHER_DATABASE_URL``;
        - ``RAPA_HTTP``, ``RAPA_UI`` (1 ou 0) e ``RAPA_PUBLIC_URL``;
        - ``RAPA_JWT_ISSUER``, ``RAPA_JWT_AUDIENCE``, ``RAPA_JWKS_URL`` e
          ``RAPA_ORGANIZATION_ALIASES`` (JSON ``{"alias": "uuid"}``);
        - ``RAPA_OIDC_CLIENT_ID``, ``RAPA_OIDC_CLIENT_SECRET``, ``RAPA_OIDC_REDIRECT_URI``;
        - ``RAPA_DEV``, ``RAPA_TOKENS_DEV`` (JSON ``{"token": {"sujeito": ..., "empresa": ...}}``)
          e ``RAPA_SEGREDOS_DEV`` (JSON ``{"uuid da empresa": {"credencial": {...}}}``);
        - ``RAPA_CONCORRENCIA``, ``RAPA_PASTA_TRABALHO``, ``RAPA_PASTA_ARTEFATOS``,
          ``RAPA_PASTA_ARQUIVOS``, ``RAPA_CHROMIUM_PATH`` e ``RAPA_ARGS_NAVEGADOR``
          (argumentos separados por espaço, com aspas se preciso). A pasta de
          arquivos de entrada tem uma subpasta por empresa:
          ``<RAPA_PASTA_ARQUIVOS>/<uuid da empresa>/<arquivo>``; cada execução só
          enxerga a subpasta da própria empresa, e arquivos soltos na raiz não valem;
        - ``RAPA_MCP_ORIGENS`` (separadas por vírgula), ``RAPA_LIMITE_POR_SEGUNDO``
          (0 desliga) e ``RAPA_COOKIE_SEGURO``;
        - sessões de gravação (a demonstração no navegador da nuvem): ``RAPA_SESSOES``
          (1 ou 0; liga por padrão), ``RAPA_SESSOES_POR_EMPRESA`` (abertas ao mesmo tempo
          por empresa; padrão 2, nunca mais que metade de ``RAPA_SESSOES_MAX``),
          ``RAPA_SESSOES_MAX`` (vagas da plataforma para as gravações, contando as
          validações rodando; padrão: o tamanho da reserva interativa, 10% das vagas),
          ``RAPA_SESSAO_OCIOSA_S`` (segundos sem entrada até encerrar; padrão 600),
          ``RAPA_SESSAO_MAX_S`` (prazo máximo; padrão 3600), ``RAPA_PASTA_SESSOES``
          (onde ficam os perfis temporários dos navegadores, apagados no fim) e
          ``RAPA_ORIGENS_AO_VIVO`` (separadas por vírgula: origens da interface aceitas
          no websocket da visão ao vivo, além da de ``RAPA_PUBLIC_URL``). Os
          navegadores usam ``RAPA_CHROMIUM_PATH`` (o Chromium completo: o
          ``chrome-headless-shell`` ignora a política do WebRTC) e ``RAPA_ARGS_NAVEGADOR``;
        - hub de LLM para as propostas da revisão: ``RAPA_LLM_BASE_URL``,
          ``RAPA_LLM_MODEL``, ``RAPA_LLM_API_KEY`` e ``RAPA_LLM_CAPACIDADES`` (a
          matriz gravada por ``python -m rapa.llm.probe --out``). Sem o hub, a revisão
          mostra só as sugestões das regras, com um aviso;
        - publicar: ``RAPA_CHAVE_WEBHOOKS`` (pelo menos 32 caracteres; a chave de cada
          webhook é derivada dela com o sal do webhook, e trocá-la invalida as chaves de
          todos; sem ela, os webhooks respondem 503), ``RAPA_AGENDA_TOLERANCIA_S`` (até
          quantos segundos depois do horário uma ocorrência de agenda ainda roda; depois
          disso, fica registrada como perdida; padrão 600), ``RAPA_AGENDA_INTERVALO_S``
          (de quantos em quantos segundos o despachante procura agendas vencidas; padrão
          5), ``RAPA_AGENDAS_POR_EMPRESA`` (agendas ligadas, no máximo, por empresa;
          padrão 200), ``RAPA_WEBHOOKS_POR_SEGUNDO`` (leituras de webhook por segundo
          neste processo, de todas as entregas juntas, antes de conferir a assinatura;
          acima disso, 429 sem ir ao banco; padrão 200; o limite de recusas é por
          endereço de origem, então atrás de um proxy o uvicorn precisa de
          ``FORWARDED_ALLOW_IPS`` para ver o do cliente), ``RAPA_LOTE_MAX_LINHAS`` (padrão
          1000) e ``RAPA_LOTE_MAX_BYTES`` (tamanho máximo da planilha de um lote, pelo
          REST e pela tela Lotes; padrão 5 MiB);
        - consertar: ``RAPA_CONSERTO_CANDIDATOS`` (quantos candidatos o conserto
          assistido mostra para o passo que quebrou, e o modelo vê; de 1 a 20, padrão
          8). O modelo que escolhe entre eles é o mesmo hub da revisão
          (``RAPA_LLM_*``); sem ele, a pessoa escolhe sozinha, com um aviso;
        - certificado A1: ``RAPA_A1_AVISO_DIAS`` (quantos dias antes do vencimento o
          aviso começa, na interface, no REST e na auditoria; de 1 a 3650, padrão 30);
        - evidências: ``RAPA_VIDEO_EM_FALHAS`` (1 ou 0; liga por padrão: cada execução
          é filmada e o vídeo fica só quando ela falha ou fica indeterminada, e só se o
          robô não digita credencial fora de um campo de senha; precisa do ``ffmpeg`` do
          Playwright no executor, e sem ele a execução roda sem vídeo, com um aviso no
          log ao subir) e os prazos de retenção, em dias, **a validar com o jurídico**:
          ``RAPA_RETENCAO_SUCESSO_DIAS`` (telas e vídeos de execuções que deram certo ou
          foram canceladas; padrão 7), ``RAPA_RETENCAO_FALHAS_DIAS`` (as evidências:
          todos os artefatos de falhas e indeterminadas, os arquivos baixados das que
          deram certo, e os retratos das falhas; padrão 90) e
          ``RAPA_RETENCAO_EVENTOS_DIAS`` (partições de eventos; padrão 400). Todos têm
          no mínimo 1 dia, e ``RAPA_RETENCAO_SUCESSO_DIAS`` não passa de
          ``RAPA_RETENCAO_FALHAS_DIAS`` (o servidor não sobe);
        - execuções e sessões em pods: ``RAPA_EXECUTOR`` (``local``, o padrão, ou
          ``kubernetes``: cada tentativa num pod de uso único, ver
          :class:`rapa.control.launcher.KubernetesLauncher`) e ``RAPA_SESSOES_EXECUTOR``
          (o mesmo para as sessões de gravação; padrão igual a ``RAPA_EXECUTOR``), com
          ``RAPA_K8S_NAMESPACE_EXECUCOES`` (padrão ``rapa-execucoes``),
          ``RAPA_K8S_IMAGEM_EXECUTOR`` (obrigatória no modo kubernetes),
          ``RAPA_K8S_RUNTIME_CLASS`` (padrão ``gvisor``; vazia, sem RuntimeClass, só para
          kind e desenvolvimento), ``RAPA_K8S_CPU`` (padrão 1), ``RAPA_K8S_MEMORIA``
          (padrão 2Gi), ``RAPA_K8S_SA_EXECUTOR`` (a conta de serviço dos pods, sem token
          montado), ``RAPA_K8S_PULL_SECRET``, ``RAPA_K8S_NODE_SELECTOR`` e
          ``RAPA_K8S_TOLERATIONS`` (JSON); o canal do despachante:
          ``RAPA_CANAL_PORTA`` (padrão 8081, onde ele escuta) e ``RAPA_CANAL_ENDERECO``
          (o endereço que os pods usam; padrão ``http://{ip}:<porta>``, e ``{ip}`` vira
          o IP do pod do despachante: cada réplica recebe os pods que lançou, então um
          endereço sem ``{ip}``, como o nome de um Service, é recusado na partida);
        - a guarda da saída dos navegadores: ``RAPA_REDES_INTERNAS_PERMITIDAS`` (CIDRs
          separados por vírgula que os robôs e as sessões alcançam além da internet, para
          portais internos; padrão nenhuma) e ``RAPA_PROXY_SAIDA`` (proxy HTTP CONNECT
          para a internet, ``http://host:porta``, sem usuário e senha). A guarda (só os
          domínios do robô, a rede interna fechada) vale sempre nos pods e, no lançador
          local, quando uma das duas está definida e sempre fora de ``RAPA_DEV``;
        - cofre, armazenamento e chaves (:mod:`rapa.control.backends`): ``RAPA_COFRE``
          (``nenhum``, ``dev`` ou ``openbao``), ``RAPA_BAO_*``, ``RAPA_ARMAZENAMENTO``
          (``local`` ou ``s3``), ``RAPA_S3_*`` com ``AWS_ACCESS_KEY_ID`` e
          ``AWS_SECRET_ACCESS_KEY``, ``RAPA_CHAVES`` (``openbao`` ou ``local``) e
          ``RAPA_CHAVE_ARTEFATOS``, conferidos na partida;
        - observabilidade: ``RAPA_METRICAS_PORTA`` (a porta do servidor de métricas,
          só ``GET /metrics``, no mesmo endereço do serviço, o ``--host``, e que nunca
          passa pelo gateway; padrão 9100, 0 desliga),
          ``RAPA_METRICAS_POR_EMPRESA`` (1 ou 0; desligado por padrão: com ele, as
          execuções concluídas, a fila e os avisos do A1 ganham o rótulo ``empresa``),
          ``RAPA_LOG_JSON`` (1 ou 0: um objeto JSON por linha, com hora, nível, logger,
          mensagem e o id de rastreio), ``OTEL_EXPORTER_OTLP_ENDPOINT`` (liga os traços
          OpenTelemetry por OTLP/HTTP; sem ele, nada é carregado),
          ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` e ``OTEL_SERVICE_NAME`` (padrão
          ``rapa``), e ``RAPA_GLITCHTIP_DSN`` (as exceções não tratadas, limpas, no
          GlitchTip). Os traços e o GlitchTip precisam do extra ``observability``.
        """
        env = os.environ if env is None else env
        data: dict[str, Any] = {
            "database_url": env.get("RAPA_DATABASE_URL", ""),
            "dispatcher_database_url": env.get("RAPA_DISPATCHER_DATABASE_URL") or None,
            "http": _flag(env, "RAPA_HTTP", True),
            "ui": _flag(env, "RAPA_UI", True),
            "public_url": env.get("RAPA_PUBLIC_URL") or None,
            "dev": _flag(env, "RAPA_DEV", False),
            "dev_tokens_file": _path(env, "RAPA_TOKENS_DEV"),
            "dev_secrets_file": _path(env, "RAPA_SEGREDOS_DEV"),
            "jwt_issuer": env.get("RAPA_JWT_ISSUER") or None,
            "jwt_audience": env.get("RAPA_JWT_AUDIENCE") or None,
            "jwks_url": env.get("RAPA_JWKS_URL") or None,
            "oidc_client_id": env.get("RAPA_OIDC_CLIENT_ID") or None,
            "oidc_client_secret": env.get("RAPA_OIDC_CLIENT_SECRET") or None,
            "oidc_redirect_uri": env.get("RAPA_OIDC_REDIRECT_URI") or None,
            "cookie_secure": _flag(env, "RAPA_COOKIE_SEGURO", True),
            "chromium": env.get("RAPA_CHROMIUM_PATH") or None,
            "browser_args": tuple(shlex.split(env.get("RAPA_ARGS_NAVEGADOR", ""))),
            "files_dir": _path(env, "RAPA_PASTA_ARQUIVOS"),
            "sessions": _flag(env, "RAPA_SESSOES", True),
            "sessions_dir": _path(env, "RAPA_PASTA_SESSOES"),
            "llm_base_url": env.get("RAPA_LLM_BASE_URL") or None,
            "llm_model": env.get("RAPA_LLM_MODEL") or None,
            "llm_api_key": env.get("RAPA_LLM_API_KEY") or None,
            "llm_capabilities_file": _path(env, "RAPA_LLM_CAPACIDADES"),
            "webhook_key": env.get("RAPA_CHAVE_WEBHOOKS") or None,
            "video_on_failure": _flag(env, "RAPA_VIDEO_EM_FALHAS", True),
            "executor": env.get("RAPA_EXECUTOR", "").strip().lower() or "local",
            "sessions_executor": env.get("RAPA_SESSOES_EXECUTOR", "").strip().lower() or None,
            "k8s_image": env.get("RAPA_K8S_IMAGEM_EXECUTOR", "").strip() or None,
            "k8s_service_account": env.get("RAPA_K8S_SA_EXECUTOR", "").strip() or None,
            "k8s_pull_secret": env.get("RAPA_K8S_PULL_SECRET", "").strip() or None,
            "k8s_node_selector": parse_node_selector(_json(env, "RAPA_K8S_NODE_SELECTOR")),
            "k8s_tolerations": parse_tolerations(_json(env, "RAPA_K8S_TOLERATIONS")),
            "channel_address": env.get("RAPA_CANAL_ENDERECO", "").strip() or None,
            "egress_proxy": env.get("RAPA_PROXY_SAIDA", "").strip() or None,
            "internal_networks": tuple(
                n.strip()
                for n in env.get("RAPA_REDES_INTERNAS_PERMITIDAS", "").split(",")
                if n.strip()
            ),
            "backends": BackendSettings.from_env(env),
            "metrics_per_company": _flag(env, "RAPA_METRICAS_POR_EMPRESA", False),
            "log_json": _flag(env, "RAPA_LOG_JSON", False),
            "otel_endpoint": env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None,
            "otel_traces_endpoint": (
                env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip() or None
            ),
            "otel_service_name": env.get("OTEL_SERVICE_NAME", "").strip() or None,
            "glitchtip_dsn": env.get("RAPA_GLITCHTIP_DSN", "").strip() or None,
        }
        for key, name in (
            ("k8s_namespace", "RAPA_K8S_NAMESPACE_EXECUCOES"),
            ("k8s_cpu", "RAPA_K8S_CPU"),
            ("k8s_memory", "RAPA_K8S_MEMORIA"),
        ):
            if raw := env.get(name, "").strip():
                data[key] = raw
        if (runtime := env.get("RAPA_K8S_RUNTIME_CLASS")) is not None:
            # Vazia: sem RuntimeClass (só kind e desenvolvimento, sem o gVisor).
            data["k8s_runtime_class"] = runtime.strip() or None
        if raw_port := env.get("RAPA_CANAL_PORTA", "").strip():
            data["channel_port"] = int(raw_port)
        for key, name, kind in (
            ("sessions_per_company", "RAPA_SESSOES_POR_EMPRESA", int),
            ("sessions_max", "RAPA_SESSOES_MAX", int),
            ("session_idle_s", "RAPA_SESSAO_OCIOSA_S", float),
            ("session_max_s", "RAPA_SESSAO_MAX_S", float),
            ("schedule_catch_up_s", "RAPA_AGENDA_TOLERANCIA_S", float),
            ("schedule_interval_s", "RAPA_AGENDA_INTERVALO_S", float),
            ("schedules_per_company", "RAPA_AGENDAS_POR_EMPRESA", int),
            ("webhook_lookups_per_s", "RAPA_WEBHOOKS_POR_SEGUNDO", float),
            ("batch_max_rows", "RAPA_LOTE_MAX_LINHAS", int),
            ("batch_max_bytes", "RAPA_LOTE_MAX_BYTES", int),
            ("repair_candidates", "RAPA_CONSERTO_CANDIDATOS", int),
            ("a1_warn_days", "RAPA_A1_AVISO_DIAS", int),
            ("retention_success_days", "RAPA_RETENCAO_SUCESSO_DIAS", int),
            ("retention_failure_days", "RAPA_RETENCAO_FALHAS_DIAS", int),
            ("retention_events_days", "RAPA_RETENCAO_EVENTOS_DIAS", int),
            ("metrics_port", "RAPA_METRICAS_PORTA", int),
        ):
            if raw := env.get(name, "").strip():
                data[key] = kind(raw)
        if aliases := env.get("RAPA_ORGANIZATION_ALIASES"):
            data["organization_aliases"] = json.loads(aliases)
        if origins := env.get("RAPA_MCP_ORIGENS"):
            data["mcp_allowed_origins"] = frozenset(
                o.strip() for o in origins.split(",") if o.strip()
            )
        if live := env.get("RAPA_ORIGENS_AO_VIVO"):
            data["live_allowed_origins"] = tuple(o.strip() for o in live.split(",") if o.strip())
        if (rate := env.get("RAPA_LIMITE_POR_SEGUNDO")) is not None:
            data["rate_limit_per_s"] = float(rate) or None
        if concurrency := env.get("RAPA_CONCORRENCIA"):
            data["concurrency"] = int(concurrency)
        for key, name in (
            ("work_dir", "RAPA_PASTA_TRABALHO"),
            ("artifacts_dir", "RAPA_PASTA_ARTEFATOS"),
        ):
            if (value := _path(env, name)) is not None:
                data[key] = value
        return cls.model_validate(data)


def load_dev_tokens(path: Path) -> dict[str, tuple[str, UUID]]:
    """Lê ``{"token": {"sujeito": "...", "empresa": "uuid"}}``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("o arquivo de tokens precisa ser um objeto JSON com pelo menos um token")
    tokens: dict[str, tuple[str, UUID]] = {}
    for token, identity in raw.items():
        if not isinstance(identity, dict) or set(identity) != {"sujeito", "empresa"}:
            raise ValueError("cada token precisa de 'sujeito' e 'empresa'")
        tokens[str(token)] = (str(identity["sujeito"]), UUID(str(identity["empresa"])))
    return tokens


class AuthenticatorLoginResolver:
    """Resolvedor do login OIDC da interface sobre o autenticador da API.

    O token de acesso recebido no login passa pela mesma verificação dos
    pedidos REST e MCP, e os papéis vêm do banco.
    """

    def __init__(self, authenticator: Authenticator) -> None:
        self.authenticator = authenticator

    async def resolve(self, claims: Mapping[str, Any], access_token: str) -> Principal | None:
        try:
            return await self.authenticator.authenticate(f"Bearer {access_token}")
        except AuthError:
            return None


def _run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Roda a corrotina até o fim, mesmo dentro de um laço de eventos já rodando."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _dev_profiles(
    database_url: str, tokens: Mapping[str, tuple[str, UUID]]
) -> list[tuple[str, Principal]]:
    db = Database.from_url(database_url)
    try:
        resolver = PgPrincipalResolver(db)
        out: list[tuple[str, Principal]] = []
        for token, (subject, company_id) in tokens.items():
            principal = await resolver.resolve(subject, company_id)
            if principal is None:
                logger.warning("token de desenvolvimento sem usuário ativo no banco; ignorado")
                continue
            out.append((token, principal))
        return out
    finally:
        await db.dispose()


def _verifier(settings: ServerSettings, db: Database) -> BearerVerifier:
    if settings.jwt_issuer and settings.jwt_audience and settings.jwks_url:
        # A empresa da organização também vem do banco (companies.idp_organization), com
        # cache curto: uma empresa nova entra sem reiniciar o serviço.
        return TokenVerifier(
            JwtSettings(
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                jwks_url=settings.jwks_url,
                organization_aliases=settings.organization_aliases,
            ),
            organizations=CachedOrganizationDirectory(PgOrganizationDirectory(db)),
        )
    if settings.dev_tokens_file is None:  # pragma: no cover - barrado pela validação
        raise ValueError("sem verificação de tokens configurada")
    return DevTokenVerifier(load_dev_tokens(settings.dev_tokens_file))


def _mount_ui(
    app: FastAPI,
    settings: ServerSettings,
    platform: PgPlatform,
    authenticator: Authenticator,
    sessions: RecordingSessions | None = None,
    publishing: PublishingServices | None = None,
    repairs: PgRepairs | None = None,
    credentials: PgCredentials | None = None,
    company: PgCompanyAdmin | None = None,
    artifacts: PgArtifacts | None = None,
) -> None:
    from rapa.ui.app import create_ui
    from rapa.ui.auth import DevLogin, DevProfile, OidcLogin, OidcSettings, UiAuth
    from rapa.ui.backend import ContractsBackendFactory
    from rapa.ui.context import UiSettings

    dev_login = None
    if settings.dev and settings.dev_tokens_file is not None:
        profiles = _run_sync(
            _dev_profiles(settings.database_url, load_dev_tokens(settings.dev_tokens_file))
        )
        dev_login = DevLogin(
            DevProfile(
                key=f"perfil{index}",
                label=principal.subject,
                principal=principal,
                description="perfil de desenvolvimento",
                access_token=token,
            )
            for index, (token, principal) in enumerate(profiles, start=1)
        )
    oidc = None
    if settings.oidc_client_id and settings.oidc_redirect_uri and settings.jwt_issuer:
        oidc = OidcLogin(
            OidcSettings(
                issuer=settings.jwt_issuer,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                redirect_uri=settings.oidc_redirect_uri,
            ),
            AuthenticatorLoginResolver(authenticator),
        )
    if dev_login is None and oidc is None:
        raise ValueError("a interface precisa do login OIDC (RAPA_OIDC_*) ou do modo RAPA_DEV")
    create_ui(
        app,
        backend_factory=ContractsBackendFactory.from_platform(
            platform,
            sessions=sessions,
            schedules=publishing.schedules if publishing else None,
            webhooks=publishing.webhooks if publishing else None,
            batches=publishing.batches if publishing else None,
            repairs=repairs,
            credentials=credentials,
            company=company,
            artifacts=artifacts,
        ),
        # A interface age direto nos serviços, então a sessão confere de novo no banco
        # (conta ativa e papéis), como a API faz a cada pedido.
        auth=UiAuth(
            dev=dev_login,
            oidc=oidc,
            cookie_secure=settings.cookie_secure,
            principals=authenticator.resolver,
        ),
        # O envio da tela Lotes aceita o mesmo tamanho de planilha que o REST.
        settings=UiSettings(batch_max_bytes=settings.batch_max_bytes),
    )


def dispatcher_pool_size(concurrency: int) -> int:
    """Conexões do pool do banco do despachante para ``concurrency`` execuções juntas.

    Cada execução em andamento pode usar duas conexões ao mesmo tempo (a renovação
    da concessão e a gravação dos eventos ou do resultado), e o laço principal usa
    mais duas (pegar a próxima execução e a manutenção da fila). Com menos, as
    execuções disputam o pool e a renovação da concessão pode atrasar.
    """
    return 2 * concurrency + 2


#: Espera máxima pelas execuções em andamento no encerramento. Toda execução termina
#: sozinha antes disso: o prazo dela é de no máximo ``DEFAULT_MAX_DURATION``, o
#: lançador para o executor logo depois do prazo e o despachante insiste em gravar o
#: resultado por até 5 minutos além dele. Passar daqui é sinal de algo preso.
_DRAIN_TIMEOUT_S = DEFAULT_MAX_DURATION.total_seconds() + 600.0


class DispatcherSupervisor:
    """Mantém o despachante deste processo rodando e diz se ele está de pé.

    O laço do despachante já sobrevive aos erros de cada volta. Se mesmo assim ele
    parar sem ter sido pedido (um erro inesperado), o supervisor registra o tipo do
    erro e o reinicia depois de ``restart_delay_s``, espera que dobra a cada parada
    seguida, até ``max_restart_delay_s``. Reiniciar é seguro: as execuções em
    andamento continuam no mesmo :class:`Dispatcher`, que só volta a pegar novas.
    Enquanto o despachante não está rodando, :attr:`healthy` é falso e ``/saude``
    responde 503.

    ``drain_timeout_s`` é quanto o encerramento espera, depois de o laço parar, as
    execuções que ficaram em andamento (veja :func:`_stop_dispatcher`).
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        restart_delay_s: float = 1.0,
        max_restart_delay_s: float = 30.0,
        drain_timeout_s: float = _DRAIN_TIMEOUT_S,
    ) -> None:
        if restart_delay_s <= 0 or max_restart_delay_s < restart_delay_s:
            raise ValueError("as esperas para reiniciar o despachante precisam ser positivas")
        if drain_timeout_s <= 0:
            raise ValueError("a espera pelas execuções no encerramento precisa ser positiva")
        self.dispatcher = dispatcher
        self.restart_delay_s = restart_delay_s
        self.max_restart_delay_s = max_restart_delay_s
        self.drain_timeout_s = drain_timeout_s
        #: Quantas vezes o despachante parou sem pedido e foi reiniciado.
        self.restarts = 0
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def healthy(self) -> bool:
        """O despachante está rodando e nenhuma etapa do laço falha (ou trava) há tempo demais."""
        running = self._running and self._task is not None and not self._task.done()
        return running and self.dispatcher.healthy

    @property
    def problem(self) -> dict[str, Any]:
        """O que ``/saude`` mostra quando não está saudável: só nomes, nunca o texto do erro."""
        if not (self._running and self._task is not None and not self._task.done()):
            return {"estado": "despachante_parado"}
        detail: dict[str, Any] = {"estado": "despachante_com_falha"}
        if self.dispatcher.stalled:
            detail["volta_presa"] = True
        failing = sorted(self.dispatcher.failing)
        if failing:
            detail["etapas"] = failing
        return detail

    def start(self, stop: asyncio.Event) -> asyncio.Task[None]:
        """Começa a supervisionar; a tarefa termina quando ``stop`` é acionado.

        Só uma vez por supervisor: uma segunda chamada levanta ``RuntimeError``
        antes de criar qualquer tarefa.
        """
        if self._task is not None:
            raise RuntimeError("o despachante já foi iniciado")
        self._running = True
        self._task = asyncio.create_task(self._supervise(stop), name="despachante")
        self._task.add_done_callback(self._ended)
        return self._task

    async def _supervise(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        delay = self.restart_delay_s
        while True:
            self._running = True
            started = loop.time()
            error: str | None = None
            try:
                await self.dispatcher.run(stop)
            except Exception as exc:
                # Só o tipo: a mensagem de um erro inesperado pode trazer dados.
                error = type(exc).__name__
                report_unhandled(exc)
            finally:
                self._running = False
            if stop.is_set():
                if error is not None:
                    logger.error("o despachante falhou ao encerrar: %s", error)
                return
            reason = error or "parou sem pedido"
            if loop.time() - started >= self.max_restart_delay_s:
                delay = self.restart_delay_s  # rodou bem por um tempo: espera curta de novo
            self.restarts += 1
            logger.error("o despachante parou (%s); reiniciando em %.1f s", reason, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            if stop.is_set():
                return
            delay = min(delay * 2, self.max_restart_delay_s)

    def _ended(self, task: asyncio.Task[None]) -> None:
        self._running = False
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("a supervisão do despachante parou: %s", type(error).__name__)
            report_unhandled(error)


async def _stop_dispatcher(
    supervisor: DispatcherSupervisor, stop: asyncio.Event, task: asyncio.Task[None]
) -> None:
    """Pede a parada e espera o despachante terminar o que está em andamento.

    Quando está rodando, o laço do despachante espera sozinho as execuções em
    andamento ao parar. Mas, se ele estava parado na espera para reiniciar, ou se a própria
    supervisão já tinha parado com erro (registrado em
    :meth:`DispatcherSupervisor._ended`), as execuções continuam no despachante e
    são esperadas aqui, antes de o ``lifespan`` fechar os bancos.

    Essa espera vai até ``drain_timeout_s``. O que ainda estiver em andamento é
    registrado (só a contagem) e interrompido: o lançador para o executor e nada é
    gravado; quando a concessão vencer, o recolhimento devolve a execução à fila
    se nenhum passo que grava tinha começado, a marca como falha se um passo que
    grava já tinha começado, ou como indeterminada se um passo irreversível já
    tinha começado.
    """
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    dispatcher = supervisor.dispatcher
    if dispatcher.in_flight == 0:
        return
    logger.warning(
        "o laço do despachante já tinha parado; esperando %d execução(ões) em andamento",
        dispatcher.in_flight,
    )
    draining = asyncio.ensure_future(dispatcher.drain())
    try:
        done, _ = await asyncio.wait({draining}, timeout=supervisor.drain_timeout_s)
        if not done:
            logger.error(
                "%d execução(ões) ainda em andamento depois de %g s; interrompidas sem "
                "gravar o resultado (o recolhimento decide quando a concessão vencer)",
                dispatcher.in_flight,
                supervisor.drain_timeout_s,
            )
    finally:
        if not draining.done():
            # Cancelar a espera cancela as execuções: o lançador para cada executor.
            draining.cancel()
            await asyncio.gather(draining, return_exceptions=True)


class _DispatcherHealth:
    """Faz ``/saude`` responder 503 enquanto o despachante deste processo estiver parado,
    com uma etapa falhando há tempo demais ou com uma volta do laço presa."""

    def __init__(self, app: ASGIApp, supervisor: DispatcherSupervisor) -> None:
        self.app = app
        self.supervisor = supervisor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/saude" and not self.supervisor.healthy:
            response = JSONResponse(
                self.supervisor.problem,
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_artifact_store(
    settings: ServerSettings, db: Database, backends: Backends | None = None
) -> ArtifactStore:
    """Onde ficam os artefatos: a pasta local de ``RAPA_PASTA_ARTEFATOS`` ou, com
    ``RAPA_ARMAZENAMENTO=s3``, o bucket cifrado (a pasta vira só a de passagem).

    ``db`` é o banco de quem usa o store (as chaves das execuções ficam nele).
    """
    return (backends or Backends(settings.backends)).artifact_store(settings.artifacts_dir, db)


def build_artifacts(
    settings: ServerSettings, db: Database, backends: Backends | None = None
) -> PgArtifacts:
    """As evidências das execuções (REST e interface), lidas pelo store dos artefatos."""
    return PgArtifacts(db, build_artifact_store(settings, db, backends))


def build_dispatcher(
    settings: ServerSettings, db: Database, backends: Backends | None = None
) -> Dispatcher:
    """Despachante com o lançador local, sobre o banco do papel do despachante.

    Registra os artefatos de cada resultado pelo store e roda a retenção uma vez por
    hora, com os prazos de ``RAPA_RETENCAO_*``. Os segredos vêm do cofre configurado
    (``RAPA_COFRE``).
    """
    backends = backends or Backends(settings.backends)
    queue = PgRunQueue(db)
    store = build_artifact_store(settings, db, backends)
    if settings.executor == "local" and settings.video_on_failure and ffmpeg_installed() is False:
        # O worker roda sem vídeo quando ele não abre; sem este aviso, só a falta dos
        # vídeos de falha mostraria que nada está sendo filmado.
        logger.warning(
            "RAPA_VIDEO_EM_FALHAS está ligado, mas o ffmpeg do Playwright não está "
            "instalado: as execuções rodam sem vídeo (instale com 'playwright install "
            "ffmpeg', ou desligue com RAPA_VIDEO_EM_FALHAS=0)"
        )
    launcher: LocalProcessLauncher | KubernetesLauncher
    if settings.executor == "kubernetes":
        launcher = build_pod_launcher(settings, queue, db, backends)
    else:
        launcher = LocalProcessLauncher(
            queue,
            LocalLauncherSettings(
                work_dir=settings.work_dir,
                artifacts_dir=settings.artifacts_dir,
                chromium=settings.chromium,
                browser_args=settings.browser_args,
                files_dir=settings.files_dir,
                video_on_failure=settings.video_on_failure,
                egress=settings.local_egress(),
            ),
            backends.secret_source(db, settings.dev_secrets_file),
        )
    retention = Retention(
        db,
        store,
        RetentionPolicy(
            success_days=settings.retention_success_days,
            failure_days=settings.retention_failure_days,
            events_days=settings.retention_events_days,
        ),
    )
    return Dispatcher(
        queue,
        launcher,
        concurrency=settings.concurrency,
        poll_interval=settings.poll_interval_s,
        artifacts=store,
        retention=retention,
    )


def build_pod_launcher(
    settings: ServerSettings,
    queue: PgRunQueue,
    db: Database | None = None,
    backends: Backends | None = None,
) -> KubernetesLauncher:
    """O lançador de pods (``RAPA_EXECUTOR=kubernetes``) com o canal e a API do cluster.

    O canal só abre a porta na subida do serviço (:func:`create_server_app`); o
    endereço que vai para os pods leva o IP deste pod (``{ip}``) por padrão. Os segredos
    vêm do cofre configurado (``RAPA_COFRE``), lidos com o banco ``db`` (o do despachante).
    """
    backends = backends or Backends(settings.backends)
    secrets = backends.secret_source(db or queue.db, settings.dev_secrets_file)
    return KubernetesLauncher(
        queue,
        KubernetesLauncherSettings(
            work_dir=settings.work_dir,
            artifacts_dir=settings.artifacts_dir,
            pod=settings.pod_settings(),
            channel_address=channel_address(settings.channel_address, settings.channel_port),
            browser_args=settings.browser_args,
            files_dir=settings.files_dir,
            egress=settings.egress_policy(),
            video_on_failure=settings.video_on_failure,
        ),
        api=KubeApi(KubeConfig.in_cluster()),
        channel=Channel(),
        secrets=secrets,
        attempts=queue,
    )


def build_publishing(
    settings: ServerSettings, db: Database, metrics: Metrics | None = None
) -> PublishingServices:
    """Agendas, webhooks (só com ``RAPA_CHAVE_WEBHOOKS``) e lotes sobre o banco da aplicação.

    Com ``metrics``, cada entrega de webhook é contada pelo resultado.
    """
    webhooks = (
        PgWebhooks(
            db,
            WebhookKeys(settings.webhook_key),
            gate=WebhookGate(lookups_per_s=settings.webhook_lookups_per_s),
        )
        if settings.webhook_key
        else None
    )
    receiver = MeteredWebhooks(webhooks, metrics) if webhooks and metrics else webhooks
    return PublishingServices(
        schedules=PgSchedules(db, max_enabled_per_company=settings.schedules_per_company),
        webhooks=webhooks,
        receiver=receiver,
        batches=PgBatches(
            db,
            limits=SheetLimits(
                max_bytes=settings.batch_max_bytes, max_rows=settings.batch_max_rows
            ),
        ),
        batch_max_bytes=settings.batch_max_bytes,
    )


def build_scheduler(settings: ServerSettings, db: Database, dispatcher_db: Database) -> Scheduler:
    """O agendador do despachante: acha as agendas pelo banco do despachante e cria as
    execuções pelo da aplicação, fixado na empresa de cada agenda."""
    return Scheduler(db, dispatcher_db, catch_up=timedelta(seconds=settings.schedule_catch_up_s))


def _hub_client(settings: ServerSettings) -> Any:
    from rapa.llm.client import HubClient

    return HubClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_s=60.0,
        max_retries=1,
    )


def _assistant(settings: ServerSettings) -> GeneralizationAssistant | None:
    if not (settings.llm_base_url and settings.llm_model):
        return None
    capabilities = str(settings.llm_capabilities_file) if settings.llm_capabilities_file else None
    return HubAssistant(lambda: _hub_client(settings), capabilities)


def build_repairs(settings: ServerSettings, db: Database) -> PgRepairs:
    """O conserto assistido, com o hub de LLM da revisão (se houver) escolhendo o candidato."""
    assistant = None
    if settings.llm_base_url and settings.llm_model:
        capabilities = (
            str(settings.llm_capabilities_file) if settings.llm_capabilities_file else None
        )
        assistant = HubRepairAssistant(lambda: _hub_client(settings), capabilities)
    return PgRepairs(db, assistant=assistant, candidates=settings.repair_candidates)


def build_credentials(settings: ServerSettings, db: Database) -> PgCredentials:
    """As credenciais registradas e as regras do certificado A1, sobre o banco da aplicação."""
    return PgCredentials(db, warn_days=settings.a1_warn_days)


def build_sessions(
    settings: ServerSettings,
    db: Database,
    platform: PgPlatform,
    fleet: Database | None = None,
) -> tuple[RecordingSessions, LocalSessionLauncher | KubernetesSessionLauncher]:
    """Serviço das sessões de gravação com o lançador local (um processo por sessão) ou,
    com ``RAPA_SESSOES_EXECUTOR=kubernetes``, com um pod de uso único por sessão.

    ``fleet`` é o banco do papel do despachante, se este processo o tiver: com ele, as
    vagas contam as sessões e as validações de toda a plataforma, e a varredura encerra
    as sessões que nenhum processo segura (a API caiu).
    """
    launcher: LocalSessionLauncher | KubernetesSessionLauncher
    if settings.sessions_runner == "kubernetes":
        launcher = KubernetesSessionLauncher(
            KubernetesSessionSettings(
                pod=settings.pod_settings(),
                browser_args=settings.browser_args,
                egress=settings.egress_policy(),
            ),
            api=KubeApi(KubeConfig.in_cluster()),
        )
    else:
        launcher = LocalSessionLauncher(
            LocalSessionSettings(
                chromium=settings.chromium,
                browser_args=settings.browser_args,
                profile_dir=settings.sessions_dir,
                egress=settings.local_egress(),
            )
        )
    service = RecordingSessions(
        PgSessionStore(db, fleet=fleet),
        launcher,
        platform,
        limits=SessionLimits(
            per_company=settings.sessions_per_company,
            global_max=settings.sessions_max,
            idle_timeout_s=settings.session_idle_s,
            max_duration_s=settings.session_max_s,
        ),
        assistant=_assistant(settings),
    )
    return service, launcher


#: Intervalo da varredura que renova as sessões deste processo e encerra as que acabaram.
_SWEEP_INTERVAL_S = 15.0


async def _sweep_sessions(sessions: RecordingSessions, stop: asyncio.Event) -> None:
    # A primeira varredura é na subida: as sessões que uma queda deixou "abertas" (ninguém
    # mais as renova) são encerradas assim que passam da folga, e liberam as vagas.
    while not stop.is_set():
        try:
            await sessions.sweep()
        except Exception as exc:
            logger.warning("falha na varredura das sessões de gravação: %s", type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_INTERVAL_S)


async def _close_sessions(
    sessions: RecordingSessions,
    launcher: LocalSessionLauncher | KubernetesSessionLauncher,
    stop: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    stop.set()
    await asyncio.gather(task, return_exceptions=True)
    with contextlib.suppress(Exception):
        await sessions.aclose()
    await launcher.aclose()
    if isinstance(launcher, KubernetesSessionLauncher):
        await launcher.api.aclose()


async def _stop_task(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.gather(task, return_exceptions=True)


async def _start_pod_launcher(
    stack: contextlib.AsyncExitStack, launcher: KubernetesLauncher, port: int
) -> None:
    """Abre o canal do despachante e a varredura dos órfãos; fecham depois do despachante."""
    server = ChannelServer(launcher.channel, port=port)
    stack.push_async_callback(launcher.api.aclose)
    await server.start()
    stack.push_async_callback(server.close)
    stop = asyncio.Event()
    sweeper = asyncio.create_task(launcher.run_sweeper(stop), name="pods-orfaos")
    stack.push_async_callback(_stop_task, stop, sweeper)


def build_readiness_checks(
    db: Database,
    dispatcher_db: Database | None,
    supervisor: DispatcherSupervisor | None,
    extra: Iterable[ReadinessCheck] = (),
) -> list[ReadinessCheck]:
    """As checagens de ``/saude/pronto``: o banco, o do despachante e o despachante.

    ``extra`` são as do cofre e do armazenamento de objetos, quando configurados (as
    ``readiness_checks()`` dos backends do OpenBao e do S3, que já têm a forma de
    :class:`~rapa.control.health.ReadinessCheck`).
    """
    checks: list[ReadinessCheck] = [DatabaseCheck(db)]
    if dispatcher_db is not None:
        checks.append(DatabaseCheck(dispatcher_db, "banco_despachante"))
    if supervisor is not None:
        checks.append(DispatcherCheck(supervisor))
    checks.extend(extra)
    return checks


async def _stop_watch(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop.set()
    await asyncio.gather(task, return_exceptions=True)


def create_server_app(settings: ServerSettings, *, tracing: Tracing | None = None) -> FastAPI:
    """Monta a aplicação: REST e MCP, interface (opcional) e despachante (opcional).

    O ``lifespan`` da aplicação roda uma vez só: para subir de novo, monte outra.
    Ele envolve tudo, inclusive a interface: o despachante para (e espera as
    execuções em andamento), depois o gateway MCP e a interface encerram, e só
    então o verificador de tokens e os bancos são fechados, mesmo se a subida
    falhar no meio.

    As métricas (``app.state.metrics``) são montadas aqui, mas o servidor delas só
    sobe em :func:`main`. Com ``OTEL_EXPORTER_OTLP_ENDPOINT`` (ou ``tracing``, nos
    testes), os pedidos e as execuções ganham traços.
    """
    version = _version()
    metrics = Metrics(per_company=settings.metrics_per_company, version=version)
    if tracing is None and settings.otel_endpoint:
        tracing = activate_tracing(
            endpoint=settings.otel_endpoint,
            traces_endpoint=settings.otel_traces_endpoint,
            service_name=settings.otel_service_name,
            version=version,
        )
    db = Database.from_url(settings.database_url)
    dispatcher_db = (
        Database.from_url(
            settings.dispatcher_database_url,
            pool_size=dispatcher_pool_size(settings.concurrency),
        )
        if settings.dispatcher_database_url
        else None
    )
    platform = PgPlatform(db)
    resolver = PgPrincipalResolver(db)
    company = PgCompanyAdmin(db)
    verifier = _verifier(settings, db) if settings.http else None
    # O primeiro login cria o usuário (sem papéis) só com os tokens do Keycloak.
    provisioner = resolver if isinstance(verifier, TokenVerifier) else None
    sessions, session_launcher = (
        build_sessions(settings, db, platform, dispatcher_db)
        if settings.http and settings.sessions
        else (None, None)
    )
    publishing = build_publishing(settings, db, metrics)
    repairs = build_repairs(settings, db)
    credentials = build_credentials(settings, db)
    backends = Backends(settings.backends)
    artifacts = build_artifacts(settings, db, backends)
    if verifier is not None:
        app = create_http_app(
            catalog=platform,
            runs=platform,
            admin=platform,
            verifier=verifier,
            resolver=resolver,
            settings=HttpSettings(
                public_url=settings.public_url,
                authorization_servers=(settings.jwt_issuer,) if settings.jwt_issuer else (),
                allow_static_tokens=settings.dev,
                mcp_allowed_origins=settings.mcp_allowed_origins,
                live_allowed_origins=settings.live_allowed_origins,
                rate_limit_per_s=settings.rate_limit_per_s,
            ),
            sessions=sessions,
            publishing=publishing,
            repairs=repairs,
            credentials=credentials,
            company=company,
            provisioner=provisioner,
            artifacts=artifacts,
        )
    else:
        app = FastAPI(title="RaPA (despachante)", docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/saude", include_in_schema=False)
        async def health() -> dict[str, str]:
            return {"estado": "ok"}

    dispatcher = build_dispatcher(settings, dispatcher_db, backends) if dispatcher_db else None
    if isinstance(dispatcher, Dispatcher) and dispatcher_db is not None:
        if dispatcher.scheduler is None:
            # As agendas vencidas viram execuções numa etapa do mesmo laço.
            dispatcher.scheduler = build_scheduler(settings, db, dispatcher_db)
            dispatcher.schedule_interval = settings.schedule_interval_s
        if dispatcher.alerts is None:
            # Os avisos de vencimento do certificado A1 são outra etapa do mesmo laço.
            dispatcher.alerts = CredentialAlerts(dispatcher_db, warn_days=settings.a1_warn_days)
        if isinstance(dispatcher.queue, PgRunQueue) and dispatcher.queue.observer is None:
            # Cada execução concluída conta nas métricas (depois de gravada).
            dispatcher.queue.observer = metrics
        if tracing is not None and dispatcher.tracer is None:
            dispatcher.tracer = tracing.run_spans()
    supervisor = DispatcherSupervisor(dispatcher) if dispatcher else None
    if supervisor is not None:
        app.add_middleware(_DispatcherHealth, supervisor=supervisor)
    metrics.watch_dispatcher(supervisor)
    metrics.watch_sessions(sessions)
    # Antes da interface: o NiceGUI, montado em "/", pegaria o caminho.
    readiness = Readiness(build_readiness_checks(db, dispatcher_db, supervisor))
    app.add_api_route(READY_PATH, readiness.endpoint, methods=["GET"], include_in_schema=False)
    app.state.dispatcher = dispatcher
    app.state.dispatcher_supervisor = supervisor
    app.state.sessions = sessions
    app.state.publishing = publishing
    app.state.repairs = repairs
    app.state.credentials = credentials
    app.state.artifacts = artifacts
    app.state.backends = backends
    # As checagens de prontidão do cofre e do S3 (as que estiverem configuradas).
    app.state.readiness_checks = backends.readiness_checks()
    app.state.metrics = metrics
    app.state.readiness = readiness
    app.state.tracing = tracing
    if settings.http and settings.ui:
        # Antes de envolver o lifespan: o NiceGUI (ui.run_with) envolve o que encontrar
        # com a subida e o encerramento dele, e esse conjunto fica dentro do nosso.
        _mount_ui(
            app,
            settings,
            platform,
            Authenticator(verifier, resolver, provisioner=provisioner),  # type: ignore[arg-type]
            sessions,
            publishing,
            repairs,
            credentials,
            company,
            artifacts,
        )
    # Por fora de tudo o que a aplicação já tem (inclusive os 413 e 503 das proteções).
    instrument_http(app, metrics)
    if tracing is not None:
        tracing.instrument_app(app)
    inner = app.router.lifespan_context
    started = False

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[Any]:
        # A interface, o gateway MCP e o despachante só sobem uma vez por aplicação.
        # Uma segunda entrada (outro TestClient na mesma aplicação, por exemplo) é
        # recusada aqui, por fora de tudo, antes de abrir qualquer coisa (nem a subida
        # do NiceGUI roda de novo) ou de mexer no que já está rodando.
        nonlocal started
        if started:
            raise RuntimeError(
                "o serviço desta aplicação já foi iniciado uma vez; "
                "monte outra com create_server_app"
            )
        started = True
        async with contextlib.AsyncExitStack() as stack:
            # Fechados por último e sempre, mesmo se a subida falhar no meio.
            stack.push_async_callback(db.dispose)
            stack.push_async_callback(backends.aclose)
            if dispatcher_db is not None:
                stack.push_async_callback(dispatcher_db.dispose)
            if isinstance(verifier, TokenVerifier):
                stack.push_async_callback(verifier.aclose)
            if tracing is not None:
                # O que ficou na fila dos traços sai antes de o processo parar.
                stack.push_async_callback(asyncio.to_thread, tracing.flush)
            stack.push_async_callback(readiness.aclose)
            # A interface (se houver) e o gateway MCP: encerram antes de fechar os bancos.
            state = await stack.enter_async_context(inner(application))
            pod_launcher = getattr(dispatcher, "launch", None)
            if isinstance(pod_launcher, KubernetesLauncher):
                # Antes do despachante: o canal fica aberto até as tentativas terminarem.
                await _start_pod_launcher(stack, pod_launcher, settings.channel_port)
            if dispatcher_db is not None and settings.metrics_port:
                # A fila e os avisos do A1, lidos do banco do despachante a cada 30 s.
                watch_stop = asyncio.Event()
                watch = asyncio.create_task(
                    watch_platform(
                        metrics,
                        dispatcher_db,
                        watch_stop,
                        warn_days=settings.a1_warn_days,
                        clock=now_utc,
                    ),
                    name="metricas-da-fila",
                )
                stack.push_async_callback(_stop_watch, watch_stop, watch)
            if sessions is not None and session_launcher is not None:
                # Os navegadores das sessões fecham antes dos bancos (a situação é gravada).
                sweep_stop = asyncio.Event()
                sweep = asyncio.create_task(
                    _sweep_sessions(sessions, sweep_stop), name="sessoes-de-gravacao"
                )
                stack.push_async_callback(
                    _close_sessions, sessions, session_launcher, sweep_stop, sweep
                )
                if isinstance(session_launcher, KubernetesSessionLauncher):
                    pods_stop = asyncio.Event()
                    pods = asyncio.create_task(
                        session_launcher.run_sweeper(pods_stop), name="pods-de-sessao"
                    )
                    stack.push_async_callback(_stop_task, pods_stop, pods)
            if supervisor is not None:
                stop = asyncio.Event()
                stack.push_async_callback(
                    _stop_dispatcher, supervisor, stop, supervisor.start(stop)
                )
            yield state

    # Por último: nada instalado depois fica por fora da recusa da segunda entrada.
    app.router.lifespan_context = lifespan
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.server",
        description="Sobe o RaPA (REST, MCP, interface e despachante) num processo só.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        settings = ServerSettings.from_env()
        if settings.metrics_port and settings.metrics_port == args.port:
            raise ValueError("RAPA_METRICAS_PORTA não pode ser a porta do serviço")
    except ValueError as exc:
        print(f"rapa.server: configuração inválida: {exc}", file=sys.stderr)
        return 2
    configure_logging(json_logs=settings.log_json)
    import uvicorn

    version = _version()
    glitchtip: GlitchTip | None = None
    tracing: Tracing | None = None
    try:
        # Primeiro o GlitchTip e o rastreio: o que ligar é o que foi pedido, ou nada sobe.
        if settings.glitchtip_dsn:
            glitchtip = configure_glitchtip(settings.glitchtip_dsn, release=f"rapa@{version}")
        if settings.otel_endpoint:
            tracing = activate_tracing(
                endpoint=settings.otel_endpoint,
                traces_endpoint=settings.otel_traces_endpoint,
                service_name=settings.otel_service_name,
                version=version,
            )
    except RuntimeError as exc:  # o extra observability não está instalado
        print(f"rapa.server: {exc}", file=sys.stderr)
        if glitchtip is not None:
            glitchtip.close()
        return 2
    metrics_server: MetricsServer | None = None
    try:
        app = create_server_app(settings, tracing=tracing)
        if settings.metrics_port:
            # Antes do serviço: sem a porta das métricas, o processo não sobe. No mesmo
            # endereço do serviço: com o padrão (127.0.0.1), nada fica aberto na rede.
            try:
                metrics_server = serve_metrics(
                    app.state.metrics, settings.metrics_port, host=args.host
                )
            except OSError as exc:
                logger.error(
                    "não foi possível abrir a porta %d das métricas: %s",
                    settings.metrics_port,
                    type(exc).__name__,
                )
                return 2
        with contextlib.suppress(KeyboardInterrupt):
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level="info",
                access_log=False,
                log_config=uvicorn_log_config(json_logs=settings.log_json),
            )
    finally:
        if metrics_server is not None:
            metrics_server.stop()
        if tracing is not None:
            tracing.shutdown()
        if glitchtip is not None:
            glitchtip.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
