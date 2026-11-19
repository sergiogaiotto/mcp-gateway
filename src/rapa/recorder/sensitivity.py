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
- campo cujo ``aria-label`` ou ``aria-labelledby`` (ou o nome acessível) fala em
  senha, mas o rótulo não (``password_by_name``): o valor sempre sai da gravação,
  mesmo que o próprio campo também diga ser o usuário e que nada mais no grupo
  seja sigiloso; o campo, ambíguo, nunca é credencial (nem usuário, nem senha) e
  o robô pede o valor numa entrada;
- usuário (``username_by_signal``): só pelo próprio campo, ``autocomplete`` de
  usuário ou e-mail, ``type=email``, ou rótulo, atributo ``name``, id ou
  placeholder com uma palavra inteira de usuário (usuário, user, login, e-mail,
  CPF, CNPJ, matrícula...). O campo cujos textos também falam de outra pessoa ou
  de um cadastro (favorecido, destinatário, beneficiário, pagador, recebedor,
  titular, cliente, contato, convidado, novo, cadastro, chave) não é o usuário.
  Nada mais vira o usuário do cofre;
- página com senha ou código: desde a última navegação que carregou documento (ou
  desde o último login confirmado na página), qualquer outro campo de texto
  preenchido sai da gravação; o compilador o transforma numa entrada
  obrigatória, com aviso (``analyze_logins``, ``secret_page_inputs``). O campo de
  usuário de uma página cujo clique leva (por navegação de documento) a uma
  página onde se preenche uma senha ``type=password`` também sai;
- campo dentro de um shadow root aberto (``in_shadow_root``): nunca é credencial
  de um login (o envio depois dele na página não é autenticação); numa página
  com senha ou código, em qualquer raiz e em qualquer ``<form>``, o valor sai da
  gravação e o compilador pede o valor numa entrada (só o ``type=password`` de um
  componente continua a senha do cofre, no campo dele). O script o identifica
  pelo caminho dos hospedeiros (``shadow_path``) e o localiza por um seletor que
  atravessa os shadow roots abertos ("#hospedeiro > #campo"), além do papel, do
  rótulo, do placeholder e do ``data-testid``, contados no documento e em todos os
  shadow roots abertos, como o Playwright procura. O valor de um campo de texto
  sai no ``change`` do próprio shadow root (o script põe ouvintes de ``change``,
  ``blur`` e ``focusout`` em cada shadow root que aparece no caminho de um evento),
  antes do blur e na ordem da pessoa; se o componente para o ``change``, sai antes
  da próxima ação (``pointerdown``, ``keydown``, ``beforeinput``) ou no ``blur`` na
  fase de captura (da janela e do shadow root), antes do ouvinte de blur do campo;
  se o componente já tirou o campo da página quando o ``change``, o ``blur`` ou o
  ``focusout`` chega (ou quando a janela perde o foco para um iframe), sai o
  descritor guardado no primeiro foco ou input do campo com o último valor que a
  pessoa digitou, com a mesma marca de sigilo (ou mais: com uma senha ou um código
  no documento, o valor só sai como segredo);
- identidade do campo (``field_key``): aba, iframes, hospedeiros, seletor, id,
  nome e tudo o que a pessoa vê ou o compilador usa como localizador (etiqueta,
  tipo, papel, nome acessível, rótulo, placeholder, ``data-testid`` e a chave da
  linha). Para juntar edições, marcações ou escolhas num passo também é preciso o
  mesmo elemento (``same_element``: o número que o script dá a cada elemento do
  DOM), então o alvo e o valor de um passo sempre vêm do mesmo elemento, mesmo
  quando dois campos coincidem em tudo o que se descreve deles (o caminho CSS
  para em seis níveis da luz). Gravações sem esse número ficam só com a descrição.
  O segundo clique de um clique duplo (``second_click``) só some no passo do
  primeiro quando é no mesmo elemento, ou quando um dos dois cliques não gravou
  nada (o robô nunca clica duas vezes onde a pessoa agiu uma vez);
