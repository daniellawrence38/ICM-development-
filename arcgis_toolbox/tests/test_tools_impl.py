import importlib.machinery
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

import fake_arcpy as fa
from wwnet_arcgis import capacity, gpkg, maplayers, symbology, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]


RES_NAME = "Design results \u2014 demo"


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
                              "wwn_pump_stations", "wwn_nodes", "wwn_node_loads", "wwn_pumps")]
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
    assert issues == []                      # P05 falls into N06 (normal): no INVERT_MISMATCH any more
    assert any("0 error(s), 0 warning(s), 0 info" in t for _, t in a.log)
    assert a.inserted == []
    assert ("table", str(tmp_path / "o.gdb"), "issues") in a.added


def test_report_issues_routes_severity(fake_arcpy):
    a = fake_arcpy([])
    iss = [dict(severity="error", code="E", object_type="node", object_id="1", clause="", message="m"),
           dict(severity="warning", code="W", object_type="node", object_id="2", clause="c", message="m")]
    c = tools_impl.report_issues(Messages(a), iss)
    assert c == {"error": 1, "warning": 1, "info": 0}
    # error-severity data issues are warnings with an ERROR: prefix: AddError would fail the tool in Pro
    assert a.log == [("warn", "ERROR: E node 1: m"), ("warn", "W node 2 (c): m")]
    assert not [lv for lv, _ in a.log if lv == "error"]


def test_run_design_selects_failed(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    # make P02 fail by flattening it below DN150's 1:200 limit
    con = sqlite3.connect(demo_gpkg)
    con.execute("update wwn_conduits set ds_invert = us_invert - 0.1 where conduit_id='P02'")
    con.commit()
    con.close()
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, add_table=True, select_failed=True, exe=wwnet_exe)
    assert d["summary"]["fail"] >= 1
    sel = {n: (m, w) for n, m, w in a.selections}
    assert "'P02'" in sel["Gravity sewers"][1] and sel["Gravity sewers"][0] == "NEW_SELECTION"
    assert "'P02'" in sel[RES_NAME][1]
    assert not a.joins
    assert any(k == "data" and p.endswith("main.wwn_results_conduits") for k, p in a.added)
    text = "\n".join(t for _, t in a.log)
    assert "P02" in text and "FAIL" in text and "Design run: 11 conduit(s)" in text


def test_run_design_all_pass_clears_selection(fake_arcpy, wwnet_exe, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, add_table=False, select_failed=True, exe=wwnet_exe)
    assert all(m == "CLEAR_SELECTION" for _, m, _ in a.selections) and len(a.selections) == 2
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
    assert labels == ["New Model", "Import Sewer Data", "Import Pressure Data", "Set Pump Station Data", "Import Diurnal Profile", "Add Model To Map", "Check Model", "Join Dangling Sewer Ends", "Run Design", "Run Simulation", "Trace",
                      "Pipe Capacity", "Export To ICM", "Set Assistant API Key", "Ask WWNet Assistant", "Verify Setup", "Self Test", "Update WWNet"]
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


