"""Import Water Corporation (WA) gravity-sewer GIS data into a WWNet model (pure Python).

No arcpy / qgis imports: the front ends read their layers into plain dicts and call
:func:`build_model`. Input rows are flat dicts of ``{source field name: value}`` plus the geometry under
the key :data:`GEOM` (points: ``(x, y)``; lines: ``[(x, y), ...]``) in EPSG:7850 (m).

Output: ``(model_dict, ImportReport)``. ``model_dict`` is the JSON form accepted by
``wwnet_native.write_model`` (``json.dumps(model_dict)``). Rules, defaults and code maps live in
``templates/import/watercorp_sewer.toml`` (see :func:`load_template`).
"""
from __future__ import annotations

import datetime as _dt
import itertools
import json
import math
import re
from dataclasses import dataclass, field, fields
from typing import Iterable

from . import paths, tomlmini

ADWF_PER_CONNECTION_LPD = 630.0   # default allowance (wwn_meta adwf_per_connection_lpd) used for the report line
GEOM = "_geom"                 # key holding the geometry coordinates in each input row
CONTEXT = "_context"           # chamber rows flagged True were read only as context for the sewers (not selected)
TEMPLATE_NAME = "watercorp_sewer.toml"

# role -> (config table, key in the template)
_ROLE_KEYS = {
    "ac_id": ("access_chambers", "id"), "ac_surface_level": ("access_chambers", "surface_level"),
    "chamber_type": ("access_chambers", "chamber_type"),
    "cp_id": ("connection_points", "id"),
    "pipe_id": ("sewers", "id"), "us_node": ("sewers", "us_node"), "ds_node": ("sewers", "ds_node"),
    "us_invert": ("sewers", "us_invert"), "ds_invert": ("sewers", "ds_invert"),
    "flow_grade": ("sewers", "flow_grade"), "length": ("sewers", "length"),
    "diameter": ("sewers", "diameter"), "status": ("sewers", "status"),
    "sewer_type": ("sewers", "sewer_type"), "purpose": ("sewers", "purpose"),
    "material": ("sewers", "material"), "install_date": ("sewers", "install_date"),
    "planset": ("sewers", "planset"), "shape_length": ("sewers", "shape_length"),
}


@dataclass
class FieldMap:
    """Role -> source field name ('' = not mapped). Defaults are Water Corporation names."""
    ac_id: str = "PACID"
    ac_surface_level: str = "TOPLEV"
    chamber_type: str = "CHAMBER_TY"
    cp_id: str = "ID"
    pipe_id: str = "ID"
    us_node: str = "US_MANHOLE_ID"
    ds_node: str = "DS_MANHOLE_ID"
    us_invert: str = "UPSTREAM"
    ds_invert: str = "DOWNSTREAM"
    flow_grade: str = "FLOW_GRADE"
    length: str = "LENGTH"
    diameter: str = ""
    status: str = "STATUS"
    sewer_type: str = "MAINTYPE"
    purpose: str = "PURPOSE"
    material: str = "MATERIAL"
    install_date: str = "DATEINST"
    planset: str = "PLANSET"
    shape_length: str = "Shape_Length"     # optional diagnostic only (grade ratio); not a required role

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "FieldMap":
        cfg = cfg or load_template()
        out = cls()
        for role, (table, key) in _ROLE_KEYS.items():
            setattr(out, role, str(cfg.get("fields", {}).get(table, {}).get(key, getattr(out, role)) or ""))
        return out

    def with_overrides(self, **kw) -> "FieldMap":
        """Copy with some roles replaced; None / missing values keep the current mapping."""
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        for k, v in kw.items():
            if k not in d:
                raise KeyError(f"unknown field role {k!r}")
            if v is not None:
                d[k] = str(v)
        return FieldMap(**d)


GRADE_MISMATCH_MIN_FALL_M = 0.05            # a levels / FLOW_GRADE grade difference is listed only above this fall difference
NEAR_MISS_M = 5.0                           # lone ends this close to another sewer's mid-line are reported


@dataclass
class ImportOptions:
    snap_tolerance_m: float = 0.5
    grade_source: str = "levels"            # "levels" | "field"
    grade_tolerance_pct: float = 10.0
    default_dn: int = 150
    tee_tolerance_m: float = 0.5            # a lone sewer end this close to another sewer's mid-line is a T-junction
    load_connection_points: bool = True     # assign every connection point read to the nearest sewer's upstream node
    cp_load_search_m: float = 10.0          # max point-to-sewer distance for that assignment
    import_is_sewers: bool = True           # False: sewers whose type maps to "is" (MAINTYPE E / IS) are not imported

    @classmethod
    def from_config(cls, cfg: dict | None = None, **overrides) -> "ImportOptions":
        o = (cfg or load_template()).get("options", {})
        out = cls(float(o.get("snap_tolerance_m", 0.5)), str(o.get("grade_source", "levels")),
                  float(o.get("grade_tolerance_pct", 10.0)), int(o.get("default_dn", 150)),
                  float(o.get("tee_tolerance_m", 0.5)), bool(o.get("load_connection_points", True)),
                  float(o.get("cp_load_search_m", 10.0)), bool(o.get("import_is_sewers", True)))
        for k, v in overrides.items():
            if v is not None:
                setattr(out, k, v)
        return out


@dataclass
class ImportWarning:
    code: str              # UPPER_SNAKE, as the model check / design / simulation codes
    object_type: str       # node | conduit | connection_point | import
    object_id: str
    message: str
    x: float | None = None  # location for issues that are not at a model object (optional)
    y: float | None = None
    severity: str = "warning"   # "warning" | "info" (info rows live in ImportReport.notes, not .warnings)

    def __str__(self) -> str:
        who = f"{self.object_type} {self.object_id}: " if self.object_id else ""
        return f"[{self.code}] {who}{self.message}"


@dataclass
class ImportReport:
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)          # [ImportWarning] (severity warning)
    notes: list = field(default_factory=list)             # [ImportWarning] with severity "info" (TEE_JOINED, END_SNAPPED ...)
    infos: list = field(default_factory=list)             # [str]
    unknown_codes: dict = field(default_factory=dict)     # {"STATUS": {"X": 3}}
    assumed_codes: dict = field(default_factory=dict)     # {"MAINTYPE": {"E": 26}}: mapped by assumption
    assumed_notes: dict = field(default_factory=dict)     # {"MAINTYPE": {"E": "meaning unconfirmed ..."}}
    diagnostics: list = field(default_factory=list)       # [str] summary lines (direction, dangling ends, grade ratios)
    status_skipped: dict = field(default_factory=dict)    # {"N": 12, "D": 3}: sewers skipped by STATUS code

    def warn(self, code: str, object_type: str, object_id: str, message: str, xy=None, severity: str = "warning"):
        x, y = (xy if xy else (None, None))
        w = ImportWarning(code, object_type, str(object_id), message, x, y, severity)
        (self.notes if severity == "info" else self.warnings).append(w)

    def issues(self) -> list:
        """The warnings as issue dicts for ``wwnet_native.store_issues(gpkg, "import", json)``: they are
        drawn at their node / conduit (or at x, y) in the model's Issues layer."""
        return [{"severity": w.severity, "code": w.code, "object_type": w.object_type, "object_id": w.object_id,
                 "clause": None, "message": w.message, "x": w.x, "y": w.y}
                for w in itertools.chain(self.warnings, self.notes)]

    def count(self, key: str, n: int = 1):
        self.counts[key] = self.counts.get(key, 0) + n

    def warning_counts(self) -> dict:
        out: dict = {}
        for w in self.warnings:
            out[w.code] = out.get(w.code, 0) + 1
        return out

    def note_counts(self) -> dict:
        out: dict = {}
        for w in self.notes:
            out[w.code] = out.get(w.code, 0) + 1
        return out

    def example_warnings(self, max_total: int = 40, per_code: int = 5) -> list:
        """Up to `max_total` warnings spread across the codes (at most `per_code` of each), 'import' first."""
        by_code: dict = {}
        for w in self.warnings:
            by_code.setdefault(w.code, []).append(w)
        order = sorted(by_code, key=lambda k: (by_code[k][0].object_type != "import", k))
        out: list = []
        for rnd in range(per_code):
            for k in order:
                if rnd < len(by_code[k]) and len(out) < max_total:
                    out.append(by_code[k][rnd])
        return out

    def summary_lines(self, max_warnings: int = 40) -> list:
        """Counts, per-code warning summary, then example warnings (0 = summary only)."""
        c = self.counts
        ctx = f" + {c['context_chambers']} context chamber(s)" if c.get("context_chambers") else ""
        lines = [f"Imported {c.get('nodes', 0)} node(s) ({c.get('access_chambers', 0)} chamber(s){ctx}, "
                 f"{c.get('connection_points_used', 0)} connection point(s), "
                 f"{c.get('junction_nodes', 0)} segment junction(s), {c.get('tee_junctions', 0)} tee junction(s), "
                 f"{c.get('dangling_ends', 0)} dangling "
                 f"end(s)) and {c.get('conduits', 0)} conduit(s).",
                 f"Skipped: {c.get('connection_points_skipped', 0)} unreferenced connection point(s), "
                 f"{c.get('conduits_skipped', 0)} sewer(s), {c.get('access_chambers_skipped', 0)} chamber(s)."]
        if c.get("inspection_shafts_skipped"):
            lines.append(f"Skipped {c['inspection_shafts_skipped']} chamber(s) of a type mapped to skip "
                         "(inspection shafts, deleted manholes, pit access): not imported.")
        if c.get("is_sewers_skipped"):
            lines.append(f"Skipped {c['is_sewers_skipped']} IS (inspection shaft) sewer(s): not imported "
                         "(option 'Import IS sewers' off).")
        if c.get("status_skipped"):
            lines.append(f"Skipped {c['status_skipped']} sewer(s) by status (not imported): "
                         + ", ".join(f"{k} x{v}" for k, v in sorted(self.status_skipped.items())) + ".")
        if c.get("self_loops_dropped"):
            lines.append(f"Dropped {c['self_loops_dropped']} self-loop sewer(s) (both ends at the same node; "
                         "not imported).")
        if c.get("context_chambers_unused"):
            lines.append(f"Ignored {c['context_chambers_unused']} context chamber(s) not touching any sewer end.")
        if c.get("reversed"):
            n, tot = c["reversed"], c.get("conduits_read") or 0
            share = f" of {tot} ({100 * n / tot:.0f}%)" if tot else ""
            conv = (" - the source is drawn downstream -> upstream; this is a convention, not an error"
                    if tot and n / tot >= 0.8 else "; each reversed sewer is noted in its notes field")
            lines.append(f"Reversed digitising direction of {n}{share} sewer(s){conv}.")
        lines.extend(self.diagnostics)
        if c.get("surface_interpolated"):
            lines.append(f"Interpolated surface level at {c['surface_interpolated']} junction/tee node(s) between "
                         "chambers (cover is checked there; ground level is a linear estimate).")
        for fld, codes in self.assumed_codes.items():
            what = ", ".join(f"{k!r} x{v}" + (f" ({self.assumed_notes[fld][k]})" if k in self.assumed_notes.get(fld, {}) else "")
                             for k, v in sorted(codes.items()))
            lines.append(f"Assumed {fld} codes: {what}")
        for fld, codes in self.unknown_codes.items():
            lines.append(f"Unknown {fld} codes: " + ", ".join(f"{k!r} x{v}" for k, v in sorted(codes.items())))
        wc = self.warning_counts()
        if wc:
            lines.append(f"{len(self.warnings)} warning(s): " + ", ".join(f"{k} x{v}" for k, v in sorted(wc.items())))
        nc = self.note_counts()
        if nc:
            lines.append(f"{len(self.notes)} note(s) (info, in the Issues layer): "
                         + ", ".join(f"{k} x{v}" for k, v in sorted(nc.items())))
        if max_warnings > 0:
            ex = self.example_warnings(max_warnings)
            lines.extend(str(w) for w in ex)
            if len(self.warnings) > len(ex):
                lines.append(f"... {len(self.warnings) - len(ex)} more warning(s) in ImportReport.warnings")
            lines.extend(self.infos[:max_warnings])
        return lines


