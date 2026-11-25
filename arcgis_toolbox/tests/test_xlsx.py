"""Tests for the stdlib-only xlsx reader (workbooks built by xlsx_fixture)."""
from __future__ import annotations

import zipfile

import pytest

from wwnet_arcgis import xlsx
from xlsx_fixture import write_xlsx

PUMP = "Motor and Pump Attributes"


def test_col_index_and_letters():
    assert xlsx.col_index("A") == 1 and xlsx.col_index("AB") == 28 and xlsx.col_index("ab") == 28
    assert xlsx.col_index("AF") == 32 and xlsx.col_index("XFD") == 16384
    for bad in ("", "A1", "1", "A B", "é", None):
        with pytest.raises(ValueError):
            xlsx.col_index(bad)
    assert xlsx.col_letters(1) == "A" and xlsx.col_letters(26) == "Z" and xlsx.col_letters(27) == "AA"
    assert xlsx.col_letters(28) == "AB"
    for n in range(1, 1500):
        assert xlsx.col_index(xlsx.col_letters(n)) == n
    with pytest.raises(ValueError):
        xlsx.col_letters(0)


def test_values_and_types(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": "  padded ", "B": 3, "C": 2.5, "D": True, "E": ("e", "#N/A"), "F": ("inline", "inl")}}})
    (row, cells), = xlsx.read_sheet(p, "S")
    assert row == 1
    assert cells == {"A": "  padded ", "B": 3.0, "C": 2.5, "D": True, "F": "inl"}
    assert isinstance(cells["B"], float) and cells["D"] is True


def test_rich_text_and_whitespace(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": ("rich", ["Pump ", "Station ", " 7"]), "B": ("inline", " x ")}}})
    assert xlsx.read_sheet(p, "S")[0][1] == {"A": "Pump Station  7", "B": " x "}


def test_formulas_use_cached_value(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": ("f", 4.5), "B": ("f", "text"), "C": ("f", None), "D": "k"}}})
    assert xlsx.read_sheet(p, "S") == [(1, {"A": 4.5, "B": "text", "D": "k"})]


def test_sparse_rows_and_columns(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {2: {"C": "S0037310", "AB": 12.0}, 5: {"D": None}, 9: {"AF": 1.0}}})
    assert xlsx.read_sheet(p, "S") == [(2, {"C": "S0037310", "AB": 12.0}), (9, {"AF": 1.0})]


def test_missing_r_attributes(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": "x", "B": "y"}, 2: {"A": 1, "B": 2}}}, with_r=False)
    assert xlsx.read_sheet(p, "S") == [(1, {"A": "x", "B": "y"}), (2, {"A": 1.0, "B": 2.0})]


def test_sheet_names_order_and_matching(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"Level Settings": {1: {"A": "l"}}, "Civil": {1: {"A": "c"}}, PUMP: {1: {"A": "m"}}})
    assert xlsx.sheet_names(p) == ["Level Settings", "Civil", PUMP]
    assert xlsx.read_sheet(p, PUMP)[0][1] == {"A": "m"}
    assert xlsx.read_sheet(p, "  motor and pump attributes ")[0][1] == {"A": "m"}
    with pytest.raises(KeyError) as e:
        xlsx.read_sheet(p, "Nope")
    assert "Civil" in str(e.value) and PUMP in str(e.value)


def test_exact_match_preferred_over_loose(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"civil": {1: {"A": "lower"}}, "Civil": {1: {"A": "exact"}}})
    assert xlsx.read_sheet(p, "Civil")[0][1] == {"A": "exact"}


def test_absolute_rel_targets(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": "shared", "B": 1}}}, absolute_targets=True)
    assert xlsx.read_sheet(p, "S") == [(1, {"A": "shared", "B": 1.0})]


def test_strict_namespaces(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {1: {"A": "s"}}}, ns="http://purl.oclc.org/ooxml/spreadsheetml/main",
               rel_ns="http://purl.oclc.org/ooxml/officeDocument/relationships")
    assert xlsx.read_sheet(p, "S") == [(1, {"A": "s"})]


def test_xlsm_extension(tmp_path):
    p = tmp_path / "a.xlsm"
    write_xlsx(p, {"S": {1: {"A": 1}}})
    assert xlsx.sheet_names(p) == ["S"]


def test_read_workbook(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"One": {1: {"A": "1"}}, "Two": {3: {"B": 2}}, "Three": {1: {"C": "x"}}})
    allw = xlsx.read_workbook(p)
    assert list(allw) == ["One", "Two", "Three"] and allw["Two"] == [(3, {"B": 2.0})]
    sub = xlsx.read_workbook(p, ["three", "One"])
    assert list(sub) == ["Three", "One"]
    with pytest.raises(KeyError):
        xlsx.read_workbook(p, ["Missing"])


def test_large_sheet(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(p, {"S": {i: {"A": float(i), "AB": "v"} for i in range(1, 5001)}})
    rows = xlsx.read_sheet(p, "S")
    assert len(rows) == 5000 and rows[-1] == (5000, {"A": 5000.0, "AB": "v"})


def test_rejects_xls_and_non_zip(tmp_path):
    xls = tmp_path / "old.xls"
    xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    junk = tmp_path / "junk.xlsx"
    junk.write_text("hello")
    notwb = tmp_path / "zip.xlsx"
    with zipfile.ZipFile(notwb, "w") as z:
        z.writestr("x.txt", "x")
    for f in (xls, junk, notwb):
        for fn in (xlsx.sheet_names, lambda q: xlsx.read_sheet(q, "S"), xlsx.read_workbook):
            with pytest.raises(ValueError, match=r"is not an \.xlsx workbook; open it in Excel and Save As \.xlsx"):
                fn(f)
        assert f.name in _msg(f)


def _msg(f):
    try:
        xlsx.sheet_names(f)
    except ValueError as e:
        return str(e)


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        xlsx.sheet_names(tmp_path / "nope.xlsx")
