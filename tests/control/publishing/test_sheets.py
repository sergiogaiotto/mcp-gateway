"""Planilhas dos lotes (sem banco): CSV e xlsx, fórmulas recusadas, limites, conversão e resultado."""

import csv
import io
import zipfile
from datetime import UTC, date, datetime
from typing import Any

import openpyxl
import pytest

from rapa.control.services.sheets import (
    SheetLimits,
    plan_rows,
    read_sheet,
    render_results,
)
from rapa.control.validation import InputValidator
from rapa.core.contracts import BatchFormat, BatchRow, InvalidInput, RunState
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from tests.control.db.support import notas, protocolo

from .support import PROTOCOL_ROWS, csv_bytes, protocol_sheet, xlsx_bytes

LIMITS = SheetLimits()
VALIDATOR = InputValidator()


def _typed_robot() -> Manifest:
    data = protocolo().model_dump(mode="json")
    data["inputs"] = [
        {"name": "nome", "title": "Nome completo", "type": "string"},
        {"name": "cpf", "title": "CPF", "type": "cpf"},
        {"name": "cnpj", "title": "CNPJ", "type": "cnpj", "required": False},
        {"name": "valor", "title": "Valor", "type": "money", "required": False},
        {"name": "quantidade", "title": "Quantidade", "type": "integer", "required": False},
        {"name": "peso", "title": "Peso", "type": "number", "required": False},
        {"name": "urgente", "title": "Urgente", "type": "boolean", "required": False},
        {"name": "vencimento", "title": "Data de vencimento", "type": "date", "required": False},
        {"name": "uf", "title": "UF", "type": "string"},
    ]
    return Manifest.model_validate(data)


def _refused(name: str, data: bytes, limits: SheetLimits = LIMITS) -> str:
    with pytest.raises(InvalidInput) as caught:
        read_sheet(name, data, limits)
    return " ".join([caught.value.message, *caught.value.errors])


# -- leitura --------------------------------------------------------------------------


@pytest.mark.parametrize("delimiter", [";", ",", "\t"])
def test_csv_with_any_common_separator(delimiter: str) -> None:
    sheet = read_sheet(
        "lote.csv",
        csv_bytes([("Nome completo", "CPF", "UF"), *PROTOCOL_ROWS], delimiter=delimiter),
        LIMITS,
    )
    assert sheet.format is BatchFormat.CSV
    assert sheet.headers == ["Nome completo", "CPF", "UF"]
    assert [line for line, _ in sheet.rows] == [2, 3, 4]
    assert sheet.rows[1][1] == list(PROTOCOL_ROWS[1])


def test_csv_in_utf8_with_bom_and_in_windows_1252() -> None:
    rows = [("Nome completo", "CPF", "UF"), ("José Conceição", "529.982.247-25", "SP")]
    with_bom = "﻿".encode() + csv_bytes(rows)
    assert read_sheet("a.csv", with_bom, LIMITS).rows[0][1][0] == "José Conceição"
    latin = csv_bytes(rows, encoding="cp1252")
    assert read_sheet("a.csv", latin, LIMITS).rows[0][1][0] == "José Conceição"


def test_empty_lines_are_skipped_and_line_numbers_kept() -> None:
    data = csv_bytes(
        [
            ("Nome completo", "CPF", "UF"),
            PROTOCOL_ROWS[0],
            ("", "", ""),
            PROTOCOL_ROWS[1],
            ("", "", ""),
        ]
    )
    sheet = read_sheet("a.csv", data, LIMITS)
    assert [line for line, _ in sheet.rows] == [2, 4]


def test_xlsx_values_are_read_and_formulas_refused() -> None:
    rows = [("Nome completo", "CPF", "UF"), *PROTOCOL_ROWS]
    sheet = read_sheet("lote.xlsx", xlsx_bytes(rows), LIMITS)
    assert sheet.format is BatchFormat.XLSX and len(sheet.rows) == 3
    message = _refused("lote.xlsx", xlsx_bytes(rows, formula_at=(3, 2)))
    assert "fórmulas" in message and "linha 3" in message


def test_xlsx_error_cells_and_macros_are_refused() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Nome completo", "CPF", "UF"])
    sheet.append(["Maria", "529.982.247-25", "SP"])
    sheet["C2"].value = "#N/A"
    sheet["C2"].data_type = "e"
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert "erro" in _refused("a.xlsx", buffer.getvalue())
    # Um xlsx com macros (o vbaProject.bin) não entra, mesmo com a extensão .xlsx.
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes([("a",), ("b",)]))) as original:
            for item in original.infolist():
                archive.writestr(item, original.read(item))
        archive.writestr("xl/vbaProject.bin", b"\x00" * 10)
    assert "macros" in _refused("a.xlsx", data.getvalue())


