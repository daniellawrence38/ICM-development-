"""'Calculate Existing Demand': read Local Planning Scheme zone (DPLH-071) and R-Code (DPLH-070) polygons with
arcpy, run the shared land-use loading (``wwnet_shared.core.demand``) and write the result into the model:
``wwn_subcatchments`` rows ``LU-<node>-<class>`` (the engine loads them in Run Design / Run Simulation) and the
review layer ``wwn_land_use`` (one row per zone polygon: class, R-code, area loaded, ADWF, nodes, flags)."""
from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timezone

from . import sewer_import, shared

LAND_USE_TABLE = "wwn_land_use"
LAND_USE_TITLE = "Land use loading"
SRS = 7850
MAX_LINES = 40

LAND_USE_DDL = f"""CREATE TABLE IF NOT EXISTS {LAND_USE_TABLE} (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom MULTIPOLYGON,
  polygon_id TEXT, zone TEXT, zone_class TEXT, rcode_source TEXT, ds50_rcode TEXT,
  area_ha REAL, loaded_ha REAL, adwf_lps REAL, nodes TEXT, flags TEXT, run_at TEXT)"""


def core():
    return shared.core("demand")


def _repair():
    return shared.core("repair")


# ------------------------------------------------------------------ field defaults

def _key(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def default_zone_field(names) -> str:
    """'Zone' (any case), else a field whose name holds 'zone' and 'name' / 'desc', else 'zone' alone ('' if none).
    Zone number / code fields are not picked."""
    names = list(names)
    keys = {n: _key(n) for n in names}
    for n in names:
        if keys[n] == "zone":
            return n
    for n in names:
        k = keys[n]
        if "zone" in k and ("name" in k or "desc" in k):
            return n
    for n in names:
        k = keys[n]
        if "zone" in k and not any(w in k for w in ("no", "num", "code", "id")):
            return n
    return ""


def default_rcode_field(names) -> str:
    """A field whose name holds 'rcode' ('R code number', RCODE_NO, R_CODE ...), preferring 'number' ('' if none)."""
    names = list(names)
    hits = [n for n in names if "rcode" in _key(n)]
    for n in hits:
        if "num" in _key(n) or _key(n).endswith("no"):
            return n
    return hits[0] if hits else ""


# ------------------------------------------------------------------ reading

def polygon_rings(shape) -> list:
    """Rings [(x, y), ...] of an arcpy Polygon (parts; interior rings follow a None in a part's array)."""
    rings = []
    if shape is None:
        return rings
    for i in range(getattr(shape, "partCount", 0) or 0):
        cur = []
        for p in shape.getPart(i):
            if p is None:
                if cur:
                    rings.append(cur)
                cur = []
            else:
                cur.append((p.X, p.Y))
        if cur:
            rings.append(cur)
    return rings


def _read(arcpy, cursor_src, value_field: str, **kw) -> list:
    out = []
    sr = arcpy.SpatialReference(SRS)
    with arcpy.da.SearchCursor(cursor_src, ["OID@", value_field, "SHAPE@"], spatial_reference=sr, **kw) as cur:
        for i, (oid, value, shape) in enumerate(cur, 1):
            out.append({"id": oid if oid is not None else i, "value": value, "rings": polygon_rings(shape)})
    return out


def read_polygons(arcpy, layer, field_name: str, label: str, ext, use_selected: bool, msgs) -> list:
    """[{id, value, rings}] in EPSG:7850. The layer's selection when `use_selected` and something is selected,
    else the polygons intersecting `ext` (spatial filter); all polygons only when arcpy cannot filter (warned)."""
    si = sewer_import
    names = si.layer_field_names(arcpy, layer)
    actual = {n.lower(): n for n in names}.get((field_name or "").lower())
    if actual is None:
        raise RuntimeError(f"{label}: field {field_name!r} does not exist. Available: {', '.join(names)}")
    if use_selected and si.selection_count(layer):
        rows = _read(arcpy, layer, actual)
        msgs.info(f"{label}: read {len(rows)} selected polygon(s).")
        return rows
    src = si._cursor_source(arcpy, layer, False, msgs, label)
    if ext is not None:
        try:
            rows = _read(arcpy, src, actual, spatial_filter=si.extent_polygon(arcpy, ext),
                         spatial_relationship="INTERSECTS")
            msgs.info(f"{label}: read {len(rows)} polygon(s) intersecting the model area (spatial filter).")
            return rows
        except TypeError:
            msgs.warn(f"{label}: this ArcGIS version cannot filter by location; reading ALL polygons (slow).")
    rows = _read(arcpy, src, actual)
    msgs.info(f"{label}: read {len(rows)} polygon(s).")
    return rows


def rows_bbox(rows: list, grow: float = 0.0):
    xs = [x for r in rows for ring in r["rings"] for x, _y in ring]
    ys = [y for r in rows for ring in r["rings"] for _x, y in ring]
    if not xs:
        return None
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


# ------------------------------------------------------------------ the model

def read_sewers(path: str) -> list:
    """[{conduit_id, us_node_id, coords}] of the model's conduits that are not abandoned (geometry present)."""
    dec = _repair().decode_blob
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = []
        for cid, us, g in con.execute("SELECT conduit_id, us_node_id, geom FROM wwn_conduits "
                                      "WHERE status IS NULL OR status <> 'abandoned'"):
            d = dec(g)
            if d and d[0] == "line" and len(d[1]) >= 2:
                out.append({"conduit_id": cid, "us_node_id": us, "coords": d[1]})
        return out
    finally:
        con.close()


def sewers_bbox(sewers: list, grow: float = 0.0):
    xs = [p[0] for s in sewers for p in s["coords"]]
    ys = [p[1] for s in sewers for p in s["coords"]]
    if not xs:
        return None
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


def existing_loads(path: str) -> dict:
    """{'cp_rows', 'cp_connections', 'cp_adwf_lps', 'other_subcatchments', 'lu_subcatchments'} of the model now."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM wwn_meta").fetchall())
        try:
            per = float(meta.get("adwf_per_connection_lpd") or 630.0)
        except ValueError:
            per = 630.0
        n, conns, adwf = 0, 0, 0.0
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "wwn_node_loads" in tables:
            for c, a in con.execute("SELECT connections, adwf_lps FROM wwn_node_loads WHERE source='connection_points'"):
                n += 1
                conns += int(c or 0)
                adwf += float(a) if a is not None else (c or 0) * per / 86400.0
        prefix = core().ID_PREFIX
        lu = con.execute("SELECT count(*) FROM wwn_subcatchments WHERE subcatchment_id LIKE ?",
                         (prefix + "%",)).fetchone()[0]
        other = con.execute("SELECT count(*) FROM wwn_subcatchments WHERE subcatchment_id NOT LIKE ?",
                            (prefix + "%",)).fetchone()[0]
        return {"cp_rows": n, "cp_connections": conns, "cp_adwf_lps": adwf, "lu_subcatchments": lu,
                "other_subcatchments": other, "per_connection_lpd": per}
    finally:
        con.close()


def _ensure_land_use_table(con):
    con.execute(LAND_USE_DDL)
    con.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description, srs_id) "
                "VALUES (?, 'features', ?, 'Existing demand: zone polygons and their DS 50 loading (Calculate "
                "Existing Demand; rewritten each run)', ?)", (LAND_USE_TABLE, LAND_USE_TITLE, SRS))
    con.execute("INSERT OR IGNORE INTO gpkg_geometry_columns (table_name, column_name, geometry_type_name, srs_id, "
                "z, m) VALUES (?, 'geom', 'MULTIPOLYGON', ?, 0, 0)", (LAND_USE_TABLE, SRS))


def _blob_env(g):
    if not g or len(g) < 40:
        return None
    if (g[3] >> 1) & 7 not in (1, 2, 3):
        return None
    x0, x1, y0, y1 = struct.unpack("<4d", g[8:40])
    return x0, y0, x1, y1


ISSUE_SOURCE = "demand"


def _store_issues(con, issues: list, run_at: str) -> int:
    """Replace the 'demand' rows of wwn_issues (written here: the engine's store_issues only takes its own
    sources). Each issue is placed at its x / y (the polygon's centre)."""
    point = _repair().point_blob
    con.execute("DELETE FROM wwn_issues WHERE source=?", (ISSUE_SOURCE,))
    for i in issues:
        g = point((i["x"], i["y"]), SRS) if i.get("x") is not None and i.get("y") is not None else None
        con.execute("INSERT INTO wwn_issues (run_at, severity, code, object_type, object_id, clause, message, "
                    "source, geom) VALUES (?,?,?,?,?,NULL,?,?,?)",
                    (run_at, i["severity"], i["code"], i["object_type"], i["object_id"], i["message"],
                     ISSUE_SOURCE, g))
    con.execute("UPDATE gpkg_contents SET last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE table_name='wwn_issues'")
    return len(issues)


def write_result(path: str, res, replace_cp_loads: bool = True) -> dict:
    """Replace the earlier land-use sub-catchments (LU-*), the review layer and the 'demand' issues; optionally
    delete the connection point loads (Import Sewer Data's connections x ADWF per connection). One transaction."""
    blob = core().multipolygon_blob
    prefix = core().ID_PREFIX
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = sqlite3.connect(path, timeout=15)
    stats = {"replaced_subcatchments": 0, "deleted_cp_loads": 0, "issues": 0}
    try:
        con.execute("BEGIN IMMEDIATE")
        stats["replaced_subcatchments"] = con.execute(
            "DELETE FROM wwn_subcatchments WHERE subcatchment_id LIKE ?", (prefix + "%",)).rowcount
        if replace_cp_loads:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "wwn_node_loads" in tables:
                stats["deleted_cp_loads"] = con.execute(
                    "DELETE FROM wwn_node_loads WHERE source='connection_points'").rowcount
        for s in res.subcatchments:
            con.execute("INSERT INTO wwn_subcatchments (geom, subcatchment_id, outlet_node_id, net_area_ha, "
                        "gross_area_ha, broadacre, land_use, rcode, ground, region, extra_flow_lps, adwf_lps, "
                        "profile_id, status, notes) VALUES (NULL,?,?,?,NULL,0,?,?,?,?,0,NULL,NULL,?,?)",
                        (s["subcatchment_id"], s["outlet_node_id"], s["net_area_ha"], s["land_use"], s["rcode"],
                         s["ground"], s["region"], s["status"], s["notes"]))
        _ensure_land_use_table(con)
        con.execute(f"DELETE FROM {LAND_USE_TABLE}")
        envs = []
        for r in res.polygons:
            g = blob(r["polygons"], SRS)
            env = _blob_env(g)
            if env:
                envs.append(env)
            con.execute(f"INSERT INTO {LAND_USE_TABLE} (geom, polygon_id, zone, zone_class, rcode_source, "
                        "ds50_rcode, area_ha, loaded_ha, adwf_lps, nodes, flags, run_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (g, r["polygon_id"], r["zone"], r["zone_class"], r["rcode_source"], r["ds50_rcode"],
                         round(r["area_ha"], 4), round(r["loaded_ha"], 4), round(r["adwf_lps"], 5), r["nodes"],
                         r["flags"], run_at))
        if envs:
            con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, "
                        "last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
                        (min(e[0] for e in envs), min(e[1] for e in envs), max(e[2] for e in envs),
                         max(e[3] for e in envs), LAND_USE_TABLE))
        con.execute("UPDATE gpkg_contents SET last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE table_name='wwn_subcatchments'")
        stats["issues"] = _store_issues(con, res.issues, run_at)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return stats
