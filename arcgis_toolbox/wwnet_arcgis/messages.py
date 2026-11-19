"""Message sink: routes to arcpy.AddMessage/AddWarning/AddError (or a list in tests)."""
from __future__ import annotations


def arcpy_mod():
    import arcpy  # noqa: PLC0415 - lazy: only available inside ArcGIS Pro
    return arcpy


class Messages:
    def __init__(self, arcpy=None):
        self._arcpy = arcpy

    @property
    def arcpy(self):
        if self._arcpy is None:
            self._arcpy = arcpy_mod()
        return self._arcpy

    def info(self, text: str):
        self.arcpy.AddMessage(str(text))

    def warn(self, text: str):
        self.arcpy.AddWarning(str(text))

    def error(self, text: str):
        self.arcpy.AddError(str(text))


class ChunkedWriter:
    """Buffers streamed text and emits it as messages at line ends or every `chunk` chars."""

    def __init__(self, emit, chunk: int = 240):
        self.emit, self.chunk, self.buf = emit, chunk, ""

    def write(self, text: str):
        self.buf += text
        while True:
            nl = self.buf.find("\n")
            if nl >= 0:
                line, self.buf = self.buf[:nl], self.buf[nl + 1:]
                if line.strip():
                    self.emit(line)
                continue
            if len(self.buf) >= self.chunk:
                cut = self.buf.rfind(" ", 0, self.chunk)
                cut = cut if cut > 0 else self.chunk
                self.emit(self.buf[:cut])
                self.buf = self.buf[cut:].lstrip(" ")
                continue
            break

    def flush(self):
        if self.buf.strip():
            self.emit(self.buf.strip())
        self.buf = ""
