"""Tool bodies for WWNet.pyt. Plain-value arguments in, messages out; arcpy injected."""
from __future__ import annotations

import os
import sqlite3

from . import (arcpy_tools, assistant_run, capacity, cli, dpapi, gpenv, gpkg, issues_table, maplayers,
               pressure_import, pumpdata, settings, shared, sewer_import, symbology, vendor)
from .messages import Messages

MAX_LINES = 300

# Pump / rising main review thresholds (one place; printed in the Run Simulation messages).
# MAX_STARTS_PER_HOUR depends on the motor size (small submersibles tolerate more starts than large
# motors): treat it as a screening value and check against the station's own specification.
MAX_STARTS_PER_HOUR = 10.0
MIN_RM_VELOCITY = 0.7     # m/s: below this solids settle in a rising main (self-cleansing concern)
MAX_RM_VELOCITY = 2.0     # m/s: above this headloss and surge (water hammer) become a concern


# --------------------------------------------------------------- model tools

def new_model(arcpy, msgs: Messages, out_path: str, add_to_map: bool = True, overwrite: bool = False):
    if not out_path.lower().endswith(".gpkg"):
        out_path += ".gpkg"
    gpkg.create_model(out_path, overwrite=overwrite)
    msgs.info(f"Created WWNet model {out_path} (EPSG:7850, schema v2).")
    if add_to_map:
        with gpenv.quiet(arcpy):
            maplayers.add_model(arcpy, out_path, msgs)
    return out_path


def add_model_to_map(arcpy, msgs: Messages, gpkg_path: str, symbolize: bool = True):
    with gpenv.quiet(arcpy):
        layers = maplayers.add_model(arcpy, gpkg_path, msgs, symbolize=symbolize)
    msgs.info(f"Added {len(layers)} layer(s) to the active map.")
    return layers


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
    if not out_path.lower().endswith(".gpkg"):
        out_path += ".gpkg"
    exists = os.path.exists(out_path)
    overwrite = bool(getattr(getattr(arcpy, "env", None), "overwriteOutput", False))
    if exists and not overwrite:
        raise RuntimeError(f"{out_path} already exists. Choose a new name or enable 'Overwrite the outputs "
                           "of geoprocessing operations' in the Geoprocessing options / Environments.")
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
    with gpenv.quiet(arcpy):
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
        model, rep = imp.build_model(acs, cps, sewers, fmap, opts, cfg)
        si.report_to_messages(msgs, rep)
        native.write_model(out_path, imp.model_json(model), exists and overwrite)
        msgs.info(f"Wrote WWNet model {out_path} (EPSG:7850).")
        issues = native.check(out_path, True)
        counts = {"error": 0, "warning": 0, "info": 0}
        for i in issues:
            counts[i["severity"] if i["severity"] in counts else "info"] += 1
        report_issues(msgs, issues[:si.MAX_ISSUE_LINES])
        if len(issues) > si.MAX_ISSUE_LINES:
            msgs.info(f"... {len(issues) - si.MAX_ISSUE_LINES} more issue(s) stored in wwn_issues "
                      "(run Check Model for the full table).")
        msgs.info(f"Model check: {counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info.")
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
                      discharge_m=num(discharge_distance, "discharge_distance_m", 10.0),
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
            b = opts.search_m + opts.discharge_m
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


def report_issues(msgs: Messages, issues: list):
    counts = {"error": 0, "warning": 0, "info": 0}
    for n, i in enumerate(issues):
        sev = i["severity"] if i["severity"] in counts else "info"
        counts[sev] += 1
        if n >= MAX_LINES:
            continue
        line = f"{i['code']} {i['object_type']} {i['object_id']}" + \
               (f" ({i['clause']})" if i["clause"] else "") + f": {i['message']}"
        # model-check findings are data issues, not tool failures: never AddError (Pro would mark the
        # tool as failed even though it finished)
        if sev == "error":
            msgs.warn(f"ERROR: {line}")
        elif sev == "warning":
            msgs.warn(line)
        else:
            msgs.info(line)
    if len(issues) > MAX_LINES:
        msgs.info(f"... {len(issues) - MAX_LINES} more issue(s) not shown; use the output table.")
    return counts


