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
    assert not hasattr(shared, "assistant")          # the assistant is not part of the ArcGIS toolbox


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
                     "templates/import/watercorp_sewer.toml", "templates/demo/demo.gpkg", "templates/profiles/example_residential.csv", "wwnet_shared/core/importer.py", "wwnet_shared/core/repair.py",
                     "wwnet_arcgis/sewer_import.py", "wwnet_shared/__init__.py", "wwnet_shared/core/schema.py",
                     "wwnet_shared/core/native.py", "wwnet_shared/core/calc.py",
                     "wwnet_shared/core/summary.py", "wwnet_shared/core/style.py", "wwnet_arcgis/tools_impl.py",
                     "wwnet_arcgis/engine.py"):
            assert top + must in names, must
        for forbidden_file in ("dock.py", "qgis_tools.py", "qt_bridge.py", "secrets.py",
                               "settings_dialog.py", "plugin.py"):
            assert not any(n.endswith("/" + forbidden_file) and "wwnet_shared" in n for n in names)
        pys = [n for n in names if n.endswith((".py", ".pyt"))]
        assert pys
        for n in pys:
            assert forbidden_in(zf.read(n).decode("utf-8")) == [], f"{n} imports qgis/Qt"


def test_no_cli_or_python_engine_copies_shipped(tmp_path):
    """The engine is wwnet_native only: no wwnet.exe, no pure-Python hydraulics / rules copies."""
    z = package.build(tmp_path / "o")
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    assert not any("/bin/" in n for n in names)
    for gone in ("core/hydraulics.py", "core/rules.py", "wwnet_arcgis/cli.py", "wwnet_arcgis/issues_table.py"):
        assert not any(n.endswith(gone) for n in names), gone
    for new in ("core/calc.py", "core/summary.py", "core/style.py", "wwnet_arcgis/engine.py"):
        assert any(n.endswith(new) for n in names), new


UPDATE_MUST = (
    "WWNet.pyt", "README.md", "wwnet_arcgis/tools_impl.py", "wwnet_arcgis/sewer_import.py",
    "wwnet_shared/__init__.py", "wwnet_shared/core/schema.py", "wwnet_shared/core/summary.py",
    "templates/schema.sql", "rulesets/ds50_v2r4.toml",
)


def _dummy_vendor(root: Path) -> Path:
    vendor = root / "vendor"
    (vendor / "anthropic").mkdir(parents=True)
    (vendor / "anthropic" / "__init__.py").write_text("# dummy\n")
    return vendor


def test_python_only_zip_has_toolbox_and_no_vendor_or_bin(tmp_path):
    fake = tmp_path / "wwnet.exe"
    fake.write_bytes(b"MZ")
    z = package.build(tmp_path / "o", cli=fake, vendor_dir=_dummy_vendor(tmp_path), python_only=True)
    assert z.name == f"wwnet-arcgis-update-{package.version()}.zip"
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
    top = package.TOP + "/"
    for must in UPDATE_MUST:
        assert top + must in names, must
    assert not any(n.startswith((top + "vendor/", top + "bin/")) for n in names)
    # same contents as the normal package, minus vendor/ and bin/
    normal = package.build(tmp_path / "n")
    with zipfile.ZipFile(normal) as zf:
        assert names == set(zf.namelist())


def test_unzipped_matches_python_only_zip(tmp_path):
    out_dir = tmp_path / "update"
    root = package.write_unzipped(out_dir, python_only=True)
    assert root == out_dir / package.TOP
    z = package.build(tmp_path / "o", python_only=True)
    with zipfile.ZipFile(z) as zf:
        zipped = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
    on_disk = {p.relative_to(out_dir).as_posix(): p.read_bytes()
               for p in out_dir.rglob("*") if p.is_file()}
    assert set(on_disk) == set(zipped)
    assert on_disk == zipped
    assert all(n.startswith(package.TOP + "/") for n in on_disk)
    assert package.TOP + "/WWNet.pyt" in on_disk


def test_cli_python_only_ignores_vendor_with_warning(tmp_path):
    out_dir = tmp_path / "update"
    r = subprocess.run(
        [sys.executable, str(Path(package.__file__)), "--python-only", "--vendor-dir",
         str(_dummy_vendor(tmp_path)), "--unzipped", str(out_dir), "--out", str(tmp_path / "o")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "--python-only ignores --cli and --vendor-dir" in r.stderr
    assert not (out_dir / package.TOP / "vendor").exists()
    assert (out_dir / package.TOP / "WWNet.pyt").is_file()
    assert (tmp_path / "o" / f"wwnet-arcgis-update-{package.version()}.zip").is_file()


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
        "print(shared.core('summary').issue_line({'code': 'X', 'object_type': 'node', 'object_id': 'N1', 'message': 'm'}))\n"
        "print(len(capacity.describe({'dn_mm': 225, 'grade_1in': 300, 'ks_mm': 1.0, 'full_flow_lps': 1.0,"
        " 'full_velocity_mps': 1.0})))\n"
        "assert not any(m.startswith('qgis') for m in sys.modules)\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code, str(root)], capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert float(r.stdout.splitlines()[-1]) > 0
