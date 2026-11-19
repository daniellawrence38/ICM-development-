"""Load WWNet GeoPackage layers into a layer group and style them (QGIS only)."""
from __future__ import annotations

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
                       QgsDefaultValue, QgsEditorWidgetSetup, QgsFillSymbol, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPalLayerSettings, QgsProject, QgsRendererCategory,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)

from .core import ruleset, schema

GROUP_NAME = "WWNet"
TABLE_PROP = "wwnet/table"
# Add order = draw order (first added is drawn on top).
LOAD_ORDER = ("wwn_nodes", "wwn_pump_stations", "wwn_conduits", "wwn_rising_mains",
              "wwn_subcatchments", "wwn_pumps", "wwn_results_conduits", "wwn_issues", "wwn_meta")

NODE_STYLE = {  # node_type: (shape, colour, size)
    "ac": ("circle", "#1f77b4", 3.0), "ms": ("circle", "#2ca02c", 3.0),
    "drop_ac": ("circle", "#9467bd", 3.4), "junction": ("circle", "#7f7f7f", 2.0),
    "wet_well": ("square", "#d62728", 3.6), "outfall": ("triangle", "#000000", 3.6),
    "io_end": ("diamond", "#ff7f0e", 2.8), "is_end": ("diamond", "#bcbd22", 2.8),
}
DN_STYLE = {150: ("#4c9be8", 0.35), 225: ("#2e7d32", 0.5), 300: ("#f9a825", 0.7),
            375: ("#ef6c00", 0.9), 450: ("#c62828", 1.1), 600: ("#6a1b9a", 1.4)}
LAND_USE_COLOUR = {"residential": "255,235,150,70", "commercial": "100,150,255,70",
                   "hotel_motel": "255,150,200,70", "perth_cba": "200,100,255,70",
                   "industrial": "170,170,170,90"}

CONDUIT_LABEL = (
    "'DN' || \"dn\" || CASE WHEN \"us_invert\" IS NOT NULL AND \"ds_invert\" IS NOT NULL "
    "AND (\"us_invert\" - \"ds_invert\") > 0 AND coalesce(\"length_m\", $length) > 0 "
    "THEN ' 1:' || to_string(to_int(round(coalesce(\"length_m\", $length) / "
    "(\"us_invert\" - \"ds_invert\")))) ELSE '' END"
)




def _label(layer, expr, is_expr=True, line=False, size=8.0, max_scale_denom=None):
    s = QgsPalLayerSettings()
    s.fieldName = expr
    s.isExpression = is_expr
    if line:
        try:
            s.placement = Qgis.LabelPlacement.Line
        except Exception:
            s.placement = QgsPalLayerSettings.Line
    fmt = QgsTextFormat()
    fmt.setSize(size)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    if max_scale_denom:
        s.scaleVisibility = True
        s.minimumScale = max_scale_denom
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)


