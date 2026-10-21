import io
import zipfile
from pathlib import Path

import pytest

from scripts.fetch_data import check_drop_columns, drop_csv_columns, scrub_xlsx_metadata


def test_drops_the_name_column_and_keeps_encoding_and_line_endings():
    src = "예산년도,연구기관,연구책임자,총 연구비\r\n2023,가나연구원,홍*동,527000000\r\n".encode("cp949")
    out = drop_csv_columns(src, ["연구책임자"])
    text = out.decode("cp949")
    assert text == "예산년도,연구기관,총 연구비\r\n2023,가나연구원,527000000\r\n"


def test_keeps_quoting_for_fields_with_commas_and_a_utf8_bom():
    src = '﻿과제명,연구책임자\n"해양, 수산 기술",가*다\n'.encode()
    out = drop_csv_columns(src, ["연구책임자"])
    assert out.startswith(b"\xef\xbb\xbf")
    assert out.decode("utf-8-sig") == '과제명\n"해양, 수산 기술"\n'


def test_a_missing_column_is_an_error():
    with pytest.raises(ValueError):
        drop_csv_columns(b"a,b\n1,2\n", ["c"])


def test_drop_columns_on_a_non_csv_file_stops_before_anything_is_written():
    ds = {"key": "kimst", "drop_columns": ["연구책임자"]}
    assert check_drop_columns(ds, ["data.csv", "portal_description.md"]) == ["연구책임자"]
    with pytest.raises(ValueError, match="CSV 가 아닌 파일"):
        check_drop_columns(ds, ["data.xlsx", "portal_description.md"])
    assert check_drop_columns({"key": "keit"}, ["data.xlsx"]) == []


CORE = (
    '<cp:coreProperties xmlns:cp="c" xmlns:dc="d"><dc:creator>홍길동</dc:creator>'
    "<cp:lastModifiedBy>Hong</cp:lastModifiedBy><dcterms:created>2026-09-18</dcterms:created></cp:coreProperties>"
)
WORKBOOK = (
    '<workbook><mc:AlternateContent><mc:Choice Requires="x15">'
    '<x15ac:absPath url="C:\\Users\\someone\\Documents\\" xmlns:x15ac="x"/></mc:Choice></mc:AlternateContent>'
    "<sheets/></workbook>"
)


def _xlsx(parts: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return out.getvalue()


def test_xlsx_metadata_loses_names_and_paths_but_nothing_else():
    sheet = "<worksheet><sheetData><row><c><v>홍길동</v></c></row></sheetData></worksheet>"
    src = _xlsx({"docProps/core.xml": CORE, "xl/workbook.xml": WORKBOOK, "xl/worksheets/sheet1.xml": sheet})
    with zipfile.ZipFile(io.BytesIO(scrub_xlsx_metadata(src))) as z:
        core, workbook = z.read("docProps/core.xml").decode(), z.read("xl/workbook.xml").decode()
        assert "홍길동" not in core and "Hong" not in core and "2026-09-18" in core
        assert "absPath" not in workbook and "Users" not in workbook and "<sheets/>" in workbook
        assert z.read("xl/worksheets/sheet1.xml").decode() == sheet  # 데이터 시트는 그대로


def test_the_committed_spreadsheets_carry_no_author_or_path():
    raw = Path(__file__).resolve().parent.parent / "data" / "raw"
    for path in raw.glob("*/*.xlsx"):
        with zipfile.ZipFile(path) as z:
            core = z.read("docProps/core.xml").decode()
            assert "lastModifiedBy>" not in core and "creator>" not in core, path.name
            assert "absPath" not in z.read("xl/workbook.xml").decode(), path.name
