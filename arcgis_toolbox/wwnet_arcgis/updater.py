"""In-place updates of an installed WWNet toolbox without restarting ArcGIS Pro and without "Unblock".

``apply_update`` reads a downloaded zip (the CI artifact, possibly GitHub's zip-in-zip wrapping) with
:mod:`zipfile` and writes the files into the toolbox folder. Files written by Python carry no Windows
Mark-of-the-Web, so no "Unblock" step is needed (unlike extracting with Explorer). Only Python and data files
are written: compiled binaries (``.pyd`` / ``.dll`` / ``.exe`` ...) are never written, staged or swapped by
the update (the engine changes only with the full bundle). ``fresh_impl`` drops the cached WWNet modules so
the next tool run sees the new Python code.

Standard library only; arcpy is never imported here.
"""
from __future__ import annotations

import importlib
import io
import re
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

BACKUP_DIR = ".update_backup"
PAYLOAD_DIR = "WWNet-ArcGIS"
PURGE_PREFIXES = ("wwnet_arcgis", "wwnet_shared")
KEEP_MODULES = ("wwnet_arcgis.vendor",)
#: files of earlier versions that no longer exist (removed by an update so nothing stale stays in the toolbox,
#: e.g. the assistant's API-key handling, which antivirus heuristics flag)
OBSOLETE_FILES = ("wwnet_arcgis/dpapi.py", "wwnet_arcgis/settings.py", "wwnet_arcgis/assistant_run.py",
                  "wwnet_arcgis/arcpy_tools.py", "wwnet_arcgis/cli.py", "wwnet_arcgis/issues_table.py",
                  "wwnet_shared/core/hydraulics.py", "wwnet_shared/core/rules.py")
OBSOLETE_DIRS = ("wwnet_shared/assistant",)
#: never written by an update: engine / binary changes need the full bundle
BINARY_SUFFIXES = (".pyd", ".dll", ".exe", ".so", ".dylib", ".pending")


def _open_zip(src, depth=0):
    """Open ``src`` (path or file object); unwrap a lone inner .zip (GitHub artifact wrapping)."""
    zf = zipfile.ZipFile(src)
    if depth < 2:
        names = [i for i in zf.infolist() if not i.is_dir()]
        if len(names) == 1 and names[0].filename.lower().endswith(".zip"):
            data = zf.read(names[0])
            zf.close()
            return _open_zip(io.BytesIO(data), depth + 1)
    return zf


def _safe_parts(name):
    """Path parts of a member name, or None if unsafe (absolute, drive letter, '..')."""
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in PurePosixPath(n).parts if p not in ("", ".")]
    if any(p == ".." or ":" in p for p in parts):
        return None
    return parts


def _payload_prefix(names):
    if any(n.startswith(PAYLOAD_DIR + "/") for n in names):
        return PAYLOAD_DIR + "/"
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1 and all("/" in n for n in names) and (tops.copy().pop() + "/WWNet.pyt") in names:
        return tops.pop() + "/"
    return ""


def _parse_version(text):
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", text, re.M)
    return m.group(1) if m else None


def apply_update(zip_path, root, log=print) -> dict:
    """Apply the update zip to the toolbox folder ``root``.

    Returns ``{"updated": [relpaths], "unchanged": n, "binaries": [relpaths not written], "skipped": [names],
    "backup": folder or None, "version": str or None}``. Raises ValueError for a zip that is not a
    WWNet update."""
    root = Path(root)
    zf = _open_zip(str(zip_path))
    with zf:
        members = {i.filename.replace("\\", "/"): i for i in zf.infolist()}
        prefix = _payload_prefix(set(members))
        files = {}
        skipped = []
        binaries = []
        for name, info in members.items():
            if not name.startswith(prefix) or info.is_dir() or name.endswith("/"):
                continue
            rel_name = name[len(prefix):]
            parts = _safe_parts(rel_name)
            if parts is None or _safe_parts(name) is None:
                log(f"Skipping unsafe path in zip: {name}")
                skipped.append(name)
                continue
            if "__pycache__" in parts or parts[-1].endswith(".pyc"):
                continue
            if parts[-1].lower().endswith(BINARY_SUFFIXES):
                binaries.append("/".join(parts))
                continue
            files["/".join(parts)] = info
        if "WWNet.pyt" not in files and not any(r.startswith("wwnet_arcgis/") for r in files):
            raise ValueError("This zip does not look like a WWNet update (no WWNet.pyt or wwnet_arcgis/ "
                             "files found). Download the 'wwnet-arcgis-update' artifact.")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_root = root / BACKUP_DIR / stamp
        updated, unchanged, backed_up = [], 0, False
        for rel, info in sorted(files.items()):
            data = zf.read(info)
            target = root / rel
            try:
                if target.is_file() and target.read_bytes() == data:
                    unchanged += 1
                    continue
            except OSError:
                pass
            try:
                if target.is_file():
                    bak = backup_root / rel
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    bak.write_bytes(target.read_bytes())
                    backed_up = True
            except OSError as exc:
                log(f"Could not back up {rel}: {exc}")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                updated.append(rel)
            except OSError as exc:
                log(f"Could not write {rel}: {exc}")
                skipped.append(rel)
        removed = remove_obsolete(root, log)
        version = None
        init = files.get("wwnet_arcgis/__init__.py")
        if init is not None:
            version = _parse_version(zf.read(init).decode("utf-8", "replace"))
    return {"updated": updated, "unchanged": unchanged, "binaries": binaries, "skipped": skipped, "removed": removed,
            "backup": str(backup_root) if backed_up else None, "version": version}


def remove_obsolete(root, log=print) -> list:
    """Delete files of earlier versions listed in OBSOLETE_FILES / OBSOLETE_DIRS (and their __pycache__)."""
    import shutil
    root = Path(root)
    removed = []
    for rel in OBSOLETE_FILES:
        p = root / rel
        if p.is_file():
            try:
                p.unlink()
                removed.append(rel)
            except OSError as exc:
                log(f"Could not remove obsolete {rel}: {exc}")
    for rel in OBSOLETE_DIRS:
        p = root / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed.append(rel + "/")
    return removed


def purge_modules(prefixes=PURGE_PREFIXES) -> list:
    """Remove WWNet modules from ``sys.modules`` (except ``wwnet_arcgis.vendor``, which holds state)."""
    removed = []
    for name in list(sys.modules):
        if name in KEEP_MODULES:
            continue
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            del sys.modules[name]
            removed.append(name)
    importlib.invalidate_caches()
    return removed


def fresh_impl():
    """Re-import ``wwnet_arcgis.tools_impl`` from disk so every tool run sees the latest files."""
    purge_modules()
    return importlib.import_module("wwnet_arcgis.tools_impl")
