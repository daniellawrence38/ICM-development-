"""Tool bodies for WWNet.pyt. Plain-value arguments in, messages out; arcpy injected.

Messages are short prose lines (the Messages pane uses a proportional font); full results live in
the model's result layers (Design results, Simulation results, Issues), which persist in the
GeoPackage and are reused when already in the map.
"""
from __future__ import annotations

import csv
import json
import os

import sqlite3

from . import (capacity, engine, gpenv, gpkg, maplayers, pressure_import, pump_register, pumpdata, shared,
               sewer_import, symbology, vendor)
from .messages import Messages

MAX_LINES = 300
MAX_ISSUE_LINES = 40
# Pump / rising main review thresholds (one place; printed in the Run Simulation messages).
# MAX_STARTS_PER_HOUR depends on the motor size (small submersibles tolerate more starts than large
# motors): treat it as a screening value and check against the station's own specification.
MAX_STARTS_PER_HOUR = 10.0
MIN_RM_VELOCITY = 0.7     # m/s: below this solids settle in a rising main (self-cleansing concern)
MAX_RM_VELOCITY = 2.0     # m/s: above this headloss and surge (water hammer) become a concern


SAVE_EDITS_NOTE = ("The engine reads the GeoPackage file: save any edits in Pro first "
                   "(Edit > Save) or they are not included.")


def _summary():
    return shared.core("summary")


def _checks():
    return shared.core("checks")


def _num(c, key, default=0.0):
    v = c.get(key)
    return default if v is None else v


def _gpkg_path(path: str) -> str:
    return path if path.lower().endswith(".gpkg") else path + ".gpkg"


# --------------------------------------------------------------- model tools

def new_model(arcpy, msgs: Messages, out_path: str, add_to_map: bool = True, overwrite: bool = False):
    out_path = _gpkg_path(out_path)
    gpkg.create_model(out_path, overwrite=overwrite)
    msgs.info(f"Created WWNet model {out_path} (EPSG:7850, schema v{shared.core('schema').SCHEMA_VERSION}).")
    if add_to_map:
        with gpenv.quiet(arcpy):
            maplayers.add_model(arcpy, out_path, msgs)
    return out_path


def add_model_to_map(arcpy, msgs: Messages, gpkg_path: str, symbolize: bool = True):
    with gpenv.quiet(arcpy):
        layers = maplayers.add_model(arcpy, gpkg_path, msgs, symbolize=symbolize)
    msgs.info(f"Added {len(layers)} layer(s) to the active map." if layers else
              "Every layer of this model is already in the active map.")
    return layers


def report_issues(msgs: Messages, issues: list, max_lines: int = MAX_LINES) -> dict:
    """Issues as messages, errors first. Model findings are data issues, not tool failures: never AddError
    (Pro would mark a finished tool as failed); error-severity ones are warnings prefixed ERROR:."""
    s = _summary()
    for n, i in enumerate(s.sorted_issues(issues)):
        if n >= max_lines:
            msgs.info(f"... {len(issues) - max_lines} more issue(s) in the model's Issues layer.")
            break
        line = s.issue_line(i)
        sev = i.get("severity")
        if sev == "error":
            msgs.warn(f"ERROR: {line}")
        elif sev == "warning":
            msgs.warn(line)
        else:
            msgs.info(line)
    return _checks().count_by_severity(issues)


def _show_issues_layer(arcpy, msgs, gpkg_path):
    try:
        with gpenv.quiet(arcpy):
            maplayers.ensure_layer(arcpy, gpkg_path, "wwn_issues", msgs)
    except Exception as exc:  # noqa: BLE001
        msgs.warn(f"Could not add the Issues layer to the map: {exc}")


def import_sewer_data(arcpy, msgs: Messages, ac_layer, cp_layer, sewer_layer, out_path: str,
                      fields: dict | None = None, snap_tolerance: float | None = None,
                      grade_source: str | None = None, use_selected: bool = False,
                      add_to_map: bool = True, native=None, tee_tolerance: float | None = None,
                      load_connection_points: bool = True, cp_load_search_m: float | None = None) -> dict:
    """Water Corporation gravity sewer layers -> new WWNet GeoPackage (see arcgis_toolbox/README.md).

    `*_layer` are map layers / paths / names; `fields` maps role -> source field name (missing roles
    use the template defaults)."""
    si = sewer_import
    imp = si.importer()
    native = native or si.native_with_write()
    out_path = _gpkg_path(out_path)
    exists = os.path.exists(out_path)
    overwrite = bool(getattr(getattr(arcpy, "env", None), "overwriteOutput", False))
    if exists and not overwrite:
        raise RuntimeError(f"{out_path} already exists. Choose a new name or enable 'Overwrite the outputs "
                           "of geoprocessing operations' in the Geoprocessing options / Environments.")
    msgs.start("Reading the source layers")
    with gpenv.quiet(arcpy):
        return _import_sewer_body(arcpy, msgs, imp, native, out_path, exists, overwrite, ac_layer, cp_layer,
                                  sewer_layer, fields, snap_tolerance, grade_source, use_selected, add_to_map,
                                  tee_tolerance, load_connection_points, cp_load_search_m)


