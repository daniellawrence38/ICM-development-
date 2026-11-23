"""A minimal fake `arcpy` for tests: messages, cursors backed by sqlite3 over a real GeoPackage,
Editor, mp (active map), management calls and Parameter. Only what the toolbox uses."""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import types
from types import SimpleNamespace as NS

_SPLIT = re.compile(r"^(.*?\.gpkg)[\\/]+(?:main\.)?([^\\/]+)$", re.IGNORECASE)


def split(ds):
    m = _SPLIT.match(ds)
    assert m, f"fake arcpy: unsupported data source {ds!r}"
    return m.group(1), m.group(2)


def gpkg_line_length(blob: bytes):
    if blob is None:
        return None
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 7]
    wkb = blob[8 + env:]
    bo = "<" if wkb[0] == 1 else ">"
    gtype, n = struct.unpack(bo + "II", wkb[1:9])
    assert gtype == 2
    pts = [struct.unpack(bo + "dd", wkb[9 + 16 * i:25 + 16 * i]) for i in range(n)]
    return sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))


def gpkg_point_xy(blob: bytes):
    if blob is None:
        return None
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 7]
    wkb = blob[8 + env:]
    bo = "<" if wkb[0] == 1 else ">"
    assert struct.unpack(bo + "I", wkb[1:5])[0] == 1
    return struct.unpack(bo + "dd", wkb[5:21])


class _Cursor:
    def __init__(self, arcpy, ds, fields, where_clause=None, writable=False):
        self.arcpy, self.writable = arcpy, writable
        self.path, self.table = split(ds)
        self.fields = list(fields)
        self.con = sqlite3.connect(self.path)
        cols = ["geom" if f.startswith("SHAPE@") else f'"{f}"' for f in self.fields]
        sql = f'SELECT fid, {", ".join(cols)} FROM "{self.table}"'
        if where_clause:
            sql += f" WHERE {where_clause}"
        arcpy.queries.append(sql)
        self.rows = self.con.execute(sql).fetchall()
        self.i = -1

    def _convert(self, raw):
        return [gpkg_line_length(v) if f == "SHAPE@LENGTH" else gpkg_point_xy(v) if f == "SHAPE@XY" else v
                for f, v in zip(self.fields, raw)]

    def __iter__(self):
        for i, r in enumerate(self.rows):
            self.i = i
            yield self._convert(r[1:])

    def updateRow(self, row):
        assert self.writable
        fid = self.rows[self.i][0]
        self.arcpy.writes.append((self.table, fid, dict(zip(self.fields, row))))
        sets = ", ".join(f'"{f}" = ?' for f in self.fields)
        self.con.execute(f'UPDATE "{self.table}" SET {sets} WHERE fid = ?', list(row) + [fid])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if self.writable:
            self.con.commit()
        self.con.close()
        return False


class _MemCursor:
    def __init__(self, rows, fields):
        self.rows, self.fields = rows, list(fields)

    def __iter__(self):
        return iter([[r.get(f) for f in self.fields] for r in self.rows])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Part(list):
    pass


class FakeShape:
    """Polyline stand-in: partCount / getPart(i) -> points with .X .Y (like arcpy.Polyline)."""

    def __init__(self, coords):
        self.coords = coords
        self.partCount = 1

    def getPart(self, i):
        return _Part(NS(X=x, Y=y) for x, y in self.coords)


class _SrcCursor(_MemCursor):
    """Rows of dicts; 'SHAPE@XY' -> (x, y), 'SHAPE@' -> FakeShape built from the row's '_geom'."""

    def __iter__(self):
        for r in self.rows:
            g = r.get("_geom")
            out = []
            for f in self.fields:
                if f == "SHAPE@XY":
                    out.append(tuple(g) if g is not None else None)
                elif f == "SHAPE@":
                    out.append(FakeShape(g) if g is not None else None)
                else:
                    out.append(r.get(f))
            yield out


def _in_bbox(g, x0, y0, x1, y1):
    pts = [g] if isinstance(g[0], (int, float)) else g
    if isinstance(g[0], (int, float)):
        return x0 <= g[0] <= x1 and y0 <= g[1] <= y1
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return not (max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1)


class FakePolygon:
    def __init__(self, pts, sr=None):
        self.pts, self.spatialReference = list(pts), sr
        xs, ys = [p.X for p in self.pts], [p.Y for p in self.pts]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))


