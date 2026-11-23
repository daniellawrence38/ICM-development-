"""GeoPackage schema handling (sqlite3 only; no qgis imports)."""
from __future__ import annotations

import os
import re
import sqlite3

from . import native, paths

SCHEMA_VERSION = 3
#: editable model layers
FEATURE_TABLES = ("wwn_nodes", "wwn_conduits", "wwn_pump_stations", "wwn_rising_mains",
                  "wwn_subcatchments")
#: result / issue map layers written by the engine (schema v3), in map order bottom -> top
RESULT_LAYERS = ("wwn_sim_compare", "wwn_sim_conduit_results", "wwn_results_conduits",
                 "wwn_sim_node_results", "wwn_issues")
ATTRIBUTE_TABLES = ("wwn_pumps", "wwn_meta", "wwn_node_loads", "wwn_profile_info", "wwn_profiles",
                    "wwn_sim_runs", "wwn_sim_links", "wwn_sim_nodes", "wwn_sim_series")
ALL_TABLES = FEATURE_TABLES + RESULT_LAYERS + ATTRIBUTE_TABLES
READ_ONLY_TABLES = RESULT_LAYERS + ("wwn_meta", "wwn_sim_runs", "wwn_sim_links", "wwn_sim_nodes",
                                    "wwn_sim_series")

# Business id column per table (used by the assistant tools and issue navigation).
ID_FIELDS = {
    "wwn_nodes": "node_id", "wwn_conduits": "conduit_id", "wwn_pump_stations": "ps_id",
    "wwn_pumps": "pump_id", "wwn_rising_mains": "rm_id",
    "wwn_subcatchments": "subcatchment_id", "wwn_results_conduits": "conduit_id",
    "wwn_node_loads": "load_id", "wwn_profile_info": "profile_id",
    "wwn_sim_conduit_results": "conduit_id", "wwn_sim_node_results": "node_id", "wwn_sim_compare": "conduit_id",
}

# Which enumeration (from the schema.sql header) applies to which field.
ENUM_FIELDS = {
    "node_type": [("wwn_nodes", "node_type")],
    "status": [(t, "status") for t in ("wwn_nodes", "wwn_conduits", "wwn_pump_stations",
                                       "wwn_rising_mains", "wwn_subcatchments")],
    "data_source": [(t, "data_source") for t in ("wwn_nodes", "wwn_conduits",
                                                 "wwn_pump_stations", "wwn_rising_mains")],
    "material": [("wwn_conduits", "material"), ("wwn_rising_mains", "material")],
    "location": [("wwn_conduits", "location")],
    "sewer_type": [("wwn_conduits", "sewer_type")],
    "land_use": [("wwn_subcatchments", "land_use")],
    "ground": [("wwn_subcatchments", "ground")],
    "region": [("wwn_subcatchments", "region")],
}
# Not in the header comment but documented inline in schema.sql.
EXTRA_ENUMS = {("wwn_pumps", "role"): ["duty", "assist", "standby"]}
DEFAULT_VALUES = {"status": "proposed", "data_source": "design"}

_ENUM_RE = re.compile(r"(\w+):\s+([a-z_]+(?:\s*\|\s*[a-z_]+)*)")


def schema_sql_text() -> str:
    return paths.schema_sql_path().read_text(encoding="utf-8")


def parse_enums(sql: str | None = None) -> dict:
    """Parse the '-- Enumerations' header comment into {enum_name: [values]}."""
    sql = sql if sql is not None else schema_sql_text()
    enums, active = {}, False
    for line in sql.splitlines():
        if not line.startswith("--"):
            if active:
                break
            continue
        body = line[2:]
        if "Enumerations" in body:
            active = True
            continue
        if active:
            for m in _ENUM_RE.finditer(body):
                enums[m.group(1)] = [v.strip() for v in m.group(2).split("|")]
    return enums


def field_enums(sql: str | None = None) -> dict:
    """{(table, field): [allowed values]}"""
    enums = parse_enums(sql)
    out = dict(EXTRA_ENUMS)
    for name, targets in ENUM_FIELDS.items():
        for tbl, fld in targets:
            if name in enums:
                out[(tbl, fld)] = enums[name]
    return out


def create_model(path: str, overwrite: bool = False) -> str:
    """Create a new, empty WWNet GeoPackage (current schema) with the engine (wwnet_native)."""
    path = os.fspath(path)
    nat = native.require("new_model")
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(path)
        os.remove(path)
    nat.new_model(path)
    return path


def upgrade_model(path: str) -> bool:
    """Bring a model file up to the current schema with the engine. Idempotent; returns True if the
    file was older than the current schema."""
    nat = native.require("upgrade_model")
    con = sqlite3.connect(os.fspath(path))
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if "wwn_subcatchments" not in names:
        raise ValueError(f"{path} is not a WWNet model (no wwn_subcatchments table)")
    before = schema_version(path)
    nat.upgrade_model(os.fspath(path))
    return before < SCHEMA_VERSION


def schema_version(path: str) -> int:
    """The file's wwn_meta schema_version (1 when missing)."""
    con = sqlite3.connect(f"file:{os.fspath(path)}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM wwn_meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    try:
        return int(row[0]) if row else 1
    except (TypeError, ValueError):
        return 1


def table_identifiers(gpkg: str, prefix: str = "") -> dict:
    """{table_name: (identifier, data_type)} from gpkg_contents (read-only), optionally only names
    starting with `prefix` (e.g. 'wwn_')."""
    con = sqlite3.connect(f"file:{os.fspath(gpkg)}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT table_name, identifier, data_type FROM gpkg_contents").fetchall()
    finally:
        con.close()
    return {r[0]: (r[1], r[2]) for r in rows if r[0].startswith(prefix)}


def column_summary(sql: str | None = None) -> str:
    """Compact text description of every wwn_* table (for the AI system prompt)."""
    sql = sql if sql is not None else schema_sql_text()
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(sql)
        enums = field_enums(sql)
        lines = []
        for tbl in ALL_TABLES:
            cols = []
            for _cid, name, typ, notnull, dflt, pk in con.execute(f"PRAGMA table_info({tbl})"):
                s = f"{name} {typ or ''}".strip()
                if notnull and not pk:
                    s += " NOT NULL"
                if dflt is not None:
                    s += f" DEFAULT {dflt}"
                if (tbl, name) in enums:
                    s += " {" + "|".join(enums[(tbl, name)]) + "}"
                cols.append(s)
            kind = "features" if tbl in FEATURE_TABLES + RESULT_LAYERS else "attributes"
            ro = " [read-only: written by check / design / simulate]" if tbl in READ_ONLY_TABLES else ""
            lines.append(f"{tbl} ({kind}{ro}): " + ", ".join(cols))
        return "\n".join(lines)
    finally:
        con.close()
