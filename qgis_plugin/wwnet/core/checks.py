"""Run the model check through wwnet_native and normalise issues (no qgis imports)."""
from __future__ import annotations

import sqlite3

from . import native

ALIASES = {
    "severity": ("severity", "level", "sev"),
    "code": ("code", "rule"),
    "object_type": ("object_type", "entity", "kind", "type", "layer"),
    "object_id": ("object_id", "object", "feature_id", "element", "target", "id"),
    "clause": ("clause", "ref", "reference"),
    "message": ("message", "msg", "text", "description"),
}
TYPE_TO_TABLE = (
    ("node", "wwn_nodes"), ("conduit", "wwn_conduits"), ("pipe", "wwn_conduits"),
    ("sewer", "wwn_conduits"), ("pump_station", "wwn_pump_stations"),
    ("pumpstation", "wwn_pump_stations"), ("ps", "wwn_pump_stations"),
    ("rising", "wwn_rising_mains"), ("subcatch", "wwn_subcatchments"), ("pump", "wwn_pumps"),
)
SEVERITIES = ("error", "warning", "info")


def normalise_issue(raw: dict) -> dict:
    out = {}
    for field, names in ALIASES.items():
        v = next((raw[n] for n in names if n in raw and raw[n] is not None), "")
        out[field] = v if isinstance(v, (int, float)) else str(v)
    out["severity"] = str(out["severity"]).lower() or "info"
    if out["severity"] == "warn":
        out["severity"] = "warning"
    if raw.get("source"):
        out["source"] = str(raw["source"])
    out["table"] = issue_table(out["object_type"])
    return out


def issue_table(object_type) -> str | None:
    t = str(object_type).lower().replace("-", "_").replace(" ", "_")
    if t.startswith("wwn_"):
        return t
    for key, tbl in TYPE_TO_TABLE:
        if key in t:
            return tbl
    return None


def count_by_severity(issues) -> dict:
    """{'error': n, 'warning': n, 'info': n} (unknown severities count as info)."""
    counts = dict.fromkeys(SEVERITIES, 0)
    for i in issues:
        sev = i.get("severity") if isinstance(i, dict) else None
        counts[sev if sev in counts else "info"] += 1
    return counts


def read_issue_table(gpkg: str) -> list:
    """Stored wwn_issues rows (all sources), errors first."""
    con = sqlite3.connect(gpkg)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM wwn_issues ORDER BY fid").fetchall()
        out = [normalise_issue(dict(r)) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()
    order = {s: n for n, s in enumerate(SEVERITIES)}
    return sorted(out, key=lambda i: order.get(i["severity"], 3))


def run_check(gpkg: str):
    """Returns (issues, message). Without wwnet_native: the stored wwn_issues and the install hint."""
    nat = native.service()
    if nat is None:
        return read_issue_table(gpkg), "wwnet_native is not installed; showing stored issues only. " + \
            native.INSTALL_HINT
    try:
        raw = nat.check(gpkg, True)
    except Exception as exc:  # noqa: BLE001 - surface any native error to the dock
        return [], f"Model check failed: {exc}"
    issues = [normalise_issue(x) for x in raw if isinstance(x, dict)]
    c = count_by_severity(issues)
    return issues, (f"Model check: {c['error']} error(s), {c['warning']} warning(s), {c['info']} info.")