def check_model(arcpy, msgs: Messages, gpkg_path: str, out_table: str | None = None, exe=None):
    issues, rc = cli.run_check(gpkg_path, exe)
    counts = report_issues(msgs, issues)
    for line in station_report_lines(gpkg_path):
        msgs.info(f"Pump station data: {line}")
    msgs.info(f"Check finished: {counts['error']} error(s), {counts['warning']} warning(s), "
              f"{counts['info']} info.")
    if out_table:
        with gpenv.quiet(arcpy):
            n = issues_table.write_issues_table(arcpy, out_table, issues)
        msgs.info(f"Wrote {n} issue(s) to {out_table}")
    return issues


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
    flow_units, speed_pct, source_note, replace_pumps. Data errors are reported as an "ERROR:" warning
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
        text = str(exc).lower()
        if "locked" in text or "busy" in text or "readonly" in text or "read-only" in text:
            msgs.warn("ERROR: The model GeoPackage is locked by ArcGIS Pro. Close any open attribute tables "
                      "and remove the model's layers from the map (or use a copy), then run the tool again.")
            return None
        raise
    msgs.info(result["summary"])
    for w in list(curve_warnings) + list(result["warnings"]):
        msgs.warn(w)
    for line in station_report_lines(gpkg_path):
        msgs.info(line)
    return result


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


def _select(arcpy, layer, ids, field):
    ds = gpkg.dataset_path(maplayers.layer_source(layer))
    where = gpkg.in_clause(arcpy.AddFieldDelimiters(ds, field), ids)
    arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION" if ids else "CLEAR_SELECTION",
                                            where if ids else "")


RESULT_FIELDS = [("conduit_id", "TEXT"), ("dn", "LONG"), ("grade_1in", "DOUBLE"), ("adwf_lps", "DOUBLE"),
                 ("gsdf_lps", "DOUBLE"),
                 ("pumped_lps", "DOUBLE"), ("total_lps", "DOUBLE"), ("capacity_lps", "DOUBLE"),
                 ("table44_lps", "DOUBLE"), ("hydraulic_capacity_lps", "DOUBLE"),
                 ("d_over_d", "DOUBLE"), ("velocity_mps", "DOUBLE"), ("result", "TEXT"), ("message", "TEXT")]


def _worst_message(c: dict) -> str:
    chks = cli.failed_checks(c)
    chks.sort(key=lambda k: 0 if k.get("result") == "fail" else 1)
    return str(chks[0].get("message") or "") if chks else ""


def _num(c, key, default=0.0):
    v = c.get(key)
    return default if v is None else v


def _opt(c, key, prec):
    v = c.get(key)
    return "-" if v is None else f"{v:.{prec}f}"


def results_table_lines(conduit_results: list, max_lines: int = MAX_LINES) -> list:
    hdr = (f"{'Conduit':<10}{'DN':>5}{'Grade 1:x':>10}{'ADWF L/s':>9}{'GSDF L/s':>9}{'Pumped':>8}{'Total L/s':>10}"
           f"{'Cap L/s':>9}{'T4.4':>6}{'Hyd':>7}{'d/D':>6}{'v m/s':>7}  {'RESULT':<6}  Worst message")
    lines = [hdr, "-" * len(hdr)]
    for c in conduit_results[:max_lines]:
        lines.append(f"{str(c['conduit_id'])[:10]:<10}{int(_num(c, 'dn')):>5}{_num(c, 'grade_1in'):>10.0f}"
                     f"{_num(c, 'adwf_lps'):>9.2f}{_num(c, 'gsdf_lps'):>9.2f}{_num(c, 'pumped_lps'):>8.2f}{_num(c, 'total_lps'):>10.2f}"
                     f"{_num(c, 'capacity_lps'):>9.1f}{_opt(c, 'table44_lps', 1):>6}"
                     f"{_opt(c, 'hydraulic_capacity_lps', 1):>7}{_num(c, 'd_over_d'):>6.2f}{_num(c, 'velocity_mps'):>7.2f}"
                     f"  {str(c['result']).upper():<6}  {_worst_message(c)[:60]}")
    if len(conduit_results) > max_lines:
        lines.append(f"... {len(conduit_results) - max_lines} more")
    return lines


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


