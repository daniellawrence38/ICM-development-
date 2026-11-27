"""'Import Pressure Data': add Water Corporation (WA) sewer pump stations and pressure mains (rising mains)
to an EXISTING WWNet GeoPackage (made by Import Sewer Data).

Pure logic (no arcpy): sentinel cleaning, id rules, endpoint-graph chaining of pressure main segments,
orientation, wet well / discharge assignment, GeoPackage read / write (sqlite3). The arcpy reading layer
reuses the gravity importer's helpers (``sewer_import``).

Rules (see arcgis_toolbox/README.md, "Import Pressure Data"):
* A rising main starts at a segment end within the search distance of a pump station and continues through
  degree-2 joins (ends within the snap tolerance) until a dead end or a junction (degree >= 3).
* The UPSTREAM / DOWNSTREAM level fields of pressure mains are unreliable and are NOT used (no profile).
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
from dataclasses import dataclass, field

from . import gpkg, sewer_import

MARKER = "Created by Import Pressure Data"
HANGING_NOTE = "rising main discharge, not connected: connect manually"   # also read by icm_export
SRS = 7850
MAX_LIST = 30
GRAVITY_READ_BUFFER_M = 200.0     # extent (model nodes + pump stations) grown by this when reading mains

#: (role, layer key, parameter label, required)
FIELD_ROLES = [
    ("ps_id", "ps", "Pump station ID field (SAP_ID)", True),
    ("ps_alt_id", "ps", "Pump station fallback ID field (used when the ID is empty)", False),
    ("ps_name", "ps", "Pump station name field", False),
    ("ps_name2", "ps", "Pump station second name field", False),
    ("ps_status", "ps", "Pump station status field", False),
    ("main_id", "main", "Pressure main ID field", False),
    ("main_status", "main", "Pressure main status field", False),
    ("diameter", "main", "Pressure main diameter field (nominal mm)", False),
    ("internal_diameter", "main", "Pressure main internal diameter field (mm)", False),
    ("material", "main", "Pressure main material field", False),
    ("length", "main", "Pressure main length field (m)", False),
    ("main_name", "main", "Pressure main name field", False),
]
ROLE_LAYER = {r: k for r, k, _l, _q in FIELD_ROLES}
_TEMPLATE_KEYS = {
    "ps_id": ("pump_stations", "id"), "ps_alt_id": ("pump_stations", "alt_id"),
    "ps_name": ("pump_stations", "name"), "ps_name2": ("pump_stations", "name2"),
    "ps_status": ("pump_stations", "status"),
    "main_id": ("mains", "id"), "main_status": ("mains", "status"), "diameter": ("mains", "diameter"),
    "internal_diameter": ("mains", "internal_diameter"), "material": ("mains", "material"),
    "length": ("mains", "length"), "main_name": ("mains", "main_name"),
}
GEOM = "_geom"


def importer():
    return sewer_import.importer()


def template_config() -> dict:
    return importer().load_template()


def template_defaults() -> dict:
    """role -> default source field name from [pressure.fields.*] ({} when the template cannot be loaded)."""
    try:
        f = template_config().get("pressure", {}).get("fields", {})
        return {r: f.get(t, {}).get(k, "") for r, (t, k) in _TEMPLATE_KEYS.items()}
    except Exception:  # noqa: BLE001 - parameter UI must not fail on a packaging problem
        return {}


def template_options() -> dict:
    try:
        return dict(template_config().get("pressure", {}))
    except Exception:  # noqa: BLE001
        return {}


def roles_for(layer_key: str, fields: dict) -> dict:
    return {r: fields.get(r) or "" for r, k, _l, _q in FIELD_ROLES if k == layer_key}


@dataclass
class Options:
    search_m: float = 30.0
    snap_m: float = 1.0
    discharge_m: float = 1.0      # far end joins an existing node only when it meets it (no guessing)
    wet_well_m: float = 1.0       # the gravity sewer end AT the station point is its wet well (no guessing further)
    include_not_in_use: bool = False


@dataclass
class PressureResult:
    pump_stations: list = field(default_factory=list)   # dicts, see _ps_row
    rising_mains: list = field(default_factory=list)
    new_nodes: list = field(default_factory=list)       # {node_id, node_type, xy, notes}
    retype_nodes: dict = field(default_factory=dict)    # node_id -> new node_type
    warnings: list = field(default_factory=list)
    infos: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)


# ------------------------------------------------------------------ value cleaning

def clean_number(v):
    """Float, or None for blanks and the GIS sentinels (-99.9 levels, -9996, -9998 ... any value <= -99)."""
    f = importer().to_float(v)
    return None if f is None or f <= -99 else f


def clean_positive(v):
    """As clean_number but also None for 0 and negatives (diameters, lengths)."""
    f = clean_number(v)
    return None if f is None or f <= 0 else f


def status_code(v) -> str:
    return importer().code_of(v)


def status_importable(code: str, include_not_in_use: bool) -> bool:
    return code == "A" or (include_not_in_use and code == "N")


def ps_id_of(row: dict) -> str:
    """SAP_ID when non-empty, else the numeric ID as integer text ('' when neither)."""
    imp = importer()
    sid = imp.to_id(row.get("ps_id"))
    if sid:
        return sid
    alt = row.get("ps_alt_id")
    f = imp.to_float(alt)
    if f is not None and f.is_integer():
        return str(int(f))
    return imp.to_id(alt)


def normalise_rows(rows: list, roles_fields: dict) -> list:
    """Source rows ({field name: value, GEOM}) -> role-keyed rows (case-insensitive field lookup)."""
    out = []
    for r in rows:
        low = {str(k).lower(): v for k, v in r.items() if k != GEOM}
        d = {role: low.get(fn.lower()) for role, fn in roles_fields.items() if fn}
        d[GEOM] = r.get(GEOM)
        out.append(d)
    return out


# ------------------------------------------------------------------ geometry helpers

def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _length(coords) -> float:
    return sum(_dist(a, b) for a, b in zip(coords, coords[1:]))


def _dedupe(coords) -> list:
    out = []
    for p in coords:
        if not out or _dist(out[-1], p) > 1e-9:
            out.append((float(p[0]), float(p[1])))
    return out


class _PointGrid:
    def __init__(self, cell: float):
        self.cell = max(cell, 1e-6)
        self.cells = {}

    def _key(self, p):
        return int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell))

    def add(self, ref, p):
        self.cells.setdefault(self._key(p), []).append((ref, p))

    def within(self, p, tol: float):
        kx, ky = self._key(p)
        n = int(math.ceil(tol / self.cell))
        for ix in range(kx - n, kx + n + 1):
            for iy in range(ky - n, ky + n + 1):
                for ref, q in self.cells.get((ix, iy), ()):
                    d = _dist(p, q)
                    if d <= tol:
                        yield d, ref

    def nearest(self, p, tol: float, pred=None):
        best = None
        for d, ref in self.within(p, tol):
            if pred is not None and not pred(ref):
                continue
            if best is None or (d, str(ref)) < (best[0], str(best[1])):
                best = (d, ref)
        return best


# ------------------------------------------------------------------ chaining

def _clean_segments(main_rows: list, cmap: dict, opts: Options, res: PressureResult):
    """Importable segments as dicts (idx, id, coords, dn, indiam, material, length, name)."""
    imp = importer()
    segs, skipped, nostatus = [], {}, False
    for i, r in enumerate(main_rows):
        code = status_code(r.get("main_status"))
        if "main_status" in r:
            if not status_importable(code, opts.include_not_in_use):
                skipped[code or "?"] = skipped.get(code or "?", 0) + 1
                continue
        else:
            nostatus = True
        g = r.get(GEOM)
        coords = _dedupe(g) if g else []
        sid = imp.to_id(r.get("main_id")) or f"row{i + 1}"
        if len(coords) < 2:
            res.warnings.append(f"Pressure main {sid}: no usable geometry; skipped.")
            continue
        mat = None
        mcode = imp.code_of(r.get("material"))
        if mcode:
            mat = cmap.get(mcode)
            if mat is None:
                mat = "other"
                res.counts.setdefault("_unknown_material", {})
                res.counts["_unknown_material"][mcode] = res.counts["_unknown_material"].get(mcode, 0) + 1
        segs.append({"idx": len(segs), "id": sid, "coords": coords, "dn": clean_positive(r.get("diameter")),
                     "indiam": clean_positive(r.get("internal_diameter")), "material": mat,
                     "length": clean_positive(r.get("length")), "name": (imp.to_id(r.get("main_name")) or ""),
                     "geom_len": _length(coords)})
    if skipped:
        res.infos.append("Pressure main segments skipped by status: "
                         + ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())) + ".")
    if nostatus:
        res.infos.append("Pressure mains: no status value / field; all of them are imported.")
    return segs


def _cluster_ends(segs: list, snap: float):
    """Union endpoints within `snap`. Returns (cluster id per (seg, end), members {cluster: [(seg, end)]})."""
    ends = [(s["idx"], e, s["coords"][0 if e == 0 else -1]) for s in segs for e in (0, 1)]
    parent = list(range(len(ends)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    grid = _PointGrid(max(snap, 0.01))
    for k, (_s, _e, p) in enumerate(ends):
        for _d, j in grid.within(p, snap):
            ra, rb = find(k), find(j)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        grid.add(k, p)
    cl, members = {}, {}
    for k, (s, e, _p) in enumerate(ends):
        c = find(k)
        cl[(s, e)] = c
        members.setdefault(c, []).append((s, e))
    return cl, members


def chain_segments(segs: list, ps_list: list, opts: Options):
    """Chain segments into rising mains. `ps_list` = [(ps_id, (x, y))].

    Returns (chains, unused_idx). A chain = {ps_id, order: [(seg_idx, reversed)], coords, end_kind
    ('dead_end' | 'junction' | 'loop'), start_dist, start_kind ('dead_end' | 'joint')}. Each chain starts at a
    segment end within `opts.search_m` of its pump station (an end is only offered to its NEAREST station;
    dead ends are preferred, else the single nearest end) and is oriented from that end."""
    cl, members = _cluster_ends(segs, opts.snap_m)
    deg = {c: len(m) for c, m in members.items()}
    by_idx = {s["idx"]: s for s in segs}
    # candidate ends per station
    cands = {}
    for s in segs:
        for e in (0, 1):
            p = s["coords"][0 if e == 0 else -1]
            near = [(_dist(p, xy), pid) for pid, xy in ps_list if _dist(p, xy) <= opts.search_m]
            if not near:
                continue
            d, pid = min(near)
            cands.setdefault(pid, []).append((0 if deg[cl[(s["idx"], e)]] == 1 else 1, d, s["idx"], e))
    picked = []
    for pid, lst in cands.items():
        dead = [c for c in lst if c[0] == 0]
        use = dead if dead else [min(lst, key=lambda c: (c[1], c[2], c[3]))]
        picked += [(c[1], pid, c[2], c[3], "dead_end" if c[0] == 0 else "joint") for c in use]
    picked.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    used, chains = set(), []
    for d, pid, s0, e0, kind in picked:
        if s0 in used:
            continue
        order, seen, seg, end, stop = [], set(), s0, e0, "dead_end"
        while True:
            rev = end == 1
            order.append((seg, rev))
            seen.add(seg)
            far = 0 if rev else 1
            c = cl[(seg, far)]
            if deg[c] == 1:
                stop = "dead_end"
                break
            if deg[c] == 2:
                other = [m for m in members[c] if m != (seg, far)][0]
                if other[0] in seen or other[0] in used:
                    stop = "loop"
                    break
                seg, end = other
                continue
            stop = "junction"
            break
        used.update(seen)
        coords = []
        for si, rev in order:
            cs = by_idx[si]["coords"][::-1] if rev else by_idx[si]["coords"]
            if coords and _dist(coords[-1], cs[0]) <= 1e-9:
                cs = cs[1:]
            coords.extend(cs)
        chains.append({"ps_id": pid, "order": order, "coords": coords, "end_kind": stop, "start_dist": d,
                       "start_kind": kind})
    return chains, [s["idx"] for s in segs if s["idx"] not in used]


def _most_common(values):
    """Most frequent value (ties: the earliest in `values`)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    best = max(set(vals), key=lambda v: (vals.count(v), -vals.index(v)))
    return best