def test_run_design_prints_table(fake_arcpy, wwnet_exe, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    text = [t for _, t in a.log]
    hdr = next(t for t in text if t.startswith("Conduit"))
    for col in ("DN", "Grade 1:x", "GSDF L/s", "Pumped", "Total L/s", "Cap L/s", "T4.4", "Hyd", "d/D", "v m/s", "RESULT",
                "Worst message"):
        assert col in hdr
    c = d["conduit_results"][0]
    row = next(t for t in text if t.startswith(c["conduit_id"] + " "))
    assert f"{c['capacity_lps']:.1f}" in row and "PASS" in row
    assert sum(1 for t in text if t.startswith(("P0", "P1")) and "PASS" in t) >= 1
    assert text.index(hdr) > next(i for i, t in enumerate(text) if t.startswith("Design run"))


def test_results_table_cap():
    rows = [dict(conduit_id=f"C{i}", dn=150, result="pass") for i in range(5)]
    lines = tools_impl.results_table_lines(rows, max_lines=3)
    assert len(lines) == 2 + 3 + 1 and lines[-1] == "... 2 more"


def test_run_design_results_layer(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    fc = a.memory["memory\\wwnet_results_demo"]
    assert fc["geom"] == "POLYLINE" and fc["sr"].factoryCode == 7850
    assert [n for n, _ in fc["fields"]] == [f for f, _ in tools_impl.RESULT_FIELDS]
    assert dict(fc["fields"])["dn"] == "LONG" and dict(fc["fields"])["message"] == "TEXT"
    assert len(fc["rows"]) == 11
    by = {r["conduit_id"]: r for r in fc["rows"]}
    for c in d["conduit_results"]:
        r = by[c["conduit_id"]]
        assert r["dn"] == c["dn"] and r["result"] == c["result"]
        assert r["total_lps"] == c["total_lps"] and r["capacity_lps"] == c["capacity_lps"]
        assert r["grade_1in"] == c["grade_1in"] and r["velocity_mps"] == c["velocity_mps"]
        assert r["SHAPE@"]
    assert [l.name for l in layers].count(RES_NAME) == 1
    assert any("Results layer added: " + RES_NAME + " (temporary" in t for _, t in a.log)


def test_run_design_replaces_existing_layer(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    assert [l.name for l in layers].count(RES_NAME) == 1
    assert a.removed == [RES_NAME] and a.deleted == ["memory\\wwnet_results_demo"]
    assert len(a.memory["memory\\wwnet_results_demo"]["rows"]) == 11


def test_run_design_symbology_failure_only_warns(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)    # fake layers have no .symbology -> styling raises
    tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    assert any(lv == "warn" and "Symbology for results" in t for lv, t in a.log)
    assert any("Results layer added" in t for _, t in a.log)


def test_run_design_show_layer_false(fake_arcpy, wwnet_exe, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, show_layer=False, exe=wwnet_exe)
    assert not a.memory and len(layers) == 2
    assert not any("Results layer" in t for _, t in a.log)


def test_design_detail_lines_cap_and_exclude_info():
    def chk(rule, res):
        return dict(rule=rule, clause="DS50 x", result=res, message="msg")
    rows = [dict(conduit_id=f"C{i}", dn=150, result="fail", checks=[chk("max_flow", "fail"),
                                                                    chk("cover_not_checked", "info")])
            for i in range(8)]
    lines = tools_impl.design_detail_lines(rows)
    assert len(lines) == 6 and lines[-1] == "... 3 more max_flow item(s)"
    assert not any("cover_not_checked" in t for t in lines)
    assert tools_impl.design_info_lines({"summary": {"cover_not_checked_ends": 34}}) == [
        "Cover not checked at 34 conduit end(s): no surface level (IS ends / junctions)."]
    assert tools_impl.design_info_lines({"summary": {}}) == []


# ---------------------------------------------------------------- profiles / loads (schema v2)

PROFILE = "Time\tFactor\n" + "".join(f"{i * 5 // 60:02d}:{i * 5 % 60:02d}\t{0.5 if i < 144 else 1.5}\n"
                                      for i in range(288))


def test_import_profile_tool_file_and_paste(fake_arcpy, tmp_path, demo_gpkg):
    a = fake_arcpy([])
    csv = tmp_path / "p.txt"
    csv.write_text(PROFILE)
    m = Messages(a)
    r = tools_impl.import_profile(m, demo_gpkg, "RES", str(csv), "desc", True, False)
    assert r["points"] == 288 and r["interval_min"] == 5 and abs(r["mean_factor"] - 1) < 1e-9
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select count(*) from wwn_profiles where profile_id='RES'").fetchone() == (288,)
    assert con.execute("select value from wwn_meta where key='default_profile'").fetchone() == ("RES",)
    assert con.execute("select description, interval_min from wwn_profile_info").fetchone() == ("desc", 5)
    con.close()
    assert any("Imported profile RES" in t for _, t in a.log)
    # pasted text, mean far from 1 -> warning; normalise fixes it
    r = tools_impl.import_profile(m, demo_gpkg, "P2", None, "", False, False, pasted="0\t2\n720\t4\n")
    assert any(lv == "warn" and "mean factor" in t for lv, t in a.log)
    r = tools_impl.import_profile(m, demo_gpkg, "P2", None, "", False, True, pasted="0\t2\n720\t4\n")
    assert r["normalised"] and abs(r["mean_factor"] - 1) < 1e-9
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select value from wwn_meta where key='default_profile'").fetchone() == ("RES",)
    con.close()


def test_import_profile_tool_errors(fake_arcpy, tmp_path, demo_gpkg):
    a = fake_arcpy([])
    m = Messages(a)
    with pytest.raises(RuntimeError, match="profile id"):
        tools_impl.import_profile(m, demo_gpkg, " ", None, pasted="0\t1\n")
    with pytest.raises(RuntimeError, match="Choose a CSV"):
        tools_impl.import_profile(m, demo_gpkg, "X")
    with pytest.raises(RuntimeError, match="not found"):
        tools_impl.import_profile(m, demo_gpkg, "X", str(tmp_path / "nope.csv"))
    with pytest.raises(Exception, match="outside"):
        tools_impl.import_profile(m, demo_gpkg, "X", None, pasted="0\t1\n24:00\t1\n")


def test_import_profile_cli_fallback(fake_arcpy, no_native, wwnet_exe, tmp_path, demo_gpkg):
    a = fake_arcpy([])
    r = tools_impl.import_profile(Messages(a), demo_gpkg, "RES", None, "d", True, False, pasted=PROFILE,
                                  exe=wwnet_exe)
    assert r["points"] == 288 and r["is_default"] is True


def test_pyt_import_profile_tool(fake_arcpy, tmp_path, demo_gpkg):
    import importlib.machinery
    import importlib.util
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_prof", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_prof", loader))
    loader.exec_module(mod)
    t = mod.ImportDiurnalProfile()
    assert t.category == "Data" and t.label == "Import Diurnal Profile"
    params = t.getParameterInfo()
    assert [p.name for p in params] == ["gpkg", "profile_id", "csv", "description", "set_default", "normalise",
                                        "pasted"]
    csv = tmp_path / "p.csv"
    csv.write_text(PROFILE)
    params[0].value, params[1].value, params[2].value = demo_gpkg, "RES", str(csv)
    t.execute(params, None)
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select count(*) from wwn_profiles").fetchone() == (288,)
    con.close()


def test_run_design_reports_adwf_and_results_field(fake_arcpy, demo_gpkg):
    a = fake_arcpy([])
    con = sqlite3.connect(demo_gpkg)
    node = con.execute("select us_node_id from wwn_conduits order by fid limit 1").fetchone()[0]
    con.close()
    from wwnet_arcgis import gpkg as g
    g.upgrade_model(demo_gpkg)
    con = sqlite3.connect(demo_gpkg)
    con.execute("insert into wwn_node_loads (load_id,node_id,connections,source) values ('L1',?,86400,'manual')",
                (node,))
    con.commit()
    con.close()
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, show_layer=False, select_failed=False)
    assert d["summary"]["total_local_adwf_lps"] == pytest.approx(630.0)
    assert any(t.startswith("Loads: ADWF 630.000 L/s") for _, t in a.log)
    assert "ADWF L/s" in tools_impl.results_table_lines(d["conduit_results"])[0]
    assert ("adwf_lps", "DOUBLE") in tools_impl.RESULT_FIELDS


