#!/usr/bin/env python3
"""Build dist/wwnet-arcgis-<version>.zip for ArcGIS Pro.

Layout inside the zip (extract anywhere, then add WWNet.pyt as a toolbox in Pro):

    WWNet-ArcGIS/WWNet.pyt
    WWNet-ArcGIS/wwnet_arcgis/*.py            toolbox modules
    WWNet-ArcGIS/wwnet_shared/core/*.py            copy of the shared QGIS-free modules (no assistant)
    WWNet-ArcGIS/templates/schema.sql
    WWNet-ArcGIS/templates/import/*.toml       import templates (Import Sewer Data)
    WWNet-ArcGIS/templates/profiles/*.csv      example diurnal profile (APPROXIMATE)
    WWNet-ArcGIS/templates/demo/demo.gpkg      demo model used by the Self Test tool
    WWNet-ArcGIS/templates/demo/pump_register_demo.xlsx   demo pump station register (Self Test, Import Pump Station Data)
    WWNet-ArcGIS/templates/demo/pump_register_demo.toml   column map of that demo workbook (Self Test; do not edit)
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
                         [--python-only] [--unzipped DIR]

--python-only   pure-Python update bundle: the same tree minus bin/ and vendor/ (--cli and --vendor-dir are
                ignored with a warning). Written as dist/wwnet-arcgis-update-<version>.zip. This is what the
                toolbox's "Update WWNet" tool consumes.
--unzipped DIR  also writes the same tree on disk to DIR/WWNet-ArcGIS/... (CI uploads this folder so the
                artifact zip is the only zip layer). The zip in --out is still written.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PLUGIN = REPO / "qgis_plugin" / "wwnet"
TOP = "WWNet-ArcGIS"

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


def collect(cli: Path | None = None, vendor_dir: Path | None = None, python_only: bool = False) -> dict:
    """Return {archive path inside TOP: bytes or Path}. Shared by the zip and unzipped writers.

    python_only drops bin/ and vendor/ regardless of cli / vendor_dir.
    """
    files: dict = {}
    files["WWNet.pyt"] = HERE / "WWNet.pyt"
    files["README.md"] = HERE / "README.md"
    for p in sorted((HERE / "wwnet_arcgis").glob("*.py")):
        files[f"wwnet_arcgis/{p.name}"] = p
    files["wwnet_shared/__init__.py"] = SHARED_STUB.encode()
    for p in sorted((PLUGIN / "core").glob("*.py")):
        files[f"wwnet_shared/core/{p.name}"] = p
    files["templates/schema.sql"] = REPO / "templates" / "schema.sql"
    for p in sorted((REPO / "templates" / "import").glob("*.toml")):
        files[f"templates/import/{p.name}"] = p
    for p in sorted((REPO / "templates" / "profiles").glob("*.*")):
        files[f"templates/profiles/{p.name}"] = p
    files["templates/demo/demo.gpkg"] = REPO / "examples" / "demo" / "demo.gpkg"
    files["templates/demo/pump_register_demo.xlsx"] = REPO / "examples" / "demo" / "pump_register_demo.xlsx"
    files["templates/demo/pump_register_demo.toml"] = REPO / "examples" / "demo" / "pump_register_demo.toml"
    for p in sorted((REPO / "rulesets").glob("*.toml")):
        files[f"rulesets/{p.name}"] = p
    if not python_only:
        if cli:
            files[f"bin/{cli.name}"] = cli
        if vendor_dir:
            files.update(vendor_files(vendor_dir))
    return files


def _data(src) -> bytes:
    return src if isinstance(src, bytes) else Path(src).read_bytes()


def build(out_dir: Path | None = None, cli: Path | None = None, vendor_dir: Path | None = None,
          python_only: bool = False) -> Path:
    out_dir = out_dir or HERE / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = "wwnet-arcgis-update" if python_only else "wwnet-arcgis"
    out = out_dir / f"{kind}-{version()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in collect(cli, vendor_dir, python_only).items():
            z.writestr(f"{TOP}/{arc}", _data(src))
    return out


def write_unzipped(dest: Path, cli: Path | None = None, vendor_dir: Path | None = None,
                   python_only: bool = False) -> Path:
    """Write the same tree as build() to dest/TOP/... on disk. Replaces any previous dest/TOP folder."""
    root = dest / TOP
    if root.exists():
        shutil.rmtree(root)
    for arc, src in collect(cli, vendor_dir, python_only).items():
        target = root / arc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_data(src))
    return root


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", type=Path, help="wwnet.exe to bundle under bin/")
    ap.add_argument("--vendor-dir", type=Path, help="prepared vendor folder to copy to vendor/")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--python-only", action="store_true",
                    help="pure-Python update bundle (no bin/ or vendor/); writes wwnet-arcgis-update-<version>.zip")
    ap.add_argument("--unzipped", type=Path, metavar="DIR",
                    help="also write the same tree on disk under DIR/WWNet-ArcGIS/")
    a = ap.parse_args()
    if a.python_only and (a.cli or a.vendor_dir):
        print("warning: --python-only ignores --cli and --vendor-dir", file=sys.stderr)
        a.cli = a.vendor_dir = None
    if a.cli and not a.cli.is_file():
        sys.exit(f"--cli {a.cli} not found")
    if a.vendor_dir and not a.vendor_dir.is_dir():
        sys.exit(f"--vendor-dir {a.vendor_dir} not found")
    if a.unzipped:
        print(f"wrote {write_unzipped(a.unzipped, a.cli, a.vendor_dir, a.python_only)}")
    print(f"wrote {build(a.out, a.cli, a.vendor_dir, a.python_only)}")
