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
                                           "wwn_pump_stations", "wwn_nodes", "wwn_node_loads", "wwn_pumps")]
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
    assert list(tools) == ["New Model", "Add Model To Map", "Model Settings", "Join Dangling Sewer Ends",
                           "Import Sewer Data", "Import Pressure Data", "Set Pump Station Data",
                           "Import Diurnal Profile", "Assign Profile", "Export To ICM", "Check Model", "Run Design",
                           "Run Simulation", "Trace", "Show Simulation Run", "Compare Simulation Runs",
                           "Plot Hydrograph", "Pipe Capacity", "Verify Setup", "Self Test", "Update WWNet"]
    assert {t.category for t in tools.values()} == {"Model", "Data", "Analysis", "Results", "Calculators", "Setup"}
    for t in tools.values():
        params = t.getParameterInfo()
        assert all(p.name and p.datatype for p in params) and len({p.name for p in params}) == len(params)
        assert not any(" wwnet " in (t.description or "") or "`wwnet" in (t.description or "") for _ in [0])
    assert not hasattr(mod, "AskWWNetAssistant") and not hasattr(mod, "SetAssistantApiKey")
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


# ================================================================ merged from the 2026-11-23/24 session
# (Pro dataSource strings, pump station results, pump station data) adapted to the v3 tools.

def pro_sqlite_source(gpkg_path, table):
    """dataSource as real Pro reports a SQLite-family layer (shape copied from a mobile-gdb layer
    on the user's PC: connection properties, then the dataset after the last separator)."""
    return (f"AUTHENTICATION_MODE=OSA;main;DB_CONNECTION_PROPERTIES={gpkg_path};"
            f"INSTANCE=sde:sqlite:{gpkg_path};IS_GEODATABASE=false;IS_NOSERVER=0;SERVER=C:\\main.{table}")


def pro_gpkg_source(gpkg_path, table):
    """dataSource exactly as real Pro 3.x reports a GeoPackage layer (pasted from the user's PC)."""
    return f"Instance={gpkg_path},Dataset=main.%{table}"


def test_split_datasource_pro_connection_string():
    p = r"C:\Users\x\Desktop\WWNet\models\Dunkley Ave PS4.gpkg"
    assert gpkg.split_datasource(pro_sqlite_source(p, "wwn_nodes")) == (p, "wwn_nodes")
    assert gpkg.dataset_path(pro_sqlite_source(p, "wwn_conduits")) == gpkg.table_path(p, "wwn_conduits")
    real = (r"AUTHENTICATION_MODE=OSA;main;DB_CONNECTION_PROPERTIES=C:\Users\u\Desktop\Shapefiles\WWLine.geodatabase;"
            r"INSTANCE=sde:sqlite:C:\Users\u\Desktop\Shapefiles\WWLine.geodatabase;IS_GEODATABASE=true;"
            r"IS_NOSERVER=0;SERVER=C:\main.WWProjects")
    assert gpkg.split_datasource(real) == (None, None) and gpkg.dataset_path(real) == real
    assert gpkg.split_datasource(f"AUTHENTICATION_MODE=OSA;DATABASE={p};SERVER=C:") == (p, None)


def test_split_datasource_real_pro_gpkg_string():
    real = r"Instance=C:\Users\lawrend0\Desktop\WWNet\models\import_test.gpkg,Dataset=main.%wwn_nodes"
    p = r"C:\Users\lawrend0\Desktop\WWNet\models\import_test.gpkg"
    assert gpkg.split_datasource(real) == (p, "wwn_nodes")
    assert gpkg.dataset_path(real) == gpkg.table_path(p, "wwn_nodes")
    sp = r"C:\a, b\my model.GPKG"   # comma and space in the folder
    assert gpkg.split_datasource(pro_gpkg_source(sp, "wwn_conduits")) == (sp, "wwn_conduits")