def _import_sewer_body(arcpy, msgs, imp, native, out_path, exists, overwrite, ac_layer, cp_layer, sewer_layer,
                       fields, snap_tolerance, grade_source, use_selected, add_to_map, tee_tolerance,
                       load_connection_points, cp_load_search_m) -> dict:
    si = sewer_import
    cfg = imp.load_template()
    fmap = imp.FieldMap.from_config(cfg).with_overrides(**{k: (v or "") for k, v in (fields or {}).items()
                                                           if k in si.ROLE_LAYER})
    opts = imp.ImportOptions.from_config(cfg, snap_tolerance_m=snap_tolerance, grade_source=grade_source,
                                         tee_tolerance_m=tee_tolerance,
                                         load_connection_points=bool(load_connection_points),
                                         cp_load_search_m=(float(cp_load_search_m) if cp_load_search_m is not None
                                                           else None))
    if cp_layer is not None and not fmap.cp_id:
        raise RuntimeError("Choose the connection point ID field.")
    if not fmap.ac_id or not fmap.pipe_id:
        raise RuntimeError("The access chamber ID field and the sewer ID field are required.")
    sources = [("Access chambers", ac_layer), ("Gravity sewers", sewer_layer)]
    if cp_layer is not None:
        sources.insert(1, ("Connection points", cp_layer))
    if fmap.chamber_type and fmap.chamber_type.lower() not in {
            n.lower() for n in si.layer_field_names(arcpy, ac_layer)}:
        msgs.warn(f"Access chambers have no field {fmap.chamber_type!r}: every chamber is imported as an "
                  "access chamber (inspection shafts are not skipped).")
        fmap = fmap.with_overrides(chamber_type="")
    si.prepare_projection(arcpy, msgs, sources)
    sewer_names = {n.lower() for n in si.layer_field_names(arcpy, sewer_layer)}
    if fmap.shape_length and fmap.shape_length.lower() not in sewer_names:
        fmap = fmap.with_overrides(shape_length="")          # optional diagnostic field absent: skip it
    roles = {k: si.roles_for(k, {r: getattr(fmap, r) for r, *_ in si.FIELD_ROLES})
             for k in ("ac", "cp", "sewer")}
    if fmap.shape_length:
        roles["sewer"]["shape_length"] = fmap.shape_length
    sewers = si.read_features(arcpy, sewer_layer, roles["sewer"], "line", "Gravity sewers",
                              use_selected, msgs)
    if not sewers:
        raise RuntimeError("No gravity sewers were read (nothing selected / empty layer?).")
    ext = si.rows_extent(sewers)
    sewers_selected = use_selected and si.selection_count(sewer_layer) > 0
    refs = {imp.to_id(r.get(f)) for r in sewers for f in (fmap.us_node, fmap.ds_node) if f}
    refs.discard("")
    acs = None
    if sewers_selected and not si.selection_count(ac_layer) and ext is not None:
        acs = si.read_in_extent(arcpy, ac_layer, roles["ac"], "point", "Access chambers", ext, msgs)
        if acs is None:
            msgs.warn("Access chambers: reading all features instead.")
        else:
            msgs.info(f"Access chambers: nothing selected; read {len(acs)} feature(s) within the selected "
                      f"sewers' extent +{si.EXTENT_BUFFER_M:g} m.")
            for r in acs:
                r[imp.CONTEXT] = True            # nothing selected: all of these are context chambers
            acs = si.add_referenced_chambers(arcpy, ac_layer, roles["ac"], fmap.ac_id, refs, acs, msgs,
                                             extent_too=False, n_selected=0)
    elif use_selected and si.selection_count(ac_layer) and ext is not None:
        acs = si.read_features(arcpy, ac_layer, roles["ac"], "point", "Access chambers", True, msgs)
        n_sel = len(acs)
        acs = si.add_referenced_chambers(arcpy, ac_layer, roles["ac"], fmap.ac_id, refs, acs, msgs, ext=ext,
                                         n_selected=n_sel)
    if acs is None:
        acs = si.read_features(arcpy, ac_layer, roles["ac"], "point", "Access chambers", use_selected, msgs)
    cps = []
    if cp_layer is not None:
        ac_ids = {imp.to_id(r.get(fmap.ac_id)) for r in acs}
        # Loads need every connection point near a sewer: the extent grows by the search radius
        # (spatial filter; the layer is never scanned in full).
        buf = max(si.EXTENT_BUFFER_M, opts.cp_load_search_m) if opts.load_connection_points \
            else si.EXTENT_BUFFER_M
        cp_ext = si.rows_extent(sewers, buf)
        cps = si.read_connection_points(arcpy, cp_layer, roles["cp"], fmap.cp_id, sorted(refs - ac_ids), cp_ext,
                                        use_selected, msgs, buffer_m=buf)
    elif load_connection_points:
        msgs.info("No connection point layer given: no connection point loads were created.")
    msgs.step("Building the network (connectivity, direction, tees)")
    model, rep = imp.build_model(acs, cps, sewers, fmap, opts, cfg)
    si.report_to_messages(msgs, rep)
    msgs.step("Writing the GeoPackage")
    native.write_model(out_path, imp.model_json(model), exists and overwrite)
    msgs.info(f"Wrote WWNet model {out_path} (EPSG:7850).")
    native.store_issues(out_path, "import", json.dumps(rep.issues(), default=str))
    msgs.step("Checking the model")
    norm = _checks().normalise_issue
    issues = [norm(i) for i in native.check(out_path, True)]
    report_issues(msgs, issues, si.MAX_ISSUE_LINES)
    msgs.info(_summary().issue_count_line(issues) +
              (f" Import warnings: {len(rep.warnings)}." if rep.warnings else "") +
              " Both are in the model's Issues layer.")
    if add_to_map:
        try:
            add_model_to_map(arcpy, msgs, out_path, True)
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not add the model to the active map: {exc}")
    return {"path": out_path, "report": rep, "issues": issues, "model": model}


def sewer_field_defaults(arcpy, layer) -> dict:
    """role -> Water Corporation default field name that exists in `layer` ('' if absent)."""
    return sewer_import.pick_defaults(sewer_import.layer_field_names(arcpy, layer),
                                      sewer_import.template_defaults())


def pressure_field_defaults(arcpy, layer, layer_key: str) -> dict:
    """role -> Water Corporation default field name present in `layer` ('' if absent), for the roles of
    `layer_key` ('ps' | 'main')."""
    pi = pressure_import
    names = sewer_import.layer_field_names(arcpy, layer)
    defaults = {r: d for r, d in pi.template_defaults().items() if pi.ROLE_LAYER[r] == layer_key}
    return sewer_import.pick_defaults(names, defaults)


def import_pressure_data(arcpy, msgs: Messages, gpkg_path: str, ps_layer, main_layer, fields: dict | None = None,
                         use_selected: bool = True, search_distance: float | None = None,
                         snap_tolerance: float | None = None, discharge_distance: float | None = None,
                         wet_well_distance: float | None = None,
                         include_not_in_use: bool = False, add_to_map: bool = True) -> dict:
    """Water Corporation pump stations + pressure mains -> wwn_pump_stations / wwn_rising_mains (+ nodes) of an
    existing model (see arcgis_toolbox/README.md). Re-running replaces the same ps_ids."""
    pi = pressure_import
    if not gpkg_path or not os.path.exists(gpkg_path):
        raise RuntimeError(f"The model {gpkg_path!r} does not exist. Run Import Sewer Data (or New Model) first.")
    if not pi.model_has_tables(gpkg_path):
        raise RuntimeError(f"{gpkg_path} is not a WWNet model (no wwn_nodes / wwn_conduits tables).")
    defaults = pi.template_options()

    def num(v, key, fallback):
        return float(v) if v is not None else float(defaults.get(key, fallback))
    opts = pi.Options(search_m=num(search_distance, "search_distance_m", 30.0),
                      snap_m=num(snap_tolerance, "snap_tolerance_m", 1.0),
                      discharge_m=num(discharge_distance, "discharge_distance_m", 1.0),
                      wet_well_m=num(wet_well_distance, "wet_well_distance_m", 1.0),
                      include_not_in_use=bool(include_not_in_use))
    roles_fields = {r: d for r, d in pi.template_defaults().items()}
    roles_fields.update({k: (v or "") for k, v in (fields or {}).items() if k in pi.ROLE_LAYER})
    if not roles_fields.get("ps_id"):
        raise RuntimeError("Choose the pump station ID field.")
    with gpenv.quiet(arcpy):
        sewer_import.prepare_projection(arcpy, msgs, [("Pump stations", ps_layer), ("Pressure mains", main_layer)])
        for key, layer, label in (("ps", ps_layer, "Pump stations"), ("main", main_layer, "Pressure mains")):
            have = {n.lower() for n in sewer_import.layer_field_names(arcpy, layer)}
            for role, k, _l, _q in pi.FIELD_ROLES:
                f = roles_fields.get(role)
                if k == key and f and f.lower() not in have:
                    if role == "ps_id":
                        raise RuntimeError(f"{label}: field {f!r} (role {role}) does not exist. "
                                           f"Available: {', '.join(sorted(have))}")
                    msgs.warn(f"{label}: field {f!r} (role {role}) does not exist; that role is not used.")
                    roles_fields[role] = ""
        model = pi.read_model(gpkg_path)
        mext = model["extent"]
        ext = None
        if mext is not None:
            b = max(opts.search_m, opts.wet_well_m) + opts.discharge_m
            ext = (mext[0] - b, mext[1] - b, mext[2] + b, mext[3] + b)
        ps_rows, main_rows = pi.read_sources(arcpy, msgs, ps_layer, main_layer, roles_fields, ext, use_selected, opts)
        if not ps_rows:
            raise RuntimeError("No pump stations were read (nothing selected / none within the model area?).")
        res = pi.build_pressure(ps_rows, main_rows, model["nodes"], model["conduits"], opts,
                                {k.upper(): v for k, v in pi.template_config().get("codes", {})
                                 .get("material", {}).items()})
        try:
            stats = pi.write_result(gpkg_path, res)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not write to {gpkg_path}: {exc}. Close other users of the file (remove its "
                               "layers from maps, stop edits) and run the tool again.") from exc
        for line in res.infos:
            msgs.info(line)
        for w in res.warnings[:sewer_import.MAX_WARNING_LINES]:
            msgs.warn(w)
        if len(res.warnings) > sewer_import.MAX_WARNING_LINES:
            msgs.warn(f"... {len(res.warnings) - sewer_import.MAX_WARNING_LINES} more warning(s) not shown.")
        msgs.info(f"Imported {len(res.pump_stations)} pump station(s) and {len(res.rising_mains)} rising main(s); "
                  f"{len(res.new_nodes)} node(s) created, {len(res.retype_nodes)} gravity node(s) set to wet_well.")
        if stats["replaced_pump_stations"] or stats["replaced_rising_mains"]:
            msgs.info(f"Replaced earlier import: {stats['replaced_pump_stations']} pump station(s), "
                      f"{stats['replaced_rising_mains']} rising main(s), {stats['deleted_nodes']} node(s). "
                      "Hydraulic values and pumps entered earlier were kept.")
        missing = pi.missing_hydraulic(gpkg_path, [p["ps_id"] for p in res.pump_stations])
        if missing:
            shown = ", ".join(missing[:pi.MAX_LIST]) + (f" ... (+{len(missing) - pi.MAX_LIST} more)"
                                                        if len(missing) > pi.MAX_LIST else "")
            msgs.warn("Enter wet_well_area_m2, start_level, stop_level, pump_rate_lps (or wwn_pumps rows with "
                      f"curve_json) for: {shown}")
        if add_to_map:
            try:
                maplayers.add_model(arcpy, gpkg_path, msgs, True,
                                    only=("wwn_nodes", "wwn_pump_stations", "wwn_rising_mains", "wwn_pumps"),
                                    skip_existing=True)
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add the layers to the active map: {exc}")
        return {"path": gpkg_path, "result": res, "missing": missing, "stats": stats}


