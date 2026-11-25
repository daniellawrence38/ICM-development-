"""Import pump station hydraulic data from the Water Corporation pump station register (.xlsx) (no arcpy).

Reads three worksheets (Level Settings, Civil, Motor and Pump Attributes; every sheet has the SAP functional
location in a key column) and writes, per model pump station whose ``ps_id`` matches a functional location
(or a SAP sub-location such as ``S0037310-PMP01``): wet well floor / ground level (the wet well NODE's invert and
surface level), stop / start / overflow levels, wet well area (storage between the high level alarm and the
overflow level / that depth), emergency storage, and one duty rate with duty / standby pump counts, through
``pumpdata.set_station``.  The column map, sheet names and units live in
``templates/import/watercorp_pump_register.toml`` (see ``load_config``).

Re-run after Import Sewer Data / Import Pressure Data, which can reset the wet well node invert.  What the code
actually does (checked): Import Sewer Data builds the model afresh (an overwrite loses every pump value and
wwn_pumps row; the wet well node invert is re-derived from the sewer inverts).  Import Pressure Data keeps the
station's hydraulic columns and its wwn_pumps rows, and leaves the invert / surface level of an EXISTING chamber
that became the wet well untouched, but it deletes and re-creates the ``WW_<ps_id>`` wet well nodes it created
earlier, so their floor and ground level are lost (the station levels stay, so the floor must be re-imported).
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

from . import pumpdata, xlsx

TEMPLATE_NAME = "watercorp_pump_register.toml"
_SHEET_ROLES = {
    "levels": ("ground", "overflow", "stop", "alarm", "start", "base"),
    "civil": ("wet_well_storage", "emergency_storage"),
    "pumps": ("design_flow", "design_head"),
}
_DEPTH_FACTOR = {"m": 1.0, "mm": 0.001}
_FLOW_FACTOR = {"l/s": 1.0, "m3/h": 1.0 / 3.6, "ml/d": 1000.0 / 86.4}
_LABEL = {"base": "base (wet well floor)", "stop": "stop level", "start": "start level",
          "alarm": "high level alarm", "overflow": "overflow level"}
_MIN_GAP_M = 0.1
_SAME_TOL = 0.01


# ------------------------------------------------------------------ config

def _cfg_error(key, msg):
    return ValueError(f"{TEMPLATE_NAME}: {key}: {msg}")


def load_config(path=None) -> dict:
    """Load and validate the TOML template (default: the bundled / repo copy) -> normalised dict.

    Keys: ``sheets`` {levels|civil|pumps: {name, key, cols: {role: letters}}}, ``depth_unit``, ``flow_unit``,
    ``depth_factor`` (to m), ``flow_factor`` (to L/s), ``n_duty``, ``n_standby``, ``separators``.
    A bad value raises ValueError naming the TOML key.
    """
    from . import shared  # lazy: same template resolution as pressure_import / importer
    if path is None:
        path = shared.core("paths").find_resource("templates/import", TEMPLATE_NAME)
    text = Path(path).read_text(encoding="utf-8")
    raw = shared.core("tomlmini").loads(text)
    sheets = {}
    for sk, roles in _SHEET_ROLES.items():
        sec = (raw.get("sheets") or {}).get(sk)
        if not isinstance(sec, dict):
            raise _cfg_error(f"[sheets.{sk}]", "section missing")
        name = sec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _cfg_error(f"sheets.{sk}.name", "must be a non-empty sheet name")
        cols = {}
        for role in ("key",) + roles:
            v = sec.get(role)
            try:
                xlsx.col_index(v)
            except ValueError:
                raise _cfg_error(f"sheets.{sk}.{role}", f"{v!r} is not an Excel column letter (e.g. 'C', 'AB')") from None
            cols[role] = v.strip().upper()
        key = cols.pop("key")
        sheets[sk] = {"name": name.strip(), "key": key, "cols": cols}
    units = raw.get("units") or {}
    du = str(units.get("depth", "m")).strip()
    if du.lower() not in _DEPTH_FACTOR:
        raise _cfg_error("units.depth", f"{du!r} is not one of 'm', 'mm'")
    fu = str(units.get("flow", "L/s")).strip()
    if fu.lower() not in _FLOW_FACTOR:
        raise _cfg_error("units.flow", f"{fu!r} is not one of 'L/s', 'm3/h', 'ML/d'")
    pumps = raw.get("pumps") or {}
    counts = {}
    for k, lo, dflt in (("n_duty", 1, 1), ("n_standby", 0, 1)):
        v = pumps.get(k, dflt)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise _cfg_error(f"pumps.{k}", f"{v!r} must be a whole number >= {lo}")
        counts[k] = v
    seps = (raw.get("matching") or {}).get("sub_location_separators", "-/._ ")
    if not isinstance(seps, str) or not seps:
        raise _cfg_error("matching.sub_location_separators", "must be a non-empty string of separator characters")
    return {"sheets": sheets, "depth_unit": du, "flow_unit": fu,
            "depth_factor": _DEPTH_FACTOR[du.lower()], "flow_factor": _FLOW_FACTOR[fu.lower()],
            "n_duty": counts["n_duty"], "n_standby": counts["n_standby"], "separators": seps}


# ------------------------------------------------------------------ reading

_TEXT_MARKS = "'\u2018\u2019`"   # leading apostrophe / typographic quote of a number or ID stored as text


def _clean_text(v: str) -> str:
    """Strip spaces (incl. non-breaking) and a leading text marker: "'0.47" -> "0.47", "'S0037310" -> "S0037310".
    Excel keeps its own text-prefix apostrophe out of the cell value, but exported registers often store it."""
    v = v.replace("\u00a0", " ").strip()
    return v.lstrip(_TEXT_MARKS).strip()


def _number(v):
    """float, or a numeric string (stripped, a leading ' removed, thousands commas removed); anything else None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, str):
        v = _clean_text(v).replace(",", "")
        if not v:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _norm_fl(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _clean_text(str(v)).upper()


def read_register(xlsx_path, cfg) -> dict:
    """Read the three sheets -> {"sheets": {key: {"name", "rows": [{"row", "fl", "v": {role: float|None}}]}},
    "counts": {key: n data rows}, "warnings": [...]}. Row numbers are Excel rows. Missing sheet -> ValueError."""
    present = xlsx.sheet_names(xlsx_path)
    wanted = {}
    for sk, sc in cfg["sheets"].items():
        hit = next((n for n in present if n.strip().casefold() == sc["name"].strip().casefold()), None)
        if hit is None:
            raise ValueError(f"The register has no sheet named '{sc['name']}' (sheets present: "
                             f"{', '.join(repr(n) for n in present)}).")
        wanted[sk] = hit
    book = xlsx.read_workbook(xlsx_path, list(wanted.values()))
    out = {"sheets": {}, "counts": {}, "warnings": []}
    for sk, sc in cfg["sheets"].items():
        rows = []
        for rno, cells in book[wanted[sk]]:
            fl = _norm_fl(cells.get(sc["key"]))
            if not fl:
                continue
            vals = {role: _number(cells.get(col)) for role, col in sc["cols"].items()}
            if all(v is None for v in vals.values()):
                continue  # title / header / blank-data row
            rows.append({"row": rno, "fl": fl, "v": vals})
        out["sheets"][sk] = {"name": sc["name"], "rows": rows}
        out["counts"][sk] = len(rows)
        if not rows:
            out["warnings"].append(f"Sheet '{sc['name']}': no data rows found (functional location in column "
                                   f"{sc['key']} plus a number in one of the value columns).")
    return out


# ------------------------------------------------------------------ matching

def _matches(ps_id, fl, seps):
    """'exact', 'sub' or None."""
    if fl == ps_id:
        return "exact"
    if len(fl) > len(ps_id) and fl.startswith(ps_id) and fl[len(ps_id)] in seps:
        return "sub"
    return None


def _station_rows(rows, ps_id, seps, all_matches):
    ex, sub = [], []
    for r in rows:
        m = _matches(ps_id, r["fl"], seps)
        if m == "exact":
            ex.append(r)
        elif m == "sub":
            sub.append(r)
    if all_matches:
        return sorted(ex + sub, key=lambda r: r["row"])
    return ex or sub


# ------------------------------------------------------------------ derive (pure)

def _fmt(v, nd=2):
    return f"{v:.{nd}f}"


def derive(ps_id, levels, civil, pumps, cfg, source_name="register.xlsx", has_curve=False) -> dict:
    """Values and messages for one station from its matching rows (lists of read_register row dicts; the
    pumps list holds every matching pump row in sheet order). Returns {"values", "messages", "error", "line"};
    ``values`` holds set_station kwargs (wet_well_floor, ground_level, stop, start, overflow, wet_well_area_m2,
    emergency_storage_m3, duty_rate_lps, n_duty, n_standby, replace_pumps, source_note) plus info keys
    (design_head_m, diameter_m, storage_m3). A failed level check -> error True, no values, "ERROR:" message."""
    sh = cfg["sheets"]
    msgs: list = []
    vals: dict = {}

    def ref(sk, role, row):
        return f"{sh[sk]['name']} {sh[sk]['cols'][role]}{row}"

    def first(sk, rows):
        if not rows:
            msgs.append(f"{sh[sk]['name']}: no row for {ps_id}; that data not imported.")
            return None
        if len(rows) > 1:
            msgs.append(f"{sh[sk]['name']}: {len(rows)} rows match {ps_id} (rows "
                        f"{', '.join(str(r['row']) for r in rows)}); the first (row {rows[0]['row']}) is used.")
        return rows[0]

    # ---- levels
    lv = first("levels", levels)
    depths, ground, lrow = {}, None, None
    if lv:
        lrow = lv["row"]
        ground = lv["v"].get("ground")
        for role in ("base", "stop", "start", "alarm", "overflow"):
            v = lv["v"].get(role)
            if v is None:
                continue
            d = v * cfg["depth_factor"]
            if d < 0:
                msgs.append(f"{ref('levels', role, lrow)}: negative depth {v:g} {cfg['depth_unit']} ignored.")
                continue
            depths[role] = d
        present = [r for r in ("base", "stop", "start", "alarm", "overflow") if r in depths]
        for a, b in zip(present, present[1:]):
            if not depths[a] > depths[b]:
                msgs.insert(0, f"ERROR: {ref('levels', a, lrow)} {_LABEL[a]} ({depths[a]:.2f} m below top of slab) "
                               f"must be deeper than {ref('levels', b, lrow)} {_LABEL[b]} ({depths[b]:.2f} m); "
                               "nothing imported for this station.")
                return {"values": {}, "messages": msgs, "error": True, "line": f"{ps_id}: {msgs[0]}"}
        if "stop" in depths and "start" in depths and depths["stop"] - depths["start"] < _MIN_GAP_M:
            msgs.insert(0, f"ERROR: stop ({ref('levels', 'stop', lrow)}) to start ({ref('levels', 'start', lrow)}) "
                           f"gap is {depths['stop'] - depths['start']:.2f} m; it must be at least {_MIN_GAP_M} m; "
                           "nothing imported for this station.")
            return {"values": {}, "messages": msgs, "error": True, "line": f"{ps_id}: {msgs[0]}"}
        if ground is None:
            msgs.append(f"{ref('levels', 'ground', lrow)}: no design ground level; levels not imported.")
        else:
            vals["ground_level"] = round(ground, 3)
            for role, key in (("base", "wet_well_floor"), ("stop", "stop"), ("start", "start"),
                              ("overflow", "overflow")):
                if role in depths:
                    vals[key] = round(ground - depths[role], 3)
                else:
                    msgs.append(f"{ref('levels', role, lrow)}: no {_LABEL[role]}; not imported.")

    # ---- civil
    cv = first("civil", civil)
    if cv:
        r = cv["v"].get("wet_well_storage")
        if r is not None and r > 0 and "alarm" in depths and "overflow" in depths:
            span = depths["alarm"] - depths["overflow"]
            area = r / span
            dia = math.sqrt(4 * area / math.pi)
            vals["wet_well_area_m2"] = round(area, 4)
            vals["diameter_m"], vals["storage_m3"] = dia, r
            if dia < 1.0 or dia > 10.0:
                msgs.append(f"Derived wet well diameter {dia:.2f} m ({r:g} m3 over {span:.2f} m) is outside "
                            f"1-10 m: check {ref('civil', 'wet_well_storage', cv['row'])} and the alarm / "
                            "overflow depths.")
        else:
            why = []
            if r is None or r <= 0:
                why.append(f"no wet well storage ({ref('civil', 'wet_well_storage', cv['row'])})")
            if "alarm" not in depths:
                why.append("no high level alarm" + (f" ({ref('levels', 'alarm', lrow)})" if lrow else ""))
            if "overflow" not in depths:
                why.append("no overflow level" + (f" ({ref('levels', 'overflow', lrow)})" if lrow else ""))
            msgs.append("No wet well area derived: " + "; ".join(why) + ". An existing area is kept, otherwise "
                        "export / engine assume 7.07 m2.")
        e = cv["v"].get("emergency_storage")
        if e is not None:
            if e < 0:
                msgs.append(f"{ref('civil', 'emergency_storage', cv['row'])}: negative emergency storage ignored.")
            else:
                vals["emergency_storage_m3"] = e
    elif "wet_well_area_m2" not in vals:
        msgs.append("No wet well area derived (no Civil row). An existing area is kept, otherwise export / "
                    "engine assume 7.07 m2.")

    # ---- pumps
    prow = []
    for r in pumps:
        q = r["v"].get("design_flow")
        if q is not None and q * cfg["flow_factor"] > 0:
            prow.append((r["row"], q * cfg["flow_factor"], r["v"].get("design_head")))
    if not prow:
        msgs.append(f"{sh['pumps']['name']}: no pump row with a design flow for {ps_id}; "
                    "pump rate not imported and pumps not replaced.")
        vals["replace_pumps"] = False
    else:
        row0, q0, h0 = prow[0]
        vals["duty_rate_lps"] = round(q0, 4)
        if h0 is not None:
            vals["design_head_m"] = h0
        vals["n_duty"] = cfg["n_duty"]
        vals["n_standby"] = max(cfg["n_standby"], len(prow) - 1)
        vals["replace_pumps"] = True
        diff = [(r, q, h) for r, q, h in prow[1:]
                if abs(q - q0) > _SAME_TOL * q0
                or (h is None) != (h0 is None)
                or (h is not None and abs(h - h0) > _SAME_TOL * abs(h0))]
        if diff:
            txt = "; ".join(f"row {r}: {q:g} L/s" + (f" @ {h:g} m" if h is not None else "") for r, q, h in diff)
            msgs.append(f"{len(diff)} more pump row(s) with different test data not used ({txt}); "
                        f"only the first (row {row0}) sets the duty rate; future enhancement.")
        if has_curve:
            vals["replace_pumps"] = False
            vals.pop("n_duty"), vals.pop("n_standby")
            msgs.append("existing pump curve kept (pump set not replaced); duty rate updated.")
        note = f"{source_name}; design {q0:g} L/s" + (f" @ {h0:g} m" if h0 is not None else "")
        vals["source_note"] = note
    if "source_note" not in vals:
        vals["source_note"] = source_name
    return {"values": vals, "messages": msgs, "error": False, "line": _line(ps_id, vals)}


def _line(ps_id, v):
    parts = []
    lv = [f"{k} {_fmt(v[key])}" for k, key in (("floor", "wet_well_floor"), ("stop", "stop"),
                                                ("start", "start"), ("overflow", "overflow")) if key in v]
    if lv:
        parts.append(", ".join(lv) + " m AHD")
    if "wet_well_area_m2" in v:
        parts.append(f"wet well {_fmt(v['wet_well_area_m2'])} m2 ({_fmt(v['diameter_m'])} m dia) from "
                     f"{v['storage_m3']:g} m3 between alarm and overflow")
    if "emergency_storage_m3" in v:
        parts.append(f"emergency storage {v['emergency_storage_m3']:g} m3")
    if "duty_rate_lps" in v:
        cfgtxt = (f"{v['n_duty']} duty" + (f" + {v['n_standby']} standby" if v["n_standby"] else "")
                  if v.get("replace_pumps") else "existing pumps kept")
        head = f" (design head {v['design_head_m']:g} m)" if "design_head_m" in v else ""
        parts.append(f"{cfgtxt} at {v['duty_rate_lps']:g} L/s{head}")
    return f"{ps_id}: " + ("; ".join(parts) if parts else "nothing to import") + "."


# ------------------------------------------------------------------ apply

_SET_KEYS = ("wet_well_floor", "ground_level", "stop", "start", "overflow", "wet_well_area_m2",
             "emergency_storage_m3", "duty_rate_lps", "n_duty", "n_standby", "replace_pumps", "source_note")


def _model_stations(gpkg_path):
    con = sqlite3.connect(gpkg_path)
    try:
        ids = [r[0] for r in con.execute("SELECT ps_id FROM wwn_pump_stations ORDER BY ps_id")]
        curved = {r[0] for r in con.execute(
            "SELECT DISTINCT ps_id FROM wwn_pumps WHERE curve_json IS NOT NULL AND TRIM(curve_json) <> ''")}
    finally:
        con.close()
    return ids, curved


def apply(gpkg_path, xlsx_path, cfg=None, preview=False) -> dict:
    """Import the register into the model. Returns {"stations": [{ps_id, values, messages, status}],
    "unmatched_model": [ps_id], "n_register_stations", "n_register_only", "warnings", "lines"}.
    status: ok | error | skipped. preview=True validates everything and writes nothing.
    sqlite3.OperationalError (locked GeoPackage) propagates."""
    cfg = cfg or load_config()
    reg = read_register(xlsx_path, cfg)
    src = os.path.basename(str(xlsx_path))
    seps = cfg["separators"]
    model_ids, curved = _model_stations(gpkg_path)
    out = {"stations": [], "unmatched_model": [], "warnings": list(reg["warnings"]), "lines": []}
    for ps_id in model_ids:
        key = _norm_fl(ps_id)
        by_sheet = {}
        for sk in ("levels", "civil"):
            by_sheet[sk] = _station_rows(reg["sheets"][sk]["rows"], key, seps, False)
        by_sheet["pumps"] = _station_rows(reg["sheets"]["pumps"]["rows"], key, seps, True)
        if not any(by_sheet.values()):
            out["unmatched_model"].append(ps_id)
            continue
        d = derive(ps_id, by_sheet["levels"], by_sheet["civil"], by_sheet["pumps"], cfg, src,
                   has_curve=ps_id in curved)
        st = {"ps_id": ps_id, "values": d["values"], "messages": list(d["messages"]), "status": "ok"}
        line = d["line"]
        if d["error"]:
            st["status"] = "error"
        elif not any(k in d["values"] for k in _SET_KEYS if k != "source_note"):
            st["status"] = "skipped"
            st["messages"].append("Nothing usable in the register for this station; not changed.")
        else:
            kw = {k: d["values"][k] for k in _SET_KEYS if k in d["values"]}
            try:
                r = pumpdata.set_station(gpkg_path, ps_id, level_mode="AHD", dry_run=preview, **kw)
                st["messages"].extend(r["warnings"])
            except ValueError as e:
                st["status"] = "error"
                st["messages"].insert(0, f"ERROR: {e}")
                line = f"{ps_id}: ERROR: {e}"
        if st["status"] == "skipped":
            line = f"{ps_id}: skipped (nothing usable in the register)."
        out["stations"].append(st)
        out["lines"].append(line)
    keys = {_norm_fl(i) for i in model_ids}
    unclaimed = {r["fl"] for sk in reg["sheets"].values() for r in sk["rows"]
                 if not any(_matches(k, r["fl"], seps) for k in keys)}
    only = {_base(fl, seps) for fl in unclaimed}
    out["n_register_only"] = len(only)
    out["n_register_stations"] = len(only) + len(out["stations"])
    return out


def _base(fl, seps):
    for i, ch in enumerate(fl):
        if ch in seps and i > 0:
            return fl[:i]
    return fl
