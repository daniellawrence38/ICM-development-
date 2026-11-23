"""Tool definitions and the host-independent tool implementations (no qgis / arcpy imports).

Calculations go through ``wwnet_native`` (``core.calc``); the flow-modelling tools read the model
GeoPackage through the engine and the stored result tables. Host tools (QgisTools / ArcpyTools)
add the layer-reading and edit tools and tell :func:`flow_handlers` which model file is open.
"""
from __future__ import annotations

import sqlite3

from ..core import calc, native, ruleset, schema, summary
from .engine import ToolError

_NUM = {"type": "number"}
_STR = {"type": "string"}
MAX_RESULT_ROWS = 50


def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": required, "additionalProperties": False},
    }


def tool_definitions() -> list:
    """Deterministic order: the tool list is part of the cached prefix."""
    return [
        _tool("list_layers",
              "List the layers in the open QGIS project: table name, geometry type, feature "
              "count, selected count, fields and the business id field of each WWNet layer.",
              {}, []),
        _tool("get_selected_features",
              "Return the attributes (no geometry) of the features currently selected in a layer "
              "(max 100). Layer may be a WWNet table name (wwn_conduits) or a layer name.",
              {"layer": _STR}, ["layer"]),
        _tool("query_features",
              "Return attributes of features matching a QGIS filter expression, e.g. "
              "\"dn\" = 225 AND \"status\" = 'proposed'. Use limit to cap rows (default 50, max 200).",
              {"layer": _STR, "expression": _STR, "limit": {"type": "integer"}},
              ["layer", "expression"]),
        _tool("get_feature",
              "Get one feature by its business id value (node_id, conduit_id, ps_id, rm_id, "
              "subcatchment_id, pump_id), including length and grade where derivable.",
              {"layer": _STR, "id_value": _STR}, ["layer", "id_value"]),
        _tool("check_conduit",
              "Check one gravity sewer against DS 50 (min DN, min/max grade, max d/D at the design "
              "flow, Table 4.4 flow limit, cover, AC spacing). Returns {results: [{rule, clause, value, limit, "
              "result, message}]}. grade_1in is the x of '1 in x'.",
              {"dn_mm": {"type": "integer"}, "grade_1in": _NUM, "gsdf_lps": _NUM,
               "land_use": {"type": "string", "enum": ["residential", "small_commercial", "commercial",
                                                       "hotel_motel", "perth_cba", "industrial"]},
               "pumped_lps": _NUM, "length_m": _NUM, "us_cover_m": _NUM, "ds_cover_m": _NUM},
              ["dn_mm", "grade_1in"]),
        _tool("pipe_capacity",
              "Colebrook-White capacity of a partially full circular pipe of any DN (ks default 1.0 mm, "
              "internal diameter = DN). Give depth_ratio (d/D, default 1.0 = full) to get capacity "
              "in L/s and velocity; give flow_lps to get the depth ratio and velocity at that flow.",
              {"dn_mm": {"type": "integer"}, "grade_1in": _NUM, "depth_ratio": _NUM,
               "ks_mm": _NUM, "flow_lps": _NUM},
              ["dn_mm", "grade_1in"]),
        _tool("residential_gsdf",
              "Residential Sewer Design Flow (GSDF, L/s) for a net area, from DS 50 Tables 4.1/4.2.",
              {"net_area_ha": _NUM, "rcode": _STR,
               "ground": {"type": "string", "enum": ["dry", "wet"]},
               "region": {"type": "string", "enum": ["south", "north"]}},
              ["net_area_ha", "rcode", "ground", "region"]),
        _tool("lookup_ds50",
              "Look up a DS 50 ruleset table. table: meta, loading, residential (key 'south:R20' "
              "or 'R20'), non_residential (key 'south:industrial'), hydraulics, sizing, grades "
              "(key = DN, e.g. '225'; omit for all), io_is_sewers.",
              {"table": {"type": "string", "enum": list(ruleset.LOOKUP_TABLES)}, "key": _STR},
              ["table"]),
        _tool("design_summary",
              "Run the steady DS 50 design of the open model in memory (nothing is written) and return the "
              "summary (pass / warn / fail counts, terminal GSDF, ADWF) and the worst conduits.",
              {}, []),
        _tool("get_design_result",
              "Design result of one conduit (in memory, nothing written): accumulated ADWF / GSDF / pumped "
              "flow, governing / Table 4.4 / hydraulic capacity, d/D, velocity and every DS 50 check.",
              {"conduit_id": _STR}, ["conduit_id"]),
        _tool("list_sim_runs",
              "List the stored dynamic (SWMM) simulation runs, newest first: run_id, label, options "
              "(duration, warm-up, roughness, flow scale) and summary (inflow ADWF, peak outlet flow, "
              "surcharged / flooded counts).",
              {}, []),
        _tool("get_sim_result",
              "Stored simulation peaks of one conduit or node in a run (run: run id, label, 'latest' or "
              "'previous'; default latest): conduit max flow, depth, d/D, velocity, time of peak, minutes "
              "surcharged; node max HGL, depth, freeboard, flood volume. Also returns whether a time series "
              "is stored for it.",
              {"object_id": _STR, "run": _STR}, ["object_id"]),
        _tool("compare_sim_runs",
              "Compare two stored simulation runs per conduit (B minus A; nothing is written): counts of "
              "worse / better / same / newly surcharged / relieved conduits and the largest d/D increases.",
              {"run_a": _STR, "run_b": _STR}, ["run_a", "run_b"]),
        _tool("trace_network",
              "Trace upstream and/or downstream from a node or a conduit id: the nodes and conduits on the "
              "path and, upstream of the start, the node / conduit / connection / sub-catchment counts, "
              "the ADWF (as simulated) and the design GSDF arriving there.",
              {"start_id": _STR, "direction": {"type": "string", "enum": ["upstream", "downstream", "both"]}},
              ["start_id"]),
        _tool("propose_edits",
              "Propose attribute edits. NEVER writes directly: the engineer sees a confirmation "
              "dialog and may reject. Each edit: id = business id value (e.g. conduit_id 'C12'), "
              "field, value. Returns whether edits were applied.",
              {"layer": _STR,
               "edits": {"type": "array", "items": {
                   "type": "object",
                   "properties": {"id": _STR, "field": _STR,
                                  "value": {"anyOf": [{"type": "string"}, {"type": "number"},
                                                      {"type": "null"}]}},
                   "required": ["id", "field", "value"], "additionalProperties": False}},
               "reason": _STR},
              ["layer", "edits", "reason"]),
    ]


