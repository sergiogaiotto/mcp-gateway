"""Planilhas pequenas que o openpyxl expande demais: recusadas depressa e com pouca memória.

Cada caso roda a leitura num processo separado, com limite de memória e de tempo:
sem a proteção, o processo estoura a memória ou o prazo (e o teste falha).
"""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.worksheet+xml"/></Types>'
)
_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_WB = (
    f'<workbook xmlns="{_NS}" xmlns:r="{_REL}"><sheets>'
    '<sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WB_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
)
_HEADER = '<row r="1"><c r="A1" t="inlineStr"><is><t>nome</t></is></c></row>'
_ROW2 = '<row r="2"><c r="A2" t="inlineStr"><is><t>Maria</t></is></c></row>'

_RUNNER = """
import json, resource, sys
limit = int(sys.argv[2]) * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
from rapa.control.services.sheets import SheetLimits, read_sheet
from rapa.core.contracts import InvalidInput
data = open(sys.argv[1], "rb").read()
try:
    sheet = read_sheet(sys.argv[1], data, SheetLimits())
    outcome = {"rows": len(sheet.rows)}
except InvalidInput as exc:
    outcome = {"refused": exc.message}
except MemoryError:
    outcome = {"memory": True}
# VmHWM é o pico deste processo (o ru_maxrss herdaria o do pytest que o criou).
with open("/proc/self/status") as status:
    peak = next(line for line in status if line.startswith("VmHWM:"))
outcome["maxrss_mib"] = int(peak.split()[1]) // 1024
print(json.dumps(outcome))
"""


