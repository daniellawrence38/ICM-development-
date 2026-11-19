"""Pipe capacity calculator (uses wwnet_native when importable, else shared Python fallback)."""
from __future__ import annotations

from . import shared


def calculate(dn_mm: float, grade_1in: float, ks_mm: float | None = None,
              flow_lps: float | None = None) -> dict:
    t = shared.assistant("tools")
    args = {"dn_mm": dn_mm, "grade_1in": grade_1in}
    if ks_mm:
        args["ks_mm"] = ks_mm
    if flow_lps is not None:
        args["flow_lps"] = flow_lps
    res = t.pipe_capacity(args)
    if flow_lps is not None:
        chk = {"dn_mm": int(round(dn_mm)), "grade_1in": grade_1in, "gsdf_lps": flow_lps}
        if ks_mm:
            chk["ks_mm"] = ks_mm
        res["ds50_checks"] = t.check_conduit(chk)
    return res


def describe(res: dict) -> list:
    """Message lines for the geoprocessing pane."""
    lines = [f"DN{res['dn_mm']:g} at 1 in {res['grade_1in']:g}, ks {res['ks_mm']:g} mm "
             f"(source: {res.get('source')})",
             f"  Full-bore capacity: {res['full_flow_lps']:.2f} L/s"]
    if "velocity_mps" in res:
        lines.append(f"  Velocity (d/D {res['depth_ratio']:g}): {res['velocity_mps']:.2f} m/s")
    if "flow_lps" in res:
        if res.get("depth_ratio_at_flow") is None:
            lines.append(f"  Flow {res['flow_lps']:g} L/s exceeds open-channel capacity (surcharge).")
        else:
            lines.append(f"  At {res['flow_lps']:g} L/s: d/D = {res['depth_ratio_at_flow']:.3f}, "
                         f"velocity {res['velocity_at_flow_mps']:.2f} m/s")
    chk = res.get("ds50_checks")
    if chk:
        lines.append(f"  DS 50 checks ({chk.get('source')}):")
        for r in chk["results"]:
            lines.append(f"    [{str(r.get('result')).upper():4}] {r.get('rule')} "
                         f"({r.get('clause')}): value={r.get('value')} limit={r.get('limit')}")
    return lines
