"""Pipe capacity and single-conduit DS 50 checks through wwnet_native (no qgis / arcpy imports).

Shared by the assistant tools and the ArcGIS Pipe Capacity calculator.
"""
from __future__ import annotations

from . import native, ruleset

#: assistant / tool land-use names -> wwnet_native.check_conduit land_use_class
NATIVE_LAND_USE = {
    "residential": "residential",
    "small_commercial": "small_commercial",
    "commercial": "industrial_large_commercial",
    "hotel_motel": "industrial_large_commercial",
    "perth_cba": "industrial_large_commercial",
    "industrial": "industrial_large_commercial",
}


def default_ks_mm() -> float:
    return float(ruleset.load_ruleset()["hydraulics"]["ks_mm"])


def ds50_dns() -> list:
    """The DNs covered by DS 50 Table 4.4 (the checks only apply to these)."""
    return [int(g["dn"]) for g in ruleset.load_ruleset()["grades"]]


def pipe_capacity(dn_mm, grade_1in, ks_mm=None, depth_ratio=None, flow_lps=None) -> dict:
    """Colebrook-White capacity of a circular pipe (any DN). With neither `depth_ratio` nor `flow_lps`
    the capacity is given at d/D 1.0 (full)."""
    ks = float(ks_mm) if ks_mm else default_ks_mm()
    if depth_ratio is None and flow_lps is None:
        depth_ratio = 1.0
    nat = native.require("pipe_capacity")
    return dict(nat.pipe_capacity(float(dn_mm), float(grade_1in), ks,
                                  None if depth_ratio is None else float(depth_ratio),
                                  None if flow_lps is None else float(flow_lps)))


def check_conduit(dn_mm, grade_1in, gsdf_lps=None, land_use=None, pumped_lps=None, length_m=None,
                  us_cover_m=None, ds_cover_m=None, location=None, levels_uncertain=None, **_ignored) -> dict:
    """DS 50 checks of one gravity sewer: {"source": "wwnet", "results": [{rule, clause, value, limit,
    result, message}], "in_ds50_range": bool}."""
    dn = int(round(float(dn_mm)))
    kw = {"dn": dn, "grade_1in": float(grade_1in), "gsdf_lps": float(gsdf_lps or 0.0)}
    if length_m is not None:
        kw["length_m"] = float(length_m)
    if pumped_lps is not None:
        kw["pumped_inflow_lps"] = float(pumped_lps)
    if land_use:
        kw["land_use_class"] = NATIVE_LAND_USE.get(land_use, "residential")
    for k, v in (("us_cover_m", us_cover_m), ("ds_cover_m", ds_cover_m), ("location", location),
                 ("levels_uncertain", levels_uncertain)):
        if v is not None:
            kw[k] = v
    res = native.require("check_conduit").check_conduit(**kw)
    return {"source": "wwnet", "in_ds50_range": dn in ds50_dns(), "results": list(res)}


def capacity_lines(res: dict) -> list:
    """Message lines for a pipe_capacity result (plus `ds50_checks` if present)."""
    lines = [f"DN{res['dn_mm']:g} at 1 in {res['grade_1in']:g}, ks {res['ks_mm']:g} mm "
             f"(internal diameter {res.get('internal_diameter_mm', res['dn_mm']):g} mm)",
             f"  Full-bore capacity: {res['full_flow_lps']:.2f} L/s, velocity {res['full_velocity_mps']:.2f} m/s"]
    if "capacity_lps" in res and res.get("depth_ratio") not in (None, 1.0):
        lines.append(f"  At d/D {res['depth_ratio']:g}: {res['capacity_lps']:.2f} L/s, "
                     f"velocity {res['velocity_mps']:.2f} m/s")
    if "flow_lps" in res:
        if res.get("depth_ratio_at_flow") is None:
            lines.append(f"  Flow {res['flow_lps']:g} L/s exceeds the open-channel capacity (surcharge).")
        else:
            lines.append(f"  At {res['flow_lps']:g} L/s: d/D = {res['depth_ratio_at_flow']:.3f}, "
                         f"velocity {res['velocity_at_flow_mps']:.2f} m/s")
    chk = res.get("ds50_checks")
    if chk:
        if not chk.get("in_ds50_range", True):
            lines.append(f"  DS 50 checks: DN{res['dn_mm']:g} is outside DS 50 Table 4.4 "
                         f"(DN{min(ds50_dns())} to DN{max(ds50_dns())}); capacity only.")
        else:
            lines.append("  DS 50 checks:")
            for r in chk["results"]:
                lines.append(f"    {str(r.get('result')).upper()}: {r.get('rule')} ({r.get('clause')}) "
                             f"value {r.get('value')}, limit {r.get('limit')}. {r.get('message') or ''}".rstrip())
    return lines
