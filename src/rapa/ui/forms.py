"""Formulários gerados a partir do JSON Schema das entradas de um robô.

Duas partes:

- funções puras que leem o esquema (:func:`fields_from_schema`), conferem e
  convertem o que a pessoa digitou no JSON que o ``RunService`` espera
  (:func:`convert_values`), com mensagens em pt-BR;
- :class:`SchemaForm`, que monta os campos no NiceGUI (com máscaras de CPF,
  CNPJ e data, valor em reais e listas editáveis como tabela) e mostra os erros
  ao lado de cada campo, tanto os da conferência local quanto os devolvidos
  pelo servidor.
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from nicegui import ui

from rapa.core.robot import FieldType, InputField
from rapa.core.templates import TemplateError, format_value

__all__ = [
    "FormField",
    "FormValidationError",
    "SchemaForm",
    "cnpj_valido",
    "convert_field",
    "convert_values",
    "cpf_valido",
    "field_error",
    "fields_from_schema",
    "split_server_errors",
]

_REQUIRED = "Campo obrigatório."
_DATE_HINT = "dd/mm/aaaa"
_FILE_DESCRIPTION = "Identificador de arquivo enviado"


@dataclass(frozen=True, slots=True)
class FormField:
    """Um campo do formulário, tirado de uma propriedade do esquema."""

    name: str
    title: str
    type: FieldType
    required: bool
    description: str = ""
    max_length: int | None = None
    pattern: str | None = None
    max_items: int | None = None
    item_fields: tuple[FormField, ...] = ()


class FormValidationError(Exception):
    """Valores que não passaram na conferência, com a mensagem de cada campo."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("corrija os campos destacados")
        self.errors = dict(errors)

    @property
    def messages(self) -> list[str]:
        return [f"{name}: {message}" for name, message in self.errors.items()]


# ---------------------------------------------------------------------------
# Leitura do esquema
# ---------------------------------------------------------------------------


def _signature(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in schema.items() if k not in {"title", "description"}}


#: Esquema de cada tipo simples, como o núcleo gera, sem título nem descrição.
_SIGNATURES: list[tuple[FieldType, dict[str, Any]]] = [
    (t, _signature(InputField(name="x", title="x", type=t).json_schema()))
    for t in FieldType
    if t is not FieldType.LIST
]


def _field_type(schema: Mapping[str, Any]) -> FieldType:
    if schema.get("type") == "array":
        return FieldType.LIST
    signature = _signature(schema)
    for kind, expected in _SIGNATURES:
        if signature == expected:
            return kind
    if schema.get("type") == "string" and schema.get("format") == "date":
        return FieldType.DATE
    return {
        "integer": FieldType.INTEGER,
        "number": FieldType.NUMBER,
        "boolean": FieldType.BOOLEAN,
    }.get(str(schema.get("type")), FieldType.STRING)


def _form_field(name: str, schema: Mapping[str, Any], required: bool) -> FormField:
    kind = _field_type(schema)
    items: tuple[FormField, ...] = ()
    max_items: int | None = None
    if kind is FieldType.LIST:
        item_schema = schema.get("items")
        item_schema = item_schema if isinstance(item_schema, Mapping) else {}
        props = item_schema.get("properties")
        props = props if isinstance(props, Mapping) else {}
        item_required = set(item_schema.get("required") or [])
        items = tuple(
            _form_field(str(key), value, key in item_required)
            for key, value in props.items()
            if isinstance(value, Mapping)
        )
        raw_max = schema.get("maxItems")
        max_items = raw_max if isinstance(raw_max, int) else None
    description = str(schema.get("description") or "")
    if kind is FieldType.FILE and description == _FILE_DESCRIPTION:
        description = ""
    raw_len = schema.get("maxLength")
    pattern = schema.get("pattern")
    return FormField(
        name=name,
        title=str(schema.get("title") or name),
        type=kind,
        required=required,
        description=description,
        max_length=raw_len if isinstance(raw_len, int) else None,
        pattern=pattern if isinstance(pattern, str) else None,
        max_items=max_items,
        item_fields=items,
    )


def fields_from_schema(schema: Mapping[str, Any]) -> list[FormField]:
    """Campos do formulário, na ordem do esquema."""
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return []
    required = set(schema.get("required") or [])
    return [
        _form_field(str(name), spec, name in required)
        for name, spec in props.items()
        if isinstance(spec, Mapping)
    ]


