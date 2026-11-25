"""Import Network Data: tools_impl.import_network_data (sewers -> pressure -> join -> register -> map) and the
.pyt tool class. Fake arcpy; gravity network + pressure layers + the demo register."""
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import fake_arcpy as fa
from wwnet_arcgis import pressure_import, sewer_import, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]
DEMO_XLSX = TOOLBOX.parent / "examples" / "demo" / "pump_register_demo.xlsx"
GEOM = "_geom"
GDA2020 = NS(factoryCode=7850, name="GDA2020 / MGA zone 50")

AC_FIELDS = [("OBJECTID", "OID"), ("PACID", "String"), ("TOPLEV", "Double"), ("Shape", "Geometry")]
SEWER_FIELDS = [(n, "String") for n in (
    "ID", "STATUS", "MAINTYPE", "PURPOSE", "DOWNSTREAM", "UPSTREAM", "FLOW_GRADE", "LENGTH", "PLANSET",
    "MATERIAL", "DATEINST", "HEIGHT", "DS_MANHOLE_ID", "US_MANHOLE_ID")] + [("Shape", "Geometry")]
PS_FIELDS = [("OBJECTID", "OID"), ("SAP_ID", "String"), ("ID", "Double"), ("SAP_NAME", "String"),
             ("STATUS", "String"), ("PSNAME", "String"), ("Shape", "Geometry")]
MAIN_FIELDS = [("OBJECTID", "OID"), ("ID", "Double"), ("STATUS", "String"), ("DIAMETER", "Double"),
               ("MATERIAL", "String"), ("INDIAM1", "Double"), ("UPSTREAM", "Double"), ("DOWNSTREAM", "Double"),
               ("LENGTH", "Double"), ("MAINNAME", "String"), ("Shape", "Geometry")]

M1, M2, M3 = (385000.0, 6460000.0), (385050.0, 6460000.0), (385100.0, 6460000.0)
B1, B2 = (385025.0, 6460030.0), (385025.0, 6460003.0)          # branch ends 3 m off the M1-M2 sewer 
I1, W = (385050.0, 6460050.0), (385050.0, 6460100.0)            # inlet to the pump station wet well
N1, N2 = (385000.0, 6460103.0), (385100.0, 6460103.0)           # a sewer passing 3 m from the wet well
PS_XY = (385050.3, 6460100.4)


def sewer(pid, line, us, ds, us_inv, ds_inv, **kw):
    r = {"ID": pid, "STATUS": "A- actual", "MAINTYPE": "R- retic", "PURPOSE": "L- local", "DOWNSTREAM": ds_inv,
         "UPSTREAM": us_inv, "FLOW_GRADE": 100, "LENGTH": 10.0, "PLANSET": "PS1",
         "MATERIAL": "PVC- polyvinyl chloride", "DATEINST": None, "HEIGHT": 225, "US_MANHOLE_ID": us,
         "DS_MANHOLE_ID": ds, GEOM: line}
    r.update(kw)
    return r


def seg(i, coords, **kw):
    r = {"ID": float(i), "STATUS": "A- actual", "DIAMETER": 300.0, "MATERIAL": "AC- asbestos cement",
         "INDIAM1": 290.0, "UPSTREAM": -99.89999, "DOWNSTREAM": -99.89999, "LENGTH": 100.0, "MAINNAME": "SPS",
         GEOM: coords}
    r.update(kw)
    return r


