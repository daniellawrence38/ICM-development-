"""QGIS-side tool implementations. ALL methods must run on the GUI thread
(they are dispatched there by MainThreadInvoker)."""
from __future__ import annotations

from qgis.core import (NULL, QgsExpression, QgsFeatureRequest, QgsProject, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.PyQt.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QLabel,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core import schema
from . import tools as pure_tools
from .engine import ToolError

TABLE_PROP = "wwnet/table"
MAX_ROWS = 200


def table_of(layer) -> str | None:
    t = layer.customProperty(TABLE_PROP)
    if t:
        return str(t)
    src = layer.source()
    if "layername=" in src:
        name = src.split("layername=")[1].split("|")[0]
        if name.startswith("wwn_"):
            return name
    return None


def vector_layers():
    return [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]


def find_layer(name: str):
    n = (name or "").strip().lower()
    layers = vector_layers()
    for l in layers:
        if (table_of(l) or "").lower() == n:
            return l
    for l in layers:
        if l.name().lower() == n or l.id().lower() == n:
            return l
    names = [table_of(l) or l.name() for l in layers]
    raise ToolError(f"Layer {name!r} not found. Available: {names}")


def py_value(v):
    if v is None or v == NULL:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def feature_dict(feat, fields=None) -> dict:
    names = [f.name() for f in (fields or feat.fields())]
    return {n: py_value(feat[n]) for n in names}


class QgisTools:
    def __init__(self, iface=None, parent_widget=None):
        self.iface = iface
        self.parent_widget = parent_widget

    # ------------------------------------------------------------ read tools
    def list_layers(self, args):
        out = []
        for l in vector_layers():
            tbl = table_of(l)
            out.append({
                "layer_name": l.name(), "wwnet_table": tbl,
                "geometry": QgsWkbTypes.displayString(l.wkbType()) if l.isSpatial() else "none",
                "feature_count": l.featureCount(), "selected": l.selectedFeatureCount(),
                "editable": l.isEditable(),
                "id_field": schema.ID_FIELDS.get(tbl or ""),
                "fields": [f.name() for f in l.fields()],
            })
        return out

    def get_selected_features(self, args):
        l = find_layer(args["layer"])
        feats = l.selectedFeatures()
        return {"layer": l.name(), "selected_count": len(feats),
                "features": [feature_dict(f) for f in feats[:100]],
                "truncated": len(feats) > 100}

    def query_features(self, args):
        l = find_layer(args["layer"])
        expr = QgsExpression(args["expression"])
        if expr.hasParserError():
            raise ToolError(f"Bad expression: {expr.parserErrorString()}")
        limit = max(1, min(int(args.get("limit") or 50), MAX_ROWS))
        req = QgsFeatureRequest().setFilterExpression(args["expression"]).setLimit(limit)
        try:
            req.setFlags(QgsFeatureRequest.NoGeometry)
        except Exception:
            pass
        rows = [feature_dict(f, l.fields()) for f in l.getFeatures(req)]
        return {"layer": l.name(), "returned": len(rows), "limit": limit, "features": rows}

    def _by_id(self, layer, id_value):
        tbl = table_of(layer)
        idf = schema.ID_FIELDS.get(tbl or "")
        if not idf or layer.fields().indexFromName(idf) < 0:
            raise ToolError(f"Layer {layer.name()!r} has no known business id field")
        req = QgsFeatureRequest().setFilterExpression(
            f"{QgsExpression.quotedColumnRef(idf)} = {QgsExpression.quotedString(str(id_value))}")
        return idf, list(layer.getFeatures(req))

    def get_feature(self, args):
        l = find_layer(args["layer"])
        idf, feats = self._by_id(l, args["id_value"])
        if not feats:
            raise ToolError(f"No feature with {idf} = {args['id_value']!r} in {l.name()}")
        f = feats[0]
        d = feature_dict(f)
        if table_of(l) == "wwn_conduits":
            length = d.get("length_m") or (f.geometry().length() if f.hasGeometry() else None)
            us, ds = d.get("us_invert"), d.get("ds_invert")
            derived = {"length_m_used": length}
            if length and us is not None and ds is not None and us > ds:
                derived["grade_1in"] = round(length / (us - ds), 1)
            d["derived"] = derived
        return d

    # ------------------------------------------------------------ edit tool
    def propose_edits(self, args):
        l = find_layer(args["layer"])
        tbl = table_of(l) or l.name()
        if not l.isSpatial() and tbl not in schema.ALL_TABLES:
            raise ToolError("Unsupported layer")
        types = {}
        for fld in l.fields():
            tn = fld.typeName().lower()
            types[fld.name()] = "int" if "int" in tn else ("real" if ("real" in tn or "double" in tn)
                                                          else "text")
        valid, problems = pure_tools.validate_edits(tbl, args.get("edits") or [], types)
        resolved = []
        for e in valid:
            idf, feats = self._by_id(l, e["id"])
            if not feats:
                problems.append(f"no feature with {idf} = {e['id']!r}")
                continue
            f = feats[0]
            resolved.append({**e, "fid": f.id(), "old": py_value(f[e["field"]])})
        if problems:
            raise ToolError("Nothing applied. Problems: " + "; ".join(problems))
        if not resolved:
            raise ToolError("No edits supplied")

        dlg = ConfirmEditsDialog(l.name(), args.get("reason", ""), resolved, self.parent_widget)
        l.selectByIds([r["fid"] for r in resolved])
        if dlg.exec() != QDialog.Accepted:
            return {"applied": False, "outcome": "rejected_by_user",
                    "message": "The engineer rejected these edits. Nothing was changed."}

        was_editing = l.isEditable()
        if not was_editing and not l.startEditing():
            raise ToolError("Could not start an edit session on this layer (read-only?)")
        errors = []
        for r in resolved:
            idx = l.fields().indexFromName(r["field"])
            val = NULL if r["value"] is None else r["value"]
            if not l.changeAttributeValue(r["fid"], idx, val):
                errors.append(f"{r['id']}.{r['field']}")
        if errors:
            msg = "Edit failed for " + ", ".join(errors)
            if not was_editing:
                l.rollBack()
                msg += "; rolled back."
            raise ToolError(msg)
        if was_editing:
            return {"applied": True, "outcome": "applied_to_open_edit_buffer", "count": len(resolved),
                    "message": "Applied to the layer's open edit session; the engineer must save edits."}
        if not l.commitChanges():
            msg = "; ".join(l.commitErrors())
            l.rollBack()
            raise ToolError(f"Commit failed, rolled back: {msg}")
        l.triggerRepaint()
        return {"applied": True, "outcome": "applied_and_saved", "count": len(resolved)}

    # ------------------------------------------------------------- registry
    def handlers(self, invoker, stop=None) -> dict:
        def wrap(fn, interruptible=True):
            return lambda a: invoker.call(fn, a, stop=stop if interruptible else None)

        return {
            "list_layers": wrap(self.list_layers),
            "get_selected_features": wrap(self.get_selected_features),
            "query_features": wrap(self.query_features),
            "get_feature": wrap(self.get_feature),
            # waits as long as the engineer takes to decide
            "propose_edits": wrap(self.propose_edits, interruptible=False),
        }


class ConfirmEditsDialog(QDialog):
    def __init__(self, layer_name, reason, edits, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WWNet assistant: confirm proposed edits")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>The AI assistant proposes {len(edits)} edit(s) to "
                             f"layer '{layer_name}'.</b>"))
        r = QLabel(f"Reason: {reason or '(none given)'}")
        r.setWordWrap(True)
        lay.addWidget(r)
        t = QTableWidget(len(edits), 4)
        t.setHorizontalHeaderLabels(["Feature", "Field", "Current", "Proposed"])
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, e in enumerate(edits):
            for j, v in enumerate((e["id"], e["field"], e["old"], e["value"])):
                t.setItem(i, j, QTableWidgetItem("NULL" if v is None else str(v)))
        t.horizontalHeader().setStretchLastSection(True)
        t.resizeColumnsToContents()
        lay.addWidget(t)
        lay.addWidget(QLabel("Edits are applied in an edit session and saved only if you approve. "
                             "Affected features are selected on the map."))
        bb = QDialogButtonBox()
        ok = bb.addButton("Apply edits", QDialogButtonBox.AcceptRole)
        no = bb.addButton("Reject", QDialogButtonBox.RejectRole)
        no.setDefault(True)
        ok.setAutoDefault(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