def station_report_lines(gpkg_path: str) -> list:
    """pumpdata.station_report for the model; [] when it has no pump stations or cannot be read."""
    try:
        return pumpdata.station_report(gpkg_path)
    except Exception:  # noqa: BLE001 - older / non-WWNet files: nothing to report
        return []


def set_pump_station_data(arcpy, msgs: Messages, gpkg_path: str, ps_id: str, **values):
    """Write pump station data typed in from the data sheet (see pumpdata.set_station).

    `values`: wet_well_diameter_m, wet_well_area_m2, level_mode, start, stop, overflow,
    emergency_storage_m3, n_duty, n_assist, n_standby, duty_rate_lps, curve_text, curve_order,
    flow_units, speed_pct, source_note, replace_pumps, wet_well_floor. Data errors are reported as an "ERROR:" warning
    (never AddError: that would mark the tool as failed) and None is returned."""
    v = dict(values)
    curve_text = v.pop("curve_text", None)
    order = v.pop("curve_order", None) or "flow_head"
    units = v.pop("flow_units", None) or "L/s"
    try:
        curve, curve_warnings = None, []
        if curve_text and str(curve_text).strip():
            curve, curve_warnings = pumpdata.parse_curve(curve_text, order=order, flow_units=units)
        result = pumpdata.set_station(gpkg_path, ps_id, curve=curve, **v)
    except ValueError as exc:
        msgs.warn(f"ERROR: {exc}")
        return None
    except sqlite3.OperationalError as exc:
        if _warn_if_locked(msgs, exc):
            return None
        raise
    msgs.info(result["summary"])
    for w in list(curve_warnings) + list(result["warnings"]):
        msgs.warn(w)
    for line in station_report_lines(gpkg_path):
        msgs.info(line)
    return result


def _warn_if_locked(msgs: Messages, exc: Exception) -> bool:
    """True (after an "ERROR:" warning) when `exc` says the GeoPackage is locked / busy / read-only."""
    text = str(exc).lower()
    if "locked" in text or "busy" in text or "readonly" in text or "read-only" in text:
        msgs.warn("ERROR: The model GeoPackage is locked by ArcGIS Pro. Close any open attribute tables "
                  "and remove the model's layers from the map (or use a copy), then run the tool again.")
        return True
    return False


def import_pump_station_data(arcpy, msgs: Messages, gpkg_path: str, register: str, config: str | None = None,
                             preview: bool = False):
    """Import wet well levels, area, emergency storage and the design pump rate from the pump station register
    workbook (see pump_register). `preview` validates and reports without writing. Data errors are "ERROR:"
    warnings (never AddError); returns the pump_register.apply result, or None after such an error."""
    if preview:
        msgs.info("PREVIEW — nothing written.")
    try:
        cfg = pump_register.load_config(config or None)
        res = pump_register.apply(gpkg_path, register, cfg, preview=bool(preview))
    except ValueError as exc:
        msgs.warn(f"ERROR: {exc}")
        return None
    except sqlite3.OperationalError as exc:
        if _warn_if_locked(msgs, exc):
            return None
        raise
    stations = res["stations"]
    msgs.info(f"Register: {os.path.basename(str(register))}; matched {len(stations)} of "
              f"{len(stations) + len(res['unmatched_model'])} model pump station(s); {res['n_register_only']} "
              "other station(s) in the register (not in this model).")
    for w in res["warnings"]:
        msgs.warn(w)
    for st, line in zip(stations, res["lines"]):
        (msgs.info if st["status"] == "ok" else msgs.warn)(line)
        for m in st["messages"]:
            if m == line or line.endswith(m):
                continue
            msgs.warn(m if m.startswith("ERROR:") else f"{st['ps_id']}: {m}")
    if res["unmatched_model"]:
        msgs.warn("Model pump station(s) not in the register: " + ", ".join(res["unmatched_model"]) + ".")
    n = {k: sum(1 for st in stations if st["status"] == k) for k in ("ok", "error", "skipped")}
    msgs.info(f"Imported {n['ok']} station(s), {n['error']} with errors, {n['skipped']} skipped.")
    if n["ok"] and not preview:
        msgs.info("Next: Export To ICM writes their pumps to ICM_Pumps (fixed-rate pump at the design flow).")
    return res


def _table_lines(header: tuple, rows: list) -> list:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = " | ".join("{:<%d}" % w for w in widths)
    return [fmt.format(*header).rstrip(), "-+-".join("-" * w for w in widths)] + [fmt.format(*r).rstrip() for r in rows]


