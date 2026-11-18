"""Regras de campos sigilosos, as mesmas no gravador e no compilador.

O script da página (``capture.js``) aplica a regra de campo de senha antes de
mandar o valor; o gravador a repete do lado do Python (segunda camada) e o
compilador também, para gravações que chegaram sem ela. As regras:

- campo de texto que é de senha: ``autocomplete`` de senha, ou rótulo, nome,
  id, título ou placeholder que falam em senha, PIN ou password (o script
  também olha a máscara por CSS, ``-webkit-text-security``, que só a página
  conhece);
- usuário do login: o campo de texto preenchido logo antes de uma senha, no
  mesmo documento (a regra de ``mark_username`` do compilador, repetida aqui
  para o gravador tirar o valor da gravação);
- literal com cara de segredo (cartão, token, chave): o mesmo padrão que a
  generalização usa para ocultar valores do modelo de linguagem.
"""

import re
import unicodedata
from collections.abc import Sequence
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
    "clickable",
    "field_key",
    "focus_only",
    "is_password_field",
    "is_password_like_text",
    "is_text_field",
    "looks_secret",
    "mentions_password",
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
    ``autocomplete``, só contam pelo ``autocomplete`` de senha.
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
            element.name,
            element.placeholder,
            element.title,
            element.name_attr,
            element.id,
        )
    )


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


def username_inputs(events: Sequence[RecordingEvent]) -> list[InputEvent]:
    """Entradas que o compilador vai tratar como o usuário do login, ainda com valor.

    É a regra de ``mark_username``: o último passo antes de digitar uma senha é
    digitar um campo de texto (não senha nem código) da mesma página. Edições
    seguidas do mesmo campo são um passo só; Tab, cliques que só focam um
    campo e outras ações que não viram passo não separam o usuário da senha.
    """
    popup_triggers = {
        e.trigger_index
        for e in events
        if isinstance(e, NavigationEvent) and e.main_frame and e.cause is NavigationCause.POPUP
    }
    found: list[InputEvent] = []
    navigated = False
    document: int | None = None
    #: Entradas do último passo, quando ele é de digitar (edições seguidas de um campo).
    run: list[InputEvent] = []
    run_document: int | None = None
    for event in events:
        if isinstance(event, NavigationEvent):
            if event.main_frame:
                navigated = True
                document = event.index
            continue
        if event.type not in USER_ACTION_TYPES or not navigated:
            continue
        if isinstance(event, KeyEvent):
            if event.key != "Tab" and not (event.key == "Enter" and clickable(event.element)):
                run = []
            continue
        if isinstance(event, ClickEvent):
            if not focus_only(event.element) and event.index not in popup_triggers:
                run = []
            continue
        if isinstance(event, SelectEvent) and not event.label:
            continue  # sem rótulo da opção o compilador não faz passo
        if not isinstance(event, InputEvent):
            run = []
            continue
        element = event.element
        key = field_key(event.page_id, element)
        if is_password_field(element):
            first = run[0].element if run else None
            if (
                first is not None
                and run_document == document
                and not first.sensitive
                and not is_password_field(first)
                and not first.is_one_time_code
                and is_text_field(first)
                and field_key(run[0].page_id, first) != key
            ):
                found.extend(e for e in run if not e.element.sensitive)
        elif not element.sensitive and event.value is None:
            continue  # o compilador não faz passo sem o valor
        if run and field_key(run[0].page_id, run[0].element) == key:
            run.append(event)
        else:
            run = [event]
            run_document = document
    return found
