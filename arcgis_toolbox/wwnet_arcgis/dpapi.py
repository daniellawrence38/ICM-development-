"""API key storage with Windows DPAPI (current-user scope), via ctypes.

The key is never logged or returned in messages. Reading order:
ANTHROPIC_API_KEY environment variable, then the DPAPI-protected file
%APPDATA%\\WWNet\\anthropic_key.bin (only decryptable by the same Windows user).
"""
from __future__ import annotations

import ctypes
import os

from . import settings

CRYPTPROTECT_UI_FORBIDDEN = 0x1
KEY_FILE = "anthropic_key.bin"


class DpapiError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _windll():
    w = getattr(ctypes, "windll", None)
    if w is None:
        raise DpapiError("Windows DPAPI is only available on Windows.")
    return w


def _prototypes(w):
    """64-bit safe signatures (pointers must not be truncated to int)."""
    P = ctypes.POINTER(_Blob)
    w.crypt32.CryptProtectData.argtypes = [P, ctypes.c_wchar_p, P, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_uint32, P]
    w.crypt32.CryptUnprotectData.argtypes = [P, ctypes.c_void_p, P, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_uint32, P]
    w.crypt32.CryptProtectData.restype = ctypes.c_int
    w.crypt32.CryptUnprotectData.restype = ctypes.c_int
    w.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    w.kernel32.LocalFree.restype = ctypes.c_void_p


def _in_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p)), buf


def _out_bytes(blob, kernel32) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            kernel32.LocalFree(blob.pbData)


def protect(data: bytes) -> bytes:
    w = _windll()
    _prototypes(w)
    crypt32, kernel32 = w.crypt32, w.kernel32
    src, keep = _in_blob(data)
    out = _Blob()
    ok = crypt32.CryptProtectData(ctypes.byref(src), "WWNet", None, None, None,
                                  CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    del keep
    if not ok:
        raise DpapiError("CryptProtectData failed")
    return _out_bytes(out, kernel32)


def unprotect(data: bytes) -> bytes:
    w = _windll()
    _prototypes(w)
    crypt32, kernel32 = w.crypt32, w.kernel32
    src, keep = _in_blob(data)
    out = _Blob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    del keep
    if not ok:
        raise DpapiError("CryptUnprotectData failed (file written by another Windows user?)")
    return _out_bytes(out, kernel32)


def key_path():
    return settings.appdata_dir() / KEY_FILE


def save_key(key: str):
    key = (key or "").strip()
    if not key:
        raise DpapiError("Empty API key")
    settings._atomic_write(key_path(), protect(key.encode("utf-8")))


def clear_key() -> bool:
    try:
        key_path().unlink()
        return True
    except FileNotFoundError:
        return False


def stored_key():
    p = key_path()
    if not p.is_file():
        return None
    try:
        return unprotect(p.read_bytes()).decode("utf-8").strip() or None
    except DpapiError:
        return None


def get_api_key():
    """Returns (key, source) with source in {'env', 'dpapi', None}."""
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env, "env"
    k = stored_key()
    return (k, "dpapi") if k else (None, None)
