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
import types

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


def run(arcpy, msgs, root, out_dir=None, include_map=False, exe=None) -> dict:
    import importlib
    from . import __version__, cli, gpkg, maplayers, vendor
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
        if cli.native_service() is not None:
            c.info("Engine: wwnet_native")
        else:
            c.info("Engine: wwnet CLI (no native module)")
            try:
                c.info(f"CLI: {exe or cli.find_exe()}")
            except Exception as exc:  # noqa: BLE001
                c.warn(f"No native module and no CLI found: {exc}")
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
        tools_impl.check_model(arcpy, c, copy, None, exe)

    def step_design(c):
        need_model()
        tools_impl.run_design(arcpy, c, copy, False, False, False, exe)

    def step_trace(c):
        need_model()
        nid = _first_node_id(copy)
        layer = types.SimpleNamespace(name="Nodes", dataSource=gpkg.table_path(copy, "wwn_nodes"))
        r = tools_impl.trace(arcpy, c, layer, nid, "Downstream", False, exe)
        c.info(f"Traced from {nid}: {len(r['nodes'])} node(s), {len(r['conduits'])} conduit(s)")

    def step_capacity(c):
        tools_impl.pipe_capacity(c, 225, 300, 1.0, 5.0)

    def step_profile(c):
        need_model()
        tools_impl.import_profile(c, copy, "SELFTEST", _find_profile(root), "Self Test profile", False, False,
                                  None, exe)

    def step_simulation(c):
        need_model()
        tools_impl.run_simulation(arcpy, c, copy, 2.0, 1.0, "manning", 0.013, 1.0, False, None, exe)

    def step_export(c):
        need_model()
        tools_impl.export_icm(arcpy, c, copy, os.path.join(out_dir, "icm"))

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
        tools_impl.trace(arcpy, c, layer, nid, "Downstream", True, exe)

    def step_map_remove(c):
        if state["snapshot"] is None:
            raise Skip("nothing was added to the map")
        c.info(f"Removed {_map_cleanup(arcpy, *state['snapshot'])} layer(s)/table(s) added by the test")

    steps = [("environment", step_environment), ("copy demo model", step_copy),
             ("check model", step_check), ("run design", step_design), ("trace", step_trace),
             ("pipe capacity", step_capacity), ("import diurnal profile", step_profile),
             ("run simulation (2 h, 1 h warm-up)", step_simulation), ("export to ICM", step_export)]
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