- login (``analyze_logins``): o envio só é autenticação quando todos os campos
  preenchidos desde a última navegação que carregou documento (ou desde o login
  anterior) são credenciais de uma credencial só: o usuário com uma senha
  ``type=password`` de verdade (que não é ``new-password``) e, se houver, o
  código de uso único que não é de transação (usuário e código sem senha nunca é
  login); ou só códigos, e então só com o botão num ``<form>`` cujos campos de
  texto são só códigos, os mesmos que foram preenchidos (sem esse ``<form>``, só
  códigos nunca é login, qualquer que seja o texto do botão). O envio é um clique
  cujo texto inteiro é de login ("Entrar", "Acessar", "Continuar"...; ou
  "Confirmar" quando só códigos foram preenchidos), ou o Enter que aciona esse
  botão no mesmo ``<form>`` (sem ``<form>``, o Enter nunca é login); nenhum verbo
  irreversível e nenhum segredo de transação (token ou código da transação, CVV,
  código de segurança, assinatura) na página. Resíduo, como em d746ee2: só
  códigos num ``<form>`` só com o código continua autenticação mesmo que a página
  use o código para confirmar uma operação; só os campos de texto contam, então
  isso inclui o ``<form>`` do código com o valor num ``<input type=hidden>`` ou
  com uma lista (``<select>``) que a pessoa não mexeu, e dois ``<form>`` iguais só
  com o código na mesma página não se distinguem. Fora isso, só caixas de seleção
  e o botão de mostrar a senha no meio. Nada pode ter gravado antes no robô (a
  mesma conta do compilador: requisição que não é GET de documento, XHR ou fetch,
  ou botão com verbo irreversível); um envio com cara de login que não passa
  nisso não divide a página;
- login em duas etapas: usuário, depois um clique cujo texto inteiro é
  "Continuar", "Avançar", "Próximo", "Seguinte" ou "Next", depois uma senha
  ``type=password`` e um envio que passa na regra acima; só caixas de seleção no
  meio (navegações no mesmo documento não atrapalham);
- login em duas páginas: a mesma forma, com a ponte na primeira página levando
  (por navegação de documento) à página da senha, e nada gravado antes, a não ser
  a própria ponte. Só decide que o campo vira o usuário do cofre: os efeitos da
  ponte e do envio ficam os das requisições deles (na dúvida, gravam);
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
    RequestEvent,
    SelectEvent,
)

__all__ = [
    "BRIDGE_WORDS",
    "BUTTON_INPUT_TYPES",
    "CODE_WORD",
    "EDITING_SLACK_MS",
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
    "in_shadow_root",
    "irreversible_click",
    "irreversible_verb",
    "is_password_field",
    "is_password_like_text",
    "is_secret_field",
    "is_transaction_secret",
    "looks_secret",
    "mentions_password",
    "names_another_party",
    "password_by_name",
    "password_toggle",
    "plain_label",
    "requests_while_editing",
    "same_element",
    "second_click",
    "secret_page_inputs",
    "username_by_signal",
    "writes_in",
]

TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", "search", "number", ""})
BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
#: Tipos em que um rótulo de senha indica senha (e-mail, endereço e busca nunca são).
_PASSWORD_LABEL_TYPES = frozenset({"text", "", "tel", "number"})
#: Tipos de campo que podem ser o usuário de um login.
_USERNAME_TYPES = frozenset({"text", "email", "tel", "number", ""})
#: Tipos em que o ``aria-label`` ou o ``aria-labelledby`` de senha esconde o valor
#: (os do usuário: um campo de e-mail que o nome diz ser senha também é ambíguo).
_NAMED_PASSWORD_TYPES = _PASSWORD_LABEL_TYPES | _USERNAME_TYPES
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
#: Palavra inteira que fala de outra pessoa ou de um cadastro ("CPF do favorecido",
#: "Chave Pix (CPF)", "Login do novo usuário"): o campo não é o usuário do login.
#: Igual a ``OTHER_PARTY_WORDS`` em ``capture.js``.
_OTHER_PARTY_WORDS = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:favorecid[oa]s?|destinatari[oa]s?|beneficiari[oa]s?|pagador(?:es|as?)?"
    r"|recebedor(?:es|as?)?|titular(?:es)?|clientes?|contatos?|convidad[oa]s?|nov[oa]s?"
    r"|cadastr(?:os?|ar|e)|chaves?)"
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


