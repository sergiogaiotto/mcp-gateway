"""Formato do robô do RaPA.

Um robô é um manifesto versionado com uma lista fechada de passos, interpretada
por um executor confiável sobre Playwright. Nada aqui é código gerado: tudo é
dado validado, o que permite mostrar o robô em pt-BR, conferir domínios,
credenciais e passos irreversíveis antes de publicar, e consertar um passo
isolado.
"""

import hashlib
import ipaddress
import json
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ARIA_ROLES",
    "SCHEMA_VERSION",
    "AssertStep",
    "CheckStep",
    "ClickStep",
    "Condition",
    "CredentialKind",
    "CredentialRef",
    "DownloadStep",
    "Effect",
    "EqualsCondition",
    "ExtractTableStep",
    "ExtractTextStep",
    "FieldType",
    "FillStep",
    "ForEachStep",
    "IfStep",
    "InputField",
    "Locator",
    "LocatorBy",
    "Manifest",
    "NavigateStep",
    "OutputField",
    "PressStep",
    "SecretValue",
    "SelectStep",
    "Step",
    "Target",
    "TextContainsCondition",
    "TextValue",
    "UploadStep",
    "UrlContainsCondition",
    "ValueSource",
    "VisibleCondition",
    "WaitForStep",
    "max_effect",
]

SCHEMA_VERSION: Literal[1] = 1

STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
NAME_PATTERN = r"^[a-z_][a-z0-9_]{0,63}$"
SLUG_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
ITEMS_REF_PATTERN = r"^(inputs|vars)\.[a-z_][a-z0-9_]{0,63}$"
VAR_REF_PATTERN = r"^vars\.[a-z_][a-z0-9_]{0,63}$"

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

#: Papéis ARIA aceitos em localizadores por papel.
ARIA_ROLES = frozenset(
    {
        "alert",
        "button",
        "cell",
        "checkbox",
        "columnheader",
        "combobox",
        "dialog",
        "form",
        "grid",
        "gridcell",
        "heading",
        "img",
        "link",
        "list",
        "listbox",
        "listitem",
        "menu",
        "menuitem",
        "navigation",
        "option",
        "radio",
        "region",
        "row",
        "rowheader",
        "searchbox",
        "spinbutton",
        "status",
        "switch",
        "tab",
        "table",
        "tabpanel",
        "textbox",
    }
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Effect(StrEnum):
    """Efeito de um passo no sistema de destino."""

    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


_EFFECT_ORDER = {Effect.READ: 0, Effect.WRITE: 1, Effect.IRREVERSIBLE: 2}


def max_effect(*effects: Effect) -> Effect:
    """Devolve o efeito mais forte (leitura < gravação < irreversível)."""
    return max(effects, key=_EFFECT_ORDER.__getitem__, default=Effect.READ)


# ---------------------------------------------------------------------------
# Localizadores
# ---------------------------------------------------------------------------


class LocatorBy(StrEnum):
    ROLE = "role"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    TEXT = "text"
    TEST_ID = "test_id"
    CSS = "css"
    XPATH = "xpath"


class Locator(_Model):
    """Uma estratégia de localização de elemento.

    Para ``role``, ``value`` é o papel ARIA e ``name`` o nome acessível
    (opcional em contêineres como linhas e tabelas). ``value`` e ``name`` podem
    conter templates (por exemplo, o número de uma nota vindo de
    ``item.numero``).
    """

    by: LocatorBy
    value: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    exact: bool = True

    @model_validator(mode="after")
    def _check_role(self) -> Locator:
        if self.by is LocatorBy.ROLE:
            if self.value not in ARIA_ROLES:
                raise ValueError(f"papel ARIA desconhecido: {self.value!r}")
        elif self.name is not None:
            raise ValueError("'name' só se aplica a localizadores por papel")
        return self


class Target(_Model):
    """Elemento-alvo de um passo.

    ``locators`` vem em ordem de preferência. O executor exige que o
    localizador encontre exatamente um elemento.
    """

    intent: str = Field(min_length=1, max_length=300, description="O que é o elemento, em pt-BR")
    locators: list[Locator] = Field(min_length=1, max_length=8)
    frames: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Seletores CSS dos iframes, do mais externo ao mais interno",
    )
    within: Locator | None = Field(
        default=None, description="Contêiner onde procurar (por exemplo, a linha da tabela)"
    )
    within_has_text: str | None = Field(
        default=None,
        max_length=500,
        description="Filtra o contêiner pelo texto (template)",
    )
    snapshot: str | None = Field(
        default=None,
        max_length=4000,
        description="Trecho da árvore de acessibilidade gravado, usado no conserto",
    )

    @model_validator(mode="after")
    def _check_within(self) -> Target:
        if self.within_has_text is not None and self.within is None:
            raise ValueError("'within_has_text' exige 'within'")
        return self


