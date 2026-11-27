"""Model repair: join dangling sewer ends to the nearest sewer (sqlite3 only; no qgis / arcpy imports).

A *dangling* node is a junction / is_end / io_end that receives at least one (non-abandoned) conduit and has
no outgoing conduit. Real terminal chambers (ac, ms, drop_ac, outfall, wet_well) are never touched. When
such a node lies close to the mid-line of another sewer (a T-junction the importer's tee tolerance missed)
the main is split there, a tee node is created and the branch is re-pointed to it.

Public API
    find_dangling_joins(gpkg, max_distance_m=5.0) -> list[dict]     read-only plan (joins and skipped nodes)
    apply_joins(gpkg, joins) -> dict                                writes the plan in one transaction
    join_dangling_ends(gpkg, max_distance_m=5.0, preview=False)     find (+ apply unless preview)

Geometry blobs are read / written directly (GeoPackage header + little-endian WKB), byte-for-byte like
crates/wwnet-core/src/gpkg.rs: points without envelope, line strings with an XY envelope, srs from
gpkg_geometry_columns (7850 by default).
"""
from __future__ import annotations

import math
import sqlite3
import struct

DANGLING_TYPES = ("junction", "is_end", "io_end")
JOIN_NODE_TOL_M = 0.5            # projection this close to an end node of the main: join that node, no split
DEFAULT_SRS = 7850
_ENV_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
_NODE_REF_TABLES = (("wwn_node_loads", "node_id"), ("wwn_pump_stations", "wet_well_node_id"),
                    ("wwn_pump_stations", "discharge_node_id"), ("wwn_rising_mains", "discharge_node_id"))


class RepairError(RuntimeError):
    """The model cannot be repaired (clear, user-facing message)."""


# ---------------------------------------------------------------- geometry blobs

def _header(srs: int, env=None) -> bytes:
    flags = 1 | (1 << 1 if env else 0)
    out = b"GP" + bytes([0, flags]) + struct.pack("<i", srs)
    if env:
        minx, miny, maxx, maxy = env
        out += struct.pack("<4d", minx, maxx, miny, maxy)
    return out


def point_blob(xy, srs: int = DEFAULT_SRS) -> bytes:
    return _header(srs) + b"\x01" + struct.pack("<I", 1) + struct.pack("<2d", float(xy[0]), float(xy[1]))


def line_blob(pts, srs: int = DEFAULT_SRS) -> bytes:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    env = (min(xs), min(ys), max(xs), max(ys))
    body = b"\x01" + struct.pack("<I", 2) + struct.pack("<I", len(pts))
    body += b"".join(struct.pack("<2d", float(p[0]), float(p[1])) for p in pts)
    return _header(srs, env) + body


