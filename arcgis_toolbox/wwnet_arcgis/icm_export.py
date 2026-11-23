"""Export a WWNet model to InfoWorks ICM import files (for the Open Data Import Centre).

Pure-Python core (`build`) reads the GeoPackage with sqlite3 and maps it to ICM node / conduit
fields; `write_gdb` (arcpy) writes ICM_Nodes / ICM_Conduits to a file geodatabase (keeps nulls and
vertices); `write_csv` writes the same rows as CSV (nodes with x / y) as a fallback source.
"""
from __future__ import annotations

import csv
import math
import os
import re
import sqlite3
import struct

from . import gpkg

SUFFIX_CHARS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # ICM link_suffix is a single character
DEFAULT_KS_MM = 1.0                                     # WWNet ruleset default when ks_mm is NULL

# WWNet node_type -> ICM node_type. Junctions (IS-sewer tees) and IS ends have no chamber -> Break.
ICM_NODE_TYPE = {"ac": "Manhole", "drop_ac": "Manhole", "ms": "Manhole", "wet_well": "Manhole",
                 "io_end": "Manhole", "junction": "Break", "is_end": "Break", "outfall": "Outfall"}

# (field, arcpy type) in output order. Names follow ICM's internal field names where one exists so
# ODIC can match them; wwn_* fields are extra information (map them to user fields or ignore).
NODE_FIELDS = [("node_id", "TEXT"), ("node_type", "TEXT"), ("ground_level", "DOUBLE"),
               ("flood_level", "DOUBLE"), ("chamber_floor", "DOUBLE"), ("chamber_area", "DOUBLE"),
               ("shaft_area", "DOUBLE"), ("wwn_node_type", "TEXT"), ("wwn_floor_source", "TEXT"),
               ("wwn_ground_source", "TEXT"), ("asset_id", "TEXT"), ("status", "TEXT"), ("notes", "TEXT")]
CONDUIT_FIELDS = [("us_node_id", "TEXT"), ("link_suffix", "TEXT"), ("ds_node_id", "TEXT"),
                  ("us_invert", "DOUBLE"), ("ds_invert", "DOUBLE"), ("conduit_length", "DOUBLE"),
                  ("conduit_shape", "TEXT"), ("conduit_width", "DOUBLE"), ("conduit_height", "DOUBLE"),
                  ("roughness_type", "TEXT"), ("bottom_roughness_CW", "DOUBLE"),
                  ("top_roughness_CW", "DOUBLE"), ("material", "TEXT"), ("wwn_conduit_id", "TEXT"),
                  ("asset_id", "TEXT"), ("status", "TEXT"), ("notes", "TEXT")]

# Names that clash (case-insensitively) with geodatabase system fields -> AddField ERROR 000012.
RESERVED_FIELDS = {"shape", "objectid", "oid", "fid", "shape_length", "shape_area", "globalid"}


def check_reserved_fields():
    bad = [n for n, _ in NODE_FIELDS + CONDUIT_FIELDS if n.lower() in RESERVED_FIELDS]
    if bad:
        raise RuntimeError(f"ICM export field name(s) clash with reserved geodatabase fields: {bad}")


check_reserved_fields()

OUT_SR = {"GDA2020 / MGA zone 50 (EPSG:7850)": 7850, "GDA94 / MGA zone 50 (EPSG:28350)": 28350}


def _line_length(blob):
    """Planar length of a GeoPackage LINESTRING blob (model CRS is metric MGA), or None."""
    if not blob or blob[:2] != b"GP":
        return None
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((blob[3] >> 1) & 7)
    if env is None:
        return None
    wkb = blob[8 + env:]
    bo = "<" if wkb[0] == 1 else ">"
    gtype, n = struct.unpack(bo + "II", wkb[1:9])
    if gtype % 1000 != 2:                       # ISO WKB: 2 / 1002 Z / 2002 M / 3002 ZM
        return None
    dims = (2, 3, 3, 4)[(gtype // 1000) % 4]
    step = 8 * dims
    pts = [struct.unpack(bo + "dd", wkb[9 + step * i:9 + step * i + 16]) for i in range(n)]
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(pts, pts[1:]))


def _rows(con, sql):
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur]


def _r(v, nd=3):
    return None if v is None else round(float(v), nd)


