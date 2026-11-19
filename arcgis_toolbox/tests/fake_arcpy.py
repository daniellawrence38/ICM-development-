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
        return [gpkg_line_length(v) if f == "SHAPE@LENGTH" else v for f, v in zip(self.fields, raw)]

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

    def list_fields(ds):
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

    a.memory = {}   # "memory\\name" -> {"fields": [(name, type)], "rows": [row], "sr": ..., "geom": ...}
    a.deleted = []
    a.Exists = lambda p: p in a.memory
    a.SpatialReference = lambda code: NS(factoryCode=code, name=f"EPSG:{code}")
    a.Describe = lambda p: NS(spatialReference=NS(factoryCode=7850, name="GDA2020 / MGA zone 50"))

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

    def search(ds, f, where_clause=None):
        if ds in a.memory:
            return _MemCursor(a.memory[ds]["rows"], f)
        return _Cursor(a, ds, f, where_clause)

    a.da = NS(SearchCursor=search,
              UpdateCursor=lambda ds, f, where_clause=None: _Cursor(a, ds, f, where_clause, True),
              Editor=FakeEditor, InsertCursor=Insert)
    def create_fc(workspace, name, geometry_type="POLYLINE", template=None, has_m=None, has_z=None,
                  spatial_reference=None):
        assert workspace == "memory", "fake arcpy: only the memory workspace is supported"
        a.memory["memory\\" + name] = {"fields": [], "rows": [], "sr": spatial_reference, "geom": geometry_type}

    def add_field(table, name, ftype=None, *args, **kw):
        if table in a.memory:
            a.memory[table]["fields"].append((name, ftype))
        else:
            a.added.append(("field", name))

    def delete_fc(path):
        a.deleted.append(path)
        a.memory.pop(path, None)

    a.management = NS(
        SelectLayerByAttribute=lambda lyr, mode, where="": a.selections.append((lyr.name, mode, where)),
        AddJoin=lambda *args: a.joins.append(args),
        CreateTable=lambda folder, name: a.added.append(("table", folder, name)),
        AddField=add_field, Delete=delete_fc, CreateFeatureclass=create_fc)

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
