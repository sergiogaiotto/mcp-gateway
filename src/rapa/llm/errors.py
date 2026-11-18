"""Erros do cliente do hub de LLM.

Nenhuma mensagem leva a chave de acesso, o prompt ou a resposta do modelo.
Trechos de corpo de erro devolvidos pelo hub ficam só no atributo
``body_excerpt``, já cortados e com a chave mascarada, e nunca na mensagem.
"""

__all__ = [
    "CapabilityMissing",
    "HubAuthError",
    "HubConfigError",
    "HubError",
    "HubRequestRejected",
    "HubUnavailable",
    "IncompleteResponse",
    "LLMError",
    "MessageShapeError",
    "ModelChanged",
    "StructuredOutputError",
]


class LLMError(Exception):
    """Erro base do pacote, com mensagem em pt-BR."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HubConfigError(LLMError):
    """Configuração ausente ou inválida (endereço, modelo, chave)."""


class MessageShapeError(LLMError, ValueError):
    """As mensagens não seguem as regras do hub (papéis e mensagem de sistema)."""


class HubError(LLMError):
    """Falha ao falar com o hub."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body_excerpt: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        #: Começo do corpo de erro do hub, cortado e com a chave mascarada.
        self.body_excerpt = body_excerpt
        self.retry_after_s = retry_after_s


class HubRequestRejected(HubError):
    """O hub recusou a requisição (HTTP 4xx que não adianta repetir)."""


class HubAuthError(HubRequestRejected):
    """O hub recusou a chave de acesso (HTTP 401 ou 403)."""


class HubUnavailable(HubError):
    """O hub não respondeu a tempo ou continuou com erro depois das novas tentativas."""


class IncompleteResponse(HubError):
    """O modelo devolveu resposta vazia ou cortada pelo limite de tokens."""


class ModelChanged(HubError):
    """O modelo por trás do apelido mudou desde a última sonda."""


class StructuredOutputError(LLMError):
    """O modelo não devolveu um JSON válido para o esquema pedido."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class CapabilityMissing(LLMError):
    """A matriz de capacidades não tem o que a operação exige."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []
