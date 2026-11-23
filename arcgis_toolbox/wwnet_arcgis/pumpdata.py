"""Pump station hydraulic data entry (no arcpy; stdlib only).

Pump station hydraulics are not held in the GIS, so the engineer types them in
from the station data sheet.  This module validates that input and writes it to
``wwn_pump_stations`` / ``wwn_pumps`` in a WWNet GeoPackage.

Intended ArcGIS tool: "Set Pump Station Data"
---------------------------------------------
Parameters (in order):
  1. Model GeoPackage (DEFAULT workspace/gpkg parameter).
  2. Pump station (string value list filled from ``list_stations``; label
     "PS1 - <notes>").
  3. Wet well diameter (m)   -- diameter OR area (if both, they must agree
  4. Wet well area (m2)         within 1 %).
  5. Level mode: "AHD" or "depth" (depth = metres above the wet well node invert).
  6. Stop level, 7. Start level, 8. Overflow level (per level mode).
  9. Emergency storage (m3).
 10. Number of duty pumps (default 1), 11. assist pumps (0), 12. standby pumps (0).
 13. Duty pump rate (L/s) -- used when no curve is supplied.
 14. Pump curve (multiline text, pasted from the data sheet / Excel / ICM, like
     "Import Diurnal Profile": one "flow<TAB>head" row per line, header optional).
 15. Column order: "flow_head" | "head_flow".
 16. Flow units: "L/s" | "m3/h" | "m3/s".
 17. Speed (%) (default 100).
 18. Source note (e.g. "Data sheet rev C") -- recorded as a
     "[pump data: <note>, <date>]" tag in the station notes.

``updateParameters`` should prefill parameters 3-12 from ``list_stations`` when
the station parameter changes (area, levels, emergency storage, pump counts by
role, duty rate), and ``execute`` should call::

    curve, curve_warnings = parse_curve(text, order=..., flow_units=...)
    result = set_station(gpkg, ps_id, ..., curve=curve)

then emit ``result["summary"]`` and every entry of ``result["warnings"]`` (plus
``curve_warnings``) as messages.  ``ValueError`` messages are user-facing.
``station_report`` gives one line per station for Check Model.
"""
from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3

_FLOW_FACTOR = {"l/s": 1.0, "m3/h": 1.0 / 3.6, "m3/s": 1000.0}
_TAG_RE = re.compile(r"\s*\[pump data:[^\]]*\]")
_ROLES = ("duty", "assist", "standby")


