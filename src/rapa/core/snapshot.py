"""Retrato da página no passo que quebrou, para o conserto assistido.

Quando um passo falha porque o alvo não foi encontrado (ou foi encontrado mais de
uma vez), o executor tira um retrato **só de leitura** dos elementos da página que
poderiam ser o alvo: papel, nome acessível, rótulo, placeholder, texto visível,
``data-testid``, id e atributo ``name``, o iframe e a linha de tabela em volta, e
quantos elementos do documento casam com cada um desses textos (para o conserto
montar localizadores únicos sem abrir o navegador de novo). Nada do retrato age na
página: ele só lê o DOM, depois que o passo já falhou.

O retrato nunca leva o valor de um campo (nem o ``value`` de um ``<input>``, nem a
opção escolhida de uma lista), nem segredos: os valores das credenciais e das
entradas da execução saem de todo texto (trocados por ``REMOVIDO``), o endereço vai
sem a consulta e sem o fragmento, e cada elemento leva as marcas de sigilo do
gravador (``rapa.recorder.sensitivity``). Tudo tem limite de tamanho: o retrato vem
da página, que não é confiável, e o plano de controle o confere de novo ao receber.

O retrato viaja no resultado da execução (``RunResult.failure_snapshot``) e fica na
tabela ``run_snapshots``, só para execuções que falharam num desses códigos.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "KIND_ROLES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SNAPSHOT_ELEMENTS",
    "MAX_SNAPSHOT_FRAMES",
    "REDACTED",
    "SNAPSHOT_ERROR_CODES",
    "SNAPSHOT_VERSION",
    "TEXT_ROLES",
    "PageSnapshot",
    "SnapshotCounts",
    "SnapshotElement",
    "SnapshotFrame",
    "SnapshotFrameInfo",
    "SnapshotRow",
    "snapshot_roles",
]

SNAPSHOT_VERSION: Literal[1] = 1
#: Marca que troca valores sigilosos nos textos do retrato (a mesma do gravador: o
#: compilador nunca monta localizador com ela).
REDACTED = "REMOVIDO"
#: Códigos de erro do executor que levam ao retrato (os de ``rapa.runner.errors``).
SNAPSHOT_ERROR_CODES = frozenset(
    {"elemento_nao_encontrado", "elemento_ambiguo", "localizador_alternativo_em_gravacao"}
)
MAX_SNAPSHOT_ELEMENTS = 150
MAX_SNAPSHOT_FRAMES = 8
#: Maior retrato guardado (a forma JSON): o executor corta elementos para caber, e o
#: plano de controle (e a migração 0005) recusam o que passar disso.
MAX_SNAPSHOT_BYTES = 1024 * 1024

#: Papéis que podem ser o alvo de cada tipo de passo.
KIND_ROLES: dict[str, frozenset[str]] = {
    "click": frozenset(
        {
            "button",
            "link",
            "menuitem",
            "menuitemcheckbox",
            "menuitemradio",
            "tab",
            "option",
            "checkbox",
            "radio",
            "switch",
            "treeitem",
        }
    ),
    "download": frozenset({"button", "link", "menuitem"}),
    "fill": frozenset({"textbox", "searchbox", "spinbutton", "combobox"}),
    "select": frozenset({"combobox", "listbox"}),
    "check": frozenset({"checkbox", "radio", "switch"}),
    "upload": frozenset({"button"}),
    "press": frozenset({"textbox", "searchbox", "spinbutton", "combobox", "button", "link"}),
    "extract_table": frozenset({"table", "grid"}),
}
#: Papéis com texto para ler ou esperar (``extract_text``, ``wait_for``, condições).
TEXT_ROLES = frozenset(
    {
        "heading",
        "status",
        "alert",
        "cell",
        "gridcell",
        "rowheader",
        "columnheader",
        "listitem",
        "link",
        "button",
        "img",
        "dialog",
        "region",
    }
)


def snapshot_roles(kind: str) -> frozenset[str]:
    """Papéis que o retrato junta para um passo do tipo ``kind``."""
    return KIND_ROLES.get(kind, TEXT_ROLES)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


type _Text = Annotated[str, Field(max_length=300)]
type _Count = Annotated[int, Field(ge=0, le=1_000_000)]


class SnapshotFrameInfo(_Model):
    """Um iframe no caminho do quadro, do mais externo ao mais interno."""

    tag: str = Field(default="iframe", max_length=20)
    id: str | None = Field(default=None, max_length=300)
    name: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    #: Só o caminho do endereço do iframe (sem consulta nem fragmento).
    src: str | None = Field(default=None, max_length=500)


class SnapshotFrame(_Model):
    """Um quadro da página: o principal (caminho vazio) ou um iframe."""

    path: list[SnapshotFrameInfo] = Field(default_factory=list, max_length=4)
    #: Origem e caminho do documento do quadro (sem consulta nem fragmento).
    url: str = Field(default="", max_length=500)


class SnapshotRow(_Model):
    """A linha de tabela em volta do elemento."""

    text: str = Field(default="", max_length=500)
    cells: list[_Text] = Field(default_factory=list, max_length=20)
    #: Texto de uma célula que só esta linha visível tem (comparado inteiro).
    key: str | None = Field(default=None, max_length=200)
    #: Quantos elementos com o mesmo papel e nome há dentro da linha.
    role_name_count: _Count | None = None
    #: Quantos elementos com o mesmo texto há dentro da linha.
    text_count: _Count | None = None


class SnapshotCounts(_Model):
    """Quantos elementos do documento casam com cada estratégia (None: não medido)."""

    role_name: _Count | None = None
    label: _Count | None = None
    placeholder: _Count | None = None
    test_id: _Count | None = None
    text: _Count | None = None
    id: _Count | None = None
    name_attr: _Count | None = None


class SnapshotElement(_Model):
    """Um elemento que pode ser o alvo do passo, sem nenhum valor de campo."""

    #: Quadro do elemento (posição em ``PageSnapshot.frames``).
    frame: int = Field(default=0, ge=0, lt=MAX_SNAPSHOT_FRAMES)
    tag: str = Field(max_length=40)
    input_type: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, max_length=40)
    name: _Text | None = None
    label: _Text | None = None
    placeholder: _Text | None = None
    test_id: _Text | None = None
    name_attr: _Text | None = None
    id: _Text | None = None
    title: _Text | None = None
    #: Texto visível (nunca o valor digitado num campo).
    text: _Text | None = None
    autocomplete: str | None = Field(default=None, max_length=100)
    #: Dentro de um shadow root aberto.
    in_shadow: bool = False
    enabled: bool = True
    #: Senha (``type=password`` ou máscara por CSS), pelo script da página.
    is_password: bool = False
    #: Código de uso único, pelo script da página.
    is_one_time_code: bool = False
    #: Campo sigiloso pelas regras do gravador (senha, código, segredo de operação).
    sensitive: bool = False
    row: SnapshotRow | None = None
    counts: SnapshotCounts = Field(default_factory=SnapshotCounts)


class PageSnapshot(_Model):
    """Os elementos da página no passo que falhou, já sem valores nem segredos."""

    version: Literal[1] = SNAPSHOT_VERSION
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    step_kind: str = Field(min_length=1, max_length=40)
    error_code: str = Field(min_length=1, max_length=100)
    #: Origem e caminho da página principal (sem consulta nem fragmento).
    url: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=300)
    frames: list[SnapshotFrame] = Field(min_length=1, max_length=MAX_SNAPSHOT_FRAMES)
    elements: list[SnapshotElement] = Field(default_factory=list, max_length=MAX_SNAPSHOT_ELEMENTS)
    #: Havia mais elementos (ou quadros) do que o retrato guarda.
    truncated: bool = False
