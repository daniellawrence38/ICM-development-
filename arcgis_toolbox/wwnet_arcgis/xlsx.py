"""Minimal read-only .xlsx / .xlsm reader (Python standard library only).

Reads cell values from worksheets: shared and inline strings, numbers, booleans,
and the cached result of formulas. Values come back as stored: str (not
stripped), float (dates stay serial numbers), bool; error cells and empty cells
(including empty strings) are omitted or None. Limits: no styles, merged-cell
expansion, hidden-sheet flags, charts or writing; legacy .xls and password-
protected files are rejected with a ValueError. Formulas written by libraries
that store no cached value read as None.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

_CELL_RE = re.compile(r"^([A-Za-z]+)(\d*)$")


def col_index(letters: str) -> int:
    """'A' -> 1, 'AB' -> 28 (case-insensitive); ValueError on anything else."""
    s = letters.strip().upper() if isinstance(letters, str) else ""
    if not s or not (s.isascii() and s.isalpha()):
        raise ValueError(f"not a column letter string: {letters!r}")
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(n: int) -> str:
    """1 -> 'A', 28 -> 'AB'."""
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"column number must be >= 1, got {n!r}")
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # namespace-agnostic (transitional and strict)


def _bad(path) -> ValueError:
    return ValueError(f"{Path(str(path)).name} is not an .xlsx workbook; open it in Excel and Save As .xlsx")


def _open(path) -> zipfile.ZipFile:
    with open(path, "rb") as f:  # FileNotFoundError propagates
        magic = f.read(4)
    if magic == b"\xd0\xcf\x11\xe0":  # OLE: .xls or encrypted workbook
        raise _bad(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise _bad(path) from None
    if "xl/workbook.xml" not in zf.namelist():
        zf.close()
        raise _bad(path)
    return zf


def _sheet_parts(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(sheet name, zip member path)] in workbook order."""
    rels: dict[str, str] = {}
    try:
        root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        root = None
    for rel in root if root is not None else []:
        if rel.get("TargetMode") == "External":
            continue
        t = rel.get("Target") or ""
        t = t[1:] if t.startswith("/") else posixpath.join("xl", t)
        rels[rel.get("Id") or ""] = posixpath.normpath(t)
    out = []
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    for el in wb.iter():
        if _local(el.tag) != "sheet":
            continue
        rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
        if rid in rels:
            out.append((el.get("name") or "", rels[rid]))
    return out


def _text(el: ET.Element) -> str:
    """Text of an <si> / <is>: plain <t> or rich <r><t> runs (phonetic <rPh> ignored)."""
    parts = []
    for ch in el:
        n = _local(ch.tag)
        if n == "t":
            parts.append(ch.text or "")
        elif n == "r":
            parts.extend(t.text or "" for t in ch if _local(t.tag) == "t")
    return "".join(parts)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = next((n for n in zf.namelist() if n.lower() == "xl/sharedstrings.xml"), None)
    if name is None:
        return []
    out: list[str] = []
    with zf.open(name) as f:
        for _, el in ET.iterparse(f):
            if _local(el.tag) == "si":
                out.append(_text(el))
                el.clear()
    return out


def _value(c: ET.Element, shared: list[str]) -> object:
    t = c.get("t", "n")
    v = None
    is_el = None
    for ch in c:
        n = _local(ch.tag)
        if n == "v":
            v = ch.text
        elif n == "is":
            is_el = ch
    if t == "inlineStr":
        return _text(is_el) if is_el is not None else None
    if v is None:
        return None
    if t == "s":
        i = int(v)
        return shared[i] if 0 <= i < len(shared) else None
    if t == "b":
        return v.strip() == "1"
    if t == "e":
        return None
    if t in ("str", "d"):
        return v
    try:
        return float(v)
    except ValueError:
        return None


def _iter_rows(zf: zipfile.ZipFile, member: str, shared: list[str]) -> Iterator[tuple[int, dict[str, object]]]:
    row_no = 0
    next_col = 0
    cells: dict[str, object] = {}
    with zf.open(member) as f:
        for ev, el in ET.iterparse(f, events=("start", "end")):
            n = _local(el.tag)
            if ev == "start":
                if n == "row":
                    r = el.get("r")
                    row_no = int(r) if r and r.isdigit() else row_no + 1
                    next_col = 0
                    cells = {}
                continue
            if n == "c":
                m = _CELL_RE.match(el.get("r") or "")
                col = col_index(m.group(1)) if m else next_col + 1
                next_col = col
                val = _value(el, shared)
                if val is not None and val != "":
                    cells[col_letters(col)] = val
                el.clear()
            elif n == "row":
                if cells:
                    yield row_no, cells
                el.clear()


def _match(parts: list[tuple[str, str]], sheet: str) -> str:
    for name, member in parts:
        if name == sheet:
            return member
    key = sheet.strip().casefold()
    for name, member in parts:
        if name.strip().casefold() == key:
            return member
    raise KeyError(f"sheet {sheet!r} not found; available sheets: {[n for n, _ in parts]}")


def sheet_names(path) -> list[str]:
    """Sheet names in workbook order."""
    with _open(path) as zf:
        return [n for n, _ in _sheet_parts(zf)]


def read_sheet(path, sheet: str) -> list[tuple[int, dict[str, object]]]:
    """[(1-based row number, {column letters: value})] for rows with any non-empty cell.

    Sheet name: exact match first, then case-insensitive and whitespace-trimmed.
    """
    with _open(path) as zf:
        member = _match(_sheet_parts(zf), sheet)
        return list(_iter_rows(zf, member, _shared_strings(zf)))


def read_workbook(path, sheets=None) -> dict[str, list[tuple[int, dict[str, object]]]]:
    """{sheet name: rows} for all sheets, or only the named ones (opens the zip once)."""
    with _open(path) as zf:
        parts = _sheet_parts(zf)
        shared = _shared_strings(zf)
        if sheets is None:
            wanted = parts
        else:
            wanted = []
            for s in sheets:
                member = _match(parts, s)
                wanted.append((next(n for n, m in parts if m == member), member))
        return {name: list(_iter_rows(zf, member, shared)) for name, member in wanted}
