import json
import math
import sqlite3

import pytest

from wwnet_arcgis import pumpdata as pd


@pytest.fixture
def gp(demo_gpkg):
    con = sqlite3.connect(demo_gpkg)
    con.execute("UPDATE wwn_nodes SET invert_level=18.0 WHERE node_id='N01'")
    con.execute("DELETE FROM wwn_pumps")
    con.execute("DELETE FROM wwn_pump_stations")
    con.execute("INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, notes) VALUES ('PS1','N01','Main PS')")
    con.execute("INSERT INTO wwn_pump_stations (ps_id, notes) VALUES ('PS2','bare')")
    con.commit()
    con.close()
    return demo_gpkg


def snap(gp):
    con = sqlite3.connect(gp)
    r = (con.execute("SELECT * FROM wwn_pump_stations ORDER BY ps_id").fetchall(),
         con.execute("SELECT * FROM wwn_pumps ORDER BY pump_id").fetchall())
    con.close()
    return r


CURVE = [(0, 30), (15, 25), (30, 10)]


def test_parse_tabs_header():
    pts, w = pd.parse_curve("Flow\tHead\n0\t30\n15\t25\n30\t10\n")
    assert pts == [(0, 30), (15, 25), (30, 10)] and w == []


def test_parse_commas_semicolon_whitespace():
    assert pd.parse_curve("0,30\n10,20")[0] == [(0, 30), (10, 20)]
    assert pd.parse_curve("0;30\n10;20")[0] == [(0, 30), (10, 20)]
    assert pd.parse_curve("0  30\n10 20")[0] == [(0, 30), (10, 20)]


def test_parse_decimal_comma_and_sort():
    pts, _ = pd.parse_curve("10,5\t20,5\n0\t30,25")
    assert pts == [(0, 30.25), (10.5, 20.5)]
    assert pd.parse_curve("1,5;9\n3,5;5")[0] == [(1.5, 9), (3.5, 5)]


def test_parse_head_flow_and_units():
    pts, _ = pd.parse_curve("30\t0\n20\t36", order="head_flow")
    assert pts == [(0, 30), (36, 20)]
    pts, _ = pd.parse_curve("0\t30\n36\t20", flow_units="m3/h")
    assert pts[1][0] == pytest.approx(10.0)
    pts, _ = pd.parse_curve("0\t30\n0.01\t20", flow_units="m3/s")
    assert pts[1][0] == pytest.approx(10.0)


def test_parse_warning_no_zero_flow():
    pts, w = pd.parse_curve("5\t30\n10\t20")
    assert len(w) == 1


@pytest.mark.parametrize("text", ["Flow Head", "0 30", "0 -3\n5 2", "0 30\n0 20",
                                  "0 10\n5 20", "0 10\n5 10", "0 30 1\n5 20 1"])
def test_parse_errors(text):
    with pytest.raises(ValueError):
        pd.parse_curve(text)


def test_set_station_ahd(gp):
    r = pd.set_station(gp, "PS1", wet_well_diameter_m=3.0, start=19.5, stop=18.5, overflow=20.5,
                       emergency_storage_m3=50, n_duty=1, n_assist=1, curve=CURVE,
                       duty_rate_lps=25, source_note="Sheet A")
    assert r["pumps"] == ["PS1_P1", "PS1_P2"]
    assert "7.07 m2 (3.00 m dia)" in r["summary"] and "1 duty + 1 assist" in r["summary"]
    assert "curve 3 pts (0-30 L/s)" in r["summary"]
    s = pd.list_stations(gp)[0]
    assert s["wet_well_area_m2"] == pytest.approx(math.pi * 9 / 4)
    assert (s["stop_level"], s["start_level"], s["overflow_level"]) == (18.5, 19.5, 20.5)
    assert s["duty_config"] == "1 duty + 1 assist"
    assert s["pumps"] == {"duty": 1, "assist": 1, "standby": 0}
    assert s["curve"] == [[0, 30], [15, 25], [30, 10]]
    assert s["wet_well_invert"] == 18.0 and s["pump_rate_lps"] == 25


