"""Dock showing `wwnet check` issues; clicking a row selects and zooms to the feature."""
from __future__ import annotations

from qgis.core import QgsApplication, QgsExpression, QgsTask
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QDockWidget, QHBoxLayout, QLabel,
                                 QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .core import checks, schema
from .layers import layer_for_table

COLUMNS = ("Severity", "Code", "Object", "Clause", "Message")
SEV_COLOUR = {"error": "#c62828", "fail": "#c62828", "warning": "#ef6c00", "warn": "#ef6c00"}


class CheckDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("WWNet model check", parent)
        self.setObjectName("WWNetCheckDock")
        self.iface = iface
        self.issues = []
        self._task = None
        self._gpkg = None
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.status = QLabel("No check run yet.")
        row.addWidget(self.status, 1)
        self.rerun = QPushButton("Re-run")
        self.rerun.clicked.connect(lambda: self.run(self._gpkg))
        row.addWidget(self.rerun)
        lay.addLayout(row)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._navigate)
        lay.addWidget(self.table)
        self.setWidget(w)

    def run(self, gpkg):
        if not gpkg:
            return
        self._gpkg = gpkg
        self.status.setText("Running wwnet check...")
        self.rerun.setEnabled(False)

        def work(task):
            return checks.run_check(gpkg)

        def finished(exc, result=None):
            self.rerun.setEnabled(True)
            if exc is not None or result is None:
                self.status.setText(f"Check failed: {exc}")
                return
            self.set_issues(*result)

        self._task = QgsTask.fromFunction("WWNet check", work, on_finished=finished)
        QgsApplication.taskManager().addTask(self._task)

    def set_issues(self, issues, message):
        self.issues = issues
        self.status.setText(message)
        self.table.setRowCount(len(issues))
        for i, it in enumerate(issues):
            vals = (it["severity"], it["code"], f"{it['object_type']} {it['object_id']}".strip(),
                    it["clause"], it["message"])
            for j, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                col = SEV_COLOUR.get(it["severity"])
                if col and j == 0:
                    item.setForeground(QColor(col))
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()

    def _navigate(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.issues):
            return
        it = self.issues[rows[0].row()]
        table, oid = it.get("table"), it.get("object_id")
        layer = layer_for_table(table) if table else None
        idf = schema.ID_FIELDS.get(table or "")
        if layer is None or not idf or oid in ("", None):
            self.iface.messageBar().pushInfo("WWNet", "This issue is not linked to a loaded feature.")
            return
        expr = f"{QgsExpression.quotedColumnRef(idf)} = {QgsExpression.quotedString(str(oid))}"
        layer.selectByExpression(expr)
        if layer.selectedFeatureCount():
            if layer.isSpatial():
                self.iface.mapCanvas().zoomToSelected(layer)
                self.iface.mapCanvas().flashFeatureIds(layer, layer.selectedFeatureIds())
        else:
            self.iface.messageBar().pushWarning("WWNet", f"{idf} {oid!r} not found in {layer.name()}.")
