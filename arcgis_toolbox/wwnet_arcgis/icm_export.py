"""Export a WWNet model to InfoWorks ICM import files (for the Open Data Import Centre).

Pure-Python core (`build`) reads the GeoPackage with sqlite3 and maps it to ICM node / conduit / pump
fields; `write_gdb` (arcpy) writes ICM_Nodes / ICM_Conduits / ICM_Pumps to a file geodatabase (keeps
nulls and vertices); `write_csv` writes the same rows as CSV (nodes with x / y) as a fallback source.

Pressure side (same network as the WWNet SWMM engine, `crates/wwnet-sim/src/pumps.rs`): each pump
station's wet well (Manhole, chamber area = wet well area) -> its pumps (ICM pump links) -> a pump
delivery node `<ps_id>_DEL` (sealed Break node at the wet well floor) -> the rising main (an ICM
conduit with solution model ForceMain) -> the rising main's discharge node.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import struct
import time
from collections import deque

from . import gpkg

SUFFIX_CHARS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # ICM link_suffix is a single character
DEFAULT_KS_MM = 1.0                                     # WWNet ruleset default when ks_mm is NULL

# Pressure-side defaults: the same values as the WWNet engine (crates/wwnet-sim/src/pumps.rs), so the
# ICM network matches what Run Simulation modelled.
DEFAULT_RM_KS_MM = 1.5              # rising main without roughness
DEFAULT_WET_WELL_AREA_M2 = 7.07     # 3.0 m diameter
DEFAULT_STOP_ABOVE_FLOOR_M = 0.3
DEFAULT_START_ABOVE_FLOOR_M = 1.0
MIN_STOP_ABOVE_FLOOR_M = 0.05
ASSIST_START_OFFSET_M = 0.2
ASSIST_STOP_OFFSET_M = 0.1
RM_SOLUTION_MODEL = "ForceMain"     # ICM solution model of a rising main (recommended over "Pressure")
RM_HEADLOSS_TYPE = "None"           # ICM E2075: Normal / High headloss not allowed with ForceMain
GRAVITY_HEADLOSS_TYPE = "Normal"    # ICM default for conduits
MIN_CONDUIT_LENGTH_M = 1.0         # ICM rejects very short conduits: shorter positive lengths are exported as this
HANGING_MARK = "rising main discharge, not connected"   # in the notes of the node Import Pressure Data leaves hanging
DELIVERY_SUFFIX = "_DEL"            # pump delivery node id = <ps_id>_DEL
DELIVERY_OFFSET_M = 1.0             # delivery node moved this far east when it would sit on the wet well

# WWNet node_type -> ICM node_type. Junctions (IS-sewer tees) and IS ends have no chamber -> Break.
ICM_NODE_TYPE = {"ac": "Manhole", "drop_ac": "Manhole", "ms": "Manhole", "wet_well": "Manhole",
                 "io_end": "Manhole", "junction": "Break", "is_end": "Break", "outfall": "Outfall"}

# (field, arcpy type) in output order. Names follow ICM's internal field names where one exists so
# ODIC can match them; wwn_* fields are extra information (map them to user fields or ignore).
# ICM system type of every exported object: a separate wastewater network. Nodes, links and sub-catchments must agree
# (ICM engineering validation: "Node is a different system type than the subcatchment").
SYSTEM_TYPE = "foul"
NODE_FIELDS = [("node_id", "TEXT"), ("node_type", "TEXT"), ("system_type", "TEXT"), ("ground_level", "DOUBLE"),
               ("flood_level", "DOUBLE"), ("flood_type", "TEXT"), ("chamber_floor", "DOUBLE"),
               ("chamber_area", "DOUBLE"),
               ("shaft_area", "DOUBLE"), ("wwn_node_type", "TEXT"), ("wwn_floor_source", "TEXT"),
               ("wwn_ground_source", "TEXT"), ("wwn_outfall_reason", "TEXT"), ("wwn_outfall", "TEXT"),
               ("wwn_connectivity", "TEXT"), ("asset_id", "TEXT"), ("status", "TEXT"), ("notes", "TEXT")]
CONDUIT_FIELDS = [("us_node_id", "TEXT"), ("link_suffix", "TEXT"), ("ds_node_id", "TEXT"), ("system_type", "TEXT"),
                  ("us_invert", "DOUBLE"), ("ds_invert", "DOUBLE"), ("conduit_length", "DOUBLE"),
                  ("conduit_shape", "TEXT"), ("conduit_width", "DOUBLE"), ("conduit_height", "DOUBLE"),
                  ("solution_model", "TEXT"), ("us_headloss_type", "TEXT"), ("ds_headloss_type", "TEXT"),
                  ("roughness_type", "TEXT"), ("bottom_roughness_CW", "DOUBLE"),
                  ("top_roughness_CW", "DOUBLE"), ("bottom_roughness_HW", "DOUBLE"), ("top_roughness_HW", "DOUBLE"),
                  ("material", "TEXT"), ("wwn_conduit_id", "TEXT"), ("wwn_link_kind", "TEXT"),
                  ("wwn_outfall", "TEXT"), ("wwn_connectivity", "TEXT"),
                  ("asset_id", "TEXT"), ("status", "TEXT"), ("notes", "TEXT")]
PUMP_FIELDS = [("us_node_id", "TEXT"), ("link_suffix", "TEXT"), ("ds_node_id", "TEXT"), ("system_type", "TEXT"),
               ("link_type", "TEXT"),
               ("switch_on_level", "DOUBLE"), ("switch_off_level", "DOUBLE"), ("discharge", "DOUBLE"),
               ("head_discharge_id", "TEXT"), ("wwn_pump_id", "TEXT"), ("wwn_ps_id", "TEXT"), ("wwn_role", "TEXT"),
               ("notes", "TEXT")]
# Connectivity report (<stem>_ICM_connectivity.csv): one row per place where sewers have no outfall downstream,
# per isolated node and per outfall.
CONNECTIVITY_FIELDS = [("kind", "TEXT"), ("node_id", "TEXT"), ("cause", "TEXT"), ("sewers", "LONG"),
                       ("nearest_sewer", "TEXT"), ("nearest_m", "DOUBLE"), ("nearest_ps", "TEXT"),
                       ("nearest_ps_m", "DOUBLE"), ("x", "DOUBLE"), ("y", "DOUBLE")]
NEAREST_SEARCH_M = 50.0     # nearest other sewer reported within this distance of a dead end / outfall
PS_NEAR_M = 50.0            # a dead end this close to a pump station with no wet well match: likely its inlet sewer
FALSE_OUTFALL_M = 5.0       # a terminal outfall with a sewer it is not connected to this close: possible missed join
REPORT_LIMIT = 30           # dead ends / loops listed in the messages (all of them are in the CSV)
HEAD_DISCHARGE_FIELDS = [("head_discharge_id", "TEXT"), ("head", "DOUBLE"), ("discharge", "DOUBLE")]
# One foul sub-catchment per loaded node (all WWNet loads on that node: land-use LU-* rows, other sub-catchments,
# node loads). Residential -> population (wastewater profile 1); commercial / industrial -> trade flow (trade profile 1);
# manual ADWF and node loads -> additional foul flow (wastewater profile 1). Points at the node: ICM draws a circle of
# total_area for a sub-catchment without a boundary.
SUBCATCHMENT_FIELDS = [("subcatchment_id", "TEXT"), ("node_id", "TEXT"), ("system_type", "TEXT"),
                       ("total_area", "DOUBLE"), ("contributing_area", "DOUBLE"), ("connectivity", "DOUBLE"),
                       ("population", "DOUBLE"), ("wastewater_profile", "LONG"), ("trade_flow", "DOUBLE"),
                       ("trade_profile", "LONG"), ("additional_foul_flow", "DOUBLE"), ("x", "DOUBLE"),
                       ("y", "DOUBLE"), ("wwn_adwf_lps", "DOUBLE"), ("wwn_residential_ha", "DOUBLE"),
                       ("wwn_commercial_ha", "DOUBLE"), ("wwn_industrial_ha", "DOUBLE"), ("wwn_rcodes", "TEXT"),
                       ("notes", "TEXT")]
WASTEWATER_PROFILE = 1
TRADE_PROFILE = 1
NOMINAL_AREA_HA = 0.01          # total_area of a sub-catchment that only carries node loads (no area)

# Names that clash (case-insensitively) with geodatabase system fields -> AddField ERROR 000012.
# WWNet node types that, with pipes in and none out, are a sewer end that should have joined another
# pipe (missed tee / main outside the selection) rather than a chamber at a free outfall.
DANGLING_TYPES = ("junction", "is_end", "io_end")

RESERVED_FIELDS = {"shape", "objectid", "oid", "fid", "shape_length", "shape_area", "globalid"}


def check_reserved_fields():
    bad = [n for n, _ in NODE_FIELDS + CONDUIT_FIELDS + PUMP_FIELDS + SUBCATCHMENT_FIELDS
           if n.lower() in RESERVED_FIELDS]
    if bad:
        raise RuntimeError(f"ICM export field name(s) clash with reserved geodatabase fields: {bad}")


check_reserved_fields()

OUT_SR = {"GDA2020 / MGA zone 50 (EPSG:7850)": 7850, "GDA94 / MGA zone 50 (EPSG:28350)": 28350}


def _gpkg_points(blob, want: int):
    """[(x, y)] of a GeoPackage POINT (want=1) or LINESTRING (want=2) blob, else None."""
    if not blob or blob[:2] != b"GP":
        return None
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((blob[3] >> 1) & 7)
    if env is None:
        return None
    wkb = blob[8 + env:]
    if len(wkb) < 5:
        return None
    bo = "<" if wkb[0] == 1 else ">"
    gtype = struct.unpack(bo + "I", wkb[1:5])[0]
    if gtype % 1000 != want:                    # ISO WKB: 1 / 2 (+1000 Z / 2000 M / 3000 ZM)
        return None
    dims = (2, 3, 3, 4)[(gtype // 1000) % 4]
    step = 8 * dims
    if want == 1:
        x, y = struct.unpack(bo + "dd", wkb[5:21])
        return None if math.isnan(x) or math.isnan(y) else [(x, y)]
    n = struct.unpack(bo + "I", wkb[5:9])[0]
    return [struct.unpack(bo + "dd", wkb[9 + step * i:9 + step * i + 16]) for i in range(n)]


def _line_length(blob):
    """Planar length of a GeoPackage LINESTRING blob (model CRS is metric MGA), or None."""
    pts = _gpkg_points(blob, 2)
    if pts is None:
        return None
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(pts, pts[1:]))


def _rows(con, sql):
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur]


def _ids(ids, limit=20):
    return ", ".join(ids[:limit]) + (" ..." if len(ids) > limit else "")


def _r(v, nd=3):
    return None if v is None else round(float(v), nd)


def _tables(con) -> set:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _pairs(text):
    """'[[a, b], ...]' (JSON) -> [(a, b)] of finite floats; None if malformed or empty."""
    try:
        out = [(float(p[0]), float(p[1])) for p in json.loads(text)]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not out or not all(math.isfinite(a) and math.isfinite(b) for a, b in out):
        return None
    return out


def _pos(v):
    """v as a float if it is a positive finite number, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def build(gpkg_path: str, gravity_only: bool = False) -> dict:
    """{'nodes', 'conduits', 'pumps', 'head_discharge': [row], 'warnings': [str], 'skipped': {table: count},
    'counts': {...}, 'connectivity': {...}}. Rows are dicts keyed by NODE_FIELDS / CONDUIT_FIELDS / PUMP_FIELDS
    names. Rows that are not in wwn_nodes / wwn_conduits (pump delivery nodes, rising mains, pumps) carry
    `_extra` = True and `_geom` = geometry hints resolved by `extra_geometry` (arcpy side).
    gravity_only: gravity sewers and their nodes only; no rising mains / pumps / delivery nodes; wet wells
    with sewers in and none out become Outfalls; nodes with no gravity sewer are left out."""
    con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        nodes = _rows(con, "SELECT node_id, node_type, surface_level, invert_level, chamber_diameter_mm, "
                           "flood_level, status, asset_id, notes, geom FROM wwn_nodes ORDER BY node_id")
        conduits = _rows(con, "SELECT conduit_id, us_node_id, ds_node_id, dn, internal_diameter_mm, material, "
                              "us_invert, ds_invert, length_m, ks_mm, status, asset_id, notes, geom "
                              "FROM wwn_conduits WHERE status <> 'abandoned' ORDER BY us_node_id, conduit_id")
        tables = _tables(con)
        skipped = {}
        subs = _rows(con, "SELECT subcatchment_id, outlet_node_id, net_area_ha, gross_area_ha, broadacre, land_use, "
                          "rcode, ground, region, extra_flow_lps, adwf_lps, profile_id, status FROM wwn_subcatchments "
                          "WHERE status IS NULL OR status <> 'abandoned' ORDER BY subcatchment_id") \
            if "wwn_subcatchments" in tables else []
        loads = _rows(con, "SELECT load_id, node_id, connections, adwf_lps, profile_id FROM wwn_node_loads "
                           "ORDER BY load_id") if "wwn_node_loads" in tables else []
        meta = dict(con.execute("SELECT key, value FROM wwn_meta").fetchall()) if "wwn_meta" in tables else {}
        stations = _rows(con, "SELECT ps_id, wet_well_node_id, discharge_node_id, rising_main_id, pump_rate_lps, "
                              "wet_well_area_m2, start_level, stop_level, status, notes FROM wwn_pump_stations "
                              "WHERE status <> 'abandoned' ORDER BY ps_id") if "wwn_pump_stations" in tables else []
        ps_pts = _rows(con, "SELECT ps_id, wet_well_node_id, geom FROM wwn_pump_stations "
                            "WHERE status <> 'abandoned' ORDER BY ps_id") if "wwn_pump_stations" in tables else []
        mains = _rows(con, "SELECT rm_id, ps_id, discharge_node_id, dn, internal_diameter_mm, material, "
                           "roughness_type, roughness, length_m, profile_json, status, notes, geom "
                           "FROM wwn_rising_mains WHERE status <> 'abandoned' ORDER BY rm_id") \
            if "wwn_rising_mains" in tables else []
        pumps = _rows(con, "SELECT pump_id, ps_id, role, curve_json, speed_pct, notes FROM wwn_pumps "
                           "ORDER BY pump_id") if "wwn_pumps" in tables else []
    finally:
        con.close()

    warnings = []
    node_ids = {n["node_id"] for n in nodes}
    node_xy = {}
    for n in nodes:
        pts = _gpkg_points(n.pop("geom"), 1)
        if pts:
            node_xy[n["node_id"]] = pts[0]
    lines = {}
    for c in conduits:
        pts = _gpkg_points(c["geom"], 2)
        if pts and len(pts) > 1:
            lines[c["conduit_id"]] = pts
    on_grav = {c["us_node_id"] for c in conduits} | {c["ds_node_id"] for c in conduits}
    ps_unmatched = []
    for st in ps_pts:
        pts = _gpkg_points(st["geom"], 1)
        if pts and (not st["wet_well_node_id"] or st["wet_well_node_id"] not in on_grav):
            ps_unmatched.append((st["ps_id"], pts[0]))
    if gravity_only:
        on_sewer = {c["us_node_id"] for c in conduits} | {c["ds_node_id"] for c in conduits}
        off = [n["node_id"] for n in nodes if n["node_id"] not in on_sewer]
        nodes = [n for n in nodes if n["node_id"] in on_sewer]
        if off:
            warnings.append(f"Gravity only: {len(off)} node(s) with no gravity sewer attached not exported "
                            "(pump station / rising main points, isolated nodes): " + _ids(off))

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
            "ground_level": _r(n["surface_level"]), "flood_level": _r(n["flood_level"]), "flood_type": None,
            "chamber_floor": _r(floor), "chamber_area": area, "shaft_area": area,
            "wwn_node_type": n["node_type"], "wwn_floor_source": src,
            "wwn_ground_source": "model" if n["surface_level"] is not None else "",
            "wwn_outfall_reason": "",
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

    per_us = {}

    def next_suffix(us):
        k = per_us.get(us, 0)
        if k >= len(SUFFIX_CHARS):
            raise RuntimeError(f"More than {len(SUFFIX_CHARS)} links leave node {us}; "
                               "ICM link suffixes are single characters.")
        per_us[us] = k + 1
        return SUFFIX_CHARS[k]

    out_links, missing_inv, missing_node, short = [], [], [], []
    for c in conduits:
        suffix = next_suffix(c["us_node_id"])
        if c["us_invert"] is None or c["ds_invert"] is None:
            missing_inv.append(c["conduit_id"])
        for nid in (c["us_node_id"], c["ds_node_id"]):
            if nid not in node_ids:
                missing_node.append(f"{c['conduit_id']} -> {nid}")
        dia = c["internal_diameter_mm"] or c["dn"]
        length = c["length_m"] if c["length_m"] is not None else _line_length(c["geom"])
        if length is not None and 0 < length < MIN_CONDUIT_LENGTH_M:
            short.append(c["conduit_id"])
            length = MIN_CONDUIT_LENGTH_M
        ks = c["ks_mm"] if c["ks_mm"] is not None else DEFAULT_KS_MM
        out_links.append({
            "us_node_id": c["us_node_id"], "link_suffix": suffix, "ds_node_id": c["ds_node_id"],
            "us_invert": _r(c["us_invert"]), "ds_invert": _r(c["ds_invert"]), "conduit_length": _r(length, 2),
            "conduit_shape": "CIRC", "conduit_width": _r(dia, 1), "conduit_height": _r(dia, 1),
            "solution_model": "Full", "us_headloss_type": GRAVITY_HEADLOSS_TYPE,
            "ds_headloss_type": GRAVITY_HEADLOSS_TYPE, "roughness_type": "CW", "bottom_roughness_CW": ks, "top_roughness_CW": ks,
            "bottom_roughness_HW": None, "top_roughness_HW": None,
            "material": (c["material"] or "").upper() or None, "wwn_conduit_id": c["conduit_id"],
            "wwn_link_kind": "gravity", "asset_id": c["asset_id"], "status": c["status"], "notes": c["notes"]})
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

    pres = _pressure(out_nodes, [] if gravity_only else stations, [] if gravity_only else mains,
                     [] if gravity_only else pumps, next_suffix, warnings, short)
    if short:
        warnings.append(f"{len(short)} conduit(s) shorter than {MIN_CONDUIT_LENGTH_M:g} m exported with length "
                        f"{MIN_CONDUIT_LENGTH_M:g} m (ICM minimum): " + _ids(short))
    if pres["not_exported"]:
        skipped["wwn_rising_mains"] = len(pres["not_exported"])

    # ICM requires every link invert at a node to be >= the node's chamber floor. The model's node invert is
    # not always the lowest connected invert (tee / junction / dangling nodes, estimated inverts, adverse
    # steps, rising main ends), so lower the floor to the lowest attached conduit invert where needed.
    low_all = {}
    for c in out_links + pres["conduits"]:
        for nid, inv in ((c["us_node_id"], c["us_invert"]), (c["ds_node_id"], c["ds_invert"])):
            if inv is not None:
                low_all[nid] = min(inv, low_all.get(nid, inv))
    lowered = []
    for n in out_nodes + pres["nodes"]:
        lo = low_all.get(n["node_id"])
        if lo is None:
            continue
        fl = n["chamber_floor"]
        if fl is None:
            n["chamber_floor"] = _r(lo)
            n["wwn_floor_source"] = "lowest connected invert"
        elif fl > lo:
            n["chamber_floor"] = _r(lo)
            n["wwn_floor_source"] = f"lowered to lowest connected invert (was {fl:g})"
            lowered.append(n["node_id"])
    if lowered:
        warnings.append(f"{len(lowered)} node(s): chamber floor lowered to the lowest connected conduit invert "
                        "(ICM requires link inverts >= chamber floor): " + _ids(lowered))

    # A terminal node (links in, none out) is a free outfall in the WWNet engine, so it is exported as an
    # ICM Outfall (ICM rejects links with no outfall). A wet well with no exported pump stays Manhole.
    n_in, n_out = {}, {}
    for c in out_links + pres["conduits"] + pres["pumps"]:
        n_out[c["us_node_id"]] = n_out.get(c["us_node_id"], 0) + 1
        n_in[c["ds_node_id"]] = n_in.get(c["ds_node_id"], 0) + 1
    wet_wells = {s["wet_well_node_id"] for s in stations if s["wet_well_node_id"]}
    terminal, dangling, rm_out, rm_hang, ww_out = [], [], [], [], []
    for n in out_nodes:
        nid = n["node_id"]
        if n_in.get(nid, 0) == 0 or n_out.get(nid, 0) > 0 or n["wwn_node_type"] == "outfall":
            continue
        if gravity_only and (n["wwn_node_type"] == "wet_well" or nid in wet_wells):
            n["node_type"] = "Outfall"
            n["wwn_outfall_reason"] = "wet well (gravity only export)"
            ww_out.append(nid)
            continue
        if n["wwn_node_type"] == "wet_well" or nid in wet_wells:
            warnings.append(f"{nid}: wet well exported as Manhole with no pump out of it (no pump / rising main "
                            "exported for its station, see the pump station warnings); ICM will report it as "
                            "not connected to an outfall")
            continue
        if nid in pres["discharge_nodes"]:
            # the rising main discharges to a point with nothing downstream: the engine's free outfall
            n["node_type"] = "Outfall"
            hanging = HANGING_MARK in (n["notes"] or "")
            n["wwn_outfall_reason"] = ("rising main discharge, not connected (connect manually)" if hanging
                                       else "rising main discharge")
            rm_out.append(nid)
            if hanging:
                rm_hang.append(nid)
        elif n["wwn_node_type"] in DANGLING_TYPES:
            # NOT an outfall: its flow belongs in the main it should join (missed tee). Making it an outfall
            # would silently drop that flow from the downstream network; leave the ICM validation error.
            n["wwn_outfall_reason"] = "dangling end"
            dangling.append(nid)
        else:
            n["node_type"] = "Outfall"
            n["wwn_outfall_reason"] = "terminal"
            terminal.append(nid)
    if gravity_only:
        warnings.append(f"Gravity only: rising mains, pumps and pump delivery nodes not exported; {len(ww_out)} "
                        "wet well(s) exported as Outfall" + (": " + _ids(ww_out) if ww_out else "") + ".")
    if terminal:
        warnings.append(f"{len(terminal)} terminal node(s) exported as Outfall: " + _ids(terminal))
    if rm_out:
        warnings.append(f"{len(rm_out)} rising main discharge node(s) have no downstream pipe: exported as Outfall "
                        "(the pumped flow leaves the model there, as in the WWNet engine): " + _ids(rm_out))
    if rm_hang:
        warnings.append(f"{len(rm_hang)} rising main(s) end at a hanging node that was never connected to the gravity "
                        "network (exported as Outfall; connect them manually): " + _ids(rm_hang))
    if dangling:
        warnings.append(f"{len(dangling)} sewer end(s) have no downstream pipe and are not chambers (possible missed "
                        "tee / main outside the selection). NOT made outfalls (their flow would leave the network): ICM will "
                        "report them as not connected to an outfall until the join is fixed: " + _ids(dangling))
    sc = subcatchments(subs, loads, meta, {n["node_id"] for n in out_nodes}, warnings)
    conn = connectivity(out_nodes + pres["nodes"], out_links + pres["conduits"] + pres["pumps"], node_xy, lines,
                        {c["conduit_id"]: (c["us_node_id"], c["ds_node_id"]) for c in conduits},
                        ps_unmatched=ps_unmatched)
    for r in out_nodes + pres["nodes"] + out_links + pres["conduits"] + pres["pumps"]:
        r["system_type"] = SYSTEM_TYPE
    counts = {"nodes": len(out_nodes) + len(pres["nodes"]), "conduits": len(out_links),
              "rising_mains": len(pres["conduits"]), "pumps": len(pres["pumps"]),
              "delivery_nodes": len(pres["nodes"]), "head_discharge_tables": len(pres["hd_ids"]),
              "subcatchments": len(sc["rows"])}
    return {"nodes": out_nodes + pres["nodes"], "conduits": out_links + pres["conduits"], "pumps": pres["pumps"],
            "head_discharge": pres["head_discharge"], "warnings": warnings, "skipped": skipped, "counts": counts,
            "subcatchments": sc["rows"], "loading": sc["summary"], "connectivity": conn,
            "gravity_only": gravity_only}


