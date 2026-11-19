"""Install the `anthropic` package into QGIS's Python (no qgis imports)."""
from __future__ import annotations

import importlib
import os
import shutil
import site
import subprocess
import sys


def python_executable() -> str | None:
    """Find a real Python interpreter for the interpreter QGIS embeds.

    Inside QGIS sys.executable is qgis.exe / qgis-bin, not python, so look next to
    the installation prefix (Windows: <prefix>/python.exe; Unix: <prefix>/bin/python3).
    """
    exe = sys.executable or ""
    if os.path.basename(exe).lower().startswith("python"):
        return exe
    cands = []
    for prefix in (sys.exec_prefix, sys.base_exec_prefix, sys.prefix):
        if not prefix:
            continue
        cands += [os.path.join(prefix, "python.exe"), os.path.join(prefix, "python3.exe"),
                  os.path.join(prefix, "bin", "python3"), os.path.join(prefix, "bin", "python")]
    cands += [shutil.which("python3") or "", shutil.which("python") or ""]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def pip_command(exe: str | None = None) -> list:
    exe = exe or python_executable()
    if not exe:
        raise RuntimeError("Could not locate QGIS's Python interpreter "
                           f"(sys.executable={sys.executable!r}, sys.exec_prefix={sys.exec_prefix!r}).")
    return [exe, "-m", "pip", "install", "--user", "--upgrade", "anthropic"]


def activate_user_site():
    """Make a just-installed --user package importable without restarting QGIS."""
    try:
        usp = site.getusersitepackages()
        if usp and os.path.isdir(usp) and usp not in sys.path:
            site.addsitedir(usp)
    except Exception:
        pass
    importlib.invalidate_caches()


def install_anthropic(timeout: int = 600):
    """Run pip. Returns (ok, combined_output)."""
    try:
        cmd = pip_command()
    except RuntimeError as exc:
        return False, str(exc)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pip failed to run: {exc}\nCommand: {' '.join(cmd)}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, out[-3000:]
    activate_user_site()
    from . import client
    if not client.anthropic_available():
        return False, "pip succeeded but `import anthropic` still fails; restart QGIS.\n" + out[-1500:]
    return True, out[-1500:]
