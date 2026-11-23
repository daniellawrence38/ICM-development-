"""The WWNet engine (``wwnet_native``) for the toolbox: thin calls with friendly errors.

There is no fallback: every analysis runs in the native module that ships in the no-install bundle's
``vendor`` folder (Verify Setup shows whether it imports).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import shared

TOOLBOX_ROOT = Path(__file__).resolve().parent.parent


class EngineError(RuntimeError):
    pass


def _core(name):
    return shared.core(name)


def native(*functions: str):
    """The native module (vendor folder activated first); EngineError with install help if missing."""
    from . import vendor
    vendor.activate(TOOLBOX_ROOT)
    nat = _core("native")
    try:
        return nat.require(*functions)
    except nat.NativeMissing as exc:
        raise EngineError(str(exc)) from exc


def available() -> bool:
    try:
        native()
        return True
    except EngineError:
        return False


def call(name: str, *args, **kwargs):
    """wwnet_native.<name>(*args, **kwargs); any engine exception becomes EngineError(message)."""
    fn = getattr(native(name), name)
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - native raises KeyError / ValueError / RuntimeError / IOError
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        if isinstance(exc, KeyError):
            msg = f"{msg!s} is neither a node nor a conduit of the model" if name == "trace" else msg
        raise EngineError(str(msg)) from exc


# ------------------------------------------------------------------ services

def run_check(gpkg: str) -> list:
    """Model check (replaces the `check` rows of the Issues layer); normalised issue dicts."""
    norm = _core("checks").normalise_issue
    return [norm(x) for x in call("check", gpkg, True) if isinstance(x, dict)]


def run_design(gpkg: str) -> dict:
    """Design JSON (conduit_results, node_flows, issues, summary); writes the Design results layer."""
    return call("design", gpkg, None, True)


def run_simulate(gpkg: str, duration_h: float = 48.0, warmup_h: float = 24.0, roughness: str = "ks",
                 n: float = 0.013, flow_scale: float = 1.0, write_inp: str | None = None, label: str = "",
                 series_ids=None) -> dict:
    """SWMM dynamic simulation; results go to the wwn_sim_* tables and the Simulation results layers."""
    return call("simulate", gpkg, duration_h, warmup_h, roughness, n, flow_scale, True,
                list(series_ids) if series_ids else None, write_inp or None, label or "")


def run_trace(gpkg: str, start: str, up: bool = True, down: bool = False) -> dict:
    return call("trace", gpkg, start, up, down)


def run_import_profile(gpkg: str, profile_id: str, text: str, description: str = "", set_default: bool = True,
                       normalise: bool = False) -> dict:
    return call("import_profile", gpkg, profile_id, text, description, set_default, normalise)


def sim_runs(gpkg: str) -> list:
    return call("sim_runs", gpkg)


def show_sim_run(gpkg: str, run: str = "latest") -> dict:
    return call("show_sim_run", gpkg, run or "latest")


def compare_sim_runs(gpkg: str, run_a: str = "previous", run_b: str = "latest", write: bool = True) -> dict:
    return call("compare_sim_runs", gpkg, run_a or "previous", run_b or "latest", write)


def sim_series(gpkg: str, run: str = "latest", ids=None) -> dict:
    return call("sim_series", gpkg, run or "latest", list(ids) if ids else None)


def get_settings(gpkg: str) -> dict:
    return call("get_settings", gpkg)


def set_settings(gpkg: str, adwf_per_connection_lpd=None, peaking_factor=None, default_profile=None) -> dict:
    return call("set_settings", gpkg, adwf_per_connection_lpd, peaking_factor, default_profile)


def assign_profile(gpkg: str, table: str, ids, profile_id: str) -> int:
    return call("assign_profile", gpkg, table, list(ids), profile_id or "")


def store_issues(gpkg: str, source: str, issues: list) -> int:
    return call("store_issues", gpkg, source, json.dumps(issues, default=str))


def write_model(gpkg: str, model_json: str, overwrite: bool = False):
    return call("write_model", gpkg, model_json, overwrite)


def run_key(title: str) -> str:
    """Run id from a 'label (run_id)' title (the engine also accepts a label or the id itself)."""
    t = (title or "").strip()
    if t.endswith(")") and " (" in t:
        return t[t.rindex(" (") + 2:-1]
    return t
