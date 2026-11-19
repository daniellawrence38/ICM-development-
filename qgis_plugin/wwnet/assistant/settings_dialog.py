"""AI settings dialog: API key, model, effort, test connection, install anthropic."""
from __future__ import annotations

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                                 QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from . import client, install, secrets
from .qt_bridge import run_in_thread

MODEL_KEY, EFFORT_KEY = "wwnet/ai/model", "wwnet/ai/effort"


def current_settings() -> dict:
    s = QgsSettings()
    model = s.value(MODEL_KEY, client.DEFAULT_MODEL)
    effort = s.value(EFFORT_KEY, client.DEFAULT_EFFORT)
    if model not in dict(client.MODELS):
        model = client.DEFAULT_MODEL
    if effort not in client.EFFORTS:
        effort = client.DEFAULT_EFFORT
    return {"model": model, "effort": effort}


class SettingsDialog(QDialog):
    def __init__(self, parent=None, message_bar=None):
        super().__init__(parent)
        self.setWindowTitle("WWNet AI assistant settings")
        self.setMinimumWidth(480)
        self._bar = message_bar
        cur = current_settings()
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("stored key is hidden; type to replace" if secrets.has_stored_key()
                                    else "sk-ant-...")
        form.addRow("Anthropic API key", self.key)
        self.model = QComboBox()
        for mid, label in client.MODELS:
            self.model.addItem(f"{label}  [{mid}]", mid)
        self.model.setCurrentIndex(max(0, self.model.findData(cur["model"])))
        form.addRow("Model", self.model)
        self.effort = QComboBox()
        for e in client.EFFORTS:
            self.effort.addItem(e, e)
        self.effort.setCurrentIndex(max(0, self.effort.findData(cur["effort"])))
        form.addRow("Effort", self.effort)
        lay.addLayout(form)

        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        lay.addWidget(self.test_btn)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.install_btn = QPushButton("Install the 'anthropic' Python package (pip --user)")
        self.install_btn.clicked.connect(self._install)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.hide()
        lay.addWidget(self.install_btn)
        lay.addWidget(self.log)
        self._refresh_install_state()

        priv = QLabel("Privacy: when the assistant uses its tools, attributes of the features it "
                      "reads (and your chat messages) are sent to Anthropic's API.")
        priv.setWordWrap(True)
        lay.addWidget(priv)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _refresh_install_state(self):
        ok = client.anthropic_available()
        self.install_btn.setVisible(not ok)
        self.test_btn.setEnabled(ok)
        if not ok:
            self.status.setText("The 'anthropic' package is not installed in QGIS's Python. "
                                "Click the button below (needs internet access); QGIS need not restart.")

    def _key_for_test(self) -> str:
        return self.key.text().strip() or secrets.load_api_key()

    def _test(self):
        key, model = self._key_for_test(), self.model.currentData()
        self.test_btn.setEnabled(False)
        self.status.setText("Testing...")

        def done(res):
            self.test_btn.setEnabled(True)
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            self.status.setText(("OK: " if ok else "FAILED: ") + msg)

        run_in_thread(self, lambda: client.test_connection(key, model), done)

    def _install(self):
        self.install_btn.setEnabled(False)
        self.log.show()
        try:
            cmd = " ".join(install.pip_command())
        except RuntimeError as exc:
            cmd = str(exc)
        self.log.setPlainText(f"Running: {cmd}\n(this can take a minute)")

        def done(res):
            self.install_btn.setEnabled(True)
            ok, out = res if isinstance(res, tuple) else (False, str(res))
            self.log.appendPlainText(out)
            self.status.setText("Installed 'anthropic'." if ok else
                                "Install failed. You can run the command above in an OSGeo4W/QGIS "
                                "shell manually.")
            self._refresh_install_state()

        run_in_thread(self, install.install_anthropic, done)

    def _save(self):
        s = QgsSettings()
        s.setValue(MODEL_KEY, self.model.currentData())
        s.setValue(EFFORT_KEY, self.effort.currentData())
        key = self.key.text().strip()
        if key:
            where, warn = secrets.save_api_key(key)
            if warn:
                QMessageBox.warning(self, "API key stored unencrypted", warn)
        self.accept()
