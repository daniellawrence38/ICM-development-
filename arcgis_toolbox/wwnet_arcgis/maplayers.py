"""WWNet layers in the active map (arcpy.mp): one place that finds, adds, styles and selects them."""
from __future__ import annotations

import os

from . import gpkg, symbology

GROUP_NAME = "WWNet"


def layer_name(lyr) -> str:
    """A layer's name, or '' for layer types that don't support NAME (real Pro raises
    "The attribute 'name' is not supported on this instance of Layer" for some layers)."""
    try:
        sup = getattr(lyr, "supports", None)
        if callable(sup) and not sup("NAME"):
            return ""
        return lyr.name or ""
    except Exception:  # noqa: BLE001
        return ""


def _from_connection_properties(lyr) -> str:
    """`<database>\\main.<dataset>` from Layer.connectionProperties (GeoPackage: workspace factory 'Sql',
    connection_info.database = the .gpkg file, dataset = 'main.<table>' or '<table>'). '' if unavailable."""
    try:
        cp = lyr.connectionProperties
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(cp, dict):
        return ""
    info = cp.get("connection_info") or {}
    db = info.get("database") or info.get("db") or ""
    ds = cp.get("dataset") or ""
    if not db or not ds or not str(db).lower().endswith(".gpkg"):
        return ""
    ds = str(ds)
    return gpkg.table_path(str(db), ds.split(".", 1)[1] if ds.lower().startswith("main.") else ds)


def layer_source(lyr) -> str:
    """A layer's data source as '<file>.gpkg\\main.<table>' when it is a GeoPackage table, else its
    dataSource ('' when the layer type doesn't support it: basemaps, groups ...). Pro raises a
    non-AttributeError for unsupported attributes, so getattr() defaults don't help."""
    ds = ""
    try:
        sup = getattr(lyr, "supports", None)
        if not (callable(sup) and not sup("DATASOURCE")):
            ds = lyr.dataSource or ""
    except Exception:  # noqa: BLE001
        ds = ""
    if ds and gpkg.split_datasource(ds)[1]:
        return ds
    return _from_connection_properties(lyr) or ds


def layer_model_table(lyr):
    """(gpkg path, table) of a GeoPackage map layer, else (None, None)."""
    return gpkg.split_datasource(layer_source(lyr))


def same_file(a, b) -> bool:
    if not a or not b:
        return False
    na = os.path.normcase(os.path.abspath(str(a).replace("/", os.sep)))
    nb = os.path.normcase(os.path.abspath(str(b).replace("/", os.sep)))
    if na == nb:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def set_layer_name(lyr, name: str, msgs=None) -> bool:
    try:
        sup = getattr(lyr, "supports", None)
        if callable(sup) and not sup("NAME"):
            raise AttributeError("layer does not support NAME")
        lyr.name = name
        return True
    except Exception as exc:  # noqa: BLE001
        if msgs is not None:
            msgs.warn(f"Could not rename the layer to {name!r} ({exc}); it keeps its default name.")
        return False


def active_map(arcpy):
    m = arcpy.mp.ArcGISProject("CURRENT").activeMap
    if m is None:
        raise RuntimeError("No active map. Open a map view and run the tool again.")
    return m


def _all_layers(m) -> list:
    out = list(m.listLayers())
    try:
        out += list(m.listTables())
    except Exception:  # noqa: BLE001
        pass
    return out


def models_in_map(arcpy) -> list:
    """GeoPackage files of the WWNet (wwn_*) layers in the active map, in layer order, no duplicates."""
    try:
        m = arcpy.mp.ArcGISProject("CURRENT").activeMap
    except Exception:  # noqa: BLE001
        return []
    if m is None:
        return []
    out = []
    for lyr in _all_layers(m):
        path, table = layer_model_table(lyr)
        if path and table and table.startswith("wwn_") and not any(same_file(path, p) for p in out):
            out.append(path)
    return out


def default_model(arcpy):
    """The model shown in the active map (None when there is none). Never raises."""
    found = models_in_map(arcpy)
    return found[0] if found else None