def test_add_model_upgrades_v1_file(fake_arcpy, demo_gpkg):
    a = fake_arcpy([])
    con = sqlite3.connect(demo_gpkg)
    cols = [r[1] for r in con.execute("pragma table_info(wwn_subcatchments)")]
    con.close()
    tools_impl.add_model_to_map(a, Messages(a), demo_gpkg)
    con = sqlite3.connect(demo_gpkg)
    after = [r[1] for r in con.execute("pragma table_info(wwn_subcatchments)")]
    assert "adwf_lps" in after and "profile_id" in after
    assert con.execute("select value from wwn_meta where key='schema_version'").fetchone() == ("2",)
    con.close()
    assert any("node loads" in t.lower() for _, t in a.log)
    assert cols is not None


# ------------------------------------------------------------------ Run Simulation

SIM_NAME = "Simulation results — demo"
NODES_NAME = "Simulation nodes — demo"


def _fake_sim(flood=False):
    links = [dict(conduit_id="P01", dn=150, max_flow_lps=1.0, max_d_over_d=0.3, max_velocity_mps=0.5,
                  surcharged_min=0.0, time_of_max_flow="07:30"),
             dict(conduit_id="P02", dn=150, max_flow_lps=9.0, max_d_over_d=0.6, max_velocity_mps=0.9,
                  surcharged_min=0.0, time_of_max_flow="08:00"),
             dict(conduit_id="P03", dn=225, max_flow_lps=30.0, max_d_over_d=0.9, max_velocity_mps=1.2,
                  surcharged_min=0.0, time_of_max_flow="08:05"),
             dict(conduit_id="P04", dn=225, max_flow_lps=40.0, max_d_over_d=0.8, max_velocity_mps=1.2,
                  surcharged_min=15.0, time_of_max_flow="08:05")]
    nodes = [dict(node_id="N01", max_hgl_m=1.0, max_depth_m=1.0, flood_volume_m3=2.5 if flood else 0.0,
                  surcharged_min=30.0)]
    return dict(run_id="2026-01-01T00:00:00Z", summary=dict(
        inflow_adwf_lps=1.0, inflow_simulated_lps=1.5, peak_outlet_flow_lps=3.0, continuity_error_pct=-0.1,
        conduits=4, nodes=1, surcharged_conduits=1, flooded_nodes=1 if flood else 0,
        total_flood_volume_m3=2.5 if flood else 0.0), links=links, nodes=nodes, warnings=["a warning"])


def test_sim_class_bands():
    assert [symbology.sim_class(d, s) for d, s in ((0.2, 0), (0.5, 0), (0.74, 0), (0.75, 0), (0.99, 0),
                                                   (1.0, 0), (0.3, 5))] == \
        ["low", "medium", "medium", "high", "high", "surcharged", "surcharged"]
    assert "L/s d/D" in symbology.SIM_LABEL_ARCADE


