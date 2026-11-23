"""updater: zip apply (flat / nested), backups, zip-slip, locked files -> .pending, purge_modules."""
import io
import pathlib
import sys
import types
import zipfile

import pytest

from wwnet_arcgis import updater


def make_zip(path, files, prefix=""):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(prefix + name, data)
    return path


def nested_zip(path, inner_files, prefix="WWNet-ArcGIS/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in inner_files.items():
            z.writestr(prefix + name, data)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("wwnet-arcgis-update.zip", buf.getvalue())
    return path


FILES = {"WWNet.pyt": "pyt v2", "wwnet_arcgis/__init__.py": '__version__ = "9.9.1"\n',
         "wwnet_arcgis/a.py": "A = 1\n", "wwnet_arcgis/__pycache__/a.cpython-311.pyc": "junk"}


def test_flat_zip(tmp_path):
    root = tmp_path / "root"
    z = make_zip(tmp_path / "u.zip", FILES)
    res = updater.apply_update(z, root, log=lambda m: None)
    assert sorted(res["updated"]) == ["WWNet.pyt", "wwnet_arcgis/__init__.py", "wwnet_arcgis/a.py"]
    assert res["unchanged"] == 0 and res["pending"] == [] and res["backup"] is None
    assert res["version"] == "9.9.1"
    assert (root / "wwnet_arcgis" / "a.py").read_text() == "A = 1\n"
    assert not (root / "wwnet_arcgis" / "__pycache__").exists()


def test_nested_zip_with_prefix(tmp_path):
    root = tmp_path / "root"
    z = nested_zip(tmp_path / "outer.zip", FILES)
    res = updater.apply_update(z, root, log=lambda m: None)
    assert (root / "WWNet.pyt").read_text() == "pyt v2"
    assert "WWNet-ArcGIS/WWNet.pyt" not in res["updated"] and "WWNet.pyt" in res["updated"]


def test_common_top_folder(tmp_path):
    root = tmp_path / "root"
    z = make_zip(tmp_path / "u.zip", FILES, prefix="build/")
    updater.apply_update(z, root, log=lambda m: None)
    assert (root / "WWNet.pyt").is_file() and not (root / "build").exists()


def test_unchanged_and_backup_only_for_changed(tmp_path):
    root = tmp_path / "root"
    updater.apply_update(make_zip(tmp_path / "u1.zip", FILES), root, log=lambda m: None)
    files2 = dict(FILES, **{"wwnet_arcgis/a.py": "A = 2\n"})
    res = updater.apply_update(make_zip(tmp_path / "u2.zip", files2), root, log=lambda m: None)
    assert res["updated"] == ["wwnet_arcgis/a.py"] and res["unchanged"] == 2
    backup = pathlib.Path(res["backup"])
    assert (backup / "wwnet_arcgis" / "a.py").read_text() == "A = 1\n"
    assert [p.name for p in backup.rglob("*") if p.is_file()] == ["a.py"]
    assert backup.parent.name == ".update_backup"


def test_zip_slip_rejected(tmp_path):
    root = tmp_path / "root"
    files = dict(FILES, **{"../evil.py": "x", "wwnet_arcgis/../../evil2.py": "x", "/abs.py": "x",
                           "C:/win.py": "x"})
    logs = []
    res = updater.apply_update(make_zip(tmp_path / "u.zip", files), root, log=logs.append)
    assert len(res["skipped"]) == 4 and len(logs) == 4
    assert not (tmp_path / "evil.py").exists() and not (tmp_path / "evil2.py").exists()
    assert not (root / "abs.py").exists()


def test_locked_file_staged_as_pending_and_applied(tmp_path, monkeypatch):
    root = tmp_path / "root"
    files = dict(FILES, **{"vendor/wwnet_native/wwnet_native.pyd": "new-binary"})
    (root / "vendor" / "wwnet_native").mkdir(parents=True)
    old = root / "vendor" / "wwnet_native" / "wwnet_native.pyd"
    old.write_text("old-binary")
    real = pathlib.Path.write_bytes

    def locked(self, data):
        if self.name == "wwnet_native.pyd":
            raise PermissionError("locked")
        return real(self, data)
    monkeypatch.setattr(pathlib.Path, "write_bytes", locked)
    res = updater.apply_update(make_zip(tmp_path / "u.zip", files), root, log=lambda m: None)
    monkeypatch.undo()
    assert res["pending"] == ["vendor/wwnet_native/wwnet_native.pyd"]
    assert old.read_text() == "old-binary"
    assert (old.with_name("wwnet_native.pyd.pending")).read_text() == "new-binary"
    assert updater.apply_pending(root) == ["vendor/wwnet_native/wwnet_native.pyd"]
    assert old.read_text() == "new-binary" and not old.with_name("wwnet_native.pyd.pending").exists()
    assert updater.apply_pending(root) == []


def test_apply_pending_leaves_locked_file(tmp_path, monkeypatch):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "x.pyd").write_text("old")
    (tmp_path / "vendor" / "x.pyd.pending").write_text("new")

    def boom(a, b):
        raise PermissionError("locked")
    monkeypatch.setattr(updater.os, "replace", boom)
    assert updater.apply_pending(tmp_path) == []
    assert (tmp_path / "vendor" / "x.pyd.pending").exists()


def test_wrong_zip_raises(tmp_path):
    z = make_zip(tmp_path / "bad.zip", {"readme.txt": "hi", "other/file.py": "x"})
    with pytest.raises(ValueError, match="WWNet update"):
        updater.apply_update(z, tmp_path / "root", log=lambda m: None)


def test_purge_modules_keeps_vendor(monkeypatch):
    names = ["wwnet_arcgis.zz_fake", "wwnet_arcgis.zz_fake.sub", "wwnet_shared.zz_fake", "wwnet_sharedx"]
    for n in names:
        monkeypatch.setitem(sys.modules, n, types.ModuleType(n))
    vend = sys.modules.get("wwnet_arcgis.vendor")
    removed = updater.purge_modules()
    assert {"wwnet_arcgis.zz_fake", "wwnet_arcgis.zz_fake.sub", "wwnet_shared.zz_fake"} <= set(removed)
    assert "wwnet_arcgis.vendor" not in removed
    assert sys.modules.get("wwnet_arcgis.vendor") is vend
    assert "wwnet_sharedx" in sys.modules and "wwnet_arcgis.zz_fake" not in sys.modules


def test_purge_then_fresh_impl_roundtrip(monkeypatch):
    saved = dict(sys.modules)
    try:
        mod = updater.fresh_impl()
        assert mod.__name__ == "wwnet_arcgis.tools_impl" and hasattr(mod, "Messages")
    finally:
        for k in [k for k in sys.modules if k not in saved]:
            if k.startswith(("wwnet_arcgis", "wwnet_shared")):
                del sys.modules[k]
        sys.modules.update(saved)