def join_dangling_ends(arcpy, msgs: Messages, gpkg_path: str, max_distance_m: float | None = 5.0,
                       preview: bool = True):
    """Join dangling sewer ends (junction / is_end / io_end with an incoming sewer and no outgoing one) to the
    nearest sewer within `max_distance_m`: the main is split at the tee (or the branch joins an existing end
    node when the projection is within 0.5 m of it). `preview` only reports. The core is sqlite only (no arcpy
    layer is touched); returns the core result dict, or None after an "ERROR:" warning."""
    repair = shared.core("repair")
    max_d = 5.0 if max_distance_m is None else float(max_distance_m)
    if not gpkg_path or not os.path.isfile(gpkg_path):
        msgs.warn(f"ERROR: model GeoPackage not found: {gpkg_path}")
        return None
    if max_d <= 0:
        msgs.warn("ERROR: the maximum distance must be greater than 0 m.")
        return None
    try:
        res = repair.join_dangling_ends(gpkg_path, max_d, preview=bool(preview))
    except repair.RepairError as exc:
        msgs.warn(f"ERROR: {exc}")
        return None
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text or "readonly" in text or "read-only" in text:
            msgs.warn("ERROR: The model GeoPackage is locked by ArcGIS Pro. Close any open attribute tables "
                      "and remove the model's layers from the map (or use a copy), then run the tool again.")
            return None
        raise
    joins, skipped = res["joins"], res["skipped"]
    verb = "would be joined" if preview else "joined"
    msgs.info(f"{len(joins)} dangling end(s) {verb}, {len(skipped)} not joined (maximum distance {max_d:g} m)"
              + (" - PREVIEW ONLY, nothing was changed." if preview else "."))
    rows = []
    for j in joins:
        main = j["main_conduit"]
        to = (f"{j['tee_node']} (existing end of {main})" if j["action"] == "join_node"
              else f"{main} (tee {j['tee_node']}, new pipe {j['new_conduit']})")
        inv = "n/a" if j.get("invert_at_tee") is None else f"{j['invert_at_tee']:.3f}"
        rows.append((j["dangling_node"], ", ".join(j["branch_conduits"]), to, f"{j['distance_m']:.2f}", inv,
                     j["note"]))
    if rows:
        for line in _table_lines(("Dangling node", "Branch", "Joined to", "Distance m", "Invert at tee", "Note"), rows):
            msgs.info(line)
        for j in joins:
            for w in j.get("warnings", []):
                msgs.warn(f"{j['dangling_node']}: {w}")
            for ch in j.get("branch_changes", []):
                msgs.info(f"{j['dangling_node']}: branch {ch['conduit']} extended by {ch['gap_m']:.2f} m "
                          f"(length_m now {ch['length_m']:.2f})")
    if skipped:
        msgs.info("Not joined:")
        for s in skipped:
            msgs.warn(f"{s['dangling_node']} ({', '.join(s.get('branch_conduits', []))}): {s.get('reason', '')}")
    if not joins and not skipped:
        msgs.info("No dangling sewer ends found.")
    if joins and not preview:
        msgs.info("The model changed on disk: refresh or re-add its layers in the map.")
    elif joins:
        msgs.info("Untick 'Preview only' to apply these joins.")
    msgs.info("Re-run Check Model / Export To ICM.")
    return res


# --------------------------------------------------------------- analysis

def check_model(arcpy, msgs: Messages, gpkg_path: str, show_layer: bool = True) -> list:
    msgs.info(SAVE_EDITS_NOTE)
    msgs.start("Checking the model")
    issues = engine.run_check(gpkg_path)
    report_issues(msgs, issues)
    for line in station_report_lines(gpkg_path):
        msgs.info(f"Pump station data: {line}")
    msgs.info(_summary().issue_count_line(issues, "Check finished"))
    if show_layer:
        _show_issues_layer(arcpy, msgs, gpkg_path)
    return issues


def run_design(arcpy, msgs: Messages, gpkg_path: str, show_layer: bool = True,
               select_failed: bool = True) -> dict:
    s = _summary()
    msgs.info(SAVE_EDITS_NOTE)
    msgs.start("Running the DS 50 design")
    d = engine.run_design(gpkg_path)
    for line in s.design_summary_lines(d):
        msgs.info(line)
    worst = s.design_worst_lines(d["conduit_results"])
    if worst:
        msgs.info("Worst conduits:")
        for line in worst:
            msgs.warn(line)
    for line in s.design_detail_lines(d["conduit_results"]):
        msgs.warn(line)
    for line in s.design_info_lines(d):
        msgs.info(line)
    other = [i for i in d.get("issues", []) if i.get("object_type") != "conduit" and i.get("severity") != "info"]
    for i in s.sorted_issues(other)[:MAX_ISSUE_LINES]:
        msgs.warn(s.issue_line(i))
    if len(other) > MAX_ISSUE_LINES:
        msgs.info(f"... {len(other) - MAX_ISSUE_LINES} more in the Issues layer.")
    if show_layer:
        msgs.step("Updating the Design results layer")
        try:
            with gpenv.quiet(arcpy):
                lyr = maplayers.ensure_layer(arcpy, gpkg_path, "wwn_results_conduits", msgs)
                if select_failed:
                    failed = s.failed_conduits(d)
                    maplayers.select(arcpy, lyr, failed, "conduit_id")
                    msgs.info(f"Selected {len(failed)} failed conduit(s) on the Design results layer." if failed
                              else "No failed conduits.")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not show the Design results layer: {exc}")
    msgs.info("Full per-conduit results: the model's Design results layer (wwn_results_conduits).")
    return d


def run_simulation(arcpy, msgs: Messages, gpkg_path: str, duration_h: float = 48.0, warmup_h: float = 24.0,
                   roughness: str = "ks", manning_n: float = 0.013, flow_scale: float = 1.0,
                   show_layer: bool = True, export_inp: str | None = None, label: str = "",
                   series_from_selection: bool = True) -> dict:
    """Run the SWMM dynamic simulation, report and show the Simulation results layers."""
    s = _summary()
    msgs.info(SAVE_EDITS_NOTE)
    series = []
    if series_from_selection:
        try:
            series = maplayers.selected_ids(arcpy, gpkg_path, "wwn_conduits", "conduit_id")
        except Exception:  # noqa: BLE001 - no map / layer: just the default series
            series = []
        if series:
            msgs.info(f"Storing time series for the {len(series)} selected conduit(s) as well (for Plot "
                      "Hydrograph).")
    msgs.start(f"Running the SWMM engine ({duration_h:g} h)")
    d = engine.run_simulate(gpkg_path, duration_h, warmup_h, roughness, manning_n, flow_scale,
                            export_inp or None, label, series)
    for line in s.sim_summary_lines(d):
        msgs.info(line)
    msgs.info("Highest peak d/D:")
    for line in s.sim_worst_lines(d["links"]):
        msgs.info(line)
    for line in pump_table_lines(d, _station_levels(gpkg_path)):
        msgs.info(line)
    for _ps, _kind, text in pump_flags(d):
        msgs.warn(text)
    issues = [i for i in d.get("issues", []) if i.get("severity") != "info"]
    for i in issues[:MAX_ISSUE_LINES]:
        msgs.warn(s.issue_line(i))
    if not d.get("issues") and d.get("warnings"):          # older engine
        for w in d["warnings"][:MAX_ISSUE_LINES]:
            msgs.warn(w)
    if export_inp:
        msgs.info(f"SWMM input file written: {export_inp}")
    if show_layer:
        msgs.step("Updating the Simulation results layers")
        _show_sim_layers(arcpy, msgs, gpkg_path, d)
    msgs.info("Stored as run " + s.run_title(d) + "; earlier runs: Show Simulation Run / Compare Simulation Runs.")
    return d


