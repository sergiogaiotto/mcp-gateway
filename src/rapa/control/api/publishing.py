"""Rotas de publicação: agendas (``/v1/agendas``), webhooks e lotes por planilha (``/v1/lotes``).

As rotas de administração usam a autenticação e o limite por empresa de toda a
API. A entrega de um webhook (``POST /v1/webhooks/{id}``) é a exceção: chega sem
token e é autenticada só pela assinatura (:mod:`rapa.control.services.webhooks`),
com o limite por webhook. Ela responde 202 com a execução (a mesma para a mesma
entrega), 401 para qualquer problema de assinatura (sem dizer qual), 409 para
webhook desligado, robô não publicado ou entrega repetida com outro corpo, 423 para
empresa congelada (só depois de conferida a assinatura), 422
para entradas inválidas e 429 acima do limite do webhook, para um endereço de
origem com recusas demais ou com leituras demais no processo (os dois últimos
antes de qualquer consulta ao banco; veja
:class:`rapa.control.services.webhooks.WebhookGate`).

A chave de um webhook só aparece na resposta da criação e da troca de chave, com
``Cache-Control: no-store``. O resultado de um lote sai como arquivo (CSV ou xlsx).

Sem o serviço configurado, as rotas dele respondem 503.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field

from rapa.control.services.webhooks import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from rapa.core.contracts import (
    MAX_BATCHES_LISTED,
    MAX_OCCURRENCES_LISTED,
    BatchFormat,
    BatchInfo,
    BatchPreview,
    BatchRow,
    BatchService,
    InvalidInput,
    NotFound,
    Principal,
    ScheduleChanges,
    ScheduleInfo,
    ScheduleOccurrence,
    ScheduleService,
    ScheduleSpec,
    ServiceUnavailable,
    SignatureInvalid,
    WebhookChanges,
    WebhookDelivery,
    WebhookInfo,
    WebhookReceiver,
    WebhookSecret,
    WebhookService,
    WebhookSpec,
)
from rapa.core.cron import DEFAULT_TIMEZONE

__all__ = [
    "DEFAULT_BATCH_MAX_BYTES",
    "CreateScheduleBody",
    "CreateWebhookBody",
    "PublishingServices",
    "UpdateScheduleBody",
    "UpdateWebhookBody",
    "add_publishing_routes",
]

#: Tamanho máximo de uma planilha de lote (``RAPA_LOTE_MAX_BYTES``).
DEFAULT_BATCH_MAX_BYTES = 5 * 1024 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
_REJECTED = "assinatura inválida, horário fora da tolerância ou webhook desconhecido"


@dataclass(frozen=True)
class PublishingServices:
    """Os serviços de publicação; o que ficar ``None`` responde 503."""

    schedules: ScheduleService | None = None
    webhooks: WebhookService | None = None
    receiver: WebhookReceiver | None = None
    batches: BatchService | None = None
    #: Tamanho máximo da planilha de um lote, em bytes.
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES


class CreateScheduleBody(BaseModel):
    """Corpo de ``POST /v1/agendas``."""

    model_config = ConfigDict(extra="forbid")

    robo_id: UUID
    titulo: str = Field(min_length=1, max_length=120)
    cron: str = Field(min_length=1, max_length=200, description="Cinco campos, como no cron")
    fuso: str = Field(default=DEFAULT_TIMEZONE, min_length=1, max_length=64)
    entradas: dict[str, Any] = Field(default_factory=dict)
    ativa: bool = True


class UpdateScheduleBody(BaseModel):
    """Corpo de ``PATCH /v1/agendas/{id}``: só o que muda."""

    model_config = ConfigDict(extra="forbid")

    titulo: str | None = Field(default=None, min_length=1, max_length=120)
    cron: str | None = Field(default=None, min_length=1, max_length=200)
    fuso: str | None = Field(default=None, min_length=1, max_length=64)
    entradas: dict[str, Any] | None = None
    ativa: bool | None = None


class CreateWebhookBody(BaseModel):
    """Corpo de ``POST /v1/robos/{id}/webhooks``."""

    model_config = ConfigDict(extra="forbid")

    titulo: str = Field(min_length=1, max_length=120)
    por_minuto: int = Field(default=60, ge=1, le=6000)
    ativo: bool = True


class UpdateWebhookBody(BaseModel):
    """Corpo de ``PATCH /v1/robos/{id}/webhooks/{webhook}``."""

    model_config = ConfigDict(extra="forbid")

    titulo: str | None = Field(default=None, min_length=1, max_length=120)
    por_minuto: int | None = Field(default=None, ge=1, le=6000)
    ativo: bool | None = None


def _columns(raw: str | None) -> dict[str, str] | None:
    if raw is None or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise InvalidInput("mapa de colunas inválido", ["colunas: JSON inválido"]) from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and len(k) <= 200 and len(v) <= 64
        for k, v in data.items()
    ):
        raise InvalidInput(
            "mapa de colunas inválido",
            ['colunas: esperado um objeto {"título da coluna": "nome da entrada"}'],
        )
    return {str(k): str(v) for k, v in data.items()}


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise InvalidInput(f"o arquivo passa do limite de {limit // 1024} KiB")
    return data


def add_publishing_routes(
    router: APIRouter,
    services: PublishingServices,
    caller: Callable[..., Any],
    check_idempotency_key: Callable[[str | None], str | None],
) -> None:
    """Acrescenta as rotas de agendas, webhooks e lotes ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def schedules() -> ScheduleService:
        if services.schedules is None:
            raise ServiceUnavailable("as agendas não estão disponíveis neste servidor")
        return services.schedules

    def webhooks() -> WebhookService:
        if services.webhooks is None:
            raise ServiceUnavailable(
                "os webhooks não estão disponíveis neste servidor (RAPA_CHAVE_WEBHOOKS)"
            )
        return services.webhooks

    def batches() -> BatchService:
        if services.batches is None:
            raise ServiceUnavailable("os lotes por planilha não estão disponíveis neste servidor")
        return services.batches

    # -- agendas ------------------------------------------------------------------

    @router.get("/agendas", response_model=list[ScheduleInfo])
    async def list_schedules(principal: Annotated[Principal, caller_dep]) -> list[ScheduleInfo]:
        return await schedules().list_schedules(principal)

    @router.post("/agendas", status_code=201, response_model=ScheduleInfo)
    async def create_schedule(
        body: CreateScheduleBody, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> ScheduleInfo:
        info = await schedules().create_schedule(
            principal,
            ScheduleSpec(
                robot_id=body.robo_id,
                title=body.titulo,
                cron=body.cron,
                timezone=body.fuso,
                inputs=body.entradas,
                enabled=body.ativa,
            ),
        )
        response.headers["Location"] = f"/v1/agendas/{info.schedule_id}"
        return info

    @router.get("/agendas/{schedule_id}", response_model=ScheduleInfo)
    async def get_schedule(
        schedule_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> ScheduleInfo:
        info = await schedules().get_schedule(principal, schedule_id)
        if info is None:
            raise NotFound("agenda não encontrada")
        return info

    @router.patch("/agendas/{schedule_id}", response_model=ScheduleInfo)
    async def update_schedule(
        schedule_id: UUID, body: UpdateScheduleBody, principal: Annotated[Principal, caller_dep]
    ) -> ScheduleInfo:
        return await schedules().update_schedule(
            principal,
            schedule_id,
            ScheduleChanges(
                title=body.titulo,
                cron=body.cron,
                timezone=body.fuso,
                inputs=body.entradas,
                enabled=body.ativa,
            ),
        )

    @router.delete("/agendas/{schedule_id}", status_code=204)
    async def delete_schedule(
        schedule_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> Response:
        await schedules().delete_schedule(principal, schedule_id)
        return Response(status_code=204)

    @router.get("/agendas/{schedule_id}/ocorrencias", response_model=list[ScheduleOccurrence])
    async def list_occurrences(
        schedule_id: UUID,
        principal: Annotated[Principal, caller_dep],
        limite: Annotated[int, Query(ge=1, le=MAX_OCCURRENCES_LISTED)] = 50,
    ) -> list[ScheduleOccurrence]:
        return await schedules().list_occurrences(principal, schedule_id, limite)

    # -- webhooks (administração) ---------------------------------------------------

    @router.get("/robos/{robot_id}/webhooks", response_model=list[WebhookInfo])
    async def list_webhooks(
        robot_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> list[WebhookInfo]:
        return await webhooks().list_webhooks(principal, robot_id)

    @router.post("/robos/{robot_id}/webhooks", status_code=201, response_model=WebhookSecret)
    async def create_webhook(
        robot_id: UUID,
        body: CreateWebhookBody,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> WebhookSecret:
        created = await webhooks().create_webhook(
            principal,
            robot_id,
            WebhookSpec(title=body.titulo, rate_per_minute=body.por_minuto, enabled=body.ativo),
        )
        response.headers.update(_NO_STORE)
        response.headers["Location"] = f"/v1/robos/{robot_id}/webhooks/{created.webhook.webhook_id}"
        return created

    @router.get("/robos/{robot_id}/webhooks/{webhook_id}", response_model=WebhookInfo)
    async def get_webhook(
        robot_id: UUID, webhook_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> WebhookInfo:
        info = await webhooks().get_webhook(principal, robot_id, webhook_id)
        if info is None:
            raise NotFound("webhook não encontrado")
        return info

    @router.patch("/robos/{robot_id}/webhooks/{webhook_id}", response_model=WebhookInfo)
    async def update_webhook(
        robot_id: UUID,
        webhook_id: UUID,
        body: UpdateWebhookBody,
        principal: Annotated[Principal, caller_dep],
    ) -> WebhookInfo:
        return await webhooks().update_webhook(
            principal,
            robot_id,
            webhook_id,
            WebhookChanges(title=body.titulo, rate_per_minute=body.por_minuto, enabled=body.ativo),
        )

    @router.post(
        "/robos/{robot_id}/webhooks/{webhook_id}/trocar-chave", response_model=WebhookSecret
    )
    async def rotate_webhook(
        robot_id: UUID,
        webhook_id: UUID,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> WebhookSecret:
        response.headers.update(_NO_STORE)
        return await webhooks().rotate_secret(principal, robot_id, webhook_id)

    @router.delete("/robos/{robot_id}/webhooks/{webhook_id}", status_code=204)
    async def delete_webhook(
        robot_id: UUID, webhook_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> Response:
        await webhooks().delete_webhook(principal, robot_id, webhook_id)
        return Response(status_code=204)

    # -- webhooks (entrega, sem token) --------------------------------------------------

    @router.post(
        "/webhooks/{webhook_id}",
        status_code=202,
        response_model=WebhookDelivery,
        responses={202: {"headers": {"Location": {"schema": {"type": "string"}}}}},
    )
    async def deliver(
        webhook_id: str,
        request: Request,
        response: Response,
        timestamp: Annotated[str | None, Header(alias=TIMESTAMP_HEADER)] = None,
        delivery_id: Annotated[str | None, Header(alias=DELIVERY_HEADER)] = None,
        signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
    ) -> WebhookDelivery:
        if services.receiver is None:
            raise ServiceUnavailable(
                "os webhooks não estão disponíveis neste servidor (RAPA_CHAVE_WEBHOOKS)"
            )
        try:
            parsed = UUID(webhook_id)
        except ValueError:
            raise SignatureInvalid(_REJECTED) from None
        delivery = await services.receiver.deliver(
            parsed,
            timestamp=timestamp,
            delivery_id=delivery_id,
            signature=signature,
            body=await request.body(),
            client=request.client.host if request.client else None,
        )
        response.headers["Location"] = f"/v1/execucoes/{delivery.run_id}"
        return delivery

    # -- lotes ------------------------------------------------------------------------

    @router.post("/lotes/previa", response_model=BatchPreview)
    async def preview_batch(
        principal: Annotated[Principal, caller_dep],
        robo_id: Annotated[UUID, Form()],
        arquivo: Annotated[UploadFile, File()],
        colunas: Annotated[str | None, Form(max_length=20_000)] = None,
    ) -> BatchPreview:
        service = batches()
        data = await _read_upload(arquivo, services.batch_max_bytes)
        return await service.preview_batch(
            principal, robo_id, arquivo.filename or "planilha", data, _columns(colunas)
        )

    @router.post("/lotes", status_code=201, response_model=BatchInfo)
    async def create_batch(
        response: Response,
        principal: Annotated[Principal, caller_dep],
        robo_id: Annotated[UUID, Form()],
        arquivo: Annotated[UploadFile, File()],
        colunas: Annotated[str | None, Form(max_length=20_000)] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> BatchInfo:
        service = batches()
        key = check_idempotency_key(idempotency_key)
        data = await _read_upload(arquivo, services.batch_max_bytes)
        info = await service.create_batch(
            principal, robo_id, arquivo.filename or "planilha", data, _columns(colunas), key
        )
        response.headers["Location"] = f"/v1/lotes/{info.batch_id}"
        return info

    @router.get("/lotes", response_model=list[BatchInfo])
    async def list_batches(
        principal: Annotated[Principal, caller_dep],
        limite: Annotated[int, Query(ge=1, le=MAX_BATCHES_LISTED)] = 50,
    ) -> list[BatchInfo]:
        return await batches().list_batches(principal, limite)

    @router.get("/lotes/{batch_id}", response_model=BatchInfo)
    async def get_batch(batch_id: UUID, principal: Annotated[Principal, caller_dep]) -> BatchInfo:
        info = await batches().get_batch(principal, batch_id)
        if info is None:
            raise NotFound("lote não encontrado")
        return info

    @router.get("/lotes/{batch_id}/linhas", response_model=list[BatchRow])
    async def list_batch_rows(
        batch_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> list[BatchRow]:
        return await batches().list_batch_rows(principal, batch_id)

    @router.get("/lotes/{batch_id}/resultado")
    async def batch_results(
        batch_id: UUID,
        principal: Annotated[Principal, caller_dep],
        formato: Annotated[BatchFormat, Query()] = BatchFormat.CSV,
    ) -> Response:
        file = await batches().batch_results(principal, batch_id, formato)
        return Response(
            content=file.content,
            media_type=file.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{file.file_name}"',
                **_NO_STORE,
            },
        )