def test_zip_bomb_is_refused_before_opening() -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (2 * 1024 * 1024))
    limits = SheetLimits(max_unzipped_bytes=1024 * 1024)
    assert "grande demais" in _refused("a.xlsx", data.getvalue(), limits)


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("a.xls", b"\xd0\xcf\x11\xe0", "formato"),
        ("a.xlsm", xlsx_bytes([("a",)]), "formato"),
        ("a.ods", b"x", "formato"),
        ("a.csv", b"", "vazio"),
        ("a.csv", xlsx_bytes([("a",)]), "Excel"),
        ("a.xlsx", b"Nome;CPF\r\n", "corrompido"),
        ("a.csv", b"\r\n\r\n", "vazia"),
        ("a.csv", b"Nome;CPF\r\n", "nenhuma linha"),
        ("a.csv", b"Nome;;CPF\r\nMaria;x;1\r\n", "coluna B"),
        ("a.csv", b"Nome;nome\r\nMaria;x\r\n", "duas vezes"),
        ("a.csv", b"Nome\r\nMaria;intrusa\r\n", "sem título"),
        ("a.csv", b'Nome\r\n"sem fim\r\n', "mal formado"),
        ("a.csv", b"Nome\x00\r\n", "inválidos"),
    ],
)
def test_bad_files_are_refused(name: str, data: bytes, message: str) -> None:
    assert message in _refused(name, data)


def test_caps_on_bytes_rows_and_columns() -> None:
    rows = [("Nome completo", "CPF", "UF"), *PROTOCOL_ROWS]
    assert "limite" in _refused("a.csv", csv_bytes(rows), SheetLimits(max_bytes=10))
    assert "limite de 2 linhas" in _refused("a.csv", csv_bytes(rows), SheetLimits(max_rows=2))
    assert "limite de 2 linhas" in _refused("a.xlsx", xlsx_bytes(rows), SheetLimits(max_rows=2))
    assert "colunas" in _refused("a.csv", csv_bytes(rows), SheetLimits(max_columns=2))
    # No limite exato, passa.
    assert len(read_sheet("a.csv", csv_bytes(rows), SheetLimits(max_rows=3)).rows) == 3


# -- colunas e entradas ---------------------------------------------------------------


