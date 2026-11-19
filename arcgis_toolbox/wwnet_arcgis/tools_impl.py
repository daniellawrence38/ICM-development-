"""Tool bodies for WWNet.pyt. Plain-value arguments in, messages out; arcpy injected."""
from __future__ import annotations

from . import (arcpy_tools, assistant_run, capacity, cli, dpapi, gpkg, issues_table, maplayers,
               settings, shared, symbology, vendor)
from .messages import Messages

MAX_LINES = 300


# --------------------------------------------------------------- model tools

def new_model(arcpy, msgs: Messages, out_path: str, add_to_map: bool = True, overwrite: bool = False):
    if not out_path.lower().endswith(".gpkg"):
        out_path += ".gpkg"
    gpkg.create_model(out_path, overwrite=overwrite)
    msgs.info(f"Created WWNet model {out_path} (EPSG:7850, schema v1).")
    if add_to_map:
        maplayers.add_model(arcpy, out_path, msgs)
    return out_path


def add_model_to_map(arcpy, msgs: Messages, gpkg_path: str, symbolize: bool = True):
    layers = maplayers.add_model(arcpy, gpkg_path, msgs, symbolize=symbolize)
    msgs.info(f"Added {len(layers)} layer(s) to the active map.")
    return layers


def report_issues(msgs: Messages, issues: list):
    counts = {"error": 0, "warning": 0, "info": 0}
    for n, i in enumerate(issues):
        sev = i["severity"] if i["severity"] in counts else "info"
        counts[sev] += 1
        if n >= MAX_LINES:
            continue
        line = f"{i['code']} {i['object_type']} {i['object_id']}" + \
               (f" ({i['clause']})" if i["clause"] else "") + f": {i['message']}"
        {"error": msgs.error, "warning": msgs.warn}.get(sev, msgs.info)(line)
    if len(issues) > MAX_LINES:
        msgs.info(f"... {len(issues) - MAX_LINES} more issue(s) not shown; use the output table.")
    return counts


def check_model(arcpy, msgs: Messages, gpkg_path: str, out_table: str | None = None, exe=None):
    issues, rc = cli.run_check(gpkg_path, exe)
    counts = report_issues(msgs, issues)
    msgs.info(f"Check finished: {counts['error']} error(s), {counts['warning']} warning(s), "
              f"{counts['info']} info.")
    if out_table:
        n = issues_table.write_issues_table(arcpy, out_table, issues)
        msgs.info(f"Wrote {n} issue(s) to {out_table}")
    return issues


def _select(arcpy, layer, ids, field):
    ds = layer.dataSource
    where = gpkg.in_clause(arcpy.AddFieldDelimiters(ds, field), ids)
    arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION" if ids else "CLEAR_SELECTION",
                                            where if ids else "")


RESULT_FIELDS = [("conduit_id", "TEXT"), ("dn", "LONG"), ("grade_1in", "DOUBLE"), ("gsdf_lps", "DOUBLE"),
                 ("pumped_lps", "DOUBLE"), ("total_lps", "DOUBLE"), ("capacity_lps", "DOUBLE"),
                 ("d_over_d", "DOUBLE"), ("velocity_mps", "DOUBLE"), ("result", "TEXT"), ("message", "TEXT")]


def _worst_message(c: dict) -> str:
    chks = cli.failed_checks(c)
    chks.sort(key=lambda k: 0 if k.get("result") == "fail" else 1)
    return str(chks[0].get("message") or "") if chks else ""


def _num(c, key, default=0.0):
    v = c.get(key)
    return default if v is None else v


def results_table_lines(conduit_results: list, max_lines: int = MAX_LINES) -> list:
    hdr = (f"{'Conduit':<10}{'DN':>5}{'Grade 1:x':>10}{'GSDF L/s':>9}{'Pumped':>8}{'Total L/s':>10}"
           f"{'Cap L/s':>9}{'d/D':>6}{'v m/s':>7}  {'RESULT':<6}  Worst message")
    lines = [hdr, "-" * len(hdr)]
    for c in conduit_results[:max_lines]:
        lines.append(f"{str(c['conduit_id'])[:10]:<10}{int(_num(c, 'dn')):>5}{_num(c, 'grade_1in'):>10.0f}"
                     f"{_num(c, 'gsdf_lps'):>9.2f}{_num(c, 'pumped_lps'):>8.2f}{_num(c, 'total_lps'):>10.2f}"
                     f"{_num(c, 'capacity_lps'):>9.1f}{_num(c, 'd_over_d'):>6.2f}{_num(c, 'velocity_mps'):>7.2f}"
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
            ins.insertRow([cid, int(_num(c, "dn")), _num(c, "grade_1in"), _num(c, "gsdf_lps"),
                           _num(c, "pumped_lps"), _num(c, "total_lps"), _num(c, "capacity_lps"),
                           _num(c, "d_over_d"), _num(c, "velocity_mps"), c["result"],
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


def run_design(arcpy, msgs: Messages, gpkg_path: str, show_layer: bool = True, add_table: bool = False,
               select_failed: bool = True, exe=None) -> dict:
    d = cli.run_design(gpkg_path, exe)
    s = d["summary"]
    msgs.info(f"Design run: {s['conduits']} conduit(s): {s['pass']} pass, {s['warn']} warn, "
              f"{s['fail']} fail. Terminal GSDF {s['terminal_gsdf_lps']:.3f} L/s "
              f"(total local {s['total_local_gsdf_lps']:.3f} L/s).")
    for line in results_table_lines(d["conduit_results"]):
        msgs.info(line)
    for c in d["conduit_results"]:
        if c["result"] == "pass":
            continue
        msgs.warn(f"{c['conduit_id']} DN{c['dn']} {c['result'].upper()}: capacity {c['capacity_lps']:.1f} L/s, "
                  f"flow {c['total_lps']:.2f} L/s, d/D {c['d_over_d']:.2f}")
        for chk in cli.failed_checks(c):
            msgs.warn(f"    {chk['rule']} ({chk['clause']}): {chk['message']}")
    for i in report_issues_sorted(d.get("issues", [])):
        msgs.warn(f"{i.get('code')} {i.get('object_type')} {i.get('object_id')} ({i.get('clause')}): "
                  f"{i.get('message')}")
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


def report_issues_sorted(issues):
    order = {"error": 0, "warning": 1}
    return sorted(issues, key=lambda i: order.get(i.get("severity"), 2))[:MAX_LINES]


def node_ids(arcpy, layer) -> list:
    with arcpy.da.SearchCursor(layer.dataSource if hasattr(layer, "dataSource") else layer,
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
        _select(arcpy, nodes_layer, sorted(traced), "node_id")
        cl = maplayers.find_layer_for(arcpy, path, "wwn_conduits")
        if cl is not None:
            _select(arcpy, cl, conduits, "conduit_id")
        else:
            msgs.warn("The conduit layer is not in the active map; only nodes were selected.")
    return {"nodes": sorted(traced), "conduits": conduits}


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
