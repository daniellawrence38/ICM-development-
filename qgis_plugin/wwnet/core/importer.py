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
import json
import math
import re
from dataclasses import dataclass, field, fields
from typing import Iterable

from . import paths, tomlmini

GEOM = "_geom"                 # key holding the geometry coordinates in each input row
TEMPLATE_NAME = "watercorp_sewer.toml"

# role -> (config table, key in the template)
_ROLE_KEYS = {
    "ac_id": ("access_chambers", "id"), "ac_surface_level": ("access_chambers", "surface_level"),
    "cp_id": ("connection_points", "id"),
    "pipe_id": ("sewers", "id"), "us_node": ("sewers", "us_node"), "ds_node": ("sewers", "ds_node"),
    "us_invert": ("sewers", "us_invert"), "ds_invert": ("sewers", "ds_invert"),
    "flow_grade": ("sewers", "flow_grade"), "length": ("sewers", "length"),
    "diameter": ("sewers", "diameter"), "status": ("sewers", "status"),
    "sewer_type": ("sewers", "sewer_type"), "purpose": ("sewers", "purpose"),
    "material": ("sewers", "material"), "install_date": ("sewers", "install_date"),
    "planset": ("sewers", "planset"),
}


@dataclass
class FieldMap:
    """Role -> source field name ('' = not mapped). Defaults are Water Corporation names."""
    ac_id: str = "PACID"
    ac_surface_level: str = "TOPLEV"
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


@dataclass
class ImportOptions:
    snap_tolerance_m: float = 0.5
    grade_source: str = "levels"            # "levels" | "field"
    grade_tolerance_pct: float = 10.0
    default_dn: int = 150

    @classmethod
    def from_config(cls, cfg: dict | None = None, **overrides) -> "ImportOptions":
        o = (cfg or load_template()).get("options", {})
        out = cls(float(o.get("snap_tolerance_m", 0.5)), str(o.get("grade_source", "levels")),
                  float(o.get("grade_tolerance_pct", 10.0)), int(o.get("default_dn", 150)))
        for k, v in overrides.items():
            if v is not None:
                setattr(out, k, v)
        return out


@dataclass
class ImportWarning:
    code: str
    object_type: str       # node | conduit | connection_point | import
    object_id: str
    message: str

    def __str__(self) -> str:
        who = f"{self.object_type} {self.object_id}: " if self.object_id else ""
        return f"[{self.code}] {who}{self.message}"