FLOW_TOOLS = ("design_summary", "get_design_result", "list_sim_runs", "get_sim_result", "compare_sim_runs",
              "trace_network")


# ---------------------------------------------------------------- pure tools

def _native_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except native.NativeMissing as exc:
        raise ToolError(str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise ToolError(str(exc.args[0] if exc.args else exc)) from exc


def pipe_capacity(args: dict) -> dict:
    return _native_call(calc.pipe_capacity, args["dn_mm"], args["grade_1in"], args.get("ks_mm"),
                        args.get("depth_ratio"), args.get("flow_lps"))


def check_conduit(args: dict) -> dict:
    kwargs = {k: v for k, v in args.items() if v is not None}
    return _native_call(calc.check_conduit, **kwargs)


def lookup_ds50(args: dict):
    return ruleset.lookup(args["table"], args.get("key"))


def residential_gsdf(args: dict) -> dict:
    return _native_call(ruleset.residential_gsdf, float(args["net_area_ha"]), args["rcode"],
                        args["ground"], args["region"])


def pure_handlers() -> dict:
    return {"pipe_capacity": pipe_capacity, "check_conduit": check_conduit,
            "lookup_ds50": lookup_ds50, "residential_gsdf": residential_gsdf}


# ---------------------------------------------------------------- flow-modelling tools

def _compact_conduit(c: dict) -> dict:
    keys = ("conduit_id", "dn", "grade_1in", "adwf_lps", "gsdf_lps", "pumped_lps", "total_lps", "capacity_lps",
            "table44_lps", "hydraulic_capacity_lps", "d_over_d", "velocity_mps", "result")
    out = {k: c.get(k) for k in keys}
    out["worst_message"] = summary.worst_message(c)
    return out


def flow_handlers(model_path) -> dict:
    """Handlers of FLOW_TOOLS. `model_path()` returns the open model's .gpkg (raise ToolError if none)."""

    def nat(*fns):
        try:
            return native.require(*fns)
        except native.NativeMissing as exc:
            raise ToolError(str(exc)) from exc

    def design_summary(args):
        d = _native_call(nat("design").design, model_path(), None, False)
        rank = {"fail": 0, "warn": 1}
        worst = sorted((c for c in d["conduit_results"] if c.get("result") in rank),
                       key=lambda c: (rank[c["result"]], -(c.get("d_over_d") or 0)))
        return {"summary": d["summary"], "worst_conduits": [_compact_conduit(c) for c in worst[:15]],
                "non_conduit_issues": [summary.issue_line(i) for i in d.get("issues", [])
                                       if i.get("object_type") != "conduit"][:20]}

    def get_design_result(args):
        d = _native_call(nat("design").design, model_path(), None, False)
        cid = str(args["conduit_id"])
        c = next((c for c in d["conduit_results"] if str(c["conduit_id"]) == cid), None)
        if c is None:
            raise ToolError(f"No conduit {cid!r} in the design results")
        out = _compact_conduit(c)
        out["checks"] = c.get("checks", [])
        return out

    def list_sim_runs(args):
        runs = _native_call(nat("sim_runs").sim_runs, model_path())
        return {"runs": runs, "count": len(runs)}

    def get_sim_result(args):
        gpkg = model_path()
        runs = _native_call(nat("sim_runs").sim_runs, gpkg)
        key = (args.get("run") or "latest").strip()
        pick = {"latest": runs[:1], "": runs[:1], "previous": runs[1:2]}.get(key) or \
            [r for r in runs if key in (r["run_id"], r.get("label"))]
        if not pick:
            raise ToolError("No stored simulation run " + repr(key) + "; stored: " +
                            ", ".join(summary.run_title(r) for r in runs) if runs else
                            "No stored simulation runs: run a simulation first")
        run = pick[0]
        oid = str(args["object_id"])
        con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            link = con.execute("SELECT * FROM wwn_sim_links WHERE run_id=? AND conduit_id=?",
                               (run["run_id"], oid)).fetchone()
            node = con.execute("SELECT s.*, n.surface_level, n.surface_level - s.max_hgl_m AS freeboard_m "
                               "FROM wwn_sim_nodes s LEFT JOIN wwn_nodes n USING (node_id) "
                               "WHERE s.run_id=? AND s.node_id=?", (run["run_id"], oid)).fetchone()
            series = con.execute("SELECT count(*) FROM wwn_sim_series WHERE run_id=? AND object_id=?",
                                 (run["run_id"], oid)).fetchone()[0]
        finally:
            con.close()
        if link is None and node is None:
            raise ToolError(f"{oid!r} is neither a conduit nor a node of run {summary.run_title(run)}")
        out = {"run": summary.run_title(run), "time_series_points": series}
        if link is not None:
            out["conduit"] = {k: link[k] for k in link.keys() if k != "run_id"}
        if node is not None:
            out["node"] = {k: node[k] for k in node.keys() if k != "run_id"}
        return out

    def compare_sim_runs(args):
        c = _native_call(nat("compare_sim_runs").compare_sim_runs, model_path(), args["run_a"], args["run_b"],
                         False)
        rows = sorted((r for r in c["rows"] if r.get("d_d_over_d") is not None),
                      key=lambda r: -(r["d_d_over_d"] or 0))
        return {"run_a": summary.run_title(c["run_a"]), "run_b": summary.run_title(c["run_b"]),
                "counts": c["counts"], "largest_d_over_d_increase": rows[:MAX_RESULT_ROWS // 5]}

    def trace_network(args):
        d = (args.get("direction") or "upstream").lower()
        res = _native_call(nat("trace").trace, model_path(), str(args["start_id"]), d in ("upstream", "both"),
                           d in ("downstream", "both"))
        for k in ("upstream", "downstream", "conduits"):
            if k in res and len(res[k]) > 200:
                res[k + "_truncated"] = len(res[k])
                res[k] = res[k][:200]
        return res

    return {"design_summary": design_summary, "get_design_result": get_design_result,
            "list_sim_runs": list_sim_runs, "get_sim_result": get_sim_result,
            "compare_sim_runs": compare_sim_runs, "trace_network": trace_network}


def conduit_derived(row: dict, geometry_length=None) -> dict:
    """Length used and grade from levels for a conduit attribute dict (shared by both hosts)."""
    length = row.get("length_m") or geometry_length
    us, ds = row.get("us_invert"), row.get("ds_invert")
    derived = {"length_m_used": length}
    if length and us is not None and ds is not None and us > ds:
        derived["grade_1in"] = round(length / (us - ds), 1)
    return derived


# ------------------------------------------------------- edit validation (pure)

IMMUTABLE_FIELDS = {"fid", "geom"}


def coerce_value(value, kind: str):
    """kind: 'int' | 'real' | 'text'. Returns (ok, value_or_error)."""
    if value is None:
        return True, None
    try:
        if kind == "int":
            f = float(value)
            if f != int(f):
                return False, f"{value!r} is not an integer"
            return True, int(f)
        if kind == "real":
            return True, float(value)
    except (TypeError, ValueError):
        return False, f"{value!r} is not a number"
    return True, str(value) if not isinstance(value, str) else value


def validate_edits(table: str, edits: list, field_types: dict, enums: dict | None = None):
    """Validate proposed edits against schema. Returns (valid_edits, problems).

    field_types: {field_name: 'int'|'real'|'text'}.
    """
    enums = enums if enums is not None else schema.field_enums()
    if table in schema.READ_ONLY_TABLES:
        return [], [f"{table} is read-only"]
    id_field = schema.ID_FIELDS.get(table)
    valid, problems = [], []
    for i, e in enumerate(edits or []):
        fld = e.get("field")
        if fld not in field_types:
            problems.append(f"edit {i}: unknown field {fld!r}")
            continue
        if fld in IMMUTABLE_FIELDS or fld == id_field:
            problems.append(f"edit {i}: field {fld!r} cannot be edited by the assistant")
            continue
        ok, val = coerce_value(e.get("value"), field_types[fld])
        if not ok:
            problems.append(f"edit {i}: {fld}: {val}")
            continue
        allowed = enums.get((table, fld))
        if allowed and val is not None and val not in allowed:
            problems.append(f"edit {i}: {fld}={val!r} not in {allowed}")
            continue
        if val is None and fld in ("dn",):
            problems.append(f"edit {i}: {fld} cannot be null")
            continue
        valid.append({"id": str(e.get("id")), "field": fld, "value": val})
    return valid, problems
