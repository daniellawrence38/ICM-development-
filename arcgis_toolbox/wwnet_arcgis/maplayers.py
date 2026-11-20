"""Add WWNet GeoPackage layers to the active map (arcpy.mp) in a "WWNet" group layer."""
from __future__ import annotations

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


def layer_source(lyr) -> str:
    """A layer's dataSource, or '' when the layer type doesn't support it (basemaps, groups ...).
    Pro raises a non-AttributeError for unsupported attributes, so getattr() defaults don't help."""
    try:
        sup = getattr(lyr, "supports", None)
        if callable(sup) and not sup("DATASOURCE"):
            return ""
        return lyr.dataSource or ""
    except Exception:  # noqa: BLE001
        return ""


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
        g = m.createGroupLayer(GROUP_NAME)
        return g
    except Exception as exc:  # noqa: BLE001 - older/unsupported API
        msgs.warn(f"Could not create the '{GROUP_NAME}' group layer ({exc}); layers added ungrouped.")
        return None


def add_model(arcpy, gpkg_path: str, msgs, symbolize: bool = True) -> list:
    """Add every wwn_* feature table present in the file. Returns the added layers."""
    present = gpkg.contents(gpkg_path)
    if "wwn_subcatchments" in present:
        try:
            if gpkg.upgrade_model(gpkg_path):
                msgs.info("Upgraded the model to schema v2 (node loads, profiles, subcatchment ADWF / profile fields).")
                present = gpkg.contents(gpkg_path)
        except Exception as exc:  # noqa: BLE001 - e.g. read-only file: show the layers anyway
            msgs.warn(f"Could not upgrade the model to schema v2 ({exc}); new fields may be missing.")
    tables = [t for t in gpkg.FEATURE_ORDER if t in present]
    if not tables:
        raise RuntimeError(f"{gpkg_path} contains no WWNet feature tables (wwn_nodes, wwn_conduits, ...).")
    m = active_map(arcpy)
    group = _ensure_group(m, msgs)
    added = []
    for t in tables:
        try:
            lyr = m.addDataFromPath(gpkg.table_path(gpkg_path, t))
            set_layer_name(lyr, present[t][0] or t)
            if group is not None:
                try:
                    m.addLayerToGroup(group, lyr)
                    m.removeLayer(lyr)
                    lyr = next((x for x in group.listLayers() if layer_name(x) == (present[t][0] or t)), lyr)
                except Exception as exc:  # noqa: BLE001
                    msgs.warn(f"Could not move {t} into the group layer: {exc}")
            added.append((t, lyr))
            msgs.info(f"Added {present[t][0] or t}")
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not add {t}: {exc}")
    if symbolize:
        for t, lyr in added:
            symbology.apply(t, lyr, msgs)
    for t in gpkg.ATTRIBUTE_ORDER:
        if t in present:
            try:
                m.addDataFromPath(gpkg.table_path(gpkg_path, t))
                msgs.info(f"Added table {present[t][0] or t}")
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not add table {t}: {exc}")
    return [lyr for _, lyr in added]


def add_results_table(arcpy, gpkg_path: str, msgs):
    m = active_map(arcpy)
    tbl = m.addDataFromPath(gpkg.table_path(gpkg_path, "wwn_results_conduits"))
    msgs.info("Added the design results table to the map.")
    return tbl


def remove_layers_named(arcpy, name: str) -> int:
    """Remove every map layer / table called `name` (also inside groups). Returns how many."""
    m = active_map(arcpy)
    n = 0
    for lyr in [x for x in list(m.listLayers()) + list(m.listTables()) if layer_name(x) == name]:
        m.removeLayer(lyr)
        n += 1
    return n


def add_results_layer(arcpy, fc_path: str, name: str, msgs):
    """Add the in-memory results feature class to the active map (in the WWNet group if present)."""
    m = active_map(arcpy)
    lyr = m.addDataFromPath(fc_path)
    renamed = set_layer_name(lyr, name, msgs)
    group = _find_group(m)
    if group is not None:
        try:
            m.addLayerToGroup(group, lyr)
            m.removeLayer(lyr)
            want = name if renamed else layer_name(lyr)
            lyr = next((x for x in group.listLayers() if layer_name(x) == want), lyr)
        except Exception as exc:  # noqa: BLE001
            msgs.warn(f"Could not move the results layer into the group layer: {exc}")
    return lyr


def find_layer_for(arcpy, gpkg_path: str, table: str):
    """The map layer showing `table` of `gpkg_path`, or None."""
    norm = lambda p: str(p).replace("/", "\\").lower()  # noqa: E731
    m = active_map(arcpy)
    for lyr in list(m.listLayers()) + list(m.listTables()):
        ds = layer_source(lyr)
        g, t = gpkg.split_datasource(ds)
        if t == table and g and norm(g) == norm(gpkg_path):
            return lyr
    return None
