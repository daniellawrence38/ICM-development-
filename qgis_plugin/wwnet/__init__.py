"""WWNet QGIS plugin entry point.

Keep this module free of qgis imports at import time so the pure-Python
parts of the package (core/, assistant/engine.py ...) can be unit tested
without QGIS.
"""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import WWNetPlugin

    return WWNetPlugin(iface)