class _SewerGrid:
    """Nearest gravity sewer to a point, ignoring the sewers attached to a given node (uniform grid)."""

    def __init__(self, lines: dict, ends: dict, cell: float = 25.0):
        """lines: {conduit_id: [(x, y), ...]}; ends: {conduit_id: (us_node, ds_node)}."""
        self.cell, self.cells, self.ends = cell, {}, ends
        for cid, pts in lines.items():
            for a, b in zip(pts, pts[1:]):
                for kx in range(math.floor(min(a[0], b[0]) / cell), math.floor(max(a[0], b[0]) / cell) + 1):
                    for ky in range(math.floor(min(a[1], b[1]) / cell), math.floor(max(a[1], b[1]) / cell) + 1):
                        self.cells.setdefault((kx, ky), []).append((cid, a, b))

    def nearest(self, p, node_id: str, max_d: float):
        """(conduit_id, distance) of the nearest sewer within max_d not attached to node_id, else (None, None)."""
        c = self.cell
        kx, ky = math.floor(p[0] / c), math.floor(p[1] / c)
        r = math.ceil(max_d / c)
        best, bd = None, None
        for ix in range(kx - r, kx + r + 1):
            for iy in range(ky - r, ky + r + 1):
                for cid, a, b in self.cells.get((ix, iy), ()):
                    if node_id in self.ends.get(cid, ()):
                        continue
                    d = _seg_dist(p, a, b)
                    if d <= max_d and (bd is None or d < bd or (d == bd and cid < best)):
                        best, bd = cid, d
        return best, bd


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _sink_loops(nodes: list, down: dict) -> list:
    """Closed loops among `nodes` (every downstream path of each node stays inside `nodes`): the strongly
    connected components with no link out of them (Kosaraju, iterative). [[node_id, ...] sorted]."""
    inside = set(nodes)
    order, seen = [], set()
    for s0 in nodes:
        if s0 in seen:
            continue
        seen.add(s0)
        stack = [(s0, iter(down.get(s0, ())))]
        while stack:
            v, it = stack[-1]
            for w in it:
                if w in inside and w not in seen:
                    seen.add(w)
                    stack.append((w, iter(down.get(w, ()))))
                    break
            else:
                stack.pop()
                order.append(v)
    rev = {}
    for v in nodes:
        for w in down.get(v, ()):
            if w in inside:
                rev.setdefault(w, []).append(v)
    comp, comps = {}, []
    for s0 in reversed(order):
        if s0 in comp:
            continue
        k = len(comps)
        comp[s0] = k
        members, stack = [s0], [s0]
        while stack:
            v = stack.pop()
            for w in rev.get(v, ()):
                if w not in comp:
                    comp[w] = k
                    members.append(w)
                    stack.append(w)
        comps.append(members)
    sinks = []
    for k, members in enumerate(comps):
        if all(comp[w] == k for v in members for w in down.get(v, ()) if w in inside):
            if len(members) > 1 or any(v in down.get(v, ()) for v in members):
                sinks.append(sorted(members))
    return sorted(sinks)