def test_set_station_depth(gp):
    pd.set_station(gp, "PS1", wet_well_area_m2=5, level_mode="depth", start=1.5, stop=0.5, overflow=2.5,
                   duty_rate_lps=20)
    s = pd.list_stations(gp)[0]
    assert (s["stop_level"], s["start_level"], s["overflow_level"]) == (18.5, 19.5, 20.5)


def test_depth_without_invert(gp):
    with pytest.raises(ValueError, match="AHD"):
        pd.set_station(gp, "PS2", level_mode="depth", start=1.5, stop=0.5)


def test_area_diameter_conflict(gp):
    with pytest.raises(ValueError):
        pd.set_station(gp, "PS1", wet_well_diameter_m=3.0, wet_well_area_m2=9.0)
    pd.set_station(gp, "PS1", wet_well_diameter_m=3.0, wet_well_area_m2=7.1, duty_rate_lps=5)


@pytest.mark.parametrize("kw", [
    dict(start=18.0, stop=19.0), dict(start=19.5, stop=19.45), dict(start=19.5, stop=18.5, overflow=19.0),
    dict(start=19.5, stop=17.0), dict(wet_well_area_m2=0), dict(duty_rate_lps=-1),
    dict(n_duty=0), dict(curve=[(0, 1), (0, 0)]),
])
def test_validation_leaves_db_unchanged(gp, kw):
    before = snap(gp)
    with pytest.raises(ValueError):
        pd.set_station(gp, "PS1", **kw)
    assert snap(gp) == before


def test_unknown_station(gp):
    with pytest.raises(ValueError, match="not found"):
        pd.set_station(gp, "NOPE")


def test_short_cycle_and_ideal_pump_warnings(gp):
    r = pd.set_station(gp, "PS1", start=18.8, stop=18.6)
    assert any("short cycling" in w for w in r["warnings"])
    assert any("ideal pump" in w for w in r["warnings"])


def test_pumps_replaced_roles_curve(gp):
    pd.set_station(gp, "PS1", n_duty=2, n_assist=1, n_standby=1, curve=CURVE, speed_pct=90)
    pd.set_station(gp, "PS1", n_duty=1, n_standby=1, curve=CURVE)
    con = sqlite3.connect(gp)
    rows = con.execute("SELECT pump_id, role, curve_json, speed_pct FROM wwn_pumps WHERE ps_id='PS1' ORDER BY pump_id").fetchall()
    con.close()
    assert [(r[0], r[1]) for r in rows] == [("PS1_P1", "duty"), ("PS1_P2", "standby")]
    assert json.loads(rows[0][2]) == [[0, 30], [15, 25], [30, 10]] and rows[0][3] == 100


def test_notes_tag_replaced(gp):
    pd.set_station(gp, "PS1", duty_rate_lps=10, source_note="first")
    pd.set_station(gp, "PS1", duty_rate_lps=10, source_note="second")
    n = pd.list_stations(gp)[0]["notes"]
    assert n.startswith("Main PS") and n.count("[pump data:") == 1 and "second" in n and "first" not in n


def test_partial_update(gp):
    pd.set_station(gp, "PS1", wet_well_area_m2=8, start=19.5, stop=18.5, duty_rate_lps=20)
    r = pd.set_station(gp, "PS1", emergency_storage_m3=40, replace_pumps=False)
    assert set(r["updated_fields"]) == {"emergency_storage_m3", "notes"}
    s = pd.list_stations(gp)[0]
    assert s["wet_well_area_m2"] == 8 and s["start_level"] == 19.5 and s["pump_rate_lps"] == 20
    assert s["pumps"]["duty"] == 1
    with pytest.raises(ValueError):  # merged with existing stop
        pd.set_station(gp, "PS1", start=18.0, replace_pumps=False)


def test_report(gp):
    lines = pd.station_report(gp)
    assert "wet_well_area_m2" in lines[0] and "pumps" in lines[0]
    assert "wet well node" in lines[1]
    pd.set_station(gp, "PS1", wet_well_area_m2=8, start=19.5, stop=18.5, curve=CURVE)
    assert pd.station_report(gp)[0] == "PS1: pump data complete"
