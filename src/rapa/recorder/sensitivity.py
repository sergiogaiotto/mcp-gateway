"""Regras de campos sigilosos e do login, as mesmas no gravador e no compilador.

O script da página (``capture.js``) aplica a regra de campo de senha antes de
mandar o valor; o gravador a repete do lado do Python (segunda camada) e o
compilador também, para gravações que chegaram sem ela. As regras:

- campo de texto que é de senha: ``autocomplete`` de senha, ou rótulo
  (``<label>``, ``aria-labelledby`` ou ``aria-label``, nessa ordem, como no
  script), título, placeholder, atributo ``name`` ou id que falam em senha, PIN
  ou password (o script também olha a máscara por CSS,
  ``-webkit-text-security``, que só a página conhece); um campo que o script já
  marcou como usuário nunca vira senha;
- campo cujo nome acessível (``aria-labelledby`` ou ``aria-label``) fala em
  senha, mas o rótulo não: o valor sai da gravação do mesmo jeito, sem a marca
  de senha (``password_by_name``); só vira a senha do cofre num login que passa
  na regra abaixo, e nunca desfaz a marca de usuário do script;
- login (``analyze_logins``), regra conservadora: na dúvida o passo grava. O
  envio só é autenticação quando o texto inteiro do botão ou link é de login
  ("Entrar", "Acessar", "Continuar"...; "Confirmar" só no formulário que só tem
  o código de uso único) ou é o Enter num campo de credencial, sem verbo
  irreversível, e na página (desde a última navegação que carregou documento)
  a pessoa só preencheu credenciais: um usuário, uma senha e, se houver, o
  código. O compilador ainda exige que nada tenha gravado antes;
- usuário do login: o campo de texto preenchido logo antes da senha (ou logo
  depois, quando o script o marcou como usuário), e só quando o envio da senha
  passa na regra; no login em duas etapas, o único campo preenchido antes de um
  botão "Continuar", "Avançar", "Próximo", "Seguinte" ou "Next" (ou de um Enter
  nele), seguido de uma senha de verdade (``type=password``). Navegações no
  mesmo documento (``pushState``, âncora) e caixas de seleção ("Lembrar meu
  usuário") no meio não atrapalham. Fora disso o campo fica literal;
- senha de transação (PIN, assinatura eletrônica, senha de pagamento) ou senha
  enviada por um botão com verbo irreversível ("Confirmar pagamento",
  "Transferir"): não é login. O campo antes dela sai da gravação, mas não vira
  o usuário do cofre (o robô pede o valor numa entrada); um campo que o script
  marcou assim nunca vira usuário. O botão de mostrar a senha não é o envio;
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
    FormInfo,
    InputEvent,
    KeyEvent,
    NavigationCause,
    NavigationEvent,
    RecordingEvent,
    SelectEvent,
)

__all__ = [
    "BRIDGE_WORDS",
    "BUTTON_INPUT_TYPES",
    "LOGIN_WORDS",
    "SENSITIVE_TEXT",
    "TEXT_INPUT_TYPES",
    "LoginAnalysis",
    "LoginGroup",
    "LoginInputs",
    "action_label",
    "analyze_logins",
    "bridge_button",
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
    "login_button",
    "login_inputs",
    "login_submit",
    "looks_secret",
    "mentions_password",
    "password_by_name",
    "password_toggle",
    "plain_label",
    "username_field",
    "username_inputs",
]

TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", "search", "number", ""})
BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
#: Tipos em que um rótulo de senha indica senha (e-mail, endereço e busca nunca são).
_PASSWORD_LABEL_TYPES = frozenset({"text", "", "tel", "number"})
#: Tipos de campo que podem ser o usuário de um login.
_USERNAME_TYPES = frozenset({"text", "email", "tel", "number", ""})
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
#: Texto inteiro de um botão ou link de login (sem acentos, em minúsculas, brancos
#: juntados). "confirmar" só vale no formulário que só tem o código de uso único.
LOGIN_WORDS = frozenset(
    {
        "entrar",
        "acessar",
        "login",
        "log in",
        "fazer login",
        "sign in",
        "logar",
        "iniciar sessao",
        "continuar",
        "avancar",
        "proximo",
        "seguinte",
        "confirmar",
    }
)
#: Texto inteiro do botão que leva do usuário à senha num login em duas etapas.
BRIDGE_WORDS = frozenset({"continuar", "avancar", "proximo", "seguinte", "next"})
_EDGE_SYMBOLS = re.compile(r"^[\W_]+|[\W_]+$")
#: Botão que mostra ou esconde a senha digitada (o "olho" ao lado do campo).
_TOGGLE_WORDS = re.compile(
    r"\b(mostrar|exibir|ocultar|esconder|ver|visualizar|revelar|show|hide|toggle|alternar)\b"
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
    ``aria-labelledby`` vem antes do ``<label>``, não marca a senha (ver
    ``password_by_name``), para que as duas camadas nunca discordem.
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


def password_by_name(element: ElementDescriptor) -> bool:
    """Campo de texto que só o nome acessível (``aria-labelledby``, ``aria-label``) diz ser senha.

    O valor sai da gravação (o campo fica sensível), mas sem a marca de senha: só
    vira a senha do cofre num login que passa na regra. Um campo que o script
    marcou como usuário continua usuário. Igual a ``isPasswordByName`` em
    ``capture.js``.
    """
    if element.tag != "input" or element.is_password or element.is_one_time_code:
        return False
    if element.is_username or (element.input_type or "") not in _PASSWORD_LABEL_TYPES:
        return False
    autocomplete = _autocomplete(element)
    if "username" in autocomplete or "email" in autocomplete:
        return False
    return mentions_password(element.name) and not is_password_like_text(element)


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


def plain_label(text: str | None) -> str:
    """Texto de botão para comparar inteiro: sem acentos, minúsculas, brancos juntados.

    Só os símbolos das pontas saem ("Entrar ›" → "entrar"); o meio fica como está.
    """
    if not text:
        return ""
    folded = " ".join(_strip_accents(text).lower().split())
    return _EDGE_SYMBOLS.sub("", folded)


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


def username_field(element: ElementDescriptor) -> bool:
    """Campo que pode ser o usuário de um login.

    Campo de texto, e-mail, telefone ou número (nunca área de texto, busca ou
    endereço), que não é senha, código nem campo que o nome acessível diz ser
    senha. Um campo que o script escondeu sem chamar de usuário (antes de uma
    senha de transação) nunca vira usuário.
    """
    if element.tag != "input" or (element.input_type or "") not in _USERNAME_TYPES:
        return False
    if is_password_field(element) or element.is_one_time_code or password_by_name(element):
        return False
    return element.is_username or not element.sensitive


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


def _only_one_time_code(form: FormInfo | None) -> bool:
    """Formulário cujo único campo de texto é o código de uso único."""
    return (
        form is not None
        and form.has_one_time_code
        and not form.has_password
        and not form.text_fields
    )


def login_button(element: ElementDescriptor | None) -> bool:
    """Botão ou link cujo texto inteiro é de login (``LOGIN_WORDS``), sem verbo irreversível.

    "Confirmar" só conta no formulário que só tem o código de uso único: com
    outros campos ele confirma qualquer coisa (um Pix, uma compra).
    """
    if not clickable(element) or element is None:
        return False
    label = plain_label(action_label(element))
    if label not in LOGIN_WORDS or irreversible_verb(label) is not None:
        return False
    return label != "confirmar" or _only_one_time_code(element.form)


def bridge_button(element: ElementDescriptor | None) -> bool:
    """Botão que leva do usuário à senha num login em duas etapas ("Continuar", "Avançar")."""
    if not clickable(element) or element is None:
        return False
    return plain_label(action_label(element)) in BRIDGE_WORDS


def password_toggle(element: ElementDescriptor | None) -> bool:
    """O botão de mostrar ou esconder a senha ("Mostrar senha"): nunca é o envio."""
    if not clickable(element) or element is None:
        return False
    label = action_label(element)
    if not mentions_password(label) or irreversible_verb(label) is not None:
        return False
    return _TOGGLE_WORDS.search(plain_label(label)) is not None


def _popup_triggers(events: Sequence[RecordingEvent]) -> set[int]:
    return {
        e.trigger_index
        for e in events
        if isinstance(e, NavigationEvent)
        and e.main_frame
        and e.cause is NavigationCause.POPUP
        and e.trigger_index is not None
    }


def _following_actions(events: Sequence[RecordingEvent]) -> dict[int, RecordingEvent | None]:
    actions = [e for e in events if e.type in USER_ACTION_TYPES]
    return {
        action.index: actions[i + 1] if i + 1 < len(actions) else None
        for i, action in enumerate(actions)
    }


def login_submit(
    events: Sequence[RecordingEvent],
    position: int,
    keys: set[tuple[Any, ...]],
    *,
    popup_triggers: set[int] | None = None,
    following: dict[int, RecordingEvent | None] | None = None,
) -> ClickEvent | KeyEvent | None:
    """A ação que envia as credenciais preenchidas até o evento em ``position``.

    É a primeira ação seguinte, no mesmo documento, que não é editar de novo um
    dos campos ``keys`` (usuário e senha) ou um código de uso único, Tab, marcar
    uma caixa ("Lembrar-me"), um clique que só foca um campo ou o botão de mostrar
    a senha. Um Enter leva ao botão quando o formulário o aciona em seguida (o
    clique de envio implícito); sem esse clique, o envio é o próprio Enter, se
    ele foi dado num dos campos ``keys``. Qualquer outra ação (inclusive em outra
    aba), uma navegação que carrega documento ou o fim da gravação: não há envio.
    """
    triggers = _popup_triggers(events) if popup_triggers is None else popup_triggers
    nexts = _following_actions(events) if following is None else following
    tab = events[position].page_id
    for event in events[position + 1 :]:
        if isinstance(event, NavigationEvent) and event.main_frame:
            if event.same_document and event.page_id == tab:
                continue
            return None
        if event.type not in USER_ACTION_TYPES:
            continue
        if event.page_id != tab:
            return None
        if isinstance(event, InputEvent):
            if event.element.is_one_time_code or field_key(event.page_id, event.element) in keys:
                continue
            return None
        if isinstance(event, KeyEvent):
            if event.key == "Tab":
                continue
            if event.key != "Enter":
                return None
            if clickable(event.element):
                continue  # o clique de ativação vem em seguida
            nxt = nexts.get(event.index)
            if isinstance(nxt, ClickEvent) and nxt.detail == 0:
                return nxt if nxt.index not in triggers else None
            if event.element is None:
                return None
            return event if field_key(event.page_id, event.element) in keys else None
        if isinstance(event, ClickEvent):
            if focus_only(event.element) or password_toggle(event.element):
                continue
            return event if event.index not in triggers else None
        if event.type == "check":
            continue
        return None
    return None


@dataclass
class _Step:
    """Um passo que o compilador vai fazer, na simulação de ``analyze_logins``."""

    #: "fill" (digitar), "select", "file", "check", "click", "enter" ou "other".
    kind: str
    event: RecordingEvent
    element: ElementDescriptor | None = None
    key: tuple[Any, ...] | None = None
    inputs: list[InputEvent] = field(default_factory=list)


def _pages(
    events: Sequence[RecordingEvent],
    popup_triggers: set[int],
    following: dict[int, RecordingEvent | None],
) -> list[list[_Step]]:
    """Os passos de cada documento do quadro principal, como o compilador os faz.

    Cada aba tem os seus documentos. Edições seguidas do mesmo campo são um passo
    só; Tab, cliques que só focam um campo, o Enter que o formulário transforma em
    clique e o segundo clique de um clique duplo não viram passo. Navegações no
    mesmo documento não abrem página nova.
    """
    pages: list[list[_Step]] = []
    current: dict[int, list[_Step]] = {}
    for event in events:
        if isinstance(event, NavigationEvent):
            if event.main_frame and (event.page_id not in current or not event.same_document):
                current[event.page_id] = []
                pages.append(current[event.page_id])
            continue
        steps = current.get(event.page_id)
        if steps is None or event.type not in USER_ACTION_TYPES:
            continue
        last = steps[-1] if steps else None
        if isinstance(event, KeyEvent):
            if event.key == "Tab":
                continue
            if event.key != "Enter":
                steps.append(_Step("other", event, event.element))
                continue
            nxt = following.get(event.index)
            if clickable(event.element) or (isinstance(nxt, ClickEvent) and nxt.detail == 0):
                continue
            key = field_key(event.page_id, event.element) if event.element is not None else None
            steps.append(_Step("enter", event, event.element, key))
        elif isinstance(event, ClickEvent):
            element = event.element
            if focus_only(element):
                continue
            if event.index in popup_triggers:
                steps.append(_Step("other", event, element))
                continue
            if (
                event.detail >= 2
                and last is not None
                and last.kind == "click"
                and last.element is not None
                and last.element.css == element.css
            ):
                continue
            steps.append(_Step("click", event, element))
        elif isinstance(event, InputEvent):
            if not event.element.sensitive and event.value is None:
                continue  # o compilador não faz passo sem o valor
            key = field_key(event.page_id, event.element)
            if last is not None and last.kind == "fill" and last.key == key:
                last.inputs.append(event)
            else:
                steps.append(_Step("fill", event, event.element, key, [event]))
        elif isinstance(event, SelectEvent):
            if event.label:  # sem rótulo da opção o compilador não faz passo
                steps.append(_Step("select", event, event.element))
        else:
            steps.append(_Step(event.type, event, getattr(event, "element", None)))
    return pages


@dataclass(frozen=True)
class LoginGroup:
    """Uma senha da gravação, o usuário que vai com ela e o envio."""

    #: Entradas do campo de senha (edições seguidas).
    password: tuple[InputEvent, ...]
    #: Entradas do campo que seria o usuário (vazio: sem usuário).
    username: tuple[InputEvent, ...]
    #: Cliques ou Enter que levaram do usuário à senha (login em duas etapas).
    bridges: tuple[ClickEvent | KeyEvent, ...]
    #: A ação que envia as credenciais (None: não há).
    submit: ClickEvent | KeyEvent | None
    #: Tem a forma de um login (regra conservadora, menos o "nada gravado antes").
    login: bool
    #: Senha de transação ou envio por um botão irreversível: não é login.
    transaction: bool


@dataclass(frozen=True)
class LoginAnalysis:
    """Os logins da gravação, para o gravador e para o compilador."""

    groups: tuple[LoginGroup, ...]
    #: Envio de um código de uso único (clique ou Enter) → para cada outro campo
    #: preenchido na página, os logins (índices em ``groups``) de que ele faz parte.
    one_time_codes: dict[int, tuple[frozenset[int], ...]]


@dataclass(frozen=True)
class _Context:
    events: Sequence[RecordingEvent]
    position_of: dict[int, int]
    popup_triggers: set[int]
    following: dict[int, RecordingEvent | None]


def _group(context: _Context, page: list[_Step], at: int) -> LoginGroup | None:
    """O login da senha no passo ``at`` da página (None: o passo não é senha)."""
    password = page[at]
    element = password.inputs[0].element
    if not (is_password_field(element) or password_by_name(element)):
        return None
    real_password = element.is_password and element.input_type == "password"
    before = [s for s in page[:at] if s.kind != "check"]
    after = [s for s in page[at + 1 :] if s.kind != "check"]
    user: _Step | None = None
    user_after = False
    bridges: list[_Step] = []
    previous = before[-1] if before else None
    if (
        previous is not None
        and previous.kind == "fill"
        and previous.key != password.key
        and previous.element is not None
        and username_field(previous.element)
    ):
        user = previous
    elif (
        previous is not None
        and real_password
        and (
            previous.kind == "enter"
            or (previous.kind == "click" and bridge_button(previous.element))
        )
    ):
        # Duas etapas: o único campo preenchido antes do "Continuar" (ou do Enter nele).
        first = before[0]
        if (
            first.kind == "fill"
            and first.key != password.key
            and first.element is not None
            and username_field(first.element)
            and all(
                (s.kind == "fill" and s.key == first.key)
                or (s.kind == "enter" and s.key == first.key)
                or (s.kind == "click" and bridge_button(s.element))
                for s in before
            )
        ):
            user = first
            bridges = [s for s in before if s.kind != "fill"]
    elif after:
        # A senha veio primeiro: só vale o campo que o script marcou como usuário.
        nxt = after[0]
        if (
            nxt.kind == "fill"
            and nxt.key != password.key
            and nxt.element is not None
            and nxt.element.sensitive
            and nxt.element.is_username
            and username_field(nxt.element)
        ):
            user = nxt
            user_after = True
    last_input = (user.inputs[-1] if user is not None and user_after else password.inputs[-1]).index
    keys = {s.key for s in (password, user) if s is not None and s.key is not None}
    submit = login_submit(
        context.events,
        context.position_of[last_input],
        keys,
        popup_triggers=context.popup_triggers,
        following=context.following,
    )
    limit = submit.index if submit is not None else last_input + 1
    username = tuple(
        entry
        for s in page
        if user is not None and s.kind == "fill" and s.key == user.key
        for entry in s.inputs
        if entry.index < limit
    )
    transaction = is_transaction_secret(element) or (
        isinstance(submit, ClickEvent) and irreversible_click(submit.element)
    )
    login = False
    if user is not None and submit is not None and not transaction:
        if isinstance(submit, ClickEvent):
            sent = login_button(submit.element)
        else:
            sent = submit.element is not None and field_key(submit.page_id, submit.element) in keys
        filled = [s for s in page if s.kind in ("fill", "select", "file") and s.event.index < limit]
        only_credentials = all(
            s.kind == "fill"
            and (s.key in keys or (s.element is not None and s.element.is_one_time_code))
            for s in filled
        )
        login = sent and only_credentials
    return LoginGroup(
        password=tuple(password.inputs),
        username=username,
        bridges=tuple(s.event for s in bridges if isinstance(s.event, ClickEvent | KeyEvent)),
        submit=submit,
        login=login,
        transaction=transaction,
    )


def analyze_logins(events: Sequence[RecordingEvent]) -> LoginAnalysis:
    """Os logins da gravação pela regra conservadora (ver o começo do módulo).

    O gravador usa o resultado para tirar da gravação o valor do usuário; o
    compilador, para decidir que campo vira o usuário do cofre e que envio é
    leitura (ele ainda confere que nada gravou antes).
    """
    following = _following_actions(events)
    context = _Context(
        events=events,
        position_of={event.index: position for position, event in enumerate(events)},
        popup_triggers=_popup_triggers(events),
        following=following,
    )
    groups: list[LoginGroup] = []
    one_time_codes: dict[int, tuple[frozenset[int], ...]] = {}
    for page in _pages(events, context.popup_triggers, following):
        first = len(groups)
        for at, step in enumerate(page):
            if step.kind == "fill":
                group = _group(context, page, at)
                if group is not None:
                    groups.append(group)
        on_page = {g: groups[g] for g in range(first, len(groups))}
        for at, step in enumerate(page):
            if step.kind == "click":
                candidate = login_button(step.element)
            elif step.kind == "enter":
                candidate = step.element is not None and step.element.is_one_time_code
            else:
                candidate = False
            if candidate:
                owners = _code_owners(page[:at], step.event.index, on_page)
                if owners is not None:
                    one_time_codes[step.event.index] = owners
    return LoginAnalysis(groups=tuple(groups), one_time_codes=one_time_codes)


def _code_owners(
    before: list[_Step], index: int, on_page: dict[int, LoginGroup]
) -> tuple[frozenset[int], ...] | None:
    """Envio de código de uso único: só códigos e credenciais de logins já enviados na página.

    Devolve, para cada campo preenchido que não é código, os logins de que ele faz
    parte (o compilador confere que um deles foi confirmado); None quando o envio
    não tem a forma do código de uso único.
    """
    owners: list[frozenset[int]] = []
    has_code = False
    for s in before:
        if s.kind not in ("fill", "select", "file"):
            continue
        if s.kind == "fill" and s.element is not None and s.element.is_one_time_code:
            if is_transaction_secret(s.element):
                return None
            has_code = True
            continue
        if s.kind != "fill":
            return None
        ids = frozenset(
            g
            for g, group in on_page.items()
            if group.login
            and group.submit is not None
            and group.submit.index < index
            and any(
                e.index == mine.index
                for e in s.inputs
                for mine in (*group.username, *group.password)
            )
        )
        if not ids:
            return None
        owners.append(ids)
    return tuple(owners) if has_code else None


@dataclass(frozen=True)
class LoginInputs:
    """O que o gravador tira da gravação ao fechar (a mesma regra do compilador)."""

    #: Usuários de login que ainda têm valor: saem como usuário.
    usernames: list[InputEvent]
    #: Campos antes de uma senha de transação (ou de uma senha enviada por um botão
    #: irreversível): saem da gravação, mas não viram o usuário do cofre.
    hidden: list[InputEvent]


def login_inputs(events: Sequence[RecordingEvent]) -> LoginInputs:
    """Usuários de login (ainda com valor) e campos antes de senhas de transação.

    Um campo só sai como usuário quando o login tem a forma da regra; fora dela
    fica literal (o compilador decide o resto: sem confirmação, um valor que já
    saiu vira entrada obrigatória). O campo antes de uma senha de transação ou de
    um envio irreversível sai sem a marca de usuário, a não ser que a página o
    declare como usuário no ``autocomplete``.
    """
    usernames: dict[int, InputEvent] = {}
    hidden: dict[int, InputEvent] = {}
    for group in analyze_logins(events).groups:
        for entry in group.username:
            if group.transaction and not declared_username(entry.element):
                hidden[entry.index] = entry
            elif group.login and not entry.element.sensitive and entry.index not in hidden:
                usernames[entry.index] = entry
    for index in hidden:
        usernames.pop(index, None)
    return LoginInputs(usernames=list(usernames.values()), hidden=list(hidden.values()))


def username_inputs(events: Sequence[RecordingEvent]) -> list[InputEvent]:
    """Entradas que o gravador tira da gravação como usuário do login."""
    return login_inputs(events).usernames