def resolve_model(arcpy, value) -> str:
    """A model .gpkg from a parameter value: a .gpkg path, a dataset path (x.gpkg\\main.wwn_nodes), a map
    layer of the model, or empty (the model in the active map)."""
    if value is not None and not isinstance(value, str):
        path, _ = layer_model_table(value)
        if path:
            return path
        value = str(value)
    text = (value or "").strip().strip('"')
    if text:
        path, _ = gpkg.split_datasource(text)
        if path:
            return path
        raise RuntimeError(f"{text!r} is not a WWNet GeoPackage (.gpkg).")
    found = default_model(arcpy)
    if not found:
        raise RuntimeError("Choose the WWNet GeoPackage (no WWNet model is in the active map).")
    return found


def find_layer_for(arcpy, gpkg_path: str, table: str):
    """The map layer showing `table` of `gpkg_path`, or None."""
    for lyr in _all_layers(active_map(arcpy)):
        g, t = layer_model_table(lyr)
        if t == table and same_file(g, gpkg_path):
            return lyr
    return None


def find_layer(arcpy, name_or_path):
    """A map layer by name or data source; None if not found."""
    text = str(name_or_path or "")
    layers = _all_layers(active_map(arcpy))
    for lyr in layers:
        if layer_name(lyr) == text:
            return lyr
    g, t = gpkg.split_datasource(text)
    if g:
        for lyr in layers:
            lg, lt = layer_model_table(lyr)
            if lt == t and same_file(lg, g):
                return lyr
    return None


def selected_ids(arcpy, gpkg_path: str, table: str, id_field: str) -> list:
    """Business ids of the features selected in the map layer of `table` ([] if none / not in the map)."""
    lyr = find_layer_for(arcpy, gpkg_path, table)
    if lyr is None:
        return []
    try:
        sel = lyr.getSelectionSet()
    except Exception:  # noqa: BLE001
        sel = None
    if not sel:
        return []
    oids = ", ".join(str(int(i)) for i in sorted(sel))
    out = []
    with arcpy.da.SearchCursor(gpkg.table_path(gpkg_path, table), [id_field],
                               where_clause=f"fid IN ({oids})") as cur:
        for (v,) in cur:
            if v is not None:
                out.append(str(v))
    return out


def _find_group(m):
    for lyr in m.listLayers():
        if getattr(lyr, "isGroupLayer", False) and layer_name(lyr) == GROUP_NAME:
            return lyr
    return None


def _ensure_group(m, msgs):
    g = _find_group(m)
    if g is not None:
        return g
    try:
        return m.createGroupLayer(GROUP_NAME)
    except Exception as exc:  # noqa: BLE001 - older/unsupported API
        msgs.warn(f"Could not create the '{GROUP_NAME}' group layer ({exc}); layers added ungrouped.")
        return None


def _add_to_group(m, group, lyr, name, msgs):
    if group is None:
        return lyr
    try:
        m.addLayerToGroup(group, lyr)
        m.removeLayer(lyr)
        return next((x for x in group.listLayers() if layer_name(x) == name), lyr)
    except Exception as exc:  # noqa: BLE001
        msgs.warn(f"Could not move {name} into the group layer: {exc}")
        return lyr


def _add_table_layer(m, group, gpkg_path, table, name, msgs):
    lyr = m.addDataFromPath(gpkg.table_path(gpkg_path, table))
    set_layer_name(lyr, name, msgs)
    return _add_to_group(m, group, lyr, name, msgs)


def _upgrade(gpkg_path, msgs):
    try:
        if gpkg.upgrade_model(gpkg_path):
            msgs.info("Upgraded the model to the current schema (result and issue layers, run labels).")
    except Exception as exc:  # noqa: BLE001 - e.g. read-only file: show the layers anyway
        msgs.warn(f"Could not upgrade the model ({exc}); newer layers may be missing.")


