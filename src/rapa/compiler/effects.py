"""Classificação determinística do efeito de cada ação gravada.

- Um clique (ou Enter) cujas requisições seguintes incluem um documento, XHR ou
  fetch que não é GET grava algo: efeito ``write``.
- Login (formulário com uma senha, ou código de uso único antes de qualquer
  gravação) continua como leitura; troca de senha (mais de um campo de senha)
  não é login.
- Botão ou link com verbo irreversível em pt-BR (emitir, enviar, pagar,
  excluir...) é ``irreversible``. A exceção é um link que só abriu outra página
  por GET onde a pessoa passou a preencher um formulário: isso é navegação.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rapa.compiler.naming import strip_accents
from rapa.core.robot import Effect
from rapa.recorder.models import ElementDescriptor, RequestEvent

__all__ = ["ActionContext", "Classification", "classify_action", "irreversible_verb", "writes_in"]

_IRREVERSIBLE = re.compile(
    r"\b("
    r"emitir|emita|enviar|envie|transmitir|transmita|pagar|pague|confirmar pagamento"
    r"|excluir|exclua|apagar|apague|assinar|assine|finalizar|finalize|concluir|conclua"
    r"|protocolar|protocole|autorizar|autorize|aprovar|aprove|solicitar|solicite"
    r"|cancelar|cancele"
    r")\b"
)
_WRITE_TYPES = frozenset({"document", "xhr", "fetch"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def irreversible_verb(text: str | None) -> str | None:
    """O verbo irreversível que aparece no texto, se houver."""
    if not text:
        return None
    match = _IRREVERSIBLE.search(" ".join(strip_accents(text).lower().split()))
    return match.group(1) if match else None


def writes_in(requests: Sequence[RequestEvent]) -> list[RequestEvent]:
    """Requisições que gravam algo (documento, XHR ou fetch que não é GET)."""
    return [
        r for r in requests if r.resource_type in _WRITE_TYPES and r.method not in _SAFE_METHODS
    ]


@dataclass(frozen=True)
class ActionContext:
    """O que se sabe em volta da ação."""

    element: ElementDescriptor | None
    #: Requisições entre a ação e a próxima ação da pessoa.
    requests: Sequence[RequestEvent]
    #: A pessoa só preencheu credenciais (usuário, senha, código) nesta página.
    only_credentials_on_page: bool
    #: Campos de senha diferentes preenchidos nesta página (troca de senha tem mais de um).
    password_fields_on_page: int
    #: Algum passo anterior já grava algo.
    wrote_before: bool
    #: A ação abriu outra página por GET e a pessoa passou a preencher campos nela.
    navigated_to_form: bool


@dataclass(frozen=True)
class Classification:
    effect: Effect
    reason: str


def _is_authentication(ctx: ActionContext) -> bool:
    element = ctx.element
    form = element.form if element is not None else None
    if form is None or not ctx.only_credentials_on_page:
        return False
    if form.has_password:
        return ctx.password_fields_on_page <= 1
    return form.has_one_time_code and not ctx.wrote_before


def classify_action(ctx: ActionContext) -> Classification:
    element = ctx.element
    writes = writes_in(ctx.requests)
    if _is_authentication(ctx):
        return Classification(Effect.READ, "autenticação")
    label = None
    if element is not None:
        label = element.name or element.text or element.title
    verb = irreversible_verb(label)
    clickable = element is not None and (
        element.role in ("button", "link", "menuitem")
        or element.tag in ("a", "button")
        or (element.tag == "input" and element.input_type in ("submit", "button", "image"))
    )
    if verb is not None and clickable and element is not None:
        is_link = element.role == "link" or element.tag == "a"
        if is_link and not writes and ctx.navigated_to_form:
            return Classification(Effect.READ, f"link “{label}” só abre um formulário")
        return Classification(Effect.IRREVERSIBLE, f"verbo “{verb}”")
    if writes:
        methods = ", ".join(sorted({r.method for r in writes}))
        return Classification(Effect.WRITE, f"requisição {methods}")
    return Classification(Effect.READ, "sem gravação")