def test_run_simulation_layers_and_messages(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    calls = []
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: calls.append(args) or _fake_sim())
    styled = []
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: styled.append(t) or True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, 12.0, 6.0, "ks", 0.014, 1.5, True, "out.inp")
    assert calls[0][1:7] == (12.0, 6.0, "ks", 0.014, 1.5, "out.inp")
    fc = a.memory["memory\\wwnet_sim_demo"]
    assert fc["geom"] == "POLYLINE" and [n for n, _ in fc["fields"]] == [f for f, _ in tools_impl.SIM_FIELDS]
    by = {r["conduit_id"]: r for r in fc["rows"]}
    assert by["P03"]["d_class"] == "high" and by["P04"]["d_class"] == "surcharged"
    assert by["P01"]["d_class"] == "low" and by["P02"]["d_class"] == "medium"
    assert [l.name for l in layers].count(SIM_NAME) == 1
    assert styled == ["sim"]
    assert not any(NODES_NAME == l.name for l in layers)       # nothing flooded -> no nodes layer
    text = [t for _, t in a.log]
    assert any(t.startswith("Conduit") and "Surch min" in t for t in text)
    assert any("continuity error" in t for t in text) and any("surcharged" in t for t in text)
    assert ("warn", "a warning") in a.log and any("out.inp" in t for t in text)
    rows = [t for t in text if t.startswith("P0")]
    assert rows[0].startswith("P03") and rows[1].startswith("P04")        # sorted by d/D desc


def test_run_simulation_flooding_nodes_layer(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_sim(flood=True))
    styled = []
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: styled.append(t) or True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    assert styled == ["sim", "sim_nodes"]
    nfc = a.memory["memory\\wwnet_simnodes_demo"]
    assert nfc["geom"] == "POINT" and [r["node_id"] for r in nfc["rows"]] == ["N01"]
    assert NODES_NAME in [l.name for l in layers]
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)       # re-run replaces both layers
    assert [l.name for l in layers].count(SIM_NAME) == 1 and [l.name for l in layers].count(NODES_NAME) == 1


def test_run_simulation_real_engine_and_inp_export(fake_arcpy, wwnet_exe, demo_gpkg, no_native, tmp_path):
    a, layers = mk(fake_arcpy, demo_gpkg)
    inp = str(tmp_path / "model.inp")
    d = tools_impl.run_simulation(a, Messages(a), demo_gpkg, 12.0, 6.0, "manning", 0.013, 1.0, True, inp,
                                  exe=wwnet_exe)
    assert d["summary"]["conduits"] == 11 and abs(d["summary"]["continuity_error_pct"]) < 1.0
    assert "[CONDUITS]" in open(inp).read()
    assert len(a.memory["memory\\wwnet_sim_demo"]["rows"]) == 11
    assert [l.name for l in layers].count(SIM_NAME) == 1
    assert any(lv == "warn" and "Symbology for sim" in t for lv, t in a.log)      # fake layer: warn only
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select count(*) from wwn_sim_links").fetchone() == (11,)
    con.close()


def test_run_simulation_show_layer_false(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_sim())
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, show_layer=False)
    assert not a.memory and len(layers) == 2


def test_run_simulation_tool_parameters(fake_arcpy):
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_sim", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_sim", loader))
    loader.exec_module(mod)
    t = mod.RunSimulation()
    assert t.category == "Analysis"
    p = {x.name: x for x in t.getParameterInfo()}
    assert list(p) == ["gpkg", "duration_h", "warmup_h", "roughness", "manning_n", "flow_scale", "show_layer",
                       "export_inp"]
    assert p["duration_h"].value == 48.0 and p["warmup_h"].value == 24.0 and p["manning_n"].value == 0.013
    assert p["roughness"].filter.list == ["manning", "ks"] and p["show_layer"].value is True
    assert p["export_inp"].direction == "Output" and p["export_inp"].parameterType == "Optional"


class _ProBasemapLayer:
    """Mimics real Pro layers (basemaps, some service layers) where reading an unsupported
    property raises: "The attribute 'name' is not supported on this instance of Layer"."""
    isGroupLayer = False
    isFeatureLayer = False

    def supports(self, prop):
        return False

    def __getattr__(self, item):
        if item in ("name", "dataSource"):
            raise NameError(f"The attribute '{item}' is not supported on this instance of Layer.")
        raise AttributeError(item)


def test_run_simulation_tolerates_layers_without_name(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    layers.insert(0, _ProBasemapLayer())
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_sim())
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, 12.0, 6.0, "manning", 0.013, 1.0, True, None)
    assert [maplayers.layer_name(l) for l in layers].count(SIM_NAME) == 1
    assert not any("Could not add the simulation layers" in t for _, t in a.log)
    # second run replaces the layer, still tolerating the odd layer
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, 12.0, 6.0, "manning", 0.013, 1.0, True, None)
    assert [maplayers.layer_name(l) for l in layers].count(SIM_NAME) == 1