# ---------------------------------------------------------------------------
# CPF e CNPJ
# ---------------------------------------------------------------------------


def cpf_valido(cpf: str) -> bool:
    """Confere os dígitos verificadores de um CPF (com ou sem pontuação)."""
    digits = [int(c) for c in cpf if c.isdigit()]
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits[:size], range(size + 1, 1, -1), strict=True))
        if (total * 10) % 11 % 10 != digits[size]:
            return False
    return True


_CNPJ_CHARS = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")


def cnpj_valido(cnpj: str) -> bool:
    """Confere os dígitos verificadores de um CNPJ numérico ou alfanumérico.

    No CNPJ alfanumérico cada caractere vale o código ASCII menos 48 (os
    dígitos continuam valendo 0 a 9) e o cálculo do módulo 11 é o mesmo.
    """
    raw = re.sub(r"[./-]", "", cnpj.strip().upper())
    if not _CNPJ_CHARS.fullmatch(raw) or len(set(raw)) == 1:
        return False
    values = [ord(c) - 48 for c in raw]
    for size in (12, 13):
        weights = [*range(size - 7, 1, -1), *range(9, 1, -1)]
        total = sum(v * w for v, w in zip(values[:size], weights, strict=True))
        check = 0 if total % 11 < 2 else 11 - total % 11
        if check != values[size]:
            return False
    return True


def _format_cpf(cpf: str) -> str:
    d = "".join(c for c in cpf if c.isdigit())
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def _format_cnpj(cnpj: str) -> str:
    d = re.sub(r"[./-]", "", cnpj.strip().upper())
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


# ---------------------------------------------------------------------------
# Conversão
# ---------------------------------------------------------------------------


class _FieldError(ValueError):
    pass


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        raise _FieldError("Valor inválido.")
    return str(value)


#: Número com ponto de milhar no formato pt-BR ("1.000", "12.345.678").
_THOUSANDS = re.compile(r"^-?[0-9]{1,3}(?:\.[0-9]{3})+$")
_NUMBER_TEXT = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


#: Inteiros aceitos: até 18 dígitos, como no texto digitado.
_MAX_INTEGER = 10**18 - 1
#: Maior inteiro que um ``float`` (o valor do campo numérico do navegador) guarda
#: sem perder dígitos. Acima disso, o número já chegou trocado: melhor recusar.
_MAX_EXACT_FLOAT_INT = 2**53
_TOO_BIG = "Número grande demais."


def _to_integer(value: Any) -> int:
    """Inteiro para um campo ``integer`` do esquema: sempre ``int``, nunca ``float``.

    O campo numérico do navegador entrega ``5.0``; ele vai como ``5``. Um ``float``
    acima de 2**53 já perdeu dígitos no navegador e é recusado.
    """
    if isinstance(value, bool):
        raise _FieldError("Informe um número inteiro.")
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER:
            raise _FieldError(_TOO_BIG)
        return value
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise _FieldError("Informe um número inteiro.")
        if abs(value) > _MAX_EXACT_FLOAT_INT:
            raise _FieldError(_TOO_BIG)
        return int(value)
    text = _as_text(value).replace(" ", "")
    if _THOUSANDS.fullmatch(text):
        text = text.replace(".", "")
    if re.fullmatch(r"-?[0-9]{1,18}", text):
        return int(text)
    if re.fullmatch(r"-?[0-9]+", text):
        raise _FieldError(_TOO_BIG)
    raise _FieldError("Informe um número inteiro.")


def _typeable_number(number: int | float) -> int | float:
    """Recusa o número que o executor não conseguiria digitar (grande ou pequeno demais)."""
    try:
        format_value(number)
    except TemplateError:
        raise _FieldError("Número grande ou pequeno demais.") from None
    return number


