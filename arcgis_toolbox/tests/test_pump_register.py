"""Pump station register import (pump_register.py): xlsx workbooks built with xlsx_fixture."""
import json
import sqlite3

import pytest

from wwnet_arcgis import icm_export, pump_register as pr
from xlsx_fixture import write_xlsx

FL = "Functional Location"


def _edit(gp, *sql):
    con = sqlite3.connect(gp)
    for s in sql:
        con.execute(s)
    con.commit()
    con.close()


def _q(gp, sql, *a):
    con = sqlite3.connect(gp)
    try:
        return con.execute(sql, a).fetchall()
    finally:
        con.close()


@pytest.fixture
def model(demo_gpkg):
    """Self Test pump scenario (P08 -> wet well WW, invert 22.2 above the real stop level; RM1 to D)
    as Import Pressure Data leaves it: no pump data."""
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'WW', 'wet_well', 22.2, 25.5 FROM wwn_nodes WHERE node_id='OUT1'",
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'D', 'ac', 25.0, 28.0 FROM wwn_nodes WHERE node_id='OUT1'",
          "UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'",
          "INSERT INTO wwn_conduits (conduit_id, us_node_id, ds_node_id, dn, length_m, us_invert, ds_invert) "
          "VALUES ('PSG1','D','OUT1',225,50.0,25.0,22.2)",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id) "
          "VALUES ('S0037310','WW','D','RM1')",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, roughness_type, "
          "roughness, material) VALUES ('RM1','S0037310','D',150,200.0,'ks',1.0,'pvc')")
    return demo_gpkg


def lev(fl, ground=26.0, base=4.5, stop=4.0, start=3.2, alarm=1.6, overflow=1.0):
    return {"C": fl, "E": ground, "F": overflow, "G": stop, "L": alarm, "P": start, "T": base}


def civ(fl, storage=2.1, emerg=120.0):
    return {"C": fl, "R": storage, "AB": emerg}


def pmp(fl, q=25.0, h=32.0):
    return {"C": fl, "AF": q, "AG": h}


def book(path, levels=None, civil=None, pumps=None, extra_levels=()):
    """Workbook with a title row and a header row above the data; rows: lists of cell dicts."""
    def sheet(title, hdr, rows):
        d = {1: {"A": title}, 3: {"C": FL, **hdr}}
        for i, r in enumerate(rows, 4):
            d[i] = r
        return d
    lv = [lev("S0037310")] if levels is None else levels
    cv = [civ("S0037310")] if civil is None else civil
    pm = [pmp("S0037310")] if pumps is None else pumps
    write_xlsx(path, {
        "Level Settings": sheet("Level settings register", {"E": "Ground", "F": "Overflow", "G": "Stop"},
                                list(lv) + list(extra_levels)),
        "Civil": sheet("Civil", {"R": "Storage", "AB": "Emergency"}, cv),
        "Motor and Pump Attributes": sheet("Motors", {"AF": "Flow", "AG": "Head"}, pm)})
    return str(path)


@pytest.fixture
def xl(tmp_path):
    return lambda **kw: book(tmp_path / "register.xlsx", **kw)


def station(res, ps="S0037310"):
    return next(s for s in res["stations"] if s["ps_id"] == ps)


# ------------------------------------------------------------------ happy path

def test_end_to_end_to_icm_export(model, xl):
    res = pr.apply(model, xl())
    st = station(res)
    assert st["status"] == "ok" and not [m for m in st["messages"] if m.startswith("ERROR")]
    ps = _q(model, "SELECT wet_well_area_m2, start_level, stop_level, overflow_level, emergency_storage_m3, "
                   "pump_rate_lps, duty_config, notes FROM wwn_pump_stations")[0]
    assert ps[0] == pytest.approx(3.5) and ps[1:4] == pytest.approx((22.8, 22.0, 25.0))
    assert ps[4] == 120 and ps[5] == 25 and ps[6] == "1 duty + 1 standby"
    assert "register.xlsx; design 25 L/s @ 32 m" in ps[7]
    assert _q(model, "SELECT role FROM wwn_pumps ORDER BY pump_id") == [("duty",), ("standby",)]
    assert _q(model, "SELECT invert_level, surface_level FROM wwn_nodes WHERE node_id='WW'") == [(21.5, 26.0)]
    # incoming conduit keeps its own invert
    assert _q(model, "SELECT ds_invert FROM wwn_conduits WHERE conduit_id='P08'")[0][0] != 21.5
    line = res["lines"][0]
    assert line.startswith("S0037310: floor 21.50, stop 22.00, start 22.80, overflow 25.00 m AHD; wet well 3.50 m2")
    assert "emergency storage 120 m3; 1 duty + 1 standby at 25 L/s (design head 32 m)." in line
    assert res["unmatched_model"] == [] and res["n_register_stations"] == 1

    d = icm_export.build(model)
    pumps = d["pumps"]
    assert len(pumps) == 1 and pumps[0]["link_type"] == "Fixpmp" and pumps[0]["discharge"] == pytest.approx(25.0)
    assert pumps[0]["switch_on_level"] == pytest.approx(22.8) and pumps[0]["switch_off_level"] == pytest.approx(22.0)
    ww = next(n for n in d["nodes"] if n["node_id"] == "WW")
    assert ww["chamber_floor"] == pytest.approx(21.5) and ww["chamber_area"] == pytest.approx(3.5)


