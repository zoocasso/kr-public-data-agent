import pytest

from scripts.fetch_data import drop_csv_columns


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
