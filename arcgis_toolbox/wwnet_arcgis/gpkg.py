"""GeoPackage helpers (sqlite3 only)."""
from __future__ import annotations

import os
import re
import sqlite3

from . import shared

ATTRIBUTE_ORDER = ("wwn_node_loads", "wwn_pumps")     # v2 tables worth showing in the map
FEATURE_ORDER = ("wwn_subcatchments", "wwn_rising_mains", "wwn_conduits",
                 "wwn_pump_stations", "wwn_nodes")      # bottom -> top


def table_path(gpkg: str, table: str) -> str:
    """ArcGIS catalog path of a GeoPackage table: <file>.gpkg\\main.<table>."""
    return f"{gpkg}{os.sep}main.{table}"


_SPLIT = re.compile(r"^(.*?\.gpkg)(?:[\\/]+(?:main\.)?([^\\/]+))?$", re.IGNORECASE)
# real Pro 3.x GeoPackage layer: 'Instance=C:\\m\\a.gpkg,Dataset=main.%wwn_nodes'
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
    """'C:\\m\\a.gpkg\\main.wwn_nodes' -> ('C:\\m\\a.gpkg', 'wwn_nodes'); also Pro's SQLite
    forms 'Instance=<file>.gpkg,Dataset=main.%<table>' and the SQLite connection string
    (see _split_connection_string); else (None, None)."""
    ds = ds or ""
    m = _INSTANCE_DATASET.match(ds)
    if m:
        return m.group(1), m.group(2)
    if "=" in ds and ";" in ds:
        return _split_connection_string(ds)
    m = _SPLIT.match(ds)
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
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT table_name, identifier, data_type FROM gpkg_contents").fetchall()
    finally:
        con.close()
    return {r[0]: (r[1], r[2]) for r in rows if r[0].startswith("wwn_")}


def create_model(path: str, overwrite: bool = False) -> str:
    return shared.core("schema").create_model(path, overwrite=overwrite)


def upgrade_model(path: str) -> bool:
    return shared.core("schema").upgrade_model(path)


def conduit_ends(gpkg: str) -> dict:
    con = sqlite3.connect(f"file:{gpkg}?mode=ro", uri=True)
    try:
        return {r[0]: (r[1], r[2]) for r in
                con.execute("SELECT conduit_id, us_node_id, ds_node_id FROM wwn_conduits")}
    finally:
        con.close()


def conduits_between(gpkg: str, nodes) -> list:
    nodes = set(nodes)
    return sorted(c for c, (u, d) in conduit_ends(gpkg).items() if u in nodes and d in nodes)


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