def build_results_fc(arcpy, gpkg_path: str, conduit_results: list) -> str:
    """Create memory\\wwnet_results_<stem> (POLYLINE) filled from the design output; returns its path."""
    fc_name = f"wwnet_results_{_safe_stem(gpkg_path)}"
    fc = "memory\\" + fc_name
    if arcpy.Exists(fc):
        arcpy.management.Delete(fc)
    arcpy.management.CreateFeatureclass("memory", fc_name, "POLYLINE", spatial_reference=_spatial_ref(arcpy, gpkg_path))
    for name, typ in RESULT_FIELDS:
        if typ == "TEXT":
            arcpy.management.AddField(fc, name, "TEXT", field_length=255 if name == "message" else 64)
        else:
            arcpy.management.AddField(fc, name, typ)
    by_id = {c["conduit_id"]: c for c in conduit_results}
    cols = [f for f, _ in RESULT_FIELDS] + ["SHAPE@"]
    n = 0
    with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_conduits"), ["conduit_id", "SHAPE@"]) as src, \
            arcpy.da.InsertCursor(fc, cols) as ins:
        for cid, shape in src:
            c = by_id.get(cid)
            if c is None:
                continue
            ins.insertRow([cid, int(_num(c, "dn")), _num(c, "grade_1in"), _num(c, "adwf_lps"), _num(c, "gsdf_lps"),
                           _num(c, "pumped_lps"), _num(c, "total_lps"), _num(c, "capacity_lps"),
                           _num(c, "table44_lps"), _num(c, "hydraulic_capacity_lps"), _num(c, "d_over_d"), _num(c, "velocity_mps"), c["result"],
                           _worst_message(c)[:255], shape])
            n += 1
    return fc


def _show_results_layer(arcpy, msgs, gpkg_path: str, d: dict, select_failed: bool):
    stem = _safe_stem(gpkg_path)
    name = f"Design results \u2014 {stem}"
    maplayers.remove_layers_named(arcpy, name)
    fc = build_results_fc(arcpy, gpkg_path, d["conduit_results"])
    lyr = maplayers.add_results_layer(arcpy, fc, name, msgs)
    symbology.apply("results", lyr, msgs)
    if select_failed:
        failed = cli.failed_conduits(d)
        try:
            _select(arcpy, lyr, failed, "conduit_id")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not select failed conduits on the results layer: {exc}")
    msgs.info(f"Results layer added: {name} (temporary \u2014 re-run Run Design after restarting Pro)")


def design_detail_lines(conduit_results: list, per_rule: int = 5) -> list:
    """Detail lines for FAIL and WARN checks only (never info), at most `per_rule` per rule."""
    by_rule: dict = {}
    for c in conduit_results:
        for chk in cli.failed_checks(c):
            by_rule.setdefault(chk.get("rule"), []).append((c, chk))
    lines: list = []
    for rule, items in by_rule.items():
        items.sort(key=lambda it: 0 if it[1].get("result") == "fail" else 1)
        for c, chk in items[:per_rule]:
            lines.append(f"{c['conduit_id']} DN{c['dn']} {str(chk['result']).upper()} {rule} "
                         f"({chk.get('clause')}): {chk.get('message')}")
        if len(items) > per_rule:
            lines.append(f"... {len(items) - per_rule} more {rule} item(s)")
    return lines


def design_info_lines(d: dict) -> list:
    n = (d.get("summary") or {}).get("cover_not_checked_ends") or 0
    if n:
        return [f"Cover not checked at {n} conduit end(s): no surface level (IS ends / junctions)."]
    return []