def _to_number(value: Any) -> int | float:
    """Número para um campo ``number`` do esquema; os inteiros vão como ``int``.

    ``5.0`` (o que o campo numérico do navegador entrega) vira ``5``, e um texto
    só com dígitos vira ``int`` sem passar por ``float`` (que perderia dígitos).
    """
    if isinstance(value, bool):
        raise _FieldError("Informe um número.")
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER:
            raise _FieldError(_TOO_BIG)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _FieldError("Informe um número.")
        if value.is_integer() and abs(value) <= _MAX_EXACT_FLOAT_INT:
            return int(value)
        return _typeable_number(value)
    text = _as_text(value).replace(" ", "")
    # Em pt-BR a vírgula separa os decimais e o ponto, os milhares ("1.234,5" ou "1.000").
    if "," in text or _THOUSANDS.fullmatch(text):
        text = text.replace(".", "").replace(",", ".")
    if not _NUMBER_TEXT.fullmatch(text):
        raise _FieldError("Informe um número.")
    if re.fullmatch(r"-?[0-9]+", text):
        if len(text.lstrip("-")) > 18:
            raise _FieldError(_TOO_BIG)
        return int(text)
    try:
        number = float(text)
    except ValueError:
        raise _FieldError("Informe um número.") from None
    if not math.isfinite(number):
        raise _FieldError("Informe um número.")
    return _typeable_number(number)


_BR_DATE = re.compile(r"^([0-9]{2})/([0-9]{2})/([0-9]{4})$")
_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")


def _to_date(value: Any) -> str:
    text = _as_text(value)
    try:
        if match := _BR_DATE.fullmatch(text):
            day, month, year = (int(g) for g in match.groups())
            return date(year, month, day).isoformat()
        if match := _ISO_DATE.fullmatch(text):
            year, month, day = (int(g) for g in match.groups())
            return date(year, month, day).isoformat()
    except ValueError:
        pass
    raise _FieldError(f"Data inválida; use o formato {_DATE_HINT}.")


_CPF_FORMAT = re.compile(r"^[0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2}$")
_CNPJ_FORMAT = re.compile(r"^[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?[0-9]{2}$")
_MONEY_INPUT = re.compile(r"^-?(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)(?:,[0-9]{1,2})?$")


def _to_cpf(value: Any) -> str:
    text = _as_text(value)
    if not _CPF_FORMAT.fullmatch(text):
        raise _FieldError("CPF inválido; use o formato 000.000.000-00.")
    if not cpf_valido(text):
        raise _FieldError("CPF inválido: confira os dígitos.")
    return _format_cpf(text)


def _to_cnpj(value: Any) -> str:
    text = _as_text(value).upper()
    if not _CNPJ_FORMAT.fullmatch(text):
        raise _FieldError("CNPJ inválido; use o formato 00.000.000/0000-00.")
    if not cnpj_valido(text):
        raise _FieldError("CNPJ inválido: confira os dígitos verificadores.")
    return _format_cnpj(text)


