"""Tool definitions and the pure-Python tool implementations (no qgis imports)."""
from __future__ import annotations

from ..core import hydraulics, native, rules, ruleset, schema

_NUM = {"type": "number"}
_STR = {"type": "string"}


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
              "flow, Table 4.4 flow limit, IO/IS lengths). Returns a list of "
              "{rule, clause, value, limit, result}. grade_1in is the x of '1 in x'.",
              {"dn_mm": {"type": "integer"}, "grade_1in": _NUM, "gsdf_lps": _NUM,
               "sewer_type": {"type": "string", "enum": ["reticulation", "io", "is", "main"]},
               "land_use": {"type": "string", "enum": ["residential", "commercial", "hotel_motel",
                                                       "perth_cba", "industrial"]},
               "ks_mm": _NUM, "pumped_lps": _NUM, "length_m": _NUM},
              ["dn_mm", "grade_1in"]),
        _tool("pipe_capacity",
              "Colebrook-White capacity of a partially full circular pipe (ks default 1.0 mm, "
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


# ---------------------------------------------------------------- pure tools

def pipe_capacity(args: dict) -> dict:
    dn = float(args["dn_mm"])
    grade = float(args["grade_1in"])
    ks = float(args.get("ks_mm") or ruleset.load_ruleset()["hydraulics"]["ks_mm"])
    nat = native.get()
    out: dict = {"dn_mm": dn, "grade_1in": grade, "ks_mm": ks}

    def part_full(y):
        if nat is not None:
            try:
                return float(nat.part_full_flow_lps(dn, grade, y, ks)), "wwnet"
            except Exception:
                pass
        return hydraulics.part_full_flow_lps(dn, grade, y, ks), "python-colebrook-white"

    def vel(y, q):
        area, _ = hydraulics.geometry(dn / 1000.0, y)
        return (q / 1000.0) / area if area > 0 else 0.0

    y_in = args.get("depth_ratio")
    if y_in is not None or args.get("flow_lps") is None:
        y = 1.0 if y_in is None else float(y_in)
        q, src = part_full(y)
        out.update(depth_ratio=y, capacity_lps=round(q, 3), velocity_mps=round(vel(y, q), 3),
                   source=src)
    qf, src = part_full(1.0)
    out["full_flow_lps"] = round(qf, 3)
    if args.get("flow_lps") is not None:
        q = float(args["flow_lps"])
        y = None
        if nat is not None:
            try:
                y = nat.depth_ratio_for_flow(q, dn, grade, ks)
                src = "wwnet"
            except Exception:
                y = None
        if y is None:
            y = hydraulics.depth_ratio_for_flow(q, dn, grade, ks)
        if y is None:
            out.update(flow_lps=q, depth_ratio_at_flow=None,
                       note="flow exceeds open-channel capacity (pipe would surcharge)")
        else:
            qy, _ = part_full(y)
            out.update(flow_lps=q, depth_ratio_at_flow=round(y, 4),
                       velocity_at_flow_mps=round(vel(y, qy), 3))
    out["source"] = src
    return out


_NATIVE_LAND_USE = {
    "residential": "residential",
    "small_commercial": "small_commercial",
    "commercial": "industrial_large_commercial",
    "hotel_motel": "industrial_large_commercial",
    "perth_cba": "industrial_large_commercial",
    "industrial": "industrial_large_commercial",
}


def _native_kwargs(kw: dict) -> dict:
    """Map the tool schema's argument names onto wwnet_native.check_conduit's signature."""
    out = {"dn": int(round(kw["dn_mm"])), "grade_1in": float(kw["grade_1in"]),
           "gsdf_lps": float(kw.get("gsdf_lps") or 0.0)}
    if kw.get("length_m") is not None:
        out["length_m"] = float(kw["length_m"])
    if kw.get("pumped_lps") is not None:
        out["pumped_inflow_lps"] = float(kw["pumped_lps"])
    if kw.get("land_use"):
        out["land_use_class"] = _NATIVE_LAND_USE.get(kw["land_use"], "residential")
    for k in ("us_cover_m", "ds_cover_m", "location", "levels_uncertain"):
        if kw.get(k) is not None:
            out[k] = kw[k]
    return out


def check_conduit(args: dict) -> dict:
    kwargs = {k: v for k, v in args.items() if v is not None}
    nat = native.get()
    if nat is not None and callable(getattr(nat, "check_conduit", None)):
        try:
            return {"source": "wwnet", "results": nat.check_conduit(**_native_kwargs(kwargs))}
        except Exception as exc:  # wrong kwargs for this build etc.
            note = f"wwnet.check_conduit failed ({type(exc).__name__}: {exc}); used Python fallback"
            return {"source": "python-fallback", "note": note, "results": rules.check_conduit(**kwargs)}
    return {"source": "python-fallback", "results": rules.check_conduit(**kwargs)}


def lookup_ds50(args: dict):
    return ruleset.lookup(args["table"], args.get("key"))


def residential_gsdf(args: dict) -> dict:
    return ruleset.residential_gsdf(float(args["net_area_ha"]), args["rcode"],
                                    args["ground"], args["region"])


def pure_handlers() -> dict:
    return {"pipe_capacity": pipe_capacity, "check_conduit": check_conduit,
            "lookup_ds50": lookup_ds50, "residential_gsdf": residential_gsdf}


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