def style_nodes(layer):
    cats = []
    for val, (shape, col, size) in NODE_STYLE.items():
        sym = QgsMarkerSymbol.createSimple({"name": shape, "color": col, "size": str(size),
                                            "outline_color": "#222222", "outline_width": "0.2"})
        cats.append(QgsRendererCategory(val, sym, val))
    other = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#999999", "size": "2"})
    cats.append(QgsRendererCategory("", other, "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("node_type", cats))
    _label(layer, "node_id", is_expr=False, size=7.0, max_scale_denom=5000)


def style_conduits(layer):
    cats = []
    for dn, (col, width) in DN_STYLE.items():
        sym = QgsLineSymbol.createSimple({"line_color": col, "line_width": str(width),
                                          "capstyle": "round"})
        cats.append(QgsRendererCategory(dn, sym, f"DN{dn}"))
    cats.append(QgsRendererCategory("", QgsLineSymbol.createSimple(
        {"line_color": "#888888", "line_width": "0.4"}), "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("dn", cats))
    _label(layer, CONDUIT_LABEL, line=True, size=8.0, max_scale_denom=3000)


def style_pump_stations(layer):
    sym = QgsMarkerSymbol.createSimple({"name": "square", "color": "#d81b60", "size": "5",
                                        "outline_color": "#ffffff", "outline_width": "0.4"})
    layer.renderer().setSymbol(sym)
    _label(layer, "ps_id", is_expr=False)


def style_rising_mains(layer):
    layer.renderer().setSymbol(QgsLineSymbol.createSimple(
        {"line_color": "#c2185b", "line_width": "0.7", "line_style": "dash"}))


def style_subcatchments(layer):
    cats = []
    for lu, col in LAND_USE_COLOUR.items():
        cats.append(QgsRendererCategory(lu, QgsFillSymbol.createSimple(
            {"color": col, "outline_color": "#555555", "outline_width": "0.2"}), lu))
    cats.append(QgsRendererCategory("", QgsFillSymbol.createSimple(
        {"color": "200,200,200,50", "outline_color": "#555555"}), "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("land_use", cats))
    _label(layer, "subcatchment_id", is_expr=False, size=8.0)


STYLERS = {"wwn_nodes": style_nodes, "wwn_conduits": style_conduits,
           "wwn_pump_stations": style_pump_stations, "wwn_rising_mains": style_rising_mains,
           "wwn_subcatchments": style_subcatchments}


def setup_forms(layer, table):
    """Value-map widgets for enum fields; default values for status/data_source."""
    fe = schema.field_enums()
    fields = layer.fields()
    for (tbl, fld), values in fe.items():
        if tbl != table:
            continue
        idx = fields.indexFromName(fld)
        if idx >= 0:
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup(
                "ValueMap", {"map": [{v: v} for v in values]}))
    if table == "wwn_conduits":
        try:
            dns = [g["dn"] for g in ruleset.load_ruleset()["grades"]]
        except Exception:
            dns = list(DN_STYLE)
        idx = fields.indexFromName("dn")
        if idx >= 0:
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup(
                "ValueMap", {"map": [{f"DN{d}": str(d)} for d in dns]}))
    for fld, default in schema.DEFAULT_VALUES.items():
        idx = fields.indexFromName(fld)
        if idx >= 0:
            layer.setDefaultValueDefinition(idx, QgsDefaultValue(f"'{default}'"))


def gpkg_of(layer) -> str:
    return layer.source().split("|")[0]


def wwnet_layers():
    return [l for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsVectorLayer) and l.customProperty(TABLE_PROP)]


def layer_for_table(table):
    for l in wwnet_layers():
        if l.customProperty(TABLE_PROP) == table:
            return l
    return None


def load_model(gpkg: str, iface=None) -> list:
    """Add all wwn_* layers of `gpkg` to the 'WWNet' group. Returns loaded layers."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
    present = schema.table_identifiers(gpkg)
    existing = {(l.customProperty(TABLE_PROP), gpkg_of(l)) for l in wwnet_layers()}
    first_load = not wwnet_layers()
    loaded = []
    for table in LOAD_ORDER:
        if table not in present or (table, gpkg) in existing:
            continue
        ident = present[table][0] or table
        lyr = QgsVectorLayer(f"{gpkg}|layername={table}", ident, "ogr")
        if not lyr.isValid():
            if iface:
                iface.messageBar().pushWarning("WWNet", f"Could not load {table} from {gpkg}")
            continue
        lyr.setCustomProperty(TABLE_PROP, table)
        project.addMapLayer(lyr, False)
        group.addLayer(lyr)
        if table in STYLERS:
            STYLERS[table](lyr)
        setup_forms(lyr, table)
        loaded.append(lyr)
        if table in schema.READ_ONLY_TABLES + ("wwn_pumps",):
            node = root.findLayer(lyr.id())
            if node:
                node.setItemVisibilityChecked(False)
    if first_load:
        project.setCrs(QgsCoordinateReferenceSystem("EPSG:7850"))
    if iface and loaded:
        feats = [l for l in loaded if l.isSpatial() and l.featureCount() > 0]
        if feats:
            ext = feats[0].extent()
            for l in feats[1:]:
                ext.combineExtentWith(l.extent())
            iface.mapCanvas().setExtent(ext)
            iface.mapCanvas().refresh()
    return loaded