def connectivity(nodes: list, links: list, node_xy: dict, lines: dict, ends: dict, ps_unmatched=None) -> dict:
    """Downstream (us -> ds) path from every exported node and link to an ICM Outfall, the way ICM's
    'not connected to an outfall' check follows the network. Sets `wwn_outfall` (the outfall reached, by
    fewest links) and `wwn_connectivity` on every node and link row in place.
    Where sewers have no outfall downstream, the place they end up is a dead end (links in, none out, not
    an outfall: dangling Break, wet well without pump) or a closed loop (sewers flowing in a circle).
    Returns {'lines': [message line], 'rows': [CONNECTIVITY_FIELDS row], 'sewers', 'sewers_ok', 'dead_ends',
    'loops', 'isolated', 'bad_links', 'false_outfalls'}.
    ps_unmatched: [(ps_id, (x, y))] pump stations with no wet well on a gravity sewer; a dead end within
    PS_NEAR_M of one is probably that station's inlet sewer and is reported as such."""
    by_id = {n["node_id"]: n for n in nodes}
    down, up, bad = {}, {}, []
    for lk in links:
        u, d = lk["us_node_id"], lk["ds_node_id"]
        if u not in by_id or d not in by_id:
            bad.append(lk)
            continue
        down.setdefault(u, []).append(d)
        up.setdefault(d, []).append(u)
    outfalls = sorted(nid for nid, n in by_id.items() if n["node_type"] == "Outfall")
    outlet = {o: o for o in outfalls}
    q = deque(outfalls)
    while q:
        v = q.popleft()
        for u in up.get(v, ()):
            if u not in outlet:
                outlet[u] = outlet[v]
                q.append(u)
    unc = sorted(nid for nid in by_id if nid not in outlet)
    isolated = [nid for nid in unc if nid not in down and nid not in up]
    dead = [nid for nid in unc if nid in up and nid not in down]
    bad_out = {}
    for lk in bad:
        bad_out.setdefault(lk["us_node_id"], []).append(lk["ds_node_id"])

    sink_of = {}

    def claim(starts, key):
        q2 = deque()
        for v in starts:
            if v not in sink_of:
                sink_of[v] = key
                q2.append(v)
        while q2:
            v = q2.popleft()
            for u in up.get(v, ()):
                if u not in sink_of and u not in outlet:
                    sink_of[u] = key
                    q2.append(u)

    for d0 in dead:
        claim([d0], d0)
    rest = [nid for nid in unc if nid not in sink_of and (nid in down or nid in up)]
    loops = _sink_loops(rest, down)
    for lp in loops:
        claim(lp, lp[0])
    loop_of = {lp[0]: lp for lp in loops}

    grid = _SewerGrid(lines, ends)

    def near(nid):
        p = node_xy.get(nid)
        if p is None:
            return None, None
        return grid.nearest(p, nid, NEAREST_SEARCH_M)

    def gravity(lk):
        return lk.get("wwn_link_kind") == "gravity"

    sewers = [lk for lk in links if gravity(lk)]
    per_sink, per_outfall = {}, {}
    for lk in sewers:
        d = lk["ds_node_id"]
        if d in outlet:
            per_outfall[outlet[d]] = per_outfall.get(outlet[d], 0) + 1
        elif d in sink_of:
            per_sink[sink_of[d]] = per_sink.get(sink_of[d], 0) + 1

    def dead_cause(nid):
        n = by_id[nid]
        t = n["wwn_node_type"]
        if nid in bad_out:
            return f"its downstream link goes to {bad_out[nid][0]}, a node not in the export"
        if t in DANGLING_TYPES:
            return f"dangling end ({t}, no chamber, nothing downstream)"
        if t == "wet_well" or n["wwn_outfall_reason"].startswith("wet well"):
            return "wet well, no pump exported out of it"
        return f"{n['node_type']} with nothing downstream"

    def near_text(cid, dist):
        if cid is None:
            return f"no other sewer within {NEAREST_SEARCH_M:g} m (main outside the selection?)"
        return f"nearest other sewer {cid} at {dist:.1f} m"

    def near_ps(nid):
        p = node_xy.get(nid)
        if p is None or not ps_unmatched:
            return None, None
        best = min(((math.hypot(p[0] - q[0], p[1] - q[1]), pid) for pid, q in ps_unmatched))
        return (best[1], best[0]) if best[0] <= PS_NEAR_M else (None, None)

    places, rows, n_ps = [], [], 0
    for key in sorted(per_sink, key=lambda k: (-per_sink[k], k)) + \
            [k for k in dead + [lp[0] for lp in loops] if k not in per_sink]:
        pid = pdist = None
        if key in loop_of:
            lp = loop_of[key]
            cause = (f"closed loop of {len(lp)} node(s) (sewers flow in a circle with no way out; check the flow "
                     "direction of these sewers): " + " ".join(lp[:8]) + (" ..." if len(lp) > 8 else ""))
            cid = dist = None
            kind = "closed_loop"
        else:
            cause = dead_cause(key)
            cid, dist = near(key)
            kind = "dead_end"
            cause += "; " + near_text(cid, dist)
            pid, pdist = near_ps(key)
            if pid is not None:
                n_ps += 1
                cause += (f"; pump station {pid} at {pdist:.1f} m (likely its inlet sewer: re-import with a wet "
                          "well distance above this)")
        places.append((key, per_sink.get(key, 0), cause))
        p = node_xy.get(key)
        rows.append({"kind": kind, "node_id": key, "cause": cause, "sewers": per_sink.get(key, 0),
                     "nearest_sewer": cid, "nearest_m": _r(dist, 2), "nearest_ps": pid, "nearest_ps_m": _r(pdist, 2),
                     "x": _r(p[0]) if p else None, "y": _r(p[1]) if p else None})

    false_out = []
    out_rows = []
    for o in outfalls:
        reason = by_id[o]["wwn_outfall_reason"] or "model outfall"
        cid, dist = near(o)
        if by_id[o]["wwn_outfall_reason"] == "terminal" and dist is not None and dist <= FALSE_OUTFALL_M:
            false_out.append((o, per_outfall.get(o, 0), cid, dist))
        p = node_xy.get(o)
        out_rows.append({"kind": "outfall", "node_id": o, "cause": reason, "sewers": per_outfall.get(o, 0),
                         "nearest_sewer": cid, "nearest_m": _r(dist, 2), "nearest_ps": None, "nearest_ps_m": None,
                         "x": _r(p[0]) if p else None, "y": _r(p[1]) if p else None})
    for nid in isolated:
        p = node_xy.get(nid)
        cause = "no link attached" + (f" (its only link goes to {bad_out[nid][0]}, not in the export)"
                                      if nid in bad_out else "")
        rows.append({"kind": "isolated", "node_id": nid, "cause": cause, "sewers": 0, "nearest_sewer": None,
                     "nearest_m": None, "nearest_ps": None, "nearest_ps_m": None,
                     "x": _r(p[0]) if p else None, "y": _r(p[1]) if p else None})
    rows += sorted(out_rows, key=lambda r: (-r["sewers"], r["node_id"]))

    # per-row fields
    for nid, n in by_id.items():
        if nid in outlet:
            n["wwn_outfall"], n["wwn_connectivity"] = outlet[nid], "ok"
        elif nid in sink_of:
            n["wwn_outfall"] = ""
            n["wwn_connectivity"] = ("no outfall: closed loop at " if sink_of[nid] in loop_of
                                     else "no outfall: dead end at ") + sink_of[nid]
        else:
            n["wwn_outfall"], n["wwn_connectivity"] = "", "no outfall: isolated node"
    bad_ids = {id(lk) for lk in bad}
    for lk in links:
        u, d = lk["us_node_id"], lk["ds_node_id"]
        if id(lk) in bad_ids:
            lk["wwn_outfall"] = ""
            lk["wwn_connectivity"] = f"not imported by ICM: node {u if u not in by_id else d} not in the export"
        elif d in outlet:
            lk["wwn_outfall"], lk["wwn_connectivity"] = outlet[d], "ok"
        else:
            lk["wwn_outfall"] = ""
            lk["wwn_connectivity"] = ("no outfall: closed loop at " if sink_of.get(d) in loop_of
                                      else "no outfall: dead end at ") + str(sink_of.get(d))

    n_ok = sum(1 for lk in sewers if lk["ds_node_id"] in outlet)
    msg = [f"Connectivity (downstream along each link to an Outfall, as ICM checks it): {n_ok} of {len(sewers)} "
           f"gravity sewer(s) reach an outfall" + ("." if n_ok == len(sewers) else f"; {len(sewers) - n_ok} do not.")]
    if places:
        msg.append(f"No outfall downstream at {len(dead)} dead end(s) and {len(loops)} closed loop(s); sewers "
                   "upstream of each, most first" + (f" (first {REPORT_LIMIT}; all in the connectivity CSV)"
                                                     if len(places) > REPORT_LIMIT else "") + ":")
        msg += [f"  {k}: {n} sewer(s); {cause}" for k, n, cause in places[:REPORT_LIMIT]]
        if n_ps:
            msg.append(f"{n_ps} dead end(s) are within {PS_NEAR_M:g} m of a pump station with no wet well connected: "
                       "raise the wet well distance in Import Network Data / Import Pressure Data to the distance "
                       "shown, then re-import.")
    if isolated:
        msg.append(f"{len(isolated)} node(s) with no link attached (ICM may report them too): " + _ids(isolated))
    if bad:
        msg.append(f"{len(bad)} link(s) reference a node not in the export (ICM will not import them): "
                   + _ids([f"{lk['us_node_id']} -> {lk['ds_node_id']}" for lk in bad]))
    reasons = {}
    for o in outfalls:
        r = by_id[o]["wwn_outfall_reason"] or "model outfall"
        reasons[r] = reasons.get(r, 0) + 1
    top = sorted(outfalls, key=lambda o: (-per_outfall.get(o, 0), o))
    msg.append(f"Outfalls: {len(outfalls)} (" + ", ".join(f"{v} {k}" for k, v in sorted(reasons.items())) + ")"
               + ("; gravity sewers draining to each, most first: " + ", ".join(
                   f"{o} {per_outfall.get(o, 0)}" for o in top[:15]) + (" ..." if len(top) > 15 else "")
                  if outfalls else "") + ".")
    if false_out:
        false_out.sort(key=lambda t: (t[3], t[0]))
        msg.append(f"Possible false outfalls ({len(false_out)}): terminal chamber with a sewer it is not connected "
                   f"to within {FALSE_OUTFALL_M:g} m (missed join, or a sewer digitised the wrong way): "
                   + "; ".join(f"{o} ({n} sewer(s), {cid} at {d:.1f} m)" for o, n, cid, d in false_out[:15])
                   + (" ..." if len(false_out) > 15 else ""))
    return {"lines": msg, "rows": rows, "sewers": len(sewers), "sewers_ok": n_ok, "dead_ends": dead,
            "loops": loops, "isolated": isolated, "bad_links": len(bad), "false_outfalls": [t[0] for t in false_out]}


