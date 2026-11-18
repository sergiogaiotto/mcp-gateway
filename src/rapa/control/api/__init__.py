"""API REST do RaPA (``/v1``): catálogo, execuções e administração de robôs."""

from rapa.control.api.errors import ErrorBody, error_response, install_error_handlers
from rapa.control.api.openapi_robots import build_robots_openapi
from rapa.control.api.ratelimit import RateLimiter
from rapa.control.api.routes import (
    MAX_IDEMPOTENCY_KEY,
    ApiServices,
    CreateRobotBody,
    StartRunBody,
    check_idempotency_key,
    create_api_router,
)

__all__ = [
    "MAX_IDEMPOTENCY_KEY",
    "ApiServices",
    "CreateRobotBody",
    "ErrorBody",
    "RateLimiter",
    "StartRunBody",
    "build_robots_openapi",
    "check_idempotency_key",
    "create_api_router",
    "error_response",
    "install_error_handlers",
]