def test_trace_from_dataset_path_uses_map_layer_or_warns(fake_arcpy, wwnet_exe, demo_gpkg):
    """Real Pro: the Nodes parameter can arrive as 'C:\\...\\m.gpkg\\main.wwn_nodes' (a path, not a layer)."""
    import types
    a, layers = mk(fake_arcpy, demo_gpkg)
    layers.insert(0, _ProBasemapLayer())
    stand_in = types.SimpleNamespace(name="main.wwn_nodes", dataSource=gpkg.table_path(demo_gpkg, "wwn_nodes"))
    r = tools_impl.trace(a, Messages(a), stand_in, "N03", "Downstream", True, wwnet_exe)
    assert "OUT1" in r["nodes"]
    assert [s[0] for s in a.selections] == ["Nodes", "Gravity sewers"]     # selected on the MAP layers
    # model not on the map: trace still runs, warns instead of selecting
    layers[:] = [_ProBasemapLayer()]
    a.selections.clear()
    r = tools_impl.trace(a, Messages(a), stand_in, "N03", "Upstream", True, wwnet_exe)
    assert a.selections == [] and any("not in the active map" in t for lv, t in a.log if lv == "warn")


def test_pyt_layer_obj_accepts_path_and_skips_unsupported_layers(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    layers.insert(0, _ProBasemapLayer())
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_lo", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_lo", loader))
    loader.exec_module(mod)
    p = fa.FakeParameter(name="nodes", datatype="GPFeatureLayer")
    p.value = gpkg.table_path(demo_gpkg, "wwn_nodes")          # a path string, like Pro passes
    got = mod._layer_obj(p)
    assert maplayers.layer_name(got) == "Nodes"               # matched to the map layer by data source
    p.value = "Nodes"
    assert maplayers.layer_name(mod._layer_obj(p)) == "Nodes"
    layers[:] = [_ProBasemapLayer()]
    p.value = gpkg.table_path(demo_gpkg, "wwn_nodes")
    assert mod._layer_obj(p).dataSource.endswith("wwn_nodes")  # stand-in when not on the map


def pro_sqlite_source(gpkg_path, table):
    """dataSource as real Pro reports a SQLite-family layer (shape copied from a mobile-gdb layer
    on the user's PC: connection properties, then the dataset after the last separator)."""
    return (f"AUTHENTICATION_MODE=OSA;main;DB_CONNECTION_PROPERTIES={gpkg_path};"
            f"INSTANCE=sde:sqlite:{gpkg_path};IS_GEODATABASE=false;IS_NOSERVER=0;SERVER=C:\\main.{table}")


def test_split_datasource_pro_connection_string():
    p = r"C:\Users\x\Desktop\WWNet\models\Dunkley Ave PS4.gpkg"
    assert gpkg.split_datasource(pro_sqlite_source(p, "wwn_nodes")) == (p, "wwn_nodes")
    assert gpkg.dataset_path(pro_sqlite_source(p, "wwn_conduits")) == gpkg.table_path(p, "wwn_conduits")
    # the real mobile-gdb string from the user's map is not a GeoPackage
    real = (r"AUTHENTICATION_MODE=OSA;main;DB_CONNECTION_PROPERTIES=C:\Users\u\Desktop\Shapefiles\WWLine.geodatabase;"
            r"INSTANCE=sde:sqlite:C:\Users\u\Desktop\Shapefiles\WWLine.geodatabase;IS_GEODATABASE=true;"
            r"IS_NOSERVER=0;SERVER=C:\main.WWProjects")
    assert gpkg.split_datasource(real) == (None, None) and gpkg.dataset_path(real) == real
    # only the file is given: path but no table
    assert gpkg.split_datasource(f"AUTHENTICATION_MODE=OSA;DATABASE={p};SERVER=C:") == (p, None)


def test_trace_with_pro_connection_string_layers(fake_arcpy, wwnet_exe, demo_gpkg):
    """Open bug from 2026-11-20: Trace reported 'not in the active map' because Pro's GeoPackage
    dataSource is a connection string, not '<file>.gpkg\\main.<table>'."""
    layers = [fa.make_layer("Nodes", pro_sqlite_source(demo_gpkg, "wwn_nodes")),
              fa.make_layer("Gravity sewers", pro_sqlite_source(demo_gpkg, "wwn_conduits"))]
    a = fake_arcpy(layers)
    layers.insert(0, _ProBasemapLayer())
    assert "N06" in tools_impl.node_ids(a, layers[1])
    r = tools_impl.trace(a, Messages(a), layers[1], "N03", "Downstream", True, wwnet_exe)
    assert "OUT1" in r["nodes"]
    assert [s[0] for s in a.selections] == ["Nodes", "Gravity sewers"]
    assert not any("not in the active map" in t for _, t in a.log)