def names_another_party(element: ElementDescriptor) -> bool:
    """Os textos do próprio campo falam de outra pessoa ou de um cadastro.

    "CPF do favorecido", "E-mail do destinatário", "Chave Pix (CPF)", "Login do
    novo usuário": o campo tem palavra de usuário, mas é o dado de outra pessoa (ou
    de quem está sendo cadastrado), nunca o usuário do login. Olha o rótulo, o nome
    acessível, o título, o placeholder, o atributo ``name`` e o id. Igual a
    ``namesAnotherParty`` em ``capture.js``.
    """
    texts = (
        element.label,
        element.name,
        element.title,
        element.placeholder,
        element.name_attr,
        element.id,
    )
    return any(_OTHER_PARTY_WORDS.search(_plain(text)) for text in texts if text)


def username_by_signal(element: ElementDescriptor) -> bool:
    """O próprio campo diz que é o usuário de um login (regra A).

    Campo de texto, e-mail, telefone ou número (nunca área de texto, busca ou
    endereço), que não é senha nem código, com ``autocomplete`` de usuário ou
    e-mail, ``type=email``, ou rótulo, atributo ``name``, id ou placeholder com uma
    palavra inteira de usuário. O texto da página (títulos, parágrafos) não conta.
    Não é o usuário o campo que o nome diz ser senha (``password_by_name``: é
    ambíguo) nem o que fala de outra pessoa ou de um cadastro
    (``names_another_party``). Igual a ``isUsernameField`` em ``capture.js``.
    """
    input_type = element.input_type or ""
    if element.tag != "input" or input_type not in _USERNAME_TYPES:
        return False
    if is_password_field(element) or element.is_one_time_code or password_by_name(element):
        return False
    if names_another_party(element):
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

    O valor sempre sai da gravação (o campo fica sensível), sem a marca de senha: o
    robô pede o valor numa entrada. Vale também para o campo que diz ser o usuário
    (rótulo "CPF", ``type=email``...) e mesmo sozinho no grupo: o campo é ambíguo e
    nunca é credencial de um login. O script manda o sinal à parte
    (``aria_mentions_password``), porque o nome acessível guarda só um dos dois; o
    nome acessível vale para gravações sem o sinal. Igual a ``isPasswordByName`` em
    ``capture.js``.
    """
    if element.tag != "input" or element.is_password or element.is_one_time_code:
        return False
    if (element.input_type or "") not in _NAMED_PASSWORD_TYPES:
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


def in_shadow_root(element: ElementDescriptor) -> bool:
    """O elemento está dentro de um shadow root aberto (o script manda os hospedeiros)."""
    return bool(element.shadow_path)


def credential_kind(element: ElementDescriptor) -> str | None:
    """ "username", "password" (``type=password`` de verdade) ou "code"; None: não é credencial.

    Nenhum campo de uma operação (``is_transaction_secret``) é credencial de login,
    nem a senha nova de um cadastro ou de uma troca de senha
    (``autocomplete="new-password"``), nem campo de dentro de um shadow root (na
    dúvida, o envio grava: o componente pode juntar campos que a página não mostra
    como um login).
    """
    if is_transaction_secret(element) or in_shadow_root(element):
        return None
    if element.is_one_time_code:
        return "code"
    if element.is_password and element.input_type == "password":
        return None if "new-password" in _autocomplete(element) else "password"
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
    """A página declara o campo como usuário (``autocomplete="username"``).

    Menos o campo que o nome diz ser senha, o que fala de outra pessoa ou de um
    cadastro e o de dentro de um shadow root: esses nunca são o usuário do cofre.
    """
    if "username" not in _autocomplete(element) or in_shadow_root(element):
        return False
    return not password_by_name(element) and not names_another_party(element)


def irreversible_click(element: ElementDescriptor | None) -> bool:
    """Botão, link ou item de menu cujo texto tem um verbo irreversível."""
    if element is None:
        return False
    is_control = element.role == "menuitem" or clickable(element)
    return is_control and irreversible_verb(action_label(element)) is not None


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
    """Identidade de um campo na gravação.

    Aba, iframes (com tudo o que os localiza), hospedeiros dos shadow roots,
    seletor, id e nome, e também o que a pessoa vê do campo e o que pode virar
    localizador (etiqueta, tipo, papel, nome acessível, rótulo, placeholder,
    ``data-testid`` e a chave da linha da tabela): dois campos que só
    coincidem no seletor (o mesmo ``#input`` dentro de dois componentes, um caminho
    CSS cortado antes da linha) nunca viram o mesmo campo. Para juntar edições,
    marcações e escolhas num passo só também é preciso o mesmo elemento
    (``same_element``).
    """
    frames = tuple((f.tag, f.id, f.name, f.title, f.src, f.css) for f in element.frame_path or [])
    return (
        page_id,
        frames,
        tuple(element.shadow_path or ()),
        element.css,
        element.id,
        element.name_attr,
        element.tag,
        element.input_type,
        element.role,
        element.name,
        element.label,
        element.placeholder,
        element.test_id,
        element.row.key if element.row is not None else None,
    )


def same_element(a: ElementDescriptor | None, b: ElementDescriptor | None) -> bool:
    """Os dois descritores podem ser do mesmo elemento do DOM.

    O script dá a cada elemento um número (``serial``) que dois elementos nunca
    têm igual: com ele, só o mesmo número é o mesmo elemento. Gravações antigas
    (sem número) ficam só com a descrição (``field_key``).
    """
    if a is None or b is None:
        return a is b
    return a.serial == b.serial


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


def _only_one_time_code(form: FormInfo | None) -> bool:
    """``<form>`` cujos campos de texto são só o código de uso único (sem senha)."""
    return (
        form is not None
        and form.has_one_time_code
        and not form.has_password
        and not form.text_fields
    )


# -- requisições que gravam (a mesma conta do compilador) ---------------------------

_WRITE_TYPES = frozenset({"document", "xhr", "fetch"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Folga no começo da edição de um campo: o aviso do script e a requisição chegam
#: ao gravador por caminhos diferentes. Uma requisição a mais só sobe o efeito.
EDITING_SLACK_MS = 250.0


def writes_in(requests: Sequence[RequestEvent]) -> list[RequestEvent]:
    """Requisições que gravam algo (documento, XHR ou fetch que não é GET)."""
    return [
        r for r in requests if r.resource_type in _WRITE_TYPES and r.method not in _SAFE_METHODS
    ]


def second_click(
    first: ElementDescriptor,
    first_requests: Sequence[RequestEvent],
    second: ElementDescriptor,
    second_requests: Sequence[RequestEvent],
) -> bool:
    """O clique com ``detail`` 2 ou mais é o segundo clique do clique anterior (não vira passo).

    Precisa do mesmo seletor e do mesmo elemento (``same_element``; gravações sem o
    número do elemento ficam só com o seletor). Noutro elemento com o mesmo seletor
    (a linha de baixo que sobe para o lugar da que a pessoa excluiu) o clique é outro
    passo só quando cada um dos dois cliques gravou algo (``writes_in`` nas
    requisições entre ele e a ação seguinte); senão continua absorvido, como antes,
    e as requisições dele contam no passo do primeiro: o robô nunca clica duas vezes
    onde o segundo clique da pessoa não fez nada (um botão que a página recriou
    entre os dois cliques).
    """
    if first.css != second.css:
        return False
    if same_element(first, second):
        return True
    return not (writes_in(first_requests) and writes_in(second_requests))


def requests_while_editing(events: Sequence[RecordingEvent], position: int) -> list[RequestEvent]:
    """Requisições entre o começo da edição do campo ``events[position]`` e o envio do valor.

    O script só manda o valor quando a pessoa deixa o campo; um salvamento
    automático com atraso acontece antes, com o campo ainda em foco. Vale o que
    veio depois da ação anterior da pessoa e na mesma página (o que a navegação
    até ela pediu fica de fora). Sem o começo da edição, nada.
    """
    event = events[position]
    if not isinstance(event, InputEvent) or event.editing_since_ms is None:
        return []
    since = event.editing_since_ms - EDITING_SLACK_MS
    found: list[RequestEvent] = []
    for earlier in reversed(events[:position]):
        if earlier.type in USER_ACTION_TYPES or earlier.t_ms < since:
            break
        if (
            isinstance(earlier, NavigationEvent)
            and earlier.main_frame
            and not earlier.same_document
            and earlier.page_id == event.page_id
        ):
            break
        if isinstance(earlier, RequestEvent):
            found.append(earlier)
    found.reverse()
    return found


def _writing_actions(events: Sequence[RecordingEvent]) -> set[int]:
    """Ações da pessoa que gravam algo, na conta do compilador ou numa conta maior.

    Gravam a ação seguida (até a próxima ação) de uma requisição que grava, o campo
    em cuja edição uma requisição gravou, e o clique ou a tecla num botão, link ou
    item de menu com verbo irreversível. Também contam as ações que o compilador não
    transforma em passo: errar para mais só faz o login não valer (o envio grava).
    """
    writing: set[int] = set()
    last: int | None = None
    for position, event in enumerate(events):
        if event.type in USER_ACTION_TYPES:
            last = event.index
            if writes_in(requests_while_editing(events, position)):
                writing.add(event.index)
            if isinstance(event, ClickEvent | KeyEvent) and irreversible_click(event.element):
                writing.add(event.index)
        elif isinstance(event, RequestEvent) and last is not None and writes_in([event]):
            writing.add(last)
    return writing


# -- passos e documentos, como o compilador os faz ----------------------------------


@dataclass
class _Step:
    """Um passo que o compilador vai fazer, na simulação de ``analyze_logins``."""

    #: "fill" (digitar), "select", "file", "check", "click", "enter" ou "other".
    kind: str
    event: RecordingEvent
    element: ElementDescriptor | None = None
    key: tuple[Any, ...] | None = None
    inputs: list[InputEvent] = field(default_factory=list)


@dataclass
class _Document:
    """Um documento do quadro principal de uma aba e os passos feitos nele."""

    page_id: int
    #: A navegação que carregou o documento.
    navigation: NavigationEvent
    steps: list[_Step] = field(default_factory=list)


def _documents(events: Sequence[RecordingEvent]) -> list[_Document]:
    """Os passos de cada documento do quadro principal, como o compilador os faz.

    Cada aba tem os seus documentos, na ordem em que foram carregados. Edições
    seguidas do mesmo campo (``field_key``) e do mesmo elemento (``same_element``)
    são um passo só; Tab, cliques que só focam um campo, o Enter que o formulário
    transforma em clique e o segundo clique de um clique duplo (``second_click``)
    não viram passo. Navegações no mesmo documento não abrem página nova; o clique
    que abre outra aba não é envio.
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
    #: Requisições entre cada ação da pessoa e a ação seguinte (como no compilador).
    after: dict[int, list[RequestEvent]] = {}
    last_action: int | None = None
    for event in events:
        if event.type in USER_ACTION_TYPES:
            last_action = event.index
            after[event.index] = []
        elif isinstance(event, RequestEvent) and last_action is not None:
            after[last_action].append(event)
    #: Passo de clique → o último clique que ele absorveu (o segundo de um clique duplo).
    latest: dict[int, int] = {}
    documents: list[_Document] = []
    current: dict[int, list[_Step]] = {}
    for event in events:
        if isinstance(event, NavigationEvent):
            if event.main_frame and (event.page_id not in current or not event.same_document):
                document = _Document(page_id=event.page_id, navigation=event)
                current[event.page_id] = document.steps
                documents.append(document)
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
                and second_click(
                    last.element,
                    after.get(latest.get(last.event.index, last.event.index), []),
                    element,
                    after.get(event.index, []),
                )
            ):
                latest[last.event.index] = event.index
                continue
            steps.append(_Step("click", event, element))
        elif isinstance(event, InputEvent):
            if not event.element.sensitive and event.value is None:
                continue  # o compilador não faz passo sem o valor
            key = field_key(event.page_id, event.element)
            if (
                last is not None
                and last.kind == "fill"
                and last.key == key
                and same_element(last.element, event.element)
            ):
                last.inputs.append(event)
            else:
                steps.append(_Step("fill", event, event.element, key, [event]))
        elif isinstance(event, SelectEvent):
            if event.label:  # sem rótulo da opção o compilador não faz passo
                steps.append(_Step("select", event, event.element))
        else:
            steps.append(_Step(event.type, event, getattr(event, "element", None)))
    return documents


