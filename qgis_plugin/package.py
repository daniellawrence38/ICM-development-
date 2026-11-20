#!/usr/bin/env python3
"""Bundle schema.sql, import templates and rulesets into the plugin package and zip it for QGIS.

Usage:  python package.py [--no-zip]
Output: dist/wwnet-<version>.zip  (install via Plugins > Manage and Install > Install from ZIP)
"""
import configparser
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PLUGIN = HERE / "wwnet"
RES = PLUGIN / "resources"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}


def copy_resources():
    RES.mkdir(exist_ok=True)
    src = REPO / "templates" / "schema.sql"
    shutil.copy2(src, RES / "schema.sql")
    n = 1
    for toml in sorted((REPO / "templates" / "import").glob("*.toml")):
        shutil.copy2(toml, RES / toml.name)
        n += 1
    for toml in sorted((REPO / "rulesets").glob("*.toml")):
        shutil.copy2(toml, RES / toml.name)
        n += 1
    for f in sorted((REPO / "templates" / "profiles").glob("*.csv")):
        shutil.copy2(f, RES / f.name)
        n += 1
    print(f"copied {n} resource file(s) into {RES}")


def version():
    cp = configparser.ConfigParser()
    cp.read(PLUGIN / "metadata.txt", encoding="utf-8")
    return cp["general"]["version"]


def make_zip():
    out_dir = HERE / "dist"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"wwnet-{version()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(PLUGIN.rglob("*")):
            if p.is_dir() or EXCLUDE_DIRS & set(p.parts) or p.suffix in (".pyc", ".pyo"):
                continue
            z.write(p, Path("wwnet") / p.relative_to(PLUGIN))
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    copy_resources()
    if "--no-zip" not in sys.argv:
        make_zip()