def run_design(arcpy, msgs: Messages, gpkg_path: str, show_layer: bool = True, add_table: bool = False,
               select_failed: bool = True, exe=None) -> dict:
    d = cli.run_design(gpkg_path, exe)
    s = d["summary"]
    msgs.info(f"Design run: {s['conduits']} conduit(s): {s['pass']} pass, {s['warn']} warn, "
              f"{s['fail']} fail. Terminal GSDF {s['terminal_gsdf_lps']:.3f} L/s "
              f"(total local {s['total_local_gsdf_lps']:.3f} L/s).")
    adwf = s.get("total_local_adwf_lps") or 0.0
    if adwf:
        msgs.info(f"Loads: ADWF {adwf:.3f} L/s total ({s.get('terminal_adwf_lps') or 0.0:.3f} L/s at terminals); "
                  f"GSDF from loads = ADWF x {s.get('peaking_factor', 1.5):g} peaking factor.")
    for line in results_table_lines(d["conduit_results"]):
        msgs.info(line)
    for line in design_detail_lines(d["conduit_results"]):
        msgs.warn(line)
    for line in design_info_lines(d):
        msgs.info(line)
    for i in report_issues_sorted([i for i in d.get("issues", []) if i.get("object_type") != "conduit"]):
        msgs.warn(f"{i.get('code')} {i.get('object_type')} {i.get('object_id')} ({i.get('clause')}): "
                  f"{i.get('message')}")
    with gpenv.quiet(arcpy):
        if show_layer:
            try:
                _show_results_layer(arcpy, msgs, gpkg_path, d, select_failed)
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add the results layer to the map: {exc}")
        if add_table:
            try:
                maplayers.add_results_table(arcpy, gpkg_path, msgs)
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add the results table to the map: {exc}")
        if select_failed:
            lyr = None
            try:
                lyr = maplayers.find_layer_for(arcpy, gpkg_path, "wwn_conduits")
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not inspect the map: {exc}")
            if lyr is None:
                msgs.warn("The model's conduit layer is not in the active map; skipping select.")
            else:
                failed = cli.failed_conduits(d)
                try:
                    _select(arcpy, lyr, failed, "conduit_id")
                    msgs.info(f"Selected {len(failed)} failed conduit(s)." if failed else "No failed conduits.")
                except Exception as exc:  # noqa: BLE001
                    msgs.warn(f"Could not select failed conduits: {exc}")
    return d


SIM_FIELDS = [("conduit_id", "TEXT"), ("dn", "LONG"), ("max_flow_lps", "DOUBLE"), ("max_d_over_d", "DOUBLE"),
              ("max_velocity_mps", "DOUBLE"), ("surcharged_min", "DOUBLE"), ("d_class", "TEXT")]
SIM_NODE_FIELDS = [("node_id", "TEXT"), ("max_hgl_m", "DOUBLE"), ("max_depth_m", "DOUBLE"),
                   ("flood_volume_m3", "DOUBLE"), ("surcharged_min", "DOUBLE")]


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


def build_sim_fc(arcpy, gpkg_path: str, links: list) -> str:
    """memory\\wwnet_sim_<stem> (POLYLINE): conduit geometry + simulation maxima; returns its path."""
    fc = _memory_fc(arcpy, gpkg_path, "wwnet_sim", "POLYLINE", SIM_FIELDS)
    by_id = {c["conduit_id"]: c for c in links}
    cols = [f for f, _ in SIM_FIELDS] + ["SHAPE@"]
    with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_conduits"), ["conduit_id", "SHAPE@"]) as src, \
            arcpy.da.InsertCursor(fc, cols) as ins:
        for cid, shape in src:
            c = by_id.get(cid)
            if c is None:
                continue
            ins.insertRow([cid, int(_num(c, "dn")), _num(c, "max_flow_lps"), _num(c, "max_d_over_d"),
                           _num(c, "max_velocity_mps"), _num(c, "surcharged_min"),
                           symbology.sim_class(c.get("max_d_over_d"), c.get("surcharged_min")), shape])
    return fc


def build_sim_nodes_fc(arcpy, gpkg_path: str, nodes: list) -> str:
    """memory\\wwnet_simnodes_<stem> (POINT): flooding nodes only; returns its path."""
    fc = _memory_fc(arcpy, gpkg_path, "wwnet_simnodes", "POINT", SIM_NODE_FIELDS)
    by_id = {n["node_id"]: n for n in nodes}
    cols = [f for f, _ in SIM_NODE_FIELDS] + ["SHAPE@"]
    with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, "wwn_nodes"), ["node_id", "SHAPE@"]) as src, \
            arcpy.da.InsertCursor(fc, cols) as ins:
        for nid, shape in src:
            n = by_id.get(nid)
            if n is None:
                continue
            ins.insertRow([nid, _num(n, "max_hgl_m"), _num(n, "max_depth_m"), _num(n, "flood_volume_m3"),
                           _num(n, "surcharged_min"), shape])
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