def decode_blob(blob):
    """GeoPackage blob -> ('point', (x, y)) | ('line', [(x, y), ...]) | None (empty / other type)."""
    if not blob or len(blob) < 8 or blob[:2] != b"GP":
        return None
    flags = blob[3]
    if flags & 0x10:                                     # empty geometry
        return None
    pos = 8 + _ENV_BYTES.get((flags >> 1) & 7, 0)
    wle = blob[pos] == 1
    e = "<" if wle else ">"
    ty = struct.unpack_from(e + "I", blob, pos + 1)[0]
    pos += 5
    base, dims = ty % 1000, 2 + (1 if (ty // 1000) in (1, 3) else 0) + (1 if (ty // 1000) in (2, 3) else 0)
    if base == 1:
        v = struct.unpack_from(e + f"{dims}d", blob, pos)
        return "point", (v[0], v[1])
    if base == 2:
        n = struct.unpack_from(e + "I", blob, pos)[0]
        pos += 4
        pts = []
        for _ in range(n):
            v = struct.unpack_from(e + f"{dims}d", blob, pos)
            pts.append((v[0], v[1]))
            pos += 8 * dims
        return "line", pts
    return None


def _srs_of(con, table: str) -> int:
    r = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?", (table,)).fetchone()
    return int(r[0]) if r and r[0] is not None else DEFAULT_SRS


# ---------------------------------------------------------------- small geometry helpers

def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _length(pts) -> float:
    return sum(_dist(a, b) for a, b in zip(pts, pts[1:]))


def _project(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    q = (a[0] + t * dx, a[1] + t * dy)
    return _dist(p, q), t, q


def _nearest_on_line(p, pts):
    """(distance, chainage along pts, projected point, segment index)."""
    best, cum = None, 0.0
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        d, t, q = _project(p, a, b)
        seg = _dist(a, b)
        if best is None or d < best[0] - 1e-12:
            best = (d, cum + t * seg, q, k)
        cum += seg
    return best


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or _dist(out[-1], p) > 1e-9:
            out.append((p[0], p[1]))
    return out


def _fmt(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


# ---------------------------------------------------------------- model state

class _State:
    """Nodes and conduits of the model, in memory, with change tracking."""

    def __init__(self, con):
        self.con = con
        self.srs = {"wwn_nodes": _srs_of(con, "wwn_nodes"), "wwn_conduits": _srs_of(con, "wwn_conduits")}
        con.row_factory = sqlite3.Row
        self.node_cols = [r[1] for r in con.execute("PRAGMA table_info(wwn_nodes)")]
        self.cond_cols = [r[1] for r in con.execute("PRAGMA table_info(wwn_conduits)")]
        self.nodes: dict = {}
        for r in con.execute("SELECT * FROM wwn_nodes ORDER BY fid"):
            d = dict(r)
            g = decode_blob(d.get("geom"))
            d["xy"] = g[1] if g and g[0] == "point" else None
            self.nodes[d["node_id"]] = d
        self.conduits: dict = {}
        for r in con.execute("SELECT * FROM wwn_conduits ORDER BY fid"):
            d = dict(r)
            g = decode_blob(d.get("geom"))
            d["pts"] = list(g[1]) if g and g[0] == "line" and len(g[1]) >= 2 else None
            self.conduits[d["conduit_id"]] = d
        con.row_factory = None
        self.new_nodes: list = []
        self.new_conduits: list = []
        self.dirty_conduits: set = set()
        self.deleted_nodes: set = set()
        self.repoint_refs: dict = {}            # old node id -> new node id (other tables)

    # ----- queries
    def live(self, c) -> bool:
        return (c.get("status") or "") != "abandoned"

    def incoming(self, nid, live_only=True):
        return [c for c in self.conduits.values() if c["ds_node_id"] == nid and (self.live(c) or not live_only)]

    def outgoing(self, nid, live_only=True):
        return [c for c in self.conduits.values() if c["us_node_id"] == nid and (self.live(c) or not live_only)]

    def dangling_ids(self) -> list:
        has_in, has_out = set(), set()
        for c in self.conduits.values():
            if self.live(c):
                has_in.add(c["ds_node_id"])
                has_out.add(c["us_node_id"])
        return sorted(nid for nid, n in self.nodes.items()
                      if n.get("node_type") in DANGLING_TYPES and nid in has_in and nid not in has_out)

    def unique_node_id(self, base):
        nid, k = base, 1
        while nid in self.nodes:
            k += 1
            nid = f"{base}_{k}"
        return nid

    def unique_conduit_id(self, base):
        cid, k = f"{base}~t", 1
        while cid in self.conduits:
            k += 1
            cid = f"{base}~t{k}"
        return cid


def _oriented(st: _State, c):
    """Conduit vertices ordered US -> DS (the digitised order is reversed when it starts at the DS node)."""
    pts = c["pts"]
    u, d = st.nodes.get(c["us_node_id"]), st.nodes.get(c["ds_node_id"])
    if u and d and u["xy"] and d["xy"] and _dist(pts[0], d["xy"]) < _dist(pts[0], u["xy"]):
        return pts[::-1], True
    return list(pts), False


def _grade_invert(c, f):
    ui, di = c.get("us_invert"), c.get("ds_invert")
    if ui is None or di is None:
        return None
    return ui - (ui - di) * f


# ---------------------------------------------------------------- planning

def _upstream_nodes(st: _State, nid: str) -> set:
    """Nodes upstream of nid (including nid): reverse walk over live conduits (one pass to index them)."""
    ups: dict = {}
    for c in st.conduits.values():
        if st.live(c):
            ups.setdefault(c["ds_node_id"], []).append(c["us_node_id"])
    seen, todo = {nid}, [nid]
    while todo:
        for u in ups.get(todo.pop(), ()):
            if u not in seen:
                seen.add(u)
                todo.append(u)
    return seen


def _end_candidates(st: _State, main, q, o) -> list:
    """End nodes of the main that the projection q is next to (distance, node), for the no-split join."""
    us, ds = st.nodes.get(main["us_node_id"]), st.nodes.get(main["ds_node_id"])
    cand = []
    for nd, vtx in ((us, o[0]), (ds, o[-1])):
        if nd is None:
            continue
        dn = min(_dist(q, nd["xy"]) if nd["xy"] else math.inf, _dist(q, vtx))
        if dn <= JOIN_NODE_TOL_M:
            cand.append((dn, nd))
    return cand


def _makes_loop(st: _State, cd, upstream: set) -> bool:
    """True when joining the dangling end to this candidate main would close a loop through it."""
    _d, mid, _s, q, _k, o = cd
    main = st.conduits[mid]
    cand = _end_candidates(st, main, q, o)
    if cand:
        return min(cand, key=lambda x: x[0])[1]["node_id"] in upstream
    return main["ds_node_id"] in upstream


def _plan(st: _State, nid: str, max_distance_m: float, force_main=None) -> dict:
    """Plan the join of one dangling node against the current state. Returns a join dict or a skip dict."""
    node = st.nodes[nid]
    branches = [c["conduit_id"] for c in st.incoming(nid, live_only=False)]
    skip = {"action": "skip", "dangling_node": nid, "node_type": node.get("node_type"), "branch_conduits": branches,
            "main_conduit": None, "distance_m": None, "nearest_conduit": None, "nearest_distance_m": None,
            "reason": "", "warnings": []}
    p = node["xy"]
    if p is None:
        skip["reason"] = "node has no point geometry"
        return skip
    upstream = _upstream_nodes(st, nid)
    cands = []
    for cid, c in st.conduits.items():
        if nid in (c["us_node_id"], c["ds_node_id"]) or not st.live(c) or not c["pts"]:
            continue
        if force_main is not None and cid != force_main:
            continue
        o, _rev = _oriented(st, c)
        d, s, q, k = _nearest_on_line(p, o)
        cands.append((d, cid, s, q, k, o))
    if not cands:
        skip["reason"] = "no other sewer in the model"
        return skip
    cands.sort(key=lambda x: (x[0], x[1]))
    skip["nearest_conduit"], skip["nearest_distance_m"] = cands[0][1], round(cands[0][0], 3)
    best, first_loop = None, None
    for cd in cands:
        if force_main is None and cd[0] > max_distance_m:
            break
        if _makes_loop(st, cd, upstream):
            if first_loop is None:
                first_loop = cd
            continue
        best = cd
        break
    if best is None:
        if first_loop is not None:
            ld, lid = first_loop[0], first_loop[1]
            if force_main is not None:
                skip["reason"] = (f"main {lid} drains into this end (joining would make a loop)")
            else:
                skip["reason"] = (f"nearest sewer {lid} at {ld:.2f} m drains into this end "
                                  f"(joining would make a loop); no other sewer within {max_distance_m:g} m")
        else:
            d0, m0 = cands[0][0], cands[0][1]
            skip["reason"] = (f"no sewer within {max_distance_m:g} m: nearest is {m0} at {d0:.2f} m "
                              "(increase the maximum distance or fix the geometry)")
        return skip
    d, mid, s, q, k, o = best

    main = st.conduits[mid]
    us, ds = st.nodes.get(main["us_node_id"]), st.nodes.get(main["ds_node_id"])
    glen = _length(o)
    warnings: list = []
    join = {"action": "split", "dangling_node": nid, "node_type": node.get("node_type"), "branch_conduits": branches,
            "main_conduit": mid, "distance_m": round(d, 3), "chainage_m": round(s, 3), "tee_node": None,
            "new_conduit": None, "invert_at_tee": None, "surface_at_tee": None, "warnings": warnings,
            "point": q, "note": ""}

    cand = _end_candidates(st, main, q, o)
    if cand:
        nd = min(cand, key=lambda x: x[0])[1]
        inv = nd.get("invert_level")
        if inv is None:
            inv = main.get("us_invert") if nd is us else main.get("ds_invert")
        join.update(action="join_node", tee_node=nd["node_id"], invert_at_tee=inv,
                    surface_at_tee=nd.get("surface_level"), point=nd["xy"] or q,
                    note=f"joined to existing node {nd['node_id']} (end of {mid}), no split")
    else:
        f = s / glen if glen > 0 else 0.0
        inv = _grade_invert(main, f)
        sl = None
        if us and ds and us.get("surface_level") is not None and ds.get("surface_level") is not None:
            sl = us["surface_level"] + (ds["surface_level"] - us["surface_level"]) * f
        join.update(invert_at_tee=None if inv is None else round(inv, 6),
                    surface_at_tee=None if sl is None else round(sl, 6), fraction=f, segment=k,
                    note=f"tee inserted on {mid} at chainage {s:.2f} m")
        if inv is None:
            warnings.append(f"main {mid} has no us / ds invert: invert at the tee left blank")
    for bid in branches:
        b = st.conduits[bid]
        bi = b.get("ds_invert")
        ti = join["invert_at_tee"]
        if bi is not None and ti is not None and bi < ti - 1e-9:
            warnings.append(f"branch {bid} arrives below the main invert by {ti - bi:.3f} m "
                            "- check levels")
    return join


# ---------------------------------------------------------------- committing a plan into the state

def _mark(st, c):
    st.dirty_conduits.add(c["conduit_id"])


def _commit(st: _State, j: dict):
    nid, mid = j["dangling_node"], j["main_conduit"]
    node, main = st.nodes[nid], st.conduits[mid]
    q = tuple(j["point"])
    if j["action"] == "join_node":
        tee = j["tee_node"]
    else:
        tee = st.unique_node_id("T_" + nid)
        j["tee_node"] = tee
        o, _rev = _oriented(st, main)
        k, f = j["segment"], j["fraction"]
        first = _dedupe(o[:k + 1] + [q])
        second = _dedupe([q] + o[k + 1:])
        orig_len, ds_inv, ds_node = main.get("length_m"), main.get("ds_invert"), main["ds_node_id"]
        branch_txt = ", ".join(j["branch_conduits"])
        new_cid = st.unique_conduit_id(mid)
        new = {c: main.get(c) for c in st.cond_cols if c != "fid"}
        new.update(conduit_id=new_cid, us_node_id=tee, ds_node_id=ds_node, us_invert=j["invert_at_tee"],
                   ds_invert=ds_inv, notes=f"split at tee for {branch_txt}", pts=second, geom=None)
        new["length_m"] = None if orig_len is None else orig_len - orig_len * f
        main["pts"], main["ds_node_id"], main["ds_invert"] = first, tee, j["invert_at_tee"]
        if orig_len is not None:
            main["length_m"] = orig_len * f
        note = f"split at tee for {branch_txt}"
        main["notes"] = f"{main['notes']}; {note}" if main.get("notes") else note
        st.conduits[new_cid] = new
        st.new_conduits.append(new_cid)
        _mark(st, new)
        _mark(st, main)
        tn = {c: None for c in st.node_cols if c != "fid"}
        tn.update(node_id=tee, node_type="junction", status=main.get("status"),
                  data_source=main.get("data_source"), invert_level=j["invert_at_tee"],
                  surface_level=j["surface_at_tee"], xy=q,
                  notes=f"tee inserted by Join Dangling Sewer Ends (d = {j['distance_m']:.2f} m from {nid})")
        for c in ("status", "data_source"):
            if tn[c] is None:
                tn[c] = "gis" if c == "data_source" else "existing"
        st.nodes[tee] = tn
        st.new_nodes.append(tee)
        j["new_conduit"] = new_cid
    # re-point the branches and move their end vertex to the tee
    j["branch_changes"] = []
    tpt = st.nodes[tee]["xy"] or q
    for bid in j["branch_conduits"]:
        b = st.conduits[bid]
        b["ds_node_id"] = tee
        if b["pts"]:
            pts = list(b["pts"])
            ie = len(pts) - 1 if (not node["xy"] or _dist(pts[-1], node["xy"]) <= _dist(pts[0], node["xy"])) else 0
            gap = _dist(pts[ie], tpt)
            pts[ie] = (tpt[0], tpt[1])
            b["pts"] = _dedupe(pts) if len(_dedupe(pts)) >= 2 else pts
            if b.get("length_m") is not None and gap > 0:
                b["length_m"] = b["length_m"] + gap
                j["branch_changes"].append({"conduit": bid, "gap_m": round(gap, 3),
                                            "length_m": round(b["length_m"], 3)})
        _mark(st, b)
    # the dangling node goes when nothing refers to it any more
    if not any(nid in (c["us_node_id"], c["ds_node_id"]) for c in st.conduits.values()):
        st.deleted_nodes.add(nid)
        st.repoint_refs[nid] = tee
        del st.nodes[nid]
        j["node_deleted"] = True
    else:
        j["node_deleted"] = False


# ---------------------------------------------------------------- public API

def _connect(gpkg):
    con = sqlite3.connect(str(gpkg), timeout=2.0)
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'rtree_wwn_%'")]
    if names:
        con.close()
        raise RepairError(
            "The GeoPackage has spatial index (R-tree) tables (" + ", ".join(sorted(names)[:3]) +
            "...). Editing geometry here would leave them out of date: open a model created by WWNet "
            "(Import Sewer Data / New Model) or remove the spatial index, then run this tool again.")
    for t in ("wwn_nodes", "wwn_conduits"):
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            con.close()
            raise RepairError(f"{gpkg} is not a WWNet model (no {t} table)")
    return con


def _strip(j: dict) -> dict:
    out = dict(j)
    out.pop("point", None)
    out.pop("fraction", None)
    out.pop("segment", None)
    return out


def find_dangling_joins(gpkg, max_distance_m: float = 5.0) -> list:
    """Plan every dangling-end join (read-only). One dict per dangling node, in node id order:
    action 'split' (tee inserted on the main) | 'join_node' (re-pointed to an existing end node of the main)
    | 'skip' (reason, nearest conduit and distance). Later plans see the earlier ones (two ends on the same
    main split it in turn), so applying the list reproduces the plan exactly."""
    con = _connect(gpkg)
    try:
        st = _State(con)
        out = []
        for nid in st.dangling_ids():
            j = _plan(st, nid, float(max_distance_m))
            if j["action"] != "skip":
                _commit(st, j)
            out.append(j)
        return [_strip(j) if j["action"] != "skip" else j for j in out]
    finally:
        con.close()


def _update_extents(con, st: _State):
    for table, rows in (("wwn_nodes", [n["xy"] for n in st.nodes.values()]),
                        ("wwn_conduits", [p for c in st.conduits.values() for p in (c["pts"] or [])])):
        pts = [p for p in rows if p]
        if not pts:
            continue
        con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, "
                    "last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
                    (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                     max(p[1] for p in pts), table))


def apply_joins(gpkg, joins) -> dict:
    """Write the joins from find_dangling_joins in one transaction (rollback on any error).
    Returns {'joined': [...], 'skipped': [...], 'nodes_added', 'nodes_deleted', 'conduits_added',
    'conduits_modified'}. A join whose dangling node is no longer dangling is skipped with a reason."""
    con = _connect(gpkg)
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            st = _State(con)
            joined, skipped = [], []
            dang = set(st.dangling_ids())
            for j in joins:
                if j.get("action") == "skip":
                    skipped.append(j)
                    continue
                nid = j["dangling_node"]
                if nid not in dang or j.get("main_conduit") not in st.conduits:
                    skipped.append({"action": "skip", "dangling_node": nid, "reason":
                                    "model changed since the preview (node no longer dangling or main missing)",
                                    "branch_conduits": j.get("branch_conduits", []), "warnings": []})
                    continue
                pj = _plan(st, nid, math.inf, force_main=j["main_conduit"])
                if pj["action"] == "skip":
                    skipped.append(pj)
                    continue
                _commit(st, pj)
                joined.append(_strip(pj))
            _check_no_self_loops(st)
            _write(con, st)
            _update_extents(con, st)
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return {"joined": joined, "skipped": skipped, "nodes_added": len(st.new_nodes),
                "nodes_deleted": len(st.deleted_nodes), "conduits_added": len(st.new_conduits),
                "conduits_modified": len(st.dirty_conduits - set(st.new_conduits))}
    finally:
        con.close()


def _check_no_self_loops(st: _State):
    bad = sorted(cid for cid, c in st.conduits.items() if c["us_node_id"] == c["ds_node_id"])
    if bad:
        raise RuntimeError("Join Dangling Sewer Ends would leave conduits with the same us and ds node: "
                           + ", ".join(bad))


def _write(con, st: _State):
    for tid in st.new_nodes:
        n = st.nodes.get(tid)
        if n is None:
            continue
        cols = [c for c in st.node_cols if c != "fid"]
        vals = [point_blob(n["xy"], st.srs["wwn_nodes"]) if c == "geom" else n.get(c) for c in cols]
        con.execute(f"INSERT INTO wwn_nodes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    cols = [c for c in st.cond_cols if c not in ("fid", "conduit_id")]
    for cid in sorted(st.dirty_conduits, key=lambda x: (x in st.new_conduits, st.new_conduits.index(x) if x in st.new_conduits else 0, x)):
        c = st.conduits[cid]
        vals = [line_blob(c["pts"], st.srs["wwn_conduits"]) if k == "geom" and c["pts"] else c.get(k) for k in cols]
        if cid in st.new_conduits:
            con.execute(f"INSERT INTO wwn_conduits (conduit_id,{','.join(cols)}) "
                        f"VALUES (?,{','.join('?' * len(cols))})", [cid] + vals)
        else:
            con.execute(f"UPDATE wwn_conduits SET {','.join(k + '=?' for k in cols)} WHERE conduit_id=?",
                        vals + [cid])
    for old, new in st.repoint_refs.items():
        for table, col in _NODE_REF_TABLES:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
    for nid in st.deleted_nodes:
        con.execute("DELETE FROM wwn_nodes WHERE node_id=?", (nid,))


def join_dangling_ends(gpkg, max_distance_m: float = 5.0, preview: bool = False) -> dict:
    """Find and (unless preview) apply. Returns {'preview': bool, 'joins': [...planned joins],
    'skipped': [...], 'applied': apply_joins result or None}."""
    plan = find_dangling_joins(gpkg, max_distance_m)
    joins = [j for j in plan if j["action"] != "skip"]
    skipped = [j for j in plan if j["action"] == "skip"]
    applied = None
    if not preview and joins:
        applied = apply_joins(gpkg, joins)
        skipped = skipped + applied["skipped"]
        joins = applied["joined"]
    return {"preview": bool(preview), "joins": joins, "skipped": skipped, "applied": applied}