@pytest.fixture
def world(fake_arcpy, tmp_path):
    layers = []
    a = fake_arcpy(layers)
    a.sources["C:/d/ac"] = {"fields": AC_FIELDS, "sr": GDA2020, "rows": [
        {"PACID": n, "TOPLEV": z, GEOM: xy} for n, z, xy in (
            ("M1", 30.0, M1), ("M2", 29.0, M2), ("M3", 28.0, M3), ("B1", 30.0, B1), ("I1", 27.0, I1),
            ("N1", 26.0, N1), ("N2", 25.5, N2))]}
    # the sewers S3 / S4 end at no chamber: the importer makes dangling junctions E1 (a missed tee on S1) and E2
    # (the inlet's dead end at the pump station = the wet well; S5 passes 3 m from it)
    a.sources["C:/d/sew"] = {"fields": SEWER_FIELDS, "sr": GDA2020, "rows": [
        sewer("S1", [M1, M2], "M1", "M2", 25.0, 24.0, LENGTH=50.0),
        sewer("S2", [M2, M3], "M2", "M3", 24.0, 23.0, LENGTH=50.0),
        sewer("S3", [B1, B2], "B1", "B2", 27.0, 26.8, LENGTH=27.0),
        sewer("S4", [I1, W], "I1", "W", 24.0, 22.2, LENGTH=50.0),
        sewer("S5", [N1, N2], "N1", "N2", 23.0, 22.0, LENGTH=100.0)]}
    a.sources["C:/d/ps"] = {"fields": PS_FIELDS, "sr": GDA2020, "rows": [
        {"SAP_ID": "PS1", "ID": 1.0, "SAP_NAME": "SPS 1 TEST ST", "STATUS": "A- actual", "PSNAME": "TEST",
         GEOM: PS_XY}]}
    a.sources["C:/d/main"] = {"fields": MAIN_FIELDS, "sr": GDA2020, "rows": [
        seg(1, [(385052.0, 6460100.0), (385100.0, 6460100.0)], LENGTH=48.0),
        seg(2, [(385100.0, 6460100.0), (385100.0, 6460000.5)], LENGTH=99.5)]}
    lyr = {k: fa.make_layer(k, f"C:/d/{k}") for k in ("ac", "sew", "ps", "main")}
    return a, lyr, str(tmp_path / "net.gpkg")


def run(a, lyr, out, **kw):
    kw.setdefault("fields", {"diameter": "HEIGHT"})
    if kw.pop("pressure", True):
        kw.setdefault("ps_layer", lyr["ps"])
        kw.setdefault("main_layer", lyr["main"])
    if kw.pop("register", True):
        kw["register"] = str(DEMO_XLSX)
    return tools_impl.import_network_data(a, Messages(a), lyr["ac"], None, lyr["sew"], out, **kw)


def q(gp, sql, *args):
    c = sqlite3.connect(gp)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def texts(a, kind=None):
    return [t for k, t in a.log if kind is None or k == kind]


def headers(a):
    return [t for t in texts(a, "info") if t.startswith("== ")]


@pytest.fixture
def trace(monkeypatch, world):
    """Record the order of the four steps and the map layers added when each one starts."""
    a = world[0]
    calls = []
    for name in ("import_sewer_data", "import_pressure_data", "join_dangling_ends", "import_pump_station_data",
                 "add_model_to_map"):
        orig = getattr(tools_impl, name)

        def wrap(*args, _orig=orig, _name=name, **kw):
            calls.append((_name, len(a.added)))
            return _orig(*args, **kw)
        monkeypatch.setattr(tools_impl, name, wrap)
    return calls


def test_without_pressure_the_join_would_tie_the_wet_well(world):
    """Control: the inlet's dead end E2 is a dangling junction 3 m from a sewer, so the join alone joins it."""
    a, lyr, out = world
    run(a, lyr, out, pressure=False, register=False, join_dangling=False)
    assert q(out, "select node_type from wwn_nodes where node_id in ('E1','E2') order by node_id") == [
        ("junction",), ("junction",)]
    res = tools_impl.join_dangling_ends(a, Messages(a), out, 5.0, True)
    assert {j["dangling_node"] for j in res["joins"]} == {"E1", "E2"}


