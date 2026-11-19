"""Minimal TOML reader for Python < 3.11 (subset sufficient for the rulesets).

Supports: comments, [tables], [[array tables]], dotted keys, basic/literal
strings, ints, floats, bools, (nested, multi-line) arrays and inline tables.
`loads()` uses stdlib tomllib when available.
"""
from __future__ import annotations

import re

try:  # Python 3.11+
    import tomllib as _tomllib
except Exception:  # pragma: no cover - exercised on old Pythons only
    _tomllib = None


class TomlError(ValueError):
    pass


_NUM = re.compile(r"[+-]?\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?")
_BARE = re.compile(r"[A-Za-z0-9_-]+")
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class _Parser:
    def __init__(self, text: str):
        self.t = text
        self.i = 0
        self.n = len(text)

    def err(self, msg):
        line = self.t.count("\n", 0, self.i) + 1
        raise TomlError(f"line {line}: {msg}")

    def ws(self):
        while self.i < self.n and self.t[self.i] in " \t":
            self.i += 1

    def skip_blank(self):
        while self.i < self.n:
            c = self.t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#":
                while self.i < self.n and self.t[self.i] != "\n":
                    self.i += 1
            else:
                break

    def expect(self, s):
        if not self.t.startswith(s, self.i):
            self.err(f"expected {s!r}")
        self.i += len(s)

    def end_of_line(self):
        self.ws()
        if self.i < self.n and self.t[self.i] == "#":
            while self.i < self.n and self.t[self.i] != "\n":
                self.i += 1
        if self.i < self.n and self.t[self.i] == "\r":
            self.i += 1
        if self.i < self.n:
            if self.t[self.i] != "\n":
                self.err("unexpected characters after value")
            self.i += 1

    def keys(self):
        out = []
        while True:
            self.ws()
            if self.i < self.n and self.t[self.i] in "\"'":
                out.append(self.string())
            else:
                m = _BARE.match(self.t, self.i)
                if not m:
                    self.err("bad key")
                out.append(m.group())
                self.i = m.end()
            self.ws()
            if self.i < self.n and self.t[self.i] == ".":
                self.i += 1
                continue
            return out

    def string(self):
        q = self.t[self.i]
        self.i += 1
        buf = []
        while self.i < self.n:
            c = self.t[self.i]
            if c == q:
                self.i += 1
                return "".join(buf)
            if c == "\n":
                self.err("newline in string")
            if q == '"' and c == "\\":
                self.i += 1
                e = self.t[self.i]
                if e in _ESC:
                    buf.append(_ESC[e])
                elif e in "uU":
                    ln = 4 if e == "u" else 8
                    buf.append(chr(int(self.t[self.i + 1 : self.i + 1 + ln], 16)))
                    self.i += ln
                else:
                    self.err("bad escape")
                self.i += 1
                continue
            buf.append(c)
            self.i += 1
        self.err("unterminated string")

    def value(self):
        if self.i >= self.n:
            self.err("missing value")
        c = self.t[self.i]
        if c in "\"'":
            return self.string()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.skip_blank()
                if self.t[self.i] == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.skip_blank()
                if self.t[self.i] == ",":
                    self.i += 1
        if c == "{":
            self.i += 1
            tbl = {}
            while True:
                self.ws()
                if self.t[self.i] == "}":
                    self.i += 1
                    return tbl
                ks = self.keys()
                self.ws()
                self.expect("=")
                self.ws()
                self._walk(tbl, ks[:-1])[ks[-1]] = self.value()
                self.ws()
                if self.t[self.i] == ",":
                    self.i += 1
        if self.t.startswith("true", self.i):
            self.i += 4
            return True
        if self.t.startswith("false", self.i):
            self.i += 5
            return False
        m = _NUM.match(self.t, self.i)
        if not m:
            self.err("bad value")
        self.i = m.end()
        s = m.group().replace("_", "")
        return float(s) if any(ch in s for ch in ".eE") else int(s)

    @staticmethod
    def _walk(d, keys):
        for k in keys:
            nxt = d.get(k)
            if nxt is None:
                nxt = d[k] = {}
            elif isinstance(nxt, list):
                nxt = nxt[-1]
            d = nxt
        return d

    def parse(self):
        root, cur = {}, None
        cur = root
        while True:
            self.skip_blank()
            if self.i >= self.n:
                return root
            if self.t[self.i] == "[":
                if self.t.startswith("[[", self.i):
                    self.i += 2
                    ks = self.keys()
                    self.expect("]]")
                    parent = self._walk(root, ks[:-1])
                    cur = {}
                    parent.setdefault(ks[-1], []).append(cur)
                else:
                    self.i += 1
                    ks = self.keys()
                    self.expect("]")
                    cur = self._walk(root, ks)
            else:
                ks = self.keys()
                self.ws()
                self.expect("=")
                self.ws()
                self._walk(cur, ks[:-1])[ks[-1]] = self.value()
            self.end_of_line()


def loads(text: str) -> dict:
    if _tomllib is not None:
        return _tomllib.loads(text)
    return _Parser(text).parse()


def loads_fallback(text: str) -> dict:
    """Always use the vendored parser (for testing)."""
    return _Parser(text).parse()
