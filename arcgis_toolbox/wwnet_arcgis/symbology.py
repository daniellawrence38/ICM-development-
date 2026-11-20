"""Runtime symbology via arcpy.mp. Everything is best effort: failures only warn."""
from __future__ import annotations

NODE_COLOURS = {  # node_type -> RGB
    "ac": [56, 118, 29], "ms": [0, 112, 255], "drop_ac": [230, 126, 34], "junction": [127, 127, 127],
    "wet_well": [142, 68, 173], "outfall": [192, 0, 0], "io_end": [0, 168, 168], "is_end": [168, 168, 0],
}
DEFAULT_RGB = [90, 90, 90]
DN_COLOURS = {150: [166, 206, 227], 225: [31, 120, 180], 300: [51, 160, 44], 375: [251, 154, 153],
              450: [227, 26, 28], 525: [255, 127, 0], 600: [106, 61, 154]}

CONDUIT_LABEL_ARCADE = """\
var len = IIf(IsEmpty($feature.length_m), Length($feature, 'meters'), $feature.length_m);
var fall = $feature.us_invert - $feature.ds_invert;
var g = "";
if (!IsEmpty($feature.us_invert) && !IsEmpty($feature.ds_invert) && fall > 0 && len > 0) {
    g = " 1:" + Text(Round(len / fall), "#");
}
return "DN" + $feature.dn + g;"""


def _rgb(c):
    return {"RGB": list(c) + [100]}


def _unique(layer, field, colour_for, size=None, width=None):
    sym = layer.symbology
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = [field]
    layer.symbology = sym              # populates the unique values from the data
    sym = layer.symbology
    for grp in sym.renderer.groups:
        for item in grp.items:
            val = item.values[0][0] if item.values and item.values[0] else None
            item.symbol.color = _rgb(colour_for(val))
            if size is not None:
                item.symbol.size = size
            if width is not None:
                try:
                    item.symbol.width = width(val)
                except Exception:  # noqa: BLE001
                    pass
    layer.symbology = sym


def style_nodes(layer):
    _unique(layer, "node_type", lambda v: NODE_COLOURS.get(str(v), DEFAULT_RGB), size=7)


def style_conduits(layer):
    def width(v):
        try:
            return 1.0 + float(v) / 150.0
        except (TypeError, ValueError):
            return 1.5

    def colour(v):
        try:
            return DN_COLOURS.get(int(float(v)), DEFAULT_RGB)
        except (TypeError, ValueError):
            return DEFAULT_RGB

    _unique(layer, "dn", colour, width=width)
    layer.showLabels = True
    lc = layer.listLabelClasses()[0]
    try:
        lc.expressionEngine = "Arcade"
    except Exception:  # noqa: BLE001
        pass
    lc.expression = CONDUIT_LABEL_ARCADE
    lc.visible = True


# #2E9E44 #E8A317 #D7263D; info (e.g. cover not checked) is coloured like pass
RESULT_COLOURS = {"pass": [46, 158, 68], "warn": [232, 163, 23], "fail": [215, 38, 61], "info": [46, 158, 68]}

RESULTS_LABEL_ARCADE = (
    '$feature.conduit_id + " DN" + $feature.dn + "  " + Text($feature.total_lps,"0.00") + "/" + '
    'Text($feature.capacity_lps,"0.0") + " L/s  d/D " + Text($feature.d_over_d,"0.00")')


def style_results(layer):
    """Pass/warn/fail unique values on `result`, 2.5 pt lines, capacity labels."""
    _unique(layer, "result", lambda v: RESULT_COLOURS.get(str(v).lower(), DEFAULT_RGB),
            width=lambda v: 2.5)
    layer.showLabels = True
    lc = layer.listLabelClasses()[0]
    try:
        lc.expressionEngine = "Arcade"
    except Exception:  # noqa: BLE001
        pass
    lc.expression = RESULTS_LABEL_ARCADE
    lc.visible = True


# Simulation results: colour by the peak d/D class (surcharged = d/D >= 1 or any surcharged time).
SIM_COLOURS = {"low": [46, 158, 68], "medium": [240, 210, 40], "high": [240, 140, 30],
               "surcharged": [215, 38, 61]}
SIM_LABEL_ARCADE = ('Text($feature.max_flow_lps,"0.0") + " L/s d/D " + Text($feature.max_d_over_d,"0.00")')


def sim_class(max_d_over_d, surcharged_min) -> str:
    """<0.5 low (green), 0.5-0.75 medium (yellow), 0.75-1.0 high (orange), surcharged (red)."""
    d = float(max_d_over_d or 0.0)
    if d >= 1.0 or float(surcharged_min or 0.0) > 0.0:
        return "surcharged"
    if d >= 0.75:
        return "high"
    if d >= 0.5:
        return "medium"
    return "low"


def style_sim(layer):
    _unique(layer, "d_class", lambda v: SIM_COLOURS.get(str(v), DEFAULT_RGB), width=lambda v: 2.5)
    layer.showLabels = True
    lc = layer.listLabelClasses()[0]
    try:
        lc.expressionEngine = "Arcade"
    except Exception:  # noqa: BLE001
        pass
    lc.expression = SIM_LABEL_ARCADE
    lc.visible = True


def style_sim_nodes(layer):
    sym = layer.symbology
    sym.updateRenderer("SimpleRenderer")
    sym.renderer.symbol.color = _rgb(SIM_COLOURS["surcharged"])
    sym.renderer.symbol.size = 9
    layer.symbology = sym


_STYLERS = {"wwn_nodes": style_nodes, "wwn_conduits": style_conduits, "results": style_results,
            "sim": style_sim, "sim_nodes": style_sim_nodes}


def apply(table: str, layer, msgs):
    fn = _STYLERS.get(table)
    if fn is None:
        return False
    try:
        fn(layer)
        return True
    except Exception as exc:  # noqa: BLE001 - never fail the tool over styling
        msgs.warn(f"Symbology for {table} could not be applied ({type(exc).__name__}: {exc}); "
                  "the layer is shown with default symbols.")
        return False
