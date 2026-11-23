import csv
import importlib.machinery
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

import fake_arcpy as fa
from conftest import rows
from wwnet_arcgis import capacity, gpkg, maplayers, symbology, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]
pytest.importorskip("wwnet_native")


def mk(fake_arcpy, gpkg_path, sel_nodes=None, sel_conduits=None):
    layers = [fa.make_layer("Nodes", gpkg.table_path(gpkg_path, "wwn_nodes"), selection=sel_nodes),
              fa.make_layer("Gravity sewers", gpkg.table_path(gpkg_path, "wwn_conduits"), selection=sel_conduits)]
    return fake_arcpy(layers), layers


def text(a, level=None):
    return "\n".join(t for lv, t in a.log if level is None or lv == level)


def names(layers):
    return [maplayers.layer_name(lyr) for lyr in layers]


def load_pyt(name="wwnet_pyt"):
    loader = importlib.machinery.SourceFileLoader(name, str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


def fid_of(path, table, idf, value):
    return rows(path, f"select fid from {table} where {idf}=?", value)[0][0]


# ---------------------------------------------------------------- model tools

def test_new_model_creates_v3_gpkg_and_adds_layers(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    out = tmp_path / "m" / "new"
    out.parent.mkdir()
    path = tools_impl.new_model(a, Messages(a), str(out), True)
    assert path.endswith(".gpkg") and os.path.isfile(path)
    assert rows(path, "select value from wwn_meta where key='schema_version'") == [("3",)]
    added = [os.path.basename(p) for k, p in a.added if k == "data"]
    # empty result layers are not added
    assert added == [f"main.{t}" for t in ("wwn_subcatchments", "wwn_rising_mains", "wwn_conduits",
                                           "wwn_pump_stations", "wwn_nodes", "wwn_node_loads")]
    assert "Symbology" in text(a, "warn")        # fake layers have no symbology -> warn, no raise
    with pytest.raises(FileExistsError):
        tools_impl.new_model(a, Messages(a), path, False)
    tools_impl.new_model(a, Messages(a), path, False, overwrite=True)


def test_symbology_failure_only_warns():
    log = []
    m = type("M", (), {"warn": lambda s, t: log.append(t)})()
    assert symbology.apply("wwn_nodes", object(), m) is False and "wwn_nodes" in log[0]
    assert symbology.apply("wwn_pumps", object(), m) is False
    assert "DN" in symbology.CONDUIT_LABEL_ARCADE and '" 1:"' in symbology.CONDUIT_LABEL_ARCADE
    assert set(symbology._STYLERS) >= {"wwn_results_conduits", "wwn_sim_conduit_results", "wwn_issues",
                                       "wwn_sim_compare", "wwn_subcatchments"}


def test_add_model_rejects_non_wwnet_file(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    p = tmp_path / "x.gpkg"
    con = sqlite3.connect(p)
    con.execute("create table gpkg_contents (table_name, identifier, data_type)")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="no WWNet"):
        tools_impl.add_model_to_map(a, Messages(a), str(p))


def test_add_model_upgrades_old_file_and_skips_layers_already_in_map(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    con = sqlite3.connect(demo_gpkg)
    con.execute("update wwn_meta set value='2' where key='schema_version'")
    con.commit()
    con.close()
    tools_impl.add_model_to_map(a, Messages(a), demo_gpkg)
    assert "Upgraded the model" in text(a)
    assert rows(demo_gpkg, "select value from wwn_meta where key='schema_version'") == [("3",)]
    assert names(layers).count("Nodes") == 1                 # already in the map: not added twice


def test_model_resolution_from_map(fake_arcpy, demo_gpkg, tmp_path):
    a, layers = mk(fake_arcpy, demo_gpkg)
    assert maplayers.same_file(maplayers.default_model(a), demo_gpkg)
    assert maplayers.resolve_model(a, None) == maplayers.default_model(a)
    assert maplayers.resolve_model(a, gpkg.table_path(demo_gpkg, "wwn_nodes")) == demo_gpkg
    assert maplayers.resolve_model(a, layers[0]) == demo_gpkg
    with pytest.raises(RuntimeError, match="not a WWNet"):
        maplayers.resolve_model(a, "C:/x/y.shp")
    a2 = fake_arcpy([])
    assert maplayers.default_model(a2) is None
    with pytest.raises(RuntimeError, match="no WWNet model is in the active map"):
        maplayers.resolve_model(a2, "")


def test_layer_source_from_connection_properties():
    lyr = fa.make_layer("Nodes", "")
    lyr.connectionProperties = {"workspace_factory": "Sql", "dataset": "main.wwn_nodes",
                                "connection_info": {"database": r"C:\m\a.gpkg"}}
    assert maplayers.layer_model_table(lyr) == (r"C:\m\a.gpkg", "wwn_nodes")
    assert gpkg.split_datasource("/t/a.gpkg|layername=wwn_conduits") == ("/t/a.gpkg", "wwn_conduits")


# ---------------------------------------------------------------- check / design

def test_check_model_messages_and_issues_layer(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    issues = tools_impl.check_model(a, Messages(a), demo_gpkg)
    assert issues == []                      # P05 falls into N06 (normal): no INVERT_MISMATCH any more
    assert "Check finished: 0 error(s), 0 warning(s), 0 info." in text(a)
    assert "save any edits" in text(a)
    assert "Issues" in names(layers)
    tools_impl.check_model(a, Messages(a), demo_gpkg)
    assert names(layers).count("Issues") == 1                # reused, not added again


def test_report_issues_routes_severity(fake_arcpy):
    a = fake_arcpy([])
    iss = [dict(severity="warning", code="W", object_type="node", object_id="2", clause="c", message="m"),
           dict(severity="error", code="E", object_type="node", object_id="1", clause="", message="m")]
    c = tools_impl.report_issues(Messages(a), iss)
    assert c == {"error": 1, "warning": 1, "info": 0}
    # errors first; error-severity data issues are warnings with an ERROR: prefix (AddError fails the tool)
    assert a.log == [("warn", "ERROR: E node 1: m"), ("warn", "W node 2 (c): m")]


def _fail_p02(path):
    con = sqlite3.connect(path)
    con.execute("update wwn_conduits set ds_invert = us_invert - 0.1 where conduit_id='P02'")
    con.commit()
    con.close()


def test_run_design_persistent_layer_and_selects_failed_once(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    _fail_p02(demo_gpkg)
    d = tools_impl.run_design(a, Messages(a), demo_gpkg)
    assert d["summary"]["fail"] >= 1
    assert names(layers).count("Design results") == 1
    sel = [(n, m, w) for n, m, w in a.selections]
    assert len(sel) == 1 and sel[0][0] == "Design results" and "'P02'" in sel[0][2]   # B4: once
    assert "Design: 11 conduit(s)" in text(a) and "P02 DN150" in text(a, "warn") and "FAIL" in text(a, "warn")
    assert not any(t.startswith("Conduit ") for _, t in a.log)          # no fixed-width table
    stored = rows(demo_gpkg, "select count(*), sum(geom is not null) from wwn_results_conduits")
    assert stored == [(11, 11)]
    tools_impl.run_design(a, Messages(a), demo_gpkg)
    assert names(layers).count("Design results") == 1                 # reused after the first run
    assert rows(demo_gpkg, "select count(*) from wwn_issues where source='design'") == \
        [(len([i for i in d["issues"]]),)]


def test_run_design_all_pass_clears_selection(fake_arcpy, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, select_failed=True)
    assert [m for _, m, _ in a.selections] == ["CLEAR_SELECTION"]
    assert "No failed conduits" in text(a)


def test_run_design_without_layer(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, show_layer=False)
    assert len(layers) == 2 and not a.selections


def test_run_design_reports_adwf(fake_arcpy, demo_gpkg):
    a = fake_arcpy([])
    node = rows(demo_gpkg, "select us_node_id from wwn_conduits order by fid limit 1")[0][0]
    con = sqlite3.connect(demo_gpkg)
    con.execute("insert into wwn_node_loads (load_id,node_id,connections,source) values ('L1',?,86400,'manual')",
                (node,))
    con.commit()
    con.close()
    d = tools_impl.run_design(a, Messages(a), demo_gpkg, show_layer=False)
    assert d["summary"]["total_local_adwf_lps"] == pytest.approx(630.0)
    assert "Loads: ADWF 630.000 L/s" in text(a)
    assert rows(demo_gpkg, "select max(adwf_lps) from wwn_results_conduits")[0][0] == pytest.approx(630.0)


# ---------------------------------------------------------------- trace

def test_trace_from_id_selects_nodes_and_conduits(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    r = tools_impl.trace(a, Messages(a), demo_gpkg, "N03", "Downstream", True)
    assert set(r["nodes"]) == {"N03", "N04", "N05", "N06", "N07", "N08", "OUT1"}
    assert sorted(r["conduits"]) == ["P03", "P04", "P05", "P06", "P07", "P08"]
    assert [s[0] for s in a.selections] == ["Nodes", "Gravity sewers"]
    assert "'OUT1'" in a.selections[0][2] and "\"conduit_id\" IN" in a.selections[1][2]
    assert "Upstream of node N03" in text(a) and "Flow at node N03" in text(a)


def test_trace_from_selected_conduit_reports_upstream_flow(fake_arcpy, flows_gpkg):
    fid = fid_of(flows_gpkg, "wwn_conduits", "conduit_id", "P08")
    a, layers = mk(fake_arcpy, flows_gpkg, sel_conduits=[fid])
    r = tools_impl.trace(a, Messages(a), flows_gpkg, "", "Upstream", True)
    assert r["start"] == "P08" and "P08" in r["conduits"]
    s = r["summary"]
    assert s["start_conduit"] == "P08" and s["upstream_connections"] == 132 and s["adwf_lps"] > 1.0
    assert "Upstream of conduit P08" in text(a) and "connection(s)" in text(a)


def test_trace_needs_a_start_and_warns_off_map(fake_arcpy, demo_gpkg):
    a, layers = mk(fake_arcpy, demo_gpkg)
    with pytest.raises(RuntimeError, match="select one conduit or node"):
        tools_impl.trace(a, Messages(a), demo_gpkg, "", "Upstream", True)
    layers[:] = [_ProBasemapLayer()]
    tools_impl.trace(a, Messages(a), demo_gpkg, "N03", "Upstream", True)
    assert a.selections == [] and "not in the active map" in text(a, "warn")


# ---------------------------------------------------------------- simulation

def test_run_simulation_stores_labelled_run_and_layers(fake_arcpy, flows_gpkg, tmp_path):
    fid = fid_of(flows_gpkg, "wwn_conduits", "conduit_id", "P02")
    a, layers = mk(fake_arcpy, flows_gpkg, sel_conduits=[fid])
    inp = str(tmp_path / "m.inp")
    d = tools_impl.run_simulation(a, Messages(a), flows_gpkg, 12.0, 6.0, "ks", 0.013, 1.0, True, inp, "base")
    assert d["label"] == "base" and "P02" in d["series_ids"]                # selected conduit series kept
    assert d["options"]["roughness"] == "ks"
    assert "[CONDUITS]" in open(inp).read()
    assert {"Simulation results (conduits)", "Simulation results (nodes)"} <= set(names(layers))
    nodes_lyr = next(lyr for lyr in layers if maplayers.layer_name(lyr) == "Simulation results (nodes)")
    assert nodes_lyr.definitionQuery == symbology.SIM_NODES_QUERY
    t = text(a)
    assert "Simulation base (" in t and "continuity error" in t and "Highest peak d/D" in t
    assert "Colebrook ks" in t and "Stored as run base (" in t
    assert rows(flows_gpkg, "select label from wwn_sim_conduit_results limit 1") == [("base",)]
    tools_impl.run_simulation(a, Messages(a), flows_gpkg, 12.0, 6.0, flow_scale=2.0, label="x2")
    assert names(layers).count("Simulation results (conduits)") == 1


def test_show_compare_and_hydrograph(fake_arcpy, flows_gpkg, tmp_path):
    a, layers = mk(fake_arcpy, flows_gpkg)
    tools_impl.run_simulation(a, Messages(a), flows_gpkg, 12.0, 6.0, label="base", show_layer=False)
    tools_impl.run_simulation(a, Messages(a), flows_gpkg, 12.0, 6.0, flow_scale=2.0, label="x2", show_layer=False)
    titles = gpkg.run_titles(flows_gpkg)
    assert titles[0].startswith("x2 (") and titles[1].startswith("base (")
    r = tools_impl.show_simulation_run(a, Messages(a), flows_gpkg, titles[1])
    assert r["label"] == "base" and "Showing run base (" in text(a)
    assert rows(flows_gpkg, "select distinct label from wwn_sim_conduit_results") == [("base",)]
    c = tools_impl.compare_simulation_runs(a, Messages(a), flows_gpkg, titles[1], titles[0])
    assert c["counts"].get("worse", 0) > 0 and "Comparison A = base" in text(a)
    assert "Simulation comparison" in names(layers)
    with pytest.raises(Exception, match="different runs"):
        tools_impl.compare_simulation_runs(a, Messages(a), flows_gpkg, titles[0], titles[0])
    ids = gpkg.series_ids(flows_gpkg)
    assert len(ids) == 5
    out = str(tmp_path / "h")
    res = tools_impl.plot_hydrograph(a, Messages(a), flows_gpkg, titles[0], ids[:2] + ["ZZ"], out, chart=True)
    assert len(res["series"]) == 2 and res["missing"] == ["ZZ"]
    with open(out + ".csv", newline="") as fh:
        data = list(csv.DictReader(fh))
    assert data and data[0]["run"].startswith("x2 (") and data[0]["time"].count(":") == 1
    assert "peak" in text(a) and "Could not create the chart" in text(a, "warn")   # fake arcpy: no charts


# ---------------------------------------------------------------- loading tools

PROFILE = "Time\tFactor\n" + "".join(f"{i * 5 // 60:02d}:{i * 5 % 60:02d}\t{0.5 if i < 144 else 1.5}\n"
                                      for i in range(288))


def test_import_profile_tool_file_and_paste(fake_arcpy, tmp_path, demo_gpkg):
    a = fake_arcpy([])
    csv_path = tmp_path / "p.txt"
    csv_path.write_text(PROFILE)
    m = Messages(a)
    r = tools_impl.import_profile(m, demo_gpkg, "RES", str(csv_path), "desc", True, False)
    assert r["points"] == 288 and r["interval_min"] == 5 and abs(r["mean_factor"] - 1) < 1e-9
    assert rows(demo_gpkg, "select count(*) from wwn_profiles where profile_id='RES'") == [(288,)]
    assert rows(demo_gpkg, "select value from wwn_meta where key='default_profile'") == [("RES",)]
    assert "Imported profile RES" in text(a)
    tools_impl.import_profile(m, demo_gpkg, "P2", None, "", False, False, pasted="0\t2\n720\t4\n")
    assert "mean factor" in text(a, "warn")
    r = tools_impl.import_profile(m, demo_gpkg, "P2", None, "", False, True, pasted="0\t2\n720\t4\n")
    assert r["normalised"] and abs(r["mean_factor"] - 1) < 1e-9


def test_import_profile_tool_errors(fake_arcpy, tmp_path, demo_gpkg):
    m = Messages(fake_arcpy([]))
    with pytest.raises(RuntimeError, match="profile id"):
        tools_impl.import_profile(m, demo_gpkg, " ", None, pasted="0\t1\n")
    with pytest.raises(RuntimeError, match="Choose a CSV"):
        tools_impl.import_profile(m, demo_gpkg, "X")
    with pytest.raises(RuntimeError, match="not found"):
        tools_impl.import_profile(m, demo_gpkg, "X", str(tmp_path / "nope.csv"))
    with pytest.raises(Exception, match="outside"):
        tools_impl.import_profile(m, demo_gpkg, "X", None, pasted="0\t1\n24:00\t1\n")


def test_model_settings_and_assign_profile(fake_arcpy, flows_gpkg):
    fid = fid_of(flows_gpkg, "wwn_nodes", "node_id", "N01")
    a, layers = mk(fake_arcpy, flows_gpkg, sel_nodes=[fid])
    m = Messages(a)
    st = tools_impl.model_settings(m, flows_gpkg)
    assert st["default_profile"] == "example_residential" and "1 profile(s):" in text(a)
    tools_impl.import_profile(m, flows_gpkg, "COMM", None, "", False, False, pasted="0\t0.8\n720\t1.2\n")
    st = tools_impl.model_settings(m, flows_gpkg, 600.0, 2.0, tools_impl.NONE_PROFILE)
    assert (st["adwf_per_connection_lpd"], st["peaking_factor"], st["default_profile"]) == (600.0, 2.0, None)
    n = tools_impl.assign_profile(a, m, flows_gpkg, "Selected nodes (their node loads)", "COMM")
    assert n == 1 and rows(flows_gpkg, "select profile_id from wwn_node_loads where node_id='N01'") == [("COMM",)]
    n = tools_impl.assign_profile(a, m, flows_gpkg, "All node loads", tools_impl.DEFAULT_PROFILE)
    assert n == 11 and rows(flows_gpkg, "select count(*) from wwn_node_loads where profile_id is not null") == [(0,)]
    with pytest.raises(RuntimeError, match="Nothing selected"):
        tools_impl.assign_profile(a, m, flows_gpkg, "Selected sub-catchments", "COMM")
    with pytest.raises(Exception, match="does not exist"):
        tools_impl.assign_profile(a, m, flows_gpkg, "All sub-catchments", "NOPE")


def test_pipe_capacity_any_dn(fake_arcpy):
    a = fake_arcpy([])
    r = tools_impl.pipe_capacity(Messages(a), 225, 300, 1.0, 5.0)
    assert 15 < r["full_flow_lps"] < 60 and r["depth_ratio_at_flow"] < 0.5
    assert r["ds50_checks"]["results"]
    assert "Full-bore capacity" in text(a) and "DS 50 checks" in text(a) and "d/D" in text(a)
    r2 = capacity.calculate(150, 100, None, 1000.0)           # far beyond capacity -> surcharge
    assert r2["depth_ratio_at_flow"] is None and any("surcharge" in ln for ln in capacity.describe(r2))
    a = fake_arcpy([])
    r3 = tools_impl.pipe_capacity(Messages(a), 100, 60, None, 1.0)
    assert r3["full_flow_lps"] > 1 and "outside DS 50 Table 4.4" in text(a)


# ---------------------------------------------------------------- the .pyt

def test_pyt_tools_parameters_and_categories(fake_arcpy):
    fake_arcpy([])
    mod = load_pyt()
    tb = mod.Toolbox()
    tools = {t().label: t() for t in tb.tools}
    assert list(tools) == ["New Model", "Add Model To Map", "Model Settings", "Import Sewer Data",
                           "Import Diurnal Profile", "Assign Profile", "Check Model", "Run Design", "Run Simulation",
                           "Trace", "Show Simulation Run", "Compare Simulation Runs", "Plot Hydrograph",
                           "Pipe Capacity", "Ask WWNet Assistant", "Set Assistant API Key", "Verify Setup"]
    assert {t.category for t in tools.values()} == {"Model", "Data", "Analysis", "Results", "Calculators",
                                                    "Assistant", "Setup"}
    for t in tools.values():
        params = t.getParameterInfo()
        assert all(p.name and p.datatype for p in params) and len({p.name for p in params}) == len(params)
        assert not any(" wwnet " in (t.description or "") or "`wwnet" in (t.description or "") for _ in [0])
    key = {p.name: p for p in tools["Set Assistant API Key"].getParameterInfo()}
    assert key["key"].datatype == "GPEncryptedString" and "exe" not in key
    ask = {p.name: p for p in tools["Ask WWNet Assistant"].getParameterInfo()}
    assert ask["model"].value == "claude-opus-5-5" and ask["model"].filter.list == mod.MODELS
    assert ask["apply"].value is False and ask["apply_last"].value is False
    assert ask["question"].controlCLSID and ask["layer"].datatype == "GPFeatureLayer"
    sim = {p.name: p for p in tools["Run Simulation"].getParameterInfo()}
    assert sim["roughness"].value == "ks" and sim["roughness"].filter.list == ["ks", "manning"]
    assert sim["export_inp"].direction == "Output" and sim["out_gpkg"].direction == "Output"
    cap = {p.name: p for p in tools["Pipe Capacity"].getParameterInfo()}
    assert cap["dn"].filter.type is None                          # any DN
    tr = {p.name: p for p in tools["Trace"].getParameterInfo()}
    assert tr["start"].parameterType == "Optional" and "nodes" not in tr


def test_pyt_model_defaults_from_map_and_run_lists(fake_arcpy, flows_gpkg):
    a, layers = mk(fake_arcpy, flows_gpkg)
    import wwnet_native
    wwnet_native.simulate(flows_gpkg, 12.0, 6.0, label="base")
    wwnet_native.simulate(flows_gpkg, 12.0, 6.0, flow_scale=2.0, label="x2")
    mod = load_pyt("wwnet_pyt_defaults")
    t = mod.CompareSimulationRuns()
    params = t.getParameterInfo()
    t.updateParameters(params)
    p = {x.name: x for x in params}
    assert maplayers.same_file(p["gpkg"].value, flows_gpkg)
    assert p["run_a"].value.startswith("base (") and p["run_b"].value.startswith("x2 (")
    assert len(p["run_a"].filter.list) == 2
    t.execute(params, None)
    assert "Comparison A = base" in text(a)
    ms = mod.ModelSettings()
    params = ms.getParameterInfo()
    ms.updateParameters(params)
    p = {x.name: x for x in params}
    assert p["peaking_factor"].value == 1.5 and p["default_profile"].value == "example_residential"
    assert p["default_profile"].filter.list == [tools_impl.NONE_PROFILE, "example_residential"]
    ph = mod.PlotHydrograph()
    params = ph.getParameterInfo()
    ph.updateParameters(params)
    p = {x.name: x for x in params}
    assert len(p["ids"].filter.list) == 5


def test_pyt_import_profile_tool_by_name(fake_arcpy, tmp_path, demo_gpkg):
    fake_arcpy([])
    mod = load_pyt("wwnet_pyt_prof")
    t = mod.ImportDiurnalProfile()
    assert t.category == "Data" and t.label == "Import Diurnal Profile"
    params = t.getParameterInfo()
    assert [p.name for p in params] == ["gpkg", "profile_id", "csv", "description", "set_default", "normalise",
                                        "pasted", "out_gpkg"]
    csv_path = tmp_path / "p.csv"
    csv_path.write_text(PROFILE)
    by = {p.name: p for p in params}
    by["gpkg"].value, by["profile_id"].value, by["csv"].value = demo_gpkg, "RES", str(csv_path)
    t.execute(params, None)
    assert rows(demo_gpkg, "select count(*) from wwn_profiles") == [(288,)]
    assert by["out_gpkg"].value == demo_gpkg                       # derived output for ModelBuilder


class _ProBasemapLayer:
    """Mimics real Pro layers (basemaps, some service layers) where reading an unsupported
    property raises: "The attribute 'name' is not supported on this instance of Layer"."""
    isGroupLayer = False
    isFeatureLayer = False

    def supports(self, prop):
        return False

    def __getattr__(self, item):
        if item in ("name", "dataSource", "connectionProperties"):
            raise NameError(f"The attribute '{item}' is not supported on this instance of Layer.")
        raise AttributeError(item)


def test_tools_tolerate_layers_without_name(fake_arcpy, flows_gpkg):
    a, layers = mk(fake_arcpy, flows_gpkg)
    layers.insert(0, _ProBasemapLayer())
    tools_impl.run_simulation(a, Messages(a), flows_gpkg, 12.0, 6.0)
    assert names(layers).count("Simulation results (conduits)") == 1
    assert "Could not show the Simulation results layers" not in text(a)
    assert maplayers.same_file(maplayers.default_model(a), flows_gpkg)
