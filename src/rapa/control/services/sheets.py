"""Planilhas dos lotes: leitura (CSV e xlsx), conversão para as entradas e o resultado.

Lógica pura, sem banco, usada por :class:`rapa.control.services.batches.PgBatches`.

**Leitura.**

- O formato vem do conteúdo: um arquivo zip é xlsx (``.xlsx``), o resto é CSV
  (``.csv``). Outras extensões (``.xls``, ``.xlsm``, ``.ods``...) são recusadas.
- CSV: UTF-8 (com ou sem BOM) ou, se não for, Windows-1252 (o "CSV" do Excel em
  português). O separador é o mais frequente da linha dos títulos entre ``;``,
  ``,`` e tabulação. Nenhum valor é avaliado.
- xlsx: lido pelo ``openpyxl`` em modo só leitura, com o ``defusedxml`` contra XML
  malicioso, só a primeira aba. **Fórmulas são recusadas** (a planilha precisa ter
  só valores; nada é calculado e nenhum valor guardado de fórmula é usado), assim
  como células com erro (``#N/A``), macros (``vbaProject.bin``) e um conteúdo que,
  descompactado, passa do limite (bomba de zip).
- A primeira linha tem os títulos das colunas; linhas totalmente vazias são
  puladas. "Linha" nas mensagens é a linha da planilha (a dos títulos é a 1).
- Limites (``SheetLimits``): tamanho do arquivo, linhas e colunas, e também o que
  a leitura produz, sem confiar no que o arquivo declara: a ``<dimension>`` da aba
  é ignorada, cada linha para uma coluna depois do limite, a leitura para depois
  de ``max_rows`` mais ``max_blank_lines`` linhas (vazias ou não, também no CSV) e
  só as linhas com algo ficam na memória. No xlsx, no máximo 64 abas (cada uma
  num arquivo, nenhuma de gráfico), e cada parte que o ``openpyxl`` monta inteira (estilos, pasta de
  trabalho, relações, propriedades, abas de gráfico) tem até ``max_part_bytes``
  descompactada; só as abas e os textos compartilhados, que ele lê aos poucos,
  podem ser maiores, dentro do total de ``max_unzipped_bytes``.

**Colunas e entradas.** Cada coluna vai para a entrada do robô com o mesmo nome
técnico ou o mesmo título (sem diferença de maiúsculas, acentos, espaços e
``_``), ou para a que o mapa explícito disser. Colunas que não casam com
nenhuma entrada são ignoradas (e aparecem na prévia). Falta a coluna de uma
entrada obrigatória, duas colunas para a mesma entrada, uma entrada do tipo
lista (que não cabe numa célula): erro da planilha inteira.

**Conversão.** Uma célula vazia é uma entrada ausente (a obrigatória dá erro).
Números do xlsx viram texto sem ``.0`` (e o CPF e o CNPJ ganham os zeros da
esquerda que o Excel tira); dinheiro vira ``1234,50``; datas aceitam
``DD/MM/AAAA`` e ``AAAA-MM-DD``; números aceitam a vírgula decimal (``1.234,56``;
sem vírgula, o ponto é o separador decimal, ``1.5``); verdadeiro ou falso aceita
``sim``/``não``, ``verdadeiro``/``falso``, ``1``/``0``.
Depois, cada linha passa pelo mesmo validador das execuções; as mensagens nunca
repetem os valores.

**Resultado.** Uma linha por linha do lote: o número da linha da planilha, a
execução, a situação, os horários, **só as saídas que o robô declara** e o erro.
As entradas não vão para o arquivo (podem ser sensíveis). No CSV, todo campo vai
entre aspas, e um ``=``, ``+``, ``-``, ``@``, tabulação ou retorno no começo do
campo ou logo depois de um separador (``;``, ``,``, tabulação ou quebra de linha)
ganha um apóstrofo antes: nem um Excel que separa as colunas por vírgula abre
uma célula que comece com fórmula. No xlsx, toda célula de texto é gravada como
texto (nada vira fórmula), sem os caracteres de controle que o XML não aceita.
"""