def test_engine_simulation_runs(model, xl):
    nat = pytest.importorskip("wwnet_native")
    pr.apply(model, xl())
    r = nat.simulate(model, duration_h=6.0, warmup_h=2.0, write=False)
    assert r["summary"]["conduits"] > 0


# ------------------------------------------------------------------ units

def test_mm_depths(model, xl):
    levels = [lev("S0037310", base=4500.0, stop=4000.0, start=3200.0, alarm=1600.0, overflow=1000.0)]
    cfg = pr.load_config()
    cfg["depth_unit"], cfg["depth_factor"] = "mm", 0.001
    pr.apply(model, xl(levels=levels), cfg)
    assert _q(model, "SELECT stop_level, wet_well_area_m2 FROM wwn_pump_stations")[0] == pytest.approx((22.0, 3.5))


@pytest.mark.parametrize("unit,q,expect", [("m3/h", 90.0, 25.0), ("ML/d", 2.16, 25.0)])
def test_flow_units(model, xl, unit, q, expect):
    cfg = pr.load_config()
    cfg["flow_unit"], cfg["flow_factor"] = unit, pr._FLOW_FACTOR[unit.lower()]
    pr.apply(model, xl(pumps=[pmp("S0037310", q=q)]), cfg)
    assert _q(model, "SELECT pump_rate_lps FROM wwn_pump_stations")[0][0] == pytest.approx(expect)


# ------------------------------------------------------------------ levels

def test_missing_ground_imports_other_data(model, xl):
    res = pr.apply(model, xl(levels=[lev("S0037310", ground=None)]))
    st = station(res)
    assert st["status"] == "ok" and any("Level Settings E4" in m and "no design ground" in m for m in st["messages"])
    ps = _q(model, "SELECT stop_level, wet_well_area_m2, pump_rate_lps FROM wwn_pump_stations")[0]
    assert ps[0] is None and ps[1] == pytest.approx(3.5) and ps[2] == 25
    assert _q(model, "SELECT invert_level FROM wwn_nodes WHERE node_id='WW'")[0][0] == 22.2


def test_floor_not_below_stop_is_error_other_station_imports(model, xl):
    _edit(model, "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
                 "SELECT geom, 'WW2', 'wet_well', 22.2, 25.5 FROM wwn_nodes WHERE node_id='OUT1'",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id) VALUES ('S0099999','WW2')")
    levels = [lev("S0037310", base=4.0, stop=4.0), lev("S0099999")]
    res = pr.apply(model, xl(levels=levels, civil=[civ("S0037310"), civ("S0099999")],
                             pumps=[pmp("S0037310"), pmp("S0099999")]))
    bad, good = station(res), station(res, "S0099999")
    assert bad["status"] == "error" and bad["messages"][0].startswith("ERROR:")
    assert "Level Settings T4" in bad["messages"][0] and "G4" in bad["messages"][0]
    assert good["status"] == "ok"
    assert _q(model, "SELECT stop_level FROM wwn_pump_stations WHERE ps_id='S0037310'")[0][0] is None
    assert _q(model, "SELECT stop_level FROM wwn_pump_stations WHERE ps_id='S0099999'")[0][0] == pytest.approx(22.0)
    assert _q(model, "SELECT invert_level FROM wwn_nodes WHERE node_id='WW'")[0][0] == 22.2


def test_order_and_gap_errors(model, xl):
    for kw in ({"stop": 3.0, "start": 3.05}, {"alarm": 3.5}, {"overflow": 2.0}):
        res = pr.apply(model, xl(levels=[lev("S0037310", **kw)]), preview=True)
        assert station(res)["status"] == "error", kw