@dataclass(frozen=True)
class Login:
    """Um envio que passa na regra do login, com as credenciais que ele leva."""

    submit: ClickEvent
    #: O clique "Continuar" de um login em duas etapas (None: uma etapa só).
    bridge: ClickEvent | None
    username: tuple[InputEvent, ...]
    password: tuple[InputEvent, ...]
    codes: tuple[InputEvent, ...]
    #: Login em duas páginas (a ponte carrega outro documento): só decide que o campo
    #: vira o usuário do cofre; a ponte e o envio não viram autenticação.
    cross_document: bool = False


@dataclass(frozen=True)
class LoginAnalysis:
    """Os logins da gravação e os campos que saem dela, para o gravador e o compilador."""

    logins: tuple[Login, ...]
    #: Entrada → "username" (o usuário pelo próprio campo) ou "other" (outro campo),
    #: quando a página, desde a última navegação ou o último login, pede senha ou
    #: código (ou leva à página que pede a senha).
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


def _fills_of(steps: list[_Step], kind: str) -> bool:
    """Os passos são edições de um campo só, credencial do tipo ``kind``."""
    return (
        bool(steps)
        and len({s.key for s in steps}) == 1
        and all(
            s.kind == "fill" and s.element is not None and credential_kind(s.element) == kind
            for s in steps
        )
    )


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
    bridge = steps[at].event
    if not _fills_of(before, "username") or not _fills_of(after, "password"):
        return None
    return bridge if isinstance(bridge, ClickEvent) else None