import csv
import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from rapa.control.validation import InputValidator
from rapa.core.contracts import (
    BatchColumn,
    BatchFormat,
    BatchRow,
    BatchRowError,
    InvalidInput,
    RunState,
)
from rapa.core.robot import FieldType, InputField, Manifest, OutputField

__all__ = [
    "CSV_MEDIA_TYPE",
    "XLSX_MEDIA_TYPE",
    "RowPlan",
    "Sheet",
    "SheetLimits",
    "plan_rows",
    "read_sheet",
    "render_results",
]

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Quantos erros de linha, no máximo, a prévia mostra.
MAX_ROW_ERRORS_SHOWN = 200
#: Quantas linhas convertidas a prévia mostra.
SAMPLE_ROWS = 5

_ZIP_MAGIC = b"PK\x03\x04"
#: Quantas abas, no máximo, o xlsx pode ter (o openpyxl abre todas).
_MAX_SHEETS = 64
#: Elementos e atributos, por coluna do limite, de um pedaço que o openpyxl monta
#: inteiro (uma linha da aba, um texto compartilhado, um elemento solto da aba).
_ITEMS_PER_COLUMN = 64
#: Elementos e atributos, por célula do limite, da aba ou dos textos inteiros.
_ITEMS_PER_CELL = 8
#: Bytes, no máximo, sem nenhum elemento completo (uma marcação ou um texto gigante).
_MAX_TOKEN_BYTES = 1024 * 1024
_ALLOWED_EXTENSIONS = {".csv": BatchFormat.CSV, ".xlsx": BatchFormat.XLSX}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
#: Um começo de fórmula logo depois de um separador de campo ou de linha (de qualquer
#: Excel: ``;``, ``,`` ou tabulação), onde outro programa pode começar uma célula.
_FORMULA_AFTER_SEPARATOR = re.compile(r"(?<=[;,\t\r\n])(?=[=+\-@\t\r])")
_TRUE = {"sim", "s", "verdadeiro", "v", "true", "t", "1", "x", "yes", "y"}
_FALSE = {"nao", "não", "n", "falso", "f", "false", "0", "no"}
_DATE_BR = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_INTEGER = re.compile(r"^[+-]?\d{1,18}$")
_NUMBER_BR = re.compile(r"^[+-]?\d{1,3}(\.\d{3})*(,\d+)?$|^[+-]?\d+(,\d+)?$")
_NUMBER_DOT = re.compile(r"^[+-]?\d+(\.\d+)?$")
_STATE_LABELS = {
    RunState.QUEUED: "Na fila",
    RunState.RUNNING: "Em execução",
    RunState.AWAITING_HUMAN: "Aguardando uma pessoa",
    RunState.SUCCEEDED: "Concluída",
    RunState.FAILED: "Falhou",
    RunState.CANCELLED: "Cancelada",
    RunState.INDETERMINATE: "Indeterminada",
}


@dataclass(frozen=True, slots=True)
class SheetLimits:
    """Limites da planilha de um lote."""

    max_bytes: int = 5 * 1024 * 1024
    max_rows: int = 1000
    max_columns: int = 100
    #: Tamanho máximo do xlsx descompactado (contra bomba de zip).
    max_unzipped_bytes: int = 64 * 1024 * 1024
    #: Tamanho máximo, descompactado, de cada parte que o openpyxl monta inteira na
    #: memória (estilos, pasta de trabalho, relações...): só a aba e os textos
    #: compartilhados são lidos aos poucos.
    max_part_bytes: int = 1024 * 1024
    #: Quantas linhas vazias (no meio ou no fim) a planilha pode ter além das ``max_rows``.
    max_blank_lines: int = 10_000

    def __post_init__(self) -> None:
        if self.max_bytes < 1 or self.max_rows < 1 or self.max_columns < 1:
            raise ValueError("os limites da planilha precisam ser positivos")


