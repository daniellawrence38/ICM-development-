"""No-install bundle: vendor.activate / report / typing_extensions swap / truststore / bundle smoke test."""
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

import package
from wwnet_arcgis import tools_impl, vendor
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean(monkeypatch):
    """Isolate sys.path / sys.modules / module state touched by vendor."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(vendor, "NOTES", [])
    monkeypatch.setattr(vendor, "_TRUST", {"done": False, "note": ""})
    monkeypatch.setitem(sys.modules, "typing_extensions", sys.modules["typing_extensions"])


def make_root(tmp_path, te_source=None):
    v = tmp_path / "root" / "vendor"
    v.mkdir(parents=True)
    if te_source is not None:
        (v / "typing_extensions.py").write_text(te_source)
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


def test_typing_extensions_swapped_when_old(tmp_path, clean):
    old = types.ModuleType("typing_extensions")
    old.__version__ = "4.9.0"
    sys.modules["typing_extensions"] = old
    root = make_root(tmp_path, "__version__ = '4.16.0'\nMARK = 'vendored'\n")
    logged = []
    vendor.activate(root, logged.append)
    new = sys.modules["typing_extensions"]
    assert new is not old and new.MARK == "vendored"
    assert Path(new.__file__) == root / "vendor" / "typing_extensions.py"
    assert logged and "typing_extensions" in logged[0] and vendor.NOTES == logged


def test_typing_extensions_old_without_version_uses_feature_probe(tmp_path, clean):
    sys.modules["typing_extensions"] = types.ModuleType("typing_extensions")   # no version, no Sentinel
    root = make_root(tmp_path, "Sentinel = object()\nMARK = 'vendored'\n")
    vendor.activate(root)
    assert sys.modules["typing_extensions"].MARK == "vendored"


def test_typing_extensions_kept_when_new_enough_or_no_vendored_copy(tmp_path, clean):
    cur = types.ModuleType("typing_extensions")
    cur.__version__ = "4.14.0"
    sys.modules["typing_extensions"] = cur
    vendor.activate(make_root(tmp_path, "__version__ = '4.16.0'\n"))
    assert sys.modules["typing_extensions"] is cur
    old = types.ModuleType("typing_extensions")
    old.__version__ = "4.9.0"
    sys.modules["typing_extensions"] = old
    vendor.activate(make_root(tmp_path / "b"))           # vendor dir, no typing_extensions.py
    assert sys.modules["typing_extensions"] is old


def test_typing_extensions_kept_when_vendored_copy_broken_or_old(tmp_path, clean):
    old = types.ModuleType("typing_extensions")
    old.__version__ = "4.9.0"
    sys.modules["typing_extensions"] = old
    vendor.activate(make_root(tmp_path, "raise RuntimeError('boom')\n"))
    assert sys.modules["typing_extensions"] is old
    vendor.activate(make_root(tmp_path / "b", "__version__ = '4.10.0'\n"))
    assert sys.modules["typing_extensions"] is old


def test_report_structure():
    rep = vendor.report()
    for name, _ in vendor.PACKAGES:
        ver, loc = rep[name]
        assert (ver is None) == (loc is None)
    assert rep["typing_extensions"][0]
    assert sys.version.split()[0] in rep["_python"]
    for k in ("_import_anthropic", "_import_wwnet_native"):
        ok, err = rep[k]
        assert isinstance(ok, bool) and (ok or err)
    lines = vendor.format_report(rep)
    assert any(l.startswith("import anthropic:") for l in lines)


def test_report_reports_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "wwnet_native", None)   # makes `import wwnet_native` fail
    ok, err = vendor.report()["_import_wwnet_native"]
    assert not ok and "wwnet_native" in err


def test_inject_truststore_once_and_only_without_httpcore2(monkeypatch, clean):
    calls = []
    fake = types.ModuleType("truststore")
    fake.inject_into_ssl = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    real_find = vendor.importlib.util.find_spec
    monkeypatch.setattr(vendor.importlib.util, "find_spec",
                        lambda n, *a: None if n == "httpcore2" else real_find(n, *a))
    note = vendor.inject_truststore()
    vendor.inject_truststore()
    assert calls == [1] and "injected" in note


def test_inject_truststore_skipped_when_httpcore2_handles_it(monkeypatch, clean):
    calls = []
    fake = types.ModuleType("truststore")
    fake.inject_into_ssl = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    real_find = vendor.importlib.util.find_spec
    monkeypatch.setattr(vendor.importlib.util, "find_spec",
                        lambda n, *a: object() if n == "httpcore2" else real_find(n, *a))
    assert "no injection needed" in vendor.inject_truststore()
    assert calls == []


# ---------------------------------------------------------------- Verify Setup tool

def test_verify_setup_report_only(fake_arcpy):
    a = fake_arcpy()
    tools_impl.verify_setup(Messages(a), live=False)
    text = "\n".join(t for _, t in a.log)
    assert "Python" in text and "import anthropic" in text and "Live API test skipped" in text


def test_verify_setup_live_ok_and_never_prints_key(fake_arcpy, monkeypatch):
    secret = "sk-ant-SECRET-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    seen = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                return types.SimpleNamespace(model=kw["model"])

    a = fake_arcpy()
    monkeypatch.setattr(vendor, "inject_truststore", lambda *x: "TLS note")
    tools_impl.verify_setup(Messages(a), live=True, client_factory=lambda k: (seen.setdefault("k", k), Client)[1])
    text = "\n".join(t for _, t in a.log)
    assert "Live API test OK" in text and secret not in text
    assert seen["max_tokens"] <= 32 and seen["k"] == secret


def test_verify_setup_live_failure_is_friendly(fake_arcpy, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-123")
    monkeypatch.setattr(vendor, "inject_truststore", lambda *x: "")

    def boom(_):
        raise RuntimeError("connection exploded")
    a = fake_arcpy()
    assert tools_impl.verify_setup(Messages(a), live=True, client_factory=boom) is False
    text = "\n".join(t for _, t in a.log)
    assert "FAILED" in text and "SECRET" not in text


def test_verify_setup_live_without_key(fake_arcpy):
    a = fake_arcpy()
    assert tools_impl.verify_setup(Messages(a), live=True) is False
    assert any(lvl == "error" and "No Anthropic API key" in t for lvl, t in a.log)


# ---------------------------------------------------------------- packaging

def test_package_vendor_dir_copies_and_never_ships_pro_packages(tmp_path):
    v = tmp_path / "vendor"
    for rel in ("anthropic/__init__.py", "typing_extensions.py", "anyio/__init__.py", "anyio-4.1.dist-info/RECORD",
                "sniffio/__init__.py", "idna/__init__.py", "idna-3.4.dist-info/METADATA",
                "certifi/cacert.pem", "certifi-2024.dist-info/METADATA", "pydantic/__pycache__/x.pyc"):
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text("x")
    z = package.build(tmp_path / "out", vendor_dir=v)
    names = set(zipfile.ZipFile(z).namelist())
    top = package.TOP + "/vendor/"
    assert {top + "anthropic/__init__.py", top + "typing_extensions.py"} <= names
    assert not [n for n in names if n.startswith(top) and any(
        f"/{p}" in n for p in ("anyio", "sniffio", "idna", "certifi", "__pycache__"))]
    assert "WWNet-ArcGIS/wwnet_arcgis/vendor.py" in names
    assert not any(n.startswith(top) for n in set(zipfile.ZipFile(package.build(tmp_path / "o2")).namelist()))


# ---------------------------------------------------------------- slow: real pip + "Pro-like" venv

def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, **kw)


@pytest.mark.slow
def test_bundle_smoke_in_pro_like_venv(tmp_path):
    """Vendor dir built with pip --target, extracted bundle, venv pinned like ArcGIS Pro's Python."""
    vend = tmp_path / "vendor"
    r = _run([sys.executable, "-m", "pip", "install", "--target", str(vend), "--only-binary=:all:",
              "--quiet", "anthropic"])
    if r.returncode:
        pytest.skip(f"pip/network unavailable: {r.stderr[-200:]}")
    native = False
    try:
        import wwnet_native
        import shutil
        shutil.copytree(Path(wwnet_native.__file__).parent, vend / "wwnet_native")
        native = True
    except Exception:  # noqa: BLE001
        pass
    venv = tmp_path / "pro-venv"
    assert _run([sys.executable, "-m", "venv", str(venv)]).returncode == 0
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    r = _run([str(py), "-m", "pip", "install", "--quiet", "anyio==4.2.0", "typing_extensions==4.9.0",
              "sniffio==1.3.0", "idna==3.4", "certifi==2024.2.2"])
    if r.returncode:
        pytest.skip(f"pip/network unavailable: {r.stderr[-200:]}")
    z = package.build(tmp_path / "out", vendor_dir=vend)
    zipfile.ZipFile(z).extractall(tmp_path / "ex")
    bundle = tmp_path / "ex" / package.TOP
    assert not (bundle / "vendor" / "anyio").exists()
    site = str(next((venv / "lib").glob("python*/site-packages"))) if sys.platform != "win32" \
        else str(venv / "Lib" / "site-packages")
    script = TOOLBOX / "tests" / "bundle_smoke.py"
    r = _run([str(py), str(script), str(bundle), site, "native" if native else "no"], cwd=tmp_path)
    assert r.returncode == 0 and "SMOKE-OK" in r.stdout, r.stdout + r.stderr


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
    src = (TOOLBOX / "WWNet.pyt").read_text()
    n_exec = src.count("def execute(self, parameters, messages):")
    assert n_exec == 11
    assert src.count("def execute(self, parameters, messages):\n        _vendor.activate(_HERE)") == n_exec