def _chain_to_main(chain: dict, by_idx: dict, rm_id: str, res: PressureResult) -> dict:
    segs = [by_idx[i] for i, _r in chain["order"]]
    dns = [round(s["dn"]) for s in segs if s["dn"]]
    dn = _most_common(dns)
    if dns and len(set(dns)) > 1:
        res.warnings.append(f"Rising main {rm_id}: diameters differ along the main ({sorted(set(dns))} mm); "
                            f"using the most common, DN{dn}.")
    if dn is None:
        res.warnings.append(f"Rising main {rm_id}: no valid diameter; dn left empty.")
    ind = _most_common([s["indiam"] for s in segs if s["dn"] and round(s["dn"]) == dn]) or \
        _most_common([s["indiam"] for s in segs])
    mats = [s["material"] for s in segs]
    material = _most_common(mats)
    if len({m for m in mats if m}) > 1:
        res.infos.append(f"Rising main {rm_id}: several materials along the main "
                         f"({', '.join(sorted({m for m in mats if m}))}); using {material}.")
    length = sum(s["length"] if s["length"] else s["geom_len"] for s in segs)
    names = sorted({s["name"] for s in segs if s["name"]})
    ids = [s["id"] for s in segs]
    shown = ", ".join(ids[:20]) + (f" ... (+{len(ids) - 20})" if len(ids) > 20 else "")
    notes = "; ".join(x for x in ("/".join(names), f"segments: {shown}") if x)
    return {"rm_id": rm_id, "ps_id": chain["ps_id"], "dn": dn, "internal_diameter_mm": ind, "material": material,
            "roughness_type": "ks", "roughness": None, "length_m": round(length, 3), "profile_json": None,
            "notes": notes, "coords": chain["coords"], "end_kind": chain["end_kind"], "status": "existing",
            "data_source": "gis"}


