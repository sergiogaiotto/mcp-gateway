"""Gateway MCP do RaPA: um endpoint, uma ferramenta por robô publicado da empresa."""

from rapa.control.mcp.server import PRINCIPAL_SCOPE_KEY, McpGateway, McpHandlers, McpSettings
from rapa.control.mcp.tools import (
    BUSCAR_ROBO,
    CONSULTAR_EXECUCAO,
    EXECUTAR_ROBO,
    IDEMPOTENCY_META_KEY,
    RESERVED_NAMES,
    robot_tool,
)

__all__ = [
    "BUSCAR_ROBO",
    "CONSULTAR_EXECUCAO",
    "EXECUTAR_ROBO",
    "IDEMPOTENCY_META_KEY",
    "PRINCIPAL_SCOPE_KEY",
    "RESERVED_NAMES",
    "McpGateway",
    "McpHandlers",
    "McpSettings",
    "robot_tool",
]
