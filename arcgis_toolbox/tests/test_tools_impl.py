import importlib.machinery
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

import fake_arcpy as fa
from wwnet_arcgis import capacity, gpkg, symbology, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]


def mk(fake_arcpy, gpkg_path):
    layers = [fa.make_layer("Nodes", gpkg.table_path(gpkg_path, "wwn_nodes")),
              fa.make_layer("Gravity sewers", gpkg.table_path(gpkg_path, "wwn_conduits"))]
    return fake_arcpy(layers), layers


def test_new_model_creates_valid_gpkg_and_adds_layers(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    out = tmp_path / "m" / "new"
    out.parent.mkdir()
    path = tools_impl.new_model(a, Messages(a), str(out), True)
    assert path.endswith(".gpkg") and os.path.isfile(path)
    con = sqlite3.connect(path)
    assert con.execute("select value from wwn_meta where key='crs'").fetchone() == ("EPSG:7850",)
    con.close()
    added = [p for k, p in a.added if k == "data"]
    assert [os.path.basename(p) for p in added] == [
        f"main.{t}" for t in ("wwn_subcatchments", "wwn_rising_mains", "wwn_conduits",
                              "wwn_pump_stations", "wwn_nodes")]
    warns = [t for lv, t in a.log if lv == "warn"]
    assert any("Symbology" in w for w in warns)        # fake layers have no symbology -> warn, no raise
    with pytest.raises(FileExistsError):
        tools_impl.new_model(a, Messages(a), path, False)
    tools_impl.new_model(a, Messages(a), path, False, overwrite=True)


def test_symbology_failure_only_warns():
    log = []
    m = type("M", (), {"warn": lambda s, t: log.append(t)})()
    assert symbology.apply("wwn_nodes", object(), m) is False and "wwn_nodes" in log[0]
    assert symbology.apply("wwn_pumps", object(), m) is False
    assert "DN" in symbology.CONDUIT_LABEL_ARCADE and '" 1:"' in symbology.CONDUIT_LABEL_ARCADE


def test_add_model_rejects_non_wwnet_file(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    p = tmp_path / "x.gpkg"
    con = sqlite3.connect(p)
    con.execute("create table gpkg_contents (table_name, identifier, data_type)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="no WWNet"):
        tools_impl.add_model_to_map(a, Messages(a), str(p))


def test_check_model_messages_and_table(fake_arcpy, wwnet_exe, demo_gpkg, tmp_path):
    a, _ = mk(fake_arcpy, demo_gpkg)
    issues = tools_impl.check_model(a, Messages(a), demo_gpkg, str(tmp_path / "o.gdb" / "issues"), wwnet_exe)
    assert len(issues) == 1
    assert ("info", "INVERT_MISMATCH conduit P05: ds invert 23.550 is 0.300 m above invert 23.250 of node 'N06'") in a.log
    assert any("0 error(s), 0 warning(s), 1 info" in t for _, t in a.log)
    assert len(a.inserted) == 1 and a.inserted[0][1] == "INVERT_MISMATCH"
    assert ("table", str(tmp_path / "o.gdb"), "issues") in a.added


def test_report_issues_routes_severity(fake_arcpy):
    a = fake_arcpy([])
    iss = [dict(severity="error", code="E", object_type="node", object_id="1", clause="", message="m"),
           dict(severity="warning", code="W", object_type="node", object_id="2", clause="c", message="m")]
    c = tools_impl.report_issues(Messages(a), iss)
    assert c == {"error": 1, "warning": 1, "info": 0}
    assert [lv for lv, _ in a.log] == ["error", "warn"]


def test_run_design_selects_failed(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    # make P02 fail by flattening it below DN150's 1:200 limit
    con = sqlite3.connect(demo_gpkg)
    con.execute("update wwn_conduits set ds_invert = us_invert - 0.1 where conduit_id='P02'")
    con.commit()
    con.close()
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, add_table=True, join=True, select_failed=True,
                              exe=wwnet_exe)
    assert d["summary"]["fail"] >= 1
    assert a.selections and a.selections[0][0] == "Gravity sewers" and "'P02'" in a.selections[0][2]
    assert a.selections[0][1] == "NEW_SELECTION"
    assert a.joins and a.joins[0][1] == "conduit_id"
    assert any(k == "data" and p.endswith("main.wwn_results_conduits") for k, p in a.added)
    text = "\n".join(t for _, t in a.log)
    assert "P02" in text and "FAIL" in text and "Design run: 11 conduit(s)" in text


def test_run_design_all_pass_clears_selection(fake_arcpy, wwnet_exe, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, add_table=False, join=False, select_failed=True,
                          exe=wwnet_exe)
    assert a.selections[0][1] == "CLEAR_SELECTION"
    assert any("No failed conduits" in t for _, t in a.log)


def test_trace_selects_nodes_and_conduits(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    ids = tools_impl.node_ids(a, layers[0])
    assert "N06" in ids and ids == sorted(ids)
    r = tools_impl.trace(a, Messages(a), layers[0], "N03", "Downstream", True, wwnet_exe)
    assert set(r["nodes"]) == {"N03", "N04", "N05", "N06", "N07", "N08", "OUT1"}
    assert r["conduits"] == ["P03", "P04", "P05", "P06", "P07", "P08"] or len(r["conduits"]) == 6
    names = [s[0] for s in a.selections]
    assert names == ["Nodes", "Gravity sewers"]
    assert "'OUT1'" in a.selections[0][2] and "\"conduit_id\" IN" in a.selections[1][2]
    with pytest.raises(RuntimeError, match="Nodes"):
        tools_impl.trace(a, Messages(a), layers[1], "N03", "Both", True, wwnet_exe)


def test_in_clause_chunks():
    c = gpkg.in_clause('"x"', [str(i) for i in range(1000)], chunk=400)
    assert c.startswith("(") and c.count(" OR ") == 2 and gpkg.in_clause('"x"', []) == "1=0"
    assert gpkg.in_clause('"x"', ["a'b"]) == "\"x\" IN ('a''b')"
    assert gpkg.split_datasource(r"C:\a b\m.GPKG\main.wwn_nodes") == (r"C:\a b\m.GPKG", "wwn_nodes")
    assert gpkg.split_datasource("/t/m.gpkg/wwn_nodes")[1] == "wwn_nodes"
    assert gpkg.split_datasource("C:/x/y.shp") == (None, None)


def test_pipe_capacity(fake_arcpy):
    a = fake_arcpy([])
    r = tools_impl.pipe_capacity(Messages(a), 225, 300, 1.0, 5.0)
    assert 15 < r["full_flow_lps"] < 60 and r["depth_ratio_at_flow"] < 0.5
    assert r["ds50_checks"]["results"]
    text = "\n".join(t for _, t in a.log)
    assert "Full-bore capacity" in text and "DS 50 checks" in text and "d/D" in text
    r2 = capacity.calculate(150, 100, None, 1000.0)           # far beyond capacity -> surcharge
    assert r2["depth_ratio_at_flow"] is None
    assert any("surcharge" in l for l in capacity.describe(r2))


def test_pyt_compiles_and_defines_tools(fake_arcpy):
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt", str(TOOLBOX / "WWNet.pyt"))
    spec = importlib.util.spec_from_loader("wwnet_pyt", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    tb = mod.Toolbox()
    labels = [t().label for t in tb.tools]
    assert labels == ["New Model", "Add Model To Map", "Check Model", "Run Design", "Trace",
                      "Pipe Capacity", "Set Assistant API Key", "Ask WWNet Assistant"]
    for t in tb.tools:
        params = t().getParameterInfo()
        assert params and all(p.name and p.datatype for p in params)
    key = mod.SetAssistantApiKey().getParameterInfo()[0]
    assert key.datatype == "GPEncryptedString"
    ask = {p.name: p for p in mod.AskWWNetAssistant().getParameterInfo()}
    assert ask["model"].value == "claude-opus-5-5" and ask["model"].filter.list == mod.MODELS
    assert ask["apply"].value is False and ask["reset"].value is False
    assert ask["question"].controlCLSID and ask["layer"].datatype == "GPFeatureLayer"
    tr = mod.Trace().getParameterInfo()
    assert tr[1].filter.type == "ValueList"