def test_columns_match_inputs_by_name_or_title_and_extra_columns_are_ignored() -> None:
    data = csv_bytes([("nome", "C.P.F.", "uf", "Observação"), PROTOCOL_ROWS[0]])
    plan = plan_rows(read_sheet("a.csv", data, LIMITS), protocolo(), VALIDATOR, {"C.P.F.": "cpf"})
    assert plan.ok, (plan.file_errors, plan.errors)
    assert [(c.header, c.input) for c in plan.columns] == [
        ("nome", "nome"),
        ("C.P.F.", "cpf"),
        ("uf", "uf"),
        ("Observação", None),
    ]
    assert plan.rows == [(2, {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"})]


@pytest.mark.parametrize(
    ("headers", "mapping", "message"),
    [
        (("Nome completo", "UF"), None, "falta a coluna da entrada obrigatória 'cpf'"),
        (("Nome completo", "CPF", "cpf_2", "UF"), {"cpf_2": "cpf"}, "mesma entrada"),
        (("Nome completo", "CPF", "UF"), {"CPF": "inexistente"}, "entrada que o robô não tem"),
        (("Nome completo", "CPF", "UF"), {"Outra": "cpf"}, "coluna que a planilha não tem"),
    ],
)
def test_mapping_problems_are_file_errors(
    headers: tuple[str, ...], mapping: dict[str, str] | None, message: str
) -> None:
    data = csv_bytes([headers, tuple("x" for _ in headers)])
    plan = plan_rows(read_sheet("a.csv", data, LIMITS), protocolo(), VALIDATOR, mapping)
    assert not plan.ok
    assert any(message in error for error in plan.file_errors), plan.file_errors


def test_list_inputs_do_not_fit_in_a_sheet() -> None:
    data = notas().model_dump(mode="json")
    data["inputs"] = [
        {
            "name": "pessoas",
            "title": "Pessoas",
            "type": "list",
            "item_fields": [{"name": "nome", "title": "Nome", "type": "string"}],
        }
    ]
    plan = plan_rows(
        read_sheet("a.csv", b"Pessoas\r\nx\r\n", LIMITS),
        Manifest.model_validate(data),
        VALIDATOR,
    )
    assert any("lista" in error for error in plan.file_errors)


def test_every_row_is_checked_and_errors_never_repeat_values() -> None:
    data = csv_bytes(
        [
            ("Nome completo", "CPF", "UF"),
            PROTOCOL_ROWS[0],
            ("Fulano", "segredo-123", "SP"),
            ("", "111.444.777-35", "RJ"),
            PROTOCOL_ROWS[2],
        ]
    )
    plan = plan_rows(read_sheet("a.csv", data, LIMITS), protocolo(), VALIDATOR)
    assert not plan.ok and not plan.file_errors
    assert [(e.line, e.errors) for e in plan.errors] == [
        (3, ["cpf: valor fora do formato esperado"]),
        (4, ["campo obrigatório ausente: nome"]),
    ]
    assert [line for line, _ in plan.rows] == [2, 5]
    assert "segredo-123" not in repr(plan.errors)


def test_cells_are_converted_to_the_input_types() -> None:
    rows: list[tuple[Any, ...]] = [
        (
            "Nome completo",
            "CPF",
            "CNPJ",
            "Valor",
            "Quantidade",
            "Peso",
            "Urgente",
            "Data de vencimento",
            "UF",
        ),
        ("Maria", 52998224725, 191, 1234.5, 3.0, "1.234,56", "Sim", datetime(2026, 12, 1), "SP"),
        ("João", 1144477735, None, "10,00", "7", 2, False, "25/12/2026", "RJ"),
        ("Ana", "935.411.347-80", None, None, None, "1.5", "não", "2026-01-31", "MG"),
    ]
    plan = plan_rows(read_sheet("a.xlsx", xlsx_bytes(rows), LIMITS), _typed_robot(), VALIDATOR)
    assert plan.ok, (plan.file_errors, plan.errors)
    first, second, third = (inputs for _, inputs in plan.rows)
    assert first == {
        "nome": "Maria",
        "cpf": "52998224725",
        "cnpj": "00000000000191",
        "valor": "1234,50",
        "quantidade": 3,
        "peso": 1234.56,
        "urgente": True,
        "vencimento": "2026-12-01",
        "uf": "SP",
    }
    # O CPF que o Excel guardou como número perde o zero da esquerda; ele volta.
    assert second["cpf"] == "01144477735"
    assert second["valor"] == "10,00" and second["quantidade"] == 7 and second["peso"] == 2
    assert second["urgente"] is False and second["vencimento"] == "2026-12-25"
    assert "cnpj" not in second  # célula vazia: entrada ausente
    assert (
        third["peso"] == 1.5 and third["urgente"] is False and third["vencimento"] == "2026-01-31"
    )


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("Quantidade", "três", "esperado número inteiro"),
        ("Quantidade", "1,5", "esperado número inteiro"),
        ("Peso", "um kilo", "esperado número"),
        ("Urgente", "talvez", "esperado sim ou não"),
        ("Data de vencimento", "31/02/2026", "data que não existe"),
        ("Data de vencimento", "amanhã", "esperada data"),
    ],
)
def test_conversion_errors_are_per_row(column: str, value: str, message: str) -> None:
    headers = ("Nome completo", "CPF", "UF", column)
    data = csv_bytes([headers, ("Maria", "529.982.247-25", "SP", value)])
    plan = plan_rows(read_sheet("a.csv", data, LIMITS), _typed_robot(), VALIDATOR)
    [error] = plan.errors
    assert error.line == 2 and any(message in e for e in error.errors), error.errors
    assert value not in repr(error.errors)


# -- resultado --------------------------------------------------------------------------


def _rows() -> list[BatchRow]:
    when = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    return [
        BatchRow(
            row_no=1,
            line=2,
            run_id=new_id(),
            state=RunState.SUCCEEDED,
            started_at=when,
            finished_at=when,
            outputs={"protocolo": '=HYPERLINK("http://mal")'},
        ),
        BatchRow(
            row_no=2,
            line=4,
            run_id=new_id(),
            state=RunState.FAILED,
            error_code="elemento_nao_encontrado",
            error_message="-o botão Emitir sumiu",
        ),
        BatchRow(row_no=3, line=5, run_id=new_id(), state=RunState.QUEUED),
    ]


def test_csv_result_has_declared_outputs_and_no_formulas() -> None:
    content = render_results(_rows(), protocolo().outputs, BatchFormat.CSV)
    assert content.startswith("﻿".encode())
    table = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
    assert table[0] == [
        "linha",
        "execucao",
        "situacao",
        "inicio",
        "fim",
        "protocolo",
        "erro",
        "mensagem",
    ]
    assert [row[0] for row in table[1:]] == ["2", "4", "5"]
    assert [row[2] for row in table[1:]] == ["Concluída", "Falhou", "Na fila"]
    assert table[1][5] == '\'=HYPERLINK("http://mal")'
    assert table[2][7] == "'-o botão Emitir sumiu"


def test_xlsx_result_stores_every_value_as_text() -> None:
    content = render_results(_rows(), protocolo().outputs, BatchFormat.XLSX)
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=False)
    sheet = workbook.active
    assert [cell.value for cell in sheet[1]] == [
        "linha",
        "execucao",
        "situacao",
        "inicio",
        "fim",
        "protocolo",
        "erro",
        "mensagem",
    ]
    formula = sheet.cell(row=2, column=6)
    assert formula.value == '=HYPERLINK("http://mal")' and formula.data_type == "s"
    assert all(cell.data_type != "f" for row in sheet.iter_rows() for cell in row)


def test_list_outputs_become_json_text() -> None:
    row = BatchRow(
        row_no=1,
        line=2,
        run_id=new_id(),
        state=RunState.SUCCEEDED,
        outputs={"notas": [{"numero": "1", "valor": "10,00"}]},
    )
    content = render_results([row], notas().outputs, BatchFormat.CSV).decode("utf-8-sig")
    assert '[{""numero"": ""1"", ""valor"": ""10,00""}]' in content


def test_protocol_sheet_helper_is_valid() -> None:
    plan = plan_rows(read_sheet("a.csv", protocol_sheet(), LIMITS), protocolo(), VALIDATOR)
    assert plan.ok and len(plan.rows) == 3
    assert date.today().year >= 2026
