"""Self Test: run every WWNet tool against the bundled demo model in a scratch folder and write ONE report.

Each step is isolated (exceptions are captured, later steps still run) and the messages a step emits are
captured for the report; only a one-line summary per step reaches the geoprocessing pane. The tool never
calls msgs.error, so the report is always written and Pro does not mark the tool as failed.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

MAX_STEP_LINES = 60
REPORT_NAME = "WWNet_selftest_report.txt"


class CapturingMessages:
    """Same interface as Messages; records (level, text) and forwards nothing."""

    def __init__(self):
        self.lines: list = []

    def info(self, text):
        self.lines.append(("info", str(text)))

    def warn(self, text):
        self.lines.append(("warning", str(text)))

    def error(self, text):
        self.lines.append(("error", str(text)))

    def step(self, text):          # progressor labels: not part of the report
        pass

    def start(self, text):
        pass


class Skip(Exception):
    """Raised by a step to report SKIP with a reason."""


def _first_existing(cands, what):
    for c in cands:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError(f"{what} not found (looked in: " + "; ".join(cands) + ")")


def _find_demo(root) -> str:
    root = os.path.abspath(str(root))
    return _first_existing([os.path.join(root, "templates", "demo", "demo.gpkg"),
                            os.path.join(os.path.dirname(root), "examples", "demo", "demo.gpkg")], "demo.gpkg")


def _find_profile(root) -> str:
    root = os.path.abspath(str(root))
    name = "example_residential.csv"
    return _first_existing([os.path.join(root, "templates", "profiles", name),
                            os.path.join(os.path.dirname(root), "templates", "profiles", name)], name)


def _first_node_id(gpkg_path: str) -> str:
    con = sqlite3.connect(gpkg_path)
    try:
        row = con.execute("SELECT node_id FROM wwn_nodes WHERE node_id IS NOT NULL "
                          "ORDER BY node_id LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("wwn_nodes is empty")
    return row[0]


def _map_snapshot(arcpy):
    from . import maplayers
    m = maplayers.active_map(arcpy)
    items = list(m.listLayers()) + list(m.listTables())
    had_group = any(getattr(x, "isGroupLayer", False) and maplayers.layer_name(x) == maplayers.GROUP_NAME
                    for x in m.listLayers())
    return {(maplayers.layer_name(x), maplayers.layer_source(x)) for x in items}, had_group


def _map_cleanup(arcpy, before, had_group) -> int:
    """Remove what the map steps added: the WWNet group (if we created it), then stray layers / tables."""
    from . import maplayers
    m = maplayers.active_map(arcpy)
    removed = 0
    if not had_group:
        for g in [x for x in m.listLayers() if getattr(x, "isGroupLayer", False)
                  and maplayers.layer_name(x) == maplayers.GROUP_NAME]:
            try:
                m.removeLayer(g)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
    for is_table, items in ((False, list(m.listLayers())), (True, list(m.listTables()))):
        for x in [x for x in items if (maplayers.layer_name(x), maplayers.layer_source(x)) not in before
                  and not getattr(x, "isGroupLayer", False)]:
            try:
                maplayers.remove_item(m, x, is_table)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
    return removed


def shared_native():
    from . import shared
    return shared.core("native")


def run(arcpy, msgs, root, out_dir=None, include_map=False, exe=None) -> dict:
    import importlib
    from . import __version__, engine, maplayers, vendor
    tools_impl = importlib.import_module(__package__ + ".tools_impl")  # via sys.modules: always the live copy

    started = datetime.datetime.now()
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "wwnet_selftest_" + started.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    copy = os.path.join(out_dir, "selftest.gpkg")
    state = {"snapshot": None}
    pro = {"text": "unknown"}

    def step_environment(c):
        c.info(f"Python {sys.version.split()[0]} ({sys.platform})")
        try:
            info = arcpy.GetInstallInfo()
            pro["text"] = f"{info.get('ProductName', '?')} {info.get('Version', '?')}"
        except Exception as exc:  # noqa: BLE001
            pro["text"] = f"unknown ({exc})"
        c.info(f"ArcGIS: {pro['text']}")
        c.info(f"WWNet toolbox version {__version__}")
        rep = vendor.report()
        for key, label in (("_import_wwnet_native", "wwnet_native"), ("_import_anthropic", "anthropic")):
            ok, err = rep[key]
            (c.info if ok else c.warn)(f"import {label}: " + ("OK" if ok else f"FAILED - {err}"))
        try:
            nat = engine.native()
            lacking = shared_native().missing_functions(nat)
            c.info(f"Engine: wwnet_native {nat.version()} (schema v{getattr(nat, 'SCHEMA_VERSION', '?')})")
            if lacking:
                c.warn(f"wwnet_native lacks {', '.join(lacking)}: install the full bundle")
        except Exception as exc:  # noqa: BLE001
            c.error(f"Engine not available: {exc}")
        c.info(f"Output folder: {out_dir}")

    def step_copy(c):
        src = _find_demo(root)
        shutil.copyfile(src, copy)
        c.info(f"Copied {src} -> {copy}")

    def need_model():
        if not os.path.isfile(copy):
            raise Skip("model copy not available (copy step failed)")

    def step_check(c):
        need_model()
        tools_impl.check_model(arcpy, c, copy, False)

    def step_design(c):
        need_model()
        tools_impl.run_design(arcpy, c, copy, False, False)

    def step_trace(c):
        need_model()
        nid = _first_node_id(copy)
        r = tools_impl.trace(arcpy, c, copy, nid, "Downstream", False)
        c.info(f"Traced from {nid}: {len(r['nodes'])} node(s), {len(r['conduits'])} conduit(s)")

    def step_capacity(c):
        tools_impl.pipe_capacity(c, 225, 300, 1.0, 5.0)

    def step_profile(c):
        need_model()
        tools_impl.import_profile(c, copy, "SELFTEST", _find_profile(root), "Self Test profile", False, False,
                                  None)

    def step_simulation(c):
        need_model()
        tools_impl.run_simulation(arcpy, c, copy, 2.0, 1.0, "manning", 0.013, 1.0, False, None, "selftest x1",
                                  False)

    def step_runs(c):
        need_model()
        tools_impl.run_simulation(arcpy, CapturingMessages(), copy, 2.0, 1.0, "manning", 0.013, 2.0, False, None,
                                  "selftest x2", False)
        tools_impl.show_simulation_run(arcpy, c, copy, "selftest x1", False)
        cmp_ = tools_impl.compare_simulation_runs(arcpy, c, copy, "selftest x1", "selftest x2", False)
        if not cmp_["rows"]:
            raise RuntimeError("the run comparison has no conduits")
        res = tools_impl.plot_hydrograph(arcpy, c, copy, "selftest x2", [], os.path.join(out_dir, "hydrograph.csv"),
                                         chart=False)
        c.info(f"Hydrograph series: {len(res['series'])}")

    def step_settings(c):
        need_model()
        st = tools_impl.model_settings(c, copy)
        if st["peaking_factor"] <= 0:
            raise RuntimeError("model settings unreadable")
        tools_impl.assign_profile(arcpy, c, copy, "All sub-catchments", "SELFTEST")
        tools_impl.assign_profile(arcpy, c, copy, "All sub-catchments", tools_impl.DEFAULT_PROFILE)

    def step_pump_simulation(c):
        need_model()
        from . import pumpdata
        pgp = os.path.join(out_dir, "selftest_pumps.gpkg")
        shutil.copyfile(_find_demo(root), pgp)
        con = sqlite3.connect(pgp)
        try:   # P08 now ends in a wet well WW; rising main PS1 -> D, then a short gravity pipe D -> OUT1
            geom = con.execute("SELECT geom FROM wwn_nodes WHERE node_id='OUT1'").fetchone()[0]
            for nid, typ, inv, sf in (("WW", "wet_well", 22.2, 25.5), ("D", "ac", 25.0, 28.0)):
                con.execute("INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
                            "VALUES (?,?,?,?,?)", (geom, nid, typ, inv, sf))
            con.execute("UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'")
            con.execute("INSERT INTO wwn_conduits (conduit_id, us_node_id, ds_node_id, dn, length_m, us_invert, "
                        "ds_invert) VALUES ('PSG1','D','OUT1',225,50.0,25.0,22.2)")
            con.execute("INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, "
                        "rising_main_id, notes) VALUES ('PS1','WW','D','RM1','Self Test station')")
            con.execute("INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, "
                        "roughness_type, roughness) VALUES ('RM1','PS1','D',150,200.0,'ks',1.0)")
            con.commit()
        finally:
            con.close()
        r = pumpdata.set_station(pgp, "PS1", wet_well_area_m2=1.0, stop=22.6, start=23.0, overflow=25.0,
                                 n_duty=1, n_standby=1,
                                 curve=[(0, 20), (10, 16.5), (20, 11.5), (30, 5)], source_note="Self Test")
        c.info(r["summary"])
        pc = CapturingMessages()
        d = tools_impl.run_simulation(arcpy, pc, pgp, 6.0, 2.0, "manning", 0.013, 1.0, False, None, "selftest pumps",
                                      False)
        if "pumps" not in d:
            raise Skip("the simulation engine has no pump support (no 'pumps' in the result)")
        c.lines.extend(pc.lines)
        text = "\n".join(t for _, t in pc.lines)
        if not d["pumps"] or not any(p["starts"] > 0 for p in d["pumps"]) or "Pump stations" not in text or "Rising mains" not in text:
            raise RuntimeError("pump station results are missing from the simulation messages")

    def step_export(c):
        need_model()
        tools_impl.export_icm(arcpy, c, copy, os.path.join(out_dir, "icm"))

    def step_pump_export(c):
        pgp = os.path.join(out_dir, "selftest_pumps.gpkg")
        if not os.path.isfile(pgp):
            raise Skip("no pump station model (the pump station simulation step did not create it)")
        r = tools_impl.export_icm(arcpy, c, pgp, os.path.join(out_dir, "icm_pumps"))
        mains = [x["wwn_conduit_id"] for x in r["conduits"] if x["wwn_link_kind"] == "rising_main"]
        if not r["pumps"] or not mains:
            raise RuntimeError("the rising main or the pumps are missing from the ICM export")
        c.info(f"ICM export of the pump station model: rising main(s) {', '.join(mains)}, "
               f"{len(r['pumps'])} pump(s) ({', '.join(x['link_type'] for x in r['pumps'])})")

    def step_map_add(c):
        need_model()
        state["snapshot"] = _map_snapshot(arcpy)
        tools_impl.add_model_to_map(arcpy, c, copy)

    def step_map_trace(c):
        need_model()
        nid = _first_node_id(copy)
        layer = maplayers.find_layer_for(arcpy, copy, "wwn_nodes")
        if layer is None:
            raise RuntimeError("The nodes layer was not found in the active map after Add Model To Map.")
        tools_impl.trace(arcpy, c, copy, nid, "Downstream", True)

    def step_map_remove(c):
        if state["snapshot"] is None:
            raise Skip("nothing was added to the map")
        c.info(f"Removed {_map_cleanup(arcpy, *state['snapshot'])} layer(s)/table(s) added by the test")

    steps = [("environment", step_environment), ("copy demo model", step_copy),
             ("check model", step_check), ("run design", step_design), ("trace", step_trace),
             ("pipe capacity", step_capacity), ("import diurnal profile", step_profile),
             ("run simulation (2 h, 1 h warm-up)", step_simulation),
             ("simulation runs: show / compare / hydrograph", step_runs), ("model settings / assign profile", step_settings),
             ("pump station simulation", step_pump_simulation),
             ("export to ICM", step_export), ("export to ICM (pump station)", step_pump_export)]
    if include_map:
        steps += [("map: add model to map", step_map_add), ("map: trace with selection", step_map_trace),
                  ("map: remove added layers", step_map_remove)]

    results = []
    for name, fn in steps:
        cap = CapturingMessages()
        t0 = time.perf_counter()
        status, tb, err = "PASS", "", ""
        try:
            fn(cap)
        except Skip as exc:
            status, err = "SKIP", str(exc)
        except Exception as exc:  # noqa: BLE001
            status, tb, err = "FAIL", traceback.format_exc(), f"{type(exc).__name__}: {exc}"
        secs = time.perf_counter() - t0
        if status == "PASS":
            if any(lv == "error" for lv, _ in cap.lines):
                status = "FAIL"
                err = next(t for lv, t in cap.lines if lv == "error")
            elif any(lv == "warning" for lv, _ in cap.lines):
                status = "WARN"
        results.append({"name": name, "status": status, "seconds": secs, "error": err,
                        "lines": cap.lines, "traceback": tb})
        line = f"[{status}] {name} ({secs:.1f} s)" + (f": {err}" if status in ("FAIL", "SKIP") and err else "")
        (msgs.warn if status == "FAIL" else msgs.info)(line)

    out = ["WWNet Self Test report", f"Date: {started.strftime('%Y-%m-%d %H:%M:%S')}",
           f"Toolbox version: {__version__}", f"Python: {sys.version.split()[0]}",
           f"ArcGIS Pro: {pro['text']}", f"Output folder: {out_dir}", "", "Summary"]
    for r in results:
        out.append(f"{r['status']:<5} {r['seconds']:6.1f} s  {r['name']}")
    out.append("")
    for r in results:
        out.append(f"=== {r['name']} [{r['status']}] ===")
        if r["error"]:
            out.append(f"Reason: {r['error']}")
        for lv, text in r["lines"][:MAX_STEP_LINES]:
            out.append(f"  {lv.upper():<7} {text}")
        if len(r["lines"]) > MAX_STEP_LINES:
            out.append(f"  ... {len(r['lines']) - MAX_STEP_LINES} more line(s) truncated")
        if r["traceback"]:
            out.append("  Traceback:")
            out.extend("    " + t for t in r["traceback"].rstrip().splitlines())
        out.append("")
    report = os.path.join(out_dir, REPORT_NAME)
    with open(report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")

    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    msgs.info(f"Report: {report}")
    msgs.info("Paste the report file contents to Claude.")
    msgs.info(f"{counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed, "
              f"{counts['SKIP']} skipped")
    return {"report": report, "out_dir": out_dir,
            "steps": [{"name": r["name"], "status": r["status"], "seconds": r["seconds"], "error": r["error"]}
                      for r in results]}
