#!/usr/bin/env python3
"""Build dist/wwnet-arcgis-<version>.zip for ArcGIS Pro.

Layout inside the zip (extract anywhere, then add WWNet.pyt as a toolbox in Pro):

    WWNet-ArcGIS/WWNet.pyt
    WWNet-ArcGIS/wwnet_arcgis/*.py            toolbox modules
    WWNet-ArcGIS/wwnet_shared/{core,assistant}/*.py   copy of the shared QGIS-free modules
    WWNet-ArcGIS/templates/schema.sql
    WWNet-ArcGIS/templates/import/*.toml       import templates (Import Sewer Data)
    WWNet-ArcGIS/templates/profiles/*.csv      example diurnal profile (APPROXIMATE)
    WWNet-ArcGIS/rulesets/*.toml
    WWNet-ArcGIS/bin/wwnet.exe                only with --cli <path>
    WWNet-ArcGIS/vendor/...                   only with --vendor-dir <path> (no-install bundle: anthropic,
                                              pydantic, httpx2, jiter, truststore, typing_extensions,
                                              wwnet_native ...; wwnet_arcgis/vendor.py puts it first on sys.path)
    WWNet-ArcGIS/README.md

--vendor-dir is a folder made by `pip install --target vendor --only-binary=:all: anthropic <wwnet_native wheel>`
(for Pro's Python: 3.11 win_amd64). anyio*, sniffio*, idna* and certifi* are never copied, even if present:
Pro's own versions must stay in use (see wwnet_arcgis/vendor.py).

Usage: python package.py [--cli path/to/wwnet.exe] [--vendor-dir vendor] [--out dist]
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PLUGIN = REPO / "qgis_plugin" / "wwnet"
TOP = "WWNet-ArcGIS"

# Shared modules that must stay free of qgis / Qt imports.
SHARED_ASSISTANT = ("__init__.py", "engine.py", "client.py", "prompt.py", "tools.py", "install.py")
SHARED_STUB = '"""WWNet shared pure-Python modules (copied from qgis_plugin/wwnet by package.py)."""\n'


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (HERE / "wwnet_arcgis" / "__init__.py").read_text())
    return m.group(1)


# Pro ships these; shadowing them could break Pro's own tools (e.g. Jupyter).
NEVER_VENDORED = ("anyio", "sniffio", "idna", "certifi")


def _skip_vendor(rel: Path) -> bool:
    if "__pycache__" in rel.parts or rel.suffix == ".pyc":
        return True
    top = rel.parts[0].lower()
    return top == "bin" or re.match(rf"({'|'.join(NEVER_VENDORED)})(-|\.|$)", top) is not None


def vendor_files(vendor_dir: Path) -> dict:
    out = {}
    for p in sorted(vendor_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(vendor_dir)
            if not _skip_vendor(rel):
                out[f"vendor/{rel.as_posix()}"] = p
    return out


def collect(cli: Path | None = None, vendor_dir: Path | None = None) -> dict:
    """Return {archive path inside TOP: bytes or Path}."""
    files: dict = {}
    files["WWNet.pyt"] = HERE / "WWNet.pyt"
    files["README.md"] = HERE / "README.md"
    for p in sorted((HERE / "wwnet_arcgis").glob("*.py")):
        files[f"wwnet_arcgis/{p.name}"] = p
    files["wwnet_shared/__init__.py"] = SHARED_STUB.encode()
    for p in sorted((PLUGIN / "core").glob("*.py")):
        files[f"wwnet_shared/core/{p.name}"] = p
    for name in SHARED_ASSISTANT:
        files[f"wwnet_shared/assistant/{name}"] = PLUGIN / "assistant" / name
    files["templates/schema.sql"] = REPO / "templates" / "schema.sql"
    for p in sorted((REPO / "templates" / "import").glob("*.toml")):
        files[f"templates/import/{p.name}"] = p
    for p in sorted((REPO / "templates" / "profiles").glob("*.*")):
        files[f"templates/profiles/{p.name}"] = p
    for p in sorted((REPO / "rulesets").glob("*.toml")):
        files[f"rulesets/{p.name}"] = p
    if cli:
        files[f"bin/{cli.name}"] = cli
    if vendor_dir:
        files.update(vendor_files(vendor_dir))
    return files


def build(out_dir: Path | None = None, cli: Path | None = None, vendor_dir: Path | None = None) -> Path:
    out_dir = out_dir or HERE / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"wwnet-arcgis-{version()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in collect(cli, vendor_dir).items():
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            z.writestr(f"{TOP}/{arc}", data)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", type=Path, help="wwnet.exe to bundle under bin/")
    ap.add_argument("--vendor-dir", type=Path, help="prepared vendor folder to copy to vendor/")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cli and not a.cli.is_file():
        sys.exit(f"--cli {a.cli} not found")
    if a.vendor_dir and not a.vendor_dir.is_dir():
        sys.exit(f"--vendor-dir {a.vendor_dir} not found")
    print(f"wrote {build(a.out, a.cli, a.vendor_dir)}")