@pytest.mark.parametrize("source", [pro_gpkg_source, pro_sqlite_source])
def test_model_found_and_traced_with_real_pro_layer_sources(fake_arcpy, demo_gpkg, source):
    """The 2026-11-20 bug ('not in the active map'): Pro's GeoPackage dataSource strings."""
    layers = [_ProBasemapLayer(), fa.make_layer("Nodes", source(demo_gpkg, "wwn_nodes")),
              fa.make_layer("Gravity sewers", source(demo_gpkg, "wwn_conduits"))]
    a = fake_arcpy(layers)
    assert maplayers.find_layer_for(a, demo_gpkg, "wwn_conduits") is layers[2]
    assert maplayers.same_file(maplayers.default_model(a), demo_gpkg)
    r = tools_impl.trace(a, Messages(a), demo_gpkg, "N03", "Downstream", True)
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
    assert maplayers.remove_layers_named(a, "Node loads") == 0


PUMP_NAME = "Simulation pump stations \u2014 demo"


def _add_station(gp, ps_id="PS1", node="N01", **cols):
    con = sqlite3.connect(gp)
    geom = con.execute("SELECT geom FROM wwn_nodes WHERE node_id=?", (node,)).fetchone()[0]
    con.execute("UPDATE wwn_nodes SET invert_level=18.0 WHERE node_id=?", (node,))
    cols = {"start_level": 19.5, "stop_level": 18.5, "overflow_level": 20.5, **cols}
    con.execute(f"INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id, {', '.join(cols)}) "
                f"VALUES (?, ?, ?, {', '.join('?' * len(cols))})", (geom, ps_id, node, *cols.values()))
    con.commit()
    con.close()


def _fake_sim():
    links = [dict(conduit_id="P01", dn=150, max_flow_lps=1.0, max_d_over_d=0.3, max_velocity_mps=0.5,
                  surcharged_min=0.0, time_of_max_flow="07:30"),
             dict(conduit_id="P04", dn=225, max_flow_lps=40.0, max_d_over_d=0.8, max_velocity_mps=1.2,
                  surcharged_min=15.0, time_of_max_flow="08:05")]
    nodes = [dict(node_id="N01", max_hgl_m=1.0, max_depth_m=1.0, flood_volume_m3=0.0, surcharged_min=30.0)]
    return dict(run_id="2026-01-01T00:00:00Z", label="", summary=dict(
        inflow_adwf_lps=1.0, inflow_simulated_lps=1.5, peak_outlet_flow_lps=3.0, continuity_error_pct=-0.1,
        conduits=2, nodes=1, surcharged_conduits=1, flooded_nodes=0, total_flood_volume_m3=0.0),
        links=links, nodes=nodes, issues=[], warnings=[])


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
    monkeypatch.setattr(tools_impl.engine, "run_simulate", lambda *args: _fake_pump_sim())
    styled = []
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: styled.append(t) or True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    t = [x for _, x in a.log]
    assert t.index("Pump stations (levels m AHD)") > max(i for i, x in enumerate(t) if x.startswith("P0"))
    row = next(x for x in t if x.startswith("PS1 ") and "19.62" in x)
    for tok in ("19.50", "18.50", "20.50", "0.00", "08:10"):
        assert tok in row
    assert any(x.startswith("RM1") and "150" in x and "420.0" in x and "1.00" in x for x in t)
    assert not any(lv == "warn" and x.startswith("PS1:") for lv, x in a.log)     # nothing flagged
    assert "sim_pumps" in styled
    r = a.memory["memory\\wwnet_simpumps_demo"]["rows"][0]
    assert r["ps_id"] == "PS1" and r["starts_per_hour"] == 4.0 and r["flag"] == "ok"
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)                  # re-run replaces the layer
    assert names(layers).count(PUMP_NAME) == 1