def test_full_sequence_order_headers_and_wet_well_not_joined(world, trace):
    a, lyr, out = world
    res = run(a, lyr, out)
    assert [c[0] for c in trace] == ["import_sewer_data", "import_pressure_data", "join_dangling_ends",
                                    "import_pump_station_data", "add_model_to_map"]
    assert [c[1] for c in trace] == [0, 0, 0, 0, 0]            # nothing on the map while any step runs
    assert a.added and a.added[0][0] == "data"                 # ... and the model is added at the end, once
    assert headers(a) == ["== 1/4 Sewers ==", "== 2/4 Pressure ==", "== 3/4 Join dangling ends ==",
                          "== 4/4 Pump station register ==", texts(a, "info")[-2]]
    assert headers(a)[-1].startswith("== Summary == ")
    # message order: each header precedes its step's own messages
    info = texts(a, "info")
    i_p = next(i for i, t in enumerate(info) if t.startswith("Imported 1 pump station(s)"))
    i_j = next(i for i, t in enumerate(info) if "dangling end(s) joined" in t and "not joined" in t)
    i_r = next(i for i, t in enumerate(info) if t.startswith("Register: pump_register_demo.xlsx"))
    assert info.index("== 2/4 Pressure ==") < i_p < info.index("== 3/4 Join dangling ends ==") < i_j \
        < info.index("== 4/4 Pump station register ==") < i_r
    # the wet well E2 is a wet_well and was NOT joined: S5 unsplit, S4 still the only sewer at E2
    assert q(out, "select node_type from wwn_nodes where node_id='E2'") == [("wet_well",)]
    assert q(out, "select conduit_id from wwn_conduits where conduit_id like 'S5%'") == [("S5",)]
    assert q(out, "select count(*) from wwn_conduits where ds_node_id='E2' or us_node_id='E2'") == [(1,)]
    assert q(out, "select wet_well_node_id from wwn_pump_stations") == [("E2",)]
    assert res["join"]["joins"] and {j["dangling_node"] for j in res["join"]["joins"]} == {"E1"}
    assert q(out, "select count(*) from wwn_conduits where conduit_id like 'S1~t%'") == [(1,)]   # E1 tee on S1
    # rising main is untouched by the join (not in wwn_conduits); register data on the station
    assert q(out, "select count(*) from wwn_rising_mains") == [(1,)]
    assert q(out, "select stop_level, start_level, pump_rate_lps from wwn_pump_stations") == [(22.5, 23.0, 25.0)]
    assert q(out, "select invert_level from wwn_nodes where node_id='E2'") == [(21.9,)]
    sm = res["summary"]
    assert (sm["sewers"], sm["pump_stations"], sm["rising_mains"], sm["joins"],
            sm["stations_with_pump_data"]) == (6, 1, 1, 1, 1)
    assert sm["nodes"] == q(out, "select count(*) from wwn_nodes")[0][0]
    assert "1 dangling end(s) joined, 1 pump station(s), 1 rising main(s), 1 station(s) with pump data" in info[-2]
    assert info[-1] == "Next: Export To ICM."
    assert any(t.startswith("Final model check") for t in info)     # issues refreshed after the join
    assert not [t for t in texts(a, "warn") if t.startswith("ERROR:")]
    assert not texts(a, "error")
    assert res["steps"] == {"sewer": "ok", "pressure": "ok", "join": "ok", "register": "ok"}


def test_gravity_only_skips_pressure_and_register(world, trace):
    a, lyr, out = world
    res = run(a, lyr, out, pressure=False, register=False)
    assert [c[0] for c in trace] == ["import_sewer_data", "join_dangling_ends", "add_model_to_map"]
    info = texts(a, "info")
    assert info[info.index("== 2/4 Pressure ==") + 1].startswith("Skipped: no pump station")
    assert info[info.index("== 4/4 Pump station register ==") + 1].startswith("Skipped: no pump station register")
    assert res["steps"]["pressure"] == res["steps"]["register"] == "skipped"
    # without a pressure import W stays a plain dangling junction: the join takes both ends
    assert {j["dangling_node"] for j in res["join"]["joins"]} == {"E1", "E2"}
    assert res["summary"]["pump_stations"] == 0 and res["summary"]["stations_with_pump_data"] == 0