def _show_sim_layers(arcpy, msgs, gpkg_path, d: dict | None = None):
    """The persistent conduit / node result layers, and (for a run just made, `d`) a temporary pump
    station layer coloured by the pump flags."""
    try:
        with gpenv.quiet(arcpy):
            maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_conduit_results", msgs)
            maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_node_results", msgs,
                                   definition_query=symbology.SIM_NODES_QUERY)
            if d is not None:
                pname = f"Simulation pump stations \u2014 {_safe_stem(gpkg_path)}"
                maplayers.remove_layers_named(arcpy, pname)
                if d.get("pump_stations"):
                    pfc = build_sim_pumps_fc(arcpy, gpkg_path, d)
                    play = maplayers.add_results_layer(arcpy, pfc, pname, msgs)
                    symbology.apply("sim_pumps", play, msgs)
                    msgs.info(f"Pump station layer added: {pname} ({len(d['pump_stations'])} station(s); "
                              "red = flagged; temporary)")
    except Exception as exc:  # noqa: BLE001
        msgs.warn(f"Could not show the Simulation results layers: {exc}")


def show_simulation_run(arcpy, msgs: Messages, gpkg_path: str, run: str = "latest",
                        show_layer: bool = True) -> dict:
    s = _summary()
    r = engine.show_sim_run(gpkg_path, engine.run_key(run))
    msgs.info(f"Showing run {s.run_title(r)} in the Simulation results layers.")
    summ = r.get("summary") or {}
    if summ:
        msgs.info(f"Peak outlet flow {summ.get('peak_outlet_flow_lps', 0):.3f} L/s; "
                  f"{summ.get('surcharged_conduits', 0)} conduit(s) surcharged; "
                  f"{summ.get('flooded_nodes', 0)} node(s) flooded.")
    for w in (r.get("warnings") or [])[:MAX_ISSUE_LINES]:
        msgs.warn(w)
    if show_layer:
        _show_sim_layers(arcpy, msgs, gpkg_path)
    return r


def compare_simulation_runs(arcpy, msgs: Messages, gpkg_path: str, run_a: str = "previous",
                            run_b: str = "latest", show_layer: bool = True) -> dict:
    c = engine.compare_sim_runs(gpkg_path, engine.run_key(run_a), engine.run_key(run_b), True)
    lines = _summary().compare_lines(c)
    for line in lines[:2]:
        msgs.info(line)
    if len(lines) > 2:
        msgs.info("Largest d/D increases:")
        for line in lines[2:]:
            msgs.warn(line) if "NEW SURCHARGE" in line else msgs.info(line)
    if show_layer:
        try:
            with gpenv.quiet(arcpy):
                maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_compare", msgs)
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not show the Simulation comparison layer: {exc}")
    return c


def _safe_stem(gpkg_path: str) -> str:
    import os
    import re
    stem = os.path.splitext(os.path.basename(gpkg_path))[0]
    return re.sub(r"\W", "_", stem) or "model"


def _spatial_ref(arcpy, gpkg_path: str):
    try:
        return arcpy.Describe(gpkg.table_path(gpkg_path, "wwn_conduits")).spatialReference
    except Exception:  # noqa: BLE001
        return arcpy.SpatialReference(7850)


def _memory_fc(arcpy, gpkg_path: str, prefix: str, geom: str, fields: list) -> str:
    fc_name = f"{prefix}_{_safe_stem(gpkg_path)}"
    fc = "memory\\" + fc_name
    if arcpy.Exists(fc):
        arcpy.management.Delete(fc)
    arcpy.management.CreateFeatureclass("memory", fc_name, geom, spatial_reference=_spatial_ref(arcpy, gpkg_path))
    for name, typ in fields:
        if typ == "TEXT":
            arcpy.management.AddField(fc, name, "TEXT", field_length=64)
        else:
            arcpy.management.AddField(fc, name, typ)
    return fc


SIM_PUMP_FIELDS = [("ps_id", "TEXT"), ("max_level_m", "DOUBLE"), ("overflow_m3", "DOUBLE"),
                   ("starts_per_hour", "DOUBLE"), ("run_time_pct", "DOUBLE"),
                   ("rm_max_velocity_mps", "DOUBLE"), ("flag", "TEXT")]


def pump_flags(d: dict) -> list:
    """[(ps_id, kind, text)] review flags for the pump results of a simulate result: kind is one of
    'overflow', 'starts', 'v_low', 'v_high'. Thresholds: MAX_STARTS_PER_HOUR, MIN/MAX_RM_VELOCITY."""
    out = []
    for s in d.get("pump_stations", []):
        if _num(s, "overflow_volume_m3") > 0:
            out.append((s["ps_id"], "overflow",
                        f"{s['ps_id']}: wet well overflow {_num(s, 'overflow_volume_m3'):.2f} m3"))
    for p in d.get("pumps", []):
        sph = _num(p, "starts_per_hour")
        if sph > MAX_STARTS_PER_HOUR:
            out.append((p.get("ps_id"), "starts",
                        f"{p.get('ps_id')}: pump {p['pump_id']} {sph:.1f} starts/h exceeds {MAX_STARTS_PER_HOUR:g} "
                        "(limit depends on motor size: check against the station specification)"))
    for m in d.get("rising_mains", []):
        v = _num(m, "max_velocity_mps")
        if v < MIN_RM_VELOCITY:
            out.append((m.get("ps_id"), "v_low",
                        f"{m.get('ps_id')}: rising main {m['rm_id']} peak velocity {v:.2f} m/s < {MIN_RM_VELOCITY:g} "
                        "m/s (sediment / self-cleansing concern)"))
        elif v > MAX_RM_VELOCITY:
            out.append((m.get("ps_id"), "v_high",
                        f"{m.get('ps_id')}: rising main {m['rm_id']} peak velocity {v:.2f} m/s > {MAX_RM_VELOCITY:g} "
                        "m/s (headloss / surge concern)"))
    return out


