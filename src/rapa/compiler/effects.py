"""Classificação determinística do efeito de cada ação gravada.

- Botão ou link com verbo irreversível em pt-BR (emitir, enviar, pagar,
  transferir, assinar, autorizar, excluir...) é ``irreversible``, venha antes
  dele uma senha ou não. A exceção é um link que só abriu outra página por GET
  onde a pessoa passou a preencher um formulário: isso é navegação.
- Login continua como leitura, mas só com cara de login: na página a pessoa só
  preencheu credenciais, um usuário e uma senha (ou só um código de uso único),
  nada foi gravado antes e o botão é de login ("Entrar", "Acessar",
  "Continuar"...) ou é o Enter num campo de credencial. Senha de transação (PIN,
  assinatura eletrônica, senha de pagamento) e troca de senha (mais de um campo
  de senha) não são login.
- Um clique (ou Enter) cujas requisições seguintes incluem um documento, XHR ou
  fetch que não é GET grava algo: efeito ``write``.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rapa.compiler.naming import strip_accents
from rapa.core.robot import Effect
from rapa.recorder.models import ElementDescriptor, RequestEvent
from rapa.recorder.sensitivity import action_label, irreversible_verb

__all__ = ["ActionContext", "Classification", "classify_action", "irreversible_verb", "writes_in"]

_WRITE_TYPES = frozenset({"document", "xhr", "fetch"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Textos de botão de login (sem acentos, em minúsculas).
_LOGIN_ACTION = re.compile(
    r"\b("
    r"entrar|entre|acessar|acesse|acesso|login|log in|logon|logar|sign in|signin"
    r"|autenticar|continuar|continue|avancar|avance|prosseguir|proximo|seguir"
    r"|ok|confirmar|confirme|validar|verificar"
    r")\b"
)


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
    #: Campos preenchidos nesta página que são o usuário de um login.
    username_fields_on_page: int = 0
    #: Alguma senha ou código preenchido nesta página é de transação (PIN, assinatura).
    transaction_secret_on_page: bool = False


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


def _login_action(element: ElementDescriptor) -> bool:
    """Botão de login, ou Enter num campo de credencial."""
    if _clickable(element):
        label = action_label(element)
        if not label:
            return False
        return _LOGIN_ACTION.search(" ".join(strip_accents(label).lower().split())) is not None
    return element.tag == "input" and element.sensitive


def _is_authentication(ctx: ActionContext) -> bool:
    element = ctx.element
    form = element.form if element is not None else None
    if element is None or form is None or not ctx.only_credentials_on_page:
        return False
    if ctx.wrote_before or ctx.transaction_secret_on_page or not _login_action(element):
        return False
    if form.has_password:
        return ctx.password_fields_on_page == 1 and ctx.username_fields_on_page >= 1
    return form.has_one_time_code


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
    if _is_authentication(ctx):
        return Classification(Effect.READ, "autenticação")
    if writes:
        methods = ", ".join(sorted({r.method for r in writes}))
        return Classification(Effect.WRITE, f"requisição {methods}")
    return Classification(Effect.READ, "sem gravação")
