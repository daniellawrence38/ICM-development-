"""Load WWNet GeoPackage layers into a layer group and style them (QGIS only)."""
from __future__ import annotations

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
                       QgsDefaultValue, QgsEditorWidgetSetup, QgsFillSymbol, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPalLayerSettings, QgsProject, QgsRendererCategory,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)

from .core import calc, schema, style

GROUP_NAME = "WWNet"
TABLE_PROP = "wwnet/table"
# Add order = draw order (first added is drawn on top). Result layers (schema v3) are hidden until a
# run fills them.
LOAD_ORDER = ("wwn_issues", "wwn_nodes", "wwn_sim_node_results", "wwn_pump_stations", "wwn_conduits",
              "wwn_results_conduits", "wwn_sim_conduit_results", "wwn_sim_compare", "wwn_rising_mains",
              "wwn_subcatchments", "wwn_pumps", "wwn_node_loads", "wwn_profile_info", "wwn_sim_runs",
              "wwn_meta")
HIDDEN_AT_LOAD = schema.RESULT_LAYERS + ("wwn_pumps", "wwn_node_loads", "wwn_profile_info", "wwn_sim_runs",
                                         "wwn_meta")

NODE_SIZE = {"junction": 2.0, "drop_ac": 3.4, "wet_well": 3.6, "outfall": 3.6, "io_end": 2.8, "is_end": 2.8}

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
    for val, col in style.NODE.items():
        sym = QgsMarkerSymbol.createSimple({"name": style.NODE_SHAPE.get(val, "circle"), "color": col,
                                            "size": str(NODE_SIZE.get(val, 3.0)),
                                            "outline_color": "#222222", "outline_width": "0.2"})
        cats.append(QgsRendererCategory(val, sym, val))
    other = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#999999", "size": "2"})
    cats.append(QgsRendererCategory("", other, "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("node_type", cats))
    _label(layer, "node_id", is_expr=False, size=7.0, max_scale_denom=5000)


def style_conduits(layer):
    cats = []
    for dn, col in style.DN.items():
        sym = QgsLineSymbol.createSimple({"line_color": col, "line_width": str(style.dn_width_mm(dn)),
                                          "capstyle": "round"})
        cats.append(QgsRendererCategory(dn, sym, f"DN{dn}"))
    cats.append(QgsRendererCategory("", QgsLineSymbol.createSimple(
        {"line_color": "#888888", "line_width": "0.4"}), "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("dn", cats))
    _label(layer, CONDUIT_LABEL, line=True, size=8.0, max_scale_denom=3000)


def style_pump_stations(layer):
    sym = QgsMarkerSymbol.createSimple({"name": "square", "color": style.PUMP_STATION, "size": "5",
                                        "outline_color": "#ffffff", "outline_width": "0.4"})
    layer.renderer().setSymbol(sym)
    _label(layer, "ps_id", is_expr=False)


def style_rising_mains(layer):
    layer.renderer().setSymbol(QgsLineSymbol.createSimple(
        {"line_color": style.RISING_MAIN, "line_width": "0.7", "line_style": "dash"}))


def style_subcatchments(layer):
    cats = []
    for lu, col in style.LAND_USE.items():
        r, g, b = style.rgb(col)
        cats.append(QgsRendererCategory(lu, QgsFillSymbol.createSimple(
            {"color": f"{r},{g},{b},70", "outline_color": "#555555", "outline_width": "0.2"}), lu))
    cats.append(QgsRendererCategory("", QgsFillSymbol.createSimple(
        {"color": "200,200,200,50", "outline_color": "#555555"}), "other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("land_use", cats))
    _label(layer, "subcatchment_id", is_expr=False, size=8.0)


def _categories_line(layer, field, colours, labels=None, width="0.9"):
    cats = [QgsRendererCategory(v, QgsLineSymbol.createSimple({"line_color": c, "line_width": width,
                                                               "capstyle": "round"}), (labels or {}).get(v, v))
            for v, c in colours.items()]
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))


def _categories_point(layer, field, colours, size="3.2"):
    cats = [QgsRendererCategory(v, QgsMarkerSymbol.createSimple({"name": "circle", "color": c, "size": size,
                                                                 "outline_color": "#ffffff"}), v)
            for v, c in colours.items()]
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))


def style_design_results(layer):
    _categories_line(layer, "result", {k: v for k, v in style.RESULT.items() if k != "info"})
    _label(layer, "\"conduit_id\" || ' ' || format_number(\"total_lps\", 2) || '/' || "
                  "format_number(\"capacity_lps\", 1) || ' L/s d/D ' || format_number(\"d_over_d\", 2)",
           line=True, size=7.5, max_scale_denom=3000)


def style_sim_conduits(layer):
    _categories_line(layer, "d_class", style.SIM_CLASS, style.SIM_CLASS_LABEL)
    _label(layer, "format_number(\"max_flow_lps\", 1) || ' L/s d/D ' || format_number(\"max_d_over_d\", 2)",
           line=True, size=7.5, max_scale_denom=3000)


def style_sim_nodes(layer):
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": style.SIM_CLASS["surcharged"], "size": "3"})
    from qgis.core import QgsRuleBasedRenderer
    root = QgsRuleBasedRenderer.Rule(None)
    rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, "\"flood_volume_m3\" > 0", "flooding")
    root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))


def style_compare(layer):
    _categories_line(layer, "change", style.COMPARE)


def style_issues(layer):
    _categories_point(layer, "severity", style.ISSUE)


STYLERS = {"wwn_nodes": style_nodes, "wwn_conduits": style_conduits,
           "wwn_pump_stations": style_pump_stations, "wwn_rising_mains": style_rising_mains,
           "wwn_subcatchments": style_subcatchments, "wwn_results_conduits": style_design_results,
           "wwn_sim_conduit_results": style_sim_conduits, "wwn_sim_node_results": style_sim_nodes,
           "wwn_sim_compare": style_compare, "wwn_issues": style_issues}


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
            dns = calc.ds50_dns()
        except Exception:
            dns = list(style.DN)
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
    if schema.schema_version(gpkg) < schema.SCHEMA_VERSION:
        try:
            schema.upgrade_model(gpkg)
            if iface:
                iface.messageBar().pushInfo("WWNet", f"Upgraded {gpkg} to schema v{schema.SCHEMA_VERSION} "
                                                     "(result and issue layers).")
        except Exception as exc:  # noqa: BLE001 - e.g. engine missing / read-only file: load what is there
            if iface:
                iface.messageBar().pushWarning("WWNet", f"Could not upgrade the model ({exc}).")
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
        if table in HIDDEN_AT_LOAD:
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