def _station_levels(gpkg_path: str) -> dict:
    """{ps_id: (start, stop, overflow)} (m AHD) from wwn_pump_stations; {} when unreadable."""
    try:
        con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
        try:
            return {r[0]: (r[1], r[2], r[3]) for r in con.execute(
                "SELECT ps_id, start_level, stop_level, overflow_level FROM wwn_pump_stations")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _lv(v) -> str:
    return "-" if v is None else f"{float(v):.2f}"


def pump_table_lines(d: dict, levels: dict | None = None) -> list:
    """Pump station / pump / rising main tables for the messages (empty when the run has no stations)."""
    stations = d.get("pump_stations", [])
    if not stations:
        return []
    levels = levels or {}
    lines = ["Pump stations (levels m AHD)"]
    hdr = f"{'PS':<10}{'Max lvl':>8}{'Start':>8}{'Stop':>8}{'Overflow':>9}{'Ovfl m3':>9}  Max at"
    lines += [hdr, "-" * len(hdr)]
    for s in stations:
        st, sp, ov = levels.get(s["ps_id"], (None, None, None))
        lines.append(f"{str(s['ps_id'])[:10]:<10}{_lv(s.get('max_level_m')):>8}{_lv(st):>8}{_lv(sp):>8}"
                     f"{_lv(ov):>9}{_num(s, 'overflow_volume_m3'):>9.2f}  {s.get('time_of_max_level') or '-'}")
    pumps = d.get("pumps", [])
    if pumps:
        hdr = f"{'Pump':<12}{'PS':<8}{'Role':<9}{'Starts':>7}{'Starts/h':>9}{'Run %':>7}{'Qmax L/s':>10}"
        lines += ["Pumps", hdr, "-" * len(hdr)]
        for p in pumps:
            lines.append(f"{str(p['pump_id'])[:12]:<12}{str(p.get('ps_id') or '')[:8]:<8}"
                         f"{str(p.get('role') or '')[:9]:<9}{int(_num(p, 'starts')):>7}"
                         f"{_num(p, 'starts_per_hour'):>9.1f}{_num(p, 'run_time_pct'):>7.1f}"
                         f"{_num(p, 'max_flow_lps'):>10.2f}")
    rms = d.get("rising_mains", [])
    if rms:
        hdr = f"{'Rising main':<12}{'PS':<8}{'ID mm':>7}{'Length m':>10}{'Qmax L/s':>10}{'v m/s':>7}"
        lines += ["Rising mains", hdr, "-" * len(hdr)]
        for m in rms:
            lines.append(f"{str(m['rm_id'])[:12]:<12}{str(m.get('ps_id') or '')[:8]:<8}"
                         f"{_num(m, 'diameter_mm'):>7.0f}{_num(m, 'length_m'):>10.1f}"
                         f"{_num(m, 'max_flow_lps'):>10.2f}{_num(m, 'max_velocity_mps'):>7.2f}")
    lines.append(f"Pump flags: wet well overflow; starts/h > {MAX_STARTS_PER_HOUR:g}; rising main velocity "
                 f"< {MIN_RM_VELOCITY:g} or > {MAX_RM_VELOCITY:g} m/s")
    return lines


def build_sim_pumps_fc(arcpy, gpkg_path: str, d: dict) -> str:
    """memory\\wwnet_simpumps_<stem> (POINT): one row per pump station with its pump / rising main maxima."""
    fc = _memory_fc(arcpy, gpkg_path, "wwnet_simpumps", "POINT", SIM_PUMP_FIELDS)
    by_id = {s["ps_id"]: s for s in d.get("pump_stations", [])}
    kinds: dict = {}
    for ps, kind, _t in pump_flags(d):
        kinds.setdefault(ps, []).append(kind)
    cols = [f for f, _ in SIM_PUMP_FIELDS] + ["SHAPE@"]
    with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_pump_stations"), ["ps_id", "SHAPE@"]) as src, \
            arcpy.da.InsertCursor(fc, cols) as ins:
        for ps, shape in src:
            s = by_id.get(ps)
            if s is None:
                continue
            pumps = [p for p in d.get("pumps", []) if p.get("ps_id") == ps]
            rms = [m for m in d.get("rising_mains", []) if m.get("ps_id") == ps]
            ins.insertRow([ps, _num(s, "max_level_m"), _num(s, "overflow_volume_m3"),
                           max([_num(p, "starts_per_hour") for p in pumps] or [0.0]),
                           max([_num(p, "run_time_pct") for p in pumps] or [0.0]),
                           max([_num(m, "max_velocity_mps") for m in rms] or [0.0]),
                           ",".join(kinds.get(ps, [])) or "ok", shape])
    return fc


def _hhmm(minute) -> str:
    m = int(round(float(minute))) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def plot_hydrograph(arcpy, msgs: Messages, gpkg_path: str, run: str = "latest", ids=None,
                    out_csv: str | None = None, chart: bool = True) -> dict:
    """Stored flow / depth series of conduits (or nodes) in a run: peaks in the messages, optional CSV,
    and a line chart on the model's series table (best effort)."""
    res = engine.sim_series(gpkg_path, engine.run_key(run), list(ids or []))
    title = _summary().run_title(res["run"])
    if res.get("missing"):
        msgs.warn(f"No stored series for {', '.join(res['missing'])} in run {title}. Series are stored for the "
                  "5 conduits nearest the outlet and for conduits selected when the run was made; available: "
                  + (", ".join(res.get("available") or []) or "none"))
    if not res["series"]:
        raise RuntimeError(f"No time series to plot in run {title}.")
    for sr in res["series"]:
        q = [v for v in sr["flow_lps"] if v is not None]
        if not q:
            continue
        k = sr["flow_lps"].index(max(q))
        msgs.info(f"{sr['object_type']} {sr['object_id']} ({title}): peak {max(q):.3f} L/s at "
                  f"{_hhmm(sr['minutes'][k])} (time of day after the warm-up), minimum {min(q):.3f} L/s, "
                  f"mean {sum(q) / len(q):.3f} L/s, {len(q)} samples.")
    if out_csv:
        if not out_csv.lower().endswith(".csv"):
            out_csv += ".csv"
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["run", "object_type", "object_id", "minute", "time", "flow_lps", "depth_m"])
            for sr in res["series"]:
                for m, q, dep in zip(sr["minutes"], sr["flow_lps"], sr["depth_m"]):
                    w.writerow([title, sr["object_type"], sr["object_id"], m, _hhmm(m), q, dep])
        msgs.info(f"Wrote {out_csv}")
    if chart:
        _series_chart(arcpy, msgs, gpkg_path, res)
    return res


def _series_chart(arcpy, msgs, gpkg_path, res):
    """Line chart of flow against minute on the wwn_sim_series table, filtered to this run and these ids."""
    ids = sorted({s["object_id"] for s in res["series"]})
    where = f"run_id = {gpkg.sql_quote(res['run']['run_id'])} AND " + gpkg.in_clause("object_id", ids)
    try:
        tbl = maplayers.find_layer_for(arcpy, gpkg_path, "wwn_sim_series")
        if tbl is None:
            tbl = maplayers.active_map(arcpy).addDataFromPath(gpkg.table_path(gpkg_path, "wwn_sim_series"))
        tbl.definitionQuery = where
        ch = arcpy.charts.Line(x="minute", y=["flow_lps"], splitCategory="object_id",
                               title=f"Flow, {_summary().run_title(res['run'])}",
                               xTitle="Minutes after the warm-up", yTitle="Flow (L/s)")
        ch.addToLayer(tbl)
        msgs.info("Chart added to the Simulation time series table (open it from the table's Charts).")
    except Exception as exc:  # noqa: BLE001 - charts are optional; the CSV / messages carry the data
        msgs.warn(f"Could not create the chart ({type(exc).__name__}: {exc}); use the CSV output instead.")


