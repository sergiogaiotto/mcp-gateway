"""Regras de campos sigilosos, as mesmas no gravador e no compilador.

O script da página (``capture.js``) aplica a regra de campo de senha antes de
mandar o valor; o gravador a repete do lado do Python (segunda camada) e o
compilador também, para gravações que chegaram sem ela. As regras:

- campo de texto que é de senha: ``autocomplete`` de senha, ou rótulo
  (``<label>``, ``aria-labelledby`` ou ``aria-label``, nessa ordem, como no
  script), título, placeholder, atributo ``name`` ou id que falam em senha, PIN
  ou password (o script também olha a máscara por CSS,
  ``-webkit-text-security``, que só a página conhece); um campo que o script já
  marcou como usuário nunca vira senha;
- usuário do login: o campo de texto preenchido logo antes de uma senha, no
  mesmo documento, ou, no login em duas etapas de uma SPA, o primeiro campo da
  página, preenchido antes de um "Continuar" ou de um Enter que mostrou a senha
  (a regra de ``mark_username`` do compilador, repetida aqui para o gravador
  tirar o valor da gravação);
- senha de transação (PIN, assinatura eletrônica, senha de pagamento) ou senha
  enviada por um botão com verbo irreversível ("Confirmar pagamento",
  "Transferir"): não é login. O campo antes dela some da gravação do mesmo jeito,
  mas não vira o usuário do cofre (o robô pede o valor numa entrada);
- literal com cara de segredo (cartão, token, chave): o mesmo padrão que a
  generalização usa para ocultar valores do modelo de linguagem.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from rapa.recorder.models import (
    USER_ACTION_TYPES,
    ClickEvent,
    ElementDescriptor,
    InputEvent,
    KeyEvent,
    NavigationCause,
    NavigationEvent,
    RecordingEvent,
    SelectEvent,
)

__all__ = [
    "BUTTON_INPUT_TYPES",
    "SENSITIVE_TEXT",
    "TEXT_INPUT_TYPES",
    "LoginInputs",
    "action_label",
    "clickable",
    "declared_username",
    "field_key",
    "focus_only",
    "irreversible_click",
    "irreversible_verb",
    "is_password_field",
    "is_password_like_text",
    "is_text_field",
    "is_transaction_secret",
    "login_inputs",
    "looks_secret",
    "mentions_password",
    "submit_after",
    "username_inputs",
]

TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", "search", "number", ""})
BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
#: Tipos em que um rótulo de senha indica senha (e-mail, endereço e busca nunca são).
_PASSWORD_LABEL_TYPES = frozenset({"text", "", "tel", "number"})
#: "senha" (mas não "resenha"), "password", "passwd", "passphrase" e as palavras
#: "pwd" e "pin". Igual a ``PASSWORD_WORDS`` em ``capture.js``.
_PASSWORD_WORDS = re.compile(
    r"(?<!re)senha|password|passwd|passphrase|(?:^|[^a-z0-9])(?:pwd|pin)(?:[^a-z0-9]|$)"
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
#: Segredo de uma operação, não de login: senha de transação, de pagamento ou do
#: cartão, assinatura eletrônica, PIN. Igual a ``TRANSACTION_WORDS`` em ``capture.js``.
_TRANSACTION_WORDS = re.compile(
    r"transac|transfer|assinatura|assinar|autoriza|pagamento|operac|cartao"
    r"|(?:^|[^a-z0-9])pin(?:[^a-z0-9]|$)"
)
#: Verbos de ação irreversível em pt-BR, sem acentos (emitir, enviar, pagar,
#: transferir, excluir...). Igual a ``IRREVERSIBLE`` em ``capture.js``.
_IRREVERSIBLE = re.compile(
    r"\b("
    r"emitir|emita|enviar|envie|transmitir|transmita|pagar|pague|confirmar pagamento"
    r"|confirmar transferencia|transferir|transfira"
    r"|excluir|exclua|apagar|apague|assinar|assine|finalizar|finalize|concluir|conclua"
    r"|protocolar|protocole|autorizar|autorize|aprovar|aprove|solicitar|solicite"
    r"|cancelar|cancele"
    r")\b"
)
#: Texto com cara de segredo (o mesmo padrão que ``rapa.llm.generalize`` usa para
#: ocultar literais do modelo de linguagem).
SENSITIVE_TEXT = re.compile(
    r"senha|password|passwd|passphrase|segredo|secret|token|api[ _-]?key|chave de acesso"
    r"|c[óo]digo de (?:verifica|seguran|acesso)|\botp\b|\btotp\b|\bpin\b|\bcvv\b|\bcvc\b"
    r"|cart[ãa]o de cr[ée]dito|n[úu]mero do cart[ãa]o|type=[\"']?password",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    """Minúsculas sem acentos, com ``camelCase`` separado ("senhaAtual" → "senha atual")."""
    split = _CAMEL.sub(r"\1 \2", " ".join(text.split()))
    decomposed = unicodedata.normalize("NFD", split)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def mentions_password(text: str | None) -> bool:
    """O texto fala em senha (rótulo, nome, id ou placeholder de um campo)."""
    return bool(text) and _PASSWORD_WORDS.search(_plain(text or "")) is not None


def _autocomplete(element: ElementDescriptor) -> list[str]:
    return (element.autocomplete or "").lower().split()


def is_password_like_text(element: ElementDescriptor) -> bool:
    """Campo de texto (não do tipo senha) que é de senha pelo autocomplete ou pelo rótulo.

    Campos de e-mail, endereço e busca, e os marcados como usuário ou e-mail no
    ``autocomplete``, só contam pelo ``autocomplete`` de senha. Os textos são os
    mesmos que o script olha: o rótulo (``<label>`` antes de ``aria-labelledby``),
    o título, o placeholder, o nome e o id; o nome acessível, em que
    ``aria-labelledby`` vem antes do ``<label>``, não entra, para que as duas
    camadas nunca discordem.
    """
    if element.tag != "input" or element.is_password or element.is_one_time_code:
        return False
    input_type = element.input_type or ""
    if input_type not in TEXT_INPUT_TYPES:
        return False
    autocomplete = _autocomplete(element)
    if "current-password" in autocomplete or "new-password" in autocomplete:
        return True
    if "username" in autocomplete or "email" in autocomplete:
        return False
    if input_type not in _PASSWORD_LABEL_TYPES:
        return False
    return any(
        mentions_password(text)
        for text in (
            element.label,
            element.title,
            element.placeholder,
            element.name_attr,
            element.id,
        )
    )


def is_transaction_secret(element: ElementDescriptor) -> bool:
    """Senha ou código de uma operação (transação, pagamento, assinatura, PIN), não de login."""
    texts = (
        element.label,
        element.name,
        element.placeholder,
        element.title,
        element.name_attr,
        element.id,
    )
    return any(_TRANSACTION_WORDS.search(_plain(text)) for text in texts if text)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def irreversible_verb(text: str | None) -> str | None:
    """O verbo irreversível que aparece no texto, se houver."""
    if not text:
        return None
    match = _IRREVERSIBLE.search(" ".join(_strip_accents(text).lower().split()))
    return match.group(1) if match else None


def action_label(element: ElementDescriptor | None) -> str | None:
    """O texto de um botão ou link que a regra do verbo irreversível olha."""
    if element is None:
        return None
    return element.name or element.text or element.title


def declared_username(element: ElementDescriptor) -> bool:
    """A página declara o campo como usuário (``autocomplete="username"``)."""
    return "username" in _autocomplete(element)


def is_password_field(element: ElementDescriptor) -> bool:
    """Campo de senha: do tipo senha ou, sem outra marca de sigilo, com cara de senha."""
    return element.is_password or (not element.sensitive and is_password_like_text(element))


def looks_secret(element: ElementDescriptor) -> bool:
    """Campo cujo rótulo, nome ou id tem cara de segredo (cartão, token, chave, senha)."""
    texts = (
        element.label,
        element.name,
        element.placeholder,
        element.title,
        element.name_attr,
        element.id,
        element.test_id,
    )
    return any(SENSITIVE_TEXT.search(text) for text in texts if text)


def field_key(page_id: int, element: ElementDescriptor) -> tuple[Any, ...]:
    """Identidade de um campo na gravação: aba, iframes, seletor, id e nome."""
    frames = tuple(f.css or f.id or f.name or "" for f in element.frame_path or [])
    return (page_id, frames, element.css, element.id, element.name_attr)


def is_text_field(element: ElementDescriptor) -> bool:
    return (element.tag == "input" and (element.input_type or "") in TEXT_INPUT_TYPES) or (
        element.tag == "textarea"
    )


def focus_only(element: ElementDescriptor) -> bool:
    """Clique que só foca ou ativa um campo (o evento do campo basta)."""
    if element.tag in ("textarea", "select", "option"):
        return True
    if element.tag == "input":
        return element.input_type not in BUTTON_INPUT_TYPES
    return False


def clickable(element: ElementDescriptor | None) -> bool:
    if element is None:
        return False
    return (
        element.role in ("button", "link")
        or element.tag in ("a", "button")
        or (element.tag == "input" and element.input_type in BUTTON_INPUT_TYPES)
    )


def irreversible_click(element: ElementDescriptor | None) -> bool:
    """Botão, link ou item de menu cujo texto tem um verbo irreversível."""
    if element is None:
        return False
    is_control = element.role == "menuitem" or clickable(element)
    return is_control and irreversible_verb(action_label(element)) is not None


def submit_after(events: Sequence[RecordingEvent], position: int) -> ClickEvent | None:
    """O clique que envia o campo do evento em ``position`` (a senha, por exemplo).

    É a primeira ação seguinte que não é editar o mesmo campo de novo, Tab ou um
    clique que só foca um campo, no mesmo documento. Um Enter só leva ao botão
    quando o formulário o aciona em seguida (o clique de envio implícito); sem
    clique assim (o próprio Enter enviou, outra ação, outra página, fim da
    gravação), não há botão.
    """
    current = events[position]
    if not isinstance(current, InputEvent):
        return None
    key = field_key(current.page_id, current.element)
    entered = False
    for event in events[position + 1 :]:
        if isinstance(event, NavigationEvent) and event.main_frame:
            return None
        if event.type not in USER_ACTION_TYPES:
            continue
        if entered and not (isinstance(event, ClickEvent) and event.detail == 0):
            return None
        if isinstance(event, InputEvent) and field_key(event.page_id, event.element) == key:
            continue
        if isinstance(event, KeyEvent) and event.key == "Tab":
            continue
        if isinstance(event, KeyEvent) and event.key == "Enter":
            entered = True
            continue
        if isinstance(event, ClickEvent):
            if focus_only(event.element):
                continue
            return event
        return None
    return None


@dataclass(frozen=True)
class LoginInputs:
    """Campos de texto que ficam antes de uma senha na gravação."""

    #: Os que o compilador trata como o usuário do login e que ainda têm valor.
    usernames: list[InputEvent]
    #: Os que ficam antes de uma senha de transação (ou de uma senha enviada por um
    #: botão irreversível): saem da gravação, mas não viram o usuário do cofre.
    hidden: list[InputEvent]


@dataclass
class _Step:
    """Um passo que o compilador vai fazer, na simulação de ``login_inputs``."""

    #: "fill" (digitar), "bridge" (clique ou Enter que não separa usuário e senha
    #: numa SPA) ou "other" (qualquer outro passo).
    kind: str
    key: tuple[Any, ...] | None = None
    inputs: list[InputEvent] = field(default_factory=list)


def _username_chain(steps: Sequence[_Step], password_key: tuple[Any, ...]) -> list[InputEvent]:
    """Entradas do campo que é o usuário da senha que vem depois de ``steps``.

    O último passo antes da senha digita um campo de texto (não senha nem código)
    diferente da senha. Se entre os dois houve só cliques sem verbo irreversível
    ou Enter (login em duas etapas de uma SPA: "Continuar" mostra a senha), o
    campo também precisa ser o primeiro preenchido no documento.
    """
    position = len(steps) - 1
    bridged = False
    while position >= 0 and steps[position].kind == "bridge":
        bridged = True
        position -= 1
    if position < 0 or steps[position].kind != "fill":
        return []
    candidate = steps[position]
    first = candidate.inputs[0].element
    if (
        candidate.key == password_key
        or is_password_field(first)
        or first.is_one_time_code
        or not is_text_field(first)
    ):
        return []
    if not bridged:
        return list(candidate.inputs)
    chain = list(candidate.inputs)
    for step in reversed(steps[:position]):
        if step.kind == "bridge":
            continue
        if step.kind == "fill" and step.key == candidate.key:
            chain = step.inputs + chain
            continue
        return []
    return chain


def login_inputs(events: Sequence[RecordingEvent]) -> LoginInputs:
    """Usuários de login (ainda com valor) e campos antes de senhas de transação.

    É a regra de ``mark_username`` do compilador, repetida sobre os eventos:
    edições seguidas do mesmo campo são um passo só; Tab, cliques que só focam um
    campo e outras ações que não viram passo não separam o usuário da senha; um
    clique sem verbo irreversível ou um Enter separam, mas ainda valem quando o
    campo foi o primeiro preenchido na página (login em duas etapas). Quando a
    senha é de transação ou vai num botão irreversível, o campo vai para
    ``hidden`` (a não ser que a página o declare como usuário no ``autocomplete``).
    """
    popup_triggers = {
        e.trigger_index
        for e in events
        if isinstance(e, NavigationEvent) and e.main_frame and e.cause is NavigationCause.POPUP
    }
    usernames: list[InputEvent] = []
    hidden: list[InputEvent] = []
    navigated = False
    #: Passos do documento atual (desde a última navegação do quadro principal).
    steps: list[_Step] = []
    for position, event in enumerate(events):
        if isinstance(event, NavigationEvent):
            if event.main_frame:
                navigated = True
                steps = []
            continue
        if event.type not in USER_ACTION_TYPES or not navigated:
            continue
        if isinstance(event, KeyEvent):
            if event.key == "Tab":
                continue
            steps.append(_Step("bridge" if event.key == "Enter" else "other"))
            continue
        if isinstance(event, ClickEvent):
            if focus_only(event.element) or event.index in popup_triggers:
                continue
            steps.append(_Step("other" if irreversible_click(event.element) else "bridge"))
            continue
        if isinstance(event, SelectEvent) and not event.label:
            continue  # sem rótulo da opção o compilador não faz passo
        if not isinstance(event, InputEvent):
            steps.append(_Step("other"))
            continue
        element = event.element
        key = field_key(event.page_id, element)
        if is_password_field(element):
            chain = _username_chain(steps, key)
            transaction = bool(chain) and (
                is_transaction_secret(element)
                or irreversible_click(_element(submit_after(events, position)))
            )
            for entry in chain:
                if transaction and not declared_username(entry.element):
                    hidden.append(entry)
                elif not entry.element.sensitive:
                    usernames.append(entry)
        elif not element.sensitive and event.value is None:
            continue  # o compilador não faz passo sem o valor
        last = steps[-1] if steps else None
        if last is not None and last.kind == "fill" and last.key == key:
            last.inputs.append(event)
        else:
            steps.append(_Step("fill", key, [event]))
    return LoginInputs(usernames=usernames, hidden=hidden)


def _element(event: ClickEvent | None) -> ElementDescriptor | None:
    return event.element if event is not None else None


def username_inputs(events: Sequence[RecordingEvent]) -> list[InputEvent]:
    """Entradas que o compilador vai tratar como o usuário do login, ainda com valor."""
    return login_inputs(events).usernames