# ---------------------------------------------------------------------------
# Origem dos valores digitados
# ---------------------------------------------------------------------------


class TextValue(_Model):
    source: Literal["text"] = "text"
    template: str = Field(max_length=4000)


class SecretValue(_Model):
    """Valor vindo do cofre. O executor substitui na hora de digitar; o modelo
    de linguagem nunca vê o valor."""

    source: Literal["secret"] = "secret"
    credential: str = Field(pattern=NAME_PATTERN)
    field: Literal["username", "password", "totp"]


type ValueSource = Annotated[TextValue | SecretValue, Field(discriminator="source")]


# ---------------------------------------------------------------------------
# Condições (conjunto fechado, sem expressões livres)
# ---------------------------------------------------------------------------


class VisibleCondition(_Model):
    op: Literal["visible"] = "visible"
    target: Target
    negate: bool = False


class TextContainsCondition(_Model):
    op: Literal["text_contains"] = "text_contains"
    target: Target | None = Field(default=None, description="Sem alvo: o texto da página")
    text: str = Field(min_length=1, max_length=500)
    negate: bool = False


class UrlContainsCondition(_Model):
    op: Literal["url_contains"] = "url_contains"
    value: str = Field(min_length=1, max_length=500)
    negate: bool = False


class EqualsCondition(_Model):
    op: Literal["equals"] = "equals"
    left: str = Field(max_length=500)
    right: str = Field(max_length=500)
    negate: bool = False


