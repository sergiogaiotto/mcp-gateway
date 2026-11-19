"""Definição das ferramentas MCP: uma por robô publicado e três ferramentas fixas."""

import json
from typing import Any

import mcp_types as types

from rapa.control.api.routes import MAX_IDEMPOTENCY_KEY
from rapa.core.contracts import RobotSummary, RunState, RunStatus
from rapa.core.robot import SLUG_PATTERN, Effect

__all__ = [
    "BUSCAR_ROBO",
    "CONSULTAR_EXECUCAO",
    "EXECUTAR_ROBO",
    "IDEMPOTENCY_META_KEY",
    "META_TOOLS",
    "RESERVED_NAMES",
    "RUN_STATUS_SCHEMA",
    "SEARCH_RESULT_SCHEMA",
    "describe_run",
    "robot_tool",
    "search_item",
]

BUSCAR_ROBO = "buscar_robo"
EXECUTAR_ROBO = "executar_robo"
CONSULTAR_EXECUCAO = "consultar_execucao"
#: Nomes das ferramentas fixas; um robô com um destes nomes não aparece no MCP.
RESERVED_NAMES = frozenset({BUSCAR_ROBO, EXECUTAR_ROBO, CONSULTAR_EXECUCAO})
#: Chave de ``_meta`` com a chave de idempotência nas ferramentas dos robôs.
IDEMPOTENCY_META_KEY = "rapa/chave_idempotencia"

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_MAX_TEXT = 4000

RUN_STATUS_SCHEMA: dict[str, Any] = RunStatus.model_json_schema()

_SEARCH_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "effect": {"type": "string", "enum": [e.value for e in Effect]},
        "version_no": {"type": "integer"},
        "input_schema": {"type": "object"},
    },
    "required": ["slug", "title", "description", "effect", "version_no", "input_schema"],
}

SEARCH_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"robos": {"type": "array", "items": _SEARCH_ITEM_SCHEMA}},
    "required": ["robos"],
}

_BUSCAR_INPUT: dict[str, Any] = {
    "$schema": _DIALECT,
    "type": "object",
    "properties": {
        "consulta": {
            "type": "string",
            "maxLength": 200,
            "title": "Consulta",
            "description": "Palavras que descrevem a tarefa, por exemplo 'emitir protocolo'.",
        },
        "limite": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
            "title": "Limite",
            "description": "Quantos robôs devolver, no máximo.",
        },
    },
    "required": ["consulta"],
    "additionalProperties": False,
}

_EXECUTAR_INPUT: dict[str, Any] = {
    "$schema": _DIALECT,
    "type": "object",
    "properties": {
        "robo": {
            "type": "string",
            "pattern": SLUG_PATTERN,
            "title": "Robô",
            "description": "Nome técnico do robô (o mesmo nome da ferramenta dele).",
        },
        "entradas": {
            "type": "object",
            "title": "Entradas",
            "description": "Entradas do robô, conforme o esquema dele (veja buscar_robo).",
        },
        "chave_idempotencia": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_IDEMPOTENCY_KEY,
            "pattern": r"^[\x21-\x7e]+$",
            "title": "Chave de idempotência",
            "description": "Repetir a chave devolve a mesma execução em vez de criar outra.",
        },
    },
    "required": ["robo"],
    "additionalProperties": False,
}

_CONSULTAR_INPUT: dict[str, Any] = {
    "$schema": _DIALECT,
    "type": "object",
    "properties": {
        "execucao_id": {
            "type": "string",
            "format": "uuid",
            "title": "Execução",
            "description": "Identificador devolvido ao iniciar a execução.",
        }
    },
    "required": ["execucao_id"],
    "additionalProperties": False,
}

