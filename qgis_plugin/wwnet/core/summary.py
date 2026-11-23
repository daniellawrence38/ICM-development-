"""Host-agnostic result text for the front ends (no qgis / arcpy imports).

Every line is short "key facts" prose rather than a fixed-width table: the ArcGIS Messages pane and
the QGIS message log use proportional fonts. Full tables belong in the result layers.
"""
from __future__ import annotations

from . import checks

WORST_N = 10


def num(d: dict, key: str, default=0.0):
    v = d.get(key)
    return default if v is None else v


def opt(v, prec: int = 2, unit: str = "") -> str:
    return "-" if v is None else f"{v:.{prec}f}{unit}"


# ------------------------------------------------------------------ issues

def issue_line(i: dict) -> str:
    who = f"{i.get('object_type')} {i.get('object_id')}".strip() if i.get("object_id") else \
        str(i.get("object_type") or "model")
    clause = f" ({i['clause']})" if i.get("clause") else ""
    return f"{i.get('code')} {who}{clause}: {i.get('message')}"


def sorted_issues(issues: list) -> list:
    order = {"error": 0, "warning": 1}
    return sorted(issues, key=lambda i: order.get(i.get("severity"), 2))


def issue_count_line(issues: list, what: str = "Model check") -> str:
    c = checks.count_by_severity(issues)
    return f"{what}: {c['error']} error(s), {c['warning']} warning(s), {c['info']} info."


# ------------------------------------------------------------------ design

def failed_conduits(design: dict) -> list:
    return [c["conduit_id"] for c in design.get("conduit_results", []) if c.get("result") == "fail"]


def failed_checks(conduit_result: dict) -> list:
    """Fail checks first, then warn checks."""
    chks = [c for c in conduit_result.get("checks", []) if c.get("result") in ("fail", "warn")]
    return sorted(chks, key=lambda k: 0 if k.get("result") == "fail" else 1)


def worst_message(conduit_result: dict) -> str:
    chks = failed_checks(conduit_result)
    return str(chks[0].get("message") or "") if chks else ""


def design_summary_lines(d: dict) -> list:
    s = d["summary"]
    lines = [f"Design: {s['conduits']} conduit(s): {s['pass']} pass, {s['warn']} warn, {s['fail']} fail. "
             f"Terminal GSDF {s['terminal_gsdf_lps']:.3f} L/s (total local {s['total_local_gsdf_lps']:.3f} L/s)."]
    adwf = s.get("total_local_adwf_lps") or 0.0
    if adwf:
        lines.append(f"Loads: ADWF {adwf:.3f} L/s total ({s.get('terminal_adwf_lps') or 0.0:.3f} L/s at terminals); "
                     f"GSDF from loads = ADWF x {s.get('peaking_factor', 1.5):g} peaking factor.")
    return lines


def conduit_line(c: dict) -> str:
    grade = f"1:{c['grade_1in']:.0f}" if c.get("grade_1in") else "grade unknown"
    return (f"{c['conduit_id']} DN{int(num(c, 'dn'))} {grade}: {str(c.get('result')).upper()}, "
            f"flow {num(c, 'total_lps'):.2f} of {opt(c.get('capacity_lps'), 1)} L/s, "
            f"d/D {opt(c.get('d_over_d'))}" + (f". {worst_message(c)}" if worst_message(c) else ""))


def design_worst_lines(conduit_results: list, n: int = WORST_N) -> list:
    """The `n` worst conduits (fail, then warn; highest d/D first)."""
    rank = {"fail": 0, "warn": 1}
    bad = [c for c in conduit_results if c.get("result") in rank]
    bad.sort(key=lambda c: (rank[c["result"]], -(c.get("d_over_d") or 0.0)))
    lines = [conduit_line(c) for c in bad[:n]]
    if len(bad) > n:
        lines.append(f"... {len(bad) - n} more failing / warning conduit(s): see the Design results layer.")
    return lines


def design_detail_lines(conduit_results: list, per_rule: int = 3) -> list:
    """Per rule: how many conduits fail / warn, with up to `per_rule` examples."""
    by_rule: dict = {}
    for c in conduit_results:
        for chk in failed_checks(c):
            by_rule.setdefault(chk.get("rule"), []).append((c, chk))
    lines = []
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda it: 0 if it[1].get("result") == "fail" else 1)
        ex = ", ".join(str(c["conduit_id"]) for c, _ in items[:per_rule])
        more = f" and {len(items) - per_rule} more" if len(items) > per_rule else ""
        lines.append(f"{rule} ({items[0][1].get('clause')}): {len(items)} conduit(s), e.g. {ex}{more}.")
    return lines


def design_info_lines(d: dict) -> list:
    n = (d.get("summary") or {}).get("cover_not_checked_ends") or 0
    return [f"Cover not checked at {n} conduit end(s): no surface level (IS ends / junctions)."] if n else []


# ------------------------------------------------------------------ simulation

def run_title(run: dict) -> str:
    lbl = (run or {}).get("label") or ""
    rid = (run or {}).get("run_id") or ""
    return f"{lbl} ({rid})" if lbl and rid else (lbl or rid or "(not stored)")