def _submit_ok(
    steps: list[_Step], at: int, previous: dict[int, RecordingEvent | None]
) -> ClickEvent | None:
    """O clique no passo ``at``, se ele pode enviar um login (senão, None).

    Botão ou link sem verbo irreversível; do teclado, só pelo envio implícito do
    mesmo ``<form>``; nenhum segredo de transação preenchido antes na página.
    """
    submit = steps[at]
    event = submit.event
    if submit.kind != "click" or not isinstance(event, ClickEvent) or not clickable(event.element):
        return None
    if irreversible_verb(action_label(event.element)) is not None:
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
    return event


def _codes_form(form: FormInfo | None, codes: list[_Step]) -> bool:
    """O botão está num ``<form>`` só de códigos, e os códigos preenchidos são desse ``<form>``.

    Resíduo, como em d746ee2: a página que usa um ``<form>`` só com o código para
    confirmar uma operação continua tendo o envio tratado como autenticação.
    """
    return _only_one_time_code(form) and all(
        s.element is not None and s.element.form == form for s in codes
    )


def _login_at(
    steps: list[_Step], at: int, start: int, previous: dict[int, RecordingEvent | None]
) -> Login | None:
    """O login enviado pelo clique no passo ``at``, olhando os passos depois de ``start``."""
    event = _submit_ok(steps, at, previous)
    if event is None:
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
    # O usuário só com uma senha de verdade (e, se houver, o código); ou só códigos.
    if not passwords and not codes:
        return None
    if bool(users) != bool(passwords):
        return None
    if start >= 0 and (users or passwords):
        return None  # depois de um login na mesma página, só o código
    label = plain_label(action_label(event.element))
    only_codes = not users and not passwords
    # Só códigos: só com o botão no <form> cujos campos de texto são só códigos, os
    # mesmos que a pessoa preencheu, qualquer que seja o texto do botão ("Continuar"
    # depois do login de uma SPA confirma um Pix com o token; sem <form>, nunca é login).
    if only_codes and not _codes_form(event.element.form, codes):
        return None
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


