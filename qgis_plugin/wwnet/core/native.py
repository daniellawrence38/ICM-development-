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
    if refresh or "mod" not in _cache:
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