@dataclass
class ImportReport:
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)          # [ImportWarning]
    infos: list = field(default_factory=list)             # [str]
    unknown_codes: dict = field(default_factory=dict)     # {"STATUS": {"X": 3}}

    def warn(self, code: str, object_type: str, object_id: str, message: str):
        self.warnings.append(ImportWarning(code, object_type, str(object_id), message))

    def count(self, key: str, n: int = 1):
        self.counts[key] = self.counts.get(key, 0) + n

    def warning_counts(self) -> dict:
        out: dict = {}
        for w in self.warnings:
            out[w.code] = out.get(w.code, 0) + 1
        return out

    def summary_lines(self, max_warnings: int = 25) -> list:
        c = self.counts
        lines = [f"Imported {c.get('nodes', 0)} node(s) ({c.get('access_chambers', 0)} access chamber(s), "
                 f"{c.get('connection_points_used', 0)} connection point(s), "
                 f"{c.get('placeholder_nodes', 0)} placeholder(s)) and {c.get('conduits', 0)} conduit(s).",
                 f"Skipped: {c.get('connection_points_skipped', 0)} unreferenced connection point(s), "
                 f"{c.get('conduits_skipped', 0)} sewer(s), {c.get('access_chambers_skipped', 0)} chamber(s)."]
        if c.get("reversed"):
            lines.append(f"Reversed digitising direction of {c['reversed']} sewer(s).")
        for fld, codes in self.unknown_codes.items():
            lines.append(f"Unknown {fld} codes: " + ", ".join(f"{k!r} x{v}" for k, v in sorted(codes.items())))
        wc = self.warning_counts()
        if wc:
            lines.append(f"{len(self.warnings)} warning(s): " + ", ".join(f"{k} x{v}" for k, v in sorted(wc.items())))
        lines.extend(str(w) for w in self.warnings[:max_warnings])
        if len(self.warnings) > max_warnings:
            lines.append(f"... {len(self.warnings) - max_warnings} more warning(s) in ImportReport.warnings")
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
    rep = ImportReport()
    tol = float(opt.snap_tolerance_m)

    def get(row, role):
        name = getattr(fm, role)
        return row.get(name) if name else None

    def code_lookup(role, fname, row, cmap, default, obj_type, oid, what):
        """Coded value -> enum text. Unknown/blank -> default + counted warning."""
        raw = get(row, role)
        c = code_of(raw)
        if c in cmap:
            return cmap[c], c
        whole = str(raw).strip().upper() if not blank(raw) else ""
        if whole in cmap:
            return cmap[whole], whole
        label = c if c else "(blank)"
        bucket = rep.unknown_codes.setdefault(fname, {})
        bucket[label] = bucket.get(label, 0) + 1
        rep.warn("unknown_code", obj_type, oid, f"{fname} code {label!r} (value {raw!r}) is not mapped; "
                 f"{what} defaulted to {default!r}")
        return default, c

    # ---- nodes (access chambers) ------------------------------------------------------------
    nodes: dict = {}                 # node_id -> dict (model form)
    node_pos: dict = {}              # node_id -> (x, y)
    ac_ids: set = set()
    cp_pool: dict = {}               # cp id -> (x, y, row)
    grid = _Grid(tol)
    used_ids: set = set()

    for row in access_chambers:
        nid = to_id(get(row, "ac_id"))
        p = _point(row)
        if not nid:
            rep.count("access_chambers_skipped")
            rep.warn("ac_no_id", "node", "", f"access chamber without {fm.ac_id} skipped")
            continue
        if nid in nodes:
            rep.count("access_chambers_skipped")
            rep.warn("ac_duplicate_id", "node", nid, "duplicate access chamber id skipped")
            continue
        if p is None:
            rep.count("access_chambers_skipped")
            rep.warn("ac_no_geometry", "node", nid, "access chamber without geometry skipped")
            continue
        nodes[nid] = _clean({"node_id": nid, "geom": list(p), "node_type": "ac",
                             "surface_level": to_float(get(row, "ac_surface_level")),
                             "status": "existing", "data_source": "gis", "asset_id": nid})
        node_pos[nid] = p
        ac_ids.add(nid)
        used_ids.add(nid)
        grid.add(nid, p)
    rep.count("access_chambers", len(ac_ids))

    for row in connection_points or ():
        cid = to_id(get(row, "cp_id"))
        p = _point(row)
        if not cid or p is None or cid in cp_pool:
            rep.count("connection_points_skipped")
            if cid and cid in cp_pool:
                rep.warn("cp_duplicate_id", "connection_point", cid, "duplicate connection point id skipped")
            continue
        cp_pool[cid] = (p[0], p[1])
    cp_used: set = set()
    cp_has_io: set = set()
    cp_grid = _Grid(tol)
    for cid, p in cp_pool.items():
        cp_grid.add(cid, p)

    def use_cp(cid):
        """Materialise a connection point as a node (once); returns its node id."""
        if cid not in cp_used:
            cp_used.add(cid)
            nid = cid if cid not in used_ids else f"CP{cid}"
            used_ids.add(nid)
            cp_map[cid] = nid
            cp_by_node[nid] = cid
            p = cp_pool[cid]
            nodes[nid] = _clean({"node_id": nid, "geom": list(p), "node_type": "junction",
                                 "status": "existing", "data_source": "gis", "asset_id": cid,
                                 "notes": "connection point"})
            node_pos[nid] = p
        return cp_map[cid]

    cp_map: dict = {}
    cp_by_node: dict = {}

    def resolve_id(ref):
        if not ref:
            return None
        if ref in ac_ids:
            return ref
        if ref in cp_pool:
            return use_cp(ref)
        return None

    def snap(p):
        """Nearest real or placeholder node within tolerance; connection points compete too."""
        nid, d1 = grid.nearest(p, tol)
        cid, d2 = cp_grid.nearest(p, tol)
        if cid is not None and (d1 is None or d2 < d1):
            return use_cp(cid)
        return nid

    def new_id(base):
        nid, k = base, 2
        while nid in used_ids:
            nid = f"{base}_{k}"
            k += 1
        used_ids.add(nid)
        return nid

    # ---- conduits --------------------------------------------------------------------------
    conduits: list = []
    pipe_ids: set = set()
    inverts: dict = {}               # node_id -> [levels]
    dn_unmapped = 0

    for row in sewers:
        pid = to_id(get(row, "pipe_id"))
        line = _line(row)
        if not pid:
            rep.count("conduits_skipped")
            rep.warn("pipe_no_id", "conduit", "", f"sewer without {fm.pipe_id} skipped")
            continue
        if pid in pipe_ids:
            rep.count("conduits_skipped")
            rep.warn("pipe_duplicate_id", "conduit", pid, "duplicate sewer id skipped")
            continue
        if line is None:
            rep.count("conduits_skipped")
            rep.warn("pipe_no_geometry", "conduit", pid, "sewer without usable geometry skipped")
            continue
        pipe_ids.add(pid)
        notes: list = []
        data_source = "gis"

        # end nodes: by id first, then geometry
        us = resolve_id(to_id(get(row, "us_node")))
        ds = resolve_id(to_id(get(row, "ds_node")))
        # digitising direction (uses whichever id-matched end is available)
        flipped = False
        if us is not None:
            flipped = _dist(node_pos[us], line[-1]) < _dist(node_pos[us], line[0])
        elif ds is not None:
            flipped = _dist(node_pos[ds], line[0]) < _dist(node_pos[ds], line[-1])
        if flipped:
            line = line[::-1]
            rep.count("reversed")
            rep.warn("direction_reversed", "conduit", pid, "digitised against flow (upstream node nearer the "
                     "last vertex); geometry reversed")
            notes.append("geometry reversed")
        for end, nid, pt in (("US", us, line[0]), ("DS", ds, line[-1])):
            if nid is None:
                why = "blank" if not to_id(get(row, "us_node" if end == "US" else "ds_node")) else "unmatched"
                nid = snap(pt)
                if nid is not None:
                    rep.count("ends_snapped")
                    rep.warn("end_snapped", "conduit", pid, f"{end} manhole id {why}; snapped to node {nid} "
                             f"({_fmt(_dist(pt, node_pos[nid]), 2)} m)")
                else:
                    nid = new_id(f"X{pid}_{end}")
                    nodes[nid] = _clean({"node_id": nid, "geom": list(pt), "node_type": "junction",
                                         "status": "existing", "data_source": "assumed",
                                         "notes": f"placeholder: {end} end of {pid} matched no chamber"})
                    node_pos[nid] = pt
                    grid.add(nid, pt)
                    rep.count("placeholder_nodes")
                    rep.warn("placeholder_node", "conduit", pid, f"{end} manhole id {why} and no node within "
                             f"{_fmt(tol, 2)} m; placeholder node {nid} created")
            else:
                d = _dist(node_pos[nid], pt)
                if d > tol:
                    rep.warn("end_far_from_node", "conduit", pid, f"{end} end is {_fmt(d, 2)} m from node {nid}")
            if end == "US":
                us = nid
            else:
                ds = nid
        if us == ds:
            rep.warn("self_loop", "conduit", pid, f"US and DS node are both {us}")

        # levels, length, grade
        ui, di = to_float(get(row, "us_invert")), to_float(get(row, "ds_invert"))
        glen = _length(line)
        length = to_float(get(row, "length"))
        if length is None or length <= 0:
            length = glen
        fgrade = to_float(get(row, "flow_grade"))
        if fgrade is not None and fgrade <= 0:
            fgrade = None
        if opt.grade_source == "field" and fgrade and length:
            if ui is not None and di is None:
                di, data_source = ui - length / fgrade, "assumed"
                notes.append(f"ds invert derived from us invert, length and grade 1 in {_fmt(fgrade, 1)}")
                rep.warn("invert_derived", "conduit", pid, f"ds invert {di:.3f} derived from FLOW_GRADE")
            elif di is not None and ui is None:
                ui, data_source = di + length / fgrade, "assumed"
                notes.append(f"us invert derived from ds invert, length and grade 1 in {_fmt(fgrade, 1)}")
                rep.warn("invert_derived", "conduit", pid, f"us invert {ui:.3f} derived from FLOW_GRADE")
        if ui is None or di is None:
            rep.warn("missing_invert", "conduit", pid, f"us invert {_fmt(ui)}, ds invert {_fmt(di)}: "
                     "level(s) missing")
        if ui is not None and di is not None:
            drop = ui - di
            if drop <= 0:
                rep.warn("non_falling_grade", "conduit", pid, f"levels give no fall (us {_fmt(ui)}, "
                         f"ds {_fmt(di)})")
            elif fgrade and length:
                lg = length / drop
                if abs(lg - fgrade) / fgrade * 100.0 > opt.grade_tolerance_pct:
                    rep.warn("grade_mismatch", "conduit", pid, f"grade from levels 1 in {lg:.1f} vs "
                             f"{fm.flow_grade} 1 in {_fmt(fgrade, 1)} (>{_fmt(opt.grade_tolerance_pct, 1)}%)")
        for nid, lv in ((us, ui), (ds, di)):
            if lv is not None:
                inverts.setdefault(nid, []).append(lv)

        # diameter
        dn, internal = opt.default_dn, None
        draw = to_float(get(row, "diameter")) if fm.diameter else None
        if not fm.diameter:
            dn_unmapped += 1
            notes.append(f"DN {dn} assumed (no diameter field mapped)")
        elif draw is None or draw < 50:
            rep.warn("dn_assumed", "conduit", pid, f"{fm.diameter} value {get(row, 'diameter')!r} is not a "
                     f"usable diameter in mm; DN {dn} assumed")
            notes.append(f"DN {dn} assumed (diameter {get(row, 'diameter')!r})")
        else:
            near = min(std, key=lambda s: (abs(s - draw), s))
            dn = near
            if abs(draw - near) > 1e-6:
                internal = draw
                notes.append(f"source diameter {_fmt(draw)} mm")
                msg = f"diameter {_fmt(draw)} mm is not a standard DN; DN{near} used, raw kept as internal diameter"
                rep.infos.append(f"conduit {pid}: {msg}")
                if abs(draw - near) / draw > 0.1:
                    rep.warn("dn_far_from_standard", "conduit", pid, msg)

        sewer_type, stype_code = code_lookup("sewer_type", fm.sewer_type, row, codes.get("sewer_type", {}),
                                             "reticulation", "conduit", pid, "sewer type")
        status, _ = code_lookup("status", fm.status, row, codes.get("status", {}), "existing",
                                "conduit", pid, "status")
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
        conduits.append(_clean({
            "conduit_id": pid, "geom": [list(p) for p in line], "us_node_id": us, "ds_node_id": ds,
            "dn": dn, "internal_diameter_mm": internal, "material": material, "us_invert": ui,
            "ds_invert": di, "length_m": length, "sewer_type": sewer_type, "status": status,
            "data_source": data_source, "install_year": year_of(get(row, "install_date")),
            "asset_id": pid, "notes": "; ".join(notes) or None}))

    if dn_unmapped:
        rep.count("dn_assumed", dn_unmapped)
        rep.warn("dn_not_mapped", "import", "", f"NO DIAMETER FIELD MAPPED: DN {opt.default_dn} ASSUMED for all "
                 f"{dn_unmapped} conduit(s). Capacity results are meaningless until real diameters are supplied.")

    # ---- node finishing --------------------------------------------------------------------
    for cid in cp_has_io:
        nodes[cp_map[cid]]["node_type"] = "io_end"
    for nid, lv in inverts.items():
        n = nodes[nid]
        n["invert_level"] = min(lv)
        n["notes"] = "; ".join(x for x in (n.get("notes"), "invert derived from connected pipes") if x)
        sl = n.get("surface_level")
        if sl is not None and n["invert_level"] > sl:
            rep.warn("invert_above_surface", "node", nid, f"invert {n['invert_level']:.3f} is above surface "
                     f"level {sl:.3f}")
    rep.count("connection_points_used", len(cp_used))
    rep.count("connection_points_skipped", len(cp_pool) - len(cp_used))
    rep.count("nodes", len(nodes))
    rep.count("conduits", len(conduits))
    model = {"nodes": list(nodes.values()), "conduits": conduits}
    return model, rep


def model_json(model: dict) -> str:
    return json.dumps(model, separators=(",", ":"))