def sim_table_lines(links: list, top: int = 20) -> list:
    """Top conduits by max d/D."""
    hdr = f"{'Conduit':<10}{'DN':>5}{'Qmax L/s':>10}{'d/D':>6}{'v m/s':>7}{'Surch min':>10}  Peak at"
    lines = [hdr, "-" * len(hdr)]
    for c in sorted(links, key=lambda x: -_num(x, "max_d_over_d"))[:top]:
        lines.append(f"{str(c['conduit_id'])[:10]:<10}{int(_num(c, 'dn')):>5}{_num(c, 'max_flow_lps'):>10.2f}"
                     f"{_num(c, 'max_d_over_d'):>6.2f}{_num(c, 'max_velocity_mps'):>7.2f}"
                     f"{_num(c, 'surcharged_min'):>10.0f}  {c.get('time_of_max_flow') or '-'}")
    if len(links) > top:
        lines.append(f"... {len(links) - top} more conduit(s) (full results in wwn_sim_links)")
    return lines


def _show_sim_layers(arcpy, msgs, gpkg_path: str, d: dict):
    stem = _safe_stem(gpkg_path)
    name = f"Simulation results \u2014 {stem}"
    maplayers.remove_layers_named(arcpy, name)
    fc = build_sim_fc(arcpy, gpkg_path, d["links"])
    lyr = maplayers.add_results_layer(arcpy, fc, name, msgs)
    symbology.apply("sim", lyr, msgs)
    msgs.info(f"Results layer added: {name} (temporary \u2014 re-run Run Simulation after restarting Pro; "
              "the results stay in the model's wwn_sim_* tables)")
    nname = f"Simulation nodes \u2014 {stem}"
    maplayers.remove_layers_named(arcpy, nname)
    flooded = [n for n in d.get("nodes", []) if _num(n, "flood_volume_m3") > 0]
    if flooded:
        nfc = build_sim_nodes_fc(arcpy, gpkg_path, flooded)
        nlyr = maplayers.add_results_layer(arcpy, nfc, nname, msgs)
        symbology.apply("sim_nodes", nlyr, msgs)
        msgs.info(f"Flooding layer added: {nname} ({len(flooded)} node(s))")
    pname = f"Simulation pump stations \u2014 {stem}"
    maplayers.remove_layers_named(arcpy, pname)
    if d.get("pump_stations"):
        pfc = build_sim_pumps_fc(arcpy, gpkg_path, d)
        play = maplayers.add_results_layer(arcpy, pfc, pname, msgs)
        symbology.apply("sim_pumps", play, msgs)
        msgs.info(f"Pump station layer added: {pname} ({len(d['pump_stations'])} station(s); red = flagged)")


def run_simulation(arcpy, msgs: Messages, gpkg_path: str, duration_h: float = 48.0, warmup_h: float = 24.0,
                   roughness: str = "manning", manning_n: float = 0.013, flow_scale: float = 1.0,
                   show_layer: bool = True, export_inp: str | None = None, exe=None) -> dict:
    """Run the SWMM dynamic simulation (`wwnet simulate`), report and optionally map the results."""
    d = cli.run_simulate(gpkg_path, duration_h, warmup_h, roughness, manning_n, flow_scale,
                         export_inp or None, exe)
    s = d["summary"]
    msgs.info(f"Simulation {d.get('run_id') or ''}: {duration_h:g} h ({warmup_h:g} h warm-up), "
              f"roughness {roughness}" + (f" n={manning_n:g}" if roughness == "manning" else "")
              + f", flow scale {flow_scale:g}.")
    msgs.info(f"Inflow ADWF {s['inflow_adwf_lps']:.3f} L/s (simulated {s['inflow_simulated_lps']:.3f} L/s); "
              f"peak outlet flow {s['peak_outlet_flow_lps']:.3f} L/s; continuity error "
              f"{s['continuity_error_pct']:.3f} %.")
    msgs.info(f"{s['surcharged_conduits']} of {s['conduits']} conduit(s) surcharged; "
              f"{s['flooded_nodes']} node(s) flooded ({s['total_flood_volume_m3']:.2f} m3).")
    for line in sim_table_lines(d["links"]):
        msgs.info(line)
    for line in pump_table_lines(d, _station_levels(gpkg_path)):
        msgs.info(line)
    for _ps, _kind, text in pump_flags(d):
        msgs.warn(text)
    for w in d.get("warnings", [])[:MAX_LINES]:
        msgs.warn(w)
    if export_inp:
        msgs.info(f"SWMM input file written: {export_inp}")
    if show_layer:
        with gpenv.quiet(arcpy):
            try:
                _show_sim_layers(arcpy, msgs, gpkg_path, d)
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add the simulation layers to the map: {exc}")
    return d


