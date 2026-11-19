"""Per-user settings and file locations (%APPDATA%\\WWNet on Windows)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULTS = {"model": "claude-opus-5-5", "effort": "medium", "wwnet_exe": ""}


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    p = Path(base) / "WWNet"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return appdata_dir() / "settings.json"


def load() -> dict:
    out = dict(DEFAULTS)
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if k in DEFAULTS and isinstance(v, str)})
    except (OSError, ValueError):
        pass
    return out


def save(**changes) -> dict:
    cur = load()
    cur.update({k: v for k, v in changes.items() if k in DEFAULTS and v is not None})
    _atomic_write(settings_path(), json.dumps(cur, indent=2).encode("utf-8"))
    return cur


def _atomic_write(path: Path, data: bytes):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def history_path(map_key: str) -> Path:
    h = hashlib.sha1(map_key.encode("utf-8")).hexdigest()[:16]
    d = appdata_dir() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.json"
