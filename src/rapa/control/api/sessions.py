"""Rotas das sessões de gravação: ``/v1/sessoes`` (REST) e a visão ao vivo (websocket).

Os pedidos REST usam a mesma autenticação da API (``Authorization: Bearer``) e o
mesmo limite por empresa. O websocket ``/v1/sessoes/{id}/ao-vivo`` não recebe o
token de acesso: só um bilhete de uso único (``?bilhete=``), pedido antes pelo
REST pela dona da sessão. Um bilhete inválido, vencido ou já usado é recusado
antes de abrir o websocket (HTTP 403). A sessão é da dona: só uma janela por vez
assiste, e uma segunda janela substitui a primeira
(:mod:`rapa.control.sessions.relay`).

Sem o serviço de gravação configurado, todas as rotas respondem 503.
"""

import contextlib
import logging
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState

from rapa.control.sessions.service import RecordingSessions
from rapa.core.contracts import (
    MAX_SESSIONS_LISTED,
    ContractError,
    DraftChoices,
    GeneralizationProposals,
    NotFound,
    Principal,
    RecordingSessionInfo,
    RobotVersionInfo,
    ServiceUnavailable,
    ViewerTicket,
)
from rapa.session.protocol import dump_event

__all__ = ["CreateSessionBody", "add_session_routes"]

logger = logging.getLogger(__name__)

#: Código de fechamento do websocket quando a sessão acaba por um erro de regra.
_POLICY_CLOSE = 1008


class CreateSessionBody(BaseModel):
    """Corpo de ``POST /v1/sessoes``."""

    model_config = ConfigDict(extra="forbid")

    area_id: UUID
    endereco: str = Field(min_length=1, max_length=2000, description="Endereço inicial")
    titulo: str = Field(default="", max_length=120)


class _SocketViewer:
    """A janela de quem assiste, sobre o websocket do Starlette."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def send_text(self, data: str) -> None:
        await self.websocket.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.websocket.send_bytes(data)

    async def receive_text(self) -> str | None:
        try:
            message = await self.websocket.receive()
        except WebSocketDisconnect, RuntimeError:
            return None
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        return text if isinstance(text, str) else ""

    async def close(self, code: int, reason: str) -> None:
        if self.websocket.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await self.websocket.close(code, reason)


def add_session_routes(
    router: APIRouter,
    sessions: RecordingSessions | None,
    caller: Callable[..., Any],
) -> None:
    """Acrescenta as rotas das sessões ao roteador de ``/v1``."""
    caller_dep = Depends(caller)

    def service() -> RecordingSessions:
        if sessions is None:
            raise ServiceUnavailable(
                "a gravação de demonstrações não está disponível neste servidor"
            )
        return sessions

    @router.post("/sessoes", status_code=201, response_model=RecordingSessionInfo)
    async def create_session(
        body: CreateSessionBody, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> RecordingSessionInfo:
        info = await service().create_session(principal, body.area_id, body.endereco, body.titulo)
        response.headers["Location"] = f"/v1/sessoes/{info.session_id}"
        return info

    @router.get("/sessoes", response_model=list[RecordingSessionInfo])
    async def list_sessions(
        principal: Annotated[Principal, caller_dep],
        limite: Annotated[int, Query(ge=1, le=MAX_SESSIONS_LISTED)] = 20,
    ) -> list[RecordingSessionInfo]:
        return await service().list_sessions(principal, limite)

    @router.get("/sessoes/{session_id}", response_model=RecordingSessionInfo)
    async def get_session(
        session_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> RecordingSessionInfo:
        info = await service().get_session(principal, session_id)
        if info is None:
            raise NotFound("sessão de gravação não encontrada")
        return info

    @router.post("/sessoes/{session_id}/bilhete", response_model=ViewerTicket)
    async def issue_ticket(
        session_id: UUID, response: Response, principal: Annotated[Principal, caller_dep]
    ) -> ViewerTicket:
        response.headers["Cache-Control"] = "no-store"
        return await service().issue_ticket(principal, session_id)

    @router.post("/sessoes/{session_id}/parar", response_model=RecordingSessionInfo)
    async def stop_session(
        session_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> RecordingSessionInfo:
        return await service().stop_session(principal, session_id)

    @router.get("/sessoes/{session_id}/propostas", response_model=GeneralizationProposals)
    async def get_proposals(
        session_id: UUID, principal: Annotated[Principal, caller_dep]
    ) -> GeneralizationProposals:
        return await service().get_proposals(principal, session_id)

    @router.post("/sessoes/{session_id}/rascunho", status_code=201, response_model=RobotVersionInfo)
    async def create_draft(
        session_id: UUID,
        body: DraftChoices,
        response: Response,
        principal: Annotated[Principal, caller_dep],
    ) -> RobotVersionInfo:
        info = await service().create_draft(principal, session_id, body)
        response.headers["Location"] = f"/v1/robos/{info.robot_id}/versoes/{info.version_no}"
        return info

    @router.websocket("/sessoes/{session_id}/ao-vivo")
    async def live(websocket: WebSocket, session_id: UUID, bilhete: str = "") -> None:
        if sessions is None:
            await websocket.close(code=_POLICY_CLOSE)
            return
        try:
            greeting = await sessions.admit_viewer(session_id, bilhete)
        except ContractError:
            # Antes de aceitar: o navegador recebe HTTP 403, sem dizer por quê.
            await websocket.close(code=_POLICY_CLOSE)
            return
        await websocket.accept()
        viewer = _SocketViewer(websocket)
        try:
            await sessions.relay_viewer(session_id, viewer, greeting)
        except ContractError as exc:
            with contextlib.suppress(Exception):
                await viewer.send_text(dump_event("fim", motivo="erro", mensagem=exc.message))
        except Exception as exc:
            logger.error("falha na visão ao vivo: %s", type(exc).__name__)
        finally:
            await viewer.close(1000, "fim")
