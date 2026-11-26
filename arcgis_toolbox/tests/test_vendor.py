"""No-install bundle: vendor.activate / report / Verify Setup / packaging of the vendor folder."""
import sys
import zipfile
from pathlib import Path

import pytest

import package
from wwnet_arcgis import tools_impl, vendor
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean(monkeypatch):
    """Isolate sys.path / module state touched by vendor."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(vendor, "NOTES", [])


def make_root(tmp_path):
    v = tmp_path / "root" / "vendor"
    v.mkdir(parents=True)
    return tmp_path / "root"


def test_activate_inserts_front_and_is_idempotent(tmp_path, clean):
    root = make_root(tmp_path)
    assert vendor.activate(root) == root / "vendor"
    assert sys.path[0] == str(root / "vendor")
    n = len(sys.path)
    vendor.activate(root)
    vendor.activate(str(root))
    assert len(sys.path) == n and sys.path.count(str(root / "vendor")) == 1


def test_activate_without_vendor_dir_is_noop(tmp_path, clean):
    before = list(sys.path)
    assert vendor.activate(tmp_path) is None
    assert sys.path == before


def test_activate_never_touches_files(tmp_path, clean):
    """No renaming / swapping of binaries at start-up (antivirus heuristics treat that as tampering)."""
    root = make_root(tmp_path)
    (root / "vendor" / "x.pyd").write_text("old")
    (root / "vendor" / "x.pyd.pending").write_text("new")
    vendor.activate(root)
    assert (root / "vendor" / "x.pyd").read_text() == "old" and (root / "vendor" / "x.pyd.pending").exists()


def test_report_structure():
    rep = vendor.report()
    assert [n for n, _ in vendor.PACKAGES] == ["wwnet_native"]
    ver, loc = rep["wwnet_native"]
    assert (ver is None) == (loc is None)
    assert sys.version.split()[0] in rep["_python"]
    ok, err = rep["_import_wwnet_native"]
    assert isinstance(ok, bool) and (ok or err)
    lines = vendor.format_report(rep)
    assert any(line.startswith("import wwnet_native:") for line in lines)
    assert not any("anthropic" in line for line in lines)


def test_report_reports_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "wwnet_native", None)   # makes `import wwnet_native` fail
    ok, err = vendor.report()["_import_wwnet_native"]
    assert not ok and "wwnet_native" in err


def test_verify_setup(fake_arcpy, monkeypatch):
    a = fake_arcpy([])
    assert tools_impl.verify_setup(Messages(a)) is True
    text = "\n".join(t for _, t in a.log)
    assert "import wwnet_native: OK" in text and "every engine function" in text
    monkeypatch.setitem(sys.modules, "wwnet_native", None)
    a.log.clear()
    assert tools_impl.verify_setup(Messages(a)) is False
    assert any(lv == "warn" and "not importable" in t for lv, t in a.log)


def test_package_vendor_dir_copies_and_never_ships_pro_packages(tmp_path):
    v = tmp_path / "vendor"
    for rel in ("wwnet_native/__init__.py", "wwnet_native/wwnet_native.pyd", "anyio/__init__.py",
                "certifi/cacert.pem", "wwnet_native/__pycache__/x.pyc"):
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text("x")
    z = package.build(tmp_path / "out", vendor_dir=v)
    names = set(zipfile.ZipFile(z).namelist())
    top = package.TOP + "/vendor/"
    assert {top + "wwnet_native/__init__.py", top + "wwnet_native/wwnet_native.pyd"} <= names
    assert not [n for n in names if n.startswith(top) and any(f"/{p}" in n for p in ("anyio", "certifi", "__pycache__"))]
    assert not any(n.startswith(top) for n in set(zipfile.ZipFile(package.build(tmp_path / "o2")).namelist()))


def test_bundle_has_no_assistant_or_credential_code(tmp_path):
    names = zipfile.ZipFile(package.build(tmp_path / "out")).namelist()
    for gone in ("dpapi.py", "settings.py", "assistant_run.py", "arcpy_tools.py", "install.py"):
        assert not any(n.endswith("/" + gone) for n in names), gone
    assert not any("/assistant/" in n for n in names)
    zf = zipfile.ZipFile(package.build(tmp_path / "o4"))
    for n in zf.namelist():
        if n.endswith((".py", ".pyt")):
            text = zf.read(n).decode("utf-8")
            for token in ("CryptUnprotectData", "CryptProtectData", "windll", "subprocess", "anthropic"):
                assert token not in text, f"{n}: {token}"


def test_vendor_module_importable_after_pro_resets_sys_path(tmp_path, clean, monkeypatch):
    """Regression: Pro restores sys.path between loading the .pyt and running a tool, which
    dropped the vendor folder (wwnet_native 'not found' on a real PC). activate() at the start of
    each run must make vendored modules importable again."""
    root = make_root(tmp_path)
    (root / "vendor" / "wwnet_probe_mod.py").write_text("X = 1\n")
    saved = list(sys.path)
    vendor.activate(root)                       # .pyt load
    sys.path[:] = saved                         # Pro resets sys.path before execute()
    monkeypatch.delitem(sys.modules, "wwnet_probe_mod", raising=False)
    with pytest.raises(ImportError):
        __import__("wwnet_probe_mod")
    vendor.activate(root)                       # execute() re-activates
    assert __import__("wwnet_probe_mod").X == 1
    assert vendor.report()["_vendor"] == str(root / "vendor")


def test_every_pyt_execute_reactivates_vendor():
    """Every tool runs through the base class execute(), which re-activates the vendor folder first
    (Pro restores sys.path before each run) and reloads the modules; only Update WWNet overrides it."""
    src = (TOOLBOX / "WWNet.pyt").read_text()
    assert src.count("def execute(self, parameters, messages):") == 2   # base class + Update WWNet
    assert src.count("def execute(self, parameters, messages):\n        _vendor.activate(_HERE)") == 1
    assert src.count('"""Not the base execute: the update must not import (and so lock) the modules it replaces."""\n'
                     '        _vendor.activate(_HERE)') == 1
    assert "impl = _impl()" in src and src.count("(_Tool):") == 24
