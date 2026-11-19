"""Chat dock for the Claude-powered assistant."""
from __future__ import annotations

import threading

from qgis.PyQt.QtCore import QEvent, QObject, Qt, QThread
from qgis.PyQt.QtGui import QColor, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                                 QTextBrowser, QVBoxLayout, QWidget)

from . import client, prompt, tools
from .engine import Engine
from .qgis_tools import QgisTools
from .qt_bridge import MainThreadInvoker, TurnWorker
from .settings_dialog import SettingsDialog, current_settings
from . import secrets


class _CtrlEnter(QObject):
    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.cb = callback

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and ev.modifiers() & Qt.ControlModifier:
            self.cb()
            return True
        return False


class AssistantDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("WWNet AI assistant", parent)
        self.setObjectName("WWNetAssistantDock")
        self.iface = iface
        self.engine = None
        self._engine_sig = None
        self._thread = None
        self._worker = None
        self._stop = threading.Event()
        self._need_break = False
        self.invoker = MainThreadInvoker(self)
        self.qtools = QgisTools(iface, iface.mainWindow())

        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.model_lbl = QLabel("")
        top.addWidget(self.model_lbl, 1)
        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.clicked.connect(self.open_settings)
        self.clear_btn = QPushButton("New chat")
        self.clear_btn.clicked.connect(self.clear_chat)
        top.addWidget(self.settings_btn)
        top.addWidget(self.clear_btn)
        lay.addLayout(top)
        priv = QLabel("Tool use sends feature attributes to Anthropic's API.")
        priv.setStyleSheet("color: gray; font-size: 9pt;")
        lay.addWidget(priv)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Ask about the model or DS 50...  (Ctrl+Enter to send)")
        self.input.setFixedHeight(70)
        self._filter = _CtrlEnter(self.send, self)
        self.input.installEventFilter(self._filter)
        lay.addWidget(self.input)
        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.stop_btn)
        row.addWidget(self.send_btn)
        lay.addLayout(row)
        self.setWidget(w)
        self._refresh_header()

    # ----------------------------------------------------------- transcript
    def _append(self, text, color=None, bold=False, italic=False, newline_before=True):
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        fmt.setFontWeight(75 if bold else 50)
        fmt.setFontItalic(italic)
        if newline_before and not self.view.document().isEmpty():
            cur.insertBlock()
        cur.insertText(text, fmt)
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    def _on_text(self, delta):
        if self._need_break:
            self._append("", newline_before=True)
            self._need_break = False
            cur = self.view.textCursor()
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        cur.insertText(delta, fmt)
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

    def _on_event(self, ev):
        t = ev.get("type")
        if t == "turn_start":
            self._need_break = True
            self.status.setText("Thinking...")
        elif t == "tool_call":
            self._append(f"tool: {ev['name']}({_short(ev.get('input'))})", "#777777", italic=True)
            self._need_break = True
            self.status.setText(f"Running {ev['name']}...")
        elif t == "tool_result":
            col = "#b00020" if ev.get("is_error") else "#777777"
            self._append(f"  -> {'error: ' if ev.get('is_error') else ''}{ev.get('preview', '')[:160]}",
                         col, italic=True)
        elif t == "notice":
            col = {"error": "#b00020", "warning": "#b36b00"}.get(ev.get("level"), "#555555")
            self._append(ev.get("text", ""), col, italic=True)
            self._need_break = True

    # --------------------------------------------------------------- engine
    def _refresh_header(self):
        s = current_settings()
        self.model_lbl.setText(f"{s['model']} / effort {s['effort']}")

    def _ensure_engine(self):
        if not client.anthropic_available():
            self._append("The 'anthropic' package is missing. Open Settings to install it.", "#b00020")
            self.open_settings()
            if not client.anthropic_available():
                return False
        key = secrets.load_api_key()
        if not key:
            self._append("No API key set. Open Settings to add one.", "#b00020")
            self.open_settings()
            key = secrets.load_api_key()
            if not key:
                return False
        s = current_settings()
        sig = (s["model"], s["effort"], bool(key), hash(key))
        if self.engine is None or sig != self._engine_sig:
            had_history = self.engine is not None and self.engine.messages
            handlers = dict(tools.pure_handlers())
            handlers.update(self.qtools.handlers(self.invoker, self._stop.is_set))
            self.engine = Engine(client.make_client(key), s["model"], prompt.build_system_blocks(),
                                 tools.tool_definitions(), handlers, effort=s["effort"])
            self._engine_sig = sig
            if had_history:
                self._append("Settings changed: started a new conversation.", "#555555", italic=True)
        return True

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self._thread is not None:
            return
        if not self._ensure_engine():
            return
        self.input.clear()
        self._append("You: " + text, "#1f4e9c", bold=True)
        self._append("Assistant:", "#2e7d32", bold=True)
        self._need_break = False
        self._stop.clear()
        self._set_busy(True)

        self._thread = QThread(self)
        self._worker = TurnWorker(self.engine, text, self._stop)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.text.connect(self._on_text)
        self._worker.event.connect(self._on_event)
        self._worker.done.connect(self._on_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_done(self, res):
        if res.stop_reason == "cancelled":
            self._append("[stopped]", "#555555", italic=True)
        elif res.stop_reason == "error":
            self._append(res.error or "Error", "#b00020")
        self.status.setText(f"{res.tool_calls} tool call(s)" if res.tool_calls else "")

    def _cleanup_thread(self):
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = self._thread = None
        self._set_busy(False)

    def _set_busy(self, busy):
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.clear_btn.setEnabled(not busy)
        self.settings_btn.setEnabled(not busy)

    def stop(self):
        self._stop.set()
        self.status.setText("Stopping...")

    def clear_chat(self):
        self.view.clear()
        if self.engine is not None:
            self.engine.reset()

    def open_settings(self):
        SettingsDialog(self.iface.mainWindow(), self.iface.messageBar()).exec()
        self._refresh_header()

    def closeEvent(self, ev):
        self._stop.set()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(ev)


def _short(d, n=80):
    s = ", ".join(f"{k}={v!r}" for k, v in (d or {}).items())
    return s if len(s) <= n else s[: n - 3] + "..."