def pro_gpkg_source(gpkg_path, table):
    """dataSource exactly as real Pro 3.x reports a GeoPackage layer (pasted from the user's PC)."""
    return f"Instance={gpkg_path},Dataset=main.%{table}"


def test_split_datasource_real_pro_gpkg_string():
    real = r"Instance=C:\Users\lawrend0\Desktop\WWNet\models\import_test.gpkg,Dataset=main.%wwn_nodes"
    p = r"C:\Users\lawrend0\Desktop\WWNet\models\import_test.gpkg"
    assert gpkg.split_datasource(real) == (p, "wwn_nodes")
    assert gpkg.dataset_path(real) == gpkg.table_path(p, "wwn_nodes")
    sp = r"C:\a, b\my model.GPKG"   # comma and space in the folder
    assert gpkg.split_datasource(pro_gpkg_source(sp, "wwn_conduits")) == (sp, "wwn_conduits")


def test_trace_and_find_layer_with_real_pro_gpkg_layers(fake_arcpy, wwnet_exe, demo_gpkg):
    layers = [fa.make_layer("Nodes", pro_gpkg_source(demo_gpkg, "wwn_nodes")),
              fa.make_layer("Gravity sewers", pro_gpkg_source(demo_gpkg, "wwn_conduits"))]
    a = fake_arcpy(layers)
    layers.insert(0, _ProBasemapLayer())
    assert maplayers.find_layer_for(a, demo_gpkg, "wwn_conduits") is layers[2]
    r = tools_impl.trace(a, Messages(a), layers[1], "N03", "Downstream", True, wwnet_exe)
    assert "OUT1" in r["nodes"]
    assert [s[0] for s in a.selections] == ["Nodes", "Gravity sewers"]
    assert not any("not in the active map" in t for _, t in a.log)


def test_remove_layers_named_removes_table_via_removetable(fake_arcpy, demo_gpkg):
    a = fake_arcpy([])
    m = a.mp.ArcGISProject("CURRENT").activeMap
    tbl = m.addDataFromPath(gpkg.table_path(demo_gpkg, "wwn_node_loads"))
    maplayers.set_layer_name(tbl, "Node loads")
    lyr = m.addDataFromPath(gpkg.table_path(demo_gpkg, "wwn_nodes"))
    maplayers.set_layer_name(lyr, "Node loads")
    assert m.listTables() == [tbl]
    assert maplayers.remove_layers_named(a, "Node loads") == 2
    assert m.listTables() == [] and a.tables == [] and m.listLayers() == []
    assert a.removed == ["Node loads", "Node loads"]
    assert maplayers.remove_layers_named(a, "Node loads") == 0


# ------------------------------------------------------------------ pump results

PUMP_NAME = "Simulation pump stations — demo"


def _add_station(gp, ps_id="PS1", node="N01", **cols):
    con = sqlite3.connect(gp)
    geom = con.execute("SELECT geom FROM wwn_nodes WHERE node_id=?", (node,)).fetchone()[0]
    con.execute("UPDATE wwn_nodes SET invert_level=18.0 WHERE node_id=?", (node,))
    cols = {"start_level": 19.5, "stop_level": 18.5, "overflow_level": 20.5, **cols}
    con.execute(f"INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id, {', '.join(cols)}) "
                f"VALUES (?, ?, ?, {', '.join('?' * len(cols))})", (geom, ps_id, node, *cols.values()))
    con.commit()
    con.close()


def _fake_pump_sim(overflow=0.0, sph=4.0, v=1.0):
    d = _fake_sim()
    d["pumps"] = [dict(pump_id="PS1_P1", ps_id="PS1", role="duty", max_flow_lps=28.5, starts=40,
                       starts_per_hour=sph, run_time_pct=12.5),
                  dict(pump_id="PS1_P2", ps_id="PS1", role="assist", max_flow_lps=27.0, starts=10,
                       starts_per_hour=2.0, run_time_pct=3.0)]
    d["pump_stations"] = [dict(ps_id="PS1", wet_well_node_id="N01", max_level_m=19.62, max_depth_m=1.62,
                               overflow_volume_m3=overflow, time_of_max_level="08:10")]
    d["rising_mains"] = [dict(rm_id="RM1", ps_id="PS1", length_m=420.0, diameter_mm=150.0,
                              max_flow_lps=28.5, max_velocity_mps=v)]
    return d


