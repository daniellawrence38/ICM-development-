"""Geoprocessing logging control for WWNet tools.

Every geoprocessing call in ArcGIS Pro writes a history entry and metadata when those logs are on,
which made each tool call cost 2-6 s in large projects. `quiet(arcpy)` turns both off for a block
and restores whatever was set before. Logging control must never break a tool, so every read and
write of the logging settings is guarded.
"""
from __future__ import annotations

import contextlib


def _log_get(arcpy, name: str):
    """Current geoprocessing log setting (GetLogHistory / GetLogMetadata), or None when arcpy lacks
    it or the call fails. Logging control must never break an export."""
    try:
        return getattr(arcpy, name)() if hasattr(arcpy, name) else None
    except Exception:  # noqa: BLE001
        return None


def _log_set(arcpy, name: str, value) -> None:
    try:
        getattr(arcpy, name)(value)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def quiet(arcpy):
    """Turn geoprocessing history and metadata logging off for the block, then restore the previous
    settings (also on error). Settings that cannot be read are left alone. Nesting is safe: an inner
    block reads the outer block's off state and restores it, and the outermost block restores the
    original state."""
    prev_hist = _log_get(arcpy, "GetLogHistory")
    prev_meta = _log_get(arcpy, "GetLogMetadata")
    try:
        if prev_hist is not None:
            _log_set(arcpy, "SetLogHistory", False)
        if prev_meta is not None:
            _log_set(arcpy, "SetLogMetadata", False)
        yield
    finally:
        if prev_hist is not None:
            _log_set(arcpy, "SetLogHistory", prev_hist)
        if prev_meta is not None:
            _log_set(arcpy, "SetLogMetadata", prev_meta)