def _previous_document(documents: list[_Document], d: int) -> int | None:
    """O documento que a mesma aba tinha logo antes do documento ``d``."""
    page_id = documents[d].page_id
    return next((k for k in range(d - 1, -1, -1) if documents[k].page_id == page_id), None)


def _cross_document_login(
    documents: list[_Document],
    d: int,
    at: int,
    starts: list[int],
    previous: dict[int, RecordingEvent | None],
) -> Login | None:
    """O login em duas páginas enviado pelo clique no passo ``at`` do documento ``d``.

    A forma exata: o documento anterior da aba só tem o usuário (pelo próprio
    campo, um campo só) e caixas de seleção, e termina no clique cujo texto inteiro
    é de ponte ("Continuar", "Avançar"...), que carregou este documento; aqui, antes
    do envio, só a senha ``type=password`` de verdade (um campo só), caixas de
    seleção e o botão de mostrar a senha, e o envio passa na regra do login.
    Nenhuma das duas páginas teve login antes.
    """
    document = documents[d]
    event = _submit_ok(document.steps, at, previous)
    if event is None or starts[d] != -1:
        return None
    if plain_label(action_label(event.element)) not in LOGIN_WORDS:
        return None
    navigation = document.navigation
    if navigation.cause is not NavigationCause.CLICK or navigation.same_document:
        return None
    first = _previous_document(documents, d)
    if first is None or starts[first] != -1:
        return None
    before = [s for s in documents[first].steps if s.kind != "check"]
    if len(before) < 2:
        return None
    bridge = before[-1]
    bridge_event = bridge.event
    if (
        bridge.kind != "click"
        or not isinstance(bridge_event, ClickEvent)
        or bridge_event.index != navigation.trigger_index
        or not _bridge_button(bridge.element)
        or not _keyboard_submit_ok(bridge_event, previous.get(bridge_event.index))
    ):
        return None
    users = before[:-1]
    after = [
        s
        for s in document.steps[:at]
        if s.kind != "check" and not (s.kind == "click" and password_toggle(s.element))
    ]
    if not _fills_of(users, "username") or not _fills_of(after, "password"):
        return None
    return Login(
        submit=event,
        bridge=bridge_event,
        username=tuple(e for s in users for e in s.inputs),
        password=tuple(e for s in after for e in s.inputs),
        codes=(),
        cross_document=True,
    )


