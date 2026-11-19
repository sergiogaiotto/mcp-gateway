"""Regras de campos sigilosos e do login, as mesmas no gravador e no compilador.

O script da página (``capture.js``) aplica as regras de campo antes de mandar o
valor; o gravador as repete do lado do Python (segunda camada) e o compilador
também, para gravações que chegaram sem elas. Na dúvida, o passo grava e o valor
fica fora da gravação. As regras:

- campo de texto que é de senha: ``autocomplete`` de senha, ou rótulo
  (``<label>``, ``aria-labelledby`` ou ``aria-label``, nessa ordem, como no
  script), título, placeholder, atributo ``name`` ou id que falam em senha, PIN
  ou password (o script também olha a máscara por CSS,
  ``-webkit-text-security``, que só a página conhece);
- campo cujo ``aria-label`` ou ``aria-labelledby`` fala em senha, mas o rótulo
  não (``password_by_name``): o valor sai da gravação e o robô pede o valor numa
  entrada; nunca desfaz a marca de usuário;
- usuário (``username_by_signal``): só pelo próprio campo, ``autocomplete`` de
  usuário ou e-mail, ``type=email``, ou rótulo, atributo ``name``, id ou
  placeholder com uma palavra inteira de usuário (usuário, user, login, e-mail,
  CPF, CNPJ, matrícula...). Nada mais vira o usuário do cofre;
- página com senha ou código: desde a última navegação que carregou documento (ou
  desde o último envio de login), qualquer outro campo de texto preenchido sai da
  gravação; o compilador o transforma numa entrada obrigatória, com aviso
  (``analyze_logins``, ``secret_page_inputs``);
- login (``analyze_logins``): o envio só é autenticação quando todos os campos
  preenchidos desde a última navegação que carregou documento (ou desde o login
  anterior) são credenciais de uma credencial só (um usuário, uma senha
  ``type=password`` de verdade e o código de uso único que não é de transação),
  com pelo menos uma senha ou código (e, com senha, o usuário); o envio é um
  clique cujo texto inteiro é de login ("Entrar", "Acessar", "Continuar"...; ou
  "Confirmar" quando só códigos foram preenchidos), ou o Enter que aciona esse
  botão no mesmo ``<form>`` (sem ``<form>``, o Enter nunca é login); nenhum
  verbo irreversível e nenhum segredo de transação (token ou código da
  transação, CVV, código de segurança, assinatura) na página. Fora isso, só
  caixas de seleção e o botão de mostrar a senha no meio. O compilador ainda
  exige que nada tenha gravado antes;
- login em duas etapas: usuário, depois um clique cujo texto inteiro é
  "Continuar", "Avançar", "Próximo", "Seguinte" ou "Next", depois uma senha
  ``type=password`` e um envio que passa na regra acima; só caixas de seleção no
  meio (navegações no mesmo documento não atrapalham);
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
    "BRIDGE_WORDS",
    "BUTTON_INPUT_TYPES",
    "CODE_WORD",
    "LOGIN_WORDS",
    "SENSITIVE_TEXT",
    "TEXT_INPUT_TYPES",
    "Login",
    "LoginAnalysis",
    "SecretPageInputs",
    "action_label",
    "analyze_logins",
    "clickable",
    "credential_kind",
    "declared_username",
    "field_key",
    "focus_only",
    "irreversible_verb",
    "is_password_field",
    "is_password_like_text",
    "is_secret_field",
    "is_transaction_secret",
    "looks_secret",
    "mentions_password",
    "password_by_name",
    "password_toggle",
    "plain_label",
    "secret_page_inputs",
    "username_by_signal",
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
#: Palavra inteira de usuário no próprio campo. Igual a ``USERNAME_WORDS`` em ``capture.js``.
_USERNAME_WORDS = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:usuario|user|username|login|e-mail|email|cpf|cnpj|matricula|codigo do usuario)"
    r"(?:[^a-z0-9]|$)"
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
#: Segredo de uma operação, não de login: senha ou token da transação, de pagamento
#: ou do cartão, assinatura eletrônica, PIN, CVV, código de segurança.
_TRANSACTION_WORDS = re.compile(
    r"transac|transfer|assinatura|assinar|autoriza|pagamento|operac|cartao|cvv|cvc"
    r"|codigo[\W_]*(?:de[\W_]*)?seguranca|(?:^|[^a-z0-9])pin(?:[^a-z0-9]|$)"
)
#: Verbos de ação irreversível em pt-BR, sem acentos (emitir, enviar, pagar,
#: transferir, excluir...).
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
#: juntados).
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
    }
)
#: Texto do botão que também envia um login quando só códigos foram preenchidos.
CODE_WORD = "confirmar"
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
    o título, o placeholder, o nome e o id; o ``aria-label`` e o
    ``aria-labelledby`` sozinhos não marcam a senha (ver ``password_by_name``),
    para que as duas camadas nunca discordem.
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


def is_password_field(element: ElementDescriptor) -> bool:
    """Campo de senha: do tipo senha ou, sem outra marca de sigilo, com cara de senha."""
    return element.is_password or (not element.sensitive and is_password_like_text(element))


def username_by_signal(element: ElementDescriptor) -> bool:
    """O próprio campo diz que é o usuário de um login (regra A).

    Campo de texto, e-mail, telefone ou número (nunca área de texto, busca ou
    endereço), que não é senha nem código, com ``autocomplete`` de usuário ou
    e-mail, ``type=email``, ou rótulo, atributo ``name``, id ou placeholder com uma
    palavra inteira de usuário. O texto da página (títulos, parágrafos) não conta.
    Igual a ``isUsernameField`` em ``capture.js``.
    """
    input_type = element.input_type or ""
    if element.tag != "input" or input_type not in _USERNAME_TYPES:
        return False
    if is_password_field(element) or element.is_one_time_code:
        return False
    autocomplete = _autocomplete(element)
    if "username" in autocomplete or "email" in autocomplete or input_type == "email":
        return True
    return any(
        _USERNAME_WORDS.search(_plain(text))
        for text in (element.label, element.name_attr, element.id, element.placeholder)
        if text
    )


def password_by_name(element: ElementDescriptor) -> bool:
    """Campo de texto que só o ``aria-label`` ou o ``aria-labelledby`` diz ser senha.

    O valor sai da gravação (o campo fica sensível), sem a marca de senha: o robô
    pede o valor numa entrada. O script manda o sinal à parte
    (``aria_mentions_password``), porque o nome acessível guarda só um dos dois;
    o nome acessível vale para gravações sem o sinal. Um campo que é o usuário pelo
    próprio campo continua usuário. Igual a ``isPasswordByName`` em ``capture.js``.
    """
    if element.tag != "input" or element.is_password or element.is_one_time_code:
        return False
    if (element.input_type or "") not in _PASSWORD_LABEL_TYPES or username_by_signal(element):
        return False
    named = element.aria_mentions_password or mentions_password(element.name)
    return named and not is_password_like_text(element)


def is_secret_field(element: ElementDescriptor) -> bool:
    """Senha, código de uso único ou campo que o nome diz ser senha."""
    return is_password_field(element) or element.is_one_time_code or password_by_name(element)


def is_transaction_secret(element: ElementDescriptor) -> bool:
    """Campo de uma operação (transação, pagamento, assinatura, PIN, CVV), não de login."""
    texts = (
        element.label,
        element.name,
        element.placeholder,
        element.title,
        element.name_attr,
        element.id,
    )
    return any(_TRANSACTION_WORDS.search(_plain(text)) for text in texts if text)


def credential_kind(element: ElementDescriptor) -> str | None:
    """ "username", "password" (``type=password`` de verdade) ou "code"; None: não é credencial.

    Nenhum campo de uma operação (``is_transaction_secret``) é credencial de login.
    """
    if is_transaction_secret(element):
        return None
    if element.is_one_time_code:
        return "code"
    if element.is_password and element.input_type == "password":
        return "password"
    if username_by_signal(element):
        return "username"
    return None


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


def password_toggle(element: ElementDescriptor | None) -> bool:
    """O botão de mostrar ou esconder a senha ("Mostrar senha"): nunca é o envio."""
    if not clickable(element) or element is None:
        return False
    label = action_label(element)
    if not mentions_password(label) or irreversible_verb(label) is not None:
        return False
    return _TOGGLE_WORDS.search(plain_label(label)) is not None


def _bridge_button(element: ElementDescriptor | None) -> bool:
    """Botão que leva do usuário à senha num login em duas etapas ("Continuar", "Avançar")."""
    return clickable(element) and plain_label(action_label(element)) in BRIDGE_WORDS


@dataclass
class _Step:
    """Um passo que o compilador vai fazer, na simulação de ``analyze_logins``."""

    #: "fill" (digitar), "select", "file", "check", "click", "enter" ou "other".
    kind: str
    event: RecordingEvent
    element: ElementDescriptor | None = None
    key: tuple[Any, ...] | None = None
    inputs: list[InputEvent] = field(default_factory=list)


def _pages(events: Sequence[RecordingEvent]) -> list[list[_Step]]:
    """Os passos de cada documento do quadro principal, como o compilador os faz.

    Cada aba tem os seus documentos. Edições seguidas do mesmo campo são um passo
    só; Tab, cliques que só focam um campo, o Enter que o formulário transforma em
    clique e o segundo clique de um clique duplo não viram passo. Navegações no
    mesmo documento não abrem página nova; o clique que abre outra aba não é envio.
    """
    actions = [e for e in events if e.type in USER_ACTION_TYPES]
    following = {
        action.index: actions[i + 1] if i + 1 < len(actions) else None
        for i, action in enumerate(actions)
    }
    popup_triggers = {
        e.trigger_index
        for e in events
        if isinstance(e, NavigationEvent)
        and e.main_frame
        and e.cause is NavigationCause.POPUP
        and e.trigger_index is not None
    }
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
                continue  # o clique de ativação (ou de envio implícito) vem em seguida
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
class Login:
    """Um envio que passa na regra do login, com as credenciais que ele leva."""

    submit: ClickEvent
    #: O clique "Continuar" de um login em duas etapas (None: uma etapa só).
    bridge: ClickEvent | None
    username: tuple[InputEvent, ...]
    password: tuple[InputEvent, ...]
    codes: tuple[InputEvent, ...]


@dataclass(frozen=True)
class LoginAnalysis:
    """Os logins da gravação e os campos que saem dela, para o gravador e o compilador."""

    logins: tuple[Login, ...]
    #: Entrada → "username" (o usuário pelo próprio campo) ou "other" (outro campo),
    #: quando a página, desde a última navegação ou o último login, pede senha ou código.
    roles: dict[int, str]


def _keyboard_submit_ok(click: ClickEvent, previous: RecordingEvent | None) -> bool:
    """O clique veio do teclado (``detail`` 0) só vale se for o envio implícito do mesmo form.

    O Enter num campo só conta quando o navegador clica no botão do mesmo
    ``<form>`` (o envio implícito); o Enter (ou o espaço) no próprio botão é um
    clique nele.
    """
    if click.detail != 0 or not isinstance(previous, KeyEvent) or previous.key != "Enter":
        return True
    if clickable(previous.element):
        return True
    field_form = previous.element.form if previous.element is not None else None
    return field_form is not None and click.element.form == field_form


def _two_step(window: list[_Step]) -> ClickEvent | None:
    """O clique do login em duas etapas: usuário, "Continuar", senha de verdade.

    Só caixas de seleção podem ficar no meio; depois da senha, o botão de mostrar
    a senha. Devolve o clique, ou None quando a forma não é essa.
    """
    steps = [s for s in window if s.kind != "check"]
    bridges = [n for n, s in enumerate(steps) if s.kind == "click" and _bridge_button(s.element)]
    if len(bridges) != 1:
        return None
    at = bridges[0]
    before, after = steps[:at], steps[at + 1 :]
    while after and after[-1].kind == "click" and password_toggle(after[-1].element):
        after.pop()
    if not before or not after:
        return None
    if len({s.key for s in before}) != 1 or len({s.key for s in after}) != 1:
        return None
    user_ok = all(
        s.kind == "fill" and s.element is not None and credential_kind(s.element) == "username"
        for s in before
    )
    password_ok = all(
        s.kind == "fill" and s.element is not None and credential_kind(s.element) == "password"
        for s in after
    )
    bridge = steps[at].event
    return bridge if user_ok and password_ok and isinstance(bridge, ClickEvent) else None


def _login_at(
    steps: list[_Step], at: int, start: int, previous: dict[int, RecordingEvent | None]
) -> Login | None:
    """O login enviado pelo clique no passo ``at``, olhando os passos depois de ``start``."""
    submit = steps[at]
    event = submit.event
    element = submit.element
    if submit.kind != "click" or not isinstance(event, ClickEvent) or not clickable(element):
        return None
    if irreversible_verb(action_label(element)) is not None:
        return None
    if not _keyboard_submit_ok(event, previous.get(event.index)):
        return None
    if any(
        s.kind in ("fill", "select", "file")
        and s.element is not None
        and is_transaction_secret(s.element)
        for s in steps[:at]
    ):
        return None
    window = steps[start + 1 : at]
    found: dict[str, list[_Step]] = {"username": [], "password": [], "code": []}
    extra = False
    for s in window:
        if s.kind == "check" or (s.kind == "click" and password_toggle(s.element)):
            continue
        kind = credential_kind(s.element) if s.kind == "fill" and s.element is not None else None
        if kind is None:
            extra = True
        else:
            found[kind].append(s)
    if any(len({s.key for s in group}) > 1 for group in found.values()):
        return None
    users, passwords, codes = found["username"], found["password"], found["code"]
    if not passwords and not codes:
        return None
    if passwords and not users:
        return None
    if start >= 0 and (users or passwords):
        return None  # depois de um login na mesma página, só o código
    label = plain_label(action_label(element))
    only_codes = not users and not passwords
    if label not in LOGIN_WORDS and not (only_codes and label == CODE_WORD):
        return None
    bridge = _two_step(window) if extra else None
    if extra and (bridge is None or codes):
        return None
    return Login(
        submit=event,
        bridge=bridge,
        username=tuple(e for s in users for e in s.inputs),
        password=tuple(e for s in passwords for e in s.inputs),
        codes=tuple(e for s in codes for e in s.inputs),
    )


def analyze_logins(events: Sequence[RecordingEvent]) -> LoginAnalysis:
    """Os logins da gravação e os campos das páginas com senha (ver o começo do módulo).

    Cada página (documento do quadro principal) se divide nos envios de login: os
    campos de um trecho com senha, código de uso único ou campo que o nome diz ser
    senha saem da gravação, como usuário (pelo próprio campo) ou como outro campo.
    O gravador usa ``roles`` para tirar os valores; o compilador, para decidir que
    campo vira o usuário do cofre (só num login confirmado) e que envio é leitura
    (ele ainda confere que nada gravou antes).
    """
    actions = [e for e in events if e.type in USER_ACTION_TYPES]
    previous: dict[int, RecordingEvent | None] = {
        action.index: actions[i - 1] if i > 0 else None for i, action in enumerate(actions)
    }
    logins: list[Login] = []
    roles: dict[int, str] = {}
    for steps in _pages(events):
        start = -1
        windows: list[list[_Step]] = []
        for at, step in enumerate(steps):
            login = _login_at(steps, at, start, previous) if step.kind == "click" else None
            if login is not None:
                logins.append(login)
                windows.append(steps[start + 1 : at])
                start = at
        windows.append(steps[start + 1 :])
        for window in windows:
            fields = [s.inputs[0].element for s in window if s.kind == "fill"]
            if not any(is_secret_field(element) for element in fields):
                continue
            for s in window:
                element = s.inputs[0].element if s.kind == "fill" else None
                if element is None or is_secret_field(element):
                    continue
                role = "username" if username_by_signal(element) else "other"
                for entry in s.inputs:
                    roles[entry.index] = role
    return LoginAnalysis(logins=tuple(logins), roles=roles)


@dataclass(frozen=True)
class SecretPageInputs:
    """O que o gravador tira da gravação ao fechar (a mesma regra do compilador)."""

    #: Usuários pelo próprio campo numa página com senha ou código.
    usernames: list[InputEvent]
    #: Os outros campos preenchidos nessa página: saem sem a marca de usuário.
    others: list[InputEvent]


def secret_page_inputs(events: Sequence[RecordingEvent]) -> SecretPageInputs:
    """Entradas que ainda têm valor e saem da gravação por estarem numa página com senha."""
    roles = analyze_logins(events).roles
    found: dict[str, list[InputEvent]] = {"username": [], "other": []}
    for event in events:
        if isinstance(event, InputEvent) and event.index in roles and not event.element.sensitive:
            found[roles[event.index]].append(event)
    return SecretPageInputs(usernames=found["username"], others=found["other"])
