"""Make the shared pure-Python WWNet modules importable as ``wwnet_shared``.

* Packaged mode: ``<toolbox>/wwnet_shared/`` is a real package (copied by package.py).
* Dev mode: ``../qgis_plugin/wwnet`` is aliased as the package ``wwnet_shared`` without
  executing its ``__init__`` (which belongs to the QGIS plugin). Only ``core`` and the
  QGIS-free ``assistant`` modules are ever imported.

Either way the shared code's relative imports (``from ..core import schema``) work unchanged.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

PACKAGE = "wwnet_shared"
TOOLBOX_ROOT = Path(__file__).resolve().parent.parent


def shared_dir() -> Path:
    packaged = TOOLBOX_ROOT / PACKAGE
    if (packaged / "core").is_dir():
        return packaged
    dev = TOOLBOX_ROOT.parent / "qgis_plugin" / "wwnet"
    if (dev / "core").is_dir():
        return dev
    raise ImportError(
        f"WWNet shared modules not found: looked for {packaged} and {dev}. "
        "Use the packaged zip (package.py) or keep the toolbox inside the WWNet repository.")


def ensure():
    """Idempotent; returns the ``wwnet_shared`` package."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    d = shared_dir()
    if d.name == PACKAGE:
        if str(TOOLBOX_ROOT) not in sys.path:
            sys.path.insert(0, str(TOOLBOX_ROOT))
        return importlib.import_module(PACKAGE)
    mod = types.ModuleType(PACKAGE)
    mod.__path__ = [str(d)]
    mod.__package__ = PACKAGE
    mod.__doc__ = "Alias of qgis_plugin/wwnet (dev mode)"
    sys.modules[PACKAGE] = mod
    return mod


def _mod(name: str):
    ensure()
    return importlib.import_module(f"{PACKAGE}.{name}")


def core(name: str):
    """core('schema') -> wwnet_shared.core.schema"""
    return _mod(f"core.{name}")


def assistant(name: str):
    """assistant('engine') -> wwnet_shared.assistant.engine"""
    return _mod(f"assistant.{name}")
