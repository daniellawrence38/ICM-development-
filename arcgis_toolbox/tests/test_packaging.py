import ast
import subprocess
import sys
import zipfile
from pathlib import Path

import package

TOOLBOX = Path(__file__).resolve().parents[1]
FORBIDDEN = {"qgis", "PyQt4", "PyQt5", "PyQt6", "sip", "PySide2", "PySide6"}


def imported_modules(tree, top_level_only=False):
    nodes = tree.body if top_level_only else ast.walk(tree)
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            yield n.module.split(".")[0]


def forbidden_in(source: str):
    return sorted(set(imported_modules(ast.parse(source))) & FORBIDDEN)


def test_dev_shared_resolves_to_plugin():
    from wwnet_arcgis import shared
    assert shared.shared_dir() == TOOLBOX.parent / "qgis_plugin" / "wwnet"
    assert shared.core("schema").FEATURE_TABLES
    assert shared.assistant("engine").Engine


def test_toolbox_modules_have_no_qgis_and_no_toplevel_arcpy():
    for p in (TOOLBOX / "wwnet_arcgis").glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        assert not set(imported_modules(tree)) & FORBIDDEN, p
        assert "arcpy" not in set(imported_modules(tree, top_level_only=True)), f"{p}: arcpy must be lazy"


def test_zip_contents_and_no_qgis(tmp_path):
    z = package.build(tmp_path)
    assert z.name == f"wwnet-arcgis-{package.version()}.zip"
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        top = package.TOP + "/"
        for must in ("WWNet.pyt", "README.md", "templates/schema.sql", "rulesets/ds50_v2r4.toml",
                     "wwnet_shared/__init__.py", "wwnet_shared/core/schema.py",
                     "wwnet_shared/core/native.py", "wwnet_shared/assistant/engine.py",
                     "wwnet_shared/assistant/client.py", "wwnet_shared/assistant/prompt.py",
                     "wwnet_shared/assistant/tools.py", "wwnet_arcgis/tools_impl.py",
                     "wwnet_arcgis/dpapi.py"):
            assert top + must in names, must
        for forbidden_file in ("dock.py", "qgis_tools.py", "qt_bridge.py", "secrets.py",
                               "settings_dialog.py", "plugin.py"):
            assert not any(n.endswith("/" + forbidden_file) and "wwnet_shared" in n for n in names)
        pys = [n for n in names if n.endswith((".py", ".pyt"))]
        assert pys
        for n in pys:
            assert forbidden_in(zf.read(n).decode("utf-8")) == [], f"{n} imports qgis/Qt"


def test_cli_bundled_only_on_request(tmp_path):
    fake = tmp_path / "wwnet.exe"
    fake.write_bytes(b"MZ")
    z = package.build(tmp_path / "o", cli=fake)
    with zipfile.ZipFile(z) as zf:
        assert "WWNet-ArcGIS/bin/wwnet.exe" in zf.namelist()


def test_extracted_zip_imports_standalone(tmp_path):
    """The packaged layout works on its own: wwnet_shared is a real package, resources found."""
    z = package.build(tmp_path)
    out = tmp_path / "x"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(out)
    root = out / package.TOP
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from wwnet_arcgis import shared, capacity, gpkg\n"
        "pkg = shared.ensure(); assert pkg.__file__.startswith(sys.argv[1]), pkg.__file__\n"
        "print(len(shared.core('schema').column_summary()))\n"
        "print(shared.assistant('prompt').build_system_text()[:20])\n"
        "print(capacity.calculate(225, 300)['full_flow_lps'])\n"
        "assert not any(m.startswith('qgis') for m in sys.modules)\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code, str(root)], capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert float(r.stdout.splitlines()[-1]) > 0