class FakeEditor:
    instances: list = []

    def __init__(self, workspace):
        self.workspace, self.calls = workspace, []
        FakeEditor.instances.append(self)

    def __getattr__(self, name):
        def rec(*a):
            self.calls.append((name, a))
        return rec


class _Filter:
    def __init__(self):
        self.type, self.list = None, []


class FakeParameter:
    def __init__(self, displayName=None, name=None, datatype=None, parameterType=None,
                 direction=None, multiValue=False):
        self.displayName, self.name, self.datatype = displayName, name, datatype
        self.parameterType, self.direction, self.multiValue = parameterType, direction, multiValue
        self.value, self.filter = None, _Filter()
        self.controlCLSID = None
        self.hasBeenValidated = False
        self._deps = []

    @property
    def parameterDependencies(self):
        return self._deps

    @parameterDependencies.setter
    def parameterDependencies(self, deps):
        # Real ArcGIS Pro fails to open the tool ("tool has failed to open") unless these are
        # parameter NAMES (strings); indices are rejected.
        if not all(isinstance(d, str) for d in deps):
            raise ValueError(f"parameterDependencies must be parameter names, got {deps!r}")
        self._deps = list(deps)

    @property
    def valueAsText(self):
        return None if self.value is None else str(self.value)


def make_layer(name, ds, selection=None, feature=True):
    return NS(name=name, dataSource=ds, isFeatureLayer=feature, isGroupLayer=False, isBroken=False,
              getSelectionSet=lambda: list(selection) if selection else None)