def build(gpkg_path: str) -> dict:
    """{'nodes': [row], 'conduits': [row], 'warnings': [str], 'skipped': {table: count}}.
    Rows are dicts keyed by NODE_FIELDS / CONDUIT_FIELDS names (plus the WWNet id for geometry lookup)."""
    con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        nodes = _rows(con, "SELECT node_id, node_type, surface_level, invert_level, chamber_diameter_mm, "
                           "flood_level, status, asset_id, notes FROM wwn_nodes ORDER BY node_id")
        conduits = _rows(con, "SELECT conduit_id, us_node_id, ds_node_id, dn, internal_diameter_mm, material, "
                              "us_invert, ds_invert, length_m, ks_mm, status, asset_id, notes, geom "
                              "FROM wwn_conduits WHERE status <> 'abandoned' ORDER BY us_node_id, conduit_id")
        present = gpkg.contents(gpkg_path)
        skipped = {}
        for t in ("wwn_pump_stations", "wwn_rising_mains", "wwn_subcatchments"):
            if t in present:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if n:
                    skipped[t] = n
    finally:
        con.close()

    warnings = []
    node_ids = {n["node_id"] for n in nodes}

    # lowest connected invert, as a chamber-floor fallback
    low = {}
    for c in conduits:
        for nid, inv in ((c["us_node_id"], c["us_invert"]), (c["ds_node_id"], c["ds_invert"])):
            if inv is not None:
                low[nid] = min(inv, low.get(nid, inv))

    out_nodes, no_floor, no_ground = [], [], []
    for n in nodes:
        floor, src = n["invert_level"], "invert_level"
        if floor is None:
            floor, src = low.get(n["node_id"]), "lowest connected invert"
            if floor is None:
                src = ""
                no_floor.append(n["node_id"])
        if n["surface_level"] is None:
            no_ground.append(n["node_id"])
        d = n["chamber_diameter_mm"]
        area = _r(math.pi * (d / 1000.0) ** 2 / 4.0, 4) if d else None
        out_nodes.append({
            "node_id": n["node_id"], "node_type": ICM_NODE_TYPE.get(n["node_type"], "Manhole"),
            "ground_level": _r(n["surface_level"]), "flood_level": _r(n["flood_level"]),
            "chamber_floor": _r(floor), "chamber_area": area, "shaft_area": area,
            "wwn_node_type": n["node_type"], "wwn_floor_source": src,
            "wwn_ground_source": "model" if n["surface_level"] is not None else "",
            "asset_id": n["asset_id"], "status": n["status"], "notes": n["notes"]})
    n_fallback = sum(1 for n in out_nodes if n["wwn_floor_source"] == "lowest connected invert")
    if n_fallback:
        warnings.append(f"{n_fallback} node(s) have no invert level: chamber_floor set to the lowest connected "
                        "conduit invert (wwn_floor_source says which).")
    if no_floor:
        warnings.append(f"{len(no_floor)} node(s) have no invert level and no connected invert; chamber_floor "
                        "left blank: " + ", ".join(no_floor[:20]) + (" ..." if len(no_floor) > 20 else ""))
    if no_ground:
        warnings.append(f"{len(no_ground)} node(s) have no surface level; ground_level left blank: "
                        + ", ".join(no_ground[:20]) + (" ..." if len(no_ground) > 20 else ""))

    out_links, per_us, missing_inv, missing_node = [], {}, [], []
    for c in conduits:
        k = per_us.get(c["us_node_id"], 0)
        if k >= len(SUFFIX_CHARS):
            raise RuntimeError(f"More than {len(SUFFIX_CHARS)} conduits leave node {c['us_node_id']}; "
                               "ICM link suffixes are single characters.")
        per_us[c["us_node_id"]] = k + 1
        if c["us_invert"] is None or c["ds_invert"] is None:
            missing_inv.append(c["conduit_id"])
        for nid in (c["us_node_id"], c["ds_node_id"]):
            if nid not in node_ids:
                missing_node.append(f"{c['conduit_id']} -> {nid}")
        dia = c["internal_diameter_mm"] or c["dn"]
        length = c["length_m"] if c["length_m"] is not None else _line_length(c["geom"])
        ks = c["ks_mm"] if c["ks_mm"] is not None else DEFAULT_KS_MM
        out_links.append({
            "us_node_id": c["us_node_id"], "link_suffix": SUFFIX_CHARS[k], "ds_node_id": c["ds_node_id"],
            "us_invert": _r(c["us_invert"]), "ds_invert": _r(c["ds_invert"]), "conduit_length": _r(length, 2),
            "conduit_shape": "CIRC", "conduit_width": _r(dia, 1), "conduit_height": _r(dia, 1),
            "roughness_type": "CW", "bottom_roughness_CW": ks, "top_roughness_CW": ks,
            "material": (c["material"] or "").upper() or None, "wwn_conduit_id": c["conduit_id"],
            "asset_id": c["asset_id"], "status": c["status"], "notes": c["notes"]})
    multi = sum(1 for v in per_us.values() if v > 1)
    if multi:
        warnings.append(f"{multi} node(s) have more than one outgoing conduit: link suffixes 1, 2, 3 ... assigned "
                        "in conduit-id order.")
    if missing_inv:
        warnings.append(f"{len(missing_inv)} conduit(s) are missing an invert level: "
                        + ", ".join(missing_inv[:20]) + (" ..." if len(missing_inv) > 20 else ""))
    if missing_node:
        warnings.append(f"{len(missing_node)} conduit end(s) reference a node that doesn't exist (ICM will reject "
                        "them): " + ", ".join(missing_node[:20]))
    for t, n in skipped.items():
        warnings.append(f"{t}: {n} row(s) NOT exported (pressure side / sub-catchments not supported yet).")
    return {"nodes": out_nodes, "conduits": out_links, "warnings": warnings, "skipped": skipped}


