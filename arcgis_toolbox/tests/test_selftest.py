"""Self Test: isolated steps, one report, never msgs.error."""
import os
import shutil

import fake_arcpy as fa
import importlib
import sys


def _mods():
    # other tests purge wwnet_arcgis from sys.modules; always take the live copies
    return (importlib.import_module("wwnet_arcgis.selftest"), importlib.import_module("wwnet_arcgis.tools_impl"))


class Rec:
    def __init__(self):
        self.calls = []

    def info(self, t):
        self.calls.append(("info", t))

    def warn(self, t):
        self.calls.append(("warn", t))

    def error(self, t):
        self.calls.append(("error", t))


def _root(tmp_path):
    from conftest import TOOLBOX
    return str(TOOLBOX)


def _by(res):
    return {s["name"]: s for s in res["steps"]}


def test_selftest_report_and_steps(fake_arcpy, wwnet_exe, tmp_path):
    selftest, tools_impl = _mods()
    a = fake_arcpy([])
    m = Rec()
    out = tmp_path / "st"
    res = selftest.run(a, m, _root(tmp_path), out_dir=str(out), exe=wwnet_exe)
    assert os.path.isfile(res["report"]) and res["out_dir"] == str(out)
    assert os.path.isfile(out / "selftest.gpkg")
    steps = _by(res)
    assert all(s["status"] in ("PASS", "WARN", "FAIL", "SKIP") for s in res["steps"])
    for name in ("check model", "run design", "trace", "pipe capacity", "import diurnal profile", "export to ICM"):
        assert steps[name]["status"] in ("PASS", "WARN"), (name, steps[name])
    assert steps["pump station simulation"]["status"] in ("PASS", "WARN"), steps["pump station simulation"]
    assert steps["export to ICM (pump station)"]["status"] in ("PASS", "WARN"), steps["export to ICM (pump station)"]
    imp = steps["import pump station data (demo register)"]
    assert imp["status"] == "PASS", imp          # preview wrote nothing; floor / levels / rate written; curve kept
    names = [s["name"] for s in res["steps"]]
    assert names.index("pump station simulation") < names.index("import pump station data (demo register)") \
        < names.index("export to ICM (pump station)")
    text = open(res["report"], encoding="utf-8").read()
    assert "Pump stations (levels m AHD)" in text and "Rising mains" in text
    assert "existing pump curve kept" in text and "Preview wrote nothing" in text
    assert "Summary" in text and "PASS" in text and "check model" in text
    assert not any(lv == "error" for lv, _ in m.calls)
    assert any(t.startswith("Report: ") for _, t in m.calls)
    assert any("passed" in t and "skipped" in t for _, t in m.calls)


def test_failure_does_not_stop_later_steps(fake_arcpy, wwnet_exe, tmp_path, monkeypatch):
    selftest, tools_impl = _mods()
    a = fake_arcpy([])

    def boom(*args, **kw):
        raise RuntimeError("synthetic design failure")
    monkeypatch.setattr(tools_impl, "run_design", boom)
    m = Rec()
    res = selftest.run(a, m, _root(tmp_path), out_dir=str(tmp_path / "st"), exe=wwnet_exe)
    steps = _by(res)
    assert steps["run design"]["status"] == "FAIL" and "synthetic design failure" in steps["run design"]["error"]
    assert steps["trace"]["status"] in ("PASS", "WARN")
    assert steps["export to ICM"]["status"] in ("PASS", "WARN")
    text = open(res["report"], encoding="utf-8").read()
    assert "Traceback" in text and "synthetic design failure" in text
    assert ("warn", "[FAIL] run design") == (m.calls[[c[1].startswith("[FAIL]") for c in m.calls].index(True)][0],
                                              "[FAIL] run design")
    assert not any(lv == "error" for lv, _ in m.calls)


def test_error_lines_make_step_fail(fake_arcpy, tmp_path, monkeypatch):
    selftest, tools_impl = _mods()
    a = fake_arcpy([])
    monkeypatch.setattr(tools_impl, "pipe_capacity", lambda msgs, *x, **k: msgs.error("bad"))
    res = selftest.run(a, Rec(), _root(tmp_path), out_dir=str(tmp_path / "st"))
    assert _by(res)["pipe capacity"]["status"] == "FAIL"


def test_missing_demo_fails_copy_and_skips_dependents(fake_arcpy, tmp_path):
    selftest, tools_impl = _mods()
    a = fake_arcpy([])
    empty = tmp_path / "empty" / "tb"
    empty.mkdir(parents=True)
    res = selftest.run(a, Rec(), str(empty), out_dir=str(tmp_path / "st"))
    steps = _by(res)
    assert steps["copy demo model"]["status"] == "FAIL"
    assert steps["check model"]["status"] == "SKIP" and steps["pipe capacity"]["status"] == "PASS"
    assert os.path.isfile(res["report"])


def test_include_map_adds_and_removes_layers(fake_arcpy, wwnet_exe, tmp_path):
    selftest, tools_impl = _mods()
    layers = []
    a = fake_arcpy(layers)
    m = Rec()
    res = selftest.run(a, m, _root(tmp_path), out_dir=str(tmp_path / "st"), include_map=True, exe=wwnet_exe)
    steps = _by(res)
    assert steps["map: add model to map"]["status"] in ("PASS", "WARN")
    assert steps["map: trace with selection"]["status"] in ("PASS", "WARN")
    assert steps["map: remove added layers"]["status"] in ("PASS", "WARN")
    assert any(k == "data" for k, *_ in a.added) and a.removed
    assert layers == []
    assert a.selections
    # the node loads table is a standalone table: it must be removed via removeTable, not left behind
    assert any(p.endswith("main.wwn_node_loads") for k, p in a.added if k == "data")
    assert a.tables == []
    # every item the run added (layers and the table) was removed successfully, so the cleanup count
    # covers the table too (a.removed only records successful removeLayer / removeTable calls)
    added_items = [p for k, p in a.added if k == "data"]
    assert len(a.removed) == len(added_items)


def test_default_out_dir_is_temp(fake_arcpy, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    selftest, tools_impl = _mods()
    a = fake_arcpy([])
    res = selftest.run(a, Rec(), _root(tmp_path))
    assert os.path.basename(res["out_dir"]).startswith("wwnet_selftest_")
    assert os.path.dirname(res["out_dir"]) == str(tmp_path)