def test_pump_results_messages_and_layer(fake_arcpy, demo_gpkg, monkeypatch):
    _add_station(demo_gpkg)
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_pump_sim())
    styled = []
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: styled.append(t) or True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    text = [t for _, t in a.log]
    i_cond = max(i for i, t in enumerate(text) if t.startswith("P0"))
    i_ps = text.index("Pump stations (levels m AHD)")
    assert i_ps > i_cond
    row = next(t for t in text if t.startswith("PS1 ") and "19.62" in t)
    for tok in ("19.50", "18.50", "20.50", "0.00", "08:10"):
        assert tok in row
    assert any(t.startswith("PS1_P1") and "40" in t and "4.0" in t and "12.5" in t and "28.50" in t for t in text)
    assert any(t.startswith("RM1") and "150" in t and "420.0" in t and "1.00" in t for t in text)
    assert any(t.startswith("Pump flags:") and "10" in t and "0.7" in t and "2" in t for t in text)
    assert not any(lv == "warn" and t.startswith("PS1:") for lv, t in a.log)     # nothing flagged
    assert styled == ["sim", "sim_pumps"]
    fc = a.memory["memory\\wwnet_simpumps_demo"]
    assert fc["geom"] == "POINT" and [n for n, _ in fc["fields"]] == [f for f, _ in tools_impl.SIM_PUMP_FIELDS]
    r = fc["rows"][0]
    assert r["ps_id"] == "PS1" and r["max_level_m"] == 19.62 and r["overflow_m3"] == 0.0
    assert r["starts_per_hour"] == 4.0 and r["run_time_pct"] == 12.5     # max over its pumps
    assert r["rm_max_velocity_mps"] == 1.0 and r["flag"] == "ok"
    assert [l.name for l in layers].count(PUMP_NAME) == 1
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)                  # re-run replaces the layer
    assert [l.name for l in layers].count(PUMP_NAME) == 1


def test_pump_flags(fake_arcpy, demo_gpkg, monkeypatch):
    _add_station(demo_gpkg)
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate",
                        lambda *args: _fake_pump_sim(overflow=3.25, sph=12.0, v=0.5))
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    warns = [t for lv, t in a.log if lv == "warn" and t.startswith("PS1:")]
    assert any("wet well overflow 3.25 m3" in w for w in warns)
    assert any("PS1_P1" in w and "12.0 starts/h" in w and "motor size" in w for w in warns)
    assert any("RM1" in w and "0.50 m/s < 0.7" in w and "sediment" in w for w in warns)
    assert not any("PS1_P2" in w for w in warns)
    r = a.memory["memory\\wwnet_simpumps_demo"]["rows"][0]
    assert r["flag"] == "overflow,starts,v_low" and r["overflow_m3"] == 3.25
    # high velocity
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_pump_sim(v=2.4))
    a.log.clear()
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    assert any("2.40 m/s > 2" in t and "surge" in t for lv, t in a.log if lv == "warn")
    assert a.memory["memory\\wwnet_simpumps_demo"]["rows"][0]["flag"] == "v_high"
    assert (tools_impl.MAX_STARTS_PER_HOUR, tools_impl.MIN_RM_VELOCITY, tools_impl.MAX_RM_VELOCITY) == (10, 0.7, 2.0)


def test_no_pump_layer_without_stations_or_show_layer(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_sim())
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    assert not any(t.startswith("Pump") for _, t in a.log) and PUMP_NAME not in [l.name for l in layers]
    _add_station(demo_gpkg)
    monkeypatch.setattr(tools_impl.cli, "run_simulate", lambda *args: _fake_pump_sim())
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, show_layer=False)
    assert PUMP_NAME not in [l.name for l in layers]
    assert "Pump stations (levels m AHD)" in [t for _, t in a.log]        # messages regardless of layer


def test_style_sim_pumps_symbology():
    class Sym:
        def __init__(self):
            self.renderer = type("R", (), {"fields": None})()
        def updateRenderer(self, name):
            self.name = name
    seen = {}
    def colour(v):
        return v
    class L:
        showLabels = False
        symbology = Sym()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(symbology, "_unique", lambda layer, field, fn, size=None, width=None:
                   seen.update(field=field, ok=fn("ok"), bad=fn("overflow,starts"), size=size))
        lc = type("LC", (), {})()
        L.listLabelClasses = lambda self: [lc]
        symbology.style_sim_pumps(L())
    assert seen["field"] == "flag" and seen["ok"] == symbology.SIM_COLOURS["low"]
    assert seen["bad"] == symbology.SIM_COLOURS["surcharged"] and lc.expression == "$feature.ps_id"