# ------------------------------------------------------------------ model-level build

def stale_node_ids(ps_ids, nodes) -> set:
    """Ids of nodes this tool created earlier for `ps_ids` (marker in notes + WW_/D_RM_ id pattern)."""
    out = set()
    for n in nodes:
        if MARKER not in (n.get("notes") or ""):
            continue
        nid = n["node_id"]
        for p in ps_ids:
            if nid == "WW_" + p or re.fullmatch(f"D_RM_{re.escape(p)}(_\\d+)?", nid):
                out.add(nid)
    return out


# Must equal importer.OVERFLOW_NOTE (conduit notes marker of emergency overflow pipes).
OVERFLOW_NOTE = "overflow pipe"


def build_pressure(ps_rows: list, main_rows: list, nodes: list, conduit_ends, opts: Options,
                   cmap: dict | None = None) -> PressureResult:
    """Pure core. `ps_rows` / `main_rows`: role-keyed rows with GEOM (EPSG:7850). `nodes`: [{node_id,
    node_type, xy, notes}] of the model; `conduit_ends`: [(us_node_id, ds_node_id[, conduit notes])]."""
    imp = importer()
    if cmap is None:
        cmap = {k.upper(): v for k, v in template_config().get("codes", {}).get("material", {}).items()}
    res = PressureResult()
    # --- pump stations
    pss, seen_ids, skipped = [], set(), {}
    for r in ps_rows:
        code = status_code(r.get("ps_status"))
        if "ps_status" in r and not status_importable(code, opts.include_not_in_use):
            skipped[code or "?"] = skipped.get(code or "?", 0) + 1
            continue
        pid = ps_id_of(r)
        g = r.get(GEOM)
        if not pid:
            res.warnings.append("A pump station has neither a SAP_ID nor an ID; skipped.")
            continue
        if not g:
            res.warnings.append(f"Pump station {pid} has no geometry; skipped.")
            continue
        base, n = pid, 1
        while pid in seen_ids:
            n += 1
            pid = f"{base}_{n}"
        if n > 1:
            res.warnings.append(f"Duplicate pump station id {base}; the repeat is imported as {pid}.")
        seen_ids.add(pid)
        names = [imp.to_id(r.get("ps_name")), imp.to_id(r.get("ps_name2"))]
        notes = " | ".join(dict.fromkeys(x for x in names if x))
        if code == "N":
            notes = (notes + " " if notes else "") + "(not in use)"
        pss.append({"ps_id": pid, "xy": (float(g[0]), float(g[1])), "notes": notes or None})
    if skipped:
        res.infos.append("Pump stations skipped by status: "
                         + ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())) + ".")
    ps_ids = [p["ps_id"] for p in pss]
    stale = stale_node_ids(ps_ids, nodes)
    live = [n for n in nodes if n["node_id"] not in stale]
    node_by_id = {n["node_id"]: n for n in live}
    # conduit_ends rows are (us, ds) or (us, ds, notes); a node whose only outgoing conduits are overflow
    # pipes (flow only when pumps fail) still counts as a dead end, so a pump station inlet is matched.
    outgoing = {c[0] for c in conduit_ends if OVERFLOW_NOTE not in ((c[2] if len(c) > 2 else None) or "")}
    incoming = {c[1] for c in conduit_ends}
    terminal = {nid for nid in incoming if nid not in outgoing and nid in node_by_id}
    cell = max(opts.search_m, opts.discharge_m, opts.wet_well_m, 1.0)
    grid = _PointGrid(cell)
    for n in live:
        grid.add(n["node_id"], n["xy"])

    def add_node(nid, ntype, xy, notes):
        if nid in node_by_id:
            return nid
        n = {"node_id": nid, "node_type": ntype, "xy": xy, "notes": notes}
        node_by_id[nid] = n
        grid.add(nid, xy)
        res.new_nodes.append(n)
        return nid

    # --- chains
    segs = _clean_segments(main_rows, cmap, opts, res)
    by_idx = {s["idx"]: s for s in segs}
    chains, unused = chain_segments(segs, [(p["ps_id"], p["xy"]) for p in pss], opts)
    if unused:
        res.infos.append(f"{len(unused)} pressure main segment(s) are not connected to an imported pump station "
                         f"(within {opts.search_m:g} m) and were ignored.")
    for code, n in sorted(res.counts.pop("_unknown_material", {}).items()):
        res.warnings.append(f"Pressure main material code {code!r} ({n} segment(s)) is not in the template; "
                            "mapped to 'other'.")
    # --- wet wells (before discharge so cascaded stations can discharge to a wet well)
    claimed = set()
    ps_by_id = {p["ps_id"]: p for p in pss}
    for p in pss:
        hit = grid.nearest(p["xy"], opts.wet_well_m, lambda nid: nid in terminal and nid not in claimed)
        if hit:
            nid = hit[1]
            claimed.add(nid)
            if node_by_id[nid]["node_type"] != "wet_well":
                res.retype_nodes[nid] = "wet_well"
                node_by_id[nid] = dict(node_by_id[nid], node_type="wet_well")
            p["wet_well_node_id"] = nid
        else:
            # never guess: no gravity sewer ends at the station point -> a new wet well there, left unconnected
            far = min(((_dist(p["xy"], node_by_id[t]["xy"]), t) for t in terminal if t not in claimed),
                      default=None, key=lambda x: (x[0], str(x[1])))
            nearest = f"nearest dead-end gravity node {far[1]} {far[0]:.1f} m" if far else "no dead-end gravity node"
            p["wet_well_node_id"] = add_node("WW_" + p["ps_id"], "wet_well", p["xy"],
                                             f"{MARKER}; no gravity sewer ends at the pump station point: "
                                             "connect the inlet sewer manually")
            res.warnings.append(f"Pump station {p['ps_id']}: no gravity sewer ends within {opts.wet_well_m:g} m of "
                                f"the station point ({nearest}); wet well {p['wet_well_node_id']} created at the "
                                "station point and left unconnected — connect the inlet sewer manually or raise "
                                "the wet well distance.")
    # --- rising mains
    counter = {}
    for ch in chains:
        pid = ch["ps_id"]
        counter[pid] = counter.get(pid, 0) + 1
        rm_id = "RM_" + pid + ("" if counter[pid] == 1 else f"_{counter[pid]}")
        rm = _chain_to_main(ch, by_idx, rm_id, res)
        if ch["start_kind"] == "joint":
            res.warnings.append(f"Rising main {rm_id}: the nearest main end to pump station {pid} is not a dead "
                                "end (joins other segments); check the chain.")
        end = rm["coords"][-1]
        not_wet = lambda nid: nid != ps_by_id[pid]["wet_well_node_id"]  # noqa: E731
        hit = grid.nearest(end, opts.discharge_m, not_wet)
        if hit:
            rm["discharge_node_id"] = hit[1]
        else:
            # never guess: the far end joins an existing node only when it meets it; otherwise it hangs at a new node
            near = grid.nearest(end, max(opts.search_m, opts.discharge_m), not_wet)
            rm["discharge_node_id"] = add_node("D_" + rm_id, "junction", (end[0], end[1]),
                                               f"{MARKER}; {HANGING_NOTE}")
            nearest = f"nearest {near[1]} {near[0]:.1f} m" if near else f"no node within {max(opts.search_m, opts.discharge_m):g} m"
            res.warnings.append(f"Rising main {rm_id}: discharge end is not at a gravity node ({nearest}); "
                                f"left hanging at new node {rm['discharge_node_id']} — connect it manually.")
        if ch["end_kind"] == "junction":
            res.warnings.append(f"Rising main {rm_id} ends at a junction of three or more pressure main "
                                "segments (e.g. mains combining); the chain stops there. Check the downstream main.")
        elif ch["end_kind"] == "loop":
            res.warnings.append(f"Rising main {rm_id} runs into a loop or another chain; the chain stops there.")
        if not hit and ch["end_kind"] == "dead_end":
            res.infos.append(f"Rising main {rm_id}: its far end is a dead end away from the gravity model; if it "
                             "should continue, select more of the layer or widen the area.")
        res.rising_mains.append(rm)
    first_rm = {}
    for rm in res.rising_mains:
        first_rm.setdefault(rm["ps_id"], rm)
    for p in pss:
        rm = first_rm.get(p["ps_id"])
        p["rising_main_id"] = rm["rm_id"] if rm else None
        p["discharge_node_id"] = rm["discharge_node_id"] if rm else None
        if rm is None:
            res.warnings.append(f"Pump station {p['ps_id']}: no pressure main end within {opts.search_m:g} m; "
                                "imported without a rising main.")
        p["status"] = "existing"
        p["data_source"] = "gis"
    res.pump_stations = pss
    res.counts.update({"pump_stations": len(pss), "rising_mains": len(res.rising_mains),
                       "new_nodes": len(res.new_nodes), "wet_wells_retyped": len(res.retype_nodes)})
    return res