META_TOOLS: tuple[types.Tool, ...] = (
    types.Tool(
        name=BUSCAR_ROBO,
        title="Buscar robô",
        description=(
            "Procura, entre os robôs publicados para a sua empresa, os que fazem a tarefa "
            "descrita. Devolve o nome, a descrição e o esquema das entradas de cada um."
        ),
        input_schema=_BUSCAR_INPUT,
        output_schema=SEARCH_RESULT_SCHEMA,
        annotations=types.ToolAnnotations(
            title="Buscar robô",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    ),
    types.Tool(
        name=EXECUTAR_ROBO,
        title="Executar robô",
        description=(
            "Inicia a execução de um robô pelo nome, com as entradas informadas. A execução é "
            "assíncrona: a resposta traz o identificador e o estado; acompanhe com "
            "consultar_execucao. Alguns robôs gravam dados ou fazem ações irreversíveis."
        ),
        input_schema=_EXECUTAR_INPUT,
        output_schema=RUN_STATUS_SCHEMA,
        annotations=types.ToolAnnotations(
            title="Executar robô",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    ),
    types.Tool(
        name=CONSULTAR_EXECUCAO,
        title="Consultar execução",
        description="Mostra o estado atual de uma execução e, quando terminar, as saídas dela.",
        input_schema=_CONSULTAR_INPUT,
        output_schema=RUN_STATUS_SCHEMA,
        annotations=types.ToolAnnotations(
            title="Consultar execução",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    ),
)

META_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    tool.name: tool.input_schema for tool in META_TOOLS
}

_EFFECT_NOTE = {
    Effect.READ: "Só lê dados no sistema de destino.",
    Effect.WRITE: "Grava dados no sistema de destino.",
    Effect.IRREVERSIBLE: "Atenção: faz uma ação irreversível no sistema de destino.",
}

_STATE_TEXT = {
    RunState.QUEUED: "na fila",
    RunState.RUNNING: "em andamento",
    RunState.AWAITING_HUMAN: "aguardando uma pessoa",
    RunState.SUCCEEDED: "concluída com sucesso",
    RunState.FAILED: "falhou",
    RunState.CANCELLED: "cancelada",
    RunState.INDETERMINATE: "indeterminada; uma pessoa precisa decidir o que aconteceu",
}


def robot_tool(robot: RobotSummary) -> types.Tool:
    """A ferramenta MCP de um robô publicado."""
    description = (
        f"{robot.description}\n\n{_EFFECT_NOTE[robot.effect]} A execução é assíncrona: a "
        "resposta traz o identificador e o estado; acompanhe com consultar_execucao."
    )
    return types.Tool(
        name=robot.slug,
        title=robot.title,
        description=description,
        input_schema=robot.input_schema,
        output_schema=RUN_STATUS_SCHEMA,
        annotations=types.ToolAnnotations(
            title=robot.title,
            read_only_hint=robot.effect is Effect.READ,
            # Pela especificação do MCP, destructiveHint=false promete só acréscimos; um
            # robô que grava pode sobrescrever dados, então todo robô que não só lê é
            # destrutivo. O caso irreversível fica na descrição e no aviso confirmado.
            destructive_hint=robot.effect is not Effect.READ,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )


def search_item(robot: RobotSummary) -> dict[str, Any]:
    return {
        "slug": robot.slug,
        "title": robot.title,
        "description": robot.description,
        "effect": robot.effect.value,
        "version_no": robot.version_no,
        "input_schema": robot.input_schema,
    }


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def describe_run(status: RunStatus, *, started: bool = False) -> str:
    """Resumo curto em pt-BR do estado de uma execução."""
    lines = [
        f"Execução {status.run_id} do robô '{status.robot_slug}' (versão {status.version_no}): "
        f"{_STATE_TEXT[status.state]}."
    ]
    if status.error_message:
        lines.append(f"Motivo: {status.error_message}")
    if status.outputs is not None:
        outputs = json.dumps(status.outputs, ensure_ascii=False, default=str)
        lines.append(f"Saídas: {outputs}")
    if not status.state.finished:
        prefix = "Execução iniciada. " if started else ""
        lines.append(f"{prefix}Acompanhe com a ferramenta {CONSULTAR_EXECUCAO}.")
    return _clip("\n".join(lines))
