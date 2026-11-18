"""Descrição de um robô em pt-BR para quem não programa.

Funções puras: recebem um :class:`~rapa.core.robot.Manifest` e devolvem linhas
numeradas, tabelas de entradas e saídas e a diferença entre duas versões, para
a tela de aprovação. Valores do cofre aparecem só como referência
("valor do cofre: <credencial>"); o manifesto nunca guarda o valor, e nada aqui
tenta buscá-lo.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from rapa.core import templates
from rapa.core.robot import (
    AssertStep,
    CheckStep,
    ClickStep,
    Condition,
    CredentialRef,
    DownloadStep,
    Effect,
    EqualsCondition,
    ExtractTableStep,
    ExtractTextStep,
    FillStep,
    ForEachStep,
    IfStep,
    InputField,
    Locator,
    LocatorBy,
    Manifest,
    NavigateStep,
    OutputField,
    PressStep,
    SecretValue,
    SelectStep,
    Step,
    Target,
    TextContainsCondition,
    TextValue,
    UploadStep,
    UrlContainsCondition,
    ValueSource,
    VisibleCondition,
    WaitForStep,
)
from rapa.ui.labels import (
    CREDENTIAL_KIND_LABELS,
    EFFECT_LABELS,
    FIELD_TYPE_LABELS,
    SECRET_FIELD_LABELS,
)

__all__ = [
    "CredentialRow",
    "FieldChange",
    "InputRow",
    "ManifestDiff",
    "OutputRow",
    "StepChange",
    "StepLine",
    "condition_text",
    "describe_credentials",
    "describe_inputs",
    "describe_outputs",
    "describe_postconditions",
    "describe_steps",
    "diff_manifests",
    "effect_label",
    "render_text",
    "step_sentence",
    "template_text",
    "value_text",
]

_MAX_LITERAL = 80
_EFFECT_ORDER = {Effect.READ: 0, Effect.WRITE: 1, Effect.IRREVERSIBLE: 2}


def effect_label(effect: Effect) -> str:
    """Selo do efeito: Leitura, Grava dados ou Irreversível."""
    return EFFECT_LABELS[effect]


def _quote(text: str) -> str:
    return f"“{text}”"


def _shorten(text: str, limit: int = _MAX_LITERAL) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Templates e valores
# ---------------------------------------------------------------------------


def _input_titles(manifest: Manifest) -> dict[str, str]:
    return {f.name: f.title for f in manifest.inputs}


def _ref_text(ref: templates.Ref, inputs: Mapping[str, str]) -> str:
    match ref.scope:
        case templates.Scope.INPUTS:
            return f"entrada {inputs.get(ref.name, ref.name)}"
        case templates.Scope.VARS:
            return f"variável {ref.name}"
        case templates.Scope.ITEM:
            return f"campo {ref.name} do item"


def template_text(template: str, inputs: Mapping[str, str] | None = None) -> str:
    """Template legível: referências viram ``[entrada CPF]``, ``[variável x]``…"""
    inputs = inputs or {}
    try:
        parts = templates.parse(template)
    except templates.TemplateError:
        return _shorten(template)
    out: list[str] = []
    for part in parts:
        if isinstance(part, templates.Ref):
            out.append(f"[{_ref_text(part, inputs)}]")
        else:
            out.append(part)
    return _shorten("".join(out), 200)


def _single_ref(template: str) -> templates.Ref | None:
    try:
        parts = templates.parse(template)
    except templates.TemplateError:
        return None
    if len(parts) == 1 and isinstance(parts[0], templates.Ref):
        return parts[0]
    return None


def value_text(value: ValueSource, inputs: Mapping[str, str] | None = None) -> str:
    """De onde vem o valor digitado, sem nunca mostrar segredos."""
    inputs = inputs or {}
    if isinstance(value, SecretValue):
        field_label = SECRET_FIELD_LABELS.get(value.field, value.field)
        return f"valor do cofre: {value.credential}, {field_label}"
    ref = _single_ref(value.template)
    if ref is not None:
        return f"valor da {_ref_text(ref, inputs)}"
    try:
        literal = not templates.has_references(value.template)
    except templates.TemplateError:
        literal = True
    if literal:
        return f"texto fixo {_quote(_shorten(value.template))}"
    return f"texto {_quote(template_text(value.template, inputs))}"


_LOCATOR_LABELS: dict[LocatorBy, str] = {
    LocatorBy.ROLE: "papel",
    LocatorBy.LABEL: "rótulo",
    LocatorBy.PLACEHOLDER: "texto de exemplo",
    LocatorBy.TEXT: "texto",
    LocatorBy.TEST_ID: "identificador de teste",
    LocatorBy.CSS: "seletor CSS",
    LocatorBy.XPATH: "XPath",
}


def _locator_text(locator: Locator, inputs: Mapping[str, str]) -> str:
    label = _LOCATOR_LABELS[locator.by]
    value = template_text(locator.value, inputs)
    if locator.by is LocatorBy.ROLE:
        if locator.name is None:
            return f"{label} {value}"
        return f"{label} {value} {_quote(template_text(locator.name, inputs))}"
    return f"{label} {_quote(value)}"


def _target_text(target: Target | None) -> str:
    return _quote(target.intent) if target is not None else "a página"


def condition_text(condition: Condition, inputs: Mapping[str, str] | None = None) -> str:
    """Condição em pt-BR (por exemplo, ‘a página contém “Protocolo emitido”’)."""
    inputs = inputs or {}
    neg = "não " if condition.negate else ""
    match condition:
        case VisibleCondition(target=target):
            return f"{_target_text(target)} {neg}está visível"
        case TextContainsCondition(target=target, text=text):
            return f"{_target_text(target)} {neg}contém {_quote(template_text(text, inputs))}"
        case UrlContainsCondition(value=value):
            return f"o endereço {neg}contém {_quote(template_text(value, inputs))}"
        case EqualsCondition(left=left, right=right):
            verb = "é diferente de" if condition.negate else "é igual a"
            return f"{_quote(template_text(left, inputs))} {verb} {_quote(template_text(right, inputs))}"
    return "condição"  # pragma: no cover - conjunto fechado


def _url_text(url: str, inputs: Mapping[str, str]) -> str:
    readable = template_text(url, inputs)
    parts = urlsplit(readable)
    if parts.hostname:
        rest = readable.split(parts.netloc, 1)[1] if parts.netloc else ""
        return f"{parts.hostname}{rest}"
    return readable


# ---------------------------------------------------------------------------
# Passos
# ---------------------------------------------------------------------------


def step_sentence(step: Step, inputs: Mapping[str, str] | None = None) -> str:
    """Uma frase por passo, a partir do tipo, do título e da intenção do alvo."""
    inputs = inputs or {}
    title = step.title.rstrip(".")
    match step:
        case NavigateStep(url=url):
            return f"{title} ({_url_text(url, inputs)})"
        case FillStep(target=target, value=value):
            return f"{title} em {_quote(target.intent)} ({value_text(value, inputs)})"
        case SelectStep(target=target, option=option):
            ref = _single_ref(option)
            chosen = (
                f"opção da {_ref_text(ref, inputs)}"
                if ref is not None
                else f"opção {_quote(template_text(option, inputs))}"
            )
            return f"{title} em {_quote(target.intent)} ({chosen})"
        case CheckStep(target=target, checked=checked):
            action = "marcar" if checked else "desmarcar"
            return f"{title} ({action} {_quote(target.intent)})"
        case ClickStep(target=target, expect_navigation=expect_navigation):
            suffix = " e esperar a nova página" if expect_navigation else ""
            return f"{title} (clicar em {_quote(target.intent)}{suffix})"
        case PressStep(target=target, key=key):
            where = f" em {_quote(target.intent)}" if target is not None else ""
            return f"{title} (tecla {key}{where})"
        case WaitForStep(target=target, state=state, url_contains=url_contains):
            waits: list[str] = []
            if target is not None:
                verb = "aparecer" if state == "visible" else "sumir"
                waits.append(f"esperar {_quote(target.intent)} {verb}")
            if url_contains is not None:
                waits.append(
                    f"esperar o endereço conter {_quote(template_text(url_contains, inputs))}"
                )
            return f"{title} ({'; '.join(waits)})"
        case ExtractTextStep(target=target, into=into):
            return f"{title} de {_quote(target.intent)} (guarda em {into})"
        case ExtractTableStep(target=target, into=into, columns=columns):
            cols = f"; colunas: {', '.join(columns)}" if columns else ""
            return f"{title} de {_quote(target.intent)} (guarda a tabela em {into}{cols})"
        case AssertStep(condition=condition):
            return f"{title} (confere que {condition_text(condition, inputs)})"
        case DownloadStep(target=target, save_as=save_as):
            return (
                f"{title} de {_quote(target.intent)} "
                f"(salva como {_quote(template_text(save_as, inputs))})"
            )
        case UploadStep(target=target, file_input=file_input):
            source = inputs.get(file_input, file_input)
            return f"{title} em {_quote(target.intent)} (arquivo da entrada {source})"
        case ForEachStep(items=items, max_items=max_items):
            scope, name = items.split(".", 1)
            what = f"entrada {inputs.get(name, name)}" if scope == "inputs" else f"tabela {name}"
            return f"{title} (repete para cada item da {what}, no máximo {max_items})"
        case IfStep(condition=condition):
            return f"{title} (se {condition_text(condition, inputs)})"
    return title  # pragma: no cover - conjunto fechado


@dataclass(frozen=True, slots=True)
class StepLine:
    """Uma linha da lista numerada de passos."""

    number: str
    depth: int
    step_id: str | None
    kind: str
    text: str
    effect: Effect

    @property
    def badge(self) -> str:
        return EFFECT_LABELS[self.effect]

    def render(self, *, badge: bool = True) -> str:
        indent = "    " * self.depth
        if self.kind == "otherwise":
            return f"{indent}{self.text}"
        suffix = f" [{self.badge}]" if badge else ""
        return f"{indent}{self.number}. {self.text}{suffix}"


def _walk_lines(
    steps: Sequence[Step],
    prefix: str,
    depth: int,
    inputs: Mapping[str, str],
    start: int = 1,
) -> Iterator[StepLine]:
    for index, step in enumerate(steps, start=start):
        number = f"{prefix}{index}"
        yield StepLine(number, depth, step.id, step.kind, step_sentence(step, inputs), step.effect)
        if isinstance(step, ForEachStep):
            yield from _walk_lines(step.steps, f"{number}.", depth + 1, inputs)
        elif isinstance(step, IfStep):
            yield from _walk_lines(step.then, f"{number}.", depth + 1, inputs)
            if step.otherwise:
                # O bloco "caso contrário" continua a numeração do bloco "então".
                yield StepLine("", depth + 1, None, "otherwise", "Caso contrário:", step.effect)
                yield from _walk_lines(
                    step.otherwise, f"{number}.", depth + 1, inputs, start=len(step.then) + 1
                )


def describe_steps(manifest: Manifest) -> list[StepLine]:
    """Lista numerada (1, 2, 2.1…) dos passos, com blocos aninhados recuados."""
    return list(_walk_lines(manifest.steps, "", 0, _input_titles(manifest)))


# ---------------------------------------------------------------------------
# Entradas, saídas, credenciais e pós-condições
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InputRow:
    name: str
    title: str
    type: str
    required: str
    description: str
    example: str
    item_fields: tuple[InputRow, ...] = ()


@dataclass(frozen=True, slots=True)
class OutputRow:
    name: str
    title: str
    type: str
    source: str
    description: str


@dataclass(frozen=True, slots=True)
class CredentialRow:
    name: str
    kind: str
    origin: str
    description: str


def _input_row(spec: InputField) -> InputRow:
    return InputRow(
        name=spec.name,
        title=spec.title,
        type=FIELD_TYPE_LABELS[spec.type],
        required="Obrigatória" if spec.required else "Opcional",
        description=spec.description,
        example=spec.example or "",
        item_fields=tuple(_input_row(item) for item in spec.item_fields or ()),
    )


def describe_inputs(manifest: Manifest) -> list[InputRow]:
    return [_input_row(spec) for spec in manifest.inputs]


def describe_outputs(manifest: Manifest) -> list[OutputRow]:
    return [
        OutputRow(
            name=out.name,
            title=out.title,
            type=FIELD_TYPE_LABELS[out.type],
            source=out.source.split(".", 1)[1],
            description=out.description,
        )
        for out in manifest.outputs
    ]


def describe_credentials(manifest: Manifest) -> list[CredentialRow]:
    """Credenciais do cofre usadas pelo robô: só nome, tipo e origem."""
    return [
        CredentialRow(
            name=cred.name,
            kind=CREDENTIAL_KIND_LABELS[cred.kind],
            origin=cred.origin,
            description=cred.description,
        )
        for cred in manifest.credentials
    ]


def describe_postconditions(manifest: Manifest) -> list[str]:
    inputs = _input_titles(manifest)
    return [condition_text(condition, inputs) for condition in manifest.postconditions]


def render_text(manifest: Manifest) -> str:
    """O robô inteiro em texto corrido (útil para revisão e para testes)."""
    lines = [f"{manifest.title} ({manifest.name})", manifest.description, ""]
    lines.append(f"Efeito geral: {effect_label(manifest.effect())}")
    lines.append(f"Domínios permitidos: {', '.join(manifest.allowed_domains)}")
    if manifest.inputs:
        lines += ["", "Entradas:"]
        for row in describe_inputs(manifest):
            lines.append(f"- {row.title} ({row.name}): {row.type}, {row.required.lower()}")
            lines += [f"    - {item.title} ({item.name}): {item.type}" for item in row.item_fields]
    if manifest.credentials:
        lines += ["", "Credenciais do cofre:"]
        lines += [
            f"- {row.name}: {row.kind}, usada em {row.origin}"
            for row in describe_credentials(manifest)
        ]
    lines += ["", "Passos:"]
    lines += [line.render() for line in describe_steps(manifest)]
    if manifest.outputs:
        lines += ["", "Saídas:"]
        lines += [
            f"- {row.title} ({row.name}): {row.type}, vem de {row.source}"
            for row in describe_outputs(manifest)
        ]
    if manifest.postconditions:
        lines += ["", "Ao final, confere que:"]
        lines += [f"- {text}" for text in describe_postconditions(manifest)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diferença entre versões
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldChange:
    """Um campo que mudou, com os valores legíveis de antes e depois."""

    field: str
    label: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class StepChange:
    step_id: str
    change: Literal["added", "removed", "changed"]
    number: str
    text: str
    effect: Effect
    fields: tuple[FieldChange, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    added: list[StepChange] = field(default_factory=list)
    removed: list[StepChange] = field(default_factory=list)
    changed: list[StepChange] = field(default_factory=list)
    info: list[FieldChange] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.info)

    #: Se alguma mudança torna o robô mais forte (passo novo que grava, efeito que sobe).
    raises_effect: bool = False


_STEP_FIELD_LABELS: dict[str, str] = {
    "kind": "tipo de passo",
    "title": "título",
    "effect": "efeito",
    "timeout_ms": "tempo limite (ms)",
    "url": "endereço",
    "value": "valor",
    "sequential": "digitação tecla a tecla",
    "option": "opção",
    "checked": "marcar",
    "key": "tecla",
    "state": "estado esperado",
    "url_contains": "endereço esperado",
    "into": "variável de destino",
    "columns": "colunas",
    "max_rows": "máximo de linhas",
    "condition": "condição",
    "message": "mensagem de falha",
    "save_as": "nome do arquivo",
    "file_input": "entrada de arquivo",
    "items": "lista percorrida",
    "max_items": "máximo de itens",
    "expect_navigation": "espera nova página",
    "target": "alvo",
}

_TARGET_FIELD_LABELS: dict[str, str] = {
    "intent": "alvo",
    "locators": "localizadores do alvo",
    "frames": "quadros (iframes) do alvo",
    "within": "contêiner do alvo",
    "within_has_text": "texto do contêiner do alvo",
    "snapshot": "trecho gravado do alvo",
}

_NESTED_KEYS = frozenset({"steps", "then", "otherwise"})


def _plain(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(v) for v in value) or "—"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return _shorten(json.dumps(value, ensure_ascii=False, sort_keys=True), 200)
    return _shorten(str(value), 200)


def _step_field_text(step: Step, key: str, inputs: Mapping[str, str]) -> str:
    value = getattr(step, key, None)
    if key == "effect" and isinstance(value, Effect):
        return EFFECT_LABELS[value]
    if key == "value" and isinstance(value, (TextValue, SecretValue)):
        return value_text(value, inputs)
    if key == "condition" and value is not None:
        return condition_text(value, inputs)
    if key in {"url", "option", "save_as", "url_contains"} and isinstance(value, str):
        return template_text(value, inputs)
    return _plain(value)


def _target_changes(
    old: Target | None,
    new: Target | None,
    inputs_old: Mapping[str, str],
    inputs_new: Mapping[str, str],
) -> list[FieldChange]:
    if old is None or new is None:
        return [
            FieldChange(
                "target",
                "alvo",
                _quote(old.intent) if old else "—",
                _quote(new.intent) if new else "—",
            )
        ]
    out: list[FieldChange] = []
    for key, label in _TARGET_FIELD_LABELS.items():
        before, after = getattr(old, key), getattr(new, key)
        if before == after:
            continue
        if key == "intent":
            out.append(FieldChange("target.intent", label, _quote(before), _quote(after)))
        elif key == "locators":
            out.append(
                FieldChange(
                    "target.locators",
                    label,
                    "; ".join(_locator_text(loc, inputs_old) for loc in before),
                    "; ".join(_locator_text(loc, inputs_new) for loc in after),
                )
            )
        elif key == "within":
            out.append(
                FieldChange(
                    "target.within",
                    label,
                    _locator_text(before, inputs_old) if before else "—",
                    _locator_text(after, inputs_new) if after else "—",
                )
            )
        else:
            out.append(FieldChange(f"target.{key}", label, _plain(before), _plain(after)))
    return out


def _step_changes(
    old: Step, new: Step, inputs_old: Mapping[str, str], inputs_new: Mapping[str, str]
) -> list[FieldChange]:
    old_data = old.model_dump(mode="json", exclude=set(_NESTED_KEYS))
    new_data = new.model_dump(mode="json", exclude=set(_NESTED_KEYS))
    changes: list[FieldChange] = []
    for key in [*old_data, *(k for k in new_data if k not in old_data)]:
        if key == "id" or old_data.get(key) == new_data.get(key):
            continue
        if key == "target":
            changes += _target_changes(
                getattr(old, "target", None), getattr(new, "target", None), inputs_old, inputs_new
            )
            continue
        changes.append(
            FieldChange(
                key,
                _STEP_FIELD_LABELS.get(key, key),
                _step_field_text(old, key, inputs_old) if key in old_data else "—",
                _step_field_text(new, key, inputs_new) if key in new_data else "—",
            )
        )
    return changes


@dataclass(frozen=True, slots=True)
class _Placed:
    step: Step
    number: str
    parent: str | None  # id do passo que contém (com o bloco), ou None no nível principal


def _place(manifest: Manifest) -> dict[str, _Placed]:
    placed: dict[str, _Placed] = {}

    def walk(steps: Sequence[Step], prefix: str, parent: str | None, start: int = 1) -> None:
        for index, step in enumerate(steps, start=start):
            number = f"{prefix}{index}"
            placed.setdefault(step.id, _Placed(step, number, parent))
            if isinstance(step, ForEachStep):
                walk(step.steps, f"{number}.", f"{step.id}:steps")
            elif isinstance(step, IfStep):
                walk(step.then, f"{number}.", f"{step.id}:then")
                walk(step.otherwise, f"{number}.", f"{step.id}:otherwise", len(step.then) + 1)

    walk(manifest.steps, "", None)
    return placed


def _lcs(a: Sequence[str], b: Sequence[str]) -> set[str]:
    """Ids que ficam na mesma ordem relativa nas duas sequências."""
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
            )
    keep: set[str] = set()
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            keep.add(a[i])
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return keep


def _parent_text(parent: str | None) -> str:
    if parent is None:
        return "nível principal"
    step_id, block = parent.split(":", 1)
    where = {"steps": "repetição", "then": "condição", "otherwise": "caso contrário"}[block]
    return f"dentro de {step_id} ({where})"


def _blocks(placed: Mapping[str, _Placed]) -> dict[str | None, list[str]]:
    blocks: dict[str | None, list[str]] = {}
    for step_id, info in placed.items():
        blocks.setdefault(info.parent, []).append(step_id)
    return blocks


def _moved(old: Mapping[str, _Placed], new: Mapping[str, _Placed]) -> set[str]:
    """Passos que mudaram de ordem dentro do mesmo bloco."""
    old_blocks, new_blocks = _blocks(old), _blocks(new)
    moved: set[str] = set()
    for parent, new_ids in new_blocks.items():
        old_ids = old_blocks.get(parent, [])
        common_old = [i for i in old_ids if i in new and new[i].parent == parent]
        common_new = [i for i in new_ids if i in old and old[i].parent == parent]
        moved |= set(common_new) - _lcs(common_old, common_new)
    return moved


def _named_items(items: Sequence[BaseModel], key: str) -> dict[str, dict[str, Any]]:
    return {str(getattr(item, key)): item.model_dump(mode="json") for item in items}


def _list_changes(
    label: str,
    old: Mapping[str, dict[str, Any]],
    new: Mapping[str, dict[str, Any]],
    describe: Any,
) -> list[FieldChange]:
    out: list[FieldChange] = []
    for name in [*old, *(n for n in new if n not in old)]:
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        out.append(
            FieldChange(
                f"{label}.{name}",
                f"{label} {name}",
                describe(before) if before is not None else "—",
                describe(after) if after is not None else "—",
            )
        )
    return out


def _input_summary(data: dict[str, Any]) -> str:
    spec = InputField.model_validate(data)
    row = _input_row(spec)
    text = f"{row.title}: {row.type}, {row.required.lower()}"
    if row.item_fields:
        text += " (" + ", ".join(f"{i.title}: {i.type}" for i in row.item_fields) + ")"
    return text


def _output_summary(data: dict[str, Any]) -> str:
    out = OutputField.model_validate(data)
    return f"{out.title}: {FIELD_TYPE_LABELS[out.type]}, vem de {out.source.split('.', 1)[1]}"


def _credential_summary(data: dict[str, Any]) -> str:
    cred = CredentialRef.model_validate(data)
    return f"{CREDENTIAL_KIND_LABELS[cred.kind]}, usada em {cred.origin}"


def _info_changes(old: Manifest, new: Manifest) -> list[FieldChange]:
    out: list[FieldChange] = []
    for key, label in (("name", "nome técnico"), ("title", "título"), ("description", "descrição")):
        if getattr(old, key) != getattr(new, key):
            out.append(FieldChange(key, label, getattr(old, key), getattr(new, key)))
    if old.allowed_domains != new.allowed_domains:
        out.append(
            FieldChange(
                "allowed_domains",
                "domínios permitidos",
                ", ".join(old.allowed_domains),
                ", ".join(new.allowed_domains),
            )
        )
    out += _list_changes(
        "entrada",
        _named_items(old.inputs, "name"),
        _named_items(new.inputs, "name"),
        _input_summary,
    )
    out += _list_changes(
        "saída",
        _named_items(old.outputs, "name"),
        _named_items(new.outputs, "name"),
        _output_summary,
    )
    out += _list_changes(
        "credencial",
        _named_items(old.credentials, "name"),
        _named_items(new.credentials, "name"),
        _credential_summary,
    )
    old_post, new_post = describe_postconditions(old), describe_postconditions(new)
    if old_post != new_post:
        out.append(
            FieldChange(
                "postconditions",
                "conferências finais",
                "; ".join(old_post) or "—",
                "; ".join(new_post) or "—",
            )
        )
    return out


def diff_manifests(old: Manifest, new: Manifest) -> ManifestDiff:
    """Passos novos, removidos e alterados (pelo id), com os campos que mudaram."""
    old_placed, new_placed = _place(old), _place(new)
    inputs_old, inputs_new = _input_titles(old), _input_titles(new)
    moved = _moved(old_placed, new_placed)
    added: list[StepChange] = []
    changed: list[StepChange] = []
    removed: list[StepChange] = []
    raises = False
    for step_id, info in new_placed.items():
        previous = old_placed.get(step_id)
        sentence = step_sentence(info.step, inputs_new)
        if previous is None:
            added.append(StepChange(step_id, "added", info.number, sentence, info.step.effect))
            raises = raises or info.step.effect is not Effect.READ
            continue
        if _EFFECT_ORDER[info.step.effect] > _EFFECT_ORDER[previous.step.effect]:
            raises = True
        fields = _step_changes(previous.step, info.step, inputs_old, inputs_new)
        if previous.parent != info.parent:
            fields.append(
                FieldChange(
                    "position",
                    "posição",
                    _parent_text(previous.parent),
                    _parent_text(info.parent),
                )
            )
        elif step_id in moved:
            fields.append(
                FieldChange("position", "ordem", f"passo {previous.number}", f"passo {info.number}")
            )
        if fields:
            changed.append(
                StepChange(
                    step_id, "changed", info.number, sentence, info.step.effect, tuple(fields)
                )
            )
    for step_id, info in old_placed.items():
        if step_id not in new_placed:
            removed.append(
                StepChange(
                    step_id,
                    "removed",
                    info.number,
                    step_sentence(info.step, inputs_old),
                    info.step.effect,
                )
            )
    return ManifestDiff(
        added=added,
        removed=removed,
        changed=changed,
        info=_info_changes(old, new),
        raises_effect=raises,
    )
