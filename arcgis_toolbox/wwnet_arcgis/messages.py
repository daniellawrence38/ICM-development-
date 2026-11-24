"""Message sink: routes to arcpy.AddMessage/AddWarning/AddError (or a list in tests)."""
from __future__ import annotations


def arcpy_mod():
    import arcpy  # noqa: PLC0415 - lazy: only available inside ArcGIS Pro
    return arcpy


class Messages:
    def __init__(self, arcpy=None):
        self._arcpy = arcpy

    @property
    def arcpy(self):
        if self._arcpy is None:
            self._arcpy = arcpy_mod()
        return self._arcpy

    def info(self, text: str):
        self.arcpy.AddMessage(str(text))

    def warn(self, text: str):
        self.arcpy.AddWarning(str(text))

    def error(self, text: str):
        self.arcpy.AddError(str(text))

    def step(self, label: str):
        """Progress label in the geoprocessing dialog (best effort)."""
        try:
            self.arcpy.SetProgressorLabel(str(label))
        except Exception:  # noqa: BLE001 - fakes / old arcpy
            pass

    def start(self, label: str):
        """Start a default (animated) progressor with `label`."""
        try:
            self.arcpy.SetProgressor("default", str(label))
        except Exception:  # noqa: BLE001
            pass