def _demand():
    from . import shared
    return shared.core("demand")


def subcatchments(subs: list, loads: list, meta: dict, node_ids: set, warnings: list) -> dict:
    """ICM foul sub-catchments, one per loaded node: {'rows': [...], 'summary': {...}}. Flows as the WWNet engine
    loads them in Run Simulation (DS 50 daily flow / 86400; manual ADWF; node loads). Residential population is the
    DS 50 persons/ha x area, scaled to the per capita flow of wastewater profile 1 where a row's L/person/day
    differs (wet ground), so population x per capita flow = the WWNet flow."""
    dm = _demand()
    rates_cache = {}

    def rates(region, ground):
        key = ((region or "south").lower(), (ground or "dry").lower())
        if key not in rates_cache:
            rates_cache[key] = dm.load_rates(*key)
        return rates_cache[key]

    try:
        per_conn = float(meta.get("adwf_per_connection_lpd") or 630.0)
    except ValueError:
        per_conn = 630.0
    regions = {((r.get("region") or "south").lower(), (r.get("ground") or "dry").lower()) for r in subs} \
        or {("south", "dry")}
    main = sorted(regions)[0] if len(regions) == 1 else ("south", "dry")
    per_capita = rates(*main).row_for(20)[3]
    if len(regions) > 1:
        warnings.append(f"Sub-catchments use more than one DS 50 region / ground ({sorted(regions)}): population is "
                        f"scaled to {per_capita:g} L/person/day (wastewater profile {WASTEWATER_PROFILE}).")
    nodes: dict = {}
    missing, no_area, extra, profiles = [], [], [], set()

    def node_row(nid):
        return nodes.setdefault(nid, {"pop": 0.0, "trade": 0.0, "aff": 0.0, "res": 0.0, "com": 0.0, "ind": 0.0,
                                      "codes": {}, "src": []})

    for s in subs:
        nid = s["outlet_node_id"]
        if nid not in node_ids:
            missing.append(s["subcatchment_id"])
            continue
        if s.get("profile_id"):
            profiles.add(s["profile_id"])
        if s.get("extra_flow_lps"):
            extra.append(s["subcatchment_id"])
        if s.get("adwf_lps") is not None:
            if s["adwf_lps"] > 0:
                n = node_row(nid)
                n["aff"] += s["adwf_lps"]
                n["src"].append(f"{s['subcatchment_id']} manual ADWF {s['adwf_lps']:.4g} L/s")
            continue
        area = s.get("net_area_ha")
        if area is None and s.get("broadacre") and s.get("gross_area_ha") is not None:
            area = s["gross_area_ha"] * 0.75                    # DS 50 4.2.1(k), as the engine
        if area is None or area <= 0:
            if area is None:
                no_area.append(s["subcatchment_id"])
            continue
        rt = rates(s.get("region"), s.get("ground"))
        n = node_row(nid)
        lu = (s.get("land_use") or "residential").lower()
        if lu == "residential":
            num, _note = dm.parse_rcode(s.get("rcode"))
            row, _rnote = rt.residential_row(num)
            n["pop"] += area * row[2] * row[3] / per_capita
            n["res"] += area
            n["codes"][row[1]] = n["codes"].get(row[1], 0.0) + area
        else:
            key = {"commercial": "suburban_commercial_school_hospital_public"}.get(lu, lu)
            lps_ha = rt.non_residential_lps_per_ha(key)
            if lps_ha is None:
                lps_ha = rt.non_residential_lps_per_ha("suburban_commercial_school_hospital_public")
            n["trade"] += area * lps_ha
            n["ind" if lu == "industrial" else "com"] += area
        if not str(s["subcatchment_id"]).startswith(dm.ID_PREFIX):
            n["src"].append(f"{s['subcatchment_id']} {lu} {area:.4g} ha")
    for ld in loads:
        nid = ld["node_id"]
        a = ld["adwf_lps"] if ld.get("adwf_lps") is not None else (
            ld["connections"] * per_conn / 86400.0 if ld.get("connections") is not None else None)
        if a is None or a <= 0 or nid not in node_ids:
            if a and nid not in node_ids:
                missing.append(ld["load_id"])
            continue
        if ld.get("profile_id"):
            profiles.add(ld["profile_id"])
        n = node_row(nid)
        n["aff"] += a
        n["src"].append(f"node load {ld['load_id']} {a:.4g} L/s")
    rows = []
    for nid in sorted(nodes):
        n = nodes[nid]
        adwf = n["pop"] * per_capita / 86400.0 + n["trade"] + n["aff"]
        if adwf <= 0:
            continue
        area = n["res"] + n["com"] + n["ind"]
        codes = "; ".join(f"{k} {v:.2f} ha" for k, v in sorted(
            n["codes"].items(), key=lambda kv: dm.parse_rcode(kv[0])[0] or 0))
        rows.append({"subcatchment_id": nid, "node_id": nid, "system_type": SYSTEM_TYPE,
                     "total_area": _r(area if area > 0 else NOMINAL_AREA_HA, 4), "contributing_area": 0.0,
                     "connectivity": 100.0, "population": _r(n["pop"], 2),
                     "wastewater_profile": WASTEWATER_PROFILE if (n["pop"] > 0 or n["aff"] > 0) else None,
                     "trade_flow": _r(n["trade"], 5) if n["trade"] > 0 else None,
                     "trade_profile": TRADE_PROFILE if n["trade"] > 0 else None,
                     "additional_foul_flow": _r(n["aff"], 5) if n["aff"] > 0 else None,
                     "x": None, "y": None, "wwn_adwf_lps": _r(adwf, 5),
                     "wwn_residential_ha": _r(n["res"], 4), "wwn_commercial_ha": _r(n["com"], 4),
                     "wwn_industrial_ha": _r(n["ind"], 4), "wwn_rcodes": codes or None,
                     "notes": "; ".join(n["src"]) or None, "_extra": True, "_geom": nid})
    if missing:
        warnings.append(f"{len(missing)} sub-catchment(s) / node load(s) drain to a node that is not in the model: "
                        "NOT exported (their flow is lost, as in the WWNet engine): " + _ids(missing))
    if no_area:
        warnings.append(f"{len(no_area)} sub-catchment(s) have no net area (only a polygon): NOT exported; set "
                        "net_area_ha: " + _ids(no_area))
    if extra:
        warnings.append(f"{len(extra)} sub-catchment(s) have extra_flow_lps (design GSDF only, not simulated): not "
                        "exported: " + _ids(extra))
    total = sum(r["wwn_adwf_lps"] for r in rows)
    summary = {"per_capita_lpd": per_capita, "adwf_lps": total, "profiles": sorted(profiles),
               "population": sum(r["population"] or 0 for r in rows),
               "trade_flow_lps": sum(r["trade_flow"] or 0 for r in rows),
               "additional_foul_flow_lps": sum(r["additional_foul_flow"] or 0 for r in rows)}
    return {"rows": rows, "summary": summary}


