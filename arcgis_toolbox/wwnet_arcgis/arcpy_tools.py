"""arcpy-backed implementations of the assistant's model tools.

`arcpy` is injected (or imported lazily) so the module can be tested with a fake.
Reading uses arcpy.da.SearchCursor on each layer's *data source* (so a map selection or
definition query never silently narrows a query); writing goes through arcpy.da.Editor +
UpdateCursor and only for Proposal objects recorded by propose_edits in this very run.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from . import gpkg, maplayers, messages, shared

MAX_ROWS = 200
MAX_SELECTED = 100
_INT = {"OID", "Integer", "SmallInteger", "BigInteger"}
_REAL = {"Double", "Single"}


def _engine():
    return shared.assistant("engine")


def _schema():
    return shared.core("schema")


def _tools():
    return shared.assistant("tools")


def py_value(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(v)


@dataclass(eq=False)
class Proposal:
    layer_name: str
    table: str
    source: str            # catalog path of the table
    workspace: str         # .gpkg file
    id_field: str
    id: str
    field: str
    old: object
    value: object
    reason: str = ""
    applied: bool = False


def default_layers(arcpy):
    """Layers and tables of the active map that have a data source."""
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    m = aprx.activeMap
    if m is None:
        raise _engine().ToolError("No active map. Open a map view first.")
    out = []
    for lyr in list(m.listLayers()) + list(m.listTables()):
        if getattr(lyr, "isGroupLayer", False) or getattr(lyr, "isBroken", False):
            continue
        if maplayers.layer_source(lyr):
            out.append(lyr)
    return out


class ArcpyTools:
    def __init__(self, arcpy=None, apply: bool = False, layers_fn=None, emit=None):
        self._arcpy = arcpy
        self.apply = bool(apply)
        self._layers_fn = layers_fn
        self.emit = emit or (lambda s: None)
        self.proposals: list[Proposal] = []     # everything proposed in this run
        self.apply_log: list[str] = []

    @property
    def arcpy(self):
        if self._arcpy is None:
            self._arcpy = messages.arcpy_mod()
        return self._arcpy

    # ------------------------------------------------------------ helpers
    def layers(self):
        return (self._layers_fn or (lambda: default_layers(self.arcpy)))()

    @staticmethod
    def table_of(layer):
        return gpkg.table_of(maplayers.layer_source(layer))

    def find_layer(self, name: str):
        n = (name or "").strip().lower()
        ls = self.layers()
        for lyr in ls:
            if (self.table_of(lyr) or "").lower() == n:
                return lyr
        for lyr in ls:
            if maplayers.layer_name(lyr).lower() == n:
                return lyr
        avail = [self.table_of(lyr) or maplayers.layer_name(lyr) for lyr in ls]
        raise _engine().ToolError(f"Layer {name!r} not found. Available: {avail}")

    def _fields(self, ds):
        return [f for f in self.arcpy.ListFields(ds) if f.type != "Geometry"]

    def _oid(self, ds):
        for f in self.arcpy.ListFields(ds):
            if f.type == "OID":
                return f.name
        return "fid"

    def _where_eq(self, ds, fld, value):
        return f"{self.arcpy.AddFieldDelimiters(ds, fld)} = {gpkg.sql_quote(value)}"

    def _rows(self, ds, names, where=None, limit=None):
        out = []
        with self.arcpy.da.SearchCursor(ds, names, where_clause=where) as cur:
            for row in cur:
                out.append({n: py_value(v) for n, v in zip(names, row)})
                if limit is not None and len(out) >= limit:
                    break
        return out

    @staticmethod
    def _selection(layer):
        try:
            sel = layer.getSelectionSet()
        except Exception:
            sel = None
        return sorted(sel) if sel else []

    # ---------------------------------------------------------- read tools
    def list_layers(self, args):
        out = []
        for lyr in self.layers():
            ds = gpkg.dataset_path(maplayers.layer_source(lyr))
            tbl = self.table_of(lyr)
            try:
                names = [f.name for f in self._fields(ds)]
                count = len(self._rows(ds, [self._oid(ds)]))
            except Exception as exc:  # noqa: BLE001
                names, count = [], None
                tbl = tbl or f"(unreadable: {exc})"
            out.append({
                "layer_name": lyr.name, "wwnet_table": tbl,
                "geometry": getattr(lyr, "isFeatureLayer", False) and "feature" or "table",
                "feature_count": count, "selected": len(self._selection(lyr)),
                "id_field": _schema().ID_FIELDS.get(tbl or ""), "fields": names,
            })
        return out

    def get_selected_features(self, args):
        lyr = self.find_layer(args["layer"])
        ds = gpkg.dataset_path(maplayers.layer_source(lyr))
        sel = self._selection(lyr)
        if not sel:
            return {"layer": lyr.name, "selected_count": 0, "features": [], "truncated": False}
        oid = self._oid(ds)
        names = [f.name for f in self._fields(ds)]
        where = f"{self.arcpy.AddFieldDelimiters(ds, oid)} IN ({', '.join(str(int(i)) for i in sel[:MAX_SELECTED])})"
        return {"layer": lyr.name, "selected_count": len(sel),
                "features": self._rows(ds, names, where), "truncated": len(sel) > MAX_SELECTED}

    def query_features(self, args):
        lyr = self.find_layer(args["layer"])
        ds = gpkg.dataset_path(maplayers.layer_source(lyr))
        limit = max(1, min(int(args.get("limit") or 50), MAX_ROWS))
        names = [f.name for f in self._fields(ds)]
        try:
            rows = self._rows(ds, names, args["expression"] or None, limit)
        except Exception as exc:  # noqa: BLE001 - bad SQL etc.
            raise _engine().ToolError(f"Query failed (use a SQL where clause): {exc}") from exc
        return {"layer": lyr.name, "returned": len(rows), "limit": limit, "features": rows}

    def get_feature(self, args):
        lyr = self.find_layer(args["layer"])
        ds, tbl = gpkg.dataset_path(maplayers.layer_source(lyr)), self.table_of(lyr)
        idf = _schema().ID_FIELDS.get(tbl or "")
        if not idf:
            raise _engine().ToolError(f"Layer {lyr.name!r} has no known business id field")
        names = [f.name for f in self._fields(ds)]
        where = self._where_eq(ds, idf, args["id_value"])
        extra = []
        if tbl == "wwn_conduits":
            extra = ["SHAPE@LENGTH"]
        try:
            rows = self._rows(ds, names + extra, where, 1)
        except Exception:  # noqa: BLE001 - geometry token unsupported
            rows, extra = self._rows(ds, names, where, 1), []
        if not rows:
            raise _engine().ToolError(f"No feature with {idf} = {args['id_value']!r} in {lyr.name}")
        d = rows[0]
        if tbl == "wwn_conduits":
            geom_len = d.pop("SHAPE@LENGTH", None)
            length = d.get("length_m") or geom_len
            us, ds_inv = d.get("us_invert"), d.get("ds_invert")
            derived = {"length_m_used": length}
            if length and us is not None and ds_inv is not None and us > ds_inv:
                derived["grade_1in"] = round(length / (us - ds_inv), 1)
            d["derived"] = derived
        return d

    # ------------------------------------------------------------- edit tool
    def _field_types(self, ds):
        types = {}
        for f in self._fields(ds):
            types[f.name] = "int" if f.type in _INT else ("real" if f.type in _REAL else "text")
        return types

    def propose_edits(self, args):
        eng = _engine()
        lyr = self.find_layer(args["layer"])
        ds, tbl = gpkg.dataset_path(maplayers.layer_source(lyr)), self.table_of(lyr)
        ws, _ = gpkg.split_datasource(ds)
        if not tbl or not ws:
            raise eng.ToolError("Only WWNet GeoPackage layers can be edited by the assistant")
        valid, problems = _tools().validate_edits(tbl, args.get("edits") or [], self._field_types(ds))
        idf = _schema().ID_FIELDS.get(tbl)
        if not idf:
            raise eng.ToolError(f"{tbl} has no business id field; cannot propose edits")
        batch = []
        for e in valid:
            rows = self._rows(ds, [e["field"]], self._where_eq(ds, idf, e["id"]), 1)
            if not rows:
                problems.append(f"no feature with {idf} = {e['id']!r}")
                continue
            batch.append(Proposal(lyr.name, tbl, ds, ws, idf, e["id"], e["field"],
                                  rows[0][e["field"]], e["value"], str(args.get("reason", ""))))
        if problems:
            raise eng.ToolError("Nothing proposed. Problems: " + "; ".join(problems))
        if not batch:
            raise eng.ToolError("No edits supplied")
        self.proposals.extend(batch)
        if not self.apply:
            return {"applied": False, "outcome": "proposed_not_applied", "count": len(batch),
                    "message": "Edits recorded as PROPOSALS only; nothing was changed. The engineer "
                               "must re-run the tool with 'Apply proposed edits' ticked to apply them."}
        n = self.apply_proposals(batch)
        return {"applied": True, "outcome": "applied_and_saved", "count": n}

    def apply_proposals(self, batch) -> int:
        """Write proposals. Refuses anything not proposed through this instance."""
        if not self.apply:
            raise PermissionError("Apply proposed edits is not ticked; refusing to write.")
        for p in batch:
            if not any(p is q for q in self.proposals):
                raise PermissionError("Refusing to apply an edit that was not proposed in this run.")
        todo = [p for p in batch if not p.applied]
        eng = _engine()
        arcpy = self.arcpy
        by_target: dict = {}
        for p in todo:
            by_target.setdefault((p.workspace, p.source, p.id_field, p.id), []).append(p)
        editor = None
        try:
            editor = arcpy.da.Editor(todo[0].workspace) if todo else None
            if editor:
                editor.startEditing(False, False)
                editor.startOperation()
        except Exception as exc:  # noqa: BLE001
            # No edit session means no undo/rollback: refuse rather than write unprotected.
            raise RuntimeError(
                f"Could not start an edit session on {todo[0].workspace} ({exc}). "
                "No edits were applied. Close other editors of this GeoPackage and re-run."
            ) from exc
        try:
            changes = []
            for (_ws, source, idf, _id), props in by_target.items():
                fields = [idf] + [p.field for p in props]
                where = self._where_eq(source, idf, props[0].id)
                found = False
                with arcpy.da.UpdateCursor(source, fields, where_clause=where) as cur:
                    for row in cur:
                        found = True
                        for i, p in enumerate(props, start=1):
                            row[i] = p.value
                        cur.updateRow(row)
                if not found:
                    raise eng.ToolError(f"{idf} = {props[0].id!r} disappeared before it could be edited")
                changes.extend(props)
            if editor:
                editor.stopOperation()
                editor.stopEditing(True)
        except Exception as exc:
            if editor:
                try:
                    editor.abortOperation()
                    editor.stopEditing(False)
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(exc, eng.ToolError):
                raise
            raise eng.ToolError(f"Edit failed ({type(exc).__name__}: {exc}). The layer may be locked "
                                "by an open Pro edit session or a join; close it and retry.") from exc
        for p in changes:
            p.applied = True
            line = f"APPLIED {p.table} {p.id}.{p.field}: {p.old!r} -> {p.value!r}"
            self.apply_log.append(line)
            self.emit(line)
        return len(changes)

    # -------------------------------------------------------------- registry
    def handlers(self) -> dict:
        return {"list_layers": self.list_layers, "get_selected_features": self.get_selected_features,
                "query_features": self.query_features, "get_feature": self.get_feature,
                "propose_edits": self.propose_edits}


def proposal_table(proposals) -> list:
    """Plain-text table lines for the geoprocessing messages."""
    rows = [("Layer", "Feature", "Field", "Current", "Proposed")]
    for p in proposals:
        rows.append((p.table, str(p.id), p.field, "NULL" if p.old is None else str(p.old),
                     "NULL" if p.value is None else str(p.value)))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    lines = [fmt(rows[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows[1:]]
    return lines
