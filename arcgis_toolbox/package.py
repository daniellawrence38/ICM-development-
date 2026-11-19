#!/usr/bin/env python3
"""Build dist/wwnet-arcgis-<version>.zip for ArcGIS Pro.

Layout inside the zip (extract anywhere, then add WWNet.pyt as a toolbox in Pro):

    WWNet-ArcGIS/WWNet.pyt
    WWNet-ArcGIS/wwnet_arcgis/*.py            toolbox modules
    WWNet-ArcGIS/wwnet_shared/{core,assistant}/*.py   copy of the shared QGIS-free modules
    WWNet-ArcGIS/templates/schema.sql
    WWNet-ArcGIS/rulesets/*.toml
    WWNet-ArcGIS/bin/wwnet.exe                only with --cli <path>
    WWNet-ArcGIS/README.md

Usage: python package.py [--cli path/to/wwnet.exe] [--out dist]
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


def collect(cli: Path | None = None) -> dict:
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
    for p in sorted((REPO / "rulesets").glob("*.toml")):
        files[f"rulesets/{p.name}"] = p
    if cli:
        files[f"bin/{cli.name}"] = cli
    return files


def build(out_dir: Path | None = None, cli: Path | None = None) -> Path:
    out_dir = out_dir or HERE / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"wwnet-arcgis-{version()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in collect(cli).items():
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            z.writestr(f"{TOP}/{arc}", data)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", type=Path, help="wwnet.exe to bundle under bin/")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cli and not a.cli.is_file():
        sys.exit(f"--cli {a.cli} not found")
    print(f"wrote {build(a.out, a.cli)}")