def report_issues_sorted(issues):
    order = {"error": 0, "warning": 1}
    return sorted(issues, key=lambda i: order.get(i.get("severity"), 2))[:MAX_LINES]


def node_ids(arcpy, layer) -> list:
    with arcpy.da.SearchCursor(gpkg.dataset_path(layer.dataSource) if hasattr(layer, "dataSource") else layer,
                               ["node_id"]) as cur:
        return sorted({r[0] for r in cur if r[0]})


def trace(arcpy, msgs: Messages, nodes_layer, node_id: str, direction: str = "Upstream",
          select: bool = True, exe=None) -> dict:
    ds = nodes_layer.dataSource
    path, table = gpkg.split_datasource(ds)
    if not path or table != "wwn_nodes":
        raise RuntimeError("Choose the WWNet 'Nodes' layer (a wwn_nodes table in a .gpkg).")
    d = direction.lower()
    res = cli.run_trace(path, node_id, up=d in ("upstream", "both"), down=d in ("downstream", "both"),
                        exe=exe)
    traced = {node_id}
    for key, ids in res.items():
        msgs.info(f"{key} of {node_id}: {len(ids)} node(s): " + ", ".join(ids[:50])
                  + (" ..." if len(ids) > 50 else ""))
        traced.update(ids)
    conduits = gpkg.conduits_between(path, traced)
    msgs.info(f"{len(conduits)} conduit(s) on the traced path.")
    if select:
        with gpenv.quiet(arcpy):
            # the parameter may be a dataset path rather than a map layer: select on the map layer
            nl = maplayers.find_layer_for(arcpy, path, "wwn_nodes")
            if nl is not None:
                _select(arcpy, nl, sorted(traced), "node_id")
            else:
                msgs.warn("The nodes layer is not in the active map; nothing selected (add the model with "
                          "'Add Model To Map').")
            cl = maplayers.find_layer_for(arcpy, path, "wwn_conduits")
            if cl is not None:
                _select(arcpy, cl, conduits, "conduit_id")
            else:
                msgs.warn("The conduit layer is not in the active map; only nodes were selected.")
    return {"nodes": sorted(traced), "conduits": conduits}


def import_profile(msgs: Messages, gpkg_path: str, profile_id: str, csv_path: str | None = None,
                   description: str = "", set_default: bool = True, normalise: bool = False,
                   pasted: str | None = None, exe=None) -> dict:
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
    r = cli.run_import_profile(gpkg_path, profile_id.strip(), text, description or "", set_default,
                               normalise, exe)
    msgs.info(f"Imported profile {r['profile_id']}: {r['points']} row(s), interval {r.get('interval_min')} min, "
              f"mean factor {r['mean_factor']:.4f}, min {r['min_factor']:.3f}, max {r['max_factor']:.3f}"
              + (" (normalised to mean 1)" if r.get("normalised") else "")
              + (". Set as the model's default profile." if r.get("is_default") else "."))
    for w in r.get("warnings", []):
        msgs.warn(w)
    return r


def pipe_capacity(msgs: Messages, dn, grade, ks=None, flow=None) -> dict:
    res = capacity.calculate(float(dn), float(grade), ks, flow)
    for line in capacity.describe(res):
        msgs.info(line)
    return res


# ------------------------------------------------------------------ settings

def set_settings(msgs: Messages, key: str | None, clear: bool, model: str | None, effort: str | None,
                 exe: str | None):
    if clear:
        msgs.info("Stored API key removed." if dpapi.clear_key() else "No stored API key to remove.")
    elif key and key.strip():
        dpapi.save_key(key)       # never echoed
        msgs.info(f"API key stored encrypted for this Windows user ({dpapi.key_path()}).")
    cur = settings.save(model=model or None, effort=effort or None,
                        wwnet_exe=exe if exe is not None else None)
    k, src = dpapi.get_api_key()
    msgs.info(f"Model: {cur['model']}, effort: {cur['effort']}, WWNet CLI: {cur['wwnet_exe'] or '(auto)'}")
    msgs.info("API key available: " + ({"env": "yes (ANTHROPIC_API_KEY environment variable)",
                                        "dpapi": "yes (stored, DPAPI)"}.get(src, "NO")))
    if clear and src == "env":
        msgs.warn("ANTHROPIC_API_KEY is still set in the environment and takes precedence.")


# -------------------------------------------------------------- verify setup

