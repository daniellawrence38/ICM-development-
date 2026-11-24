"""No-install bundle support: make a ``<toolbox>/vendor`` folder importable and describe the result.

The CI bundle ships the ``wwnet_native`` engine extension (``wwnet_native.pyd``) in ``<toolbox>/vendor`` so
nothing has to be pip-installed into ArcGIS Pro's (read-only) Python. ``vendor`` goes at the FRONT of
``sys.path`` (idempotent). Nothing else is vendored, and no file is renamed, swapped or patched at start-up.

Only the standard library is imported here, so this module is safe to import anywhere (and from tests).
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger("wwnet.vendor")

#: (import name, distribution name) in report order
PACKAGES = (("wwnet_native", "wwnet_native"),)

NOTES: list = []          # human-readable things activate() did (shown by Verify Setup)


def vendor_dir(root) -> Path:
    return Path(root) / "vendor"


def activate(root, log_fn=None):
    """Put ``<root>/vendor`` first on sys.path (once). Returns the vendor path, or None when the folder does
    not exist (e.g. dev checkout)."""
    vd = vendor_dir(root)
    if not vd.is_dir():
        return None
    s = str(vd)
    if s not in sys.path:
        sys.path.insert(0, s)
        importlib.invalidate_caches()
    return vd


def _locate(mod_name: str):
    mod = sys.modules.get(mod_name)
    if mod is not None:
        f = getattr(mod, "__file__", None) or (list(getattr(mod, "__path__", [])) or [None])[0]
        return mod, (str(f) if f else None)
    try:
        spec = importlib.util.find_spec(mod_name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None, None
    if spec.origin not in (None, "namespace"):
        return None, spec.origin
    locs = list(spec.submodule_search_locations or [])
    return None, (str(locs[0]) if locs else None)


def _try_import(name: str):
    try:
        importlib.import_module(name)
        return True, ""
    except BaseException as exc:            # noqa: BLE001 - report any failure, incl. SystemExit
        return False, f"{type(exc).__name__}: {exc}"


def report() -> dict:
    """``{package: (version, location)}`` (``(None, None)`` when missing) plus the keys ``_python``,
    ``_vendor`` (path on sys.path or None), ``_notes`` and ``_import_wwnet_native`` (``(ok, error text)``)."""
    out: dict = {}
    for mod_name, dist in PACKAGES:
        mod, loc = _locate(mod_name)
        if loc is None:
            out[mod_name] = (None, None)
            continue
        try:
            ver = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            ver = getattr(mod, "__version__", None) if mod is not None else None
        out[mod_name] = (ver or "unknown", loc)
    out["_python"] = f"{sys.version.split()[0]} ({sys.platform}, {sys.executable})"
    out["_vendor"] = next((p for p in sys.path if Path(p).name == "vendor"), None)
    out["_notes"] = list(NOTES)
    out["_import_wwnet_native"] = _try_import("wwnet_native")
    return out


def format_report(rep: dict) -> list:
    """Message lines for the geoprocessing pane."""
    lines = [f"Python {rep['_python']}", f"Vendor folder: {rep['_vendor'] or '(none on sys.path)'}"]
    for mod_name, _ in PACKAGES:
        ver, loc = rep[mod_name]
        lines.append(f"  {mod_name:<18} {ver or 'not found':<10} {loc or ''}")
    ok, err = rep["_import_wwnet_native"]
    lines.append("import wwnet_native: " + ("OK" if ok else f"FAILED - {err}"))
    lines.extend(f"Note: {n}" for n in rep["_notes"])
    return lines