def test_run_simulation_real_engine_pump_station(fake_arcpy, demo_gpkg, tmp_path):
    from wwnet_arcgis import pumpdata
    con = sqlite3.connect(demo_gpkg)
    geom = con.execute("SELECT geom FROM wwn_nodes WHERE node_id='OUT1'").fetchone()[0]
    for nid, typ, inv, sf in (("WW", "wet_well", 22.2, 25.5), ("D", "ac", 25.0, 28.0)):
        con.execute("INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
                    "VALUES (?,?,?,?,?)", (geom, nid, typ, inv, sf))
    con.execute("UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'")
    con.execute("INSERT INTO wwn_conduits (conduit_id, us_node_id, ds_node_id, dn, length_m, us_invert, ds_invert)"
                " VALUES ('PSG1','D','OUT1',225,50.0,25.0,22.2)")
    con.execute("INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id, discharge_node_id, rising_main_id)"
                " VALUES (?, 'PS1','WW','D','RM1')", (geom,))
    con.execute("INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, roughness_type, "
                "roughness) VALUES ('RM1','PS1','D',150,200.0,'ks',1.0)")
    con.commit()
    con.close()
    pumpdata.set_station(demo_gpkg, "PS1", wet_well_area_m2=1.0, stop=22.6, start=23.0, overflow=25.0,
                         curve=[(0, 20), (10, 16.5), (20, 11.5), (30, 5)])
    a, layers = mk(fake_arcpy, demo_gpkg)
    d = tools_impl.run_simulation(a, Messages(a), demo_gpkg, 6.0, 2.0)
    assert d["pumps"] and d["pumps"][0]["starts"] > 0 and d["rising_mains"][0]["rm_id"] == "RM1"
    text = [t for _, t in a.log]
    assert "Pump stations (levels m AHD)" in text and "Rising mains" in text
    assert [l.name for l in layers].count(PUMP_NAME) == 1
    assert a.memory["memory\\wwnet_simpumps_demo"]["rows"][0]["ps_id"] == "PS1"
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select count(*) from wwn_sim_pump_stations").fetchone() == (1,)
    con.close()


def test_check_model_prints_station_report(fake_arcpy, wwnet_exe, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.check_model(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    assert not any("Pump station data" in t for _, t in a.log)           # no stations -> silent
    _add_station(demo_gpkg)
    tools_impl.check_model(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    assert ("info", "Pump station data: PS1: missing wet_well_area_m2, pumps") in a.log


def test_set_pump_station_data_messages(fake_arcpy, demo_gpkg):
    _add_station(demo_gpkg, ps_id="PS1")
    _add_station(demo_gpkg, ps_id="PS2", node="N02")
    a = fake_arcpy([])
    r = tools_impl.set_pump_station_data(
        a, Messages(a), demo_gpkg, "PS1", wet_well_area_m2=7.0, level_mode="AHD", n_duty=2,
        curve_text="Flow\tHead\n10\t30\n20\t10", curve_order="flow_head", flow_units="L/s",
        source_note="Data sheet rev C")
    assert r["pumps"] == ["PS1_P1", "PS1_P2"]
    assert any(lv == "info" and t.startswith("PS1: wet well 7.00 m2") for lv, t in a.log)
    assert any(lv == "warn" and "not zero flow" in t for lv, t in a.log)
    assert ("info", "PS1: pump data complete") in a.log and ("info", "PS2: missing wet_well_area_m2, pumps") in a.log
    a.log.clear()
    assert tools_impl.set_pump_station_data(a, Messages(a), demo_gpkg, "PS1", stop=19.9, start=19.5) is None
    assert a.log == [("warn", "ERROR: Stop level must be below start level.")]
    a.log.clear()
    assert tools_impl.set_pump_station_data(a, Messages(a), demo_gpkg, "PS1", curve_text="10 30\n20 40") is None
    assert a.log[0][0] == "warn" and a.log[0][1].startswith("ERROR: Pump curve head must strictly decrease")
    assert not any(lv == "error" for lv, _ in a.log)


def test_set_pump_station_data_locked(fake_arcpy, demo_gpkg):
    _add_station(demo_gpkg)
    a = fake_arcpy([])
    con = sqlite3.connect(demo_gpkg, isolation_level=None)
    con.execute("BEGIN EXCLUSIVE")
    try:
        import wwnet_arcgis.pumpdata as pd
        orig = pd._connect
        pd._connect = lambda g: _short_timeout(g)
        try:
            assert tools_impl.set_pump_station_data(a, Messages(a), demo_gpkg, "PS1", wet_well_area_m2=5.0) is None
        finally:
            pd._connect = orig
    finally:
        con.execute("ROLLBACK")
        con.close()
    assert len(a.log) == 1 and a.log[0][0] == "warn" and a.log[0][1].startswith("ERROR: ")
    assert "locked by ArcGIS Pro" in a.log[0][1] and "attribute tables" in a.log[0][1]


def _short_timeout(g):
    con = sqlite3.connect(g, isolation_level=None, timeout=0.05)
    con.row_factory = sqlite3.Row
    return con