def add_model(arcpy, gpkg_path: str, msgs, symbolize: bool = True, only=None, skip_existing: bool = True) -> list:
    """Add the model's wwn_* feature tables and the result layers that hold data (or just `only`); tables
    the active map already shows are skipped. Returns the added layers."""
    present = gpkg.contents(gpkg_path)
    if "wwn_subcatchments" in present:
        _upgrade(gpkg_path, msgs)
        present = gpkg.contents(gpkg_path)
    if not any(t in present for t in gpkg.FEATURE_ORDER):
        raise RuntimeError(f"{gpkg_path} contains no WWNet feature tables (wwn_nodes, wwn_conduits, ...).")

    def wanted(t):
        if only is not None and t not in only:
            return False
        return not (skip_existing and find_layer_for(arcpy, gpkg_path, t) is not None)

    tables = [t for t in gpkg.RESULT_ORDER if t in present and gpkg.row_count(gpkg_path, t) and wanted(t)] + \
        [t for t in gpkg.FEATURE_ORDER if t in present and wanted(t)]
    m = active_map(arcpy)
    group = _ensure_group(m, msgs) if tables else None
    added = []
    for t in tables:
        try:
            name = present[t][0] or t
            lyr = _add_table_layer(m, group, gpkg_path, t, name, msgs)
            added.append((t, lyr))
            msgs.info(f"Added {name}")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not add {t}: {exc}")
    if symbolize:
        for t, lyr in added:
            symbology.apply(t, lyr, msgs)
    for t in gpkg.ATTRIBUTE_ORDER:
        if t in present and wanted(t):
            try:
                m.addDataFromPath(gpkg.table_path(gpkg_path, t))
                msgs.info(f"Added table {present[t][0] or t}")
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add table {t}: {exc}")
    return [lyr for _, lyr in added]


def ensure_layer(arcpy, gpkg_path: str, table: str, msgs, definition_query: str | None = None):
    """The map layer of `table` (a result / issue layer): reused when already in the map (Pro redraws it
    from the file), else added to the WWNet group and styled. Returns the layer."""
    lyr = find_layer_for(arcpy, gpkg_path, table)
    if lyr is None:
        m = active_map(arcpy)
        name = (gpkg.contents(gpkg_path).get(table) or (table,))[0] or table
        lyr = _add_table_layer(m, _ensure_group(m, msgs), gpkg_path, table, name, msgs)
        symbology.apply(table, lyr, msgs)
        msgs.info(f"Added layer {name} (stored in the model: it stays after Pro restarts).")
    if definition_query is not None:
        try:
            lyr.definitionQuery = definition_query
        except Exception:  # noqa: BLE001
            pass
    return lyr


def remove_item(m, item, is_table: bool):
    """Remove a layer or a standalone table from map `m`. Real ArcGIS Pro only removes tables via
    removeTable(); removeLayer() on a table fails."""
    if is_table:
        m.removeTable(item)
    else:
        m.removeLayer(item)


def remove_layers_named(arcpy, name: str) -> int:
    """Remove every map layer / table called `name` (also inside groups). Returns how many were removed.
    Failures propagate, so only successful removals are counted."""
    m = active_map(arcpy)
    n = 0
    for is_table, items in ((False, list(m.listLayers())), (True, list(m.listTables()))):
        for item in [x for x in items if layer_name(x) == name]:
            remove_item(m, item, is_table)
            n += 1
    return n


def add_results_layer(arcpy, fc_path: str, name: str, msgs):
    """Add a temporary (memory) results feature class to the active map, in the WWNet group if present
    (used for the pump station results; the conduit / node results are GeoPackage layers)."""
    m = active_map(arcpy)
    lyr = m.addDataFromPath(fc_path)
    set_layer_name(lyr, name, msgs)
    return _add_to_group(m, _find_group(m), lyr, name, msgs)


def select(arcpy, layer, ids, field):
    """New selection of `ids` (by `field`) on `layer`; an empty list clears the selection."""
    where = gpkg.in_clause(arcpy.AddFieldDelimiters(gpkg.dataset_path(layer_source(layer)), field), ids)
    arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION" if ids else "CLEAR_SELECTION",
                                            where if ids else "")


def select_in_model(arcpy, gpkg_path, table, ids, field, msgs, what: str) -> bool:
    """Select `ids` on the model's `table` layer; warn (and return False) when it is not in the map."""
    lyr = find_layer_for(arcpy, gpkg_path, table)
    if lyr is None:
        msgs.warn(f"The model's {what} layer is not in the active map; nothing selected there "
                  "(add the model with 'Add Model To Map').")
        return False
    try:
        select(arcpy, lyr, ids, field)
        return True
    except Exception as exc:  # noqa: BLE001
        msgs.warn(f"Could not select {what}: {exc}")
        return False
