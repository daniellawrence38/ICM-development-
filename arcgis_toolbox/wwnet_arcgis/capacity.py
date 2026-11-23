"""Pipe capacity calculator: the shared engine calculation (wwnet_shared.core.calc)."""
from __future__ import annotations

from . import shared


def calculate(dn_mm: float, grade_1in: float, ks_mm: float | None = None,
              flow_lps: float | None = None) -> dict:
    """Colebrook-White capacity for any DN; with a flow, the depth at that flow and (DN150 to DN600)
    the DS 50 checks."""
    calc = shared.core("calc")
    res = calc.pipe_capacity(dn_mm, grade_1in, ks_mm, None, flow_lps)
    if flow_lps is not None:
        res["ds50_checks"] = calc.check_conduit(dn_mm, grade_1in, gsdf_lps=flow_lps)
    return res


def describe(res: dict) -> list:
    """Message lines for the geoprocessing pane."""
    return shared.core("calc").capacity_lines(res)