def trace(arcpy, msgs: Messages, gpkg_path: str, start: str | None = None, direction: str = "Upstream",
          select: bool = True) -> dict:
    """Trace from a node or conduit id (blank: the conduit or node selected in the map) and report the
    upstream loading."""
    start = (start or "").strip()
    if not start:
        picked = maplayers.selected_ids(arcpy, gpkg_path, "wwn_conduits", "conduit_id") or \
            maplayers.selected_ids(arcpy, gpkg_path, "wwn_nodes", "node_id")
        if not picked:
            raise RuntimeError("Enter a start node / conduit id, or select one conduit or node of the model "
                               "in the map.")
        if len(picked) > 1:
            msgs.warn(f"{len(picked)} features are selected; tracing from {picked[0]}.")
        start = picked[0]
    d = direction.lower()
    res = engine.run_trace(gpkg_path, start, up=d in ("upstream", "both"), down=d in ("downstream", "both"))
    for line in _summary().trace_lines(res, start):
        msgs.info(line)
    s = res.get("summary") or {}
    nodes = sorted({*res.get("upstream", []), *res.get("downstream", []), s.get("start_node") or start})
    conduits = list(res.get("conduits") or [])
    if select:
        with gpenv.quiet(arcpy):
            maplayers.select_in_model(arcpy, gpkg_path, "wwn_nodes", nodes, "node_id", msgs, "Nodes")
            maplayers.select_in_model(arcpy, gpkg_path, "wwn_conduits", conduits, "conduit_id", msgs, "conduit")
    return {"nodes": nodes, "conduits": conduits, "summary": s, "start": start}


# --------------------------------------------------------------- loading data