def _scan(
    documents: list[_Document],
    actions: list[RecordingEvent],
    previous: dict[int, RecordingEvent | None],
    writing: set[int],
    failed: set[int],
) -> tuple[list[Login], set[int]]:
    """Os logins, na ordem das ações, com a conta do que gravou antes de cada um.

    Um envio só é login se nada gravou antes no robô (a não ser os logins e as
    pontes deles). O clique de ponte ("Continuar") de um login em duas etapas
    conta como sem gravação enquanto pode ser a ponte (como no compilador); os de
    ``failed`` contam como qualquer outro. No login em duas páginas nada pode ter
    gravado, a não ser a própria ponte. Devolve os logins e as pontes supostas.
    """
    place = {
        s.event.index: (d, at)
        for d, document in enumerate(documents)
        for at, s in enumerate(document.steps)
    }
    starts = [-1] * len(documents)
    logins: list[Login] = []
    assumed: set[int] = set()
    writes: list[int] = []
    for action in actions:
        found = place.get(action.index)
        if found is not None and documents[found[0]].steps[found[1]].kind == "click":
            d, at = found
            steps = documents[d].steps
            if not writes:
                login = _login_at(steps, at, starts[d], previous)
                if login is not None:
                    logins.append(login)
                    starts[d] = at
                    continue
                if _bridge_button(steps[at].element) and action.index not in failed:
                    assumed.add(action.index)
                    continue
            cross = _cross_document_login(documents, d, at, starts, previous)
            if cross is not None and cross.bridge is not None:
                if set(writes) <= {cross.bridge.index}:
                    logins.append(cross)
        if action.index in writing:
            writes.append(action.index)
    return logins, assumed


