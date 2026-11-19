"""Compilação determinística de uma gravação num manifesto de robô.

Nenhum modelo de linguagem participa: as regras abaixo transformam a lista de
eventos em passos, escolhem localizadores, classificam o efeito de cada passo
e trocam senha, usuário e código de uso único por referências ao cofre.

Regras principais:

- a primeira navegação vira ``navigate``; as seguintes causadas por clique viram
  ``click.expect_navigation``; as digitadas viram ``navigate``; as feitas pela
  própria página (redirecionamentos) são ignoradas;
- entradas seguidas no mesmo campo (a identidade de
  ``rapa.recorder.sensitivity.field_key``, com os hospedeiros dos shadow roots, o
  que a pessoa vê do campo e o que vira localizador, como o ``data-testid``) e no
  mesmo elemento (``same_element``: o número que o script dá a cada elemento, quando
  a gravação tem), no mesmo documento, viram um ``fill`` com o valor final;
  edições, marcações e escolhas de elementos diferentes nunca se juntam, então o
  alvo e o valor de um passo sempre vêm do mesmo elemento; o segundo clique de um
  clique duplo só some no passo do primeiro no mesmo elemento ou quando um dos
  dois cliques não gravou nada (``second_click``); as requisições feitas
  enquanto a pessoa editava o campo, depois de cada edição e depois do Tab que
  deixa o campo contam no efeito do passo, como num salvamento automático;
- campo de senha → ``SecretValue(field="password")`` de uma credencial por
  origem; código de uso único → ``SecretValue(field="totp")`` de outra
  credencial;
- usuário e envio do login pela regra de
  ``rapa.recorder.sensitivity.analyze_logins``: o envio só é leitura quando só
  credenciais de uma credencial foram preenchidas, o botão é de login e nada
  gravou antes no robô (a mesma conta do gravador, confirmada na classificação);
  o campo que é o usuário pelo próprio campo vira o usuário do cofre só num login
  confirmado (ou quando a página o declara no ``autocomplete``). No login em duas
  páginas o usuário também vira o do cofre, mas a ponte e o envio ficam com o
  efeito das requisições deles. Qualquer outro campo de texto de uma página com
  senha ou código, o usuário de um login não confirmado e o campo de um shadow
  root (nunca credencial de login) viram uma entrada obrigatória com aviso, nunca
  um literal nem um segredo do cofre;
- campo de texto com cara de senha (rótulo, nome, id ou placeholder de senha,
  ``rapa.recorder.sensitivity``) é tratado como senha, e o valor gravado é
  descartado; o que só o ``aria-label`` ou o ``aria-labelledby`` diz ser senha
  perde o valor e vira uma entrada; outro literal com cara de segredo (cartão,
  token, chave), ou com um valor sensível apagado pela limpeza da gravação
  (``REMOVIDO``), vira uma entrada obrigatória do robô, sem o valor digitado;
- lista → ``select``; caixa de seleção e opção → ``check``; arquivo →
  ``upload`` com uma entrada do tipo arquivo; clique seguido de download →
  ``download``; Enter → ``press``; campo com máscara → ``sequential``.
"""

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from rapa.compiler.effects import AUTHENTICATION, ActionContext, classify_action
from rapa.compiler.generalize import ParameterCandidate, generalize_inputs
from rapa.compiler.locators import (
    BuiltTarget,
    TargetError,
    build_target,
    element_label,
    element_noun,
)
from rapa.compiler.naming import (
    IdAllocator,
    article,
    clip_label,
    lower_first,
    name_from,
    page_phrase,
    quoted,
)
from rapa.core import policy, templates
from rapa.core.robot import CredentialKind, Effect, Manifest
from rapa.recorder.models import (
    REDACTED,
    USER_ACTION_TYPES,
    CheckEvent,
    ClickEvent,
    DownloadEvent,
    ElementDescriptor,
    FileEvent,
    InputEvent,
    KeyEvent,
    NavigationCause,
    NavigationEvent,
    PageTextEvent,
    Recording,
    RecordingEvent,
    RequestEvent,
    SelectEvent,
)
from rapa.recorder.sensitivity import (
    Login,
    analyze_logins,
    clickable,
    declared_username,
    field_key,
    focus_only,
    is_password_like_text,
    looks_secret,
    password_by_name,
    requests_while_editing,
    same_element,
    second_click,
    writes_in,
)

__all__ = ["CompileError", "CompileResult", "lower"]

#: Sessão de servidores Java no caminho: não vale numa nova execução.
_JSESSIONID = re.compile(r";jsessionid=[^?#/]*", re.IGNORECASE)


class CompileError(ValueError):
    """A gravação não pôde virar um robô válido (mensagem em pt-BR)."""