def make_fake(layers=None, map_name="Map"):
    a = types.ModuleType("arcpy")
    a.log, a.queries, a.writes = [], [], []
    a.selections, a.joins, a.added, a.inserted, a.removed = [], [], [], [], []
    a.AddMessage = lambda t: a.log.append(("info", t))
    a.AddWarning = lambda t: a.log.append(("warn", t))
    a.AddError = lambda t: a.log.append(("error", t))
    a.AddFieldDelimiters = lambda ds, f: f'"{f}"'
    a.Parameter = FakeParameter
    a.Point = lambda x, y: NS(X=x, Y=y)
    a.Array = lambda pts=None: list(pts or [])
    a.Polygon = FakePolygon

    a.sources = {}   # data source path -> {"fields": [(name, type)], "rows": [dict], "sr": NS(...)}
    a.env = NS(overwriteOutput=False, geographicTransformations=None)

    def src_of(ds):
        return a.sources.get(getattr(ds, "dataSource", ds))

    def list_fields(ds):
        if src_of(ds) is not None:
            return [NS(name=n, type=t) for n, t in src_of(ds)["fields"]]
        path, table = split(ds)
        con = sqlite3.connect(path)
        try:
            geom = {r[0] for r in con.execute(
                "SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?", (table,))}
            out = []
            for _cid, name, typ, _nn, _d, pk in con.execute(f'PRAGMA table_info("{table}")'):
                if name in geom:
                    t = "Geometry"
                elif pk:
                    t = "OID"
                else:
                    t = {"INTEGER": "Integer", "REAL": "Double"}.get((typ or "").upper(), "String")
                out.append(NS(name=name, type=t))
            return out
        finally:
            con.close()

    a.ListFields = list_fields

    a.cursor_calls = []
    a.memory = {}   # "memory\\name" -> {"fields": [(name, type)], "rows": [row], "sr": ..., "geom": ...}
    a.deleted = []
    a.Exists = lambda p: p in a.memory or p in a.gdbs
    a.SpatialReference = lambda code: NS(factoryCode=code, name=f"EPSG:{code}")
    a.transformations = ["GDA_1994_To_GDA2020_1", "GDA_1994_To_GDA2020_NTv2_Conformal",
                         "GDA_1994_To_GDA2020_NTv2_Conformal_and_Distortion"]
    a.ListTransformations = lambda from_sr, to_sr, *args: list(a.transformations)
    a.Describe = lambda p: NS(spatialReference=(src_of(p) or {}).get(
        "sr", NS(factoryCode=7850, name="GDA2020 / MGA zone 50")))

    class Insert:
        def __init__(self, path, fields):
            self.path, self.fields = path, list(fields)

        def insertRow(self, row):
            if self.path in a.memory:
                a.memory[self.path]["rows"].append(dict(zip(self.fields, row)))
            else:
                a.inserted.append(row)

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    a.cursor_log = []                  # one dict per synthetic-source read: where / spatial filter / count
    a.spatial_filter_supported = True  # False: SearchCursor raises TypeError for spatial_filter

    def where_match(where, row):
        """Only `"F" IN (...)` (as built by the tool) is understood; anything else matches every row."""
        m = re.fullmatch(r'\s*"?(\w+)"?\s+IN\s*\((.*)\)\s*', where or "", re.S)
        if not m:
            return True
        vals = [v.strip().strip("'").replace("''", "'") for v in re.findall(r"'(?:[^']|'')*'|[^,\s]+", m.group(2))]
        return str(row.get(m.group(1))) in vals

    def search(ds, f, where_clause=None, spatial_reference=None, spatial_filter=None,
               spatial_relationship=None):
        src = src_of(ds)
        if src is not None:                       # synthetic source layer (Import Sewer Data tests)
            if spatial_filter is not None and not a.spatial_filter_supported:
                raise TypeError("SearchCursor() got an unexpected keyword argument 'spatial_filter'")
            a.cursor_calls.append((f, spatial_reference))
            sel = ds.getSelectionSet() if hasattr(ds, "getSelectionSet") else None
            rows = [r for i, r in enumerate(src["rows"], 1) if not sel or i in sel]
            if where_clause:
                rows = [r for r in rows if where_match(where_clause, r)]
            if spatial_filter is not None:
                x0, y0, x1, y1 = spatial_filter.bbox
                rows = [r for r in rows if r.get("_geom") is not None and _in_bbox(r["_geom"], x0, y0, x1, y1)]
            a.cursor_log.append({"source": getattr(ds, "dataSource", ds), "where": where_clause,
                                 "spatial_filter": spatial_filter, "relationship": spatial_relationship,
                                 "selected": bool(sel), "rows": len(rows),
                                 "full_scan": not where_clause and spatial_filter is None and not sel})
            return _SrcCursor(rows, f)
        if ds in a.memory:
            return _MemCursor(a.memory[ds]["rows"], f)
        return _Cursor(a, ds, f, where_clause)

    a.da = NS(SearchCursor=search,
              UpdateCursor=lambda ds, f, where_clause=None: _Cursor(a, ds, f, where_clause, True),
              Editor=FakeEditor, InsertCursor=Insert)
    def create_fc(workspace, name, geometry_type="POLYLINE", template=None, has_m=None, has_z=None,
                  spatial_reference=None):
        key = "memory\\" + name if workspace == "memory" else os.path.join(workspace, name)
        if workspace != "memory":
            assert workspace in a.gdbs, f"fake arcpy: workspace {workspace!r} does not exist"
        a.memory[key] = {"fields": [], "rows": [], "sr": spatial_reference, "geom": geometry_type}

    def add_field(table, name, ftype=None, *args, **kw):
        if table in a.memory:
            a.memory[table]["fields"].append((name, ftype))
        else:
            a.added.append(("field", name))

    a.gdbs = set()

    def create_gdb(folder, name):
        assert os.path.isdir(folder), f"fake arcpy: folder {folder!r} does not exist"
        a.gdbs.add(os.path.join(folder, name))

    def delete_fc(path):
        a.deleted.append(path)
        a.memory.pop(path, None)

    a.management = NS(
        SelectLayerByAttribute=lambda lyr, mode, where="": a.selections.append((lyr.name, mode, where)),
        AddJoin=lambda *args: a.joins.append(args),
        CreateTable=lambda folder, name: a.added.append(("table", folder, name)),
        AddField=add_field, Delete=delete_fc, CreateFeatureclass=create_fc, CreateFileGDB=create_gdb)

    layers = layers if layers is not None else []

    class Map:
        name = map_name

        def listLayers(self):
            return list(layers)

        def listTables(self):
            return []

        def addDataFromPath(self, path):
            lyr = make_layer(os.path.basename(path), path)
            a.added.append(("data", path))
            layers.append(lyr)
            return lyr

        def removeLayer(self, lyr):
            layers.remove(lyr)
            a.removed.append(lyr.name)

    m = Map()
    a.mp = NS(ArcGISProject=lambda _x: NS(activeMap=m, filePath="C:/p/test.aprx"))
    return a