def _pressure(out_nodes: list, stations: list, mains: list, pumps: list, next_suffix, warnings: list,
              short: list | None = None) -> dict:
    """Pump delivery nodes, rising-main conduits, pumps and head-discharge rows (see the module docstring).
    Edits wet-well rows of `out_nodes` in place (chamber / shaft area = wet well area)."""
    res = {"nodes": [], "conduits": [], "pumps": [], "head_discharge": [], "hd_ids": [], "discharge_nodes": set(),
           "not_exported": []}
    if not mains and not stations:
        return res
    node_by_id = {n["node_id"]: n for n in out_nodes}
    used_ids = set(node_by_id)
    st_by_id = {s["ps_id"]: s for s in stations}
    by_main = {s["rising_main_id"]: s for s in stations if s["rising_main_id"]}
    pumps_by_ps = {}
    for p in pumps:
        pumps_by_ps.setdefault(p["ps_id"], []).append(p)

    def unique(base):
        nid, k = base, 1
        while nid in used_ids:
            k += 1
            nid = f"{base}_{k}"
        used_ids.add(nid)
        return nid

    delivery = {}            # ps_id -> delivery node row
    no_rough, rm_by_ps = [], {}
    for rm in mains:
        rid = rm["rm_id"]
        st = st_by_id.get(rm["ps_id"]) or by_main.get(rid)
        ds = rm["discharge_node_id"] or (st or {}).get("discharge_node_id")
        if not ds or ds not in node_by_id:
            warnings.append(f"Rising main {rid}: discharge node {ds or '(none)'} is not in the model; NOT exported "
                            "(re-run Import Pressure Data or set wwn_rising_mains.discharge_node_id).")
            res["not_exported"].append(rid)
            continue
        profile = None
        if rm["profile_json"]:
            profile = _pairs(rm["profile_json"])
            if profile is None or len(profile) < 2:
                warnings.append(f"Rising main {rid}: profile_json is not [[chainage_m, level_m], ...] with at least "
                                "2 points; ignored.")
                profile = None
            else:
                profile.sort()
                if len(profile) > 2:
                    warnings.append(f"Rising main {rid}: {len(profile) - 2} intermediate profile point(s) not "
                                    "exported (one ICM conduit from the first to the last profile level).")
        ww = (st or {}).get("wet_well_node_id")
        wet = node_by_id.get(ww) if ww else None
        if st is not None and wet is not None:
            us = delivery.get(st["ps_id"])
            if us is None:
                floor, fsrc = wet["chamber_floor"], "wet well floor"
                if profile:
                    floor, fsrc = profile[0][1], "rising main profile"
                us = {"node_id": unique(st["ps_id"] + DELIVERY_SUFFIX), "node_type": "Break",
                      "ground_level": wet["ground_level"], "flood_level": None, "flood_type": "Sealed",
                      "chamber_floor": _r(floor), "chamber_area": None, "shaft_area": None,
                      "wwn_node_type": "pump_delivery", "wwn_floor_source": fsrc if floor is not None else "",
                      "wwn_ground_source": "wet well" if wet["ground_level"] is not None else "",
                      "wwn_outfall_reason": "", "asset_id": None, "status": st["status"],
                      "notes": f"Pump delivery node of pump station {st['ps_id']} (start of rising main {rid})",
                      "_extra": True, "_ground_from": ww, "_offset_from": ww,
                      "_geom": [("rm_start", rid), ("ps", st["ps_id"]), ("node", ww)]}
                delivery[st["ps_id"]] = us
                res["nodes"].append(us)
            else:
                warnings.append(f"Pump station {st['ps_id']}: more than one rising main; {rid} also starts at "
                                f"{us['node_id']} (parallel main). The WWNet simulation models only "
                                f"{st['rising_main_id'] or rm_by_ps[st['ps_id']]}.")
            rm_by_ps.setdefault(st["ps_id"], rid)
        else:
            why = (f"its pump station {rm['ps_id']} is not in the model" if st is None and rm["ps_id"]
                   else "it has no pump station" if st is None
                   else f"the wet well node {ww or '(none)'} of pump station {st['ps_id']} is not in the model")
            floor = profile[0][1] if profile else node_by_id[ds]["chamber_floor"]
            us = {"node_id": unique(rid + "_US"), "node_type": "Break", "ground_level": None, "flood_level": None,
                  "flood_type": "Sealed", "chamber_floor": _r(floor), "chamber_area": None, "shaft_area": None,
                  "wwn_node_type": "rising_main_start", "wwn_floor_source": "", "wwn_ground_source": "",
                  "wwn_outfall_reason": "", "asset_id": None, "status": rm["status"],
                  "notes": f"Start of rising main {rid} (no pump: {why})", "_extra": True,
                  "_geom": [("rm_start", rid)] + ([("ps", st["ps_id"])] if st else [])}
            res["nodes"].append(us)
            warnings.append(f"Rising main {rid}: {why}; exported from an unconnected start node {us['node_id']} "
                            "with no pump (no flow enters it).")
        # inverts: wet well floor (or the first profile level) -> discharge node floor (as in the engine)
        ds_floor = node_by_id[ds]["chamber_floor"]
        us_inv = us["chamber_floor"]
        ds_inv = ds_floor
        if profile:
            ds_inv = profile[-1][1] if ds_floor is None else max(profile[-1][1], ds_floor)
        length = _pos(rm["length_m"]) or _line_length(rm["geom"]) or None
        if not length and profile and profile[-1][0] > profile[0][0]:
            length = profile[-1][0] - profile[0][0]
        if not length:
            warnings.append(f"Rising main {rid}: no length, geometry or profile; conduit_length left blank.")
        elif length < MIN_CONDUIT_LENGTH_M:
            if short is not None:
                short.append(rid)
            length = MIN_CONDUIT_LENGTH_M
        dia = _pos(rm["internal_diameter_mm"]) or _pos(rm["dn"])
        if dia is None:
            warnings.append(f"Rising main {rid}: no diameter; conduit_width / conduit_height left blank.")
        rtype, rval = (rm["roughness_type"] or "ks").strip().lower(), _pos(rm["roughness"])
        cw = hw = None
        if rtype == "hw_c" and rval is not None:
            rt, hw = "HW", rval
        elif rtype == "ks" and rval is not None:
            rt, cw = "CW", rval
        else:
            rt, cw = "CW", DEFAULT_RM_KS_MM
            no_rough.append(rid)
        if us_inv is None or ds_inv is None:
            warnings.append(f"Rising main {rid}: no invert level at the "
                            + ("start (wet well floor unknown)" if us_inv is None else f"discharge node {ds}")
                            + "; left blank.")
        res["discharge_nodes"].add(ds)
        res["conduits"].append({
            "us_node_id": us["node_id"], "link_suffix": next_suffix(us["node_id"]), "ds_node_id": ds,
            "us_invert": _r(us_inv), "ds_invert": _r(ds_inv), "conduit_length": _r(length, 2),
            "conduit_shape": "CIRC", "conduit_width": _r(dia, 1), "conduit_height": _r(dia, 1),
            "solution_model": RM_SOLUTION_MODEL, "us_headloss_type": RM_HEADLOSS_TYPE,
            "ds_headloss_type": RM_HEADLOSS_TYPE, "roughness_type": rt, "bottom_roughness_CW": cw,
            "top_roughness_CW": cw, "bottom_roughness_HW": hw, "top_roughness_HW": hw,
            "material": (rm["material"] or "").upper() or None, "wwn_conduit_id": rid,
            "wwn_link_kind": "rising_main", "asset_id": None, "status": rm["status"], "notes": rm["notes"],
            "_extra": True, "_geom": [("rm_line", rid), ("nodes", us["node_id"], ds)]})
    if no_rough:
        warnings.append(f"{len(no_rough)} rising main(s) have no roughness: Colebrook-White ks {DEFAULT_RM_KS_MM} mm "
                        "assumed (as in the WWNet engine): " + _ids(no_rough))

    for st in stations:
        pid = st["ps_id"]
        if pid not in delivery:
            ww = st["wet_well_node_id"]
            if ww not in node_by_id:
                warnings.append(f"Pump station {pid}: wet well node {ww or '(none)'} is not in the model; "
                                "its pumps are NOT exported.")
            else:
                warnings.append(f"Pump station {pid}: no rising main exported, so its pumps are NOT exported.")
            continue
        _station_pumps(st, node_by_id[st["wet_well_node_id"]], delivery[pid], pumps_by_ps.get(pid, []),
                       next_suffix, res, warnings)
    return res