def _to_money(value: Any) -> str:
    """Valor em reais no formato do esquema: ``1.234,56``."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = Decimal(str(value))
    else:
        text = _as_text(value).replace("R$", "").replace(" ", "")
        if not _MONEY_INPUT.fullmatch(text):
            raise _FieldError("Valor inválido; use o formato 1.234,56.")
        try:
            amount = Decimal(text.replace(".", "").replace(",", "."))
        except InvalidOperation:  # pragma: no cover - a expressão já garante
            raise _FieldError("Valor inválido; use o formato 1.234,56.") from None
    if not amount.is_finite():
        raise _FieldError("Valor inválido; use o formato 1.234,56.")
    # Mais de 20 dígitos inteiros nunca cabem (o limite abaixo é menor); recusar antes
    # evita que o arredondamento passe da precisão do Decimal (InvalidOperation).
    if amount and amount.adjusted() >= 20:
        raise _FieldError("Valor grande demais.")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation:  # pragma: no cover - barrado pelo limite acima
        raise _FieldError("Valor grande demais.") from None
    if quantized != amount:
        raise _FieldError("Use no máximo dois dígitos de centavos.")
    sign = "-" if quantized < 0 else ""
    whole, _, cents = f"{abs(quantized):.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    result = f"{sign}{grouped},{cents}"
    if len(result.lstrip("-").split(",")[0]) > 20:
        raise _FieldError("Valor grande demais.")
    return result


def _to_string(spec: FormField, value: Any) -> str:
    text = _as_text(value)
    if spec.max_length is not None and len(text) > spec.max_length:
        raise _FieldError(f"Use no máximo {spec.max_length} caracteres.")
    if spec.pattern is not None:
        try:
            if re.search(spec.pattern, text) is None:
                raise _FieldError("Formato inválido.")
        except re.error:
            pass
    return text


def _is_blank_row(row: Mapping[str, Any], spec: FormField) -> bool:
    return all(
        _blank(row.get(item.name)) or (item.type is FieldType.BOOLEAN and not row.get(item.name))
        for item in spec.item_fields
    )


def _to_list(spec: FormField, value: Any, errors: dict[str, str], path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _FieldError("Valor inválido.")
    rows = [row for row in value if isinstance(row, Mapping) and not _is_blank_row(row, spec)]
    if spec.max_items is not None and len(rows) > spec.max_items:
        raise _FieldError(f"Use no máximo {spec.max_items} linhas.")
    out: list[Any] = []
    for index, row in enumerate(rows, start=1):
        item: dict[str, Any] = {}
        for item_spec in spec.item_fields:
            try:
                present, converted = convert_field(item_spec, row.get(item_spec.name))
            except _FieldError as exc:
                errors[f"{path}[{index}].{item_spec.name}"] = (
                    f"Linha {index}, “{item_spec.title}”: {exc}"
                )
                continue
            if present:
                item[item_spec.name] = converted
        out.append(item)
    return out


def convert_field(spec: FormField, value: Any) -> tuple[bool, Any]:
    """Converte um valor digitado. Devolve ``(presente, valor)``.

    Campos opcionais vazios voltam como ausentes, para não mandar ``""`` ou
    ``null`` onde o esquema pede outro tipo. Levanta ``ValueError`` com a
    mensagem em pt-BR quando o valor não serve.
    """
    if spec.type is FieldType.BOOLEAN:
        if value is None:
            return (True, False) if spec.required else (False, None)
        if not isinstance(value, bool):
            raise _FieldError("Valor inválido.")
        return True, value
    if spec.type is FieldType.LIST:
        errors: dict[str, str] = {}
        rows = _to_list(spec, value if value is not None else [], errors, spec.name)
        if errors:
            raise _FieldError(" ".join(errors.values()))
        if not rows:
            if spec.required:
                raise _FieldError("Preencha ao menos uma linha.")
            return False, None
        return True, rows
    if _blank(value):
        if spec.required:
            raise _FieldError(_REQUIRED)
        return False, None
    match spec.type:
        case FieldType.INTEGER:
            return True, _to_integer(value)
        case FieldType.NUMBER:
            return True, _to_number(value)
        case FieldType.DATE:
            return True, _to_date(value)
        case FieldType.CPF:
            return True, _to_cpf(value)
        case FieldType.CNPJ:
            return True, _to_cnpj(value)
        case FieldType.MONEY:
            return True, _to_money(value)
        case _:
            return True, _to_string(spec, value)


def field_error(spec: FormField, value: Any) -> str | None:
    """Mensagem do problema no valor, ou None (usada na conferência campo a campo)."""
    try:
        convert_field(spec, value)
    except ValueError as exc:
        return str(exc)
    return None


def convert_values(fields: Sequence[FormField], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Converte todos os valores no JSON das entradas, ou levanta :class:`FormValidationError`."""
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for spec in fields:
        if spec.type is FieldType.LIST:
            value = raw.get(spec.name)
            try:
                rows = _to_list(spec, value if value is not None else [], errors, spec.name)
            except _FieldError as exc:
                errors[spec.name] = str(exc)
                continue
            if rows:
                out[spec.name] = rows
            elif spec.required:
                errors.setdefault(spec.name, "Preencha ao menos uma linha.")
            continue
        try:
            present, value = convert_field(spec, raw.get(spec.name))
        except _FieldError as exc:
            errors[spec.name] = str(exc)
            continue
        if present:
            out[spec.name] = value
    if errors:
        raise FormValidationError(errors)
    return out


_SERVER_PATH = re.compile(r"^(?:entradas\.|/)?([a-z_][a-z0-9_]{0,63})\b")