type Condition = Annotated[
    VisibleCondition | TextContainsCondition | UrlContainsCondition | EqualsCondition,
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# Passos
# ---------------------------------------------------------------------------


class _StepBase(_Model):
    id: str = Field(pattern=STEP_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200, description="Descrição do passo em pt-BR")
    effect: Effect = Effect.READ
    timeout_ms: int = Field(default=15_000, ge=500, le=600_000)


class NavigateStep(_StepBase):
    kind: Literal["navigate"] = "navigate"
    url: str = Field(min_length=1, max_length=2000)


class ClickStep(_StepBase):
    kind: Literal["click"] = "click"
    target: Target
    expect_navigation: bool = False


class FillStep(_StepBase):
    kind: Literal["fill"] = "fill"
    target: Target
    value: ValueSource
    sequential: bool = Field(
        default=False, description="Digitar tecla a tecla (campos com máscara)"
    )


class SelectStep(_StepBase):
    kind: Literal["select"] = "select"
    target: Target
    option: str = Field(min_length=1, max_length=500, description="Rótulo da opção (template)")


class CheckStep(_StepBase):
    kind: Literal["check"] = "check"
    target: Target
    checked: bool = True


class PressStep(_StepBase):
    kind: Literal["press"] = "press"
    target: Target | None = None
    key: str = Field(pattern=r"^[A-Za-z0-9+]{1,40}$")


class WaitForStep(_StepBase):
    kind: Literal["wait_for"] = "wait_for"
    target: Target | None = None
    state: Literal["visible", "hidden"] = "visible"
    url_contains: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_something(self) -> WaitForStep:
        if self.target is None and self.url_contains is None:
            raise ValueError("wait_for exige 'target' ou 'url_contains'")
        return self


class ExtractTextStep(_StepBase):
    kind: Literal["extract_text"] = "extract_text"
    target: Target
    into: str = Field(pattern=NAME_PATTERN)


class ExtractTableStep(_StepBase):
    kind: Literal["extract_table"] = "extract_table"
    target: Target
    into: str = Field(pattern=NAME_PATTERN)
    columns: list[str] | None = Field(
        default=None,
        max_length=50,
        description="Nomes das colunas, na ordem; sem isso, usa os cabeçalhos",
    )
    max_rows: int = Field(default=1000, ge=1, le=100_000)

    @field_validator("columns")
    @classmethod
    def _check_columns(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for column in value:
                if not re.fullmatch(NAME_PATTERN, column):
                    raise ValueError(f"nome de coluna inválido: {column!r}")
            if len(set(value)) != len(value):
                raise ValueError("nomes de coluna repetidos")
        return value


class AssertStep(_StepBase):
    kind: Literal["assert"] = "assert"
    condition: Condition
    message: str = Field(min_length=1, max_length=300, description="Mensagem de falha em pt-BR")


class DownloadStep(_StepBase):
    kind: Literal["download"] = "download"
    target: Target
    save_as: str = Field(min_length=1, max_length=255, description="Nome do arquivo (template)")
    into: str = Field(pattern=NAME_PATTERN)


class UploadStep(_StepBase):
    kind: Literal["upload"] = "upload"
    target: Target
    file_input: str = Field(pattern=NAME_PATTERN, description="Entrada do tipo arquivo")


class ForEachStep(_StepBase):
    kind: Literal["for_each"] = "for_each"
    items: str = Field(pattern=ITEMS_REF_PATTERN, description="Lista a percorrer")
    steps: list[Step] = Field(min_length=1, max_length=200)
    max_items: int = Field(default=1000, ge=1, le=10_000)


class IfStep(_StepBase):
    kind: Literal["if"] = "if"
    condition: Condition
    then: list[Step] = Field(min_length=1, max_length=200)
    otherwise: list[Step] = Field(default_factory=list, max_length=200)


type Step = Annotated[
    NavigateStep
    | ClickStep
    | FillStep
    | SelectStep
    | CheckStep
    | PressStep
    | WaitForStep
    | ExtractTextStep
    | ExtractTableStep
    | AssertStep
    | DownloadStep
    | UploadStep
    | ForEachStep
    | IfStep,
    Field(discriminator="kind"),
]

ForEachStep.model_rebuild()
IfStep.model_rebuild()


def child_blocks(step: BaseModel) -> list[list[Any]]:
    """Blocos de passos aninhados de um passo de controle."""
    if isinstance(step, ForEachStep):
        return [step.steps]
    if isinstance(step, IfStep):
        return [step.then, step.otherwise]
    return []


# ---------------------------------------------------------------------------
# Entradas, saídas e credenciais
# ---------------------------------------------------------------------------


class FieldType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    CPF = "cpf"
    CNPJ = "cnpj"
    MONEY = "money"
    FILE = "file"
    LIST = "list"


_SIMPLE_SCHEMAS: dict[FieldType, dict[str, Any]] = {
    FieldType.STRING: {"type": "string", "maxLength": 4000},
    FieldType.INTEGER: {"type": "integer"},
    FieldType.NUMBER: {"type": "number"},
    FieldType.BOOLEAN: {"type": "boolean"},
    FieldType.DATE: {"type": "string", "format": "date"},
    FieldType.CPF: {"type": "string", "pattern": r"^[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2}$"},
    # CNPJ alfanumérico (emitido desde julho de 2026) ou numérico.
    FieldType.CNPJ: {
        "type": "string",
        "pattern": r"^[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?[0-9]{2}$",
    },
    FieldType.MONEY: {"type": "string", "pattern": r"^-?[0-9.]{1,20}(,[0-9]{2})?$"},
    FieldType.FILE: {"type": "string", "description": "Identificador de arquivo enviado"},
}


class InputField(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    type: FieldType
    required: bool = True
    example: str | None = Field(default=None, max_length=500)
    item_fields: list[InputField] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _check_list(self) -> InputField:
        if self.type is FieldType.LIST:
            if not self.item_fields:
                raise ValueError(f"a lista {self.name!r} precisa declarar item_fields")
            for item in self.item_fields:
                if item.type in (FieldType.LIST, FieldType.FILE):
                    raise ValueError("campos de item não podem ser listas nem arquivos")
            names = [item.name for item in self.item_fields]
            if len(set(names)) != len(names):
                raise ValueError(f"campos repetidos em {self.name!r}")
        elif self.item_fields is not None:
            raise ValueError("item_fields só se aplica a campos do tipo lista")
        return self

    def json_schema(self) -> dict[str, Any]:
        if self.type is FieldType.LIST and self.item_fields is not None:
            schema: dict[str, Any] = {
                "type": "array",
                "maxItems": 10_000,
                "items": {
                    "type": "object",
                    "properties": {f.name: f.json_schema() for f in self.item_fields},
                    "required": [f.name for f in self.item_fields if f.required],
                    "additionalProperties": False,
                },
            }
        else:
            schema = dict(_SIMPLE_SCHEMAS[self.type])
        schema["title"] = self.title
        if self.description:
            schema["description"] = self.description
        return schema


InputField.model_rebuild()


class OutputField(_Model):
    name: str = Field(pattern=NAME_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    type: FieldType
    source: str = Field(pattern=VAR_REF_PATTERN, description="Variável que guarda o valor")

    def json_schema(self) -> dict[str, Any]:
        if self.type is FieldType.LIST:
            schema: dict[str, Any] = {
                "type": "array",
                "items": {"type": "object", "additionalProperties": {"type": "string"}},
            }
        else:
            schema = dict(_SIMPLE_SCHEMAS[self.type])
        schema["title"] = self.title
        if self.description:
            schema["description"] = self.description
        return schema


class CredentialKind(StrEnum):
    PASSWORD = "password"  # noqa: S105 - tipo de credencial, não um segredo
    TOTP = "totp"
    PFX_A1 = "pfx_a1"


class CredentialRef(_Model):
    """Referência a uma credencial guardada no cofre. Nunca contém o valor."""

    name: str = Field(pattern=NAME_PATTERN)
    kind: CredentialKind
    origin: str = Field(max_length=300, description="Origem onde a credencial pode ser usada")
    description: str = Field(default="", max_length=500)

    @field_validator("origin")
    @classmethod
    def _check_origin(cls, value: str) -> str:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        # http só no domínio reservado para testes (RFC 2606).
        if not host or not (
            parts.scheme == "https" or (parts.scheme == "http" and host.endswith(".test"))
        ):
            raise ValueError("a origem da credencial deve ser https://host")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("a origem da credencial não pode ter caminho, consulta ou usuário")
        return f"{parts.scheme}://{parts.netloc.lower()}"


def _check_hostname(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"use nome de domínio, não endereço IP: {host!r}")
    if not _HOSTNAME.fullmatch(host):
        raise ValueError(f"domínio inválido: {host!r}")
    return host


# ---------------------------------------------------------------------------
# Manifesto
# ---------------------------------------------------------------------------


class Manifest(_Model):
    """Definição completa e versionável de um robô."""

    schema_version: Literal[1] = SCHEMA_VERSION
    name: str = Field(pattern=SLUG_PATTERN, description="Nome técnico; vira o nome da tool MCP")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    inputs: list[InputField] = Field(default_factory=list, max_length=50)
    outputs: list[OutputField] = Field(default_factory=list, max_length=50)
    credentials: list[CredentialRef] = Field(default_factory=list, max_length=10)
    allowed_domains: list[str] = Field(min_length=1, max_length=20)
    steps: list[Step] = Field(min_length=1, max_length=500)
    postconditions: list[Condition] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_domains")
    @classmethod
    def _check_domains(cls, value: list[str]) -> list[str]:
        hosts = [_check_hostname(host) for host in value]
        if len(set(hosts)) != len(hosts):
            raise ValueError("domínios repetidos")
        return hosts

    @model_validator(mode="after")
    def _check_unique_names(self) -> Manifest:
        for label, names in (
            ("entradas", [f.name for f in self.inputs]),
            ("saídas", [f.name for f in self.outputs]),
            ("credenciais", [c.name for c in self.credentials]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"nomes repetidos em {label}")
        return self

    def iter_steps(self) -> Iterator[tuple[Step, tuple[str, ...]]]:
        """Percorre todos os passos, em ordem, com os ids dos passos que os contêm."""

        def walk(
            steps: list[Step], parents: tuple[str, ...]
        ) -> Iterator[tuple[Step, tuple[str, ...]]]:
            for step in steps:
                yield step, parents
                for block in child_blocks(step):
                    yield from walk(block, (*parents, step.id))

        yield from walk(self.steps, ())

    def step_by_id(self, step_id: str) -> Step | None:
        for step, _ in self.iter_steps():
            if step.id == step_id:
                return step
        return None

    def effect(self) -> Effect:
        """Efeito mais forte entre todos os passos."""
        return max_effect(*(step.effect for step, _ in self.iter_steps()))

    def input_json_schema(self) -> dict[str, Any]:
        """JSON Schema 2020-12 das entradas: vira o inputSchema da tool MCP e o corpo do REST."""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {f.name: f.json_schema() for f in self.inputs},
            "required": [f.name for f in self.inputs if f.required],
            "additionalProperties": False,
        }

    def output_json_schema(self) -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {f.name: f.json_schema() for f in self.outputs},
            "required": [f.name for f in self.outputs],
            "additionalProperties": False,
        }

    def fingerprint(self) -> str:
        """SHA-256 da forma canônica do manifesto (identifica a versão publicada)."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
