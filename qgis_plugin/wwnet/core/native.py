"""Optional access to the PyO3 extension module.

NAME CLASH WARNING: this plugin package is itself called `wwnet` (QGIS imports
plugins by folder name), so a top-level `import wwnet` may return the plugin
instead of the Rust extension.  We therefore probe several candidate module
names and accept only a module that actually exposes the expected functions.
Recommended: publish the extension as `wwnet_native` (or `wwnet._native`).
"""
from __future__ import annotations

import importlib

_CANDIDATES = ("wwnet_native", "wwnet._native", "wwnet.wwnet", "wwnet")
_REQUIRED = ("part_full_flow_lps", "depth_ratio_for_flow")
_cache: dict = {}


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


_SERVICE = ("check", "design", "trace")

INSTALL_HINT = (
    "Install the wwnet_native wheel (no .exe needed): "
    "pip install wwnet_native-<version>-cp39-abi3-win_amd64.whl "
    "into the Python that runs this application (QGIS: OSGeo4W Shell; ArcGIS Pro: a cloned "
    "conda environment)."
)


def service():
    """The native module if it provides check/design/trace (wheel >= file-level API), else None."""
    mod = get()
    if mod is not None and all(callable(getattr(mod, f, None)) for f in _SERVICE):
        return mod
    return None