def sim_summary_lines(d: dict) -> list:
    s, o = d["summary"], d.get("options") or {}
    rough = o.get("roughness", "ks")
    lines = [f"Simulation {run_title(d)}: {o.get('duration_h', 0):g} h ({o.get('warmup_h', 0):g} h warm-up), "
             f"roughness {rough}" + (f" n={o.get('manning_n'):g}" if rough == "manning" else " (Colebrook ks)")
             + f", flow scale {o.get('flow_scale', 1.0):g}.",
             f"Inflow ADWF {s['inflow_adwf_lps']:.3f} L/s (simulated {s['inflow_simulated_lps']:.3f} L/s); "
             f"peak outlet flow {s['peak_outlet_flow_lps']:.3f} L/s; continuity error {s['continuity_error_pct']:.3f} %.",
             f"{s['surcharged_conduits']} of {s['conduits']} conduit(s) surcharged; "
             f"{s['flooded_nodes']} node(s) flooded ({s['total_flood_volume_m3']:.2f} m3)."]
    return lines


def sim_worst_lines(links: list, n: int = WORST_N) -> list:
    """The `n` conduits with the highest peak d/D."""
    top = sorted(links, key=lambda x: -num(x, "max_d_over_d"))[:n]
    lines = [f"{c['conduit_id']} DN{int(num(c, 'dn'))}: peak {num(c, 'max_flow_lps'):.2f} L/s at "
             f"{c.get('time_of_max_flow') or '-'}, d/D {num(c, 'max_d_over_d'):.2f}, "
             f"{num(c, 'max_velocity_mps'):.2f} m/s" +
             (f", surcharged {num(c, 'surcharged_min'):.0f} min" if num(c, 'surcharged_min') else "")
             for c in top]
    if len(links) > n:
        lines.append(f"... {len(links) - n} more conduit(s): see the Simulation results layer.")
    return lines


def runs_lines(runs: list) -> list:
    if not runs:
        return ["No stored simulation runs: run Run Simulation first."]
    out = []
    for r in runs:
        s = r.get("summary") or {}
        peak = s.get("peak_outlet_flow_lps")
        out.append(("* " if r.get("shown") else "  ") + run_title(r) +
                   (f": peak outlet {peak:.3f} L/s" if isinstance(peak, (int, float)) else "") +
                   (f", {s.get('surcharged_conduits')} surcharged" if s.get("surcharged_conduits") is not None else "") +
                   (f", flow scale {(r.get('options') or {}).get('flow_scale'):g}"
                    if (r.get("options") or {}).get("flow_scale") is not None else ""))
    out.append("* = shown in the Simulation results layers.")
    return out


CHANGE_TEXT = {"new_surcharge": "newly surcharged", "relieved": "no longer surcharged", "worse": "worse",
               "better": "better", "same": "about the same", "only_a": "only in A", "only_b": "only in B"}


def compare_lines(c: dict, n: int = WORST_N) -> list:
    counts = c.get("counts") or {}
    lines = [f"Comparison A = {run_title(c['run_a'])}, B = {run_title(c['run_b'])} (B minus A):",
             ", ".join(f"{counts[k]} {CHANGE_TEXT.get(k, k)}" for k in CHANGE_TEXT if counts.get(k)) or "no conduits"]
    rows = [r for r in c.get("rows", []) if r.get("d_d_over_d") is not None]
    rows.sort(key=lambda r: -(r["d_d_over_d"] or 0.0))
    for r in rows[:n]:
        if (r.get("d_d_over_d") or 0.0) <= 0:
            break
        pct = r.get("d_flow_pct")
        lines.append(f"{r['conduit_id']}: d/D {opt(r.get('max_d_over_d_a'))} -> {opt(r.get('max_d_over_d_b'))}, "
                     f"peak {opt(r.get('max_flow_a_lps'))} -> {opt(r.get('max_flow_b_lps'))} L/s"
                     + (f" ({pct:+.0f} %)" if pct is not None else "") +
                     (" NEW SURCHARGE" if r.get("change") == "new_surcharge" else ""))
    return lines


# ------------------------------------------------------------------ trace

def trace_lines(res: dict, start: str) -> list:
    lines = []
    for key in ("upstream", "downstream"):
        ids = res.get(key)
        if ids is None:
            continue
        lines.append(f"{key.capitalize()} of {start}: {len(ids)} node(s)" +
                     (": " + ", ".join(ids[:30]) + (" ..." if len(ids) > 30 else "") if ids else "."))
    lines.append(f"{len(res.get('conduits') or [])} conduit(s) on the traced path(s).")
    s = res.get("summary") or {}
    if s:
        at = f"conduit {s['start_conduit']}" if s.get("start_conduit") else f"node {s.get('start_node')}"
        lines.append(f"Upstream of {at}: {s.get('upstream_nodes', 0)} node(s), {s.get('upstream_conduits', 0)} "
                     f"conduit(s), {s.get('upstream_connections', 0)} connection(s), "
                     f"{s.get('upstream_subcatchments', 0)} sub-catchment(s).")
        lines.append(f"Flow at {at}: ADWF {opt(s.get('adwf_lps'), 3)} L/s, design GSDF {opt(s.get('gsdf_lps'), 3)} L/s"
                     + (f", pumped {s['pumped_lps']:.3f} L/s" if s.get("pumped_lps") else "") + ".")
    return lines
