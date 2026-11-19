"""Run the native check (CLI as fallback) and normalise its issues (no qgis imports)."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess

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


def find_cli():
    return shutil.which("wwnet")


def normalise_issue(raw: dict) -> dict:
    out = {}
    for field, names in ALIASES.items():
        v = next((raw[n] for n in names if n in raw and raw[n] is not None), "")
        out[field] = v if isinstance(v, (int, float)) else str(v)
    out["severity"] = str(out["severity"]).lower() or "info"
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


def parse_issues(stdout: str) -> list:
    data = json.loads(stdout)
    if isinstance(data, dict):
        for k in ("issues", "results", "findings", "diagnostics"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            data = []
    return [normalise_issue(x) for x in data if isinstance(x, dict)]


def read_issue_table(gpkg: str) -> list:
    con = sqlite3.connect(gpkg)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM wwn_issues ORDER BY fid").fetchall()
        return [normalise_issue(dict(r)) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _run_cli(exe: str, gpkg: str, timeout: int):
    try:
        proc = subprocess.run([exe, "check", gpkg, "--json"], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"wwnet check failed to run: {exc}"
    try:
        issues = parse_issues(proc.stdout)
    except (ValueError, TypeError):
        detail = (proc.stderr or proc.stdout).strip()[:500]
        return [], f"wwnet check returned unparseable output (exit {proc.returncode}): {detail}"
    return issues, f"wwnet check finished (exit {proc.returncode}); {len(issues)} issue(s)."


def run_check(gpkg: str, timeout: int = 300):
    """Returns (issues, message).

    Order: native wwnet_native module, then the `wwnet` CLI (fallback only), then the stored
    wwn_issues table with a message explaining how to install the wheel.
    """
    nat = native.service()
    if nat is not None:
        try:
            raw = nat.check(gpkg, True)
        except Exception as exc:  # noqa: BLE001 - surface any native error to the dock
            return [], f"wwnet check failed: {exc}"
        issues = [normalise_issue(x) for x in raw if isinstance(x, dict)]
        return issues, f"wwnet check finished (native); {len(issues)} issue(s)."
    exe = find_cli()
    if exe:
        return _run_cli(exe, gpkg, timeout)
    return read_issue_table(gpkg), "wwnet_native is not installed; showing stored wwn_issues only. " + native.INSTALL_HINT