def _station_pumps(st: dict, wet: dict, dnode: dict, defs: list, next_suffix, res: dict, warnings: list):
    pid, ww = st["ps_id"], wet["node_id"]
    area = _pos(st["wet_well_area_m2"])
    if area is None:
        area = DEFAULT_WET_WELL_AREA_M2
        warnings.append(f"Pump station {pid}: no wet well area; chamber / shaft area of {ww} set to "
                        f"{DEFAULT_WET_WELL_AREA_M2} m2 (3.0 m dia, as in the WWNet engine).")
    wet["chamber_area"] = wet["shaft_area"] = _r(area, 4)
    floor = wet["chamber_floor"]
    start, stop = st["start_level"], st["stop_level"]
    if floor is not None:
        if stop is None:
            stop = floor + DEFAULT_STOP_ABOVE_FLOOR_M
            warnings.append(f"Pump station {pid}: no stop level; {DEFAULT_STOP_ABOVE_FLOOR_M} m above the wet well "
                            "floor assumed.")
        if stop < floor + MIN_STOP_ABOVE_FLOOR_M:
            stop = floor + MIN_STOP_ABOVE_FLOOR_M
            warnings.append(f"Pump station {pid}: stop level not above the wet well floor; "
                            f"{MIN_STOP_ABOVE_FLOOR_M} m above the floor used.")
        if start is None:
            start = floor + DEFAULT_START_ABOVE_FLOOR_M
            warnings.append(f"Pump station {pid}: no start level; {DEFAULT_START_ABOVE_FLOOR_M} m above the wet "
                            "well floor assumed.")
    elif start is None or stop is None:
        warnings.append(f"Pump station {pid}: wet well {ww} has no floor level and the start / stop level is "
                        "missing; switch levels left blank (enter them with Set Pump Station Data).")
    if start is not None and stop is not None and not start > stop:
        start = stop + 0.5
        warnings.append(f"Pump station {pid}: start level not above stop level; start set to stop + 0.5 m.")
    rate = _pos(st["pump_rate_lps"])
    if not defs:
        if rate is None:
            warnings.append(f"Pump station {pid}: no pumps and no pump rate; NO pump exported (enter the station "
                            "data with Set Pump Station Data). Its wet well has no outlet in ICM.")
            return
        warnings.append(f"Pump station {pid}: no pumps listed; one duty pump at the station rate "
                        f"({rate:g} L/s) exported.")
        defs = [{"pump_id": f"{pid}_P1", "role": "duty", "curve_json": None, "speed_pct": 100, "notes": None}]
    standby, n_out = [], 0
    for p in defs:
        role = (p["role"] or "duty").strip().lower()
        if role == "standby":
            standby.append(p["pump_id"])
            continue
        if role not in ("duty", "assist"):
            warnings.append(f"Pump {p['pump_id']}: unknown role '{p['role']}'; exported as duty.")
            role = "duty"
        speed = p["speed_pct"] if p["speed_pct"] is not None else 100
        if _pos(speed) is None:
            warnings.append(f"Pump {p['pump_id']}: invalid speed {speed}%; 100% used.")
            speed = 100
        s = float(speed) / 100.0
        curve = None
        if p["curve_json"]:
            curve = _pairs(p["curve_json"])
            if curve is None:
                warnings.append(f"Pump {p['pump_id']}: curve_json is not [[q_lps, head_m], ...]; ignored.")
        on, off = start, stop
        if role == "assist":
            on = None if start is None else start + ASSIST_START_OFFSET_M
            off = None if stop is None else stop + ASSIST_STOP_OFFSET_M
        row = {"us_node_id": ww, "link_suffix": None, "ds_node_id": dnode["node_id"], "link_type": None,
               "switch_on_level": _r(on), "switch_off_level": _r(off), "discharge": None, "head_discharge_id": None,
               "wwn_pump_id": p["pump_id"], "wwn_ps_id": pid, "wwn_role": role, "notes": p["notes"],
               "_extra": True, "_geom": [("nodes", ww, dnode["node_id"])]}
        if curve:
            # ICM head-discharge table: decreasing head, first discharge 0 (closed valve); speed by the affinity laws
            pts = sorted({(round(h * s * s, 3), round(q * s, 3)) for q, h in curve}, key=lambda t: (-t[0], t[1]))
            hd = f"HD_{p['pump_id']}"
            n = 1
            while hd in res["hd_ids"]:
                n += 1
                hd = f"HD_{p['pump_id']}_{n}"
            res["hd_ids"].append(hd)
            res["head_discharge"] += [{"head_discharge_id": hd, "head": h, "discharge": q} for h, q in pts]
            row.update(link_type="Rotpmp", head_discharge_id=hd)
            if pts[0][1] != 0:
                warnings.append(f"Pump {p['pump_id']}: the curve's highest head ({pts[0][0]:g} m) has flow "
                                f"{pts[0][1]:g} L/s; ICM expects 0 there (closed valve head). Check table {hd}.")
            if abs(s - 1.0) > 1e-9:
                warnings.append(f"Pump {p['pump_id']}: speed {speed:g}% applied to head-discharge table {hd} "
                                "(affinity laws).")
        elif rate is not None:
            row.update(link_type="Fixpmp", discharge=_r(rate * s, 3))
            warnings.append(f"Pump {p['pump_id']}: no curve; fixed discharge {rate * s:g} L/s (station pump rate) "
                            "exported.")
        else:
            warnings.append(f"Pump {p['pump_id']}: no curve and no station pump rate; NOT exported.")
            continue
        row["link_suffix"] = next_suffix(ww)
        res["pumps"].append(row)
        n_out += 1
    if standby:
        warnings.append(f"Pump station {pid}: standby pump(s) not exported (as in the WWNet engine): "
                        + ", ".join(standby))
    if n_out and not any(r["wwn_role"] == "duty" for r in res["pumps"] if r["wwn_ps_id"] == pid):
        warnings.append(f"Pump station {pid}: no duty pump (assist only); the WWNet simulation does not model "
                        "this station.")


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
               warnings: list, pumps_csv: str | None = None, hd_csv: str | None = None,
               head_discharge: list | None = None, subcatchments_csv: str | None = None,
               loading: dict | None = None) -> str:
    lines = [
        f"WWNet -> InfoWorks ICM export of {gpkg_path}",
        f"Coordinate system: {out_sr_label}",
        "",
        "Files",
        f"  {gdb}\\ICM_Nodes, ICM_Conduits" + (", ICM_Pumps" if pumps_csv else "")
        + (", ICM_Subcatchments" if subcatchments_csv else "")
        + "   (preferred: keeps blank values and pipe vertices)",
        f"  {nodes_csv}   (fallback: x / y columns)",
        f"  {conduits_csv}   (fallback: no geometry; ICM draws straight links between nodes)",
    ]
    if pumps_csv:
        lines.append(f"  {pumps_csv}   (fallback: pumps)")
    if hd_csv:
        lines.append(f"  {hd_csv}   (pump head-discharge tables)")
    if subcatchments_csv:
        lines.append(f"  {subcatchments_csv}   (fallback: sub-catchments with x / y)")
    lines += [
        "",
        "ICM: File > Import > InfoWorks > Open Data Import Centre. Import NODES FIRST, then conduits"
        + (", then pumps" if pumps_csv else "") + (", then sub-catchments." if subcatchments_csv else "."),
        "Field names already match ICM's where one exists; check each in the ODIC drop-downs.",
        f"system_type = {SYSTEM_TYPE} on every node, conduit, pump and sub-catchment: MAP IT in each table (a saved",
        "ODIC config made before 2026-11-26 lacks it); ICM engineering validation warns when a sub-catchment drains to",
        "a node of a different system type.",
        "",
        "Node table",
        "  node_id -> Node ID | node_type -> Node type (Manhole / Break / Outfall)",
        "  Terminal chambers (pipes in, none out) are exported as Outfall (free outfall, as in the WWNet engine);",
        "  rising main discharge nodes left hanging by Import Pressure Data are Outfalls too (wwn_outfall_reason ends",
        "  'not connected (connect manually)'); join them to the gravity network by hand",
        "  Conduits shorter than 1 m (ICM minimum) are exported with length 1 m; inverts are unchanged",
        "  dangling sewer ends (tee / IS / IO ends with nothing downstream) stay Break: fix the join, not the outfall",
        "  wwn_outfall = the Outfall the node drains to (blank = none); wwn_connectivity = ok / why there is no outfall",
        "  (dead end or closed loop at <node>); the same two fields are on the conduits. Symbolise them in ArcGIS Pro",
        "  to see the areas ICM reports as not connected to an outfall; the _ICM_connectivity.csv lists each place.",
        "  Gravity only export: wet wells are Outfalls and no rising mains / pumps are written.",
        "  ground_level -> Ground level | flood_level -> Flood level | chamber_floor -> Chamber floor level",
        "  Chamber floor is the node invert, lowered to the lowest connected conduit invert where a pipe is lower",
        "  (tee branches, adverse steps, estimated inverts, rising main ends; ICM: 'ds invert is below attached",
        "  node floor level'); wwn_floor_source says 'lowered to lowest connected invert (was X)'",
        "  flood_type -> Flood type (Sealed on pump delivery / rising main start nodes; blank = ICM default)",
        "  chamber_area / shaft_area -> Chamber area / Shaft area (m2, from chamber_diameter_mm; wet wells: the",
        "    station's wet well area; blank = ICM default)",
        "Conduit table (gravity sewers AND rising mains; wwn_link_kind = gravity / rising_main)",
        "  us_node_id + link_suffix -> US node ID + Link suffix (unique per US node) | ds_node_id -> DS node ID",
        "  us_invert / ds_invert -> US / DS invert level | conduit_length -> Length (m)",
        "  conduit_shape = CIRC -> Shape | conduit_width / conduit_height -> Width / Height (mm; internal diameter, else DN)",
        f"  solution_model -> Solution model (Full for gravity sewers, {RM_SOLUTION_MODEL} for rising mains).",
        "    MAP THIS FIELD: without it the rising mains import as Full (gravity) conduits",
        f"  us_headloss_type / ds_headloss_type -> US / DS headloss type (Normal for gravity sewers, {RM_HEADLOSS_TYPE}",
        "    for rising mains: ICM error E2075 'Normal or high headlosses are not allowed for ForceMain')",
        "  roughness_type = CW or HW | bottom/top_roughness_CW -> Colebrook-White ks (mm) |",
        "    bottom/top_roughness_HW -> Hazen-Williams C (rising mains with a C value)",
        "  wwn_conduit_id / asset_id -> a user text field or Asset ID (optional)",
    ]
    if pumps_csv:
        lines += [
            "Pressure side (same network as the WWNet simulation)",
            "  wet well (Manhole) -> pump(s) -> <ps_id>_DEL pump delivery node (Break, Sealed, at the wet well floor)",
            "  -> rising main conduit (ForceMain) -> discharge chamber. Rising main levels are not in the GIS: the",
            "  conduit runs from the wet well floor to the discharge chamber floor (as in the WWNet engine).",
            "Pump table (import after nodes and conduits; ODIC object type Pump)",
            "  us_node_id (wet well) + link_suffix -> US node ID + Link suffix | ds_node_id -> DS node ID",
            "  link_type -> Link type: Rotpmp (rotodynamic, uses head_discharge_id) or Fixpmp (fixed discharge)",
            "  switch_on_level / switch_off_level -> Switch on / off level (m AHD)",
            "  discharge -> Discharge (Fixpmp only; L/s: set the ODIC unit to l/s if ICM's flow unit is m3/s)",
            "  head_discharge_id -> Head discharge ID (Rotpmp only)",
            "  Assist pumps: on / off 0.2 / 0.1 m above duty; standby pumps are not exported (as in the engine).",
        ]
    if head_discharge:
        lines += ["Head-discharge tables (create each in ICM with this ID, or import the CSV, then validate):",
                  "  head (m), discharge (L/s); decreasing head, first discharge 0 (closed valve head)"]
        cur = None
        for r in head_discharge:
            if r["head_discharge_id"] != cur:
                cur = r["head_discharge_id"]
                lines.append(f"  {cur}")
            lines.append(f"    {r['head']:g}\t{r['discharge']:g}")
    if subcatchments_csv:
        lo = loading or {}
        pc = lo.get("per_capita_lpd", 180)
        lines += [
            "Sub-catchment table (import after nodes; ODIC object type Subcatchment): dry weather (foul) loads",
            "  One sub-catchment per loaded node; subcatchment_id = node_id = the node it drains to. Point geometry",
            "  at the node and x / y: ICM draws a circle of total_area for a sub-catchment without a boundary.",
            "  system_type = foul | total_area -> Total area (ha; area loaded, 0.01 ha for nodes with node loads only)",
            "  contributing_area = 0 (no rainfall runoff from these sub-catchments) | connectivity = 100 (%)",
            f"  population -> Population, with wastewater_profile = {WASTEWATER_PROFILE}: residential (DS 50 persons/ha"
            f" x area, at {pc:g} L/person/day)",
            f"  trade_flow -> Trade flow (L/s) with trade_profile = {TRADE_PROFILE}: commercial and industrial "
            "(DS 50 Table 4.3)",
            f"  additional_foul_flow -> Additional foul flow (L/s; wastewater profile {WASTEWATER_PROFILE}): manual "
            "ADWF and node loads",
            "  Flows are L/s: set the ODIC unit to l/s if ICM's flow unit is m3/s. wwn_* fields: for checking",
            "  (wwn_adwf_lps = the node's ADWF as WWNet simulates it).",
            "  In ICM create a Waste Water event with profile "
            f"{WASTEWATER_PROFILE}: per capita flow {pc:g} l/hd/d and the diurnal",
            f"  multipliers; and a Trade Waste event with profile {TRADE_PROFILE}: flow scaling factor 1.0 and the "
            "diurnal multipliers.",
            "  Run the simulation with both events. Calibrating: change the per capita flow / multipliers there.",
        ]
        if lo.get("profiles"):
            lines.append("  WWNet diurnal profile(s) in use: " + ", ".join(lo["profiles"])
                         + " (enter their factors in the ICM profiles; not exported)")
        lines.append(f"  Totals: population {lo.get('population', 0):.0f}, trade flow {lo.get('trade_flow_lps', 0):.2f}"
                     f" L/s, additional foul flow {lo.get('additional_foul_flow_lps', 0):.2f} L/s, ADWF "
                     f"{lo.get('adwf_lps', 0):.2f} L/s")
    lines += ["", "Not exported: diurnal profiles (set them in the ICM Waste Water / Trade Waste events), standby "
                  "pumps" + ("" if subcatchments_csv else ", loads (the model has none)") + "."]
    if warnings:
        lines += ["", "Warnings"] + [f"  - {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def _xy(p):
    """(x, y) floats, or None. arcpy returns (None, None) for SHAPE@XY of a feature without geometry."""
    if p is None:
        return None
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _first_xy(shape):
    """First vertex of a polyline as (x, y), or None (no geometry / empty geometry)."""
    fp = getattr(shape, "firstPoint", None)
    return None if fp is None else _xy((getattr(fp, "X", None), getattr(fp, "Y", None)))


def point_geometry(arcpy, xy, sr):
    return arcpy.PointGeometry(arcpy.Point(xy[0], xy[1]), sr)


def line_geometry(arcpy, pts, sr):
    """A 2+ point polyline, or None when the points coincide (ICM then draws the link between its nodes)."""
    if len(pts) < 2 or all(math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) < 1e-6 for p in pts[1:]):
        return None
    return arcpy.Polyline(arcpy.Array([arcpy.Point(x, y) for x, y in pts]), sr)