def test_pump_flags(fake_arcpy, demo_gpkg, monkeypatch):
    _add_station(demo_gpkg)
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.engine, "run_simulate", lambda *args: _fake_pump_sim(overflow=3.25, sph=12.0, v=0.5))
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    warns = [x for lv, x in a.log if lv == "warn" and x.startswith("PS1:")]
    assert any("wet well overflow 3.25 m3" in w for w in warns)
    assert any("PS1_P1" in w and "12.0 starts/h" in w and "motor size" in w for w in warns)
    assert any("RM1" in w and "0.50 m/s < 0.7" in w for w in warns)
    assert a.memory["memory\\wwnet_simpumps_demo"]["rows"][0]["flag"] == "overflow,starts,v_low"


def test_no_pump_layer_without_stations_or_show_layer(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = mk(fake_arcpy, demo_gpkg)
    monkeypatch.setattr(tools_impl.engine, "run_simulate", lambda *args: _fake_sim())
    monkeypatch.setattr(tools_impl.symbology, "apply", lambda t, lyr, m: True)
    tools_impl.run_simulation(a, Messages(a), demo_gpkg)
    assert not any(x.startswith("Pump") for _, x in a.log) and PUMP_NAME not in names(layers)
    _add_station(demo_gpkg)
    monkeypatch.setattr(tools_impl.engine, "run_simulate", lambda *args: _fake_pump_sim())
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, show_layer=False)
    assert PUMP_NAME not in names(layers) and "Pump stations (levels m AHD)" in [x for _, x in a.log]


def test_run_simulation_real_engine_pump_station(fake_arcpy, demo_gpkg):
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
    t = [x for _, x in a.log]
    assert "Pump stations (levels m AHD)" in t and "Rising mains" in t
    assert not any("PUMP_STATION_NOT_SIMULATED" in x for x in t)          # the pressure side is simulated now
    assert names(layers).count(PUMP_NAME) == 1
    assert rows(demo_gpkg, "select count(*) from wwn_sim_pump_stations") == [(1,)]


def test_check_model_prints_station_report(fake_arcpy, demo_gpkg):
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.check_model(a, Messages(a), demo_gpkg, False)
    assert not any("Pump station data" in t for _, t in a.log)           # no stations -> silent
    _add_station(demo_gpkg)
    tools_impl.check_model(a, Messages(a), demo_gpkg, False)
    assert ("info", "Pump station data: PS1: missing wet_well_area_m2, pumps") in a.log


def test_style_sim_pumps_uses_shared_palette(monkeypatch):
    from wwnet_arcgis import shared
    st = shared.core("style")
    seen = {}
    monkeypatch.setattr(symbology, "_unique", lambda layer, field, fn, size=None, width=None:
                        seen.update(field=field, ok=fn("ok"), bad=fn("overflow,starts")))
    monkeypatch.setattr(symbology, "_label", lambda layer, expr: seen.update(label=expr))
    symbology.style_sim_pumps(object())
    assert seen == {"field": "flag", "ok": st.SIM_CLASS["low"], "bad": st.SIM_CLASS["surcharged"],
                    "label": "$feature.ps_id"}


def test_set_pump_station_data_messages(fake_arcpy, demo_gpkg):
    _add_station(demo_gpkg, ps_id="PS1")
    _add_station(demo_gpkg, ps_id="PS2", node="N02")
    a = fake_arcpy([])
    r = tools_impl.set_pump_station_data(
        a, Messages(a), demo_gpkg, "PS1", wet_well_area_m2=7.0, level_mode="AHD", n_duty=2,
        curve_text="Flow\tHead\n10\t30\n20\t10", curve_order="flow_head", flow_units="L/s",
        source_note="Data sheet rev C")
    assert r["pumps"] == ["PS1_P1", "PS1_P2"]
    assert ("info", "PS1: pump data complete") in a.log and ("info", "PS2: missing wet_well_area_m2, pumps") in a.log
    a.log.clear()
    assert tools_impl.set_pump_station_data(a, Messages(a), demo_gpkg, "PS1", stop=19.9, start=19.5) is None
    assert a.log == [("warn", "ERROR: Stop level must be below start level.")]