@dataclass(frozen=True, slots=True)
class Sheet:
    """Uma planilha lida: os títulos e as linhas (com o número da linha na planilha)."""

    format: BatchFormat
    headers: list[str]
    rows: list[tuple[int, list[Any]]]


def _refuse(message: str, *details: str) -> InvalidInput:
    return InvalidInput(message, list(details))


def _format_of(file_name: str, data: bytes) -> BatchFormat:
    name = file_name.lower().strip()
    dot = name.rfind(".")
    extension = name[dot:] if dot >= 0 else ""
    if extension and extension not in _ALLOWED_EXTENSIONS:
        raise _refuse(
            "formato de planilha não aceito", "envie um arquivo .xlsx ou .csv (sem macros)"
        )
    if data.startswith(_ZIP_MAGIC):
        if extension == ".csv":
            raise _refuse("o arquivo .csv parece ser uma planilha do Excel; salve-o como .xlsx")
        return BatchFormat.XLSX
    if extension == ".xlsx":
        raise _refuse("o arquivo .xlsx está corrompido ou não é uma planilha do Excel")
    return BatchFormat.CSV


def _clean_header(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return " ".join(text.split())


def _finish(
    format: BatchFormat, raw_rows: list[tuple[int, list[Any]]], limits: SheetLimits
) -> Sheet:
    if not raw_rows:
        raise _refuse(
            "a planilha está vazia", "a primeira linha precisa ter os títulos das colunas"
        )
    _, header_cells = raw_rows[0]
    headers = [_clean_header(cell) for cell in header_cells]
    while headers and not headers[-1]:
        headers.pop()
    if not headers:
        raise _refuse(
            "a planilha está vazia", "a primeira linha precisa ter os títulos das colunas"
        )
    if len(headers) > limits.max_columns:
        raise _refuse(f"a planilha passa do limite de {limits.max_columns} colunas")
    seen: set[str] = set()
    for index, header in enumerate(headers):
        if not header:
            raise _refuse(f"a coluna {_column_letter(index)} não tem título na primeira linha")
        key = _fold(header)
        if key in seen:
            raise _refuse(f"a coluna '{header[:80]}' aparece duas vezes")
        seen.add(key)
    rows: list[tuple[int, list[Any]]] = []
    for line, cells in raw_rows[1:]:
        if all(_is_empty(cell) for cell in cells):
            continue
        extra = cells[len(headers) :]
        if any(not _is_empty(cell) for cell in extra):
            raise _refuse(f"a linha {line} tem valores em colunas sem título")
        padded = list(cells[: len(headers)]) + [None] * (len(headers) - len(cells))
        rows.append((line, padded))
        if len(rows) > limits.max_rows:
            raise _refuse(f"o lote passa do limite de {limits.max_rows} linhas")
    if not rows:
        raise _refuse("a planilha não tem nenhuma linha além dos títulos")
    return Sheet(format=format, headers=headers, rows=rows)


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def _is_empty(cell: Any) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _read_csv(data: bytes, limits: SheetLimits) -> Sheet:
    if b"\x00" in data:
        raise _refuse("o arquivo .csv tem caracteres inválidos (salve como CSV UTF-8)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            raise _refuse("não foi possível ler o arquivo .csv; salve como CSV UTF-8") from None
    first_line = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=first_line.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    raw: list[tuple[int, list[Any]]] = []
    filled = 0
    try:
        for record, cells in enumerate(reader, start=1):
            if record > limits.max_rows + limits.max_blank_lines + 1:
                raise _refuse(f"o lote passa do limite de {limits.max_rows} linhas")
            # Só contam (e só ficam guardadas) as linhas com algo, além da dos títulos.
            if record == 1:
                raw.append((record, list(cells)))
            elif any(cell.strip() for cell in cells):
                raw.append((record, list(cells)))
                filled += 1
                if filled > limits.max_rows:
                    raise _refuse(f"o lote passa do limite de {limits.max_rows} linhas")
    except csv.Error:
        raise _refuse("o arquivo .csv está mal formado (aspas sem fechar?)") from None
    return _finish(BatchFormat.CSV, raw, limits)


def _check_parts(
    data: bytes, sizes: Mapping[str, int], limits: SheetLimits
) -> tuple[str, str | None]:
    """Só as abas e os textos compartilhados, que o openpyxl lê aos poucos, podem ser grandes.

    As outras partes (o manifesto, a pasta de trabalho, as relações, os estilos, as
    propriedades, as abas de gráfico...) ele monta inteiras na memória, e um arquivo
    pequeno e repetitivo vira gigabytes: cada uma fica em ``max_part_bytes``. As
    abas e os textos são os que o próprio openpyxl vai ler aos poucos, achados do
    mesmo jeito que ele acha (manifesto e relações da pasta de trabalho).
    """
    from openpyxl.packaging.manifest import Manifest as Package
    from openpyxl.packaging.relationship import get_rels_path
    from openpyxl.reader.excel import _find_workbook_part
    from openpyxl.reader.workbook import WorkbookParser
    from openpyxl.xml.constants import (
        ARC_CONTENT_TYPES,
        ARC_CORE,
        ARC_CUSTOM,
        ARC_ROOT_RELS,
        ARC_STYLE,
        SHARED_STRINGS,
    )
    from openpyxl.xml.functions import fromstring

    too_big = _refuse("a planilha descompactada é grande demais")

    def small(name: str) -> None:
        if sizes.get(name, 0) > limits.max_part_bytes:
            raise too_big

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            small(ARC_CONTENT_TYPES)
            package = Package.from_tree(fromstring(archive.read(ARC_CONTENT_TYPES)))
            workbook = _find_workbook_part(package).PartName[1:]
            small(workbook)
            small(get_rels_path(workbook))
            parser = WorkbookParser(archive, workbook, keep_links=False)
            parser.parse()
            sheets = [rel for _, rel in parser.find_sheets()]
            strings = package.find(SHARED_STRINGS)
    except InvalidInput:
        raise
    except Exception:  # o openpyxl levanta vários tipos para arquivos ruins
        raise _refuse("não foi possível ler o arquivo .xlsx") from None
    targets = [rel.target for rel in sheets]
    if len(targets) > _MAX_SHEETS or len(set(targets)) != len(targets):
        raise _refuse(f"a planilha precisa ter até {_MAX_SHEETS} abas, cada uma num arquivo")
    # Uma aba de gráfico o openpyxl monta inteira, com os desenhos, os gráficos e as
    # imagens dela (pelo mesmo critério que ele usa para reconhecê-la).
    if any("chartsheet" in rel.Type for rel in sheets):
        raise _refuse("a planilha tem abas de gráfico; deixe só as abas com os dados")
    streamed = {rel.target for rel in sheets if "chartsheet" not in rel.Type}
    streamed -= {rel.target for rel in sheets if "chartsheet" in rel.Type}
    if strings is not None:
        streamed.add(strings.PartName[1:])
    streamed -= {ARC_CONTENT_TYPES, ARC_ROOT_RELS, ARC_STYLE, ARC_CORE, ARC_CUSTOM, workbook}
    whole = 0
    for name, size in sizes.items():
        if name not in streamed or name.endswith(".rels"):
            whole += size
            if size > limits.max_part_bytes:
                raise too_big
    # O openpyxl lê inteiras, também no modo só leitura, as relações de cada aba: muitas
    # abas pequenas somariam segundos de CPU.
    if whole > 2 * limits.max_part_bytes:
        raise too_big
    if not targets:
        raise _refuse("a planilha não tem nenhuma aba")
    # A primeira aba é a que o lote lê; os textos compartilhados são lidos inteiros.
    return targets[0], strings.PartName[1:] if strings is not None else None


def _check_shape(data: bytes, name: str, limits: SheetLimits, *, sheet: bool) -> None:
    """Percorre uma parte lida aos poucos (a aba ou os textos) sem montar nada.

    O openpyxl lê a aba e os textos compartilhados aos poucos, mas monta inteiro
    cada pedaço (uma linha, um texto, uma lista de células mescladas, um elemento
    que ele não conhece e guarda): um arquivo de poucos KB com um pedaço de milhões
    de elementos vira gigabytes. Antes dele, o expat confere: cada pedaço com até
    ``64`` elementos e atributos por coluna do limite, a parte inteira com até
    ``8`` por célula do limite (fora das linhas da aba, ``8`` pedaços) e nada de
    1 MiB sem um elemento completo.
    """
    import xml.parsers.expat

    from openpyxl.xml.constants import SHEET_MAIN_NS

    piece_limit = _ITEMS_PER_COLUMN * (limits.max_columns + 1)
    cells = (limits.max_rows + 1) * (limits.max_columns + 1) + limits.max_blank_lines
    total_limit = _ITEMS_PER_CELL * cells
    rows_tag = f"{SHEET_MAIN_NS} sheetData" if sheet else None
    too_big = _refuse("a planilha tem uma parte grande demais para ler")
    # Profundidade atual, a do pedaço em andamento (0: nenhum) e a do sheetData.
    state = {"depth": 0, "piece": 0, "rows": 0, "items": 0, "total": 0, "outside": 0, "seen": 0}
    parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")

    def start(tag: str, attrs: dict[str, str]) -> None:
        state["seen"] = parser.CurrentByteIndex
        depth = state["depth"] = state["depth"] + 1
        items = 1 + len(attrs)
        state["total"] += items
        if state["total"] > total_limit:
            raise too_big
        # Fora das linhas da aba (formatação, validações, elementos soltos), o
        # openpyxl guarda tudo o que montou: no máximo oito pedaços inteiros.
        if sheet and depth > 1 and not (state["rows"] and depth > state["rows"]):
            state["outside"] += items
            if state["outside"] > 8 * piece_limit:
                raise too_big
        if state["piece"]:
            state["items"] += items
        elif depth == 2 and tag == rows_tag:
            state["rows"] = depth  # as linhas dentro dele é que são os pedaços
        elif depth == 2 or (state["rows"] and depth == state["rows"] + 1):
            state["piece"], state["items"] = depth, items
        if state["items"] > piece_limit:
            raise too_big

    def end(tag: str) -> None:
        state["seen"] = parser.CurrentByteIndex
        if state["depth"] == state["piece"]:
            state["piece"] = state["items"] = 0
        if state["depth"] == state["rows"]:
            state["rows"] = 0
        state["depth"] -= 1

    def entity(*_: object) -> None:
        raise _refuse("a planilha declara entidades XML, o que não é aceito")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.EntityDeclHandler = entity
    fed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive, archive.open(name) as stream:
            while chunk := stream.read(64 * 1024):
                parser.Parse(chunk, False)
                fed += len(chunk)
                # Uma marcação (ou um texto) enorme sem fim à vista: recusa antes de o
                # expat montá-la (milhões de atributos num elemento, por exemplo).
                if fed - state["seen"] > _MAX_TOKEN_BYTES:
                    raise too_big
            parser.Parse(b"", True)
    except InvalidInput:
        raise
    except Exception:  # XML malformado, parte que falta, zip ruim
        raise _refuse("não foi possível ler o arquivo .xlsx") from None


def _read_xlsx(data: bytes, limits: SheetLimits) -> Sheet:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
    except zipfile.BadZipFile:
        raise _refuse("o arquivo .xlsx está corrompido ou não é uma planilha do Excel") from None
    if sum(member.file_size for member in members) > limits.max_unzipped_bytes:
        raise _refuse("a planilha descompactada é grande demais")
    if any(member.filename.lower().endswith("vbaproject.bin") for member in members):
        raise _refuse("planilhas com macros não são aceitas; salve como .xlsx sem macros")
    first, strings = _check_parts(
        data, {member.filename: member.file_size for member in members}, limits
    )
    _check_shape(data, first, limits, sheet=True)
    if strings is not None:
        _check_shape(data, strings, limits, sheet=False)
    import openpyxl  # só aqui: a leitura de CSV não precisa dele

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
    except Exception:  # o openpyxl levanta vários tipos para arquivos ruins
        raise _refuse("não foi possível ler o arquivo .xlsx") from None
    try:
        if not workbook.worksheets:
            raise _refuse("a planilha não tem nenhuma aba")
        sheet = workbook.worksheets[0]
        # A <dimension> do arquivo não decide quantas células são lidas, e cada linha
        # para numa coluna além do limite (a que tiver título lá é recusada depois).
        sheet.reset_dimensions()
        max_lines = limits.max_rows + limits.max_blank_lines + 1
        raw: list[tuple[int, list[Any]]] = []
        filled = 0
        for line, cells in enumerate(sheet.iter_rows(max_col=limits.max_columns + 1), start=1):
            if line > max_lines:
                raise _refuse(f"o lote passa do limite de {limits.max_rows} linhas")
            values: list[Any] = []
            for cell in cells:
                kind = getattr(cell, "data_type", None)
                if kind == "f":
                    raise _refuse(
                        f"a planilha tem fórmulas (a primeira na linha {line}); "
                        "salve uma cópia só com os valores"
                    )
                if kind == "e":
                    raise _refuse(f"a planilha tem uma célula com erro na linha {line}")
                values.append(getattr(cell, "value", None))
            if line == 1:
                raw.append((line, values))
            elif any(not _is_empty(value) for value in values):
                raw.append((line, values))
                filled += 1
                if filled > limits.max_rows:
                    raise _refuse(f"o lote passa do limite de {limits.max_rows} linhas")
    finally:
        workbook.close()
    return _finish(BatchFormat.XLSX, raw, limits)


def read_sheet(file_name: str, data: bytes, limits: SheetLimits) -> Sheet:
    """Lê a planilha; levanta :class:`InvalidInput` com o motivo em pt-BR."""
    if not data:
        raise _refuse("o arquivo está vazio")
    if len(data) > limits.max_bytes:
        raise _refuse(f"o arquivo passa do limite de {limits.max_bytes // 1024} KiB")
    format = _format_of(file_name, data)
    if format is BatchFormat.XLSX:
        return _read_xlsx(data, limits)
    return _read_csv(data, limits)


# ---------------------------------------------------------------------------
# Colunas → entradas
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(stripped.casefold().replace("_", " ").split())


@dataclass(frozen=True, slots=True)
class RowPlan:
    """O que cada linha vira: as entradas das linhas boas e os erros das outras."""

    columns: list[BatchColumn]
    file_errors: list[str]
    rows: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    errors: list[BatchRowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.file_errors and not self.errors


def _map_columns(
    headers: Sequence[str], inputs: Sequence[InputField], explicit: Mapping[str, str] | None
) -> tuple[list[BatchColumn], list[str]]:
    by_name = {spec.name: spec for spec in inputs}
    by_fold: dict[str, InputField] = {}
    for spec in inputs:
        by_fold.setdefault(_fold(spec.name), spec)
        by_fold.setdefault(_fold(spec.title), spec)
    errors: list[str] = []
    explicit_folded: dict[str, str] = {}
    for header, name in (explicit or {}).items():
        if name not in by_name:
            errors.append(f"o mapa das colunas cita uma entrada que o robô não tem: {name[:80]}")
            continue
        explicit_folded[_fold(header)] = name
    known = {_fold(h) for h in headers}
    for header in explicit or {}:
        if _fold(header) not in known:
            errors.append(
                f"o mapa das colunas cita uma coluna que a planilha não tem: {header[:80]}"
            )
    columns: list[BatchColumn] = []
    used: dict[str, str] = {}
    for header in headers:
        key = _fold(header)
        target: InputField | None
        if key in explicit_folded:
            target = by_name[explicit_folded[key]]
        else:
            target = by_fold.get(key)
        if target is not None and target.type is FieldType.LIST:
            errors.append(
                f"a entrada '{target.name}' é uma lista e não cabe numa coluna da planilha"
            )
            target = None
        if target is not None and target.name in used:
            errors.append(
                f"as colunas '{used[target.name][:80]}' e '{header[:80]}' vão para a mesma "
                f"entrada '{target.name}'"
            )
            target = None
        if target is not None:
            used[target.name] = header
        columns.append(BatchColumn(header=header, input=target.name if target else None))
    for spec in inputs:
        if spec.required and spec.name not in used:
            if spec.type is FieldType.LIST:
                errors.append(
                    f"o robô pede a lista '{spec.name}' ({spec.title}), que não cabe numa planilha"
                )
            else:
                errors.append(f"falta a coluna da entrada obrigatória '{spec.name}' ({spec.title})")
    return columns, errors


def _number_text(value: int | float | Decimal) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value)


def _convert(spec: InputField, value: Any) -> tuple[Any, str | None]:
    """O valor para a entrada, ou o motivo de não dar (sem repetir o valor)."""
    kind = spec.type
    if isinstance(value, str):
        value = value.strip()
    if isinstance(value, bool):
        if kind is FieldType.BOOLEAN:
            return value, None
        if kind in (FieldType.INTEGER, FieldType.NUMBER):
            return None, "esperado número"
        return ("sim" if value else "não"), None
    if kind is FieldType.BOOLEAN:
        if isinstance(value, int | float) and value in (0, 1):
            return bool(value), None
        if isinstance(value, str) and value.casefold() in _TRUE:
            return True, None
        if isinstance(value, str) and value.casefold() in _FALSE:
            return False, None
        return None, "esperado sim ou não"
    if kind is FieldType.INTEGER:
        if isinstance(value, int):
            return value, None
        if isinstance(value, float) and value.is_integer():
            return int(value), None
        if isinstance(value, str) and _INTEGER.fullmatch(value):
            return int(value), None
        return None, "esperado número inteiro"
    if kind is FieldType.NUMBER:
        if isinstance(value, int | float):
            return value, None
        if isinstance(value, str):
            try:
                if _NUMBER_DOT.fullmatch(value) and "," not in value:
                    number = Decimal(value)
                elif _NUMBER_BR.fullmatch(value):
                    number = Decimal(value.replace(".", "").replace(",", "."))
                else:
                    return None, "esperado número (use a vírgula para os decimais)"
            except InvalidOperation:
                return None, "esperado número"
            return (int(number) if number == number.to_integral_value() else float(number)), None
        return None, "esperado número"
    if kind is FieldType.DATE:
        if isinstance(value, datetime):
            return value.date().isoformat(), None
        if isinstance(value, date):
            return value.isoformat(), None
        if isinstance(value, str):
            if match := _DATE_BR.fullmatch(value):
                day, month, year = (int(part) for part in match.groups())
            elif match := _DATE_ISO.fullmatch(value):
                year, month, day = (int(part) for part in match.groups())
            else:
                return None, "esperada data (DD/MM/AAAA ou AAAA-MM-DD)"
            try:
                return date(year, month, day).isoformat(), None
            except ValueError:
                return None, "data que não existe"
        return None, "esperada data (DD/MM/AAAA ou AAAA-MM-DD)"
    # Os tipos de texto: texto, CPF, CNPJ, dinheiro e arquivo.
    if isinstance(value, datetime):
        return value.isoformat(), None
    if isinstance(value, date | time):
        return value.isoformat(), None
    if isinstance(value, int | float | Decimal):
        if kind is FieldType.MONEY:
            return f"{Decimal(str(value)):.2f}".replace(".", ","), None
        text = _number_text(value)
        if kind is FieldType.CPF and text.isdigit() and len(text) < 11:
            text = text.zfill(11)
        elif kind is FieldType.CNPJ and text.isdigit() and len(text) < 14:
            text = text.zfill(14)
        return text, None
    return str(value), None


def plan_rows(
    sheet: Sheet,
    manifest: Manifest,
    validator: InputValidator,
    columns: Mapping[str, str] | None = None,
) -> RowPlan:
    """Confere a planilha inteira contra as entradas do robô, sem criar nada."""
    mapped, file_errors = _map_columns(sheet.headers, manifest.inputs, columns)
    specs = {spec.name: spec for spec in manifest.inputs}
    plan = RowPlan(columns=mapped, file_errors=file_errors)
    if file_errors:
        return plan
    schema = manifest.input_json_schema()
    for line, cells in sheet.rows:
        inputs: dict[str, Any] = {}
        problems: list[str] = []
        for column, cell in zip(mapped, cells, strict=True):
            if column.input is None or _is_empty(cell):
                continue
            value, problem = _convert(specs[column.input], cell)
            if problem is not None:
                problems.append(f"{column.input}: {problem}")
            else:
                inputs[column.input] = value
        if not problems:
            problems = validator.errors(schema, inputs)
        if problems:
            plan.errors.append(BatchRowError(line=line, errors=problems))
        else:
            plan.rows.append((line, inputs))
    return plan


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=False, default=str)


def _csv_safe(text: str) -> str:
    """Um texto que o Excel nunca lê como fórmula, com qualquer separador de colunas."""
    text = _FORMULA_AFTER_SEPARATOR.sub("'", text)
    return "'" + text if text.startswith(_FORMULA_START) else text


def render_results(
    rows: Sequence[BatchRow], outputs: Sequence[OutputField], format: BatchFormat
) -> bytes:
    """O arquivo do resultado do lote (só as saídas declaradas, sem as entradas)."""
    names = [output.name for output in outputs]
    headers = [
        "linha",
        "execucao",
        "situacao",
        "inicio",
        "fim",
        *names,
        "erro",
        "mensagem",
    ]
    table: list[list[str]] = []
    for row in rows:
        values = row.outputs or {}
        table.append(
            [
                str(row.line),
                str(row.run_id),
                _STATE_LABELS.get(row.state, row.state.value),
                _cell_text(row.started_at),
                _cell_text(row.finished_at),
                *[_cell_text(values.get(name)) for name in names],
                row.error_code or "",
                row.error_message or "",
            ]
        )
    if format is BatchFormat.CSV:
        buffer = io.StringIO(newline="")
        # Tudo entre aspas: um separador dentro de um campo nunca abre outra coluna.
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_ALL)
        writer.writerow(headers)
        for line in table:
            writer.writerow([_csv_safe(cell) for cell in line])
        # Com BOM: o Excel em português abre o UTF-8 com os acentos certos.
        return buffer.getvalue().encode("utf-8-sig")
    import openpyxl
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    workbook = openpyxl.Workbook(write_only=False)
    sheet = workbook.active
    sheet.title = "Resultado"
    for line_no, line in enumerate([headers, *table], start=1):
        for column_no, text in enumerate(line, start=1):
            cell = sheet.cell(row=line_no, column=column_no)
            # O XML do xlsx não aceita alguns caracteres de controle (o openpyxl recusaria).
            cell.value = ILLEGAL_CHARACTERS_RE.sub("", text)
            cell.data_type = "s"  # texto, nunca fórmula
    buffer_bytes = io.BytesIO()
    workbook.save(buffer_bytes)
    return buffer_bytes.getvalue()