def import_profile(msgs: Messages, gpkg_path: str, profile_id: str, csv_path: str | None = None,
                   description: str = "", set_default: bool = True, normalise: bool = False,
                   pasted: str | None = None) -> dict:
    """Import a diurnal profile (CSV / TXT file, or text pasted from ICM: tab-separated Time / Factor)."""
    if not (profile_id or "").strip():
        raise RuntimeError("Enter a profile id.")
    if csv_path:
        if not os.path.isfile(csv_path):
            raise RuntimeError(f"Profile file not found: {csv_path}")
        with open(csv_path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    elif pasted and pasted.strip():
        text = pasted
    else:
        raise RuntimeError("Choose a CSV / TXT profile file or paste the profile text.")
    r = engine.run_import_profile(gpkg_path, profile_id.strip(), text, description or "", set_default, normalise)
    msgs.info(f"Imported profile {r['profile_id']}: {r['points']} row(s), interval {r.get('interval_min')} min, "
              f"mean factor {r['mean_factor']:.4f}, min {r['min_factor']:.3f}, max {r['max_factor']:.3f}"
              + (" (normalised to mean 1)" if r.get("normalised") else "")
              + (". Set as the model's default profile." if r.get("is_default") else "."))
    for w in r.get("warnings", []):
        msgs.warn(w)
    return r


NONE_PROFILE = "(none)"
DEFAULT_PROFILE = "(model default)"


def settings_lines(st: dict) -> list:
    lines = [f"ADWF per connection {st['adwf_per_connection_lpd']:g} L/day "
             f"({st['adwf_per_connection_lpd'] / 86400:.5f} L/s); peaking factor {st['peaking_factor']:g} "
             "(GSDF = ADWF x peaking factor); default profile " + (st.get("default_profile") or NONE_PROFILE) + "."]
    profs = st.get("profiles") or []
    if profs:
        lines.append(f"{len(profs)} profile(s):")
        for p in profs:
            lines.append(f"  {p['profile_id']}: {p['points']} point(s)"
                         + (f", {p['interval_min']} min" if p.get("interval_min") else "")
                         + (f", mean {p['mean_factor']:.3f}" if p.get("mean_factor") is not None else "")
                         + (f" - {p['description']}" if p.get("description") else ""))
    else:
        lines.append("No diurnal profiles: import one with Import Diurnal Profile (the simulation uses a constant "
                     "flow without one).")
    return lines


def model_settings(msgs: Messages, gpkg_path: str, adwf_per_connection_lpd=None, peaking_factor=None,
                   default_profile: str | None = None) -> dict:
    """Show / change the loading settings in wwn_meta (None keeps a value)."""
    dp = None if default_profile is None else ("" if default_profile == NONE_PROFILE else default_profile)
    st = engine.set_settings(gpkg_path, adwf_per_connection_lpd, peaking_factor, dp)
    for line in settings_lines(st):
        msgs.info(line)
    return st


ASSIGN_TARGETS = {
    "Selected nodes (their node loads)": ("node_loads", "wwn_nodes", "node_id"),
    "Selected node loads": ("node_loads", "wwn_node_loads", "load_id"),
    "Selected sub-catchments": ("subcatchments", "wwn_subcatchments", "subcatchment_id"),
    "All node loads": ("node_loads", None, None),
    "All sub-catchments": ("subcatchments", None, None),
}


def assign_profile(arcpy, msgs: Messages, gpkg_path: str, target: str, profile_id: str) -> int:
    """Set the diurnal profile of node loads / sub-catchments (selected in the map, or all)."""
    if target not in ASSIGN_TARGETS:
        raise RuntimeError(f"Choose what to assign: {', '.join(ASSIGN_TARGETS)}")
    kind, table, idf = ASSIGN_TARGETS[target]
    pid = "" if profile_id in (None, "", DEFAULT_PROFILE) else profile_id
    if table is None:
        import sqlite3
        con = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
        try:
            sql = ("SELECT load_id FROM wwn_node_loads" if kind == "node_loads"
                   else "SELECT subcatchment_id FROM wwn_subcatchments")
            ids = [r[0] for r in con.execute(sql)]
        finally:
            con.close()
    else:
        ids = maplayers.selected_ids(arcpy, gpkg_path, table, idf)
        if not ids:
            raise RuntimeError(f"Nothing selected in the model's {table} layer / table in the active map.")
    n = engine.assign_profile(gpkg_path, kind, ids, pid)
    what = "node load(s)" if kind == "node_loads" else "sub-catchment(s)"
    msgs.info(f"Profile {pid or DEFAULT_PROFILE} assigned to {n} {what}" +
              (f" (from {len(ids)} selected id(s))" if table else "") + ".")
    if n == 0 and kind == "node_loads" and table == "wwn_nodes":
        msgs.warn("The selected nodes have no node loads (import connection point loads, or add rows to "
                  "wwn_node_loads).")
    return n


def pipe_capacity(msgs: Messages, dn, grade, ks=None, flow=None) -> dict:
    res = capacity.calculate(float(dn), float(grade), ks, flow)
    for line in capacity.describe(res):
        msgs.info(line)
    return res


# -------------------------------------------------------------- verify setup

def verify_setup(msgs: Messages) -> bool:
    """Print where the engine (wwnet_native) is loaded from, whether it imports and whether it provides every
    function this toolbox uses. Returns True when it does."""
    rep = vendor.report()
    for line in vendor.format_report(rep):
        msgs.info(line)
    if not rep["_import_wwnet_native"][0]:
        msgs.warn("wwnet_native is not importable: no WWNet tool can run. Install the full bundle "
                  "(wwnet-arcgis-bundle-win-py311).")
        return False
    lacking = shared.core("native").missing_functions()
    if lacking:
        msgs.warn(f"wwnet_native is too old for this toolbox: missing {', '.join(lacking)}. Install the current "
                  "full bundle.")
        return False
    msgs.info("wwnet_native provides every engine function this toolbox uses.")
    return True


# ------------------------------------------------------------- ICM export

def export_icm(arcpy, msgs: Messages, gpkg_path: str, out_folder: str,
               out_sr_label: str = "GDA2020 / MGA zone 50 (EPSG:7850)", dem: str | None = None) -> dict:
    """Write <stem>_ICM.gdb (ICM_Nodes, ICM_Conduits incl. rising mains, ICM_Pumps when there are pumps),
    the same tables as CSV (+ pump head-discharge tables) and an import-notes file to out_folder."""
    import time
    from . import icm_export, sewer_import
    t0 = t = time.perf_counter()
    tm = {}

    def lap(k):
        nonlocal t
        now = time.perf_counter()
        tm[k] = now - t
        t = now

    if not gpkg_path or not os.path.isfile(gpkg_path):
        raise RuntimeError(f"Model not found: {gpkg_path}")
    os.makedirs(out_folder, exist_ok=True)
    data = icm_export.build(gpkg_path)
    if not data["nodes"] or not data["conduits"]:
        raise RuntimeError("The model has no nodes or no conduits to export.")
    lap("read model")
    if dem:
        filled, nodata = icm_export.fill_ground_from_dem(arcpy, msgs, data["nodes"],
                                                         gpkg.table_path(gpkg_path, "wwn_nodes"), dem)
        blank = [n["node_id"] for n in data["nodes"] if n["ground_level"] is None and not n.get("_extra")]
        data["warnings"] = [w for w in data["warnings"] if "node(s) have no surface level" not in w]
        if filled or blank:
            data["warnings"].append(
                f"{filled} node(s) had no surface level: ground_level filled from the DEM (wwn_ground_source = DEM); "
                f"{len(blank)} still blank" + (": " + ", ".join(blank[:20]) + (" ..." if len(blank) > 20 else "")
                                              if blank else "") + ".")
        if nodata:
            data["warnings"].append(f"{len(nodata)} node(s) are outside the DEM or on NoData: "
                                    + ", ".join(nodata[:20]) + (" ..." if len(nodata) > 20 else ""))
    lap("DEM")
    code = icm_export.OUT_SR.get(out_sr_label, 7850)
    sr = arcpy.SpatialReference(code)
    if code != 7850:
        name = sewer_import.choose_gda_transformation(arcpy, sr)
        if name:
            arcpy.env.geographicTransformations = name
            msgs.info(f"Projecting GDA2020 -> GDA94 with {name}.")
        else:
            msgs.warn("No GDA94 <-> GDA2020 transformation is listed by ArcGIS; coordinates may be ~1.5 m out.")
    node_tm, conduit_tm, pump_tm, gdb_s = {}, {}, {}, 0.0
    pumps_csv = hd_csv = None
    with gpenv.quiet(arcpy):
        stem = icm_export.safe_stem(gpkg_path)
        gdb = os.path.join(out_folder, f"{stem}_ICM.gdb")
        if not arcpy.Exists(gdb):
            t_gdb = time.perf_counter()
            arcpy.management.CreateFileGDB(out_folder, f"{stem}_ICM.gdb")
            gdb_s = time.perf_counter() - t_gdb
        else:
            msgs.info(f"Replacing the ICM tables in the existing {gdb}")
        geo = icm_export.read_pressure_geometry(arcpy, gpkg_path, data, sr) \
            if any(r.get("_extra") for r in data["nodes"] + data["conduits"]) else {"ps": {}, "rm": {}}
        xy = icm_export.write_gdb(arcpy, gdb, "ICM_Nodes", "POINT", icm_export.NODE_FIELDS, data["nodes"],
                                  "node_id", gpkg.table_path(gpkg_path, "wwn_nodes"), "node_id", sr,
                                  timings=node_tm, extra_geom=icm_export.node_geometry_fn(arcpy, sr, geo))
        lap("nodes")
        link_geom = icm_export.link_geometry_fn(arcpy, sr, geo, xy)
        icm_export.write_gdb(arcpy, gdb, "ICM_Conduits", "POLYLINE", icm_export.CONDUIT_FIELDS, data["conduits"],
                             "wwn_conduit_id", gpkg.table_path(gpkg_path, "wwn_conduits"), "conduit_id", sr,
                             timings=conduit_tm, extra_geom=link_geom)
        lap("conduits")
        pumps_fc = os.path.join(gdb, "ICM_Pumps")
        if data["pumps"]:
            icm_export.write_gdb(arcpy, gdb, "ICM_Pumps", "POLYLINE", icm_export.PUMP_FIELDS, data["pumps"],
                                 "wwn_pump_id", None, None, sr, timings=pump_tm, extra_geom=link_geom)
            lap("pumps")
        elif arcpy.Exists(pumps_fc):            # stale table from an earlier export of this model
            arcpy.management.Delete(pumps_fc)
        nodes_csv = os.path.join(out_folder, f"{stem}_ICM_nodes.csv")
        conduits_csv = os.path.join(out_folder, f"{stem}_ICM_conduits.csv")
        icm_export.write_csv(nodes_csv, icm_export.NODE_FIELDS, data["nodes"], xy=xy)
        icm_export.write_csv(conduits_csv, icm_export.CONDUIT_FIELDS, data["conduits"])
        for name in ("pumps", "head_discharge"):
            path = os.path.join(out_folder, f"{stem}_ICM_{name}.csv")
            if data[name]:
                fields = icm_export.PUMP_FIELDS if name == "pumps" else icm_export.HEAD_DISCHARGE_FIELDS
                icm_export.write_csv(path, fields, data[name])
                if name == "pumps":
                    pumps_csv = path
                else:
                    hd_csv = path
            elif os.path.isfile(path):
                os.remove(path)
        notes = os.path.join(out_folder, f"{stem}_ICM_import_notes.txt")
        with open(notes, "w", encoding="utf-8") as fh:
            fh.write(icm_export.notes_text(gpkg_path, out_sr_label, gdb, nodes_csv, conduits_csv, data["warnings"],
                                           pumps_csv=pumps_csv, hd_csv=hd_csv, head_discharge=data["head_discharge"]))
        k = data["counts"]
        msgs.info(f"Exported {k['nodes']} node(s), {k['conduits']} gravity sewer(s), {k['rising_mains']} rising "
                  f"main(s) and {k['pumps']} pump(s) in {out_sr_label}.")
        for w in data["warnings"]:
            msgs.warn(w)
        msgs.info(f"Geodatabase: {gdb}")
        msgs.info(f"CSV: {nodes_csv} ; {conduits_csv}" + "".join(f" ; {x}" for x in (pumps_csv, hd_csv) if x))
        msgs.info(f"Import notes (field mapping for ICM's Open Data Import Centre): {notes}")
        lap("CSV/notes")
    msgs.info("Timing: " + ", ".join(f"{k} {v:.1f} s" for k, v in tm.items())
              + f", total {time.perf_counter() - t0:.1f} s")
    tkeys = ("exists", "delete", "create", "fields", "copy")
    msgs.info("Timing ICM_Nodes: " + ", ".join([f"create gdb {gdb_s:.1f} s"]
                                              + [f"{k} {node_tm.get(k, 0.0):.1f} s" for k in tkeys]))
    msgs.info("Timing ICM_Conduits: " + ", ".join(f"{k} {conduit_tm.get(k, 0.0):.1f} s" for k in tkeys))
    if pump_tm:
        msgs.info("Timing ICM_Pumps: " + ", ".join(f"{k} {pump_tm.get(k, 0.0):.1f} s" for k in tkeys))
    return {"gdb": gdb, "nodes_csv": nodes_csv, "conduits_csv": conduits_csv, "pumps_csv": pumps_csv,
            "head_discharge_csv": hd_csv, "notes": notes, **data}

