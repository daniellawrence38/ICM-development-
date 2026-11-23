"""Tool bodies for WWNet.pyt. Plain-value arguments in, messages out; arcpy injected.

Messages are short prose lines (the Messages pane uses a proportional font); full results live in
the model's result layers (Design results, Simulation results, Issues), which persist in the
GeoPackage and are reused when already in the map.
"""
from __future__ import annotations

import csv
import json
import os

from . import (arcpy_tools, assistant_run, capacity, dpapi, engine, gpkg, maplayers, settings, shared,
               sewer_import, symbology, vendor)
from .messages import Messages

MAX_LINES = 300
MAX_ISSUE_LINES = 40
SAVE_EDITS_NOTE = ("The engine reads the GeoPackage file: save any edits in Pro first "
                   "(Edit > Save) or they are not included.")


def _summary():
    return shared.core("summary")


def _checks():
    return shared.core("checks")


def _gpkg_path(path: str) -> str:
    return path if path.lower().endswith(".gpkg") else path + ".gpkg"


# --------------------------------------------------------------- model tools

def new_model(arcpy, msgs: Messages, out_path: str, add_to_map: bool = True, overwrite: bool = False):
    out_path = _gpkg_path(out_path)
    gpkg.create_model(out_path, overwrite=overwrite)
    msgs.info(f"Created WWNet model {out_path} (EPSG:7850, schema v{shared.core('schema').SCHEMA_VERSION}).")
    if add_to_map:
        maplayers.add_model(arcpy, out_path, msgs)
    return out_path


def add_model_to_map(arcpy, msgs: Messages, gpkg_path: str, symbolize: bool = True):
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


# --------------------------------------------------------------- analysis

def check_model(arcpy, msgs: Messages, gpkg_path: str, show_layer: bool = True) -> list:
    msgs.info(SAVE_EDITS_NOTE)
    msgs.start("Checking the model")
    issues = engine.run_check(gpkg_path)
    report_issues(msgs, issues)
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
        _show_sim_layers(arcpy, msgs, gpkg_path)
    msgs.info("Stored as run " + s.run_title(d) + "; earlier runs: Show Simulation Run / Compare Simulation Runs.")
    return d


def _show_sim_layers(arcpy, msgs, gpkg_path):
    try:
        maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_conduit_results", msgs)
        maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_node_results", msgs,
                               definition_query=symbology.SIM_NODES_QUERY)
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
            maplayers.ensure_layer(arcpy, gpkg_path, "wwn_sim_compare", msgs)
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not show the Simulation comparison layer: {exc}")
    return c


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


# ------------------------------------------------------------------ settings

def set_settings(msgs: Messages, key: str | None, clear: bool, model: str | None, effort: str | None):
    if clear:
        msgs.info("Stored API key removed." if dpapi.clear_key() else "No stored API key to remove.")
    elif key and key.strip():
        dpapi.save_key(key)       # never echoed
        msgs.info(f"API key stored encrypted for this Windows user ({dpapi.key_path()}).")
    cur = settings.save(model=model or None, effort=effort or None)
    k, src = dpapi.get_api_key()
    msgs.info(f"Model: {cur['model']}, effort: {cur['effort']}")
    msgs.info("API key available: " + ({"env": "yes (ANTHROPIC_API_KEY environment variable)",
                                        "dpapi": "yes (stored, DPAPI)"}.get(src, "NO")))
    if clear and src == "env":
        msgs.warn("ANTHROPIC_API_KEY is still set in the environment and takes precedence.")


# -------------------------------------------------------------- verify setup

def verify_setup(msgs: Messages, live: bool = False, model: str | None = None, client_factory=None) -> bool:
    """Print the vendor/package report and the engine functions; with `live` make one tiny API call.
    Never prints the key. Returns True when imports (and the live call, if requested) succeeded."""
    rep = vendor.report()
    for line in vendor.format_report(rep):
        msgs.info(line)
    ok = rep["_import_anthropic"][0] and rep["_import_wwnet_native"][0]
    if not rep["_import_anthropic"][0]:
        msgs.warn("anthropic is not importable: the assistant will not work (check/design/simulate do not need it).")
    if not rep["_import_wwnet_native"][0]:
        msgs.warn("wwnet_native is not importable: no WWNet tool except the assistant settings can run.")
    else:
        nat = shared.core("native")
        lacking = nat.missing_functions()
        if lacking:
            ok = False
            msgs.warn(f"wwnet_native is too old for this toolbox: missing {', '.join(lacking)}. Install the "
                      "current bundle.")
        else:
            msgs.info("wwnet_native provides every engine function this toolbox uses.")
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


def apply_last_proposals(arcpy, msgs: Messages, layers_fn=None) -> int:
    """Write exactly the edits the assistant proposed in the last run (for this project + map), after
    checking that every current value still equals the value seen when it was proposed."""
    key = map_key(arcpy)
    saved = assistant_run.load_proposals(key)
    if not saved:
        raise RuntimeError("No saved proposals: the last assistant run proposed no edits (or they were applied).")
    tools = arcpy_tools.ArcpyTools(arcpy, apply=True, layers_fn=layers_fn, emit=msgs.info)
    ok, conflicts = tools.adopt_saved(saved)
    if conflicts:
        for c in conflicts:
            msgs.warn(c)
        raise RuntimeError(f"Nothing applied: {len(conflicts)} of {len(saved)} proposal(s) no longer match the "
                           "data. Ask the assistant again.")
    n = tools.apply_proposals(ok)
    assistant_run.save_proposals(key, [])
    msgs.info(f"Applied {n} saved proposal(s).")
    return n


def ask_assistant(arcpy, msgs: Messages, question: str, layer_name: str | None = None,
                  use_selected: bool = False, model: str | None = None, effort: str | None = None,
                  apply: bool = False, reset: bool = False, client_factory=None, layers_fn=None,
                  apply_last: bool = False):
    if apply_last:
        return apply_last_proposals(arcpy, msgs, layers_fn=layers_fn)
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
    if layer_name:
        try:
            path, table = gpkg.split_datasource(maplayers.layer_source(tools.find_layer(layer_name)))
            if path and table and table.startswith("wwn_"):
                tools.model = path
        except Exception:  # noqa: BLE001 - selection_context reports a bad layer below
            pass
    context = selection_context(tools, layer_name, use_selected)
    msgs.info(f"Asking {model} (effort {effort}); edits will be "
              + ("APPLIED if proposed." if apply else "proposed only (nothing is written).")
              + " Tool results (attribute values) are sent to Anthropic.")
    hkey = map_key(arcpy)
    res = assistant_run.run(question, api_key=key, model=model, effort=effort, tools=tools, msgs=msgs,
                            history_key=hkey, reset=reset, context=context,
                            client_factory=client_factory)
    if res.error:
        msgs.error(res.error)
    pending = [p for p in res.proposals if not p.applied]
    if res.proposals and not apply:
        msgs.info("")
        msgs.info(f"PROPOSED EDITS ({len(res.proposals)}) - NOT applied:")
        for line in arcpy_tools.proposal_lines(res.proposals):
            msgs.info("  " + line)
        msgs.info("To write exactly these edits, run this tool again with 'Apply the proposals from the last "
                  "run' ticked (no new question is sent).")
    elif res.proposals:
        msgs.info(f"{sum(p.applied for p in res.proposals)} of {len(res.proposals)} proposed edit(s) applied.")
    assistant_run.save_proposals(hkey, arcpy_tools.proposal_dicts(pending))
    msgs.info(f"Done ({res.stop_reason}); tokens in/out: {res.tokens_in}/{res.tokens_out}.")
    return res