def _connect(gpkg):
    con = sqlite3.connect(gpkg, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def _num(tok):
    try:
        v = float(tok)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_curve(text, order="flow_head", flow_units="L/s"):
    """Parse pasted pump curve text -> ([(q_lps, head_m), ...], [warnings])."""
    if order not in ("flow_head", "head_flow"):
        raise ValueError("order must be 'flow_head' or 'head_flow'.")
    factor = _FLOW_FACTOR.get(str(flow_units).strip().lower())
    if factor is None:
        raise ValueError("flow_units must be one of 'L/s', 'm3/h', 'm3/s'.")
    pts = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" in raw:
            toks = [t.strip().replace(",", ".") for t in raw.split("\t")]
        elif ";" in line:
            toks = [t.strip().replace(",", ".") for t in line.split(";")]
        elif "," in line:
            toks = [t.strip() for t in line.split(",")]
        else:
            toks = line.split()
        toks = [t for t in toks if t != ""]
        vals = [_num(t) for t in toks]
        if len(vals) < 2 or any(v is None for v in vals):
            continue  # header / junk row
        if len(vals) != 2:
            raise ValueError(
                f"Curve row '{line}' has {len(vals)} values; expected 2 (flow and head).")
        a, b = vals
        q, h = (a, b) if order == "flow_head" else (b, a)
        pts.append((q * factor, h))
    if not pts:
        raise ValueError("No numeric rows found in the pump curve text "
                         "(expected two columns: flow and head).")
    if len(pts) < 2:
        raise ValueError("A pump curve needs at least 2 points.")
    return _validate_curve(pts)


def _validate_curve(pts):
    pts = sorted((float(q), float(h)) for q, h in pts)
    if len(pts) < 2:
        raise ValueError("A pump curve needs at least 2 points.")
    for q, h in pts:
        if q < 0 or h < 0:
            raise ValueError(f"Pump curve values must not be negative (flow {q:g} L/s, head {h:g} m).")
    for (q1, h1), (q2, h2) in zip(pts, pts[1:]):
        if q1 == q2:
            raise ValueError(f"Duplicate flow {q1:g} L/s in the pump curve.")
        if not h2 < h1:
            raise ValueError(
                f"Pump curve head must strictly decrease with increasing flow "
                f"(head {h1:g} m at {q1:g} L/s, {h2:g} m at {q2:g} L/s).")
    warnings = []
    if pts[0][0] > 0:
        warnings.append(
            f"Curve starts at {pts[0][0]:g} L/s, not zero flow (shut-off head unknown); "
            "the engine holds the end values flat.")
    return pts, warnings


def _pump_counts(con, ps_id):
    counts = {r: 0 for r in _ROLES}
    for row in con.execute("SELECT role, COUNT(*) n FROM wwn_pumps WHERE ps_id=? GROUP BY role", (ps_id,)):
        counts[row["role"] or "duty"] = counts.get(row["role"] or "duty", 0) + row["n"]
    return counts


def list_stations(gpkg):
    """One dict per pump station, for a tool drop-down / parameter prefill."""
    con = _connect(gpkg)
    try:
        out = []
        rows = con.execute(
            "SELECT s.*, n.invert_level AS wet_well_invert FROM wwn_pump_stations s "
            "LEFT JOIN wwn_nodes n ON n.node_id = s.wet_well_node_id ORDER BY s.ps_id").fetchall()
        for r in rows:
            d = {k: r[k] for k in ("ps_id", "notes", "wet_well_node_id", "wet_well_invert",
                                   "wet_well_area_m2", "start_level", "stop_level",
                                   "overflow_level", "emergency_storage_m3",
                                   "pump_rate_lps", "duty_config")}
            d["pumps"] = _pump_counts(con, r["ps_id"])
            curves = [p["curve_json"] for p in con.execute(
                "SELECT curve_json FROM wwn_pumps WHERE ps_id=? ORDER BY pump_id", (r["ps_id"],))
                if p["curve_json"]]
            d["curve"] = json.loads(curves[0]) if curves else None
            out.append(d)
        return out
    finally:
        con.close()


def station_report(gpkg):
    """One line per station naming hydraulic fields still missing."""
    lines = []
    for s in list_stations(gpkg):
        miss = []
        if not s["wet_well_node_id"]:
            miss.append("wet well node")
        elif s["wet_well_invert"] is None:
            miss.append("wet well invert")
        for f in ("wet_well_area_m2", "start_level", "stop_level"):
            if s[f] is None:
                miss.append(f)
        if sum(s["pumps"].values()) == 0:
            miss.append("pumps")
        elif s["curve"] is None and not s["pump_rate_lps"]:
            miss.append("pump curve or duty rate")
        if miss:
            lines.append(f"{s['ps_id']}: missing " + ", ".join(miss))
        else:
            lines.append(f"{s['ps_id']}: pump data complete")
    return lines


def _fmt_config(nd, na, ns):
    parts = [f"{nd} duty"]
    if na:
        parts.append(f"{na} assist")
    if ns:
        parts.append(f"{ns} standby")
    return " + ".join(parts)


def set_station(gpkg, ps_id, *, wet_well_diameter_m=None, wet_well_area_m2=None,
                level_mode="AHD", start=None, stop=None, overflow=None,
                emergency_storage_m3=None, n_duty=1, n_assist=0, n_standby=0,
                duty_rate_lps=None, curve=None, speed_pct=100, source_note=None,
                replace_pumps=True):
    """Validate and write pump station data in one transaction. See module docstring."""
    mode = str(level_mode).strip().lower()
    if mode not in ("ahd", "depth"):
        raise ValueError("level_mode must be 'AHD' or 'depth'.")
    warnings = []
    con = _connect(gpkg)
    try:
        row = con.execute(
            "SELECT s.*, n.invert_level AS inv FROM wwn_pump_stations s "
            "LEFT JOIN wwn_nodes n ON n.node_id = s.wet_well_node_id WHERE s.ps_id=?",
            (ps_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pump station '{ps_id}' not found in the model.")
        inv = row["inv"]

        # area
        area = wet_well_area_m2
        if wet_well_diameter_m is not None:
            if wet_well_diameter_m <= 0:
                raise ValueError("Wet well diameter must be greater than zero.")
            a_d = math.pi * wet_well_diameter_m ** 2 / 4.0
            if area is not None and abs(area - a_d) > 0.01 * a_d:
                raise ValueError(
                    f"Wet well area {area:g} m2 does not match diameter {wet_well_diameter_m:g} m "
                    f"(area {a_d:.2f} m2). Enter one or the other.")
            if area is None:
                area = a_d
        if area is not None and area <= 0:
            raise ValueError("Wet well area must be greater than zero.")

        # levels -> AHD
        lv = {"start": start, "stop": stop, "overflow": overflow}
        if mode == "depth":
            if any(v is not None for v in lv.values()):
                if inv is None:
                    raise ValueError(
                        f"Wet well invert of '{ps_id}' is unknown, so depths cannot be converted. "
                        "Enter levels as AHD, or set the invert level of the wet well node.")
                lv = {k: (None if v is None else inv + v) for k, v in lv.items()}
        for k, v in lv.items():
            if v is not None and inv is not None and v <= inv:
                raise ValueError(
                    f"{k.capitalize()} level {v:.2f} m AHD is not above the wet well invert {inv:.2f} m AHD.")
        eff = {"start": row["start_level"], "stop": row["stop_level"], "overflow": row["overflow_level"]}
        eff.update({k: v for k, v in lv.items() if v is not None})
        if any(v is not None for v in lv.values()):
            if eff["stop"] is not None and eff["start"] is not None:
                if not eff["stop"] < eff["start"]:
                    raise ValueError("Stop level must be below start level.")
                gap = eff["start"] - eff["stop"]
                if gap < 0.1:
                    raise ValueError(f"Start - stop is {gap:.2f} m; it must be at least 0.1 m.")
                if gap < 0.3:
                    warnings.append(f"Start - stop is only {gap:.2f} m (< 0.3 m): short cycling likely.")
            if eff["overflow"] is not None:
                top = eff["start"] if eff["start"] is not None else eff["stop"]
                if top is not None and not top < eff["overflow"]:
                    raise ValueError("Overflow level must be above the start level.")
        if emergency_storage_m3 is not None and emergency_storage_m3 < 0:
            raise ValueError("Emergency storage must not be negative.")

        # pumps
        if replace_pumps:
            for nm, n in (("duty", n_duty), ("assist", n_assist), ("standby", n_standby)):
                if int(n) < 0:
                    raise ValueError(f"Number of {nm} pumps must not be negative.")
            if int(n_duty) < 1:
                raise ValueError("A station needs at least one duty pump.")
        if duty_rate_lps is not None and duty_rate_lps <= 0:
            raise ValueError("Duty pump rate must be greater than zero.")
        if speed_pct is None or speed_pct <= 0:
            raise ValueError("Speed (%) must be greater than zero.")
        cpts = None
        if curve is not None:
            cpts, cw = _validate_curve(curve)
            warnings.extend(cw)
        if replace_pumps and cpts is None and duty_rate_lps is None:
            warnings.append("No pump curve or duty rate given: the engine will use an ideal pump.")
        if not replace_pumps and cpts is not None:
            warnings.append("Curve ignored because replace_pumps is False.")

        # build update
        upd = {}
        if area is not None:
            upd["wet_well_area_m2"] = area
        for k, col in (("start", "start_level"), ("stop", "stop_level"), ("overflow", "overflow_level")):
            if lv[k] is not None:
                upd[col] = lv[k]
        if emergency_storage_m3 is not None:
            upd["emergency_storage_m3"] = emergency_storage_m3
        if duty_rate_lps is not None:
            upd["pump_rate_lps"] = duty_rate_lps
        if replace_pumps:
            upd["duty_config"] = _fmt_config(int(n_duty), int(n_assist), int(n_standby))
        tag = f"[pump data: {source_note.strip() + ', ' if source_note and source_note.strip() else ''}{datetime.date.today().isoformat()}]"
        base = _TAG_RE.sub("", row["notes"] or "").strip()
        upd["notes"] = (base + " " + tag).strip()

        pump_ids = []
        con.execute("BEGIN")
        try:
            sets = ", ".join(f"{c}=?" for c in upd)
            con.execute(f"UPDATE wwn_pump_stations SET {sets} WHERE ps_id=?", (*upd.values(), ps_id))
            if replace_pumps:
                con.execute("DELETE FROM wwn_pumps WHERE ps_id=?", (ps_id,))
                cj = json.dumps([[q, h] for q, h in cpts]) if cpts else None
                i = 0
                for role, n in (("duty", n_duty), ("assist", n_assist), ("standby", n_standby)):
                    for _ in range(int(n)):
                        i += 1
                        pid = f"{ps_id}_P{i}"
                        con.execute(
                            "INSERT INTO wwn_pumps (pump_id, ps_id, role, curve_json, speed_pct) "
                            "VALUES (?,?,?,?,?)", (pid, ps_id, role, cj, speed_pct))
                        pump_ids.append(pid)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        # summary
        fa = upd.get("wet_well_area_m2", row["wet_well_area_m2"])
        bits = [f"{ps_id}:"]
        if fa is not None:
            d = math.sqrt(4 * fa / math.pi)
            bits.append(f"wet well {fa:.2f} m2 ({d:.2f} m dia),")
        lvls = [f"{k} {eff[k]:.2f}" for k in ("stop", "start", "overflow") if eff[k] is not None]
        if lvls:
            bits.append(" / ".join(lvls) + " m AHD,")
        if replace_pumps:
            bits.append(_fmt_config(int(n_duty), int(n_assist), int(n_standby)) + ",")
        if cpts:
            bits.append(f"curve {len(cpts)} pts ({cpts[0][0]:g}-{cpts[-1][0]:g} L/s)")
        elif upd.get("pump_rate_lps") or row["pump_rate_lps"]:
            bits.append(f"duty rate {upd.get('pump_rate_lps', row['pump_rate_lps']):g} L/s")
        else:
            bits.append("no curve")
        summary = " ".join(bits).rstrip(",")
        return {"ps_id": ps_id, "updated_fields": list(upd), "pumps": pump_ids,
                "warnings": warnings, "summary": summary}
    finally:
        con.close()