def test_negative_depth_ignored(model, xl):
    res = pr.apply(model, xl(levels=[lev("S0037310", overflow=-0.5)]))
    st = station(res)
    assert st["status"] == "ok" and any("negative depth" in m for m in st["messages"])
    assert _q(model, "SELECT overflow_level, wet_well_area_m2 FROM wwn_pump_stations")[0] == (None, None)
    assert any("No wet well area" in m for m in st["messages"])


def test_area_diameter_check_warning(model, xl):
    res = pr.apply(model, xl(civil=[civ("S0037310", storage=0.3)]))
    assert any("outside 1-10 m" in m for m in station(res)["messages"])


# ------------------------------------------------------------------ pumps

def test_sub_location_pump_rows_and_different_second_row(model, xl):
    pumps = [pmp("S0037310-PMP01", 25.0, 32.0), pmp("S0037310-PMP02", 40.0, 28.0), pmp("S0037311", 99, 9)]
    res = pr.apply(model, xl(pumps=pumps))
    st = station(res)
    flag = [m for m in st["messages"] if "more pump row(s) with different test data not used" in m]
    assert len(flag) == 1 and "40 L/s @ 28 m" in flag[0] and "future enhancement" in flag[0]
    assert _q(model, "SELECT pump_rate_lps, duty_config FROM wwn_pump_stations")[0] == (25.0, "1 duty + 1 standby")
    assert res["n_register_stations"] == 2 and res["n_register_only"] == 1


def test_identical_second_row_not_flagged_and_standby_count(model, xl):
    cfg = pr.load_config()
    cfg["n_standby"] = 0
    pumps = [pmp("S0037310", 25.0, 32.0), pmp("S0037310", 25.1, 32.1), pmp("S0037310", 25.0, 32.0)]
    res = pr.apply(model, xl(pumps=pumps), cfg)
    assert not any("different test data" in m for m in station(res)["messages"])
    assert _q(model, "SELECT duty_config FROM wwn_pump_stations")[0][0] == "1 duty + 2 standby"


def test_existing_curve_kept(model, xl):
    from wwnet_arcgis import pumpdata
    pumpdata.set_station(model, "S0037310", curve=[(0, 30), (15, 25), (30, 10)], n_standby=1)
    res = pr.apply(model, xl())
    st = station(res)
    assert any("existing pump curve kept" in m for m in st["messages"])
    assert len(_q(model, "SELECT pump_id FROM wwn_pumps WHERE curve_json IS NOT NULL")) == 2
    assert json.loads(_q(model, "SELECT curve_json FROM wwn_pumps")[0][0])[1] == [15, 25]
    assert _q(model, "SELECT pump_rate_lps, stop_level FROM wwn_pump_stations")[0] == (25.0, 22.0)


def test_no_pump_row_keeps_pumps(model, xl):
    res = pr.apply(model, xl(pumps=[pmp("S0037310", q="N/A", h="N/A")]))
    st = station(res)
    assert st["status"] == "ok" and any("no pump row with a design flow" in m for m in st["messages"])
    assert _q(model, "SELECT COUNT(*) FROM wwn_pumps") == [(0,)]
    assert _q(model, "SELECT stop_level FROM wwn_pump_stations")[0][0] == pytest.approx(22.0)


# ------------------------------------------------------------------ matching, counting, preview

def test_unmatched_and_register_only(model, xl):
    _edit(model, "INSERT INTO wwn_pump_stations (ps_id) VALUES ('S0000001')")
    other = [f"S{i:07d}" for i in range(500, 505)]
    res = pr.apply(model, xl(levels=[lev("S0037310")] + [lev(o) for o in other], civil=[civ("S0037310")],
                             pumps=[pmp("S0037310")] + [pmp(o) for o in other]))
    assert res["unmatched_model"] == ["S0000001"]
    assert res["n_register_only"] == 5 and res["n_register_stations"] == 6
    text = "\n".join(res["lines"] + res["warnings"] + [m for s in res["stations"] for m in s["messages"]])
    assert "S0000500" not in text


def test_functional_location_normalised(model, xl):
    res = pr.apply(model, xl(levels=[lev(" s0037310 ")], civil=[civ("s0037310")], pumps=[pmp("S0037310 ")]))
    assert station(res)["status"] == "ok"
    assert _q(model, "SELECT stop_level FROM wwn_pump_stations")[0][0] == pytest.approx(22.0)


