"""Compilação determinística de uma gravação num manifesto de robô.

Nenhum modelo de linguagem participa: as regras abaixo transformam a lista de
eventos em passos, escolhem localizadores, classificam o efeito de cada passo
e trocam senha, usuário e código de uso único por referências ao cofre.

Regras principais:

- a primeira navegação vira ``navigate``; as seguintes causadas por clique viram
  ``click.expect_navigation``; as digitadas viram ``navigate``; as feitas pela
  própria página (redirecionamentos) são ignoradas;
- entradas seguidas no mesmo campo viram um ``fill`` com o valor final;
- campo de senha → ``SecretValue(field="password")`` de uma credencial por
  origem; o campo preenchido logo antes, na mesma página, vira o usuário;
  código de uso único → ``SecretValue(field="totp")`` de outra credencial;
- lista → ``select``; caixa de seleção e opção → ``check``; arquivo →
  ``upload`` com uma entrada do tipo arquivo; clique seguido de download →
  ``download``; Enter → ``press``; campo com máscara → ``sequential``.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from rapa.compiler.effects import ActionContext, classify_action, writes_in
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

__all__ = ["CompileError", "CompileResult", "lower"]

_TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", "search", "number", ""})
#: Sessão de servidores Java no caminho: não vale numa nova execução.
_JSESSIONID = re.compile(r";jsessionid=[^?#/]*", re.IGNORECASE)
_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


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
    only_credentials: bool = False
    password_fields: int = 0


@dataclass
class _Document:
    """Uma página carregada (do quadro principal), entre duas navegações."""

    index: int | None
    url: str
    fills: list[InputEvent | SelectEvent | FileEvent] = field(default_factory=list)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _host(url: str) -> str | None:
    host = urlsplit(url).hostname
    return host.lower().rstrip(".") if host else None


def _field_key(event: InputEvent | SelectEvent | CheckEvent | FileEvent) -> tuple[Any, ...]:
    el = event.element
    frames = tuple(f.css or f.id or f.name or "" for f in el.frame_path or [])
    return (event.page_id, frames, el.css, el.id, el.name_attr)


def _focus_only(element: ElementDescriptor) -> bool:
    """Clique que só foca ou ativa um campo (o evento do campo basta)."""
    if element.tag in ("textarea", "select", "option"):
        return True
    if element.tag == "input":
        return element.input_type not in _BUTTON_INPUT_TYPES
    return False


def _clickable(element: ElementDescriptor | None) -> bool:
    if element is None:
        return False
    return (
        element.role in ("button", "link")
        or element.tag in ("a", "button")
        or (element.tag == "input" and element.input_type in _BUTTON_INPUT_TYPES)
    )


def _is_text_field(element: ElementDescriptor) -> bool:
    return (element.tag == "input" and (element.input_type or "") in _TEXT_INPUT_TYPES) or (
        element.tag == "textarea"
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
        self.file_inputs: list[dict[str, Any]] = []
        self.outputs: list[dict[str, Any]] = []
        self.var_names: set[str] = set()
        self.postconditions: list[dict[str, Any]] = []
        self.document = _Document(index=None, url="")
        self.navigated = False
        #: Campos que viraram usuário de uma credencial (regra "logo antes da senha").
        self.username_fields: set[tuple[Any, ...]] = set()
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
        if _focus_only(element):
            return
        if event.index in self.popup_triggers:
            return
        previous = self.drafts[-1] if self.drafts else None
        if (
            event.detail >= 2
            and previous is not None
            and previous.element is not None
            and previous.step["kind"] in ("click", "download")
            and previous.element.css == element.css
        ):
            return  # segundo clique de um clique duplo
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
                    only_credentials=self.only_credentials(),
                    password_fields=self.password_fields(),
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
                only_credentials=self.only_credentials(),
                password_fields=self.password_fields(),
            )
        )

    def only_credentials(self) -> bool:
        """Na página atual a pessoa só preencheu usuário, senha ou código."""
        fills = [f for f in self.document.fills if not isinstance(f, FileEvent)]
        return bool(fills) and all(
            isinstance(f, InputEvent)
            and (f.element.sensitive or _field_key(f) in self.username_fields)
            for f in fills
        )

    def password_fields(self) -> int:
        return len(
            {
                _field_key(f)
                for f in self.document.fills
                if isinstance(f, InputEvent) and f.element.is_password
            }
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

    def merge_target(self, key: tuple[Any, ...], kind: str) -> _Draft | None:
        last = self.drafts[-1] if self.drafts else None
        if last is not None and last.field_key == key and last.step["kind"] == kind:
            return last
        return None

    def on_input(self, event: InputEvent) -> None:
        element = event.element
        self.document.fills.append(event)
        key = _field_key(event)
        url = event.frame_url or self.document.url
        label = element_label(element)
        title = f"Digitar {article(label)} {lower_first(clip_label(label))}"
        if element.is_password:
            credential = self.credential(url, CredentialKind.PASSWORD)
            value: dict[str, Any] = {
                "source": "secret",
                "credential": credential,
                "field": "password",
            }
            self.mark_username(credential, event)
        elif element.is_one_time_code:
            value = {
                "source": "secret",
                "credential": self.credential(url, CredentialKind.TOTP),
                "field": "totp",
            }
        elif element.sensitive:
            value = {
                "source": "secret",
                "credential": self.credential(url, CredentialKind.PASSWORD),
                "field": "username",
            }
        elif event.value is None:
            self.warn(f"O valor do campo {quoted(label)} não foi gravado; o passo ficou de fora.")
            return
        else:
            value = {"source": "text", "template": templates.escape(event.value)}
        masked = event.script_modified or (event.typed is not None and event.typed != event.value)
        existing = self.merge_target(key, "fill")
        if existing is not None:
            existing.step["value"] = value
            existing.step["sequential"] = bool(existing.step.get("sequential")) or masked
            existing.event_index = event.index
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
            )
        )

    def mark_username(self, credential: str, password: InputEvent) -> None:
        """O campo de texto preenchido logo antes da senha, na mesma página, é o usuário."""
        if not self.drafts:
            return
        last = self.drafts[-1]
        element = last.element
        if (
            last.step["kind"] != "fill"
            or element is None
            or last.document_index != self.document.index
            or element.is_password
            or element.is_one_time_code
            or not _is_text_field(element)
            or last.field_key == _field_key(password)
        ):
            return
        last.step["value"] = {"source": "secret", "credential": credential, "field": "username"}
        last.step.pop("sequential", None)
        if last.field_key is not None:
            self.username_fields.add(last.field_key)

    def on_select(self, event: SelectEvent) -> None:
        element = event.element
        self.document.fills.append(event)
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
        existing = self.merge_target(key, "select")
        if existing is not None:
            existing.step["option"] = option
            existing.event_index = event.index
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
            )
        )

    def on_check(self, event: CheckEvent) -> None:
        element = event.element
        key = _field_key(event)
        label = element_label(element)
        verb = "Marcar" if event.checked else "Desmarcar"
        existing = self.merge_target(key, "check")
        if existing is not None:
            existing.step["checked"] = event.checked
            existing.step["title"] = f"{verb} {quoted(label)}"
            existing.event_index = event.index
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
            )
        )

    def on_file(self, event: FileEvent) -> None:
        element = event.element
        self.document.fills.append(event)
        label = element.label or element.name or element.name_attr or "Arquivo"
        taken = {f["name"] for f in self.file_inputs}
        base = name_from(label, "arquivo")
        name = base
        n = 2
        while name in taken:
            name = f"{base}_{n}"
            n += 1
        self.file_inputs.append(
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
            return
        element = event.element
        if event.key == "Enter":
            if _clickable(element):
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
        if element is not None and (event.key == "Enter" or not _clickable(element)):
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
                only_credentials=self.only_credentials(),
                password_fields=self.password_fields(),
            )
        )

    # -- efeitos e verificações ------------------------------------------------

    def classify(self) -> None:
        wrote_before = False
        for draft in self.drafts:
            if draft.action_index is None:
                continue
            kind = draft.step["kind"]
            requests = self.requests_after(draft.action_index)
            if kind in ("click", "download", "press"):
                result = classify_action(
                    ActionContext(
                        element=draft.element,
                        requests=requests,
                        only_credentials_on_page=draft.only_credentials,
                        password_fields_on_page=draft.password_fields,
                        wrote_before=wrote_before,
                        navigated_to_form=self.navigated_to_form(draft.action_index),
                    )
                )
                effect = result.effect
                if (
                    effect is Effect.READ
                    and result.reason.startswith("link")
                    and draft.element is not None
                ):
                    self.warn(
                        f"O link {quoted(element_label(draft.element))} tem um verbo de ação, "
                        "mas só abriu um formulário; ficou como leitura."
                    )
            else:
                # Campo que dispara uma gravação sozinho (por exemplo, salvamento automático).
                effect = Effect.WRITE if writes_in(requests) else Effect.READ
            if effect is not Effect.READ:
                draft.step["effect"] = effect.value
                wrote_before = True

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
        """Título que apareceu depois da ação e não existia antes (confiança alta)."""
        action_index = draft.action_index
        if action_index is None:
            return None, None
        navigation = self.navigation_by_trigger.get(action_index)
        before = self.texts_of(draft.document_index)
        if navigation is not None and not navigation.same_document:
            after = self.texts_of(navigation.index)
        else:
            after = [
                e for e in before if e.after_index is not None and e.after_index >= action_index
            ]
            before = [e for e in before if e.after_index is None or e.after_index < action_index]
        if not after:
            return None, None
        seen = {h.text for e in before for h in e.headings}
        for page in after:
            for heading in sorted(page.headings, key=lambda h: h.level):
                text = " ".join(heading.text.split())
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
        data: dict[str, Any] = {
            "name": name,
            "title": title,
            "description": description,
            "inputs": self.file_inputs,
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
