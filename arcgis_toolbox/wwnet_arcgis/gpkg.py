"""GeoPackage helpers (sqlite3 only, read-only; writes go through the engine)."""
from __future__ import annotations

import os
import re
import sqlite3

from . import shared

ATTRIBUTE_ORDER = ("wwn_node_loads",)     # attribute tables worth showing in the map
FEATURE_ORDER = ("wwn_subcatchments", "wwn_rising_mains", "wwn_conduits",
                 "wwn_pump_stations", "wwn_nodes")      # bottom -> top
#: result / issue layers (schema v3), bottom -> top; added when they hold data
RESULT_ORDER = ("wwn_sim_compare", "wwn_sim_conduit_results", "wwn_results_conduits",
                "wwn_sim_node_results", "wwn_issues")


def table_path(gpkg: str, table: str) -> str:
    """ArcGIS catalog path of a GeoPackage table: <file>.gpkg\\main.<table>."""
    return f"{gpkg}{os.sep}main.{table}"


_SPLIT = re.compile(r"^(.*?\.gpkg)(?:[\\/]+(?:main\.)?([^\\/|]+))?(?:\|.*)?$", re.IGNORECASE)


def split_datasource(ds: str):
    """'C:\\m\\a.gpkg\\main.wwn_nodes' -> ('C:\\m\\a.gpkg', 'wwn_nodes'); also '...gpkg/wwn_nodes' and
    QGIS-style '...gpkg|layername=wwn_nodes'; a bare '.gpkg' gives (path, None); else (None, None)."""
    text = (ds or "").strip()
    m = re.match(r"^(.*?\.gpkg)\|layername=([^|]+)", text, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = _SPLIT.match(text)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def table_of(ds: str):
    return split_datasource(ds)[1]


def contents(gpkg: str) -> dict:
    """{table: (identifier, data_type)} for wwn_* tables present in the file."""
    return shared.core("schema").table_identifiers(gpkg, "wwn_")


def row_count(gpkg: str, table: str) -> int:
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def node_ids(gpkg: str) -> list:
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT node_id FROM wwn_nodes ORDER BY node_id")]
    finally:
        con.close()


def create_model(path: str, overwrite: bool = False) -> str:
    return shared.core("schema").create_model(path, overwrite=overwrite)


def upgrade_model(path: str) -> bool:
    return shared.core("schema").upgrade_model(path)


def sql_quote(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def in_clause(field_sql: str, values, chunk: int = 400) -> str:
    """Build `field IN (...) OR field IN (...)` (chunked); '1=0' if empty."""
    vals = list(values)
    if not vals:
        return "1=0"
    parts = [f"{field_sql} IN ({', '.join(sql_quote(v) for v in vals[i:i + chunk])})"
             for i in range(0, len(vals), chunk)]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _ro(gpkg: str):
    return sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)


def meta(gpkg: str) -> dict:
    """wwn_meta as {key: value} ({} if unreadable). Read-only: safe in parameter validation."""
    try:
        con = _ro(gpkg)
        try:
            return dict(con.execute("SELECT key, value FROM wwn_meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def profile_ids(gpkg: str) -> list:
    try:
        con = _ro(gpkg)
        try:
            return [r[0] for r in con.execute("SELECT DISTINCT profile_id FROM wwn_profiles ORDER BY profile_id")]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def series_ids(gpkg: str, run_id: str | None = None) -> list:
    """Objects with a stored time series in `run_id` (the newest run when None)."""
    try:
        con = _ro(gpkg)
        try:
            if not run_id:
                row = con.execute("SELECT run_id FROM wwn_sim_runs ORDER BY run_id DESC LIMIT 1").fetchone()
                run_id = row[0] if row else None
            if not run_id:
                return []
            return [r[0] for r in con.execute(
                "SELECT DISTINCT object_id FROM wwn_sim_series WHERE run_id=? ORDER BY object_id", (run_id,))]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def run_titles(gpkg: str) -> list:
    """'label (run_id)' (or the run id) of the stored simulation runs, newest first. Read-only."""
    try:
        con = _ro(gpkg)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(wwn_sim_runs)")}
            sql = ("SELECT run_id, label FROM wwn_sim_runs ORDER BY run_id DESC" if "label" in cols
                   else "SELECT run_id, NULL FROM wwn_sim_runs ORDER BY run_id DESC")
            return [f"{lbl} ({rid})" if lbl else rid for rid, lbl in con.execute(sql)]
        finally:
            con.close()
    except sqlite3.Error:
        return []
