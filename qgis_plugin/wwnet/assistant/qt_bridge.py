"""Qt glue: run the engine on a QThread and execute tools on the GUI thread."""
from __future__ import annotations

import threading

from qgis.PyQt.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from .engine import ToolError


class _Request:
    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.result = None
        self.exc = None
        self.done = threading.Event()


class MainThreadInvoker(QObject):
    """Create this on the GUI thread. `call()` may be used from any thread."""

    _call = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._call.connect(self._run, Qt.QueuedConnection)

    @pyqtSlot(object)
    def _run(self, req):
        try:
            req.result = req.fn(*req.args)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            req.exc = exc
        finally:
            req.done.set()

    def call(self, fn, *args, stop=None):
        if QThread.currentThread() is self.thread():
            return fn(*args)
        req = _Request(fn, args)
        self._call.emit(req)
        while not req.done.wait(0.2):
            if stop is not None and stop():
                raise ToolError("Cancelled by user")
        if req.exc is not None:
            raise req.exc
        return req.result


class TurnWorker(QObject):
    """Runs Engine.send(text) in its own thread; forwards callbacks as signals."""

    text = pyqtSignal(str)
    event = pyqtSignal(object)
    done = pyqtSignal(object)

    def __init__(self, engine, user_text, stop_flag: threading.Event):
        super().__init__()
        self.engine, self.user_text, self.stop_flag = engine, user_text, stop_flag
        engine.on_text = self.text.emit
        engine.on_event = self.event.emit
        engine.should_stop = self.stop_flag.is_set

    @pyqtSlot()
    def run(self):
        try:
            res = self.engine.send(self.user_text)
        except BaseException as exc:  # noqa: BLE001
            from .engine import TurnResult
            res = TurnResult(stop_reason="error", error=f"{type(exc).__name__}: {exc}")
        self.done.emit(res)


class FunctionThread(QThread):
    """Run fn() off the GUI thread; `finished_with` is emitted with its result."""

    finished_with = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.finished_with.emit(self._fn())
        except BaseException as exc:  # noqa: BLE001
            self.finished_with.emit(exc)


def run_in_thread(owner, fn, callback):
    """Start fn in a thread; callback(result_or_exception) runs on the GUI thread."""
    th = FunctionThread(fn, owner)
    owner.__dict__.setdefault("_wwnet_threads", []).append(th)

    def _done(res):
        try:
            owner._wwnet_threads.remove(th)
        except ValueError:
            pass
        callback(res)

    th.finished_with.connect(_done)
    th.start()
    return th