# ------------------------------------------------------------------ GeoPackage I/O

def _wkb_point(xy) -> bytes:
    return struct.pack("<BIdd", 1, 1, float(xy[0]), float(xy[1]))


def _wkb_line(coords) -> bytes:
    return struct.pack("<BII", 1, 2, len(coords)) + b"".join(struct.pack("<dd", float(x), float(y))
                                                              for x, y in coords)


def point_blob(xy) -> bytes:
    return b"GP" + bytes([0, 1]) + struct.pack("<i", SRS) + _wkb_point(xy)


def line_blob(coords) -> bytes:
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return (b"GP" + bytes([0, 3]) + struct.pack("<i", SRS)
            + struct.pack("<4d", min(xs), max(xs), min(ys), max(ys)) + _wkb_line(coords))


def blob_xy(blob):
    """First point of a GeoPackage point blob (any envelope), or None."""
    if not blob:
        return None
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((blob[3] >> 1) & 7, 0)
    wkb = blob[8 + env:]
    bo = "<" if wkb[0] == 1 else ">"
    if struct.unpack(bo + "I", wkb[1:5])[0] != 1:
        return None
    return struct.unpack(bo + "dd", wkb[5:21])


def read_model(path: str) -> dict:
    """{'nodes': [{node_id, node_type, xy, notes}], 'conduits': [(us, ds, notes)], 'extent': (x0, y0, x1, y1) | None}."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        nodes = []
        for nid, nt, g, notes in con.execute("SELECT node_id, node_type, geom, notes FROM wwn_nodes"):
            xy = blob_xy(g)
            if xy is not None:
                nodes.append({"node_id": nid, "node_type": nt, "xy": xy, "notes": notes})
        conduits = [(u, d, n) for u, d, n in con.execute("SELECT us_node_id, ds_node_id, notes FROM wwn_conduits")]
    finally:
        con.close()
    ext = None
    if nodes:
        xs, ys = [n["xy"][0] for n in nodes], [n["xy"][1] for n in nodes]
        ext = (min(xs), min(ys), max(xs), max(ys))
    return {"nodes": nodes, "conduits": conduits, "extent": ext}


_HYD = ("pump_rate_lps", "duty_config", "wet_well_area_m2", "start_level", "stop_level", "overflow_level",
        "emergency_storage_m3")


def write_result(path: str, res: PressureResult) -> dict:
    """Replace the previous pressure data of the imported ps_ids and insert the new rows. Hydraulic values
    already entered on an existing pump station row (and its wwn_pumps rows) are kept."""
    ps_ids = [p["ps_id"] for p in res.pump_stations]
    q = lambda n: ",".join("?" * n)  # noqa: E731
    con = sqlite3.connect(path, timeout=15)
    stats = {"replaced_pump_stations": 0, "replaced_rising_mains": 0, "deleted_nodes": 0}
    try:
        con.execute("BEGIN IMMEDIATE")
        kept = {}
        if ps_ids:
            for row in con.execute(f"SELECT ps_id, {', '.join(_HYD)} FROM wwn_pump_stations "
                                   f"WHERE ps_id IN ({q(len(ps_ids))})", ps_ids):
                kept[row[0]] = dict(zip(_HYD, row[1:]))
            stats["replaced_rising_mains"] = con.execute(
                f"DELETE FROM wwn_rising_mains WHERE ps_id IN ({q(len(ps_ids))})", ps_ids).rowcount
            stats["replaced_pump_stations"] = con.execute(
                f"DELETE FROM wwn_pump_stations WHERE ps_id IN ({q(len(ps_ids))})", ps_ids).rowcount
        nodes = [{"node_id": r[0], "notes": r[1]} for r in
                 con.execute("SELECT node_id, notes FROM wwn_nodes WHERE notes LIKE ?", (f"%{MARKER}%",))]
        for nid in sorted(stale_node_ids(ps_ids, nodes)):
            ref = con.execute("SELECT 1 FROM wwn_rising_mains WHERE discharge_node_id=? "
                              "UNION SELECT 1 FROM wwn_pump_stations WHERE wet_well_node_id=? "
                              "OR discharge_node_id=?", (nid, nid, nid)).fetchone()
            if not ref:
                stats["deleted_nodes"] += con.execute("DELETE FROM wwn_nodes WHERE node_id=?", (nid,)).rowcount
        for n in res.new_nodes:
            con.execute("INSERT OR IGNORE INTO wwn_nodes (geom, node_id, node_type, status, data_source, notes) "
                        "VALUES (?,?,?,?,?,?)", (point_blob(n["xy"]), n["node_id"], n["node_type"], "existing",
                                                 "gis", n["notes"]))
        for nid, nt in res.retype_nodes.items():
            con.execute("UPDATE wwn_nodes SET node_type=? WHERE node_id=?", (nt, nid))
        for rm in res.rising_mains:
            con.execute("INSERT INTO wwn_rising_mains (geom, rm_id, ps_id, discharge_node_id, dn, "
                        "internal_diameter_mm, material, roughness_type, roughness, length_m, profile_json, "
                        "status, data_source, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (line_blob(rm["coords"]), rm["rm_id"], rm["ps_id"], rm["discharge_node_id"], rm["dn"],
                         rm["internal_diameter_mm"], rm["material"], "ks", None, rm["length_m"], None,
                         "existing", "gis", rm["notes"]))
        for p in res.pump_stations:
            old = kept.get(p["ps_id"], {})
            vals = [old.get(k) for k in _HYD]
            con.execute("INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id, discharge_node_id, "
                        f"rising_main_id, {', '.join(_HYD)}, status, data_source, notes) "
                        f"VALUES (?,?,?,?,?,{q(len(_HYD))},?,?,?)",
                        [point_blob(p["xy"]), p["ps_id"], p["wet_well_node_id"], p["discharge_node_id"],
                         p["rising_main_id"]] + vals + ["existing", "gis", p["notes"]])
        for table in ("wwn_nodes", "wwn_pump_stations", "wwn_rising_mains"):
            _update_extent(con, table)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return stats


def _update_extent(con, table: str):
    xs, ys = [], []
    for (g,) in con.execute(f"SELECT geom FROM {table}"):
        if not g:
            continue
        env = (g[3] >> 1) & 7
        if env in (1, 2, 3):
            x0, x1, y0, y1 = struct.unpack("<4d", g[8:40])
            xs += [x0, x1]
            ys += [y0, y1]
        else:
            xy = blob_xy(g)
            if xy:
                xs.append(xy[0])
                ys.append(xy[1])
    if xs:
        con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, "
                    "last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
                    (min(xs), min(ys), max(xs), max(ys), table))


def missing_hydraulic(path: str, ps_ids) -> list:
    """ps_ids (of `ps_ids`) lacking wet_well_area_m2 / start_level / stop_level / pump_rate_lps and with no
    wwn_pumps row carrying a curve."""
    ps_ids = list(ps_ids)
    if not ps_ids:
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = []
        for pid, a, s0, s1, rate in con.execute(
                "SELECT ps_id, wet_well_area_m2, start_level, stop_level, pump_rate_lps FROM wwn_pump_stations "
                f"WHERE ps_id IN ({','.join('?' * len(ps_ids))}) ORDER BY ps_id", ps_ids):
            curve = con.execute("SELECT 1 FROM wwn_pumps WHERE ps_id=? AND curve_json IS NOT NULL "
                                "AND curve_json<>''", (pid,)).fetchone()
            if a is None or s0 is None or s1 is None or (rate is None and not curve):
                out.append(pid)
        return out
    finally:
        con.close()


def model_has_tables(path: str) -> bool:
    try:
        present = gpkg.contents(path)
    except sqlite3.Error:
        return False
    return "wwn_nodes" in present and "wwn_conduits" in present


# ------------------------------------------------------------------ arcpy reading

def read_sources(arcpy, msgs, ps_layer, main_layer, roles_fields: dict, ext, use_selected: bool, opts: Options):
    """(ps rows, main rows) as role-keyed rows. Selection honoured (when `use_selected` and something is
    selected); else a spatial filter on the model extent (pump stations: grown by the search distance; mains:
    plus the pump station extent and GRAVITY_READ_BUFFER_M). Never a full scan unless the installed arcpy
    cannot filter (warned)."""
    si = sewer_import
    out = {}
    ps_roles, main_roles = roles_for("ps", roles_fields), roles_for("main", roles_fields)
    for key, layer, roles, kind, label in (("ps", ps_layer, ps_roles, "point", "Pump stations"),
                                           ("main", main_layer, main_roles, "line", "Pressure mains")):
        rows = None
        if use_selected and si.selection_count(layer):
            rows = si.read_features(arcpy, layer, roles, kind, label, True, msgs)
        else:
            if use_selected:
                msgs.info(f"{label}: nothing selected; reading the features within the model area.")
            e = ext if key == "ps" else out.get("main_ext", ext)
            if e is not None:
                rows = si.read_in_extent(arcpy, layer, roles, kind, label, e, msgs)
                if rows is not None:
                    msgs.info(f"{label}: read {len(rows)} feature(s) within the model extent (spatial filter).")
            if rows is None:
                msgs.warn(f"{label}: reading ALL features (no spatial filter available); this can be slow.")
                rows = si.read_features(arcpy, layer, roles, kind, label, False, msgs)
        out[key] = normalise_rows(rows, roles)
        if key == "ps":
            pe = si.rows_extent(rows, 0.0)
            base = [e for e in (ext, pe) if e is not None]
            if base:
                b = GRAVITY_READ_BUFFER_M
                out["main_ext"] = (min(e[0] for e in base) - b, min(e[1] for e in base) - b,
                                   max(e[2] for e in base) + b, max(e[3] for e in base) + b)
    return out["ps"], out["main"]
