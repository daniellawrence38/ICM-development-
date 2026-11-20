"""'Import Sewer Data': read Water Corporation GIS layers with arcpy, build the model with the shared
pure-Python importer (``wwnet_shared.core.importer``) and write a GeoPackage via ``wwnet_native``."""
from __future__ import annotations

from . import shared

TARGET_EPSG = 7850
# Preference order when choosing a GDA94 -> GDA2020 transformation from the ones ArcGIS lists.
# Names are matched as substrings; the actual name always comes from arcpy.ListTransformations.
TRANSFORM_PREFERENCE = ("NTV2_CONFORMAL_AND_DISTORTION", "NTV2_CD", "NTV2_CONFORMAL", "NTV2", "")
MAX_ISSUE_LINES = 50
MAX_WARNING_LINES = 60

#: (field role, layer key, parameter label, required)  -- one Field parameter per role, in this order
FIELD_ROLES = [
    ("ac_id", "ac", "Access chamber ID field", True),
    ("ac_surface_level", "ac", "Access chamber lid level field (m AHD)", False),
    ("cp_id", "cp", "Connection point ID field", False),
    ("pipe_id", "sewer", "Sewer ID field", True),
    ("us_node", "sewer", "Sewer upstream chamber ID field", False),
    ("ds_node", "sewer", "Sewer downstream chamber ID field", False),
    ("us_invert", "sewer", "Sewer upstream invert field (m AHD)", False),
    ("ds_invert", "sewer", "Sewer downstream invert field (m AHD)", False),
    ("flow_grade", "sewer", "Sewer grade field (1 in x)", False),
    ("length", "sewer", "Sewer length field (m)", False),
    ("diameter", "sewer", "Sewer diameter field (mm)", False),
    ("status", "sewer", "Sewer status field", False),
    ("sewer_type", "sewer", "Sewer type field (MAINTYPE)", False),
    ("purpose", "sewer", "Sewer purpose field", False),
    ("material", "sewer", "Sewer material field", False),
    ("install_date", "sewer", "Sewer install date field", False),
    ("planset", "sewer", "Sewer plan set field", False),
]
ROLE_LAYER = {r: k for r, k, _l, _q in FIELD_ROLES}


def importer():
    return shared.core("importer")


def template_defaults() -> dict:
    """role -> default source field name from the bundled template ({} if it cannot be loaded)."""
    try:
        m = importer()
        fm = m.FieldMap.from_config()
        return {r: getattr(fm, r) for r, _k, _l, _q in FIELD_ROLES}
    except Exception:  # noqa: BLE001 - parameter UI must not fail on a packaging problem
        return {}


def layer_field_names(arcpy, source) -> list:
    return [f.name for f in arcpy.ListFields(source)
            if getattr(f, "type", "") not in ("Geometry", "Blob", "Raster")]


def pick_defaults(names, defaults: dict) -> dict:
    """role -> the layer's actual field name matching the default (case-insensitive), '' if absent."""
    lower = {n.lower(): n for n in names}
    return {r: lower.get((d or "").lower(), "") for r, d in defaults.items()}


def is_gda94(sr) -> bool:
    code = getattr(sr, "factoryCode", 0) or 0
    name = str(getattr(sr, "name", "") or "").upper().replace(" ", "_")
    return code == 4283 or 28348 <= code <= 28358 or "GDA_1994" in name or "GDA94" in name


def prepare_projection(arcpy, msgs, sources: list) -> str:
    """Describe each source's SR; set the GDA94 -> GDA2020 transformation when needed. Returns a note."""
    gda94, notes, gda94_sr = False, [], None
    for label, src in sources:
        sr = arcpy.Describe(src).spatialReference
        code, name = getattr(sr, "factoryCode", 0) or 0, getattr(sr, "name", "")
        if code == TARGET_EPSG:
            notes.append(f"{label}: EPSG:7850")
            continue
        if not code or str(name).lower() == "unknown":
            msgs.warn(f"{label} has no spatial reference; its coordinates are ASSUMED to be EPSG:{TARGET_EPSG}.")
            continue
        notes.append(f"{label}: {name} ({code})")
        msgs.info(f"{label}: {name} ({code}) will be projected to GDA2020 / MGA zone 50 (EPSG:7850).")
        if is_gda94(sr) and not gda94:
            gda94, gda94_sr = True, sr
        gda94 = gda94 or is_gda94(sr)
    if gda94:
        name = choose_gda_transformation(arcpy, gda94_sr)
        if name:
            try:
                arcpy.env.geographicTransformations = name
                msgs.info(f"Source data is GDA94: using geographic transformation {name}.")
            except Exception as exc:  # noqa: BLE001
                msgs.warn(f"Could not set the geographic transformation {name} ({exc}); ArcGIS will use its "
                          "default GDA94 -> GDA2020 transformation, if any. Check the coordinates.")
        else:
            msgs.warn("No GDA94 -> GDA2020 transformation is listed by ArcGIS; coordinates may be up to "
                      "~1.5 m out. Check against a known GDA2020 feature.")
    return "; ".join(notes)


