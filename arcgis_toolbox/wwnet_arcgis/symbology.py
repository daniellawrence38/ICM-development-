"""Runtime symbology via arcpy.mp, from the shared palette (wwnet_shared.core.style) so QGIS and
ArcGIS legends match. Everything is best effort: failures only warn."""
from __future__ import annotations

from . import shared


def _style():
    return shared.core("style")


CONDUIT_LABEL_ARCADE = """\
var len = IIf(IsEmpty($feature.length_m), Length($feature, 'meters'), $feature.length_m);
var fall = $feature.us_invert - $feature.ds_invert;
var g = "";
if (!IsEmpty($feature.us_invert) && !IsEmpty($feature.ds_invert) && fall > 0 && len > 0) {
    g = " 1:" + Text(Round(len / fall), "#");
}
return "DN" + $feature.dn + g;"""

RESULTS_LABEL_ARCADE = (
    '$feature.conduit_id + " DN" + $feature.dn + "  " + Text($feature.total_lps,"0.00") + "/" + '
    'Text($feature.capacity_lps,"0.0") + " L/s  d/D " + Text($feature.d_over_d,"0.00")')
SIM_LABEL_ARCADE = 'Text($feature.max_flow_lps,"0.0") + " L/s d/D " + Text($feature.max_d_over_d,"0.00")'
COMPARE_LABEL_ARCADE = ('"d/D " + Text($feature.max_d_over_d_a,"0.00") + " > " + '
                        'Text($feature.max_d_over_d_b,"0.00")')
#: only nodes that surcharge or flood are drawn in the Simulation results (nodes) layer
SIM_NODES_QUERY = "flood_volume_m3 > 0 OR surcharged_min > 0"


def _rgb(hex_colour):
    return {"RGB": _style().rgb(hex_colour) + [100]}


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


def _simple(layer, colour, size=None, width=None):
    sym = layer.symbology
    sym.updateRenderer("SimpleRenderer")
    sym.renderer.symbol.color = _rgb(colour)
    if size is not None:
        sym.renderer.symbol.size = size
    if width is not None:
        sym.renderer.symbol.width = width
    layer.symbology = sym


def _label(layer, arcade: str):
    layer.showLabels = True
    lc = layer.listLabelClasses()[0]
    try:
        lc.expressionEngine = "Arcade"
    except Exception:  # noqa: BLE001
        pass
    lc.expression = arcade
    lc.visible = True


def style_nodes(layer):
    s = _style()
    _unique(layer, "node_type", lambda v: s.NODE.get(str(v), s.DEFAULT), size=7)


def style_conduits(layer):
    s = _style()

    def width(v):
        try:
            return 1.0 + float(v) / 150.0
        except (TypeError, ValueError):
            return 1.5

    _unique(layer, "dn", s.dn_colour, width=width)
    _label(layer, CONDUIT_LABEL_ARCADE)


def style_subcatchments(layer):
    s = _style()
    _unique(layer, "land_use", lambda v: s.LAND_USE.get(str(v), s.DEFAULT))
    try:
        layer.transparency = 60
    except Exception:  # noqa: BLE001
        pass


def style_pump_stations(layer):
    _simple(layer, _style().PUMP_STATION, size=10)


def style_rising_mains(layer):
    _simple(layer, _style().RISING_MAIN, width=1.5)


def style_results(layer):
    """Pass/warn/fail unique values on `result`, 2.5 pt lines, capacity labels."""
    s = _style()
    _unique(layer, "result", lambda v: s.RESULT.get(str(v).lower(), s.DEFAULT), width=lambda v: 2.5)
    _label(layer, RESULTS_LABEL_ARCADE)


def style_sim(layer):
    s = _style()
    _unique(layer, "d_class", lambda v: s.SIM_CLASS.get(str(v), s.DEFAULT), width=lambda v: 2.5)
    _label(layer, SIM_LABEL_ARCADE)


def style_sim_nodes(layer):
    _simple(layer, _style().SIM_CLASS["surcharged"], size=9)
    try:
        layer.definitionQuery = SIM_NODES_QUERY
    except Exception:  # noqa: BLE001
        pass


def style_compare(layer):
    s = _style()
    _unique(layer, "change", lambda v: s.COMPARE.get(str(v), s.DEFAULT), width=lambda v: 2.5)
    _label(layer, COMPARE_LABEL_ARCADE)


def style_issues(layer):
    s = _style()
    _unique(layer, "severity", lambda v: s.ISSUE.get(str(v), s.DEFAULT), size=8)


def style_sim_pumps(layer):
    """Pump stations: red when any flag was raised (flag != 'ok'), green otherwise."""
    s = _style()
    _unique(layer, "flag", lambda v: s.SIM_CLASS["surcharged"] if str(v) != "ok" else s.SIM_CLASS["low"], size=11)
    _label(layer, "$feature.ps_id")


_STYLERS = {"wwn_nodes": style_nodes, "wwn_conduits": style_conduits, "wwn_subcatchments": style_subcatchments,
            "wwn_pump_stations": style_pump_stations, "wwn_rising_mains": style_rising_mains,
            "wwn_results_conduits": style_results, "wwn_sim_conduit_results": style_sim,
            "wwn_sim_node_results": style_sim_nodes, "wwn_sim_compare": style_compare,
            "wwn_issues": style_issues, "sim_pumps": style_sim_pumps}


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
