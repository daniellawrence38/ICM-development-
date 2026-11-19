"""Locate bundled resources (schema.sql, rulesets) with a dev-tree fallback."""
from __future__ import annotations

from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PLUGIN_DIR / "resources"


def _repo_root_candidates():
    # qgis_plugin/wwnet/core/paths.py -> repo root is 3 levels above wwnet/
    here = PLUGIN_DIR
    for parent in list(here.parents)[:4]:
        yield parent


def find_resource(kind: str, name: str) -> Path:
    """kind is 'templates' or 'rulesets'. Prefer packaged copy in resources/."""
    packaged = RESOURCES_DIR / name
    if packaged.is_file():
        return packaged
    for root in _repo_root_candidates():
        cand = root / kind / name
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        f"{name} not found in {RESOURCES_DIR} or any parent {kind}/ directory. "
        "Run `python package.py` to bundle resources."
    )


def schema_sql_path() -> Path:
    return find_resource("templates", "schema.sql")


def ruleset_path(name: str = "ds50_v2r4.toml") -> Path:
    return find_resource("rulesets", name)
