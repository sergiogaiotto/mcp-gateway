"""Documento OpenAPI 3.1 com os robôs publicados visíveis para quem chama.

Cada robô vira uma operação ``POST /v1/catalogo/{slug}/execucoes`` com o
esquema das entradas dele. Serve para clientes REST gerarem código e para
ferramentas que importam OpenAPI.

O ``operationId`` de cada robô é ``executar_<slug>``: o prefixo garante que
nenhum robô repita o de uma operação fixa (como ``consultar_execucao``), mesmo
um robô antigo publicado antes de esses nomes ficarem reservados.
"""

import copy
from collections.abc import Sequence
from typing import Any

from rapa.control.api.errors import ErrorBody
from rapa.core.contracts import RobotSummary, RunStatus
from rapa.core.robot import Effect

__all__ = ["build_robots_openapi"]

_REF = "#/components/schemas/{model}"
#: Prefixo dos ``operationId`` dos robôs; nenhuma operação fixa começa assim.
_ROBOT_OPERATION_PREFIX = "executar_"
_RUN_OPERATION_ID = "consultar_execucao"
_EFFECT_TEXT = {
    Effect.READ: "Só lê dados no sistema de destino.",
    Effect.WRITE: "Grava dados no sistema de destino.",
    Effect.IRREVERSIBLE: "Faz uma ação irreversível no sistema de destino.",
}


def _schemas() -> dict[str, Any]:
    components: dict[str, Any] = {}
    for model in (RunStatus, ErrorBody):
        schema = model.model_json_schema(ref_template=_REF)
        components.update(schema.pop("$defs", {}))
        components[model.__name__] = schema
    return components


def _embedded(schema: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(schema)
    out.pop("$schema", None)
    return out


def _error(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": _REF.format(model="ErrorBody")}}},
    }


def _operation(robot: RobotSummary) -> dict[str, Any]:
    body_schema = {
        "type": "object",
        "properties": {"entradas": _embedded(robot.input_schema)},
        "required": ["entradas"],
        "additionalProperties": False,
    }
    return {
        "operationId": f"{_ROBOT_OPERATION_PREFIX}{robot.slug}",
        "summary": robot.title,
        "description": f"{robot.description}\n\n{_EFFECT_TEXT[robot.effect]}",
        "tags": ["robôs"],
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": False,
                "description": "Repetir a chave devolve a mesma execução em vez de criar outra.",
                "schema": {"type": "string", "maxLength": 200},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": body_schema}},
        },
        "responses": {
            "202": {
                "description": "Execução aceita; acompanhe pelo endereço em Location.",
                "headers": {
                    "Location": {
                        "description": "Endereço da execução",
                        "schema": {"type": "string"},
                    }
                },
                "content": {
                    "application/json": {"schema": {"$ref": _REF.format(model="RunStatus")}}
                },
            },
            "401": _error("Token ausente ou inválido."),
            "403": _error("Sem permissão para executar o robô."),
            "404": _error("Robô não encontrado."),
            "409": _error("Chave de idempotência já usada com outra requisição."),
            "422": _error("Entradas inválidas."),
            "429": _error("Limite excedido; veja Retry-After."),
        },
        "x-rapa-efeito": robot.effect.value,
        "x-rapa-versao": robot.version_no,
    }


def build_robots_openapi(
    robots: Sequence[RobotSummary], *, server_url: str | None = None
) -> dict[str, Any]:
    """Monta o documento OpenAPI 3.1 dos robôs informados (já filtrados pela empresa)."""
    paths: dict[str, Any] = {}
    for robot in sorted(robots, key=lambda r: r.slug):
        paths[f"/v1/catalogo/{robot.slug}/execucoes"] = {"post": _operation(robot)}
    paths["/v1/execucoes/{run_id}"] = {
        "get": {
            "operationId": _RUN_OPERATION_ID,
            "summary": "Consultar uma execução",
            "tags": ["execuções"],
            "parameters": [
                {
                    "name": "run_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                }
            ],
            "responses": {
                "200": {
                    "description": "Estado atual da execução.",
                    "content": {
                        "application/json": {"schema": {"$ref": _REF.format(model="RunStatus")}}
                    },
                },
                "401": _error("Token ausente ou inválido."),
                "404": _error("Execução não encontrada."),
            },
        }
    }
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Robôs do RaPA",
            "version": "1",
            "description": "Robôs publicados para a sua empresa. Cada execução é assíncrona.",
        },
        "paths": paths,
        "components": {
            "schemas": _schemas(),
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        },
        "security": [{"bearer": []}],
    }
    if server_url:
        document["servers"] = [{"url": server_url}]
    return document
