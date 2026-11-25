"""SetPumpStationData tool class in WWNet.pyt: parameters, prefill, validation messages, execute."""
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path

import pytest

TOOLBOX = Path(__file__).resolve().parents[1]


@pytest.fixture
def pyt(fake_arcpy):
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_ps", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_ps", loader))
    loader.exec_module(mod)
    return mod


@pytest.fixture
def model(demo_gpkg):
    con = sqlite3.connect(demo_gpkg)
    geom = con.execute("SELECT geom FROM wwn_nodes WHERE node_id='N01'").fetchone()[0]
    con.execute("UPDATE wwn_nodes SET invert_level=18.0 WHERE node_id='N01'")
    con.execute("INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id, notes, wet_well_area_m2, "
                "start_level, stop_level, overflow_level, pump_rate_lps) VALUES (?, 'PS1', 'N01', 'Main PS', "
                "7.0, 19.5, 18.5, 20.5, 25.0)", (geom,))
    con.execute("INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id) VALUES (?, 'PS2', 'N02')", (geom,))
    con.execute("INSERT INTO wwn_pumps (pump_id, ps_id, role, curve_json) VALUES "
                "('PS1_P1','PS1','duty','[[0,30],[15,25],[30,10]]'), ('PS1_P2','PS1','standby',NULL)")
    con.commit()
    con.close()
    return demo_gpkg


def params(t):
    return t.getParameterInfo()


def by(ps):
    return {x.name: x for x in ps}


def test_parameters(pyt):
    t = pyt.SetPumpStationData()
    assert t.label == "Set Pump Station Data" and t.category == "Data"
    p = {x.name: x for x in params(t)}
    assert list(p) == ["gpkg", "station", "diameter_m", "area_m2", "level_mode", "floor_level", "stop_level",
                       "start_level",
                       "overflow_level", "emergency_m3", "n_duty", "n_assist", "n_standby", "duty_rate",
                       "pump_curve", "curve_order", "flow_units", "speed_pct", "source_note", "replace_pumps",
                       "out_gpkg"]
    assert p["gpkg"].datatype == "DEFile" and p["station"].filter.type == "ValueList"
    assert p["pump_curve"].controlCLSID == pyt.MULTILINE
    assert [p[n].value for n in ("n_duty", "n_assist", "n_standby", "speed_pct", "replace_pumps")] == \
        [1, 0, 0, 100.0, True]
    assert p["n_duty"].datatype == "GPLong" and p["level_mode"].filter.list == ["AHD", "Depth above wet well invert"]
    assert p["curve_order"].filter.list == ["flow,head", "head,flow"] and p["flow_units"].filter.list == ["L/s", "m3/h", "m3/s"]
    assert p["floor_level"].datatype == "GPDouble" and p["floor_level"].parameterType == "Optional"
    assert p["floor_level"].value is None and "invert" in t.description and "floor" in t.description
    assert "Required" == p["station"].parameterType and p["pump_curve"].parameterType == "Optional"


def test_update_parameters_fills_stations_and_prefills(pyt, model):
    t = pyt.SetPumpStationData()
    ps = params(t)
    ps[0].value = model
    t.updateParameters(ps)
    assert ps[1].filter.list == ["PS1 — Main PS", "PS2"]
    ps[0].hasBeenValidated = True
    ps[1].value = "PS1 — Main PS"
    t.updateParameters(ps)
    v = {x.name: x.value for x in ps}
    assert v["area_m2"] == 7.0 and v["level_mode"] == "AHD"
    assert (v["stop_level"], v["start_level"], v["overflow_level"]) == (18.5, 19.5, 20.5)
    assert (v["n_duty"], v["n_assist"], v["n_standby"]) == (1, 0, 1) and v["duty_rate"] == 25.0
    assert v["pump_curve"] == "0\t30\n15\t25\n30\t10"
    assert v["floor_level"] is None        # never prefilled
    ps[1].hasBeenValidated = True
    by(ps)["area_m2"].value = 9.0          # user edit after validation is not overwritten
    t.updateParameters(ps)
    assert by(ps)["area_m2"].value == 9.0
    # a station without data: defaults, empty fields
    ps[1].hasBeenValidated = False
    ps[1].value = "PS2"
    t.updateParameters(ps)
    v = {x.name: x.value for x in ps}
    assert v["area_m2"] is None and v["pump_curve"] is None and (v["n_duty"], v["n_assist"], v["n_standby"]) == (1, 0, 0)