def _xlsx(sheet_xml: str, extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", _CT)
        archive.writestr("_rels/.rels", _RELS)
        parts = {
            "xl/workbook.xml": _WB,
            "xl/_rels/workbook.xml.rels": _WB_RELS,
            "xl/worksheets/sheet1.xml": sheet_xml,
            **(extra or {}),
        }
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _sheet(rows: str, dimension: str | None = None) -> str:
    dim = f'<dimension ref="{dimension}"/>' if dimension else ""
    return f'<worksheet xmlns="{_NS}">{dim}<sheetData>{rows}</sheetData></worksheet>'


def _read_isolated(tmp_path: Path, name: str, data: bytes, *, memory_mib: int) -> dict[str, object]:
    path = tmp_path / name
    path.write_bytes(data)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    try:
        done = subprocess.run(  # noqa: S603 - o próprio interpretador dos testes
            [sys.executable, "-c", _RUNNER, str(path), str(memory_mib)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("a leitura da planilha passou de 30 s")
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_dimension_with_a_far_row_is_refused_quickly(tmp_path: Path) -> None:
    # 1,6 KB: a <dimension> diz A1:XFD1048576 e há uma linha vazia lá no fim. Sem
    # proteção, o openpyxl entrega 1 milhão de linhas de 16 384 células.
    data = _xlsx(
        _sheet(_HEADER + _ROW2 + '<row r="1048576"><c r="A1048576"/></row>', "A1:XFD1048576")
    )
    assert len(data) < 4096
    outcome = _read_isolated(tmp_path, "a.xlsx", data, memory_mib=1024)
    assert "memory" not in outcome
    assert "refused" in outcome, outcome


def test_cell_in_the_last_column_of_every_row_is_refused_quickly(tmp_path: Path) -> None:
    # Sem <dimension>: cada linha vazia tem uma célula na coluna XFD (16 384 células por linha).
    rows = (
        _HEADER + _ROW2 + "".join(f'<row r="{n}"><c r="XFD{n}"/></row>' for n in range(3, 60_000))
    )
    outcome = _read_isolated(tmp_path, "a.xlsx", _xlsx(_sheet(rows)), memory_mib=1024)
    assert "memory" not in outcome
    assert outcome.get("rows") == 1 or "refused" in outcome, outcome


def test_huge_style_sheet_is_refused_before_openpyxl_parses_it(tmp_path: Path) -> None:
    # 89 KB compactado, 60 MB descompactado (abaixo dos 64 MiB): milhões de estilos vazios,
    # que o openpyxl monta inteiros na memória (gigabytes).
    count = 12_000_000
    styles = f'<styleSheet xmlns="{_NS}"><cellXfs count="{count}">' + "<xf/>" * count
    styles += "</cellXfs></styleSheet>"
    data = _xlsx(_sheet(_HEADER + _ROW2), {"xl/styles.xml": styles})
    assert len(data) < 200_000
    outcome = _read_isolated(tmp_path, "a.xlsx", data, memory_mib=1024)
    assert "memory" not in outcome
    assert "refused" in outcome, outcome
    # O MemoryError do openpyxl também vira recusa: a prova é a memória usada.
    assert int(outcome["maxrss_mib"]) < 300, outcome  # type: ignore[call-overload]


def test_csv_of_blank_lines_does_not_take_a_gigabyte(tmp_path: Path) -> None:
    # 5 MiB de linhas vazias: antes, uma tupla e uma lista por linha (cerca de 1 GB).
    data = b"nome\n" + b"\n" * (5 * 1024 * 1024 - 16)
    outcome = _read_isolated(tmp_path, "a.csv", data, memory_mib=2048)
    assert "refused" in outcome, outcome
    assert int(outcome["maxrss_mib"]) < 300, outcome  # type: ignore[call-overload]


def test_workbook_part_named_like_a_sheet_is_still_limited(tmp_path: Path) -> None:
    # A pasta de trabalho, montada inteira pelo openpyxl, com o nome de uma aba e 100 mil abas.
    sheets = "".join(f'<sheet name="S{i}" sheetId="{i + 1}" r:id="rId1"/>' for i in range(100_000))
    workbook = f'<workbook xmlns="{_NS}" xmlns:r="{_REL}"><sheets>{sheets}</sheets></workbook>'
    content_types = _CT.replace("/xl/workbook.xml", "/xl/worksheets/sheet9.xml")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels", _RELS.replace("xl/workbook.xml", "xl/worksheets/sheet9.xml")
        )
        archive.writestr("xl/worksheets/sheet9.xml", workbook)
        archive.writestr(
            "xl/worksheets/_rels/sheet9.xml.rels",
            _WB_RELS.replace("worksheets/sheet1.xml", "sheet1.xml"),
        )
        archive.writestr("xl/worksheets/sheet1.xml", _sheet(_HEADER + _ROW2))
    outcome = _read_isolated(tmp_path, "a.xlsx", buffer.getvalue(), memory_mib=1024)
    assert "refused" in outcome, outcome
    assert int(outcome["maxrss_mib"]) < 300, outcome  # type: ignore[call-overload]


def test_many_tabs_on_the_same_sheet_part_are_refused(tmp_path: Path) -> None:
    sheets = "".join(f'<sheet name="S{i}" sheetId="{i + 1}" r:id="rId1"/>' for i in range(200))
    workbook = f'<workbook xmlns="{_NS}" xmlns:r="{_REL}"><sheets>{sheets}</sheets></workbook>'
    data = _xlsx(_sheet(_HEADER + _ROW2), {"xl/workbook.xml": workbook})
    outcome = _read_isolated(tmp_path, "a.xlsx", data, memory_mib=1024)
    assert "abas" in str(outcome.get("refused", "")), outcome


_SST_TYPE = (
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
)


def _with_strings(sst_xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", _CT.replace("</Types>", _SST_TYPE))
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("xl/workbook.xml", _WB)
        archive.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", _sheet(_HEADER + _ROW2))
        archive.writestr("xl/sharedStrings.xml", sst_xml)
    return buffer.getvalue()


def _one_huge_piece(kind: str) -> bytes:
    # Cada caso tem um só pedaço que o openpyxl monta inteiro (milhões de elementos ou
    # de atributos), dentro do limite de 64 MiB descompactados.
    if kind == "linha":
        return _xlsx(_sheet(_HEADER + '<row r="2">' + "<c/>" * 15_000_000 + "</row>"))
    if kind == "texto_na_celula":
        cell = '<c r="A2" t="inlineStr"><is>' + "<r><t/></r>" * 5_000_000 + "</is></c>"
        return _xlsx(_sheet(_HEADER + f'<row r="2">{cell}</row>'))
    if kind == "elemento_solto":
        rows = f"<sheetData>{_HEADER}{_ROW2}</sheetData>"
        junk = "<extra>" + "<x/>" * 15_000_000 + "</extra>"
        return _xlsx(f'<worksheet xmlns="{_NS}">{junk}{rows}</worksheet>')
    if kind == "celulas_mescladas":
        merged = "<mergeCells>" + '<mergeCell ref="A1:B2"/>' * 2_500_000 + "</mergeCells>"
        rows = f"<sheetData>{_HEADER}{_ROW2}</sheetData>"
        return _xlsx(f'<worksheet xmlns="{_NS}">{merged}{rows}</worksheet>')
    if kind == "atributos":
        attrs = " ".join(f'a{i:x}=""' for i in range(2_000_000))
        return _xlsx(_sheet(_HEADER + f'<row r="2" {attrs}/>'))
    assert kind == "texto_compartilhado"
    return _with_strings(f'<sst xmlns="{_NS}"><si>' + "<r><t/></r>" * 5_000_000 + "</si></sst>")


@pytest.mark.parametrize(
    "kind",
    [
        "linha",
        "texto_na_celula",
        "elemento_solto",
        "celulas_mescladas",
        "atributos",
        "texto_compartilhado",
    ],
)
def test_one_huge_piece_of_a_streamed_part_is_refused_before_openpyxl(
    tmp_path: Path, kind: str
) -> None:
    # A aba e os textos são lidos aos poucos, mas cada pedaço deles (uma linha, um texto,
    # um elemento) o openpyxl monta inteiro: 60 a 150 KB viravam de 0,6 a mais de 3 GB.
    data = _one_huge_piece(kind)
    assert len(data) < 5 * 1024 * 1024
    outcome = _read_isolated(tmp_path, "a.xlsx", data, memory_mib=1024)
    assert "grande demais" in str(outcome.get("refused", "")), outcome
    assert int(outcome["maxrss_mib"]) < 200, outcome  # type: ignore[call-overload]


def test_largest_ordinary_workbook_is_still_read(tmp_path: Path) -> None:
    # 1000 linhas de 100 colunas, com estilo em todas as células, células mescladas,
    # validação, formatação condicional e outra aba: tudo dentro dos limites da leitura.
    import openpyxl
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append([f"coluna{c}" for c in range(100)])
    for r in range(1000):
        sheet.append([f"texto {r} {c} " * 3 if c % 2 else r * c + 0.5 for c in range(100)])
    bold = Font(bold=True)
    for row in sheet.iter_rows(min_row=1, max_row=1001, max_col=100):
        for cell in row:
            cell.font = bold
    for i in range(200):
        sheet.merge_cells(start_row=1002 + i, start_column=1, end_row=1002 + i, end_column=3)
    validation = DataValidation(type="list", formula1='"a,b,c"')
    sheet.add_data_validation(validation)
    validation.add("A2:A1001")
    sheet.conditional_formatting.add(
        "B2:B1001", CellIsRule(operator="lessThan", formula=["0"], font=bold)
    )
    workbook.create_sheet("outra").append(["x"] * 50)
    buffer = io.BytesIO()
    workbook.save(buffer)
    outcome = _read_isolated(tmp_path, "a.xlsx", buffer.getvalue(), memory_mib=1024)
    assert outcome.get("rows") == 1000, outcome


def test_many_small_whole_parts_together_are_refused() -> None:
    # Cada parte que o openpyxl monta inteira fica abaixo do limite, mas juntas passam
    # do dobro dele: relações de abas que ninguém lê ainda custam CPU e memória.
    from rapa.control.services.sheets import SheetLimits, read_sheet
    from rapa.core.contracts import InvalidInput

    limits = SheetLimits(max_part_bytes=64 * 1024)
    filler = "<!--" + "x" * (60 * 1024) + "-->"
    extra = {
        f"xl/worksheets/_rels/sheet{i}.xml.rels": f"<Relationships xmlns='{_REL}'>{filler}"
        "</Relationships>"
        for i in range(2, 5)
    }
    data = _xlsx(_sheet(_HEADER + _ROW2), extra)
    with pytest.raises(InvalidInput):
        read_sheet("a.xlsx", data, limits)
    # Com uma só dessas partes a planilha continua legível.
    one = _xlsx(_sheet(_HEADER + _ROW2), dict(list(extra.items())[:1]))
    assert read_sheet("a.xlsx", one, limits).rows
