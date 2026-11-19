"""Run WWNet check / design / trace.

The `wwnet_native` Python extension (a wheel, no .exe) is the first choice. The `wwnet` CLI is
used only as a fallback when the extension is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import settings, shared

TOOLBOX_ROOT = Path(__file__).resolve().parent.parent


class CliError(RuntimeError):
    pass


def find_exe(configured: str | None = None, root: Path | None = None) -> str:
    """Toolbox setting, then PATH, then bundled bin/wwnet.exe."""
    root = root or TOOLBOX_ROOT
    cfg = configured if configured is not None else settings.load().get("wwnet_exe", "")
    if cfg:
        if os.path.isfile(cfg):
            return cfg
        raise CliError(f"Configured WWNet CLI not found: {cfg} "
                       "(fix it in 'Set Assistant API Key' > WWNet CLI path)")
    onpath = shutil.which("wwnet")
    if onpath:
        return onpath
    for name in ("wwnet.exe", "wwnet"):
        b = root / "bin" / name
        if b.is_file():
            return str(b)
    raise CliError(no_backend_message(root))


def no_backend_message(root: Path | None = None) -> str:
    root = root or TOOLBOX_ROOT
    return ("The WWNet native module (wwnet_native) is not installed, so check/design/trace cannot run. "
            "Use the no-install bundle (CI artifact wwnet-arcgis-bundle-win-py311, which carries it in "
            f"{root / 'vendor'}) and run the 'Verify Setup' tool, or pip install "
            "`wwnet_native-<version>-cp39-abi3-win_amd64.whl` into a cloned environment (no .exe required). "
            "(Fallback only: a `wwnet` CLI on PATH, in "
            f"{root / 'bin'}, or set in 'Set Assistant API Key'.)")


def _activate_vendor():
    from . import vendor
    vendor.activate(TOOLBOX_ROOT)


def native_service():
    """The wwnet_native module if it provides check/design/trace, else None (the probe)."""
    _activate_vendor()
    try:
        return shared.core("native").service()
    except Exception:  # noqa: BLE001 - any import problem means "not available"
        return None


def _run(args, timeout=600):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(f"Could not run {args[0]}: {exc}") from exc


def _json(proc, what):
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise CliError(f"wwnet {what} returned unparseable output (exit {proc.returncode}): {detail}") from exc


def _native_call(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - native raises KeyError/RuntimeError/IOError/ValueError
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        raise CliError(str(msg)) from exc


def run_check(gpkg: str, exe: str | None = None):
    """Returns (issues, returncode); issues normalised by the shared checks module."""
    nat = native_service()
    if nat is not None:
        raw = _native_call(nat.check, gpkg, True)
        issues = [shared.core("checks").normalise_issue(x) for x in raw if isinstance(x, dict)]
        return issues, 1 if any(i["severity"] == "error" for i in issues) else 0
    proc = _run([exe or find_exe(), "check", gpkg, "--json"])
    _json(proc, "check")  # validates JSON before normalising
    return shared.core("checks").parse_issues(proc.stdout), proc.returncode


def run_design(gpkg: str, exe: str | None = None) -> dict:
    """Returns the design JSON: conduit_results, node_flows, issues, summary."""
    nat = native_service()
    if nat is not None:
        return _native_call(nat.design, gpkg, None, True)
    proc = _run([exe or find_exe(), "design", gpkg, "--json"])
    data = _json(proc, "design")
    if not isinstance(data, dict) or "summary" not in data:
        raise CliError(f"Unexpected design output (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return data


_HEAD = re.compile(r"^(upstream|downstream) of (\S+) \((\d+)\):\s*$")


def parse_trace(stdout: str) -> dict:
    """{'upstream': [ids], 'downstream': [ids]} from the CLI's text output."""
    out: dict = {}
    cur = None
    for line in stdout.splitlines():
        m = _HEAD.match(line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur and line.startswith("  ") and line.strip():
            out[cur].append(line.strip())
    return out


def run_trace(gpkg: str, node_id: str, up: bool = True, down: bool = False,
              exe: str | None = None) -> dict:
    nat = native_service()
    if nat is not None:
        return _native_call(nat.trace, gpkg, node_id, up, down)
    args = [exe or find_exe(), "trace", gpkg, node_id]
    if up:
        args.append("--up")
    if down:
        args.append("--down")
    proc = _run(args)
    res = parse_trace(proc.stdout)
    if proc.returncode != 0 or not res:
        raise CliError((proc.stderr or proc.stdout).strip()[:500] or f"wwnet trace failed ({proc.returncode})")
    return res


def failed_conduits(design: dict) -> list:
    return [c["conduit_id"] for c in design.get("conduit_results", []) if c.get("result") == "fail"]


def failed_checks(conduit_result: dict) -> list:
    return [c for c in conduit_result.get("checks", []) if c.get("result") in ("fail", "warn")]