def read_pressure_geometry(arcpy, gpkg_path: str, data: dict, sr) -> dict:
    """{'ps': {ps_id: (x, y)}, 'rm': {rm_id: polyline}} in `sr`, read only when the export has pressure rows."""
    out = {"ps": {}, "rm": {}}
    hints = {h[0] for r in data["nodes"] + data["conduits"] if r.get("_extra") for h in r["_geom"]}
    if "ps" in hints:
        with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_pump_stations"), ["ps_id", "SHAPE@XY"],
                                   spatial_reference=sr) as cur:
            out["ps"] = {pid: _xy(xy) for pid, xy in cur if _xy(xy) is not None}
    if hints & {"rm_start", "rm_line"}:
        with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_rising_mains"), ["rm_id", "SHAPE@"],
                                   spatial_reference=sr) as cur:
            out["rm"] = {rid: shp for rid, shp in cur if _first_xy(shp) is not None}   # skip NULL / empty
    return out


def node_geometry_fn(arcpy, sr, geo: dict):
    """write_gdb `extra_geom` callback for pump delivery / rising main start nodes."""
    def fn(row, xy):
        pt = None
        for h in row.get("_geom", ()):
            if h[0] == "rm_start":
                pt = _first_xy(geo["rm"].get(h[1]))
            elif h[0] == "ps":
                pt = _xy(geo["ps"].get(h[1]))
            elif h[0] == "node":
                pt = _xy(xy.get(h[1]))
            if pt is not None:
                break
        if pt is None:
            return None
        ref = _xy(xy.get(row.get("_offset_from")))
        if ref is not None and math.hypot(pt[0] - ref[0], pt[1] - ref[1]) < 0.01:
            pt = (pt[0] + DELIVERY_OFFSET_M, pt[1])          # keep it off the wet well (ICM: distinct nodes)
        return point_geometry(arcpy, pt, sr)
    return fn


