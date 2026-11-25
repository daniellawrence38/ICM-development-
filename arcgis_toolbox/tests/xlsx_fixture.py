"""Tiny stdlib writer of minimal valid .xlsx files for tests (no openpyxl needed).

Cell spec per value: str -> shared string; ("inline", s) -> inline string;
float/int -> number; bool -> t="b"; ("f", cached) -> formula with cached <v>
(cached str -> t="str", number -> numeric, None -> no <v>); ("e", "#N/A") -> error;
None -> skipped. Rows are {col letters: spec}; a dict key of int row numbers maps to rows.
"""
from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _t(s: str) -> str:
    return f'<t xml:space="preserve">{escape(s)}</t>'


def _cell(ref: str | None, spec, shared: list[str]) -> str:
    r = f' r="{ref}"' if ref else ""
    if isinstance(spec, tuple):
        kind, arg = spec
        if kind == "inline":
            return f'<c{r} t="inlineStr"><is>{_t(arg)}</is></c>'
        if kind == "rich":  # list of run texts
            runs = "".join(f"<r>{_t(x)}</r>" for x in arg)
            shared.append(f"<si>{runs}</si>")
            return f'<c{r} t="s"><v>{len(shared) - 1}</v></c>'
        if kind == "e":
            return f'<c{r} t="e"><v>{arg}</v></c>'
        if kind == "f":
            if arg is None:
                return f"<c{r}><f>A1+1</f></c>"
            if isinstance(arg, str):
                return f'<c{r} t="str"><f>A1&amp;"x"</f><v>{escape(arg)}</v></c>'
            return f"<c{r}><f>A1+1</f><v>{arg!r}</v></c>"
    if isinstance(spec, bool):
        return f'<c{r} t="b"><v>{int(spec)}</v></c>'
    if isinstance(spec, (int, float)):
        return f"<c{r}><v>{spec!r}</v></c>"
    shared.append(f"<si>{_t(spec)}</si>")
    return f'<c{r} t="s"><v>{len(shared) - 1}</v></c>'


def _sheet_xml(rows, shared, with_r=True, ns=MAIN) -> str:
    out = []
    for n, cells in sorted(rows.items()):
        rattr = f' r="{n}"' if with_r else ""
        cs = "".join(_cell(f"{c}{n}" if with_r else None, v, shared)
                     for c, v in cells.items() if v is not None)
        out.append(f"<row{rattr}>{cs}</row>")
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{ns}"><sheetData>{"".join(out)}</sheetData></worksheet>')


def write_xlsx(path, sheets: dict[str, dict[int, dict[str, object]]], *, with_r: bool = True,
               absolute_targets: bool = False, ns: str = MAIN, rel_ns: str = REL) -> None:
    """sheets: {sheet name: {row number: {col letters: spec}}}, written in dict order."""
    shared: list[str] = []
    sheet_xml = [_sheet_xml(rows, shared, with_r, ns) for rows in sheets.values()]
    sst = (f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{ns}" count="{len(shared)}" '
           f'uniqueCount="{len(shared)}">{"".join(shared)}</sst>')
    wb_sheets = "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                        for i, n in enumerate(sheets, 1))
    wb = (f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{ns}" xmlns:r="{rel_ns}">'
          f"<sheets>{wb_sheets}</sheets></workbook>")
    pre = "/xl/" if absolute_targets else ""
    rels = "".join(f'<Relationship Id="rId{i}" Type="{rel_ns}/worksheet" Target="{pre}worksheets/sheet{i}.xml"/>'
                   for i in range(1, len(sheets) + 1))
    rels += f'<Relationship Id="rId99" Type="{rel_ns}/sharedStrings" Target="{pre}sharedStrings.xml"/>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("_rels/.rels", f'<?xml version="1.0"?><Relationships xmlns="{PKG_REL}"><Relationship Id="rId1" '
                   f'Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="{PKG_REL}">{rels}</Relationships>')
        z.writestr("xl/sharedStrings.xml", sst)
        for i, x in enumerate(sheet_xml, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", x)
