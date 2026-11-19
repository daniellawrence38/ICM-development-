"""Tool bodies for WWNet.pyt. Plain-value arguments in, messages out; arcpy injected."""
from __future__ import annotations

from . import (arcpy_tools, assistant_run, capacity, cli, dpapi, gpkg, issues_table, maplayers,
               settings, shared)
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


def run_design(arcpy, msgs: Messages, gpkg_path: str, add_table: bool = True, join: bool = False,
               select_failed: bool = True, exe=None) -> dict:
    d = cli.run_design(gpkg_path, exe)
    s = d["summary"]
    msgs.info(f"Design run: {s['conduits']} conduit(s): {s['pass']} pass, {s['warn']} warn, "
              f"{s['fail']} fail. Terminal GSDF {s['terminal_gsdf_lps']:.3f} L/s "
              f"(total local {s['total_local_gsdf_lps']:.3f} L/s).")
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
    if add_table:
        try:
            maplayers.add_results_table(arcpy, gpkg_path, msgs)
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not add the results table to the map: {exc}")
    lyr = None
    if select_failed or join:
        try:
            lyr = maplayers.find_layer_for(arcpy, gpkg_path, "wwn_conduits")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not inspect the map: {exc}")
        if lyr is None:
            msgs.warn("The model's conduit layer is not in the active map; skipping select/join.")
    if lyr is not None and select_failed:
        failed = cli.failed_conduits(d)
        try:
            _select(arcpy, lyr, failed, "conduit_id")
            msgs.info(f"Selected {len(failed)} failed conduit(s)." if failed else "No failed conduits.")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not select failed conduits: {exc}")
    if lyr is not None and join:
        try:
            arcpy.management.AddJoin(lyr, "conduit_id", gpkg.table_path(gpkg_path, "wwn_results_conduits"),
                                     "conduit_id")
            msgs.info("Joined wwn_results_conduits to the conduit layer (remove it from Layer Properties > Joins).")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not join the results table: {exc}")
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