def verify_setup(msgs: Messages, live: bool = False, model: str | None = None, client_factory=None) -> bool:
    """Print the vendor/package report; with `live` make one tiny API call. Never prints the key.
    Returns True when imports (and the live call, if requested) succeeded."""
    rep = vendor.report()
    for line in vendor.format_report(rep):
        msgs.info(line)
    ok = rep["_import_anthropic"][0] and rep["_import_wwnet_native"][0]
    if not rep["_import_anthropic"][0]:
        msgs.warn("anthropic is not importable: the assistant will not work (check/design do not need it).")
    if not rep["_import_wwnet_native"][0]:
        msgs.warn("wwnet_native is not importable: check/design/trace need it (or a wwnet CLI fallback).")
    if not live:
        msgs.info("Live API test skipped (tick 'Live API test' to run it).")
        return ok
    key, src = dpapi.get_api_key()
    if not key:
        msgs.error("No Anthropic API key: run 'Set Assistant API Key' first (or set ANTHROPIC_API_KEY).")
        return False
    if not rep["_import_anthropic"][0]:
        return False
    client_mod = shared.assistant("client")
    model = model or settings.load()["model"]
    try:
        note = vendor.inject_truststore()
        if note:
            msgs.info(note)
        client = (client_factory or client_mod.make_client)(key)
        res = client.messages.create(model=model, max_tokens=16,
                                     messages=[{"role": "user", "content": "Reply with the word: ok"}])
        msgs.info(f"Live API test OK: model {getattr(res, 'model', model)} responded "
                  f"(key from {'environment variable' if src == 'env' else 'stored DPAPI file'}).")
        return ok
    except Exception as exc:  # noqa: BLE001 - mapped to friendly text; the key is never part of it
        msgs.error("Live API test FAILED: " + client_mod.friendly_error(exc))
        return False


# ----------------------------------------------------------------- assistant

def map_key(arcpy) -> str:
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        return f"{getattr(aprx, 'filePath', '')}|{aprx.activeMap.name}"
    except Exception:  # noqa: BLE001
        return "default"


def selection_context(tools: arcpy_tools.ArcpyTools, layer_name: str | None, use_selected: bool) -> str:
    if not layer_name:
        return ""
    lyr = tools.find_layer(layer_name)
    tbl = tools.table_of(lyr)
    if not use_selected:
        return f"Context: the engineer is working with layer '{lyr.name}' ({tbl})."
    sel = tools.get_selected_features({"layer": layer_name})
    idf = shared.core("schema").ID_FIELDS.get(tbl or "")
    ids = [f.get(idf) for f in sel["features"] if idf and f.get(idf)]
    if not sel["selected_count"]:
        return f"Context: layer '{lyr.name}' ({tbl}) has no features selected."
    return (f"Context: {sel['selected_count']} feature(s) are selected in layer '{lyr.name}' ({tbl}); "
            f"{idf} values: {ids}. Use get_selected_features for their attributes.")


def ask_assistant(arcpy, msgs: Messages, question: str, layer_name: str | None = None,
                  use_selected: bool = False, model: str | None = None, effort: str | None = None,
                  apply: bool = False, reset: bool = False, client_factory=None, layers_fn=None):
    assistant_run.check_prereqs()
    key, src = dpapi.get_api_key()
    if not key:
        raise RuntimeError("No Anthropic API key. Run 'Set Assistant API Key' first (or set the "
                           "ANTHROPIC_API_KEY environment variable).")
    cfg = settings.load()
    model = model or cfg["model"]
    effort = effort or cfg["effort"]
    if not (question or "").strip():
        raise RuntimeError("Enter a question.")
    tools = arcpy_tools.ArcpyTools(arcpy, apply=apply, layers_fn=layers_fn, emit=msgs.info)
    context = selection_context(tools, layer_name, use_selected)
    msgs.info(f"Asking {model} (effort {effort}); edits will be "
              + ("APPLIED if proposed." if apply else "proposed only (nothing is written).")
              + " Tool results (attribute values) are sent to Anthropic.")
    res = assistant_run.run(question, api_key=key, model=model, effort=effort, tools=tools, msgs=msgs,
                            history_key=map_key(arcpy), reset=reset, context=context,
                            client_factory=client_factory)
    if res.error:
        msgs.error(res.error)
    if res.proposals and not apply:
        msgs.info("")
        msgs.info(f"PROPOSED EDITS ({len(res.proposals)}) - NOT applied:")
        for line in arcpy_tools.proposal_table(res.proposals):
            msgs.info("  " + line)
        msgs.info("Re-run this tool with 'Apply proposed edits' ticked to apply them. The assistant "
                  "will propose again; only edits proposed in that run are written.")
    elif res.proposals:
        msgs.info(f"{sum(p.applied for p in res.proposals)} of {len(res.proposals)} proposed edit(s) applied.")
    msgs.info(f"Done ({res.stop_reason}); tokens in/out: {res.tokens_in}/{res.tokens_out}.")
    return res