def test_join_off(world, trace):
    a, lyr, out = world
    res = run(a, lyr, out, join_dangling=False)
    assert "join_dangling_ends" not in [c[0] for c in trace]
    info = texts(a, "info")
    assert info[info.index("== 3/4 Join dangling ends ==") + 1].startswith("Skipped: 'Join dangling sewer ends' is off")
    assert q(out, "select count(*) from wwn_conduits") == [(5,)] and res["summary"]["joins"] == 0
    assert res["steps"]["join"] == "skipped" and res["steps"]["register"] == "ok"


def test_add_to_map_off(world):
    a, lyr, out = world
    run(a, lyr, out, add_to_map=False)
    assert not a.added


def test_sewer_failure_stops_everything(world, trace):
    a, lyr, out = world
    a.sources["C:/d/sew"]["rows"] = []
    with pytest.raises(RuntimeError, match="No gravity sewers were read"):
        run(a, lyr, out)
    assert [c[0] for c in trace] == ["import_sewer_data"]
    assert headers(a) == ["== 1/4 Sewers =="]
    ws = texts(a, "warn")
    assert any(w.startswith("ERROR: Sewer import failed") for w in ws)
    assert any("stopped: the pressure, join and register steps were not run" in w for w in ws)
    assert not a.added


def test_pressure_failure_skips_register_but_join_continues(world, trace):
    a, lyr, out = world
    a.sources["C:/d/ps"]["rows"][0][GEOM] = (500000.0, 6400000.0)       # outside the model: nothing read
    res = run(a, lyr, out)
    assert [c[0] for c in trace] == ["import_sewer_data", "import_pressure_data", "join_dangling_ends",
                                    "add_model_to_map"]
    ws = texts(a, "warn")
    assert any(w.startswith("ERROR: Pressure import failed: No pump stations were read") for w in ws)
    assert any("register step will be skipped" in w for w in ws)
    assert any(w.startswith("Skipped: the pressure step failed") for w in ws)
    assert res["steps"] == {"sewer": "ok", "pressure": "failed", "join": "ok", "register": "skipped"}
    assert any(w.startswith("ERROR: Step(s) with errors: pressure") for w in ws)
    assert texts(a, "info")[-1] == "Next: Export To ICM."
    assert a.added                                                      # the model is still added


def test_join_failure_continues_with_register(world, trace, monkeypatch):
    a, lyr, out = world

    def bad_join(arcpy, msgs, gpkg_path, max_distance_m=5.0, preview=True):
        msgs.warn("ERROR: simulated join failure")
        return None
    monkeypatch.setattr(tools_impl, "join_dangling_ends", bad_join)
    res = run(a, lyr, out)
    assert res["steps"] == {"sewer": "ok", "pressure": "ok", "join": "failed", "register": "ok"}
    assert any(w.startswith("ERROR: The join step failed; continuing") for w in texts(a, "warn"))
    assert res["summary"]["stations_with_pump_data"] == 1 and res["summary"]["joins"] == 0


def test_one_pressure_layer_only_is_rejected_before_anything_runs(world, trace):
    a, lyr, out = world
    with pytest.raises(RuntimeError, match="both the pump station layer and the pressure main layer"):
        run(a, lyr, out, ps_layer=lyr["ps"], main_layer=None)
    assert not trace and not a.log


# ------------------------------------------------------------------ the .pyt tool

def load_pyt():
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_net", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_net", loader))
    loader.exec_module(mod)
    return mod