def safe_stem(gpkg_path: str) -> str:
    stem = os.path.splitext(os.path.basename(gpkg_path))[0]
    s = re.sub(r"\W", "_", stem) or "model"
    return s if not s[0].isdigit() else "m_" + s


def write_csv(path: str, fields: list, rows: list, xy: dict | None = None, id_key: str = "node_id") -> None:
    names = [f for f, _ in fields] + (["x", "y"] if xy is not None else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(names)
        for r in rows:
            vals = ["" if r.get(f) is None else r.get(f) for f, _ in fields]
            if xy is not None:
                p = xy.get(r[id_key])
                vals += ["", ""] if p is None else [round(p[0], 3), round(p[1], 3)]
            w.writerow(vals)


def notes_text(gpkg_path: str, out_sr_label: str, gdb: str, nodes_csv: str, conduits_csv: str,
               warnings: list) -> str:
    lines = [
        f"WWNet -> InfoWorks ICM export of {gpkg_path}",
        f"Coordinate system: {out_sr_label}",
        "",
        "Files",
        f"  {gdb}\\ICM_Nodes, ICM_Conduits   (preferred: keeps blank values and pipe vertices)",
        f"  {nodes_csv}   (fallback: x / y columns)",
        f"  {conduits_csv}   (fallback: no geometry; ICM draws straight links between nodes)",
        "",
        "ICM: File > Import > InfoWorks > Open Data Import Centre. Import NODES FIRST, then conduits.",
        "Field names already match ICM's where one exists; check each in the ODIC drop-downs.",
        "",
        "Node table",
        "  node_id -> Node ID | node_type -> Node type (Manhole / Break / Outfall)",
        "  ground_level -> Ground level | flood_level -> Flood level | chamber_floor -> Chamber floor level",
        "  chamber_area / shaft_area -> Chamber area / Shaft area (m2, from chamber_diameter_mm; blank = ICM default)",
        "Conduit table",
        "  us_node_id + link_suffix -> US node ID + Link suffix (unique per US node) | ds_node_id -> DS node ID",
        "  us_invert / ds_invert -> US / DS invert level | conduit_length -> Length (m)",
        "  conduit_shape = CIRC -> Shape | conduit_width / conduit_height -> Width / Height (mm; internal diameter, else DN)",
        "  roughness_type = CW | bottom/top_roughness_CW -> Colebrook-White ks (mm)",
        "  wwn_conduit_id / asset_id -> a user text field or Asset ID (optional)",
        "",
        "Not exported: pump stations, rising mains, sub-catchments / loads, diurnal profiles.",
    ]
    if warnings:
        lines += ["", "Warnings"] + [f"  - {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def write_gdb(arcpy, gdb: str, table: str, geom_type: str, fields: list, rows: list, key: str,
              src_ds: str, src_key: str, sr) -> dict:
    """Create <gdb>\\<table> (replacing it) and fill it with `rows`, taking geometry from `src_ds`
    (projected by the cursor to `sr`). Returns {id: (x, y)} for POINT tables (else {})."""
    fc = os.path.join(gdb, table)
    if arcpy.Exists(fc):
        arcpy.management.Delete(fc)
    arcpy.management.CreateFeatureclass(gdb, table, geom_type, spatial_reference=sr)
    desc = [[name, typ, "", (255 if name == "notes" else 64) if typ == "TEXT" else ""] for name, typ in fields]
    try:
        add_fields = arcpy.management.AddFields
    except AttributeError:                      # older arcpy: one call per field (slow)
        add_fields = None
    if add_fields is not None:
        add_fields(fc, desc)
    else:
        for name, typ in fields:
            if typ == "TEXT":
                arcpy.management.AddField(fc, name, "TEXT", field_length=255 if name == "notes" else 64)
            else:
                arcpy.management.AddField(fc, name, typ)
    by_id = {r[key]: r for r in rows}
    cols = [f for f, _ in fields] + ["SHAPE@"]
    xy = {}
    src_fields = [src_key, "SHAPE@"] + (["SHAPE@XY"] if geom_type == "POINT" else [])
    with arcpy.da.SearchCursor(src_ds, src_fields, spatial_reference=sr) as src, \
            arcpy.da.InsertCursor(fc, cols) as ins:
        for rec in src:
            r = by_id.get(rec[0])
            if r is None:
                continue
            ins.insertRow([r.get(f) for f, _ in fields] + [rec[1]])
            if geom_type == "POINT" and rec[2] is not None:
                xy[rec[0]] = rec[2]
    return xy


def _cell_value(arcpy, dem, x: float, y: float):
    """DEM value at (x, y) in the DEM's SR, or None for NoData. RasterToNumPyArray (1 x 1 window,
    not a geoprocessing tool, so fast); falls back to GetCellValue (a GP tool: seconds per call)."""
    try:
        arr = arcpy.RasterToNumPyArray(dem, arcpy.Point(x, y), 1, 1, float("nan"))
        v = float(arr.flat[0])
        return None if v != v else round(v, 3)      # NaN -> NoData
    except Exception:  # noqa: BLE001 - unsupported source: use the GP tool
        pass
    try:
        txt = str(arcpy.management.GetCellValue(dem, f"{x} {y}").getOutput(0)).strip()
        if txt.lower() == "nodata":
            return None
        return round(float(txt.replace(",", ".")), 3)
    except Exception:  # noqa: BLE001
        return None


def fill_ground_from_dem(arcpy, msgs, nodes: list, nodes_ds: str, dem: str):
    """Fill blank ground_level of `nodes` (dicts, edited in place) from a DEM via GetCellValue (no
    Spatial Analyst needed). Returns (filled count, [node ids with NoData / unreadable cell])."""
    from . import sewer_import
    todo = {n["node_id"]: n for n in nodes if n.get("ground_level") is None}
    if not todo:
        return 0, []
    desc = arcpy.Describe(dem)
    sr = desc.spatialReference
    src = getattr(desc, "catalogPath", None) or dem      # a raster layer name -> its dataset path
    if sewer_import.is_gda94(sr) and getattr(sr, "factoryCode", None) not in (7850, 0, None):
        name = sewer_import.choose_gda_transformation(arcpy, sr)
        if name:
            arcpy.env.geographicTransformations = name
            msgs.info(f"Reading the DEM in GDA94 using {name}.")
        else:
            msgs.warn("No GDA94 <-> GDA2020 transformation is listed by ArcGIS; DEM heights may be taken "
                      "from a cell ~1.5 m away.")
    pts = {}
    with arcpy.da.SearchCursor(nodes_ds, ["node_id", "SHAPE@XY"], spatial_reference=sr) as cur:
        for nid, xy in cur:
            if nid in todo and xy is not None:
                pts[nid] = xy
    filled, nodata = 0, []
    for nid in sorted(todo):
        xy = pts.get(nid)
        val = None
        if xy is not None:
            val = _cell_value(arcpy, src, xy[0], xy[1])
        if val is None:
            nodata.append(nid)
            continue
        todo[nid]["ground_level"] = val
        todo[nid]["wwn_ground_source"] = "DEM"
        filled += 1
    low = [n["node_id"] for n in nodes if n.get("wwn_ground_source") == "DEM"
           and n.get("chamber_floor") is not None and n["ground_level"] < n["chamber_floor"] + 0.3]
    if low:
        msgs.warn(f"DEM ground level is less than 0.3 m above the chamber floor at {len(low)} node(s) "
                  "- check: " + ", ".join(low[:20]) + (" ..." if len(low) > 20 else ""))
    return filled, nodata
