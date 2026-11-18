"""Rótulos em pt-BR usados nas telas e nas descrições dos robôs."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rapa.core.contracts import RobotStatus, RunState, RunTrigger, VersionStatus
from rapa.core.robot import CredentialKind, Effect, FieldType

__all__ = [
    "CREDENTIAL_KIND_LABELS",
    "EFFECT_COLORS",
    "EFFECT_LABELS",
    "FIELD_TYPE_LABELS",
    "ROBOT_STATUS_LABELS",
    "RUN_STATE_COLORS",
    "RUN_STATE_LABELS",
    "SECRET_FIELD_LABELS",
    "TRIGGER_LABELS",
    "VERSION_STATUS_LABELS",
    "format_datetime",
]

EFFECT_LABELS: dict[Effect, str] = {
    Effect.READ: "Leitura",
    Effect.WRITE: "Grava dados",
    Effect.IRREVERSIBLE: "Irreversível",
}

#: Cores do Quasar para os selos de efeito.
EFFECT_COLORS: dict[Effect, str] = {
    Effect.READ: "grey-7",
    Effect.WRITE: "orange-9",
    Effect.IRREVERSIBLE: "red-9",
}

RUN_STATE_LABELS: dict[RunState, str] = {
    RunState.QUEUED: "Na fila",
    RunState.RUNNING: "Em execução",
    RunState.AWAITING_HUMAN: "Aguardando uma pessoa",
    RunState.SUCCEEDED: "Concluída",
    RunState.FAILED: "Falhou",
    RunState.CANCELLED: "Cancelada",
    RunState.INDETERMINATE: "Indeterminada",
}

RUN_STATE_COLORS: dict[RunState, str] = {
    RunState.QUEUED: "grey-7",
    RunState.RUNNING: "blue-8",
    RunState.AWAITING_HUMAN: "orange-9",
    RunState.SUCCEEDED: "green-8",
    RunState.FAILED: "red-9",
    RunState.CANCELLED: "grey-8",
    RunState.INDETERMINATE: "purple-8",
}

ROBOT_STATUS_LABELS: dict[RobotStatus, str] = {
    RobotStatus.DRAFT: "Rascunho",
    RobotStatus.PUBLISHED: "Publicado",
    RobotStatus.DISABLED: "Desativado",
    RobotStatus.NEEDS_REPAIR: "Precisa de conserto",
}

VERSION_STATUS_LABELS: dict[VersionStatus, str] = {
    VersionStatus.DRAFT: "Rascunho",
    VersionStatus.VALIDATED: "Validada",
    VersionStatus.PUBLISHED: "Publicada",
    VersionStatus.REJECTED: "Rejeitada",
    VersionStatus.SUPERSEDED: "Substituída",
}

TRIGGER_LABELS: dict[RunTrigger, str] = {
    RunTrigger.MCP: "MCP",
    RunTrigger.REST: "API REST",
    RunTrigger.SCHEDULE: "Agenda",
    RunTrigger.WEBHOOK: "Webhook",
    RunTrigger.BATCH: "Lote",
    RunTrigger.MANUAL: "Interface",
    RunTrigger.VALIDATION: "Validação",
}

FIELD_TYPE_LABELS: dict[FieldType, str] = {
    FieldType.STRING: "Texto",
    FieldType.INTEGER: "Número inteiro",
    FieldType.NUMBER: "Número",
    FieldType.BOOLEAN: "Sim ou não",
    FieldType.DATE: "Data",
    FieldType.CPF: "CPF",
    FieldType.CNPJ: "CNPJ",
    FieldType.MONEY: "Valor em reais",
    FieldType.FILE: "Arquivo",
    FieldType.LIST: "Lista",
}

CREDENTIAL_KIND_LABELS: dict[CredentialKind, str] = {
    CredentialKind.PASSWORD: "Usuário e senha",
    CredentialKind.TOTP: "Código de verificação (TOTP)",
    CredentialKind.PFX_A1: "Certificado digital A1",
}

#: Nome, em pt-BR, de cada campo de uma credencial do cofre.
SECRET_FIELD_LABELS: dict[str, str] = {
    "username": "usuário",
    "password": "senha",
    "totp": "código de verificação",
}

try:
    _LOCAL_TZ: ZoneInfo | None = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:  # pragma: no cover - depende da base de fusos do sistema
    _LOCAL_TZ = None


def format_datetime(value: datetime | None) -> str:
    """Data e hora no horário de Brasília, no formato dd/mm/aaaa hh:mm."""
    if value is None:
        return "—"
    if _LOCAL_TZ is not None and value.tzinfo is not None:
        value = value.astimezone(_LOCAL_TZ)
    return value.strftime("%d/%m/%Y %H:%M")