def export_icm(arcpy, msgs: Messages, gpkg_path: str, out_folder: str,
               out_sr_label: str = "GDA2020 / MGA zone 50 (EPSG:7850)", dem: str | None = None) -> dict:
    """Write <stem>_ICM.gdb (ICM_Nodes, ICM_Conduits), two CSVs and an import-notes file to out_folder."""
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
        blank = [n["node_id"] for n in data["nodes"] if n["ground_level"] is None]
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
    node_tm, conduit_tm, gdb_s = {}, {}, 0.0
    with gpenv.quiet(arcpy):
        stem = icm_export.safe_stem(gpkg_path)
        gdb = os.path.join(out_folder, f"{stem}_ICM.gdb")
        if not arcpy.Exists(gdb):
            t_gdb = time.perf_counter()
            arcpy.management.CreateFileGDB(out_folder, f"{stem}_ICM.gdb")
            gdb_s = time.perf_counter() - t_gdb
        else:
            msgs.info(f"Replacing ICM_Nodes / ICM_Conduits in the existing {gdb}")
        xy = icm_export.write_gdb(arcpy, gdb, "ICM_Nodes", "POINT", icm_export.NODE_FIELDS, data["nodes"],
                                  "node_id", gpkg.table_path(gpkg_path, "wwn_nodes"), "node_id", sr,
                                  timings=node_tm)
        lap("nodes")
        icm_export.write_gdb(arcpy, gdb, "ICM_Conduits", "POLYLINE", icm_export.CONDUIT_FIELDS, data["conduits"],
                             "wwn_conduit_id", gpkg.table_path(gpkg_path, "wwn_conduits"), "conduit_id", sr,
                             timings=conduit_tm)
        lap("conduits")
        nodes_csv = os.path.join(out_folder, f"{stem}_ICM_nodes.csv")
        conduits_csv = os.path.join(out_folder, f"{stem}_ICM_conduits.csv")
        icm_export.write_csv(nodes_csv, icm_export.NODE_FIELDS, data["nodes"], xy=xy)
        icm_export.write_csv(conduits_csv, icm_export.CONDUIT_FIELDS, data["conduits"])
        notes = os.path.join(out_folder, f"{stem}_ICM_import_notes.txt")
        with open(notes, "w", encoding="utf-8") as fh:
            fh.write(icm_export.notes_text(gpkg_path, out_sr_label, gdb, nodes_csv, conduits_csv, data["warnings"]))
        msgs.info(f"Exported {len(data['nodes'])} node(s) and {len(data['conduits'])} conduit(s) "
                  f"in {out_sr_label}.")
        for w in data["warnings"]:
            msgs.warn(w)
        msgs.info(f"Geodatabase: {gdb}")
        msgs.info(f"CSV: {nodes_csv} ; {conduits_csv}")
        msgs.info(f"Import notes (field mapping for ICM's Open Data Import Centre): {notes}")
        lap("CSV/notes")
    msgs.info("Timing: " + ", ".join(f"{k} {v:.1f} s" for k, v in tm.items())
              + f", total {time.perf_counter() - t0:.1f} s")
    tkeys = ("exists", "delete", "create", "fields", "copy")
    msgs.info("Timing ICM_Nodes: " + ", ".join([f"create gdb {gdb_s:.1f} s"]
                                              + [f"{k} {node_tm.get(k, 0.0):.1f} s" for k in tkeys]))
    msgs.info("Timing ICM_Conduits: " + ", ".join(f"{k} {conduit_tm.get(k, 0.0):.1f} s" for k in tkeys))
    return {"gdb": gdb, "nodes_csv": nodes_csv, "conduits_csv": conduits_csv, "notes": notes, **data}