def test_preview_writes_nothing(model, xl):
    con = sqlite3.connect(model)
    before = list(con.iterdump())
    con.close()
    res = pr.apply(model, xl(), preview=True)
    assert station(res)["status"] == "ok" and res["lines"][0].startswith("S0037310: floor 21.50")
    con = sqlite3.connect(model)
    assert list(con.iterdump()) == before
    con.close()
    # preview still reports pumpdata validation errors
    _edit(model, "UPDATE wwn_pump_stations SET wet_well_node_id='NOPE'")
    res = pr.apply(model, xl(), preview=True)
    assert station(res)["status"] == "error" and "does not exist" in station(res)["messages"][0]


def test_duplicate_level_rows_warn(model, xl):
    res = pr.apply(model, xl(extra_levels=[lev("S0037310", stop=3.9)]))
    st = station(res)
    assert any("2 rows match" in m and "first (row 4)" in m for m in st["messages"])
    assert _q(model, "SELECT stop_level FROM wwn_pump_stations")[0][0] == pytest.approx(22.0)


def test_non_numeric_cells(model, xl):
    res = pr.apply(model, xl(levels=[lev("S0037310", overflow="N/A", alarm=" 1.6 ")],
                             civil=[civ("S0037310", emerg="n/a")]))
    st = station(res)
    assert st["status"] == "ok"
    ps = _q(model, "SELECT overflow_level, emergency_storage_m3, stop_level FROM wwn_pump_stations")[0]
    assert ps[0] is None and ps[1] is None and ps[2] == pytest.approx(22.0)
    reg = pr.read_register(xl(levels=[lev("S0037310", overflow="N/A")]), pr.load_config())
    assert reg["sheets"]["levels"]["rows"][0]["v"]["overflow"] is None
    assert reg["counts"] == {"levels": 1, "civil": 1, "pumps": 1}  # title / header rows are skipped
    assert reg["sheets"]["levels"]["rows"][0]["row"] == 4


# ------------------------------------------------------------------ errors and config

def test_missing_sheet_error(model, tmp_path):
    p = tmp_path / "x.xlsx"
    write_xlsx(p, {"Level Settings": {1: {"C": "S1", "E": 1.0}}, "Other": {1: {"A": "x"}}})
    with pytest.raises(ValueError, match=r"no sheet named 'Civil'.*'Level Settings'.*'Other'"):
        pr.apply(model, str(p))


def test_default_config_values():
    cfg = pr.load_config()
    assert cfg["sheets"]["levels"]["cols"]["base"] == "T" and cfg["sheets"]["pumps"]["cols"]["design_head"] == "AG"
    assert cfg["sheets"]["civil"]["cols"]["emergency_storage"] == "AB"
    assert (cfg["n_duty"], cfg["n_standby"], cfg["depth_unit"], cfg["flow_unit"]) == (1, 1, "m", "L/s")


@pytest.mark.parametrize("old,new,key", [
    ('stop = "G"', 'stop = "G1"', "sheets.levels.stop"),
    ('depth = "m"', 'depth = "cm"', "units.depth"),
    ('flow = "L/s"', 'flow = "gpm"', "units.flow"),
    ("n_standby = 1", "n_standby = -1", "pumps.n_standby")])
def test_bad_config_names_key(tmp_path, old, new, key):
    from wwnet_arcgis import shared
    src = shared.core("paths").find_resource("templates/import", pr.TEMPLATE_NAME).read_text()
    assert old in src
    p = tmp_path / "t.toml"
    p.write_text(src.replace(old, new, 1))
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        pr.load_config(p)


@pytest.mark.parametrize("mark", ["", "'", "’"])
def test_values_stored_as_text(model, xl, mark):
    """Registers exported with every value as text ('0.47): numbers and the functional location still read."""
    def txt(d):
        return {k: f"{mark}{v:g}" if isinstance(v, float) else f"{mark}{v}" for k, v in d.items()}
    res = pr.apply(model, xl(levels=[txt(lev("S0037310"))], civil=[txt(civ("S0037310"))],
                             pumps=[txt(pmp("S0037310"))]))
    st = station(res)
    assert st["status"] == "ok", st["messages"]
    v = st["values"]
    assert v["wet_well_floor"] == pytest.approx(21.5) and v["stop"] == pytest.approx(22.0)
    assert v["start"] == pytest.approx(22.8) and v["overflow"] == pytest.approx(25.0)
    assert v["duty_rate_lps"] == pytest.approx(25.0) and v["emergency_storage_m3"] == pytest.approx(120.0)
    assert v["wet_well_area_m2"] == pytest.approx(2.1 / 0.6, rel=1e-3)
    assert _q(model, "SELECT invert_level FROM wwn_nodes WHERE node_id='WW'")[0][0] == pytest.approx(21.5)
    assert pr._number(" '1,250.5 ") == pytest.approx(1250.5) and pr._number("'N/A") is None
