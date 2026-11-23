"""'Import Sewer Data': read Water Corporation GIS layers with arcpy, build the model with the shared
pure-Python importer (``wwnet_shared.core.importer``) and write a GeoPackage via ``wwnet_native``."""
from __future__ import annotations

import re

from . import gpkg, shared

TARGET_EPSG = 7850
MAX_ISSUE_LINES = 50
MAX_WARNING_LINES = 40
EXTENT_BUFFER_M = 2.0           # sewer extent is grown by this much when reading chambers / connection points
ID_CHUNK = 900                  # ids per `ID IN (...)` query
MAX_ID_LOOKUP = 5000            # more referenced-but-unmatched ids than this are not looked up in the CP layer
_NUMERIC_TYPES = ("Integer", "SmallInteger", "BigInteger", "Double", "Single", "OID")

#: (field role, layer key, parameter label, required)  -- one Field parameter per role, in this order
FIELD_ROLES = [
    ("ac_id", "ac", "Access chamber ID field", True),
    ("ac_surface_level", "ac", "Access chamber lid level field (m AHD)", False),
    ("chamber_type", "ac", "Access chamber type field (M / J / I)", False),
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


def _columns(arcpy, src, roles_fields: dict, label: str):
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
    return cols


def _rows(arcpy, cursor_src, cols, kind: str, **kw) -> list:
    GEOM = importer().GEOM
    token = "SHAPE@XY" if kind == "point" else "SHAPE@"
    out = []
    sr = arcpy.SpatialReference(TARGET_EPSG)
    with arcpy.da.SearchCursor(cursor_src, cols + [token], spatial_reference=sr, **kw) as cur:
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
    return out


def selection_count(src) -> int:
    try:
        sel = src.getSelectionSet() if hasattr(src, "getSelectionSet") else None
    except Exception:  # noqa: BLE001
        sel = None
    return len(sel) if sel else 0


def read_features(arcpy, src, roles_fields: dict, kind: str, label: str, use_selected: bool, msgs) -> list:
    """List of {source field: value, GEOM: coords} read with spatial_reference EPSG:7850."""
    cols = _columns(arcpy, src, roles_fields, label)
    cursor_src = _cursor_source(arcpy, src, use_selected, msgs, label)
    out = _rows(arcpy, cursor_src, cols, kind)
    msgs.info(f"{label}: read {len(out)} feature(s).")
    return out


def rows_extent(rows: list, buffer_m: float = EXTENT_BUFFER_M):
    """(xmin, ymin, xmax, ymax) of the geometry of `rows` (points or lines) grown by `buffer_m`, or None."""
    GEOM = importer().GEOM
    xs, ys = [], []
    for r in rows:
        g = r.get(GEOM)
        if not g:
            continue
        pts = [g] if isinstance(g[0], (int, float)) else g
        for p in pts:
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        return None
    return (min(xs) - buffer_m, min(ys) - buffer_m, max(xs) + buffer_m, max(ys) + buffer_m)


def extent_polygon(arcpy, ext):
    sr = arcpy.SpatialReference(TARGET_EPSG)
    x0, y0, x1, y1 = ext
    pts = [arcpy.Point(x, y) for x, y in ((x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0))]
    return arcpy.Polygon(arcpy.Array(pts), sr)


def read_in_extent(arcpy, src, roles_fields: dict, kind: str, label: str, ext, msgs, where=None):
    """Read only the features intersecting `ext` (spatial filter on the data source). Returns None, after a
    message, when the installed arcpy cannot do that (caller decides on a fallback)."""
    cols = _columns(arcpy, src, roles_fields, label)
    cursor_src = _cursor_source(arcpy, src, False, msgs, label)
    try:
        return _rows(arcpy, cursor_src, cols, kind, where_clause=where,
                     spatial_filter=extent_polygon(arcpy, ext), spatial_relationship="INTERSECTS")
    except TypeError:
        msgs.warn(f"{label}: this ArcGIS version does not support a spatial filter on SearchCursor.")
        return None


def _id_literals(arcpy, src, id_field: str, ids) -> list:
    """SQL literals for `ids`, matching the id field's type (text is quoted, numbers are not)."""
    ftype = ""
    for f in arcpy.ListFields(src):
        if f.name.lower() == id_field.lower():
            ftype = getattr(f, "type", "")
    if ftype in _NUMERIC_TYPES:
        return [i for i in ids if re.fullmatch(r"-?\d+", i)]
    return [gpkg.sql_quote(i) for i in ids]


def read_connection_points(arcpy, src, roles_fields: dict, id_field: str, ref_ids, ext, use_selected: bool,
                           msgs, label: str = "Connection points", buffer_m: float = EXTENT_BUFFER_M) -> list:
    """Connection points are never read in full (the layer can hold ~1e6 features). Reads (a) those whose id
    is in `ref_ids` (sewer end ids not matched by a chamber) and (b) those within `ext` (sewer extent grown
    by `buffer_m`: 2 m, or the connection point load search radius when loads are imported). A selection in the layer is honoured when `use_selected`."""
    GEOM = importer().GEOM
    if use_selected and selection_count(src):
        return read_features(arcpy, src, roles_fields, "point", label, True, msgs)
    cols = _columns(arcpy, src, roles_fields, label)
    cursor_src = _cursor_source(arcpy, src, False, msgs, label)
    actual_id = next(c for c in cols if c.lower() == id_field.lower())
    out, seen = [], set()

    def add(rows):
        n = 0
        for r in rows:
            k = str(r.get(actual_id))
            if k not in seen:
                seen.add(k)
                out.append(r)
                n += 1
        return n

    ids = sorted(set(ref_ids or ()))
    n_id = 0
    if len(ids) > MAX_ID_LOOKUP:
        msgs.warn(f"{label}: {len(ids)} referenced ids are not chambers; not looking them up by id "
                  f"(limit {MAX_ID_LOOKUP}), using the sewer extent only.")
        ids = []
    lits = _id_literals(arcpy, src, actual_id, ids)
    delim = arcpy.AddFieldDelimiters(cursor_src, actual_id)
    for i in range(0, len(lits), ID_CHUNK):
        where = f"{delim} IN ({', '.join(lits[i:i + ID_CHUNK])})"
        n_id += add(_rows(arcpy, cursor_src, cols, "point", where_clause=where))
    n_ext = 0
    if ext is not None:
        got = read_in_extent(arcpy, src, roles_fields, "point", label, ext, msgs)
        if got is None:
            msgs.warn(f"{label}: read by id only; connection points that are only located by geometry "
                      "will be missing.")
        else:
            n_ext = add(got)
    msgs.info(f"{label}: read {len(out)} feature(s) by query (not the whole layer): {n_id} matching "
              f"{len(lits)} sewer end id(s), {n_ext} more within the sewer extent +{buffer_m:g} m.")
    for r in out:
        r.setdefault(GEOM, None)
    return out


def add_referenced_chambers(arcpy, src, roles_fields: dict, id_field: str, ref_ids, rows: list, msgs, ext=None,
                            n_selected=None, extent_too: bool = True, label: str = "Access chambers") -> list:
    """Context chambers: `rows` (already read) plus the chambers whose id is in `ref_ids` (sewer end ids) and,
    when `ext` is given, those within it. Never scans the whole layer. Added rows carry the CONTEXT flag.
    The chamber-type rule is applied later by the importer."""
    imp = importer()
    cols = _columns(arcpy, src, roles_fields, label)
    cursor_src = _cursor_source(arcpy, src, False, msgs, label)
    actual_id = next(c for c in cols if c.lower() == id_field.lower())
    seen = {imp.to_id(r.get(actual_id)) for r in rows}
    out = list(rows)
    n_sel = len(rows) if n_selected is None else n_selected
    todo = sorted(set(i for i in (ref_ids or ()) if i and i not in seen))
    n_id = n_ext = 0

    def add(new):
        n = 0
        for r in new:
            k = imp.to_id(r.get(actual_id))
            if k in seen:
                continue
            seen.add(k)
            r[imp.CONTEXT] = True
            out.append(r)
            n += 1
        return n

    lits = _id_literals(arcpy, src, actual_id, todo)
    delim = arcpy.AddFieldDelimiters(cursor_src, actual_id)
    for i in range(0, len(lits), ID_CHUNK):
        where = f"{delim} IN ({', '.join(lits[i:i + ID_CHUNK])})"
        n_id += add(_rows(arcpy, cursor_src, cols, "point", where_clause=where))
    if ext is not None and extent_too:
        got = read_in_extent(arcpy, src, roles_fields, "point", label, ext, msgs)
        if got is None:
            msgs.warn(f"{label}: referenced chambers read by id only; chambers only located by geometry "
                      "will be missing.")
        else:
            n_ext = add(got)
    if extent_too or n_id:
        msgs.info(f"{label}: {n_sel} selected + {n_id + n_ext} referenced/nearby read (not the whole layer): "
                  f"{n_id} by id, {n_ext} within the sewer extent +{EXTENT_BUFFER_M:g} m.")
    return out


def roles_for(layer_key: str, fields: dict) -> dict:
    return {r: fields.get(r) or "" for r, k, _l, _q in FIELD_ROLES if k == layer_key}


def report_to_messages(msgs, rep, max_warnings: int = MAX_WARNING_LINES):
    """Summary by code first (info), then example warnings spread over the codes (max 5 per code)."""
    for line in rep.summary_lines(max_warnings=0):
        msgs.info(line)
    ex = rep.example_warnings(max_warnings)
    for w in ex:
        msgs.warn(str(w))
    if len(rep.warnings) > len(ex):
        msgs.warn(f"... {len(rep.warnings) - len(ex)} more import warning(s) not shown: all of them are in the "
                  "model's Issues layer (source 'import').")
    if rep.infos:
        msgs.info(f"{len(rep.infos)} diameter(s) mapped to the nearest standard DN (raw value kept as the "
                  f"internal diameter), e.g. {rep.infos[0]}")


def native_with_write():
    """The engine with write_model / check / store_issues (EngineError with install help if missing)."""
    from . import engine
    return engine.native("write_model", "check", "store_issues")


def _tnorm(name: str) -> str:
    return str(name).upper().replace(" ", "_")


def choose_gda_transformation(arcpy, from_sr) -> str:
    """Pick a DIRECT GDA94 -> GDA2020 transformation from those ArcGIS lists: the name must contain
    GDA_1994_TO_GDA2020 and neither 'AGD' nor '+' (composites go via AGD66 and are wrong). Preference:
    NTv2 conformal-and-distortion, then NTv2 conformal, then any other direct one. '' if none."""
    try:
        names = list(arcpy.ListTransformations(from_sr, arcpy.SpatialReference(TARGET_EPSG)) or [])
    except Exception:  # noqa: BLE001
        return ""
    direct = [n for n in names if "GDA_1994_TO_GDA2020" in _tnorm(n) and "AGD" not in _tnorm(n) and "+" not in n]

    def ntv2(n):
        return "NTV2" in _tnorm(n)

    def cd(n):
        u = _tnorm(n)
        return "CONFORMAL_AND_DISTORTION" in u or re.search(r"(^|_)CD($|_)", u) is not None

    for ok in (lambda n: ntv2(n) and cd(n), lambda n: ntv2(n) and "CONFORMAL" in _tnorm(n), lambda n: True):
        for n in direct:
            if ok(n):
                return n
    return ""