def subcatchment_geometry_fn(arcpy, sr, node_xy: dict):
    """write_gdb `extra_geom` callback: a sub-catchment's point is its node (as written to ICM_Nodes)."""
    def fn(row, _seen):
        p = _xy(node_xy.get(row.get("_geom")))
        return point_geometry(arcpy, p, sr) if p is not None else None
    return fn


def set_subcatchment_xy(rows: list, node_xy: dict) -> int:
    """x / y of each sub-catchment = its node (output coordinates); returns how many have no node location."""
    n = 0
    for r in rows:
        p = _xy(node_xy.get(r["node_id"]))
        r["x"], r["y"] = (round(p[0], 3), round(p[1], 3)) if p is not None else (None, None)
        n += p is None
    return n


def link_geometry_fn(arcpy, sr, geo: dict, node_xy: dict):
    """write_gdb `extra_geom` callback for rising mains (their own line) and pumps (wet well -> delivery)."""
    def fn(row, _seen):
        for h in row.get("_geom", ()):
            if h[0] == "rm_line" and geo["rm"].get(h[1]) is not None:
                return geo["rm"][h[1]]
            a, b = (_xy(node_xy.get(h[1])), _xy(node_xy.get(h[2]))) if h[0] == "nodes" else (None, None)
            if a is not None and b is not None:
                return line_geometry(arcpy, [a, b], sr)
        return None
    return fn


NOTES_LENGTH = 2000          # gdb text field length of "notes" (merged runs list their segments)
TEXT_LENGTH = 254            # every other text field (ids, codes, sources)


def text_length(name: str) -> int:
    return NOTES_LENGTH if name == "notes" else TEXT_LENGTH


def _values(row: dict, fields: list) -> list:
    """Row values in field order; text longer than its gdb field is cut to fit (ending " ...") instead of
    failing the insert (Pro: "Field length exceeded"). The CSV files keep the full text."""
    out = []
    for f, typ in fields:
        v = row.get(f)
        if typ == "TEXT" and isinstance(v, str) and len(v) > text_length(f):
            v = v[:text_length(f) - 4] + " ..."
        out.append(v)
    return out


def write_gdb(arcpy, gdb: str, table: str, geom_type: str, fields: list, rows: list, key: str,
              src_ds: str | None, src_key: str | None, sr, timings: dict | None = None, extra_geom=None) -> dict:
    """Create <gdb>\\<table> (replacing it) and fill it with `rows`, taking geometry from `src_ds`
    (projected by the cursor to `sr`). Rows marked `_extra` (not in `src_ds`) are inserted after the
    others with the geometry `extra_geom(row, xy_so_far)` returns (None = no geometry).
    Returns {id: (x, y)} for POINT tables (else {}).
    If `timings` is a dict, it receives seconds for: exists, delete (0.0 if not called), create,
    fields, copy."""
    pc = time.perf_counter
    tm = {"delete": 0.0}
    fc = os.path.join(gdb, table)
    t0 = pc()
    exists = arcpy.Exists(fc)
    tm["exists"] = pc() - t0
    if exists:
        t0 = pc()
        arcpy.management.Delete(fc)
        tm["delete"] = pc() - t0
    t0 = pc()
    arcpy.management.CreateFeatureclass(gdb, table, geom_type, spatial_reference=sr)
    tm["create"] = pc() - t0
    desc = [[name, typ, "", text_length(name) if typ == "TEXT" else ""] for name, typ in fields]
    t0 = pc()
    try:
        add_fields = arcpy.management.AddFields
    except AttributeError:                      # older arcpy: one call per field (slow)
        add_fields = None
    if add_fields is not None:
        add_fields(fc, desc)
    else:
        for name, typ in fields:
            if typ == "TEXT":
                arcpy.management.AddField(fc, name, "TEXT", field_length=text_length(name))
            else:
                arcpy.management.AddField(fc, name, typ)
    tm["fields"] = pc() - t0
    by_id = {r[key]: r for r in rows if not r.get("_extra")}
    extras = [r for r in rows if r.get("_extra")]
    cols = [f for f, _ in fields] + ["SHAPE@"]
    xy = {}
    t0 = pc()
    with arcpy.da.InsertCursor(fc, cols) as ins:
        if src_ds is not None and by_id:
            src_fields = [src_key, "SHAPE@"] + (["SHAPE@XY"] if geom_type == "POINT" else [])
            with arcpy.da.SearchCursor(src_ds, src_fields, spatial_reference=sr) as src:
                for rec in src:
                    r = by_id.get(rec[0])
                    if r is None:
                        continue
                    ins.insertRow(_values(r, fields) + [rec[1]])
                    p = _xy(rec[2]) if geom_type == "POINT" else None
                    if p is not None:
                        xy[rec[0]] = p
        for r in extras:
            g = extra_geom(r, xy) if extra_geom is not None else None
            ins.insertRow(_values(r, fields) + [g])
            p = _first_xy(g) if geom_type == "POINT" else None
            if p is not None:
                xy[r[key]] = p
    tm["copy"] = pc() - t0
    if isinstance(timings, dict):
        timings.update(tm)
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
    todo = {n["node_id"]: n for n in nodes if n.get("ground_level") is None and not n.get("_extra")}
    if not todo:
        _copy_ground(nodes)
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
            if nid in todo and _xy(xy) is not None:
                pts[nid] = _xy(xy)
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
    _copy_ground(nodes)
    low = [n["node_id"] for n in nodes if n.get("wwn_ground_source") == "DEM"
           and n.get("chamber_floor") is not None and n["ground_level"] < n["chamber_floor"] + 0.3]
    if low:
        msgs.warn(f"DEM ground level is less than 0.3 m above the chamber floor at {len(low)} node(s) "
                  "- check: " + ", ".join(low[:20]) + (" ..." if len(low) > 20 else ""))
    return filled, nodata


def _copy_ground(nodes: list):
    """Pump delivery nodes take the (possibly DEM-filled) ground level of their wet well."""
    by_id = {n["node_id"]: n for n in nodes}
    for n in nodes:
        src = by_id.get(n.get("_ground_from"))
        if n.get("ground_level") is None and src is not None and src.get("ground_level") is not None:
            n["ground_level"] = src["ground_level"]
            n["wwn_ground_source"] = "wet well"
