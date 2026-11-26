"""One colour palette for both front ends (QGIS layers.py and ArcGIS symbology.py), so a legend
means the same thing in either GIS. Colours are hex; use rgb() for [r, g, b]."""
from __future__ import annotations

NODE = {  # node_type: colour
    "ac": "#1f77b4", "ms": "#2ca02c", "drop_ac": "#9467bd", "junction": "#7f7f7f",
    "wet_well": "#d62728", "outfall": "#000000", "io_end": "#ff7f0e", "is_end": "#bcbd22",
}
NODE_SHAPE = {"wet_well": "square", "outfall": "triangle", "io_end": "diamond", "is_end": "diamond"}
DN = {150: "#4c9be8", 225: "#2e7d32", 300: "#f9a825", 375: "#ef6c00", 450: "#c62828",
      525: "#ad1457", 600: "#6a1b9a"}
DEFAULT = "#8c8c8c"
LAND_USE = {"residential": "#ffeb96", "commercial": "#6496ff", "hotel_motel": "#ff96c8",
            "perth_cba": "#c864ff", "industrial": "#aaaaaa"}
#: Calculate Existing Demand review layer: zone class (land use classes + mixed and zero-flow zones)
LAND_USE_CLASS = {**LAND_USE, "residential_or_commercial": "#ffc864", "zero": "#e6e6e6"}
PUMP_STATION = "#d81b60"
RISING_MAIN = "#c2185b"
#: design result (pass / warn / fail; info is coloured as pass)
RESULT = {"pass": "#2e9e44", "warn": "#e8a317", "fail": "#d7263d", "info": "#2e9e44"}
#: simulation peak d/D class (see wwnet_sim::service::d_class)
SIM_CLASS = {"low": "#2e9e44", "medium": "#f0d228", "high": "#f08c1e", "surcharged": "#d7263d"}
SIM_CLASS_LABEL = {"low": "d/D < 0.5", "medium": "0.5 - 0.75", "high": "0.75 - 1.0",
                   "surcharged": "surcharged"}
#: run comparison change
COMPARE = {"new_surcharge": "#8b0000", "worse": "#d7263d", "same": "#bdbdbd", "better": "#2e9e44",
           "relieved": "#1565c0", "only_a": "#7f7f7f", "only_b": "#7f7f7f"}
ISSUE = {"error": "#d7263d", "warning": "#e8a317", "info": "#1f77b4"}


def rgb(hex_colour: str) -> list:
    h = hex_colour.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def dn_colour(v) -> str:
    try:
        return DN.get(int(float(v)), DEFAULT)
    except (TypeError, ValueError):
        return DEFAULT


def dn_width_mm(v) -> float:
    """QGIS line width (mm) by DN."""
    try:
        return round(0.3 + float(v) / 600.0, 2)
    except (TypeError, ValueError):
        return 0.4