def _leads_to_secret(documents: list[_Document], d: int) -> bool:
    """O documento ``d`` leva, na mesma aba, a uma página onde se preenche senha ou código.

    Vale o próximo documento da aba em que a pessoa fez alguma coisa, desde que
    nenhum endereço tenha sido digitado no caminho.
    """
    page_id = documents[d].page_id
    for following in documents[d + 1 :]:
        if following.page_id != page_id:
            continue
        if following.navigation.cause is NavigationCause.TYPED:
            return False
        if following.steps:
            return any(
                s.kind == "fill" and s.element is not None and is_secret_field(s.element)
                for s in following.steps
            )
    return False


def _roles(documents: list[_Document], logins: list[Login]) -> dict[int, str]:
    """Os campos que saem da gravação: os de cada trecho com senha ou código.

    Cada documento se divide nos envios dos logins que dão autenticação. Num trecho
    com senha, código ou campo que o nome diz ser senha, os outros campos saem,
    como usuário (pelo próprio campo) ou como outro campo. No último trecho de um
    documento que leva à página da senha (login em duas páginas), o usuário (pelo
    próprio campo) também sai.
    """
    submits = {login.submit.index for login in logins if not login.cross_document}
    roles: dict[int, str] = {}
    for d, document in enumerate(documents):
        start = -1
        windows: list[list[_Step]] = []
        for at, step in enumerate(document.steps):
            if step.kind == "click" and step.event.index in submits:
                windows.append(document.steps[start + 1 : at])
                start = at
        windows.append(document.steps[start + 1 :])
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
        if _leads_to_secret(documents, d):
            for s in windows[-1]:
                if s.kind == "fill" and username_by_signal(s.inputs[0].element):
                    for entry in s.inputs:
                        roles.setdefault(entry.index, "username")
    return roles


def analyze_logins(events: Sequence[RecordingEvent]) -> LoginAnalysis:
    """Os logins da gravação e os campos das páginas com senha (ver o começo do módulo).

    Os logins são os que o compilador confirma: além da forma, nada gravou antes no
    robô, na mesma conta que ele faz (``_writing_actions``, que pode contar a
    mais). Cada página (documento do quadro principal) se divide só nesses envios:
    um envio com cara de login que não vale não divide a página, e os campos depois
    dele continuam na página com senha. Os campos de um trecho com senha, código de
    uso único ou campo que o nome diz ser senha saem da gravação, como usuário (pelo
    próprio campo) ou como outro campo. O gravador usa ``roles`` para tirar os
    valores; o compilador, para decidir que campo vira o usuário do cofre (só num
    login confirmado) e que envio é leitura.
    """
    actions = [e for e in events if e.type in USER_ACTION_TYPES]
    previous: dict[int, RecordingEvent | None] = {
        action.index: actions[i - 1] if i > 0 else None for i, action in enumerate(actions)
    }
    documents = _documents(events)
    writing = _writing_actions(events)
    failed: set[int] = set()
    while True:
        logins, assumed = _scan(documents, actions, previous, writing, failed)
        bridges = {
            login.bridge.index
            for login in logins
            if login.bridge is not None and not login.cross_document
        }
        # Uma ponte suposta que não é de nenhum login conta como qualquer clique: a
        # conta recomeça (cada volta tira pontes, então termina).
        dropped = assumed - bridges
        if not dropped:
            break
        failed |= dropped
    return LoginAnalysis(logins=tuple(logins), roles=_roles(documents, logins))


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
