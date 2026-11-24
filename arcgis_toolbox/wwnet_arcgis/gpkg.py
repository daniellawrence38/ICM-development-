"""GeoPackage helpers (sqlite3 only, read-only; writes go through the engine)."""
from __future__ import annotations

import os
import re
import sqlite3

from . import shared

ATTRIBUTE_ORDER = ("wwn_node_loads", "wwn_pumps")     # attribute tables worth showing in the map
FEATURE_ORDER = ("wwn_subcatchments", "wwn_rising_mains", "wwn_conduits",
                 "wwn_pump_stations", "wwn_nodes")      # bottom -> top
#: result / issue layers (schema v3), bottom -> top; added when they hold data
RESULT_ORDER = ("wwn_sim_compare", "wwn_sim_conduit_results", "wwn_results_conduits",
                "wwn_sim_node_results", "wwn_issues")


def table_path(gpkg: str, table: str) -> str:
    """ArcGIS catalog path of a GeoPackage table: <file>.gpkg\\main.<table>."""
    return f"{gpkg}{os.sep}main.{table}"


_SPLIT = re.compile(r"^(.*?\.gpkg)(?:[\\/]+(?:main\.)?([^\\/|]+))?(?:\|.*)?$", re.IGNORECASE)
# real Pro 3.x GeoPackage layer: 'Instance=C:\\m\\a.gpkg,Dataset=main.%wwn_nodes' (verified in Pro 3.3.5)
_INSTANCE_DATASET = re.compile(r"^\s*Instance=(.+?\.gpkg)\s*,\s*Dataset=(?:main\.)?%?([A-Za-z_]\w*)\s*$",
                               re.IGNORECASE)
_CONN_FILE_KEYS = ("DB_CONNECTION_PROPERTIES", "INSTANCE", "DATABASE")


def _split_connection_string(ds: str):
    """Pro reports SQLite-family layers (GeoPackage, mobile gdb) as a connection string with the
    dataset appended after the last separator, e.g.
    'AUTHENTICATION_MODE=OSA;main;DB_CONNECTION_PROPERTIES=C:\\m\\a.gpkg;INSTANCE=sde:sqlite:C:\\m\\a.gpkg;
    IS_GEODATABASE=false;IS_NOSERVER=0;SERVER=C:\\main.wwn_nodes'."""
    props = {}
    for part in ds.split(";"):
        k, sep, v = part.partition("=")
        if sep:
            props.setdefault(k.strip().upper(), v.strip())
    path = None
    for k in _CONN_FILE_KEYS:
        v = props.get(k, "")
        if v.lower().startswith("sde:sqlite:"):
            v = v[len("sde:sqlite:"):]
        if v.lower().endswith(".gpkg"):
            path = v
            break
    if path is None:
        return None, None
    tail = ds.rsplit(";", 1)[-1].partition("=")[2] or ds.rsplit(";", 1)[-1]
    last = re.sub(r"^main\.", "", re.split(r"[\\/]", tail)[-1], flags=re.IGNORECASE)
    return path, (last if re.fullmatch(r"[A-Za-z_]\w*", last) else None)


def split_datasource(ds: str):
    """(gpkg path, table) of a GeoPackage data source, in any form Pro / QGIS report it:
    'C:\\m\\a.gpkg\\main.wwn_nodes', Pro's 'Instance=<file>.gpkg,Dataset=main.%<table>', Pro's SQLite connection
    string (see _split_connection_string), '...gpkg/wwn_nodes' and QGIS '...gpkg|layername=wwn_nodes'. A bare
    '.gpkg' gives (path, None); anything else (None, None)."""
    text = (ds or "").strip()
    m = _INSTANCE_DATASET.match(text)
    if m:
        return m.group(1), m.group(2)
    if "=" in text and ";" in text:
        return _split_connection_string(text)
    m = re.match(r"^(.*?\.gpkg)\|layername=([^|]+)", text, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = _SPLIT.match(text)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def dataset_path(ds: str) -> str:
    """A path arcpy cursors accept: '<file>.gpkg\\main.<table>' for GeoPackage sources, else `ds`."""
    path, table = split_datasource(ds)
    return table_path(path, table) if path and table else ds


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