class CompileResult(BaseModel):
    """Resultado da compilação."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: Manifest
    #: Id do passo → índice do evento da gravação que o originou.
    provenance: dict[str, int]
    #: Valores literais que podem virar entradas (para ``rapa.core.edits.Parametrize``).
    parameter_candidates: list[ParameterCandidate]
    #: Textos vistos na gravação (para ``apply_edits(observed_texts=...)``).
    observed_texts: set[str]
    #: Avisos em pt-BR para a pessoa que revisa o robô.
    warnings: list[str]


@dataclass
class _Draft:
    step: dict[str, Any]
    event_index: int
    id_base: str
    element: ElementDescriptor | None = None
    field_key: tuple[Any, ...] | None = None
    #: Ação da pessoa usada para classificar o efeito (None: não classificar).
    action_index: int | None = None
    document_index: int | None = None
    #: Todas as ações que o passo absorveu (a primeira é ``action_index``): edições
    #: seguidas do mesmo campo, o Tab que deixa o campo, o segundo clique de um
    #: clique duplo. As requisições depois de cada uma contam no efeito.
    absorbed: list[int] = field(default_factory=list)
    #: Entrada criada para um valor que não fica no robô (``Lowering.secret_input``).
    secret_input: str | None = None
    #: Usuário de login que espera a confirmação do envio (``Lowering.finish_logins``).
    pending: bool = False
    #: Última entrada do campo (já sem o valor, se ele era sigiloso) e o endereço dela.
    source: InputEvent | None = None
    url: str = ""
    #: Documento carregado (navegação do quadro principal que não é no mesmo
    #: documento) em que o passo nasceu: edições só se juntam no mesmo.
    load: int = 0

    def __post_init__(self) -> None:
        if self.action_index is not None and self.action_index not in self.absorbed:
            self.absorbed.insert(0, self.action_index)


@dataclass
class _Document:
    """Uma página carregada (do quadro principal), entre duas navegações."""

    index: int | None
    url: str


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _host(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.lower().rstrip(".") if host else None


def _field_key(event: InputEvent | SelectEvent | CheckEvent | FileEvent) -> tuple[Any, ...]:
    return field_key(event.page_id, event.element)


def _secret_page_warning(label: str, name: str) -> str:
    """Aviso do campo preenchido numa página com senha ou código, que virou a entrada ``name``."""
    return (
        f"O campo {quoted(label)} foi preenchido numa página que pede senha ou código; para "
        "não confundir o dado com uma credencial, não foi tratado como usuário do login, o "
        f"valor digitado não entrou no robô e virou a entrada {name}."
    )


def _login_warning(label: str, name: str) -> str:
    """Aviso do campo de usuário de um login que não foi confirmado."""
    return (
        f"O campo {quoted(label)} parecia o usuário de um login, mas o envio não passou na "
        "regra do login (botão de login, só credenciais na página e nada gravado antes); "
        "não foi tratado como usuário do cofre, o valor digitado não entrou no robô e virou "
        f"a entrada {name}."
    )


def _named_password_warning(label: str, name: str) -> str:
    """Aviso do campo que só o ``aria-label`` ou o ``aria-labelledby`` diz ser senha."""
    return (
        f"O nome do campo {quoted(label)} fala em senha; o valor digitado não entrou no "
        f"robô e virou a entrada {name}."
    )


def _redacted_warning(label: str, name: str) -> str:
    """Aviso do campo cujo valor tinha um dado sensível apagado na gravação."""
    return (
        f"O valor digitado no campo {quoted(label)} tinha um dado sensível (usuário, senha "
        "ou código) que foi apagado da gravação; o valor não entrou no robô e virou a "
        f"entrada {name}."
    )


class _Lowering:
    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.events: list[RecordingEvent] = list(recording.events)
        indices = [e.index for e in self.events]
        if any(b <= a for a, b in pairwise(indices)):
            raise CompileError("Os eventos da gravação estão fora de ordem.")
        self.by_index: dict[int, RecordingEvent] = {e.index: e for e in self.events}
        self.drafts: list[_Draft] = []
        self.draft_by_event: dict[int, _Draft] = {}
        self.warnings: list[str] = []
        self.hosts: list[str] = []
        self.credentials: dict[tuple[str, CredentialKind], str] = {}
        self.credential_names: set[str] = set()
        #: Entradas do robô: arquivos enviados e literais com cara de segredo.
        self.inputs: list[dict[str, Any]] = []
        #: Campo → entrada criada para o literal com cara de segredo digitado nele.
        self.secret_inputs: dict[tuple[Any, ...], str] = {}
        self.outputs: list[dict[str, Any]] = []
        self.var_names: set[str] = set()
        self.postconditions: list[dict[str, Any]] = []
        self.document = _Document(index=None, url="")
        #: Documentos carregados até aqui em cada aba (o mesmo corte de ``analyze_logins``).
        self.loads: dict[int, int] = {}
        self.navigated = False
        #: Logins da gravação (pela regra), os campos das páginas com senha e os
        #: logins que a classificação confirmou (pelo índice do envio).
        self.logins = analyze_logins(self.events)
        self.confirmed: set[int] = set()
        #: Todos os logins pelo envio; os que dão autenticação (os de uma página e os
        #: em duas etapas na mesma página) e os em duas páginas, que só dão o usuário.
        self.all_logins: dict[int, Login] = {
            login.submit.index: login for login in self.logins.logins
        }
        self.login_by_submit = {
            index: login for index, login in self.all_logins.items() if not login.cross_document
        }
        self.login_by_bridge = {
            login.bridge.index: login
            for login in self.login_by_submit.values()
            if login.bridge is not None
        }
        self.cross_logins = [login for login in self.logins.logins if login.cross_document]
        #: Entrada do usuário → envios dos logins de que ela faz parte.
        self.username_submits: dict[int, set[int]] = {}
        for login in self.logins.logins:
            for entry in login.username:
                self.username_submits.setdefault(entry.index, set()).add(login.submit.index)
        user_actions = [e for e in self.events if e.type in USER_ACTION_TYPES]
        self.action_indices = [e.index for e in user_actions]
        self.next_action: dict[int, RecordingEvent | None] = {}
        for i, action in enumerate(user_actions):
            self.next_action[action.index] = (
                user_actions[i + 1] if i + 1 < len(user_actions) else None
            )
        #: Requisições entre cada ação da pessoa e a ação seguinte.
        self.requests_by_action: dict[int, list[RequestEvent]] = {}
        last_action: int | None = None
        for event in self.events:
            if event.type in USER_ACTION_TYPES:
                last_action = event.index
                self.requests_by_action[event.index] = []
            elif isinstance(event, RequestEvent) and last_action is not None:
                self.requests_by_action[last_action].append(event)
        #: Requisições feitas enquanto a pessoa editava o campo, antes de o valor sair
        #: (a mesma conta de ``rapa.recorder.sensitivity``).
        self.requests_while_editing: dict[int, list[RequestEvent]] = {
            event.index: requests_while_editing(self.events, position)
            for position, event in enumerate(self.events)
            if isinstance(event, InputEvent) and event.editing_since_ms is not None
        }
        self.downloads: dict[int, DownloadEvent] = {}
        self.navigation_by_trigger: dict[int, NavigationEvent] = {}
        self.popup_triggers: set[int] = set()
        for event in self.events:
            if isinstance(event, DownloadEvent) and event.trigger_index is not None:
                self.downloads.setdefault(event.trigger_index, event)
            elif isinstance(event, NavigationEvent) and event.main_frame:
                if event.trigger_index is None:
                    continue
                if event.cause is NavigationCause.POPUP:
                    self.popup_triggers.add(event.trigger_index)
                elif event.cause in (NavigationCause.CLICK, NavigationCause.KEY):
                    self.navigation_by_trigger.setdefault(event.trigger_index, event)

    # -- utilidades ------------------------------------------------------------

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_host(self, url: str) -> None:
        host = _host(url)
        if host and host not in self.hosts:
            self.hosts.append(host)

    def emit(self, draft: _Draft) -> _Draft:
        self.drafts.append(draft)
        self.draft_by_event[draft.event_index] = draft
        return draft

    def target(self, element: ElementDescriptor, *, prefer_label: bool = False) -> dict[str, Any]:
        intent = f"{element_noun(element)} {quoted(element_label(element))}"
        try:
            built: BuiltTarget = build_target(element, intent, prefer_label=prefer_label)
        except TargetError as exc:
            raise CompileError(str(exc)) from None
        for message in built.warnings:
            self.warn(message)
        if element.frame_path is None:
            self.warn(
                f"Não foi possível descobrir em que iframe está {intent}; o robô procura na "
                "página principal."
            )
        data = built.target.model_dump(mode="json", exclude_none=True)
        if built.target.within is not None and element.row is not None and element.row.key:
            data["intent"] = f"{intent} da linha {quoted(element.row.key, 40)}"[:300]
        return data

    def requests_after(self, action_index: int) -> list[RequestEvent]:
        return self.requests_by_action.get(action_index, [])

    def requests_of(self, action_index: int) -> list[RequestEvent]:
        """Requisições que contam no efeito de uma ação: durante a edição e depois dela."""
        during = self.requests_while_editing.get(action_index, [])
        return during + self.requests_after(action_index)

    def credential(self, url: str, kind: CredentialKind) -> str:
        origin = _origin(url)
        key = (origin, kind)
        name = self.credentials.get(key)
        if name is not None:
            return name
        host = _host(url) or "portal"
        labels = [p for p in host.split(".") if p and p != "www"]
        base = name_from(labels[0] if labels else "portal", "portal")
        if kind is CredentialKind.TOTP:
            base = f"{base}_otp"
        name = base
        n = 2
        while name in self.credential_names:
            name = f"{base}_{n}"
            n += 1
        self.credentials[key] = name
        self.credential_names.add(name)
        return name

    # -- eventos ---------------------------------------------------------------

    def run(self) -> None:
        for event in self.events:
            if not self.navigated and event.type in USER_ACTION_TYPES:
                self.warn("Ações feitas antes de abrir a primeira página ficaram de fora do robô.")
                continue
            match event:
                case NavigationEvent():
                    self.on_navigation(event)
                case ClickEvent():
                    self.on_click(event)
                case InputEvent():
                    self.on_input(event)
                case SelectEvent():
                    self.on_select(event)
                case CheckEvent():
                    self.on_check(event)
                case FileEvent():
                    self.on_file(event)
                case KeyEvent():
                    self.on_key(event)
                case DownloadEvent():
                    self.add_host(event.url)
                    if event.trigger_index is None or event.trigger_index not in self.by_index:
                        self.warn(
                            f"O download {quoted(event.suggested_filename)} não veio de um "
                            "clique gravado e ficou de fora do robô."
                        )
                case _:
                    pass
        if not self.navigated:
            raise CompileError("A gravação não abriu nenhuma página.")

    def on_navigation(self, event: NavigationEvent) -> None:
        self.add_host(event.url)
        if not event.main_frame:
            return
        if not event.same_document:
            self.loads[event.page_id] = self.loads.get(event.page_id, 0) + 1
        self.document = _Document(index=event.index, url=event.url)
        url = _JSESSIONID.sub("", event.url)
        navigate = {"kind": "navigate", "url": templates.escape(url)}
        phrase, slug = page_phrase(url)
        title = f"Abrir {phrase}"
        if REDACTED in url and event.cause is not NavigationCause.SCRIPT:
            self.warn(f"O endereço {url} tinha um valor sensível que foi removido na gravação.")
        if not self.navigated:
            self.navigated = True
            self.emit(_Draft({**navigate, "title": title}, event.index, f"abrir_{slug}"))
            return
        if event.cause in (NavigationCause.CLICK, NavigationCause.KEY):
            trigger = (
                self.draft_by_event.get(event.trigger_index)
                if event.trigger_index is not None
                else None
            )
            if trigger is not None:
                if trigger.step["kind"] == "click" and not event.same_document:
                    trigger.step["expect_navigation"] = True
                return
            self.warn(
                f"A navegação para {event.url} veio de uma ação que não virou passo; "
                "o robô abre o endereço diretamente."
            )
        elif event.cause is NavigationCause.SCRIPT:
            return
        elif event.cause is NavigationCause.POPUP:
            self.warn(
                "A gravação abriu uma nova aba; o robô segue na mesma aba, abrindo o "
                "endereço diretamente."
            )
        self.emit(_Draft({**navigate, "title": title}, event.index, f"abrir_{slug}"))

    def on_click(self, event: ClickEvent) -> None:
        element = event.element
        if focus_only(element):
            return
        if event.index in self.popup_triggers:
            return
        previous = self.drafts[-1] if self.drafts else None
        if (
            event.detail >= 2
            and previous is not None
            and previous.element is not None
            and previous.step["kind"] in ("click", "download")
            and second_click(
                previous.element,
                self.requests_after(previous.absorbed[-1]),
                element,
                self.requests_after(event.index),
            )
        ):
            # Segundo clique de um clique duplo: o que ele gravou conta no mesmo passo.
            previous.absorbed.append(event.index)
            return
        nxt = self.next_action.get(event.index)
        if (
            isinstance(nxt, FileEvent)
            and not self.requests_after(event.index)
            and event.index not in self.navigation_by_trigger
        ):
            # Botão que só abre o seletor de arquivos: o upload basta.
            return
        label = element_label(element)
        download = self.downloads.get(event.index)
        if download is not None:
            into = self.var_name(download.suggested_filename)
            save_as = templates.escape(download.suggested_filename)
            self.emit(
                _Draft(
                    {
                        "kind": "download",
                        "title": f"Baixar o arquivo {quoted(download.suggested_filename)}",
                        "target": self.target(element),
                        "save_as": save_as,
                        "into": into,
                    },
                    event.index,
                    f"baixar_{download.suggested_filename.rsplit('.', 1)[0]}",
                    element=element,
                    action_index=event.index,
                    document_index=self.document.index,
                )
            )
            self.outputs.append(
                {
                    "name": into,
                    "title": clip_label(f"Arquivo {download.suggested_filename}", 200),
                    "type": "file",
                    "source": f"vars.{into}",
                }
            )
            return
        self.emit(
            _Draft(
                {
                    "kind": "click",
                    "title": f"Clicar em {quoted(label)}",
                    "target": self.target(element),
                },
                event.index,
                f"clicar_{label}",
                element=element,
                action_index=event.index,
                document_index=self.document.index,
            )
        )

    def var_name(self, filename: str) -> str:
        base = name_from(filename.rsplit(".", 1)[0], "arquivo")
        name = base
        n = 2
        while name in self.var_names:
            name = f"{base}_{n}"
            n += 1
        self.var_names.add(name)
        return name

    def merge_target(
        self, key: tuple[Any, ...], kind: str, page_id: int, element: ElementDescriptor
    ) -> _Draft | None:
        """O passo anterior, se for do mesmo campo (``field_key``), elemento, tipo e documento.

        Só edições, marcações ou escolhas seguidas do mesmo elemento (o mesmo
        ``serial`` do script, quando a gravação tem, ``same_element``) viram um passo
        só: o alvo e o valor do passo sempre vêm do mesmo campo.
        """
        last = self.drafts[-1] if self.drafts else None
        if (
            last is not None
            and last.field_key == key
            and same_element(last.element, element)
            and last.step["kind"] == kind
            and last.load == self.loads.get(page_id, 0)
        ):
            return last
        return None

    def input_name(self, label: str, fallback: str) -> str:
        """Nome livre para uma nova entrada do robô."""
        taken = {f["name"] for f in self.inputs}
        base = name_from(label, fallback)
        name = base
        n = 2
        while name in taken:
            name = f"{base}_{n}"
            n += 1
        return name

    def hide_password_like(self, event: InputEvent) -> InputEvent:
        """Senha num campo que não é do tipo senha (mascarado por CSS, rótulo "Senha").

        O gravador já marca esses campos; aqui é a segunda camada, para gravações
        que chegaram sem a marca: o valor gravado é descartado e o campo passa a
        ser tratado como senha (credencial do cofre e regra do usuário).
        """
        element = event.element
        if element.sensitive or not is_password_like_text(element):
            return event
        self.warn(
            f"O campo {quoted(element_label(element))} parece de senha, mas não é do tipo "
            "senha; o valor gravado foi descartado e o robô digita a senha do cofre."
        )
        hidden = element.model_copy(update={"is_password": True, "sensitive": True})
        return event.model_copy(
            update={"element": hidden, "value": None, "typed": None, "script_modified": False}
        )

    def secret_input(
        self, key: tuple[Any, ...], label: str, warning: Callable[[str], str] | None = None
    ) -> str:
        """Entrada obrigatória para um valor que não fica no robô (segredo, campo de login).

        O valor digitado na demonstração não entra no robô: quem chama informa o
        valor a cada execução.
        """
        name = self.secret_inputs.get(key)
        if name is None:
            name = self.input_name(label, "valor")
            self.inputs.append(
                {
                    "name": name,
                    "title": clip_label(label, 200),
                    "description": clip_label(
                        f"Valor digitado no campo {quoted(label)}; parece sigiloso e não foi "
                        "guardado no robô.",
                        1000,
                    ),
                    "type": "string",
                    "required": True,
                }
            )
            self.secret_inputs[key] = name
            self.warn(
                warning(quoted(name))
                if warning is not None
                else f"O campo {quoted(label)} parece guardar um dado sigiloso; o valor "
                f"digitado não entrou no robô e virou a entrada {quoted(name)}."
            )
        return name

    def hide_password_by_name(self, event: InputEvent) -> InputEvent:
        """Campo que só o ``aria-label`` ou o ``aria-labelledby`` diz ser senha (segunda camada).

        O valor gravado é descartado; o campo continua sem a marca de senha e vira
        uma entrada obrigatória (nunca um segredo do cofre).
        """
        element = event.element
        if element.sensitive or not password_by_name(element):
            return event
        hidden = element.model_copy(update={"sensitive": True})
        return event.model_copy(
            update={"element": hidden, "value": None, "typed": None, "script_modified": False}
        )

    def literal(
        self, event: InputEvent, key: tuple[Any, ...], label: str
    ) -> tuple[dict[str, Any], str | None]:
        """Valor de um campo comum: o literal, ou uma entrada se ele tem cara de segredo.

        Um valor em que a limpeza da gravação apagou um dado sensível (``REMOVIDO``)
        também vira entrada: o robô nunca digita a marca no sistema.
        """
        text = event.value or ""
        if REDACTED in text:
            name = self.secret_input(key, label, partial(_redacted_warning, label))
        elif looks_secret(event.element):
            name = self.secret_input(key, label)
        else:
            return {"source": "text", "template": templates.escape(text)}, None
        return {"source": "text", "template": f"{{{{ inputs.{name} }}}}"}, name

    def hidden_input(
        self, key: tuple[Any, ...], label: str, warning: Callable[[str, str], str]
    ) -> tuple[dict[str, Any], str]:
        """Campo cujo valor saiu da gravação e não é credencial: vira entrada obrigatória."""
        name = self.secret_input(key, label, partial(warning, label))
        return {"source": "text", "template": f"{{{{ inputs.{name} }}}}"}, name

    def on_input(self, event: InputEvent) -> None:
        event = self.hide_password_like(event)
        event = self.hide_password_by_name(event)
        element = event.element
        key = _field_key(event)
        url = event.frame_url or self.document.url
        label = element_label(element)
        title = f"Digitar {article(label)} {lower_first(clip_label(label))}"
        secret_input: str | None = None
        pending = False
        role = self.logins.roles.get(event.index)
        value: dict[str, Any]
        if element.is_password:
            value = {
                "source": "secret",
                "credential": self.credential(url, CredentialKind.PASSWORD),
                "field": "password",
            }
        elif element.is_one_time_code:
            value = {
                "source": "secret",
                "credential": self.credential(url, CredentialKind.TOTP),
                "field": "totp",
            }
        elif declared_username(element) and (element.sensitive or role == "username"):
            # A própria página declara o campo como usuário (autocomplete="username").
            value = {
                "source": "secret",
                "credential": self.credential(url, CredentialKind.PASSWORD),
                "field": "username",
            }
        elif role == "username":
            # Depende do envio do login: ``finish_logins`` decide depois da classificação.
            pending = True
            value = {"source": "text", "template": ""}
        elif role == "other" or element.sensitive:
            # Campo de uma página com senha ou código (ou que o gravador escondeu) que
            # não é credencial: quem chama informa o valor.
            if password_by_name(element):
                warning = _named_password_warning
            elif element.is_username and role is None:
                warning = _login_warning
            else:
                warning = _secret_page_warning
            value, secret_input = self.hidden_input(key, label, warning)
        elif event.value is None:
            self.warn(f"O valor do campo {quoted(label)} não foi gravado; o passo ficou de fora.")
            return
        else:
            value, secret_input = self.literal(event, key, label)
        masked = event.script_modified or (event.typed is not None and event.typed != event.value)
        existing = self.merge_target(key, "fill", event.page_id, event.element)
        if existing is not None:
            existing.step["value"] = value
            existing.step["sequential"] = bool(existing.step.get("sequential")) or masked
            existing.event_index = event.index
            existing.absorbed.append(event.index)
            existing.secret_input = secret_input
            existing.pending = pending
            existing.source = event
            existing.url = url
            self.draft_by_event[event.index] = existing
            return
        step: dict[str, Any] = {
            "kind": "fill",
            "title": title,
            "target": self.target(element),
            "value": value,
        }
        if masked and value["source"] == "text":
            step["sequential"] = True
        self.emit(
            _Draft(
                step,
                event.index,
                f"digitar_{label}",
                element=element,
                field_key=key,
                action_index=event.index,
                document_index=self.document.index,
                load=self.loads.get(event.page_id, 0),
                secret_input=secret_input,
                pending=pending,
                source=event,
                url=url,
            )
        )

    def finish_logins(self) -> None:
        """Usuário dos logins, depois que a classificação confirmou os envios.

        Num login confirmado, o campo vira o usuário do cofre, com a credencial da
        senha que vai com ele. Sem confirmação o cofre nunca é digitado nele: o valor,
        que o gravador tira da gravação, vira uma entrada obrigatória com aviso.
        """
        for draft in self.drafts:
            source = draft.source
            if not draft.pending or source is None or draft.field_key is None:
                continue
            submits = {s for i in draft.absorbed for s in self.username_submits.get(i, ())}
            confirmed = sorted(submits & self.confirmed)
            secret_input: str | None = None
            if confirmed:
                # A credencial da senha que vai com o usuário (mesma origem, em geral).
                url = draft.url
                for submit in confirmed:
                    login = self.all_logins[submit]
                    owner = (
                        self.draft_by_event.get(login.password[0].index) if login.password else None
                    )
                    if owner is not None and owner.url:
                        url = owner.url
                        break
                value: dict[str, Any] = {
                    "source": "secret",
                    "credential": self.credential(url, CredentialKind.PASSWORD),
                    "field": "username",
                }
                draft.step.pop("sequential", None)
            else:
                label = element_label(source.element)
                value, secret_input = self.hidden_input(draft.field_key, label, _login_warning)
            draft.step["value"] = value
            draft.secret_input = secret_input
            draft.pending = False

    def on_select(self, event: SelectEvent) -> None:
        element = event.element
        key = _field_key(event)
        if event.multiple:
            self.warn(
                f"Na lista {quoted(element_label(element))} várias opções foram escolhidas; "
                "o robô escolhe só a primeira."
            )
        if not event.label:
            self.warn(f"A opção escolhida em {quoted(element_label(element))} não tem rótulo.")
            return
        option = templates.escape(event.label)
        existing = self.merge_target(key, "select", event.page_id, event.element)
        if existing is not None:
            existing.step["option"] = option
            existing.event_index = event.index
            existing.absorbed.append(event.index)
            self.draft_by_event[event.index] = existing
            return
        label = element_label(element)
        self.emit(
            _Draft(
                {
                    "kind": "select",
                    "title": f"Escolher {article(label)} {lower_first(clip_label(label))}",
                    "target": self.target(element),
                    "option": option,
                },
                event.index,
                f"escolher_{label}",
                element=element,
                field_key=key,
                action_index=event.index,
                document_index=self.document.index,
                load=self.loads.get(event.page_id, 0),
            )
        )

    def on_check(self, event: CheckEvent) -> None:
        element = event.element
        key = _field_key(event)
        label = element_label(element)
        verb = "Marcar" if event.checked else "Desmarcar"
        existing = self.merge_target(key, "check", event.page_id, event.element)
        if existing is not None:
            existing.step["checked"] = event.checked
            existing.step["title"] = f"{verb} {quoted(label)}"
            existing.event_index = event.index
            existing.absorbed.append(event.index)
            self.draft_by_event[event.index] = existing
            return
        self.emit(
            _Draft(
                {
                    "kind": "check",
                    "title": f"{verb} {quoted(label)}",
                    "target": self.target(element),
                    "checked": event.checked,
                },
                event.index,
                f"{verb.lower()}_{label}",
                element=element,
                field_key=key,
                action_index=event.index,
                document_index=self.document.index,
                load=self.loads.get(event.page_id, 0),
            )
        )

    def on_file(self, event: FileEvent) -> None:
        element = event.element
        label = element.label or element.name or element.name_attr or "Arquivo"
        name = self.input_name(label, "arquivo")
        self.inputs.append(
            {
                "name": name,
                "title": clip_label(label, 200),
                "description": clip_label(f"Arquivo enviado no campo {quoted(label)}.", 1000),
                "type": "file",
                "required": True,
            }
        )
        if len(event.file_names) > 1:
            self.warn(
                f"Vários arquivos foram enviados em {quoted(label)}; o robô recebe um só arquivo."
            )
        self.emit(
            _Draft(
                {
                    "kind": "upload",
                    "title": f"Anexar o arquivo em {quoted(label)}",
                    "target": self.target(element, prefer_label=True),
                    "file_input": name,
                },
                event.index,
                f"anexar_{label}",
                element=element,
                action_index=event.index,
                document_index=self.document.index,
            )
        )

    def on_key(self, event: KeyEvent) -> None:
        if event.key == "Tab":
            self.absorb_tab(event)
            return
        element = event.element
        if event.key == "Enter":
            if clickable(element):
                return  # o clique de ativação vem em seguida
            nxt = self.next_action.get(event.index)
            if isinstance(nxt, ClickEvent) and nxt.detail == 0:
                return  # envio implícito do formulário: o clique no botão fica
        key_name = "Enter" if event.key == "Enter" else "Escape"
        step: dict[str, Any] = {
            "kind": "press",
            "key": key_name,
            "title": f"Pressionar {key_name}",
        }
        if element is not None and (event.key == "Enter" or not clickable(element)):
            label = element_label(element)
            step["title"] = f"Pressionar {key_name} em {quoted(label)}"
            step["target"] = self.target(element)
        self.emit(
            _Draft(
                step,
                event.index,
                f"pressionar_{key_name}",
                element=element,
                action_index=event.index,
                document_index=self.document.index,
            )
        )

    def absorb_tab(self, event: KeyEvent) -> None:
        """Tab não vira passo, mas deixa o campo: o change/blur pode gravar algo.

        As requisições depois do Tab passam a contar no efeito do passo do campo
        que a pessoa deixou (o último passo, de digitar, escolher ou marcar, no
        mesmo campo e na mesma página). Em qualquer outro caso ficam sem dono;
        nunca vão para um clique ou tecla.
        """
        last = self.drafts[-1] if self.drafts else None
        if (
            last is None
            or event.element is None
            or last.step["kind"] not in ("fill", "select", "check")
            or last.field_key is None
            or last.field_key != field_key(event.page_id, event.element)
            or last.document_index != self.document.index
        ):
            return
        last.absorbed.append(event.index)

    # -- efeitos e verificações ------------------------------------------------

    def classify(self) -> None:
        """Efeito de cada passo e logins confirmados, depois o usuário do login.

        O envio de um login pela regra é leitura quando nada gravou antes. O
        "Continuar" de um login em duas etapas também é, supondo que o envio se
        confirme; se não se confirmar (algo gravou entre os dois), a classificação
        recomeça sem o login: cair só sobe efeitos, então termina.
        """
        failed: set[int] = set()
        while True:
            confirmed, notes, assumed, writers = self.classify_pass(failed)
            dropped = assumed - confirmed
            if not dropped:
                break
            failed |= dropped
        self.confirmed = confirmed | self.confirm_cross_logins(writers)
        for message in notes:
            self.warn(message)
        self.finish_logins()

    def confirm_cross_logins(self, writers: list[int]) -> set[int]:
        """Os logins em duas páginas em que nada gravou antes do envio, a não ser a ponte.

        ``writers``: as ações (na ordem dos passos) dos passos que gravaram. A ponte e
        o envio ficam com o efeito que tiveram; o login só decide o usuário do cofre.
        """
        position = {index: n for n, draft in enumerate(self.drafts) for index in draft.absorbed}
        confirmed: set[int] = set()
        for login in self.cross_logins:
            bridge = login.bridge.index if login.bridge is not None else None
            submit = position.get(login.submit.index)
            if bridge is None or submit is None or bridge not in position:
                continue
            before = {position[index] for index in writers if position[index] < submit}
            if before <= {position[bridge]}:
                confirmed.add(login.submit.index)
        return confirmed

    def classify_pass(self, failed: set[int]) -> tuple[set[int], list[str], set[int], list[int]]:
        """Uma passada da classificação, sem contar as pontes dos logins ``failed``.

        Devolve os envios de login confirmados, os avisos, os envios cuja ponte
        ("Continuar") foi tratada como autenticação e as ações dos passos que gravaram.
        """
        confirmed: set[int] = set()
        assumed: set[int] = set()
        notes: list[str] = []
        writers: list[int] = []
        wrote_before = False
        for draft in self.drafts:
            draft.step.pop("effect", None)
            if draft.action_index is None:
                continue
            kind = draft.step["kind"]
            # Requisições depois de cada ação que o passo absorveu: mais requisições
            # só podem subir o efeito (leitura → gravação).
            requests = [r for index in draft.absorbed for r in self.requests_of(index)]
            if kind in ("click", "download", "press"):
                index = draft.action_index
                authentication = False
                if not wrote_before and kind == "click":
                    bridge_of = self.login_by_bridge.get(index)
                    if index in self.login_by_submit:
                        authentication = True
                    elif bridge_of is not None and bridge_of.submit.index not in failed:
                        authentication = True
                        assumed.add(bridge_of.submit.index)
                result = classify_action(
                    ActionContext(
                        element=draft.element,
                        requests=requests,
                        navigated_to_form=self.navigated_to_form(index),
                        authentication=authentication,
                    )
                )
                effect = result.effect
                if result.reason == AUTHENTICATION and index in self.login_by_submit:
                    confirmed.add(index)
                if (
                    effect is Effect.READ
                    and result.reason.startswith("link")
                    and draft.element is not None
                ):
                    notes.append(
                        f"O link {quoted(element_label(draft.element))} tem um verbo de ação, "
                        "mas só abriu um formulário; ficou como leitura."
                    )
            else:
                # Campo que dispara uma gravação sozinho (por exemplo, salvamento automático).
                effect = Effect.WRITE if writes_in(requests) else Effect.READ
            if effect is not Effect.READ:
                draft.step["effect"] = effect.value
                wrote_before = True
                writers.append(draft.action_index)
        return confirmed, notes, assumed, writers

    def navigated_to_form(self, action_index: int) -> bool:
        navigation = self.navigation_by_trigger.get(action_index)
        if navigation is None or navigation.same_document:
            return False
        position = bisect_right(self.action_indices, navigation.index)
        if position >= len(self.action_indices):
            return False
        following = self.by_index[self.action_indices[position]]
        return following.type in ("input", "select", "check", "file")

    def texts_of(self, document_index: int | None) -> list[PageTextEvent]:
        return [
            e
            for e in self.events
            if isinstance(e, PageTextEvent) and e.navigation_index == document_index
        ]

    def add_assertions(self) -> None:
        result: list[_Draft] = []
        last_assert: tuple[_Draft, str] | None = None
        for position, draft in enumerate(self.drafts):
            result.append(draft)
            effect = draft.step.get("effect", "read")
            if effect == "read" or draft.action_index is None:
                continue
            heading, source = self.new_heading(draft)
            if heading is None or source is None:
                continue
            label = element_label(draft.element) if draft.element is not None else "a ação"
            assertion = _Draft(
                {
                    "kind": "assert",
                    "title": clip_label(f"Conferir que a página mostra {quoted(heading)}", 200),
                    "condition": {"op": "text_contains", "text": templates.escape(heading)},
                    "message": clip_label(
                        f"A página não mostrou {quoted(heading)} depois de {quoted(label)}.",
                        300,
                    ),
                },
                source.index,
                f"conferir_{heading}",
            )
            result.append(assertion)
            if position == len(self.drafts) - 1:
                last_assert = (assertion, heading)
        self.drafts = result
        if last_assert is not None:
            self.postconditions.append(
                {"op": "text_contains", "text": templates.escape(last_assert[1])}
            )

    def new_heading(self, draft: _Draft) -> tuple[str | None, PageTextEvent | None]:
        """Título que apareceu depois da ação e não existia antes (confiança alta).

        Só valem as capturas feitas antes da próxima ação da pessoa (depois da
        última que o passo absorveu): o que aparece depois dela é obra dela, e a
        verificação não pode passar na frente. Título com um valor sigiloso apagado
        pela limpeza da gravação (``REMOVIDO``) não serve.
        """
        action_index = draft.action_index
        if action_index is None:
            return None, None
        following = self.next_action.get(max(draft.absorbed, default=action_index))
        navigation = self.navigation_by_trigger.get(action_index)
        before = self.texts_of(draft.document_index)
        if navigation is not None and not navigation.same_document:
            after = self.texts_of(navigation.index)
        else:
            after = [
                e for e in before if e.after_index is not None and e.after_index >= action_index
            ]
            before = [e for e in before if e.after_index is None or e.after_index < action_index]
        if following is not None:
            after = [e for e in after if e.index < following.index]
        if not after:
            return None, None
        seen = {h.text for e in before for h in e.headings}
        for page in after:
            for heading in sorted(page.headings, key=lambda h: h.level):
                text = " ".join(heading.text.split())
                # Um título com um valor sigiloso apagado nunca aparece assim na página.
                if REDACTED in text:
                    continue
                if heading.level <= 2 and 3 <= len(text) <= 200 and text not in seen:
                    return text, page
        return None, None

    # -- manifesto -------------------------------------------------------------

    def build(self, name: str, title: str, description: str) -> tuple[Manifest, dict[str, int]]:
        allocator = IdAllocator()
        provenance: dict[str, int] = {}
        steps: list[dict[str, Any]] = []
        for draft in self.drafts:
            step_id = allocator.allocate(draft.id_base)
            draft.step["id"] = step_id
            provenance[step_id] = draft.event_index
            steps.append(draft.step)
        credentials = [
            {
                "name": cred_name,
                "kind": kind.value,
                "origin": origin,
                "description": (
                    "Usuário e senha" if kind is CredentialKind.PASSWORD else "Código de uso único"
                )
                + f" de {urlsplit(origin).hostname}",
            }
            for (origin, kind), cred_name in self.credentials.items()
        ]
        # Entrada de literal com cara de segredo cujo campo virou usuário do login: sobra.
        used = {draft.secret_input for draft in self.drafts}
        unused = set(self.secret_inputs.values()) - used
        data: dict[str, Any] = {
            "name": name,
            "title": title,
            "description": description,
            "inputs": [spec for spec in self.inputs if spec["name"] not in unused],
            "outputs": self.outputs,
            "credentials": credentials,
            "allowed_domains": self.hosts,
            "steps": steps,
            "postconditions": self.postconditions,
        }
        try:
            manifest = Manifest.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise CompileError(
                f"A gravação gerou um robô inválido ({where}): {first['msg']}"
            ) from None
        return manifest, provenance


def lower(recording: Recording, *, name: str, title: str, description: str) -> CompileResult:
    """Compila a gravação num manifesto de robô, sem modelo de linguagem.

    Levanta ``CompileError`` (mensagem em pt-BR) quando a gravação não pode virar
    um robô válido, por exemplo sem nenhuma página aberta ou com uma credencial
    num endereço sem https.
    """
    lowering = _Lowering(recording)
    lowering.run()
    lowering.classify()
    lowering.add_assertions()
    for origin, _ in lowering.credentials:
        if not (origin.startswith("https://") or (_host(origin) or "").endswith(policy.TEST_TLD)):
            raise CompileError(
                f"A página {origin} pede senha sem https; o RaPA não guarda credenciais "
                "para endereços sem TLS."
            )
    manifest, provenance = lowering.build(name, title, description)
    extra_hosts = sorted(set(recording.other_hosts) - set(manifest.allowed_domains))
    if extra_hosts:
        shown = ", ".join(extra_hosts[:5]) + ("…" if len(extra_hosts) > 5 else "")
        lowering.warn(
            f"As páginas também usam recursos de outros domínios ({shown}), que não entram "
            "nos domínios permitidos do robô."
        )
    for violation in policy.check(manifest):
        lowering.warn(f"Política: {violation.message}")
    return CompileResult(
        manifest=manifest,
        provenance=provenance,
        parameter_candidates=generalize_inputs(manifest),
        observed_texts=recording.observed_texts(),
        warnings=lowering.warnings,
    )
