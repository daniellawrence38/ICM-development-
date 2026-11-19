"""WWNet main plugin class: toolbar, menu, actions."""
from __future__ import annotations

import os

from qgis.core import Qgis
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox

from .core import schema
from . import layers

MENU = "&WWNet"


class WWNetPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.toolbar = None
        self.check_dock = None
        self.assistant_dock = None

    # ---------------------------------------------------------------- gui
    def _action(self, text, slot, tip=None, checkable=False):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        a = QAction(icon, text, self.iface.mainWindow())
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(slot)
        self.iface.addPluginToMenu(MENU, a)
        self.actions.append(a)
        return a

    def initGui(self):  # noqa: N802
        self.toolbar = self.iface.addToolBar("WWNet")
        self.toolbar.setObjectName("WWNetToolbar")
        new = self._action("New model...", self.new_model, "Create an empty WWNet GeoPackage")
        opn = self._action("Open model...", self.open_model, "Load wwn_* layers from a GeoPackage")
        chk = self._action("Check model", self.check_model, "Run `wwnet check` on the loaded model")
        self.ai_action = self._action("AI assistant", self.toggle_assistant,
                                      "Claude-powered DS 50 design assistant", checkable=True)
        st = self._action("AI settings...", self.open_ai_settings)
        for a in (new, opn, chk, self.ai_action):
            self.toolbar.addAction(a)
        self.actions_by_name = {"settings": st}

    def unload(self):
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
        self.actions = []
        if self.toolbar is not None:
            del self.toolbar
            self.toolbar = None
        for d in (self.check_dock, self.assistant_dock):
            if d is not None:
                self.iface.removeDockWidget(d)
                d.deleteLater()
        self.check_dock = self.assistant_dock = None

    # ------------------------------------------------------------ actions
    def _bar(self, level, msg):
        self.iface.messageBar().pushMessage("WWNet", msg, level=level, duration=8)

    def new_model(self):
        path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), "New WWNet model",
                                              "", "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        try:
            schema.create_model(path, overwrite=True)  # the save dialog already confirmed overwrite
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.iface.mainWindow(), "WWNet", f"Could not create model:\n{exc}")
            return
        layers.load_model(path, self.iface)
        self._bar(Qgis.Success, f"Created {os.path.basename(path)} (EPSG:7850).")

    def open_model(self):
        path, _ = QFileDialog.getOpenFileName(self.iface.mainWindow(), "Open WWNet model",
                                              "", "GeoPackage (*.gpkg)")
        if not path:
            return
        try:
            present = schema.table_identifiers(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self.iface.mainWindow(), "WWNet", f"Not a readable GeoPackage:\n{exc}")
            return
        if not any(t in present for t in schema.ALL_TABLES):
            QMessageBox.warning(self.iface.mainWindow(), "WWNet",
                                "This GeoPackage has no wwn_* tables.")
            return
        loaded = layers.load_model(path, self.iface)
        self._bar(Qgis.Info, f"Loaded {len(loaded)} layer(s) from {os.path.basename(path)}.")

    def current_gpkg(self):
        act = self.iface.activeLayer()
        if act is not None and act.customProperty(layers.TABLE_PROP):
            return layers.gpkg_of(act)
        ls = layers.wwnet_layers()
        return layers.gpkg_of(ls[0]) if ls else None

    def check_model(self):
        gpkg = self.current_gpkg()
        if not gpkg:
            self._bar(Qgis.Warning, "No WWNet model loaded. Use New model or Open model first.")
            return
        if any(l.isModified() for l in layers.wwnet_layers()):
            QMessageBox.information(self.iface.mainWindow(), "WWNet",
                                    "Some WWNet layers have unsaved edits. Save them first so the "
                                    "check sees your changes.")
            return
        if self.check_dock is None:
            from .check_dock import CheckDock

            self.check_dock = CheckDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.check_dock)
        self.check_dock.show()
        self.check_dock.run(gpkg)

    def toggle_assistant(self, checked=None):
        if self.assistant_dock is None:
            from .assistant.dock import AssistantDock

            self.assistant_dock = AssistantDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.assistant_dock)
            self.assistant_dock.visibilityChanged.connect(self.ai_action.setChecked)
            return
        self.assistant_dock.setVisible(not self.assistant_dock.isVisible())

    def open_ai_settings(self):
        from .assistant.settings_dialog import SettingsDialog

        SettingsDialog(self.iface.mainWindow(), self.iface.messageBar()).exec()