# ------------------------------------------------------------------ template

def template_path():
    return paths.find_resource("templates/import", TEMPLATE_NAME)


def load_template(path=None) -> dict:
    p = path or template_path()
    return tomlmini.loads(p.read_text(encoding="utf-8") if hasattr(p, "read_text") else open(p, encoding="utf-8").read())


# ------------------------------------------------------------------ value helpers

def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and math.isnan(v))


def to_float(v):
    if blank(v):
        return None
    try:
        f = float(str(v).strip().replace(",", "")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def to_id(v) -> str:
    """Identifier text: strips blanks and turns 22030208.0 into '22030208'."""
    if blank(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


LEVEL_PLACEHOLDER = -99.0      # source levels <= this (-99.9, -9996, -9998 ...) are placeholders = missing


def to_level(v):
    """A level (m AHD) or None: blanks and the GIS placeholders (-99.9, -9996, -9998; any value <= -99) are missing."""
    f = to_float(v)
    return None if f is None or f <= LEVEL_PLACEHOLDER else f


def to_ref_id(v) -> str:
    """Identifier as :func:`to_id`, but blank and all-zero values ('0', '0.0', '0000') mean "no id"."""
    s = to_id(v)
    return "" if s and re.fullmatch(r"0+(\.0*)?", s) else s


def code_of(v) -> str:
    """'A- actual' -> 'A' (text before the first '-', stripped, upper-cased)."""
    if blank(v):
        return ""
    return str(v).split("-", 1)[0].strip().upper()


def year_of(v):
    if blank(v):
        return None
    if isinstance(v, (_dt.datetime, _dt.date)):
        y = v.year
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        y = int(v)
    else:
        s = str(v).strip()
        m = re.search(r"(?<!\d)(18|19|20)\d\d(?!\d)", s)
        if m:
            y = int(m.group(0))
        elif re.fullmatch(r"(18|19|20)\d{6}", s):
            y = int(s[:4])
        else:
            return None
    return y if 1850 <= y <= 2100 else None


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _length(coords) -> float:
    return sum(_dist(a, b) for a, b in zip(coords, coords[1:]))


def _fmt(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}".rstrip("0").rstrip(".")


class _Grid:
    """Nearest-neighbour lookup within a tolerance (uniform grid, cell = tolerance)."""

    def __init__(self, cell: float):
        self.cell = max(cell, 1e-6)
        self.cells: dict = {}

    def _key(self, p):
        return (math.floor(p[0] / self.cell), math.floor(p[1] / self.cell))

    def add(self, ref, p):
        self.cells.setdefault(self._key(p), []).append((ref, p))

    def nearest(self, p, tol: float):
        kx, ky = self._key(p)
        best, bd = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ref, q in self.cells.get((kx + dx, ky + dy), ()):
                    d = _dist(p, q)
                    if d <= tol and (bd is None or d < bd):
                        best, bd = ref, d
        return best, bd


class _SegGrid:
    """Polyline pieces indexed by bounding box (expanded by a tolerance) in a uniform grid."""

    def __init__(self, cell: float, pad: float):
        self.cell = max(cell, 1e-6)
        self.pad = pad
        self.cells: dict = {}

    def add(self, ref, a, b):
        c, p = self.cell, self.pad
        x0, x1 = math.floor((min(a[0], b[0]) - p) / c), math.floor((max(a[0], b[0]) + p) / c)
        y0, y1 = math.floor((min(a[1], b[1]) - p) / c), math.floor((max(a[1], b[1]) + p) / c)
        for kx in range(x0, x1 + 1):
            for ky in range(y0, y1 + 1):
                self.cells.setdefault((kx, ky), []).append((ref, a, b))

    def candidates(self, p):
        return self.cells.get((math.floor(p[0] / self.cell), math.floor(p[1] / self.cell)), ())


def _project(p, a, b):
    """Nearest point of segment a-b to p: (distance, t in [0, 1], point)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    q = (a[0] + t * dx, a[1] + t * dy)
    return _dist(p, q), t, q


def _cut_line(coords, cuts):
    """Split a polyline at the given (chainage, point) cuts (ascending). Returns the pieces (point lists)."""
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _dist(a, b))
    pieces, cur, k = [], [coords[0]], 0
    for ch, pt in cuts:
        while k + 1 < len(coords) - 1 and cum[k + 1] < ch:
            k += 1
            cur.append(coords[k])
        # vertices strictly before the cut chainage are in `cur`; add the cut point and start the next piece
        cur.append(pt)
        pieces.append(cur)
        cur = [pt]
    cur.extend(coords[k + 1:])
    pieces.append(cur)
    return pieces


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _point(row):
    g = row.get(GEOM)
    try:
        return (float(g[0]), float(g[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _line(row):
    g = row.get(GEOM)
    try:
        pts = [(float(p[0]), float(p[1])) for p in g]
    except (TypeError, ValueError, IndexError):
        return None
    return pts if len(pts) >= 2 else None


# ------------------------------------------------------------------ main entry

def build_model(access_chambers: Iterable[dict], connection_points: Iterable[dict] | None,
                sewers: Iterable[dict], fmap: FieldMap | None = None,
                options: ImportOptions | None = None, config: dict | None = None):
    """Convert source rows to ``(model_dict, ImportReport)``. See the module docstring and the template."""
    cfg = config or load_template()
    fm = fmap or FieldMap.from_config(cfg)
    opt = options or ImportOptions.from_config(cfg)
    if opt.grade_source not in ("levels", "field"):
        raise ValueError(f"grade_source must be 'levels' or 'field', not {opt.grade_source!r}")
    std = sorted(int(x) for x in cfg.get("standard_dn", {}).get("list", [150]))
    codes = cfg.get("codes", {})
    assumed = cfg.get("assumed", {})
    rep = ImportReport()
    tol = float(opt.snap_tolerance_m)

    def get(row, role):
        name = getattr(fm, role)
        return row.get(name) if name else None

    def level(v):
        """Level or None; placeholders (<= -99) count as missing and are tallied for the summary."""
        f = to_float(v)
        if f is not None and f <= LEVEL_PLACEHOLDER:
            rep.count("placeholder_levels")
            return None
        return f

    def code_lookup(role, fname, row, cmap, default, obj_type, oid, what):
        """Coded value -> enum text. Unknown/blank -> default + counted warning."""
        raw = get(row, role)
        c = code_of(raw)
        whole = str(raw).strip().upper() if not blank(raw) else ""
        for key in (c, whole):
            if key in cmap:
                note = assumed.get(role, {}).get(key)
                if note is not None:
                    bucket = rep.assumed_codes.setdefault(fname, {})
                    bucket[key] = bucket.get(key, 0) + 1
                    rep.assumed_notes.setdefault(fname, {})[key] = note
                return cmap[key], key
        label = c if c else "(blank)"
        bucket = rep.unknown_codes.setdefault(fname, {})
        bucket[label] = bucket.get(label, 0) + 1
        rep.warn("UNKNOWN_CODE", obj_type, oid, f"{fname} code {label!r} (value {raw!r}) is not mapped; "
                 f"{what} defaulted to {default!r}")
        return default, c

    # ---- nodes (access chambers) ------------------------------------------------------------
    nodes: dict = {}                 # node_id -> dict (model form)
    node_pos: dict = {}              # node_id -> (x, y)
    node_ref: dict = {}              # node_id -> source id (chamber id / connection point id)
    ac_ids: set = set()
    ctx_ids: set = set()             # chambers read only as context (referenced by id / near the sewers)
    cp_pool: dict = {}               # cp id -> (x, y)
    grid = _Grid(tol)
    used_ids: set = set()
    ctype_map = codes.get("chamber_type", {})

    for row in access_chambers:
        node_type = "ac"
        if fm.chamber_type and fm.chamber_type in row:
            node_type, _c = code_lookup("chamber_type", fm.chamber_type, row, ctype_map, "ac", "node",
                                        to_id(get(row, "ac_id")), "chamber type")
            if node_type == "skip":
                rep.count("inspection_shafts_skipped")
                continue
        nid = to_ref_id(get(row, "ac_id"))
        p = _point(row)
        if not nid:
            rep.count("access_chambers_skipped")
            rep.warn("AC_NO_ID", "node", "", f"access chamber without {fm.ac_id} skipped")
            continue
        if nid in nodes:
            rep.count("access_chambers_skipped")
            rep.warn("AC_DUPLICATE_ID", "node", nid, "duplicate access chamber id skipped")
            continue
        if p is None:
            rep.count("access_chambers_skipped")
            rep.warn("AC_NO_GEOMETRY", "node", nid, "access chamber without geometry skipped")
            continue
        if row.get(CONTEXT):
            ctx_ids.add(nid)
        nodes[nid] = _clean({"node_id": nid, "geom": list(p), "node_type": node_type,
                             "surface_level": level(get(row, "ac_surface_level")),
                             "status": "existing", "data_source": "gis", "asset_id": nid})
        node_pos[nid] = p
        node_ref[nid] = nid
        ac_ids.add(nid)
        used_ids.add(nid)
        grid.add(nid, p)

    for row in connection_points or ():
        cid = to_id(get(row, "cp_id"))
        p = _point(row)
        if not cid or p is None or cid in cp_pool:
            rep.count("connection_points_skipped")
            if cid and cid in cp_pool:
                rep.warn("CP_DUPLICATE_ID", "connection_point", cid, "duplicate connection point id skipped")
            continue
        cp_pool[cid] = (p[0], p[1])
    cp_used: set = set()
    cp_has_io: set = set()
    cp_grid = _Grid(tol)
    for cid, p in cp_pool.items():
        cp_grid.add(cid, p)
    cp_map: dict = {}
    cp_by_node: dict = {}

    def use_cp(cid):
        """Materialise a connection point as a node (once); returns its node id."""
        if cid not in cp_used:
            cp_used.add(cid)
            nid = cid if cid not in used_ids else f"CP{cid}"
            used_ids.add(nid)
            cp_map[cid] = nid
            cp_by_node[nid] = cid
            node_ref[nid] = cid
            p = cp_pool[cid]
            nodes[nid] = _clean({"node_id": nid, "geom": list(p), "node_type": "junction",
                                 "status": "existing", "data_source": "gis", "asset_id": cid,
                                 "notes": "connection point"})
            node_pos[nid] = p
        return cp_map[cid]

    def new_id(base):
        nid, k = base, 2
        while nid in used_ids:
            nid = f"{base}_{k}"
            k += 1
        used_ids.add(nid)
        return nid

    # ---- sewer segments: parse ----------------------------------------------------------------
    segs: list = []
    pipe_ids: set = set()
    type_codes = codes.get("sewer_type", {})
    for row in sewers:
        pid = to_id(get(row, "pipe_id"))
        line = _line(row)
        status, scode = code_lookup("status", fm.status, row, codes.get("status", {}), "existing",
                                    "conduit", pid, "status")
        if status == "skip":             # N- not in use, D- dead: not imported, counted only
            rep.count("status_skipped")
            rep.status_skipped[scode] = rep.status_skipped.get(scode, 0) + 1
            continue
        if not opt.import_is_sewers:
            raw = get(row, "sewer_type")
            if not blank(raw) and (type_codes.get(code_of(raw)) or type_codes.get(str(raw).strip().upper())) == "is":
                rep.count("is_sewers_skipped")
                continue
        if not pid:
            rep.count("conduits_skipped")
            rep.warn("PIPE_NO_ID", "conduit", "", f"sewer without {fm.pipe_id} skipped")
            continue
        if pid in pipe_ids:
            rep.count("conduits_skipped")
            rep.warn("PIPE_DUPLICATE_ID", "conduit", pid, "duplicate sewer id skipped")
            continue
        if line is None:
            rep.count("conduits_skipped")
            rep.warn("PIPE_NO_GEOMETRY", "conduit", pid, "sewer without usable geometry skipped")
            continue
        pipe_ids.add(pid)
        rep.count("conduits_read")
        segs.append({"pid": pid, "row": row, "line": line, "us_id": to_ref_id(get(row, "us_node")),
                     "ds_id": to_ref_id(get(row, "ds_node")), "ends": [None, None], "status": status})

    # ---- connectivity by geometry ---------------------------------------------------------------
    # 1. segment end within tolerance of a chamber, else of a connection point
    for s in segs:
        for e, pt in ((0, s["line"][0]), (1, s["line"][-1])):
            nid, d1 = grid.nearest(pt, tol)
            cid, d2 = cp_grid.nearest(pt, tol)
            if nid is not None and (cid is None or d1 <= d2):
                s["ends"][e] = nid
            elif cid is not None:
                s["ends"][e] = use_cp(cid)
    used_ends = {n for s in segs for n in s["ends"] if n is not None}
    ctx_unused = ctx_ids - used_ends
    for nid in ctx_unused:           # context chambers no sewer end touches are not imported
        nodes.pop(nid, None)
        node_pos.pop(nid, None)
        node_ref.pop(nid, None)
    rep.count("access_chambers", len(ac_ids - ctx_ids))
    rep.count("context_chambers", len(ctx_ids - ctx_unused))
    rep.count("context_chambers_unused", len(ctx_unused))
    # 2. connection point named by id but slightly further away than the tolerance
    cp_reach = max(4 * tol, 2.0)
    for s in segs:
        for cid in dict.fromkeys(i for i in (s["us_id"], s["ds_id"]) if i in cp_pool and i not in ac_ids):
            best = None
            for e, pt in ((0, s["line"][0]), (1, s["line"][-1])):
                d = _dist(pt, cp_pool[cid])
                if s["ends"][e] is None and d <= cp_reach and (best is None or d < best[1]):
                    best = (e, d)
            if best is not None:
                s["ends"][best[0]] = use_cp(cid)
                rep.warn("END_FAR_FROM_NODE", "conduit", s["pid"], f"end is {_fmt(best[1], 2)} m from "
                         f"connection point {cid} named by id; connected anyway")
    # 3. remaining ends: coincident ends share a junction, lone ends are dangling
    cgrid = _Grid(tol)
    clusters: list = []
    for si, s in enumerate(segs):
        for e, pt in ((0, s["line"][0]), (1, s["line"][-1])):
            if s["ends"][e] is not None:
                continue
            k, _d = cgrid.nearest(pt, tol)
            if k is None:
                k = len(clusters)
                clusters.append({"pts": [], "members": []})
                cgrid.add(k, pt)
            clusters[k]["pts"].append(pt)
            clusters[k]["members"].append((si, e))
    jn, en = 0, 0
    singles: list = []               # lone sewer ends: (node id, segment index, end, point)
    multis: list = []                # coincident-end clusters (>= 2 ends): (node id, members, centroid)
    for cl in clusters:
        pts = cl["pts"]
        c = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        if len(cl["members"]) >= 2:
            jn += 1
            nid, note = new_id(f"J{jn}"), "segment junction"
            rep.count("junction_nodes")
            multis.append((nid, list(cl["members"]), c))
        else:
            en += 1
            nid, note = new_id(f"E{en}"), "dangling end"
            rep.count("dangling_ends")
            singles.append((nid, cl["members"][0][0], cl["members"][0][1], pts[0]))
        nodes[nid] = _clean({"node_id": nid, "geom": list(c), "node_type": "junction",
                             "status": "existing", "data_source": "gis", "notes": note})
        node_pos[nid] = c
        node_ref[nid] = None
        for si, e in cl["members"]:
            segs[si]["ends"][e] = nid
    # 3b. T-junctions: a lone end that lies on another sewer's interior (junction fitting, the main is not
    # split in the GIS) is joined there. The main is split at the tee point into pieces (digitised order,
    # the upstream piece keeps the original id, further pieces get "~2", "~3", ...), a junction node is
    # created at the tee and the branch end attaches to it.
    sewer_codes = codes.get("sewer_type", {})

    def stype_of(row):
        raw = get(row, "sewer_type")
        if blank(raw):
            return ""
        return sewer_codes.get(code_of(raw)) or sewer_codes.get(str(raw).strip().upper()) or ""

    for si, s in enumerate(segs):
        s["parent"], s["part"], s["full"] = si, 0, s["line"]
    tee_tol = float(opt.tee_tolerance_m)
    tee_ends: list = []              # (segment index, end) of branch ends joined at a tee (flow into the main)
    tee_nodes: set = set()
    tees: list = []
    cross_cand: dict = {}            # J node id -> (main pid, pids of a possible pass-through pair)
    tee_cross: dict = {}             # tee node id -> (J node id, main pid, pair pids) for the invert plausibility check
    tee_end_inv: dict = {}           # tee node -> [(pid, invert at the tee)] of unsplit conduits touching it
    suspect_nodes: set = set()       # dangling end nodes whose tee was refused (SUSPECT_TEE)
    leftover: list = []              # multi clusters whose IS / IO members stay behind: (J node id, [members])
    if tee_tol > 0 and (singles or multis):
        sgrid = _SegGrid(10.0, tee_tol)
        cums: dict = {}
        for si, s in enumerate(segs):
            cum = [0.0]
            for k, (a, b) in enumerate(zip(s["line"], s["line"][1:])):
                sgrid.add((si, k), a, b)
                cum.append(cum[-1] + _dist(a, b))
            cums[si] = cum

        def free_by_design(si, e):
            return stype_of(segs[si]["row"]) in ("is", "io") and segs[si]["ends"][1 - e] in ac_ids

        def find_tee(pt, skip):
            """Nearest interior hit of a sewer not in `skip`: (distance, segment, chainage, point). A hit whose
            projection lies next to the main's own end is the endpoint case, returned as the second value."""
            best, near_end = None, None
            for (tj, k), a, b in sgrid.candidates(pt):
                if tj in skip:
                    continue
                d, t, q = _project(pt, a, b)
                if d > tee_tol:
                    continue
                tl = segs[tj]["line"]
                for ei, tp in ((0, tl[0]), (1, tl[-1])):
                    if _dist(q, tp) <= tol:
                        if near_end is None or d < near_end[0]:
                            near_end = (d, tj, ei)
                        break
                else:
                    if best is None or d < best[0]:
                        best = (d, tj, cums[tj][k] + t * _dist(a, b), q)
            return best, near_end

        def false_tee(si, e, tj, nid, chain):
            """A branch end must not tee into a main whose end node the branch's OTHER end already sits at (a short
            stub / parallel sewer leaving the same manhole): it would make a cycle. Reported, end left as is."""
            far = segs[si]["ends"][1 - e]
            if far is None or far not in segs[tj]["ends"]:
                return False
            suspect_nodes.add(nid)
            rep.count("suspect_tees")
            rep.warn("SUSPECT_TEE", "conduit", segs[si]["pid"],
                     f"end lies on sewer {segs[tj]['pid']} (chainage {_fmt(chain, 2)} m) but the other end of "
                     f"{segs[si]['pid']} is already at {far}, an end node of {segs[tj]['pid']}: not joined as a tee "
                     f"(parallel stub / duplicate?); end left to dangling / snap handling")
            return True

        for nid, si, e, pt in singles:
            if free_by_design(si, e):
                continue                 # IS / IO upstream end: free by design
            best, near_end = find_tee(pt, {si})
            if best is not None and false_tee(si, e, best[1], nid, best[2]):
                continue
            if best is not None:
                tees.append((si, e, nid, best[1], best[2], best[3], best[0]))
            elif near_end is not None:
                # beyond the snap tolerance of the main's end node, but its projection is next to that node:
                # join the end to the node, as the endpoint case would
                tn_id = segs[near_end[1]]["ends"][near_end[2]]
                if tn_id is not None and tn_id != nid and tn_id in nodes and \
                        nodes[tn_id].get("notes") != "dangling end":
                    segs[si]["ends"][e] = tn_id
                    del nodes[nid], node_pos[nid], node_ref[nid]
                    rep.count("dangling_ends", -1)
                    rep.warn("END_FAR_FROM_NODE", "conduit", segs[si]["pid"],
                             f"end is {_fmt(_dist(pt, node_pos[tn_id]), 2)} m from node {tn_id} but lies on sewer "
                             f"{segs[near_end[1]]['pid']} next to it; connected there")
        # clusters of two or more ends: when the cluster point lies on the interior of a sewer that is not
        # one of its members, the member ends are branches joined to that main (a junction fitting)
        for nid, members, c in multis:
            best, _ne = find_tee(c, {si for si, _e in members})
            if best is None:
                continue
            att = [(si, e) for si, e in members if not free_by_design(si, e)
                   and not false_tee(si, e, best[1], nid, best[2])]
            # a run split where it crosses the main: the end of one segment and the start of the next segment
            # of the same run (same non-blank us / ds manhole ids) are a continuing run, not two branches
            through: set = set()
            for (si, e), (sj, f) in itertools.combinations(att, 2):
                a_, b_ = segs[si], segs[sj]
                if si != sj and e != f and a_["us_id"] and a_["ds_id"] and \
                        (a_["us_id"], a_["ds_id"]) == (b_["us_id"], b_["ds_id"]) \
                        and (si, e) not in through and (sj, f) not in through:
                    through.update({(si, e), (sj, f)})
            # blank / differing ids: a pair of opposite ends is a possible crossing, checked on the inverts later
            pair_pids: set = set()
            for (si, e), (sj, f) in itertools.combinations([m for m in att if m not in through], 2):
                if si != sj and e != f:
                    pair_pids.update({segs[si]["pid"], segs[sj]["pid"]})
            att = [m for m in att if m not in through]
            if pair_pids and att:
                cross_cand[nid] = (segs[best[1]]["pid"], pair_pids)
            for si, e in att:
                tees.append((si, e, nid, best[1], best[2], best[3], best[0]))
            if att and len(att) < len(members):
                leftover.append((nid, [m for m in members if m not in att]))
    if tees:
        by_t: dict = {}
        for t in tees:
            by_t.setdefault(t[3], []).append(t)
        assign: list = []
        split_of: dict = {}
        tn = 0
        for tj, lst in by_t.items():
            lst.sort(key=lambda t: t[4])
            groups: list = []
            for t in lst:
                if groups and t[4] - groups[-1][0][4] <= tol:
                    groups[-1].append(t)
                else:
                    groups.append([t])
            cuts, tids = [], []
            for g in groups:
                tn += 1
                tid = new_id(f"T{tn}")
                q = g[0][5]
                nodes[tid] = _clean({"node_id": tid, "geom": list(q), "node_type": "junction",
                                     "status": "existing", "data_source": "gis", "notes": "tee (mid-line junction)"})
                node_pos[tid] = q
                node_ref[tid] = None
                tee_nodes.add(tid)
                cuts.append((g[0][4], q))
                tids.append(tid)
                rep.count("tee_junctions")
                for t in g:
                    if t[2] in cross_cand:
                        tee_cross[tid] = (t[2],) + cross_cand[t[2]]
                    assign.append((t[0], t[1], t[2], tid))
                    rep.count("tee_branch_ends")
                    rep.warn("TEE_JOINED", "conduit", segs[t[0]]["pid"], f"end joined mid-line to sewer "
                             f"{segs[tj]['pid']} at chainage {_fmt(t[4], 2)} m ({_fmt(t[6], 2)} m from the line); "
                             f"flow assumed into the main", severity="info")
            main = segs[tj]
            pieces = _cut_line(main["full"], cuts)
            chain = [main["ends"][0]] + tids + [main["ends"][1]]
            main["tee_for"] = [segs[t[0]]["pid"] for t in lst]
            main["line"], main["ends"] = pieces[0], [chain[0], chain[1]]
            split_of[tj] = [tj]
            for r in range(1, len(pieces)):
                segs.append({"pid": main["pid"], "row": main["row"], "line": pieces[r], "us_id": main["us_id"],
                             "ds_id": main["ds_id"], "ends": [chain[r], chain[r + 1]], "parent": tj, "part": r,
                             "full": main["full"]})
                split_of[tj].append(len(segs) - 1)
        keep_j = {nid for nid, _m in leftover}
        for si, e, old, tid in assign:
            idx = si if si not in split_of or e == 0 else split_of[si][-1]
            segs[idx]["ends"][e] = tid
            tee_ends.append((idx, e))
            if old in nodes and old not in keep_j:
                rep.count("dangling_ends" if nodes[old].get("notes") == "dangling end" else "junction_nodes", -1)
                del nodes[old], node_pos[old], node_ref[old]
        for nid, rest in leftover:       # IS / IO ends that stay: a junction if two or more, else dangling
            if len(rest) >= 2:
                continue
            c = node_pos.pop(nid)
            del nodes[nid], node_ref[nid]
            rep.count("junction_nodes", -1)
            en += 1
            e_id = new_id(f"E{en}")
            nodes[e_id] = _clean({"node_id": e_id, "geom": list(c), "node_type": "junction", "status": "existing",
                                  "data_source": "gis", "notes": "dangling end"})
            node_pos[e_id], node_ref[e_id] = c, None
            rep.count("dangling_ends")
            segs[rest[0][0]]["ends"][rest[0][1]] = e_id
            singles.append((e_id, rest[0][0], rest[0][1], c))
        rep.count("tee_mains", len(by_t))
        rep.diagnostics.append(f"Tees: {len(tees)} branch end(s) joined mid-line to {len(by_t)} sewer(s).")
    # 3c. Self-loops: an unsplit sewer whose two ends landed on one node (shorter than the snap tolerance, or
    # a loop back to the same chamber, or both ends teed at one point) cannot be a conduit. It is dropped here,
    # before the topology is built; the node keeps its other sewers.
    parent_n: dict = {}
    for s in segs:
        parent_n[s["parent"]] = parent_n.get(s["parent"], 0) + 1
    dropped: set = set()
    for si, s in enumerate(segs):
        if s["ends"][0] is not None and s["ends"][0] == s["ends"][1] and parent_n[s["parent"]] == 1:
            dropped.add(si)
            s["drop"] = True
    for si in sorted(dropped):
        s = segs[si]
        nid = s["ends"][0]
        rep.count("self_loops_dropped")
        rep.warn("SELF_LOOP", "conduit", s["pid"], f"both ends at node {nid}; length {_fmt(_length(s['line']), 2)} m: "
                 "not imported", xy=node_pos.get(nid))
    for nid in {segs[si]["ends"][0] for si in dropped}:
        if node_ref.get(nid) is not None or nid not in nodes or nid in tee_nodes:
            continue                     # chambers / connection points / tee joints stay
        n_left = sum(1 for si, s in enumerate(segs) if si not in dropped for n in s["ends"] if n == nid)
        if n_left == 0:                  # a junction / dangling node only the dropped sewer touched
            rep.count("dangling_ends" if nodes[nid].get("notes") == "dangling end" else "junction_nodes", -1)
            del nodes[nid], node_pos[nid], node_ref[nid]
        elif n_left == 1 and nodes[nid].get("notes") == "segment junction":
            nodes[nid]["notes"] = "dangling end"      # one sewer end left: no longer a junction
            rep.count("junction_nodes", -1)
            rep.count("dangling_ends")
            multis = [m for m in multis if m[0] != nid]
    parents: dict = {}
    for si, s in enumerate(segs):
        if si not in dropped:
            parents.setdefault(s["parent"], []).append(si)
    tee_by_seg: dict = {}
    for si, e in tee_ends:
        if si not in dropped:
            tee_by_seg.setdefault(si, set()).add(e)
    node_segs: dict = {}
    for si, s in enumerate(segs):
        if si in dropped:
            continue
        for e in (0, 1):
            node_segs.setdefault(s["ends"][e], []).append((si, e))

    # ---- direction ----------------------------------------------------------------------------
    up_end: dict = {si: 0 for si in dropped}      # segment index -> index (0/1) of its upstream end

    def ref_of(si, e):
        return node_ref.get(segs[si]["ends"][e])

    groups: dict = {}
    for si, s in enumerate(segs):
        if si not in dropped:
            groups.setdefault((s["us_id"], s["ds_id"]), []).append(si)
    for (us_id, ds_id), members in groups.items():
        mset = set(members)
        queue: list = []
        for want_us in (True, False):
            for si in members:
                if si in up_end:
                    continue
                target = us_id if want_us else ds_id
                if not target:
                    continue
                for e in (0, 1):
                    if ref_of(si, e) == target:
                        up_end[si] = e if want_us else 1 - e
                        queue.append(si)
                        break
        idset = {us_id, ds_id}
        while queue:
            si = queue.pop(0)
            up = up_end[si]
            for e_here, as_up in ((1 - up, True), (up, False)):
                node = segs[si]["ends"][e_here]
                if node_ref.get(node) in idset and node_ref.get(node):
                    continue
                # Only an unambiguous continuation is followed. At a node where several unoriented sewers of the
                # group meet (a branch carrying the same manhole ids as the main, at a segment joint or tee) the
                # sewer split from the same GIS feature is the continuation; otherwise none is guessed and the
                # neighbour / dead-end / tee rules decide.
                at_node = [(sj, e) for sj, e in node_segs.get(node, ()) if sj in mset and sj != si]
                cands = [(sj, e) for sj, e in at_node if sj not in up_end]
                if len(at_node) > 1:
                    same = [(sj, e) for sj, e in cands if segs[sj]["parent"] == segs[si]["parent"]]
                    # else the one whose far end is attached (a stub's far end is a free dangling end)
                    cands = same or [(sj, e) for sj, e in cands
                                     if node_ref.get(segs[sj]["ends"][1 - e]) is not None
                                     or len(node_segs.get(segs[sj]["ends"][1 - e], ())) >= 2]
                if len(cands) == 1:
                    sj, e = cands[0]
                    up_end[sj] = e if as_up else 1 - e
                    queue.append(sj)
    for si in up_end:
        segs[si]["why"] = "ids"

    def attached(si, e):
        node = segs[si]["ends"][e]
        return node_ref.get(node) is not None or len(node_segs.get(node, ())) >= 2

    def neighbour_hints() -> bool:
        """Orient sewers from already oriented neighbours (any run), repeated until nothing changes."""
        any_change, changed = False, True
        while changed:
            changed = False
            for si, s in enumerate(segs):
                if si in up_end:
                    continue
                votes = set()
                for e in (0, 1):
                    has_in = has_out = False
                    sole = True              # no other unoriented sewer at this node
                    for sj, ej in node_segs.get(s["ends"][e], ()):
                        if sj == si:
                            continue
                        if sj not in up_end:
                            sole = False
                            continue
                        if up_end[sj] == ej:
                            has_out = True
                        else:
                            has_in = True
                    # flows INTO an outflow-only node; flows OUT of an inflow-only node only when this is the only
                    # unoriented sewer there (else it may be one of several branches: ambiguous, order-dependent)
                    if has_out and not has_in:
                        votes.add(1 - e)
                    elif has_in and not has_out and sole:
                        votes.add(e)
                if len(votes) == 1:
                    up_end[si] = votes.pop()
                    s["why"] = "neighbours"
                    changed = any_change = True
        return any_change

    def dead_end_rule() -> bool:
        """Exactly one end attached to the network, the other dangling: the dangling end is upstream."""
        ch = False
        for si, s in enumerate(segs):
            if si in up_end:
                continue
            a0, a1 = attached(si, 0), attached(si, 1)
            if a0 != a1:
                up_end[si] = 1 if a0 else 0
                s["why"] = "dead_end"
                ch = True
        return ch

    def is_io_rule() -> bool:
        """IS / IO sewers: the end that is not at a chamber is upstream."""
        ch = False
        for si, s in enumerate(segs):
            if si in up_end or stype_of(s["row"]) not in ("is", "io"):
                continue
            at_ch = [e for e in (0, 1) if s["ends"][e] in ac_ids and s["ends"][e] in nodes]
            if len(at_ch) == 1:
                up_end[si] = 1 - at_ch[0]
                s["why"] = "is_io"
                ch = True
        return ch

    def tee_rule() -> bool:
        """A branch end joined mid-line to another sewer is the branch's downstream end."""
        ch = False
        for si, es in tee_by_seg.items():
            if si not in up_end and len(es) == 1:
                up_end[si] = 1 - next(iter(es))
                segs[si]["why"] = "tee"
                ch = True
        return ch

    def split_rule() -> bool:
        """The pieces of a split sewer share one orientation (digitised direction of the parent)."""
        ch = False
        for idxs in parents.values():
            known = next((i for i in idxs if i in up_end), None)
            if known is None:
                continue
            for i in idxs:
                if i not in up_end:
                    up_end[i], segs[i]["why"] = up_end[known], segs[known]["why"]
                    ch = True
        return ch

    changed = True
    while changed:
        changed = split_rule()
        changed = tee_rule() or changed
        changed = neighbour_hints() or changed
        changed = dead_end_rule() or changed
        changed = is_io_rule() or changed
    why_notes = {"tee": "direction from tee rule (branch end joined mid-line is downstream)",
                 "neighbours": "direction from neighbouring sewers",
                 "dead_end": "direction from dead-end rule (dangling end is upstream)",
                 "is_io": "direction from IS/IO rule (end not at a chamber is upstream)"}
    why_counts = {"ids": 0, "neighbours": 0, "tee": 0, "dead_end": 0, "is_io": 0, "unknown": 0}
    for idxs in parents.values():
        for si in idxs:
            if si not in up_end:
                up_end[si] = 0
                segs[si]["why"] = "unknown"
        s0 = segs[idxs[0]]
        if s0["why"] == "unknown":
            rep.warn("DIRECTION_UNKNOWN", "conduit", s0["pid"], "flow direction could not be determined from "
                     "manhole ids, neighbouring sewers, tee, dead-end / IS rules; digitised direction kept")
        why_counts[s0["why"]] += 1
    for k, v in why_counts.items():
        rep.count(f"direction_{k}", v)
    rep.diagnostics.append(
        f"Direction: {why_counts['ids']} from manhole ids, {why_counts['neighbours']} from neighbouring sewers, "
        f"{why_counts['tee']} by tee rule, {why_counts['dead_end']} by dead-end rule, {why_counts['is_io']} by IS/IO rule, "
        f"{why_counts['unknown']} unknown (digitised direction kept).")

    # ---- levels: read, then fill the missing inverts ---------------------------------------------
    # Placeholders (<= -99) are missing. A missing invert is estimated (never over a real value) once the
    # connectivity and flow direction are known: from the other end and the grade, else from the adjoining sewers.
    recs: dict = {}                  # parent segment index -> level record of the oriented sewer
    for idxs in parents.values():
        s = segs[idxs[0]]
        flipped = up_end[idxs[0]] == 1
        line = s["full"][::-1] if flipped else s["full"]
        nchain = [s["ends"][0]] + [segs[i]["ends"][1] for i in idxs]
        if flipped:
            nchain = nchain[::-1]
        glen = _length(line)
        length = to_float(get(s["row"], "length"))
        if length is None or length <= 0:
            length = glen
        fgrade = to_float(get(s["row"], "flow_grade"))
        if fgrade is not None and fgrade <= 0:
            fgrade = None
        plens = [_length(segs[i]["line"]) for i in idxs]
        if flipped:
            nchain, plens = nchain[::-1], plens[::-1]
        draw0 = to_float(get(s["row"], "diameter")) if fm.diameter else None
        dmm = float(draw0) if draw0 is not None and draw0 >= 50 else float(opt.default_dn)
        cum, acc = [], 0.0
        for q_ in plens[:-1]:
            acc += q_
            cum.append(acc)
        recs[idxs[0]] = {"pid": s["pid"], "us": nchain[0], "ds": nchain[-1], "glen": glen, "length": length,
                         "fgrade": fgrade, "est": {}, "rej": {}, "dmm": dmm,
                         "dn": min(std, key=lambda x: (abs(x - dmm), x)),
                         "mid": list(zip(nchain[1:-1], cum)),
                         "ui": level(get(s["row"], "us_invert")), "di": level(get(s["row"], "ds_invert"))}
    if recs:
        _fill_inverts(list(recs.values()), rep)

    # ---- conduits --------------------------------------------------------------------------------
    grade_rounding = 0
    conduits: list = []
    outgoing: dict = {}              # node_id -> [us inverts]
    incoming: dict = {}              # node_id -> [ds inverts]
    dn_unmapped = 0
    ratios = {"field": [], "geom": [], "shape": [], "len_vs_geom": [], "shape_vs_len": []}     # levels grade / FLOW_GRADE variants

    cond_ids = set(pipe_ids)
    tee_inv: dict = {}               # tee node -> interpolated invert of the main
    branch_ds: list = []             # (conduit id, ds node, ds invert) of conduits ending at a tee node
    through_pairs: list = []         # (tee node, piece conduit id, next piece conduit id) of split mains
    for idxs in parents.values():
        s = segs[idxs[0]]
        pid, row = s["pid"], s["row"]
        notes: list = []
        data_source = "gis"
        flipped = up_end[idxs[0]] == 1
        line, pieces = s["full"], [segs[i]["line"] for i in idxs]
        nchain = [s["ends"][0]] + [segs[i]["ends"][1] for i in idxs]
        if flipped:
            line, pieces, nchain = line[::-1], [q[::-1] for q in pieces[::-1]], nchain[::-1]
            rep.count("reversed")      # summary line only: a source drawn downstream->upstream reverses all
            notes.append("geometry reversed")
        us, ds = nchain[0], nchain[-1]
        if s["why"] in why_notes:
            notes.append(why_notes[s["why"]])

        # ids are validation only
        mismatch = []
        for end, nid, rid in (("US", us, s["us_id"]), ("DS", ds, s["ds_id"])):
            ref = node_ref.get(nid)
            if ref is None or ref == rid:
                continue
            known = rid in ac_ids or rid in cp_pool
            if not rid or not known:
                why = "blank" if not rid else "unmatched"
                rep.count("ends_snapped")
                rep.warn("END_SNAPPED", "conduit", pid, f"{end} manhole id {why}; connected to node {nid} "
                         f"by geometry ({_fmt(_dist(line[0] if end == 'US' else line[-1], node_pos[nid]), 2)} m)",
                         severity="info")
            elif ref not in (s["us_id"], s["ds_id"]):
                mismatch.append(f"{end} end at {nid}")
        if mismatch:
            rep.warn("ID_MISMATCH", "conduit", pid, f"{'; '.join(mismatch)}, but the sewer's manhole ids are "
                     f"{s['us_id'] or '(blank)'} -> {s['ds_id'] or '(blank)'}")
        # levels, length, grade (read and filled in the pre-pass above)
        rc = recs[idxs[0]]
        ui, di, glen, length, fgrade = rc["ui"], rc["di"], rc["glen"], rc["length"], rc["fgrade"]
        if rc["est"]:
            data_source = "assumed"
            notes.extend(f"{k} invert estimated from {m}" for k, (_v, m, _c) in sorted(rc["est"].items()))
            rep.warn("INVERT_ESTIMATED", "conduit", pid, "; ".join(
                f"{k} invert {_fmt(v, 3)} estimated from {m}" for k, (v, m, _c) in sorted(rc["est"].items(),
                                                                       key=lambda kv: kv[0] != "us")))
        # Missing levels and non-falling grades are reported by the model check that runs after the
        # import (MISSING_INVERT, ADVERSE_GRADE / FLAT_GRADE), not here as well.
        if ui is not None and di is not None:
            drop = ui - di
            if drop > 0 and fgrade and length:
                ratios["field"].append(length / drop / fgrade)
                if glen > 0:
                    ratios["geom"].append(glen / drop / fgrade)
                    ratios["len_vs_geom"].append(length / glen)
                shp = to_float(get(row, "shape_length")) if fm.shape_length else None
                if shp and shp > 0:
                    ratios["shape"].append(shp / drop / fgrade)
                    ratios["shape_vs_len"].append(shp / length)
                lg = length / drop
                if not rc["est"] and abs(lg - fgrade) / fgrade * 100.0 > opt.grade_tolerance_pct:
                    if abs(drop - length / fgrade) > GRADE_MISMATCH_MIN_FALL_M:
                        rep.warn("GRADE_MISMATCH", "conduit", pid, f"grade from levels 1 in {lg:.1f} vs "
                                 f"{fm.flow_grade} 1 in {_fmt(fgrade, 1)} (>{_fmt(opt.grade_tolerance_pct, 1)}%); "
                                 f"fall differs by {abs(drop - length / fgrade):.3f} m over {_fmt(length, 1)} m")
                    else:
                        grade_rounding += 1

        # diameter
        dn, internal = opt.default_dn, None
        draw = to_float(get(row, "diameter")) if fm.diameter else None
        if not fm.diameter:
            dn_unmapped += 1
            notes.append(f"DN {dn} assumed (no diameter field mapped)")
        elif draw is None or draw < 50:
            rep.warn("DN_ASSUMED", "conduit", pid, f"{fm.diameter} value {get(row, 'diameter')!r} is not a "
                     f"usable diameter in mm; DN {dn} assumed")
            notes.append(f"DN {dn} assumed (diameter {get(row, 'diameter')!r})")
        else:
            near = min(std, key=lambda x: (abs(x - draw), x))
            dn = near
            if abs(draw - near) > 1e-6:
                internal = draw
                notes.append(f"source diameter {_fmt(draw)} mm")
                msg = f"diameter {_fmt(draw)} mm is not a standard DN; DN{near} used, raw kept as internal diameter"
                rep.infos.append(f"conduit {pid}: {msg}")
                if abs(draw - near) / draw > 0.1:
                    rep.warn("DN_FAR_FROM_STANDARD", "conduit", pid, msg)

        sewer_type, _stc = code_lookup("sewer_type", fm.sewer_type, row, codes.get("sewer_type", {}),
                                       "reticulation", "conduit", pid, "sewer type")
        status = s["status"]
        material, _ = code_lookup("material", fm.material, row, codes.get("material", {}), "other",
                                  "conduit", pid, "material")
        pur, plan = get(row, "purpose"), get(row, "planset")
        extra = []
        if not blank(pur):
            extra.append(f"PURPOSE={code_of(pur) or str(pur).strip()}")
        if not blank(plan):
            extra.append(f"PLANSET={str(plan).strip()}")
        notes = extra + notes

        if sewer_type == "io":
            cp_has_io.update(cp_by_node[n] for n in (us, ds) if n in cp_by_node)
        if len(pieces) > 1:
            notes.append("split at tee for " + ", ".join(s.get("tee_for", ())))

        def inv_at(x):
            """Invert interpolated by chainage x along the oriented original line."""
            if ui is None or di is None:
                return None
            return round(ui + (di - ui) * (x / glen if glen > 0 else 0.0), 6)

        pos = 0.0
        piece_cids: list = []
        for j, piece in enumerate(pieces):
            pl = _length(piece)
            a, b = pos, pos + pl
            pos = b
            last = j == len(pieces) - 1
            pui = ui if j == 0 else inv_at(a)
            pdi = di if last else inv_at(b)
            plen = length if len(pieces) == 1 else (length * pl / glen if glen > 0 else length / len(pieces))
            cid = pid
            if j:
                cid, k = f"{pid}~{j + 1}", 0
                while cid in cond_ids:
                    k += 1
                    cid = f"{pid}~{j + 1}_{k}"
                cond_ids.add(cid)
            piece_cids.append(cid)
            pus, pds = nchain[j], nchain[j + 1]
            if len(pieces) == 1:
                for tn_, iv_ in ((pus, pui), (pds, pdi)):
                    if tn_ in tee_nodes and iv_ is not None:
                        tee_end_inv.setdefault(tn_, []).append((pid, iv_))
            if pui is not None:
                outgoing.setdefault(pus, []).append(pui)
            if pdi is not None:
                incoming.setdefault(pds, []).append(pdi)
            if not last:
                tee_inv[pds] = pdi
            if last and pds in tee_nodes:
                branch_ds.append((cid, pds, pdi))
            conduits.append(_clean({
                "conduit_id": cid, "geom": [list(p) for p in piece], "us_node_id": pus, "ds_node_id": pds,
                "dn": dn, "internal_diameter_mm": internal, "material": material, "us_invert": pui,
                "ds_invert": pdi, "length_m": plen, "sewer_type": sewer_type, "status": status,
                "data_source": data_source, "install_year": year_of(get(row, "install_date")),
                "asset_id": pid, "notes": "; ".join(notes) or None}))
        for j in range(len(piece_cids) - 1):
            through_pairs.append((nchain[j + 1], piece_cids[j], piece_cids[j + 1]))

    if grade_rounding:
        rep.count("grade_rounding", grade_rounding)
        rep.diagnostics.append(
            f"{grade_rounding} sewer(s) have a levels / {fm.flow_grade} grade difference > "
            f"{_fmt(opt.grade_tolerance_pct, 1)} % but < {GRADE_MISMATCH_MIN_FALL_M * 1000:.0f} mm of fall "
            "(rounding); not listed")
    if rep.counts.get("placeholder_levels"):
        rep.diagnostics.append(f"{rep.counts['placeholder_levels']} placeholder level(s) (<= -99, e.g. -99.9) in "
                               "invert / surface level fields treated as missing.")
    _report_tee_cycles(segs, up_end, tee_nodes, tee_by_seg, rep)
    for cid, nid, di in branch_ds:
        inv = tee_inv.get(nid)
        if inv is not None and di is not None and di < inv - 0.01:
            rep.warn("TEE_ADVERSE", "conduit", cid, f"ds invert {di:.3f} is below the main's interpolated invert "
                     f"{inv:.3f} at the tee ({nid}): data quality")

    # Near misses: lone ends still dangling (and no-chamber clusters that only receive flow) that lie within
    # NEAR_MISS_M of another sewer's mid-line but beyond the tee tolerance (a joined-looking end that did not
    # join). Reported only. Runs after the direction rules, which decide which clusters are inflow-only.
    cond_us = {c["us_node_id"] for c in conduits}
    cond_ds = {c["ds_node_id"] for c in conduits}
    inflow_only: list = []           # (node id, members, centroid) of J clusters nothing flows out of
    for nid, members, c in multis:
        if nid in nodes and node_ref.get(nid) is None and nid in cond_ds and nid not in cond_us:
            inflow_only.append((nid, members, c))
            nodes[nid]["notes"] = "segment junction; inflow only, no downstream sewer"
            rep.count("dangling_ends")
            rep.count("inflow_only_junctions")
    near_miss: list = []
    still = [(t[0], {t[1]}, t[3], t[1], t[2]) for t in singles if t[0] in nodes and t[0] not in suspect_nodes]
    still += [(nid, {si for si, _e in m}, c, m[0][0], m[0][1]) for nid, m, c in inflow_only]
    if still:
        ngrid = _SegGrid(10.0, NEAR_MISS_M)
        for si, s in enumerate(segs):
            if si in dropped:
                continue
            for k, (a, b) in enumerate(zip(s["line"], s["line"][1:])):
                ngrid.add((si, k), a, b)
        for nid, msegs, pt, si, e in still:
            if len(msegs) == 1 and stype_of(segs[si]["row"]) in ("is", "io") and segs[si]["ends"][1 - e] in ac_ids:
                continue                 # IS / IO upstream end: free by design
            mparents = {segs[i]["parent"] for i in msegs}
            best = None
            for (tj, k), a, b in ngrid.candidates(pt):
                if segs[tj]["parent"] in mparents:
                    continue
                d, _t, q = _project(pt, a, b)
                if d > NEAR_MISS_M or (best is not None and d >= best[0]):
                    continue
                tl = segs[tj]["line"]
                if _dist(q, tl[0]) <= tol or _dist(q, tl[-1]) <= tol:
                    continue             # next to a chamber / node: the snap case, not a tee
                best = (d, tj)
            if best is not None:
                near_miss.append((best[0], nid, segs[best[1]]["pid"]))
    if near_miss:
        near_miss.sort()
        rep.count("possible_missed_tees", len(near_miss))
        rep.diagnostics.append(
            f"Possible missed tees: {len(near_miss)} lone sewer end(s) lie within {_fmt(NEAR_MISS_M, 1)} m of "
            f"another sewer's mid-line but beyond the tee join distance ({_fmt(tee_tol, 2)} m).")
        for d, nid, cid in near_miss[:20]:
            rep.diagnostics.append(
                f"  possible missed tee: {nid} {_fmt(d, 2)} m from {cid} - run Join Dangling Sewer Ends or "
                "re-import with a larger tee join distance")
        if len(near_miss) > 20:
            rep.diagnostics.append(f"  ... {len(near_miss) - 20} more not listed.")

    for tid, (jn_id, mpid, pids) in tee_cross.items():
        mi = tee_inv.get(tid)
        far = [(p, v) for p, v in tee_end_inv.get(tid, ()) if p in pids and mi is not None and abs(v - mi) > 1.0]
        if far:
            rep.count("possible_crossing_tees")
            rep.diagnostics.append(
                f"possible crossing joined as a tee: {jn_id} on {mpid} (now {tid}): "
                + ", ".join(f"{p} invert {v:.2f}" for p, v in far)
                + f" vs main {mi:.2f} at the point; check whether these sewers cross the main without connecting")

    if dn_unmapped:
        rep.count("dn_assumed", dn_unmapped)
        rep.warn("DN_NOT_MAPPED", "import", "", f"NO DIAMETER FIELD MAPPED: DN {opt.default_dn} ASSUMED for all "
                 f"{dn_unmapped} conduit(s). Capacity results are meaningless until real diameters are supplied.")

    def med(xs):
        xs = sorted(xs)
        n = len(xs)
        return None if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)

    if ratios["field"]:
        parts = [f"{fm.length or 'length field'}: {med(ratios['field']):.3f}",
                 f"geometry length: {med(ratios['geom']):.3f}" if ratios["geom"] else ""]
        # Shape_Length is in the source layer's units: degrees for geographic (e.g. GDA94 4283) layers,
        # where it is ~1e-5 of the metre length and meaningless here.
        if ratios["shape"] and med(ratios["shape_vs_len"]) > 0.5:
            parts.append(f"{fm.shape_length}: {med(ratios['shape']):.3f}")
        elif ratios["shape"]:
            parts.append(f"{fm.shape_length} ignored (source layer in degrees)")
        rep.diagnostics.append(
            f"Grade check ({len(ratios['field'])} sewer(s) with levels and {fm.flow_grade}): median ratio "
            f"levels grade / {fm.flow_grade} (1 in x values; 1.0 = agree) using " + ", ".join(p for p in parts if p)
            + (f"; median {fm.length}/geometry length {med(ratios['len_vs_geom']):.3f}"
               if ratios["len_vs_geom"] else "") + ". Levels grade is used.")

    # ---- node finishing --------------------------------------------------------------------
    for cid in cp_has_io:
        nodes[cp_map[cid]]["node_type"] = "io_end"
    # The upstream end of an IS / IO sewer has no chamber by design: type the dangling end as such
    # (DS 50 4.2.2) instead of leaving it as an unexplained dangling junction.
    end_types = {"is": ("is_end", "inspection shaft (IS sewer upstream end)"),
                 "io": ("io_end", "inspection opening (IO sewer upstream end)")}
    for c in conduits:
        t = end_types.get(c.get("sewer_type"))
        n = nodes.get(c.get("us_node_id"))
        if t and n is not None and n.get("notes") == "dangling end":
            n["node_type"], n["notes"] = t
            rep.count("is_io_ends")
    dang = {nid for nid, n in nodes.items() if n.get("notes") in ("dangling end",) or
            n.get("notes", "").startswith(("inspection shaft (IS", "inspection opening (IO", "segment junction; inflow only"))}
    n_isio = n_dead = 0
    other: list = []
    for c in conduits:
        for end, nid in (("us", c["us_node_id"]), ("ds", c["ds_node_id"])):
            if nid not in dang:
                continue
            n = nodes[nid]
            if n["node_type"] in ("is_end", "io_end") and end == "us" and n.get("notes", "").startswith("insp"):
                n_isio += 1
            elif end == "us":
                n_dead += 1
            else:
                other.append((c["conduit_id"], n["geom"]))
    rep.count("dangling_is_io", n_isio)
    rep.count("dangling_dead_end", n_dead)
    rep.count("dangling_other", len(other))
    if dang:
        rep.diagnostics.append(f"Dangling ends: {n_isio} IS/IO upstream ends (expected), {n_dead} dead-end "
                               f"upstream starts, {len(other)} other" + (" (listed)." if other else "."))
    for cid, g in other[:20]:
        rep.warn("DANGLING_END", "conduit", cid, f"downstream end is dangling (no chamber, connection point or "
                 f"other sewer) at ({g[0]:.2f}, {g[1]:.2f})")
    if len(other) > 20:
        rep.diagnostics.append(f"... {len(other) - 20} more other dangling end(s) not listed.")
    _interpolate_surface(nodes, conduits, through_pairs, rep)
    for nid, n in nodes.items():
        if nid in outgoing:
            lv, how = min(outgoing[nid]), "lowest outgoing sewer us invert"
        elif nid in incoming:
            lv, how = min(incoming[nid]), "lowest incoming sewer ds invert (no outgoing sewer)"
        else:
            continue
        n["invert_level"] = lv
        n["notes"] = "; ".join(x for x in (n.get("notes"), f"invert derived from {how}") if x)
        # an invert above the lid is reported by the model check (INVERT_ABOVE_SURFACE)
    rep.count("connection_points_used", len(cp_used))
    rep.count("connection_points_skipped", len(cp_pool) - len(cp_used))
    rep.count("nodes", len(nodes))
    rep.count("conduits", len(conduits))
    model = {"nodes": list(nodes.values()), "conduits": conduits}
    if opt.load_connection_points and cp_pool:
        loads = _assign_cp_loads(cp_pool, conduits, float(opt.cp_load_search_m), rep)
        if loads:
            model["node_loads"] = loads
    return model, rep


def _ds50_min_grades() -> tuple:
    """(sorted [(dn, 1-in-x)], None) from the DS 50 ruleset, or ([], reason) when it can't be read."""
    try:
        from . import ruleset
        rows = sorted((int(g["dn"]), float(g["min_grade_1in"])) for g in ruleset.load_ruleset()["grades"])
        if rows:
            return rows, None
        return [], "no grade table"
    except Exception as e:                       # missing / unreadable ruleset: fall back to 1 in 200
        return [], str(e) or e.__class__.__name__


def _fill_inverts(recs: list, rep: ImportReport, max_iter: int = 50):
    """Estimate missing inverts of oriented sewers (``recs``: dicts with us / ds node, ui / di, dn, length,
    fgrade, est, rej, mid), never over a real value. For each missing end, in priority:
    (1) the other side of the node: ds from the largest-DN outgoing sewer's (or the main's tee) us invert,
    lifted so the obverts match when the pipe gets bigger; us from the lowest of the incoming ds inverts, each
    lowered likewise; rejected when it would make the sewer adverse; (2) the other end of the sewer and
    FLOW_GRADE x length; (3) the other end and the DS 50 minimum grade for the DN. Rule 1 runs to a fixpoint
    on real / already estimated values before rules 2 / 3 are applied to what is left, and the whole is
    repeated until nothing changes (bounded). Each estimate is ``rec["est"][end] = (value, method, category)``."""
    by_us: dict = {}
    by_ds: dict = {}
    tee_on: dict = {}                # node -> [(main rec, fraction along it)] where a main passes through it
    for r in recs:
        by_us.setdefault(r["us"], []).append(r)
        by_ds.setdefault(r["ds"], []).append(r)
        for nid, x in r.get("mid", ()):
            tee_on.setdefault(nid, []).append((r, x / r["glen"] if r["glen"] > 0 else 0.0))
    grades, gerr = _ds50_min_grades()

    def put(r, end, v, method, cat):
        v = round(v, 6)
        if r["rej"].get(end):
            method = f"{method}; {r['rej'][end]}"
        r["ui" if end == "us" else "di"] = v
        r["est"][end] = (v, method, cat)

    def tee_value(m, frac):
        if m["ui"] is None or m["di"] is None:
            return None
        return round(m["ui"] + (m["di"] - m["ui"]) * frac, 6)

    def node_side(r, end):
        """(value, method) from the other side of the node, or None."""
        if end == "ds":
            n = r["ds"]
            c = [(q["dmm"], q["ui"], f"the outgoing sewer {q['pid']} us invert at node {n}", q["pid"])
                 for q in by_us.get(n, ()) if q is not r and q["ui"] is not None]
            for m, fr in tee_on.get(n, ()):
                t = tee_value(m, fr) if m is not r else None
                if t is not None:
                    c.append((m["dmm"], t, f"the main {m['pid']} invert at tee node {n}", m["pid"]))
            if not c:
                return None
            d, v, how, _p = min(c, key=lambda x: (-x[0], x[1]))
            lift = max(0.0, d - r["dmm"]) / 1000.0
            if lift > 0:
                how += f" lifted {_fmt(lift, 3)} m (DN{_fmt(r['dmm'])} into DN{_fmt(d)}, obverts matched)"
            return v + lift, how
        n = r["us"]
        c = [(q["di"] - max(0.0, r["dmm"] - q["dmm"]) / 1000.0, q["pid"], q["dmm"])
             for q in by_ds.get(n, ()) if q is not r and q["di"] is not None]
        for m, fr in tee_on.get(n, ()):
            t = tee_value(m, fr) if m is not r else None
            if t is not None:
                c.append((t - max(0.0, r["dmm"] - m["dmm"]) / 1000.0, m["pid"], m["dmm"]))
        if not c:
            return None
        v, pid, _d = min(c)
        how = f"the incoming sewer {pid} ds invert at node {n}" if len(c) == 1 else \
            f"the lowest of {len(c)} incoming sewers' ds inverts at node {n} ({pid})"
        return v, how

    def rule1(r) -> bool:
        did = False
        for end in ("us", "ds"):
            if r["ui" if end == "us" else "di"] is not None or end in r["rej"]:
                continue
            res = node_side(r, end)
            if res is None:
                continue
            v, how = res
            other = r["di"] if end == "us" else r["ui"]
            if other is not None and ((end == "ds" and v > other + 1e-9) or (end == "us" and v < other - 1e-9)):
                r["rej"][end] = f"node-side value {_fmt(v, 3)} rejected (adverse)"
                continue
            put(r, end, v, how, "node")
            did = True
        return did

    def fallback(r) -> bool:
        if (r["ui"] is None) == (r["di"] is None):
            return False
        ln = r["length"]
        if not ln or ln <= 0:
            return False
        end = "ds" if r["di"] is None else "us"
        known, sgn = (r["ui"], -1.0) if end == "ds" else (r["di"], 1.0)
        kn = "us" if end == "ds" else "ds"
        g = r["fgrade"]
        if g:
            how, cat = f"{kn} invert and FLOW_GRADE 1 in {_fmt(g, 1)} over {_fmt(ln, 1)} m", "grade"
        else:
            if grades:
                g = [x for dn, x in grades if dn <= r["dn"]]
                g = g[-1] if g else grades[0][1]
                how = f"{kn} invert and assumed DS 50 minimum grade 1 in {_fmt(g, 1)} over {_fmt(ln, 1)} m"
            else:
                g = 200.0
                how = (f"{kn} invert and assumed 1 in 200 over {_fmt(ln, 1)} m (DS 50 minimum grade table could "
                       f"not be read: {gerr})")
            cat = "ds50"
        put(r, end, known + sgn * ln / g, how, cat)
        return True

    for _ in range(max_iter):
        changed = False
        while True:                                  # rule 1 on real / estimated values, to a fixpoint
            step = False
            for r in recs:
                if rule1(r):
                    step = True
            changed = changed or step
            if not step:
                break
        for r in recs:                               # rules 2 / 3 for what rule 1 found nothing for
            if fallback(r):
                changed = True
        if not changed:
            break
    n_sewers = sum(1 for r in recs if r["est"])
    if n_sewers:
        cats = {"node": 0, "grade": 0, "ds50": 0}
        for r in recs:
            for _v, _m, c in r["est"].values():
                cats[c] += 1
        tot = sum(cats.values())
        rep.count("inverts_estimated", tot)
        rep.count("inverts_estimated_node", cats["node"])
        rep.count("inverts_estimated_grade", cats["grade"])
        rep.count("inverts_estimated_ds50", cats["ds50"])
        rep.count("sewers_with_estimated_inverts", n_sewers)
        left = sum(1 for r in recs if r["ui"] is None or r["di"] is None)
        rep.diagnostics.append(
            f"Estimated {tot} missing invert(s) in {n_sewers} sewer(s) (INVERT_ESTIMATED): {cats['node']} from "
            f"the invert on the other side of the node, {cats['grade']} from FLOW_GRADE and length, "
            f"{cats['ds50']} from the DS 50 minimum grade; {left} sewer(s) still missing an invert "
            "(reported by the model check).")


def _report_tee_cycles(segs: list, up_end: dict, tee_nodes: set, tee_by_seg: dict, rep: ImportReport):
    """After flow directions are set: a directed cycle through a tee node usually means a false tee (SUSPECT_TEE)."""
    if not tee_nodes:
        return
    succ: dict = {}
    for si, s in enumerate(segs):
        if s.get("drop"):
            continue
        up = up_end[si]
        succ.setdefault(s["ends"][up], []).append(s["ends"][1 - up])
    branches: dict = {}
    for si, es in tee_by_seg.items():
        for e in es:
            branches.setdefault(segs[si]["ends"][e], []).append(segs[si]["pid"])
    seen: set = set()
    for tee in sorted(tee_nodes):
        prev: dict = {}
        frontier = list(succ.get(tee, ()))
        for n in frontier:
            prev.setdefault(n, tee)
        found = tee in prev
        while frontier and not found:
            nxt = []
            for n in frontier:
                for m in succ.get(n, ()):
                    if m not in prev:
                        prev[m] = n
                        nxt.append(m)
            found = tee in prev
            frontier = nxt
        if not found:
            continue
        cyc, n = [tee], prev[tee]
        while n != tee:
            cyc.append(n)
            n = prev[n]
        cyc.reverse()
        cyc = [tee] + cyc[:-1]
        key = frozenset(cyc)
        if key in seen:
            continue
        seen.add(key)
        rep.count("suspect_tees")
        rep.warn("SUSPECT_TEE", "node", tee,
                 f"directed cycle through tee {tee}: {' -> '.join(cyc + [cyc[0]])}; branch sewer(s) joined at "
                 f"this tee: {', '.join(branches.get(tee, ())) or '(none)'}; check the tee is real")


def _assign_cp_loads(cp_pool: dict, conduits: list, radius: float, rep: ImportReport) -> list:
    """Each connection point goes to the nearest imported sewer within `radius` (point-to-polyline
    distance); its load is put on that sewer's UPSTREAM node. One ``wwn_node_loads`` row per node."""
    grid = _SegGrid(max(radius, 1.0), radius)
    for ci, c in enumerate(conduits):
        pts = c.get("geom") or []
        for a, b in zip(pts, pts[1:]):
            grid.add(ci, a, b)
    per_node: dict = {}
    unassigned: list = []
    for cid, p in cp_pool.items():
        best = None
        for ci, a, b in grid.candidates(p):
            d = _project(p, a, b)[0]
            if d <= radius and (best is None or (d, ci) < best):
                best = (d, ci)
        if best is None:
            unassigned.append(cid)
            continue
        node = conduits[best[1]]["us_node_id"]
        per_node.setdefault(node, []).append(cid)
    rows = []
    for node, cids in per_node.items():
        rows.append({"load_id": f"CP-{node}", "node_id": node, "connections": len(cids),
                     "source": "connection_points",
                     "notes": f"{len(cids)} connection point(s) within {radius:g} m of the sewer starting here"})
    n_assigned = sum(len(v) for v in per_node.values())
    rep.count("cp_loads_assigned", n_assigned)
    rep.count("cp_load_nodes", len(rows))
    rep.count("cp_loads_unassigned", len(unassigned))
    adwf = n_assigned * ADWF_PER_CONNECTION_LPD / 86400.0
    rep.diagnostics.append(f"Loads: {n_assigned} connection point(s) assigned to {len(rows)} node(s); "
                           f"{len(unassigned)} unassigned; ADWF total {adwf:.3f} L/s at "
                           f"{ADWF_PER_CONNECTION_LPD:g} L/d/connection")
    for cid in unassigned[:10]:
        rep.warn("CP_NOT_ASSIGNED", "connection_point", cid,
                 f"no imported sewer within {radius:g} m; its load is not assigned")
    return rows


def _interpolate_surface(nodes: dict, conduits: list, through_pairs: list, rep: ImportReport):
    """Fill surface_level of junction / tee nodes lying on a chain of sewers between two nodes that
    have a surface level (chambers), linearly by cumulative chainage. A chain runs through nodes that
    have no surface level, are typed `junction` and either join exactly two sewer ends or are a tee on a
    split main (the branch is not part of the main's chain). Chains with a surface level at one end
    only (or none) are left null."""
    ends: dict = {}                  # node -> [(conduit index, end)]
    index = {c["conduit_id"]: i for i, c in enumerate(conduits)}
    for i, c in enumerate(conduits):
        ends.setdefault(c["us_node_id"], []).append((i, 0))
        ends.setdefault(c["ds_node_id"], []).append((i, 1))
    through = {n: (index[a], index[b]) for n, a, b in through_pairs}
    link: dict = {}                  # node -> (conduit i, conduit j) continuing the chain through it
    for nid, lst in ends.items():
        n = nodes.get(nid)
        if n is None or n.get("surface_level") is not None or n.get("node_type") != "junction":
            continue
        if nid in through:
            link[nid] = through[nid]
        elif len(lst) == 2 and lst[0][0] != lst[1][0]:
            link[nid] = (lst[0][0], lst[1][0])

    def other_end(ci, node):
        c = conduits[ci]
        return c["ds_node_id"] if c["us_node_id"] == node else c["us_node_id"]

    def walk(ci, node):
        """Follow the chain from conduit `ci` away from `node`; returns [(conduit, far node), ...]."""
        out: list = []
        seen = {ci}
        while True:
            far = other_end(ci, node)
            out.append((ci, far))
            pair = link.get(far)
            if pair is None:
                return out
            nxt = pair[1] if pair[0] == ci else pair[0]
            if nxt in seen or ci not in pair:
                return out
            seen.add(nxt)
            ci, node = nxt, far

    def clen(ci):
        c = conduits[ci]
        return c.get("length_m") or _length(c["geom"])

    done: set = set()
    for nid, n in nodes.items():
        if n.get("surface_level") is None:
            continue
        for ci, e in ends.get(nid, ()):
            if ci in done:
                continue
            chain = walk(ci, nid)
            done.update(c for c, _ in chain)
            last = chain[-1][1]
            lastn = nodes.get(last)
            if len(chain) < 2 or lastn is None or lastn.get("surface_level") is None or last == nid:
                continue
            z0, z1 = n["surface_level"], lastn["surface_level"]
            total = sum(clen(c) for c, _ in chain)
            cum = 0.0
            for c, far in chain[:-1]:
                cum += clen(c)
                node = nodes[far]
                z = z0 + (z1 - z0) * (cum / total if total > 0 else 0.5)
                node["surface_level"] = round(z, 3)
                node["notes"] = (f"{node['notes']} + " if node.get("notes") else "+ ") + \
                    f"surface interpolated between {nid} and {last}"
                rep.count("surface_interpolated")


def model_json(model: dict) -> str:
    return json.dumps(model, separators=(",", ":"))
