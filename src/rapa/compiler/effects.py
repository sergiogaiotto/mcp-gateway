"""Classificação determinística do efeito de cada ação gravada.

- Botão ou link com verbo irreversível em pt-BR (emitir, enviar, pagar,
  transferir, assinar, autorizar, excluir...) é ``irreversible``, venha antes
  dele uma senha ou não. A exceção é um link que só abriu outra página por GET
  onde a pessoa passou a preencher um formulário: isso é navegação.
- Login é leitura, mas só pela regra de
  ``rapa.recorder.sensitivity.analyze_logins``: o texto inteiro do botão é de
  login (o Enter só conta quando aciona esse botão no mesmo ``<form>``), na
  página a pessoa só preencheu credenciais de uma credencial e nada gravou antes
  no robô. Quem decide é o compilador (``ActionContext.authentication``); na
  dúvida, o envio grava.
- Um clique (ou Enter) cujas requisições seguintes incluem um documento, XHR ou
  fetch que não é GET grava algo: efeito ``write``.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from rapa.core.robot import Effect
from rapa.recorder.models import ElementDescriptor, RequestEvent
from rapa.recorder.sensitivity import action_label, irreversible_verb, writes_in

__all__ = [
    "AUTHENTICATION",
    "ActionContext",
    "Classification",
    "classify_action",
    "irreversible_verb",
    "writes_in",
]

#: Motivo da classificação de um envio de login.
AUTHENTICATION = "autenticação"


@dataclass(frozen=True)
class ActionContext:
    """O que se sabe em volta da ação."""

    element: ElementDescriptor | None
    #: Requisições entre a ação e a próxima ação da pessoa.
    requests: Sequence[RequestEvent]
    #: A ação abriu outra página por GET e a pessoa passou a preencher campos nela.
    navigated_to_form: bool
    #: A ação envia um login que passou na regra conservadora (e nada gravou antes).
    authentication: bool = False


@dataclass(frozen=True)
class Classification:
    effect: Effect
    reason: str


def _clickable(element: ElementDescriptor) -> bool:
    return (
        element.role in ("button", "link", "menuitem")
        or element.tag in ("a", "button")
        or (element.tag == "input" and element.input_type in ("submit", "button", "image"))
    )


def classify_action(ctx: ActionContext) -> Classification:
    element = ctx.element
    writes = writes_in(ctx.requests)
    label = action_label(element)
    verb = irreversible_verb(label)
    # O verbo vem antes do login: "Confirmar pagamento" depois de uma senha de
    # transação nunca é autenticação.
    if verb is not None and element is not None and _clickable(element):
        is_link = element.role == "link" or element.tag == "a"
        if is_link and not writes and ctx.navigated_to_form:
            return Classification(Effect.READ, f"link “{label}” só abre um formulário")
        return Classification(Effect.IRREVERSIBLE, f"verbo “{verb}”")
    if ctx.authentication:
        return Classification(Effect.READ, AUTHENTICATION)
    if writes:
        methods = ", ".join(sorted({r.method for r in writes}))
        return Classification(Effect.WRITE, f"requisição {methods}")
    return Classification(Effect.READ, "sem gravação")