def test_update_parameters_bad_gpkg_and_stale_station(pyt, model, tmp_path):
    t = pyt.SetPumpStationData()
    ps = params(t)
    ps[0].value = str(tmp_path / "missing.gpkg")
    ps[1].value = "PSX"
    t.updateParameters(ps)
    assert ps[1].filter.list == [] and ps[1].value is None


def test_update_messages_curve(pyt):
    t = pyt.SetPumpStationData()
    ps = params(t)
    q = by(ps)
    curve = by(ps)["pump_curve"]
    t.updateMessages(ps)                                  # no curve: nothing
    assert q["pump_curve"].message is None
    q["pump_curve"].value = "0\t30\n10\t40"
    t.updateMessages(ps)
    assert q["pump_curve"].message_level == "error" and "strictly decrease" in q["pump_curve"].message
    q["pump_curve"].clearMessage()
    q["pump_curve"].value = "10\t30\n20\t10"
    t.updateMessages(ps)
    assert q["pump_curve"].message_level == "warning" and "not zero flow" in q["pump_curve"].message
    q["pump_curve"].clearMessage()
    q["pump_curve"].value = "30\t0\n10\t36"
    q["curve_order"].value = "head,flow"
    q["flow_units"].value = "m3/h"
    t.updateMessages(ps)
    assert q["pump_curve"].message is None
    q["pump_curve"].value = "1 2 3"
    t.updateMessages(ps)
    assert q["pump_curve"].message_level == "error" and "expected 2" in q["pump_curve"].message


def test_execute_writes_gpkg(pyt, model, fake_arcpy):
    a = fake_arcpy([])                         # replaces arcpy for tools_impl messages
    pyt.arcpy = a
    t = pyt.SetPumpStationData()
    ps = params(t)
    q = by(ps)
    ps[0].value = model
    ps[1].value = "PS2"
    q["area_m2"].value = 4.0
    q["level_mode"].value = "Depth above wet well invert"
    q["stop_level"].value, q["start_level"].value, q["overflow_level"].value = 0.5, 1.5, 2.5
    q["emergency_m3"].value = 12.0
    q["n_duty"].value, q["n_assist"].value = 2, 1
    q["pump_curve"].value = "Flow\tHead\n0\t40\n36\t20\n72\t5"
    q["flow_units"].value = "m3/h"
    q["source_note"].value = "Data sheet PS2 rev C"
    t.execute(ps, None)
    con = sqlite3.connect(model)
    row = con.execute("SELECT wet_well_area_m2, stop_level, start_level, overflow_level, emergency_storage_m3, "
                      "duty_config, notes FROM wwn_pump_stations WHERE ps_id='PS2'").fetchone()
    inv = 25.3                                   # N02 invert in the demo model: depths are added to it
    assert row[:6] == (4.0, pytest.approx(inv + 0.5), pytest.approx(inv + 1.5), pytest.approx(inv + 2.5), 12.0,
                       "2 duty + 1 assist")
    pumps = con.execute("SELECT pump_id, role, curve_json FROM wwn_pumps WHERE ps_id='PS2' ORDER BY pump_id").fetchall()
    con.close()
    assert [p[1] for p in pumps] == ["duty", "duty", "assist"]
    assert "Data sheet PS2 rev C" in row[6]
    import json
    assert json.loads(pumps[0][2]) == [[0, 40], [10, 20], [20, 5]]       # m3/h -> L/s
    log = [t for _, t in a.log]
    assert any(t.startswith("PS2: wet well 4.00 m2") for t in log)
    assert "PS1: pump data complete" in log


def test_execute_data_error_is_warning_not_error(pyt, model, fake_arcpy):
    a = fake_arcpy([])
    pyt.arcpy = a
    t = pyt.SetPumpStationData()
    ps = params(t)
    q = by(ps)
    ps[0].value = model
    ps[1].value = "PS1 — Main PS"
    q["area_m2"].value, q["diameter_m"].value = 7.0, 9.0          # area and diameter disagree
    t.execute(ps, None)
    assert [lv for lv, _ in a.log] == ["warn"] and a.log[0][1].startswith("ERROR: Wet well area")
