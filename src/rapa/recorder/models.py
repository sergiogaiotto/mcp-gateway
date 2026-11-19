"""Modelo da gravação de uma demonstração.

A gravação é uma lista ordenada de eventos: ações da pessoa (com o descritor do
elemento), navegações, downloads, requisições e o texto visível das páginas.
É só dado: o compilador (``rapa.compiler``) a transforma num robô sem usar
modelo de linguagem.

Nenhum evento guarda o valor de campos de senha, de código de uso único, do
usuário de login nem dos outros campos de texto de uma página que pede senha ou
código: esses campos chegam marcados como sensíveis e sem valor.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RECORDING_VERSION",
    "REDACTED",
    "USER_ACTION_TYPES",
    "CheckEvent",
    "ClickEvent",
    "DownloadEvent",
    "ElementDescriptor",
    "FileEvent",
    "FormInfo",
    "FrameInfo",
    "Heading",
    "InputEvent",
    "KeyEvent",
    "LocatorCounts",
    "LocatorProbe",
    "NavigationCause",
    "NavigationEvent",
    "PageTextEvent",
    "Recording",
    "RecordingEvent",
    "RequestEvent",
    "RowInfo",
    "SelectEvent",
]

RECORDING_VERSION: Literal[1] = 1
#: Marca que o gravador põe no lugar de valores sensíveis em endereços.
REDACTED = "REMOVIDO"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameInfo(_Model):
    """Um iframe no caminho do elemento, do mais externo ao mais interno."""

    tag: str = Field(default="iframe", max_length=20)
    id: str | None = Field(default=None, max_length=300)
    name: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    src: str | None = Field(default=None, max_length=500)
    css: str | None = Field(default=None, max_length=1000)


class RowInfo(_Model):
    """Linha de tabela que contém o elemento."""

    text: str = Field(max_length=500)
    cells: list[str] = Field(default_factory=list, max_length=20)
    #: Texto de uma célula que só aparece nesta linha da tabela.
    key: str | None = Field(default=None, max_length=200)
    #: Quantos elementos com o mesmo papel e nome existem dentro da linha.
    role_name_count: int | None = None
    #: Quantos elementos com o mesmo texto existem dentro da linha.
    text_count: int | None = None


class FormInfo(_Model):
    """Formulário do elemento (``el.form`` ou o ``<form>`` mais próximo)."""

    action: str | None = Field(default=None, max_length=2000)
    method: str = Field(default="get", max_length=20)
    has_password: bool = False
    has_one_time_code: bool = False
    #: Campos de texto que não são código de uso único.
    text_fields: int = 0


class LocatorCounts(_Model):
    """Quantos elementos do documento casam com cada estratégia (None: não medido)."""

    role_name: int | None = None
    label: int | None = None
    placeholder: int | None = None
    test_id: int | None = None
    text: int | None = None
    id: int | None = None
    name_attr: int | None = None


class LocatorProbe(_Model):
    """Conferência feita pelo Playwright logo depois do evento, se o elemento ainda existia."""

    found: bool
    #: Código Python do localizador sugerido por ``Locator.normalize()``.
    normalized: str | None = Field(default=None, max_length=1000)
    role_name: int | None = None
    label: int | None = None
    placeholder: int | None = None
    text: int | None = None


class ElementDescriptor(_Model):
    """Tudo o que o compilador precisa para localizar o elemento de novo."""

    tag: str = Field(max_length=40)
    input_type: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, max_length=40)
    #: Nome acessível (aria-label, aria-labelledby, rótulo, texto, title ou placeholder).
    name: str | None = Field(default=None, max_length=300)
    label: str | None = Field(default=None, max_length=300)
    placeholder: str | None = Field(default=None, max_length=300)
    test_id: str | None = Field(default=None, max_length=300)
    name_attr: str | None = Field(default=None, max_length=300)
    id: str | None = Field(default=None, max_length=300)
    #: Seletor CSS; dentro de shadow roots abertos, ele os atravessa a partir do
    #: documento ("#hospedeiro > #campo"), como o CSS do Playwright.
    css: str = Field(max_length=1000)
    #: Hospedeiros dos shadow roots abertos em volta do elemento, do mais externo ao
    #: mais interno, cada um pelo caminho dentro da sua raiz (None: fora de shadow
    #: root). Faz parte da identidade do campo: dois componentes iguais (o mesmo id
    #: por dentro) são campos diferentes.
    shadow_path: list[Annotated[str, Field(max_length=1000)]] | None = Field(
        default=None, max_length=16
    )
    #: Número que o script dá a cada elemento do DOM, o mesmo em todos os eventos
    #: dele e nunca o mesmo para dois elementos (None: gravação antiga, ou o script
    #: não deu). Edições, marcações e escolhas só se juntam num passo com o mesmo
    #: número.
    serial: int | None = Field(default=None, ge=0)
    #: Texto visível (nunca o valor digitado num campo).
    text: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    href: str | None = Field(default=None, max_length=500)
    autocomplete: str | None = Field(default=None, max_length=100)
    #: Iframes do topo até o quadro do elemento; None se não deu para descobrir.
    frame_path: list[FrameInfo] | None = None
    is_password: bool = False
    is_one_time_code: bool = False
    is_username: bool = False
    sensitive: bool = False
    #: O ``aria-label`` ou o texto do ``aria-labelledby`` do campo fala em senha (o
    #: script olha os dois, mesmo quando o nome acessível vem só de um deles).
    aria_mentions_password: bool = False
    row: RowInfo | None = None
    form: FormInfo | None = None
    counts: LocatorCounts = Field(default_factory=LocatorCounts)
    probe: LocatorProbe | None = None


class NavigationCause(StrEnum):
    """Por que o quadro principal mudou de endereço."""

    #: Primeira página aberta quando a gravação começou.
    INITIAL = "initial"
    #: A pessoa digitou o endereço (sem clique antes e sem Referer).
    TYPED = "typed"
    CLICK = "click"
    KEY = "key"
    #: A própria página navegou (redirecionamento por script, meta refresh).
    SCRIPT = "script"
    #: Primeira navegação de uma aba aberta pela página.
    POPUP = "popup"


class _EventBase(_Model):
    index: int = Field(ge=0, description="Posição na gravação")
    t_ms: float = Field(ge=0, description="Milissegundos desde o início da gravação")
    page_id: int = Field(ge=0, description="Aba onde o evento aconteceu")
    frame_url: str | None = Field(default=None, max_length=2000)


class ClickEvent(_EventBase):
    type: Literal["click"] = "click"
    element: ElementDescriptor
    #: Contagem de cliques do evento; 0 quando o clique veio do teclado (Enter num formulário).
    detail: int = 1


class InputEvent(_EventBase):
    """Valor final de um campo, enviado no change/blur ou antes da próxima ação."""

    type: Literal["input"] = "input"
    element: ElementDescriptor
    value: str | None = Field(default=None, max_length=4000)
    #: Texto inserido pela pessoa (None se não deu para acompanhar ou se é sensível).
    typed: str | None = Field(default=None, max_length=4000)
    #: A página alterou o valor por script durante a digitação (máscara).
    script_modified: bool = False
    #: Quando a pessoa começou a editar o campo, no mesmo relógio de ``t_ms`` (None se
    #: o script não soube). O valor só sai quando ela deixa o campo; as requisições
    #: feitas enquanto ela digitava (salvamento automático) contam no efeito do passo.
    editing_since_ms: float | None = Field(default=None, ge=0)


class SelectEvent(_EventBase):
    type: Literal["select"] = "select"
    element: ElementDescriptor
    label: str = Field(max_length=500)
    value: str = Field(max_length=500)
    multiple: bool = False


class CheckEvent(_EventBase):
    type: Literal["check"] = "check"
    element: ElementDescriptor
    checked: bool


class FileEvent(_EventBase):
    type: Literal["file"] = "file"
    element: ElementDescriptor
    file_names: list[str] = Field(default_factory=list, max_length=10)


class KeyEvent(_EventBase):
    type: Literal["key"] = "key"
    key: Literal["Enter", "Tab", "Escape"]
    element: ElementDescriptor | None = None


class NavigationEvent(_EventBase):
    type: Literal["navigation"] = "navigation"
    url: str = Field(max_length=2000)
    main_frame: bool = True
    frame_path: list[FrameInfo] | None = None
    cause: NavigationCause = NavigationCause.TYPED
    #: Evento (clique ou tecla) que causou a navegação.
    trigger_index: int | None = None
    #: Mudança de endereço sem carregar documento novo (history API, âncora).
    same_document: bool = False


class DownloadEvent(_EventBase):
    type: Literal["download"] = "download"
    #: Endereço sem a consulta (que pode ter assinatura ou token).
    url: str = Field(max_length=2000)
    suggested_filename: str = Field(max_length=255)
    trigger_index: int | None = None


class RequestEvent(_EventBase):
    """Requisição de documento, XHR/fetch ou qualquer uma que não seja GET."""

    type: Literal["request"] = "request"
    method: str = Field(max_length=20)
    #: Endereço sem a consulta.
    url: str = Field(max_length=2000)
    resource_type: str = Field(max_length=40)
    main_frame: bool = True
    is_navigation: bool = False
    has_referer: bool = False
    redirected: bool = False


class Heading(_Model):
    level: int = Field(ge=1, le=6)
    text: str = Field(max_length=500)


class PageTextEvent(_EventBase):
    """Texto visível de uma página depois que ela assentou."""

    type: Literal["page_text"] = "page_text"
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=500)
    headings: list[Heading] = Field(default_factory=list, max_length=50)
    text: str = Field(default="", max_length=100_000)
    frame_texts: list[str] = Field(default_factory=list, max_length=20)
    #: Último evento antes da captura.
    after_index: int | None = None
    #: Navegação que levou a esta página, se a captura veio de uma navegação.
    navigation_index: int | None = None


type RecordingEvent = Annotated[
    ClickEvent
    | InputEvent
    | SelectEvent
    | CheckEvent
    | FileEvent
    | KeyEvent
    | NavigationEvent
    | DownloadEvent
    | RequestEvent
    | PageTextEvent,
    Field(discriminator="type"),
]

#: Tipos de evento que são ações da pessoa.
USER_ACTION_TYPES = frozenset({"click", "input", "select", "check", "file", "key"})


class Recording(BaseModel):
    """Gravação completa de uma demonstração."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = RECORDING_VERSION
    started_at: datetime
    finished_at: datetime | None = None
    events: list[RecordingEvent] = Field(default_factory=list)
    #: Domínios de outros recursos (imagens, scripts, estilos) usados pelas páginas.
    other_hosts: list[str] = Field(default_factory=list)
    #: Mensagens da página descartadas por não terem a assinatura do gravador.
    dropped_messages: int = 0

    def observed_texts(self) -> set[str]:
        """Textos vistos na gravação: títulos, linhas do texto visível e dos iframes."""
        seen: set[str] = set()
        for event in self.events:
            if not isinstance(event, PageTextEvent):
                continue
            for heading in event.headings:
                seen.add(heading.text)
            if event.title:
                seen.add(event.title)
            for block in (event.text, *event.frame_texts):
                for line in block.splitlines():
                    line = " ".join(line.split())
                    if line:
                        seen.add(line[:500])
        return seen