def test_pyt_tool_parameters(world):
    mod = load_pyt()
    t = mod.ImportNetworkData()
    assert t.label == "Import Network Data" and t.category == "Data"
    tools = [x().label for x in mod.Toolbox().tools]
    assert tools.index("Import Network Data") < tools.index("Import Sewer Data") \
        < tools.index("Import Pressure Data") and len(tools) == 23
    params = t.getParameterInfo()
    names = [p.name for p in params]
    assert len(set(names)) == len(names)
    assert names[:13] == ["ac_layer", "cp_layer", "sewer_layer", "model_gpkg", "use_selected", "ps_layer",
                          "main_layer", "ps_use_selected", "register", "register_config", "join_dangling",
                          "join_distance", "add_to_map"]
    assert names[-1] == "out_gpkg"
    by = {p.name: p for p in params}
    # same types / filters / defaults as the individual tools
    sew = {p.name: p for p in mod.ImportSewerData().getParameterInfo()}
    pres = {p.name: p for p in mod.ImportPressureData().getParameterInfo()}
    for n in ("ac_layer", "cp_layer", "sewer_layer", "use_selected", "load_cps", "cp_radius", "snap", "grade_source",
              "tee_join"):
        assert (by[n].datatype, by[n].parameterType, by[n].value, by[n].filter.list) == \
            (sew[n].datatype, sew[n].parameterType, sew[n].value, sew[n].filter.list)
    assert by["model_gpkg"].direction == "Output" and by["model_gpkg"].filter.list == ["gpkg"]
    for old, new in (("ps_layer", "ps_layer"), ("main_layer", "main_layer"), ("use_selected", "ps_use_selected"),
                     ("search", "ps_search"), ("snap", "ps_snap"), ("discharge", "ps_discharge"),
                     ("wet_well", "ps_wet_well"), ("include_n", "ps_include_n")):
        assert (by[new].datatype, by[new].value, by[new].filter.list) == \
            (pres[old].datatype, pres[old].value, pres[old].filter.list)
    assert (by["ps_search"].value, by["ps_snap"].value, by["ps_discharge"].value, by["ps_wet_well"].value) == \
        (30.0, 1.0, 1.0, 1.0)
    assert by["ps_use_selected"].value is True and by["ps_include_n"].value is False
    assert by["ps_layer"].parameterType == by["main_layer"].parameterType == "Optional"
    assert by["ps_layer"].filter.list == ["Point"] and by["main_layer"].filter.list == ["Polyline"]
    assert by["register"].parameterType == "Optional" and by["register"].filter.list == ["xlsx", "xlsm"]
    assert by["register_config"].filter.list == ["toml"]
    assert by["join_dangling"].value is True and by["join_distance"].value == 5.0
    assert by["add_to_map"].value is True and by["out_gpkg"].direction == "Output"
    assert [n for n in names if n.startswith("f_")] == ["f_" + r for r, *_ in sewer_import.FIELD_ROLES]
    assert [n for n in names if n.startswith("pf_")] == ["pf_" + r for r, *_ in pressure_import.FIELD_ROLES]
    assert by["pf_diameter"].parameterDependencies == ["main_layer"]
    assert by["pf_ps_id"].parameterDependencies == ["ps_layer"] and by["f_diameter"].parameterDependencies == \
        ["sewer_layer"]
    assert by["pf_ps_id"].parameterType == "Required"


def test_pyt_tool_execute(world):
    a, lyr, out = world
    mod = load_pyt()
    mod.arcpy = a
    t = mod.ImportNetworkData()
    params = t.getParameterInfo()
    by = {p.name: p for p in params}
    by["ac_layer"].value, by["sewer_layer"].value = lyr["ac"], lyr["sew"]
    by["ps_layer"].value, by["main_layer"].value = lyr["ps"], lyr["main"]
    t.updateParameters(params)                                          # field defaults from the layers
    assert by["f_ac_id"].value == "PACID" and by["pf_ps_id"].value == "SAP_ID" and by["pf_diameter"].value
    by["f_diameter"].value = "HEIGHT"
    by["model_gpkg"].value = out
    by["register"].value = str(DEMO_XLSX)
    t.execute(params, None)
    assert by["out_gpkg"].value == out
    assert headers(a)[:4] == ["== 1/4 Sewers ==", "== 2/4 Pressure ==", "== 3/4 Join dangling ends ==",
                              "== 4/4 Pump station register =="]
    assert q(out, "select count(*) from wwn_pump_stations") == [(1,)]
