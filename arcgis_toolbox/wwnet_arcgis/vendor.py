"""No-install bundle support: make a ``<toolbox>/vendor`` folder importable and describe the result.

The CI bundle ships ``anthropic``, ``pydantic``, ``httpx2``, ``jiter``, ``truststore`` ... and the
``wwnet_native`` extension in ``<toolbox>/vendor`` so nothing has to be pip-installed into ArcGIS Pro's
(read-only) Python. Rules:

* ``vendor`` goes at the FRONT of ``sys.path`` (idempotent).
* ``anyio``, ``sniffio``, ``idna``, ``certifi`` are NEVER vendored: Pro's own copies stay in use so Pro's
  Jupyter / other tools are not disturbed (the bundle is built without them).
* ``anthropic`` needs ``typing_extensions >= 4.14`` but Pro ships 4.9 and usually has it imported already.
  ``activate`` then loads the vendored copy and replaces ``sys.modules['typing_extensions']`` (the library is
  backward compatible, so modules holding the old one keep working).

Only the standard library is imported here, so this module is safe to import anywhere (and from tests).
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("wwnet.vendor")

MIN_TYPING_EXTENSIONS = (4, 14)
NEVER_VENDORED = ("anyio", "sniffio", "idna", "certifi")

#: (import name, distribution name) in report order
PACKAGES = (
    ("wwnet_native", "wwnet_native"), ("anthropic", "anthropic"), ("pydantic", "pydantic"),
    ("pydantic_core", "pydantic_core"), ("httpx2", "httpx2"), ("httpx", "httpx"),
    ("httpcore2", "httpcore2"), ("jiter", "jiter"), ("typing_extensions", "typing_extensions"),
    ("anyio", "anyio"), ("sniffio", "sniffio"), ("idna", "idna"), ("truststore", "truststore"),
    ("certifi", "certifi"),
)

NOTES: list = []          # human-readable things activate() did (shown by Verify Setup)
_TRUST = {"done": False, "note": ""}


def _note(msg: str, log_fn=None):
    if msg not in NOTES:
        NOTES.append(msg)
    log.info(msg)
    if log_fn:
        log_fn(msg)


def _parse_version(text) -> tuple:
    nums = []
    for part in str(text).split("."):
        m = re.match(r"\d+", part)
        if not m:
            break
        nums.append(int(m.group()))
    return tuple(nums)


def _module_version(mod) -> tuple | None:
    """Version of an already-imported module (typing_extensions has no __version__ before 4.14?):
    ``__version__``, else a sibling ``<name>-<ver>.dist-info`` next to its file, else None."""
    v = getattr(mod, "__version__", None)
    if v:
        return _parse_version(v)
    f = getattr(mod, "__file__", None)
    if f:
        name = mod.__name__.replace("-", "_")
        for d in Path(f).resolve().parent.glob(f"{name}-*.dist-info"):
            m = re.match(rf"{re.escape(name)}-(.+)\.dist-info$", d.name, re.I)
            if m:
                return _parse_version(m.group(1))
    return None


def _te_is_new_enough(mod) -> bool:
    ver = _module_version(mod)
    if ver:
        return ver >= MIN_TYPING_EXTENSIONS
    return hasattr(mod, "Sentinel")      # added in 4.14.0; last resort for unknown versions


def vendor_dir(root) -> Path:
    return Path(root) / "vendor"


def activate(root, log_fn=None):
    """Put ``<root>/vendor`` first on sys.path (once) and make sure typing_extensions is new enough.
    Returns the vendor path, or None when the folder does not exist (e.g. dev checkout)."""
    vd = vendor_dir(root)
    if not vd.is_dir():
        return None
    s = str(vd)
    if s not in sys.path:
        sys.path.insert(0, s)
        importlib.invalidate_caches()
    _ensure_typing_extensions(vd, log_fn)
    return vd


def _ensure_typing_extensions(vd: Path, log_fn=None):
    old = sys.modules.get("typing_extensions")
    if old is None:
        return                              # not loaded yet: the vendored copy (front of path) wins
    if _te_is_new_enough(old):
        return
    src = vd / "typing_extensions.py"
    if not src.is_file():
        return
    spec = importlib.util.spec_from_file_location("typing_extensions", src)
    new = importlib.util.module_from_spec(spec)
    sys.modules["typing_extensions"] = new
    try:
        spec.loader.exec_module(new)
    except Exception as exc:                # noqa: BLE001 - keep the old module on any failure
        sys.modules["typing_extensions"] = old
        _note(f"Could not load vendored typing_extensions ({exc}); keeping {getattr(old, '__file__', old)}.",
              log_fn)
        return
    if not _te_is_new_enough(new):          # vendored copy too old as well: undo
        sys.modules["typing_extensions"] = old
        return
    _note("typing_extensions: replaced the already-loaded older copy "
          f"({getattr(old, '__file__', '?')}) with the vendored one ({src}); it is backward compatible.",
          log_fn)


def inject_truststore(log_fn=None) -> str:
    """Trust the Windows certificate store for the assistant's HTTPS (corporate TLS inspection).
    Call only from inside an assistant run. Returns a one-line status; never injects twice.

    anthropic 2.x uses httpx2/httpcore2, which already builds ``truststore.SSLContext`` on Windows/macOS
    itself (unless SSL_CERT_FILE / SSL_CERT_DIR is set), so nothing is injected then. Older httpx+certifi
    stacks get ``truststore.inject_into_ssl()``."""
    if _TRUST["done"]:
        return _TRUST["note"]
    _TRUST["done"] = True
    if importlib.util.find_spec("httpcore2") is not None:
        note = "TLS: httpx2/httpcore2 already uses the operating-system trust store; no injection needed."
    else:
        try:
            import truststore
            truststore.inject_into_ssl()
            note = "TLS: truststore injected, so the Windows certificate store is trusted."
        except Exception as exc:            # noqa: BLE001
            note = f"TLS: truststore not available ({type(exc).__name__}); using the bundled CA list."
    _TRUST["note"] = note
    _note(note, log_fn)
    return note


def _locate(mod_name: str):
    mod = sys.modules.get(mod_name)
    if mod is not None:
        f = getattr(mod, "__file__", None) or (list(getattr(mod, "__path__", [])) or [None])[0]
        return mod, (str(f) if f else None)
    try:
        spec = importlib.util.find_spec(mod_name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None, None
    if spec.origin not in (None, "namespace"):
        return None, spec.origin
    locs = list(spec.submodule_search_locations or [])
    return None, (str(locs[0]) if locs else None)


def _try_import(name: str):
    try:
        importlib.import_module(name)
        return True, ""
    except BaseException as exc:            # noqa: BLE001 - report any failure, incl. SystemExit
        return False, f"{type(exc).__name__}: {exc}"


def report() -> dict:
    """``{package: (version, location)}`` (``(None, None)`` when missing) plus the keys
    ``_python``, ``_vendor`` (path on sys.path or None), ``_notes``, ``_import_anthropic`` and
    ``_import_wwnet_native`` (each ``(ok, error text)``)."""
    out: dict = {}
    for mod_name, dist in PACKAGES:
        mod, loc = _locate(mod_name)
        if loc is None:
            out[mod_name] = (None, None)
            continue
        ver = None
        if mod is not None and mod_name == "typing_extensions":
            v = _module_version(mod)
            ver = ".".join(map(str, v)) if v else None
        if ver is None:
            try:
                ver = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                ver = getattr(mod, "__version__", None) if mod is not None else None
        out[mod_name] = (ver or "unknown", loc)
    out["_python"] = f"{sys.version.split()[0]} ({sys.platform}, {sys.executable})"
    out["_vendor"] = next((p for p in sys.path if Path(p).name == "vendor"), None)
    out["_notes"] = list(NOTES)
    out["_import_anthropic"] = _try_import("anthropic")
    out["_import_wwnet_native"] = _try_import("wwnet_native")
    return out


def format_report(rep: dict) -> list:
    """Message lines for the geoprocessing pane."""
    lines = [f"Python {rep['_python']}", f"Vendor folder: {rep['_vendor'] or '(none on sys.path)'}"]
    for mod_name, _ in PACKAGES:
        ver, loc = rep[mod_name]
        lines.append(f"  {mod_name:<18} {ver or 'not found':<10} {loc or ''}")
    for key, label in (("_import_anthropic", "import anthropic"), ("_import_wwnet_native", "import wwnet_native")):
        ok, err = rep[key]
        lines.append(f"{label}: " + ("OK" if ok else f"FAILED - {err}"))
    lines.extend(f"Note: {n}" for n in rep["_notes"])
    return lines