def _cursor_source(arcpy, src, use_selected: bool, msgs, label: str):
    """Layer object / name honouring the selection, or the underlying data source (all features)."""
    sel = None
    if hasattr(src, "getSelectionSet"):
        try:
            sel = src.getSelectionSet()
        except Exception:  # noqa: BLE001
            sel = None
    if use_selected:
        if sel:
            msgs.info(f"{label}: using {len(sel)} selected feature(s).")
        else:
            msgs.info(f"{label}: nothing selected; using all features.")
        return src
    if hasattr(src, "dataSource"):
        return src.dataSource
    try:
        return getattr(arcpy.Describe(src), "catalogPath", None) or src
    except Exception:  # noqa: BLE001
        return src


def _line_coords(shape):
    pts = []
    for i in range(shape.partCount):
        for p in shape.getPart(i):
            if p is not None:
                pts.append((p.X, p.Y))
    return pts


def read_features(arcpy, src, roles_fields: dict, kind: str, label: str, use_selected: bool, msgs) -> list:
    """List of {source field: value, GEOM: coords} read with spatial_reference EPSG:7850."""
    GEOM = importer().GEOM
    names = layer_field_names(arcpy, src)
    lower = {n.lower(): n for n in names}
    cols, seen = [], set()
    for role, fname in roles_fields.items():
        if not fname:
            continue
        actual = lower.get(fname.lower())
        if actual is None:
            raise RuntimeError(f"{label}: field {fname!r} (role {role}) does not exist. "
                               f"Available: {', '.join(names)}")
        if actual not in seen:
            seen.add(actual)
            cols.append(actual)
    token = "SHAPE@XY" if kind == "point" else "SHAPE@"
    cursor_src = _cursor_source(arcpy, src, use_selected, msgs, label)
    out = []
    sr = arcpy.SpatialReference(TARGET_EPSG)
    with arcpy.da.SearchCursor(cursor_src, cols + [token], spatial_reference=sr) as cur:
        for row in cur:
            d = dict(zip(cols, row[:-1]))
            shape = row[-1]
            if shape is None:
                d[GEOM] = None
            elif kind == "point":
                d[GEOM] = (shape[0], shape[1]) if shape[0] is not None else None
            else:
                d[GEOM] = _line_coords(shape)
            out.append(d)
    msgs.info(f"{label}: read {len(out)} feature(s).")
    return out


def roles_for(layer_key: str, fields: dict) -> dict:
    return {r: fields.get(r) or "" for r, k, _l, _q in FIELD_ROLES if k == layer_key}


def report_to_messages(msgs, rep, max_warnings: int = MAX_WARNING_LINES):
    for line in rep.summary_lines(max_warnings=0):
        msgs.info(line)
    ordered = sorted(rep.warnings, key=lambda w: (w.object_type != "import", w.code))
    for w in ordered[:max_warnings]:
        msgs.warn(str(w))
    if len(ordered) > max_warnings:
        msgs.warn(f"... {len(ordered) - max_warnings} more import warning(s) not shown.")
    if rep.infos:
        msgs.info(f"{len(rep.infos)} diameter(s) mapped to the nearest standard DN (raw value kept as the "
                  f"internal diameter), e.g. {rep.infos[0]}")


def native_with_write():
    from . import cli
    native = cli.native_service()
    if native is None:
        raise RuntimeError(cli.no_backend_message())
    if not callable(getattr(native, "write_model", None)):
        raise RuntimeError("The installed wwnet_native is too old: it has no write_model(). Install the "
                           "wheel built from this version of WWNet (or the current no-install bundle).")
    return native


def choose_gda_transformation(arcpy, from_sr) -> str:
    """Pick a GDA94 -> GDA2020 transformation from those ArcGIS actually has, preferring the NTv2
    conformal-and-distortion grid. Returns '' if none is listed."""
    try:
        names = list(arcpy.ListTransformations(from_sr, arcpy.SpatialReference(TARGET_EPSG)) or [])
    except Exception:  # noqa: BLE001
        return ""
    for pref in TRANSFORM_PREFERENCE:
        for n in names:
            if pref in n.upper().replace(" ", "_"):
                return n
    return ""
