"""Access to the ``wwnet_native`` PyO3 extension (the WWNet engine: check, design, trace, simulate,
hydraulics). It is required: there are no pure-Python copies of the engine.

NAME CLASH WARNING: the QGIS plugin package is itself called ``wwnet`` (QGIS imports plugins by
folder name), so a top-level ``import wwnet`` may return the plugin instead of the extension. We
therefore probe several candidate module names and accept only a module that actually exposes the
expected functions.
"""
from __future__ import annotations

import importlib

_CANDIDATES = ("wwnet_native", "wwnet._native", "wwnet.wwnet", "wwnet")
_REQUIRED = ("part_full_flow_lps", "depth_ratio_for_flow")
#: functions the front ends call; an older wheel without them is reported by Verify Setup
SERVICE_FUNCTIONS = ("check", "design", "trace", "simulate", "write_model", "new_model", "upgrade_model",
                     "import_profile", "store_issues", "sim_runs", "show_sim_run", "compare_sim_runs",
                     "sim_series", "get_settings", "set_settings", "assign_profile", "pipe_capacity",
                     "check_conduit", "residential_gsdf", "schema_version")
_cache: dict = {}

INSTALL_HINT = (
    "The WWNet engine (wwnet_native) is not installed. ArcGIS Pro: use the no-install bundle (CI artifact "
    "wwnet-arcgis-bundle-win-py311) and run Verify Setup. QGIS: pip install "
    "wwnet_native-<version>-cp39-abi3-win_amd64.whl into QGIS's Python (OSGeo4W Shell)."
)


class NativeMissing(RuntimeError):
    """wwnet_native is not importable, or is too old for this front end."""


def get(refresh: bool = False):
    """Return the native module or None."""
    # Only successes are cached: a miss is retried, because the module may become importable
    # later (e.g. ArcGIS Pro resets sys.path between tool runs; the vendor folder is re-added).
    if refresh or _cache.get("mod") is None:
        _cache["mod"] = None
        for name in _CANDIDATES:
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue
            if all(callable(getattr(mod, f, None)) for f in _REQUIRED):
                _cache["mod"] = mod
                break
    return _cache["mod"]


def available() -> bool:
    return get() is not None


def missing_functions(mod=None) -> list:
    """Service functions the installed module lacks (all of them when it is not installed)."""
    mod = mod if mod is not None else get()
    return [f for f in SERVICE_FUNCTIONS if not callable(getattr(mod, f, None))]


def require(*functions: str):
    """The native module; raises NativeMissing (with install instructions) when it is missing or lacks
    any of `functions`."""
    mod = get()
    if mod is None:
        raise NativeMissing(INSTALL_HINT)
    lacking = [f for f in functions if not callable(getattr(mod, f, None))]
    if lacking:
        raise NativeMissing(f"The installed wwnet_native ({_version(mod)}) is too old: it has no "
                            f"{', '.join(lacking)}(). Install the current bundle / wheel.")
    return mod


def service():
    """The native module if it provides the file-level services, else None."""
    mod = get()
    if mod is not None and all(callable(getattr(mod, f, None)) for f in ("check", "design", "trace")):
        return mod
    return None


def _version(mod) -> str:
    try:
        return str(mod.version())
    except Exception:  # noqa: BLE001
        return "unknown version"