def split_server_errors(
    fields: Sequence[FormField], errors: Sequence[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Separa os erros devolvidos pelo servidor entre os campos e o restante.

    Um erro vai para um campo quando começa pelo nome dele (por exemplo,
    ``"cpf: formato inválido"`` ou ``"entradas.cpf: …"``) ou cita o nome no
    fim (``"entrada obrigatória ausente: cpf"``).
    """
    names = {spec.name for spec in fields}
    by_field: dict[str, list[str]] = {}
    general: list[str] = []
    for message in errors:
        target: str | None = None
        match = _SERVER_PATH.match(message)
        if match and match.group(1) in names:
            # O nome precisa vir seguido de ":", "[" ou ".", e não de uma palavra.
            if message[match.end() :].lstrip()[:1] in {":", "[", "."}:
                target = match.group(1)
        if target is None and ":" in message:
            tail = message.rsplit(":", 1)[1].strip()
            head = re.split(r"[.\[]", tail, maxsplit=1)[0]
            target = head if head in names else None
        if target is None:
            general.append(message)
        else:
            by_field.setdefault(target, []).append(message)
    return by_field, general


# ---------------------------------------------------------------------------
# Campos no NiceGUI
# ---------------------------------------------------------------------------

type _Widget = ui.input | ui.number | ui.checkbox


def _label(spec: FormField) -> str:
    return f"{spec.title} *" if spec.required else spec.title


def _validator(spec: FormField) -> Callable[[Any], str | None]:
    def check(value: Any) -> str | None:
        return field_error(spec, value)

    return check


def _build_widget(spec: FormField, marker: str) -> _Widget:
    """Um campo simples, com máscara ou dica conforme o tipo."""
    label = _label(spec)
    widget: _Widget
    match spec.type:
        case FieldType.BOOLEAN:
            widget = ui.checkbox(spec.title)
        case FieldType.INTEGER:
            widget = ui.number(label, precision=0, validation=_validator(spec))
        case FieldType.NUMBER:
            widget = ui.number(label, validation=_validator(spec))
        case FieldType.DATE:
            widget = ui.input(label, placeholder=_DATE_HINT, validation=_validator(spec))
            widget.props["mask"] = "##/##/####"
        case FieldType.CPF:
            widget = ui.input(label, placeholder="000.000.000-00", validation=_validator(spec))
            widget.props["mask"] = "###.###.###-##"
        case FieldType.CNPJ:
            widget = ui.input(label, placeholder="00.000.000/0000-00", validation=_validator(spec))
            # X: letra ou dígito, em maiúsculas (CNPJ alfanumérico).
            widget.props["mask"] = "XX.XXX.XXX/XXXX-##"
        case FieldType.MONEY:
            widget = ui.input(label, placeholder="0,00", prefix="R$", validation=_validator(spec))
            widget.props["inputmode"] = "decimal"
        case FieldType.FILE:
            widget = ui.input(
                label,
                placeholder="identificador do arquivo enviado",
                validation=_validator(spec),
            )
        case _:
            widget = ui.input(label, validation=_validator(spec))
            if spec.max_length is not None:
                widget.props["maxlength"] = spec.max_length
    if spec.description and not isinstance(widget, ui.checkbox):
        widget.props["hint"] = spec.description
    widget.mark(marker)
    if not isinstance(widget, ui.checkbox):
        widget.without_auto_validation()
        widget.on("blur", lambda _e, w=widget: w.validate(return_result=False))
    return widget


def _show_error(widget: _Widget, message: str | None) -> None:
    if isinstance(widget, (ui.input, ui.number)):
        widget.error = message


def _widget_value(widget: _Widget) -> Any:
    return widget.value


@dataclass
class _ListEditor:
    """Lista de objetos como uma tabela editável: uma linha por item."""

    spec: FormField
    container: ui.column | None = None
    rows: list[dict[str, _Widget]] = field(default_factory=list)
    error_label: ui.label | None = None
    created: int = 0

    def build(self) -> None:
        with ui.card().classes("w-full").mark(f"campo-{self.spec.name}"):
            ui.label(_label(self.spec)).classes("text-subtitle1")
            if self.spec.description:
                ui.label(self.spec.description).classes("text-caption")
            with ui.row().classes("w-full items-center gap-2 font-bold"):
                for item in self.spec.item_fields:
                    ui.label(_label(item)).classes("w-40")
            self.container = ui.column().classes("w-full gap-1")
            self.error_label = ui.label("").classes("text-negative")
            self.error_label.visible = False
            ui.button(
                "Adicionar linha", icon="add", on_click=self.add_row, color="secondary"
            ).props("flat").mark(f"campo-{self.spec.name}-adicionar")
        self.add_row()

    def add_row(self) -> None:
        if self.container is None:
            raise RuntimeError("monte a lista antes de adicionar linhas")
        index = self.created
        self.created += 1
        widgets: dict[str, _Widget] = {}
        with self.container, ui.row().classes("w-full items-center gap-2 no-wrap") as row:
            for item in self.spec.item_fields:
                widget = _build_widget(item, f"campo-{self.spec.name}-{index}-{item.name}")
                widget.classes("w-40")
                widgets[item.name] = widget
            ui.button(icon="delete", on_click=lambda: self._remove(row, widgets)).props(
                "flat round dense"
            ).mark(f"campo-{self.spec.name}-{index}-remover")
        self.rows.append(widgets)

    def _remove(self, row: ui.row, widgets: dict[str, _Widget]) -> None:
        if widgets in self.rows:
            self.rows.remove(widgets)
        row.delete()

    def raw(self) -> list[dict[str, Any]]:
        return [{name: _widget_value(w) for name, w in row.items()} for row in self.rows]

    def show_errors(self, errors: Mapping[str, str]) -> None:
        """Erros por linha (chaves ``nome[linha].campo``) e da lista toda (chave ``nome``)."""
        rows = [row for row in self.rows if not _is_blank_row(self._raw_row(row), self.spec)]
        for row in self.rows:
            for widget in row.values():
                _show_error(widget, None)
        for key, message in errors.items():
            match = re.fullmatch(rf"{re.escape(self.spec.name)}\[([0-9]+)\]\.([a-z0-9_]+)", key)
            if match:
                index, item = int(match.group(1)) - 1, match.group(2)
                if 0 <= index < len(rows) and item in rows[index]:
                    _show_error(rows[index][item], message.split(": ", 1)[-1])
        general = [m for k, m in errors.items() if k == self.spec.name or "[" in k]
        self.set_general_error(" ".join(general) if general else None)

    def set_general_error(self, message: str | None) -> None:
        if self.error_label is not None:
            self.error_label.text = message or ""
            self.error_label.visible = bool(message)

    @staticmethod
    def _raw_row(row: Mapping[str, _Widget]) -> dict[str, Any]:
        return {name: _widget_value(w) for name, w in row.items()}


class SchemaForm:
    """Formulário NiceGUI montado a partir do esquema das entradas de um robô."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self.fields = fields_from_schema(schema)
        self._widgets: dict[str, _Widget] = {}
        self._lists: dict[str, _ListEditor] = {}
        self._general: ui.column | None = None

    def build(self) -> None:
        """Cria os campos no contêiner atual do NiceGUI."""
        with ui.column().classes("w-full gap-3").mark("formulario-entradas"):
            if not self.fields:
                ui.label("Este robô não tem entradas.")
            for spec in self.fields:
                if spec.type is FieldType.LIST:
                    editor = _ListEditor(spec)
                    editor.build()
                    self._lists[spec.name] = editor
                else:
                    widget = _build_widget(spec, f"campo-{spec.name}")
                    widget.classes("w-full max-w-xl")
                    self._widgets[spec.name] = widget
            self._general = ui.column().classes("text-negative").mark("erros-formulario")

    def raw_values(self) -> dict[str, Any]:
        raw: dict[str, Any] = {name: _widget_value(w) for name, w in self._widgets.items()}
        raw |= {name: editor.raw() for name, editor in self._lists.items()}
        return raw

    def submit(self) -> dict[str, Any] | None:
        """Confere tudo; mostra os erros e devolve None, ou devolve o JSON das entradas."""
        self._set_general([])
        try:
            values = convert_values(self.fields, self.raw_values())
        except FormValidationError as exc:
            self._show_errors(exc.errors)
            return None
        self._show_errors({})
        return values

    def _show_errors(self, errors: Mapping[str, str]) -> None:
        for name, widget in self._widgets.items():
            _show_error(widget, errors.get(name))
        for name, editor in self._lists.items():
            editor.show_errors(
                {k: v for k, v in errors.items() if k == name or k.startswith(f"{name}[")}
            )

    def show_server_errors(self, errors: Sequence[str]) -> None:
        """Mostra os erros devolvidos pelo servidor nos campos certos (ou embaixo)."""
        by_field, general = split_server_errors(self.fields, errors)
        for name, messages in by_field.items():
            text = " ".join(messages)
            if name in self._widgets:
                _show_error(self._widgets[name], text)
            elif name in self._lists:
                self._lists[name].set_general_error(text)
        self._set_general(general)

    def _set_general(self, messages: Sequence[str]) -> None:
        if self._general is None:
            return
        self._general.clear()
        with self._general:
            for message in messages:
                ui.label(message)
