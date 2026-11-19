"""Pure-Python DS 50 single-conduit checks (fallback for wwnet.check_conduit)."""
from __future__ import annotations

from . import hydraulics, ruleset


def _res(rule, clause, value, limit, result, note=None):
    d = {"rule": rule, "clause": clause, "value": value, "limit": limit, "result": result}
    if note:
        d["note"] = note
    return d


def max_d_over_d(dn: int, rs: dict) -> float:
    m = rs["hydraulics"]["max_d_over_D"]
    return float(m.get(str(dn), m["default"]))


def check_conduit(dn_mm, grade_1in, gsdf_lps=None, sewer_type="reticulation",
                  ks_mm=None, land_use="residential", pumped_lps=None,
                  length_m=None, **_ignored) -> list:
    rs = ruleset.load_ruleset()
    out = []
    dn = int(round(dn_mm))
    g = next((x for x in rs["grades"] if x["dn"] == dn), None)
    if g is None:
        return [_res("dn_in_table_4_4", "Table 4.4", dn, [x["dn"] for x in rs["grades"]], "fail",
                     "DN is outside the DS 50 range (DN150 to DN600)")]
    ks = float(ks_mm) if ks_mm else float(rs["hydraulics"]["ks_mm"])
    st = (sewer_type or "reticulation").lower()
    steep_ok = st in ("io", "is")

    # minimum diameter
    if land_use in ("industrial", "commercial", "hotel_motel", "perth_cba"):
        min_dn = rs["sizing"]["min_dn_industrial_large_commercial"]
        clause = "4.2.1(o)"
    else:
        min_dn = rs["sizing"]["min_dn_residential"]
        clause = "4.2.1(n)"
    out.append(_res("min_dn", clause, dn, min_dn, "pass" if dn >= min_dn else "fail"))

    # minimum grade (flatter = larger 1-in-x)
    min_g = g["min_grade_1in"]
    cond = g.get("conditional_min_grade_1in")
    if grade_1in <= min_g:
        out.append(_res("min_grade", "Table 4.4", grade_1in, min_g, "pass"))
    elif cond and grade_1in <= cond:
        if dn == 150:
            out.append(_res("min_grade", "4.2.1(q)", grade_1in, min_g, "warn",
                            "1:250 is permitted only with justification"))
        else:
            ok = pumped_lps is not None and pumped_lps >= g.get("conditional_min_pumped_lps", 14.0)
            out.append(_res("min_grade", "4.2.1(r)", grade_1in, min_g, "warn" if ok else "fail",
                            "1:350 only where a pump station discharges >= 14 L/s into the sewer"))
    else:
        out.append(_res("min_grade", "Table 4.4", grade_1in, min_g, "fail"))
    if dn == 225 and land_use in ("industrial", "commercial") and gsdf_lps is not None:
        lim = rs["sizing"]["industrial_dn225_low_flow"]
        if gsdf_lps < lim["gsdf_lt_lps"] and grade_1in > lim["min_grade_1in"]:
            out.append(_res("industrial_dn225_low_flow", "4.2.1(s)", grade_1in,
                            lim["min_grade_1in"], "fail"))

    # maximum grade
    if grade_1in >= g["max_grade_1in"]:
        out.append(_res("max_grade", "Table 4.4", grade_1in, g["max_grade_1in"], "pass"))
    else:
        out.append(_res("max_grade", "Table 4.4", grade_1in, g["max_grade_1in"],
                        "warn" if steep_ok else "fail",
                        "IS/IO sewers may be steeper (Table 4.4 note 1)" if steep_ok else None))

    # capacity at design flow
    if gsdf_lps is not None:
        lim = max_d_over_d(dn, rs)
        y = hydraulics.depth_ratio_for_flow(gsdf_lps, dn, grade_1in, ks)
        out.append(_res("max_d_over_D", "4.2.1(m)", None if y is None else round(y, 4), lim,
                        "pass" if (y is not None and y <= lim + 1e-9) else "fail",
                        "flow exceeds open-channel capacity" if y is None else None))
        band = next((b for b in g["bands"] if (b[0] == 0 or grade_1in >= b[0]) and grade_1in <= b[1]), None)
        if band:
            out.append(_res("table_4_4_max_flow", "Table 4.4", gsdf_lps, band[2],
                            "pass" if gsdf_lps <= band[2] + 1e-9 else "fail",
                            "conditional band" if band[3] else None))
    if st == "io" and length_m and length_m > rs["io_is_sewers"]["io_max_length_m"]:
        out.append(_res("io_length", "4.2.2", length_m, rs["io_is_sewers"]["io_max_length_m"], "fail"))
    if st == "is" and length_m and length_m > rs["io_is_sewers"]["is_max_length_m"]:
        out.append(_res("is_length", "4.2.2", length_m, rs["io_is_sewers"]["is_max_length_m"], "fail"))
    return out
