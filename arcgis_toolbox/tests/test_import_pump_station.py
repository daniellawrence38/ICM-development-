"""Import Pump Station Data: tools_impl.import_pump_station_data, the .pyt tool class and the demo register."""
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path

import pytest

import make_pump_register_demo as demo
from wwnet_arcgis import pump_register, tools_impl, xlsx
from wwnet_arcgis.messages import Messages
from xlsx_fixture import write_xlsx

TOOLBOX = Path(__file__).resolve().parents[1]
DEMO_XLSX = TOOLBOX.parent / "examples" / "demo" / "pump_register_demo.xlsx"


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
    """The Self Test pump scenario: station PS1, wet well WW (invert 22.2, ground 25.5), no pump data."""
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'WW', 'wet_well', 22.2, 25.5 FROM wwn_nodes WHERE node_id='OUT1'",
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'D', 'ac', 25.0, 28.0 FROM wwn_nodes WHERE node_id='OUT1'",
          "UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id) "
          "VALUES ('PS1','WW','D','RM1')",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, roughness_type, "
          "roughness) VALUES ('RM1','PS1','D',150,200.0,'ks',1.0)")
    return demo_gpkg


def run(fake_arcpy, model, register, **kw):
    a = fake_arcpy([])
    res = tools_impl.import_pump_station_data(a, Messages(a), model, str(register), **kw)
    return a, res, [t for _, t in a.log]


STATE = "SELECT stop_level, start_level, overflow_level, wet_well_area_m2, pump_rate_lps FROM wwn_pump_stations"


def test_happy_path_messages_and_values(fake_arcpy, model):
    a, res, log = run(fake_arcpy, model, DEMO_XLSX)
    assert not any(lv == "error" for lv, _ in a.log)
    assert log[0] == ("Register: pump_register_demo.xlsx; matched 1 of 1 model pump station(s); "
                      "1 other station(s) in the register (not in this model).")
    assert log[1].startswith("PS1: floor 21.90, stop 22.50, start 23.00, overflow 24.50 m AHD")
    assert a.log[1][0] == "info"
    assert log[-2] == "Imported 1 station(s), 0 with errors, 0 skipped."
    assert log[-1].startswith("Next: Export To ICM writes their pumps to ICM_Pumps")
    assert _q(model, "SELECT invert_level, surface_level FROM wwn_nodes WHERE node_id='WW'") == [(21.9, 25.5)]
    stop, start, ovf, area, rate = _q(model, STATE)[0]
    assert (stop, start, ovf, area, rate) == (22.5, 23.0, 24.5, 7.0, 25.0)
    pumps = _q(model, "SELECT role FROM wwn_pumps WHERE ps_id='PS1' ORDER BY pump_id")
    assert [r[0] for r in pumps] == ["duty", "standby"]


def test_preview_writes_nothing(fake_arcpy, model):
    before = (_q(model, STATE), _q(model, "SELECT invert_level FROM wwn_nodes WHERE node_id='WW'"),
              _q(model, "SELECT count(*) FROM wwn_pumps"))
    a, res, log = run(fake_arcpy, model, DEMO_XLSX, preview=True)
    assert log[0] == "PREVIEW — nothing written."
    assert res["stations"][0]["status"] == "ok"
    assert log[-1] == "Imported 1 station(s), 0 with errors, 0 skipped."     # no "Next:" line
    after = (_q(model, STATE), _q(model, "SELECT invert_level FROM wwn_nodes WHERE node_id='WW'"),
             _q(model, "SELECT count(*) FROM wwn_pumps"))
    assert after == before


def test_not_in_register_and_error_station(fake_arcpy, model, tmp_path):
    _edit(model, "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id) VALUES ('PSX','D')")
    a, res, log = run(fake_arcpy, model, DEMO_XLSX)
    assert any(t.startswith("Model pump station(s) not in the register: PSX") and lv == "warn" for lv, t in a.log)
    # stop deeper than the base: an ERROR station, reported as warnings (never AddError)
    sh = demo.sheets()
    sh["Level Settings"][4]["Q"] = 5.0
    bad = tmp_path / "bad.xlsx"
    write_xlsx(bad, sh)
    a, res, log = run(fake_arcpy, model, bad)
    assert res["stations"][0]["status"] == "error"
    assert all(lv == "warn" for lv, t in a.log if "ERROR" in t) and any("ERROR:" in t for t in log)
    assert not any(lv == "error" for lv, _ in a.log)
    assert "Imported 0 station(s), 1 with errors, 0 skipped." in log
    assert not any(t.startswith("Next:") for t in log)


def test_bad_workbook_and_bad_config_are_error_warnings(fake_arcpy, model, tmp_path):
    notx = tmp_path / "notes.xlsx"
    notx.write_text("not a zip")
    a, res, log = run(fake_arcpy, model, notx)
    assert res is None and [lv for lv, _ in a.log] == ["warn"] and log[0].startswith("ERROR:")
    nosheet = tmp_path / "nosheet.xlsx"
    write_xlsx(nosheet, {"Other": {1: {"A": "x"}}})
    a, res, log = run(fake_arcpy, model, nosheet)
    assert res is None and [lv for lv, _ in a.log] == ["warn"] and "no sheet named 'Level Settings'" in log[0]
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[sheets.levels]\nname = 'x'\n")
    a, res, log = run(fake_arcpy, model, DEMO_XLSX, config=str(cfg))
    assert res is None and [lv for lv, _ in a.log] == ["warn"] and log[0].startswith("ERROR:")


def test_locked_geopackage_message(fake_arcpy, model, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(pump_register, "apply", boom)
    a, res, log = run(fake_arcpy, model, DEMO_XLSX)
    assert res is None and [lv for lv, _ in a.log] == ["warn"]
    assert log[0].startswith("ERROR: The model GeoPackage is locked by ArcGIS Pro")

    def other(*a, **k):
        raise sqlite3.OperationalError("no such table: nope")
    monkeypatch.setattr(pump_register, "apply", other)
    with pytest.raises(sqlite3.OperationalError):
        run(fake_arcpy, model, DEMO_XLSX)


# ------------------------------------------------------------------ the .pyt tool

@pytest.fixture
def pyt(fake_arcpy):
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_ipsd", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_ipsd", loader))
    loader.exec_module(mod)
    return mod


def test_tool_parameters_and_execute(pyt, model, fake_arcpy):
    t = pyt.ImportPumpStationData()
    assert t.label == "Import Pump Station Data" and t.category == "Data"
    ps = t.getParameterInfo()
    p = {x.name: x for x in ps}
    assert list(p) == ["gpkg", "register", "config", "preview", "out_gpkg"]
    assert p["register"].datatype == "DEFile" and p["register"].filter.list == ["xlsx", "xlsm"]
    assert p["config"].parameterType == "Optional" and p["config"].filter.list == ["toml"]
    assert p["preview"].datatype == "GPBoolean" and p["preview"].value is False
    assert p["preview"].name == "preview" and "Preview only" in p["preview"].displayName
    assert p["out_gpkg"].direction == "Output"
    for word in ("Level Settings", "Civil", "Motor and Pump Attributes", "functional location", "TOML",
                 "Re-run after Import Sewer Data"):
        assert word in t.description
    a = fake_arcpy([])
    pyt.arcpy = a
    p["gpkg"].value, p["register"].value = model, str(DEMO_XLSX)
    t.execute(ps, None)
    assert p["out_gpkg"].value == model
    assert _q(model, STATE)[0][0] == 22.5
    assert any(t_.startswith("Imported 1 station(s)") for _, t_ in a.log)


# ------------------------------------------------------------------ the demo register

def test_committed_demo_register_matches_the_generator(tmp_path):
    fresh = demo.build(tmp_path / "fresh.xlsx")
    names = xlsx.sheet_names(fresh)
    assert names == xlsx.sheet_names(DEMO_XLSX) == ["Level Settings", "Civil", "Motor and Pump Attributes"]
    assert xlsx.read_workbook(DEMO_XLSX, names) == xlsx.read_workbook(fresh, names)
    cfg = pump_register.load_config()
    reg = pump_register.read_register(DEMO_XLSX, cfg)
    assert reg["counts"] == {"levels": 2, "civil": 2, "pumps": 4} and reg["warnings"] == []
    # real-register layout: title row 1, header row 3, data from row 4, functional location in C
    rows = xlsx.read_workbook(DEMO_XLSX, ["Level Settings"])["Level Settings"]
    assert rows[0][0] == 1 and rows[1][0] == 3 and rows[1][1]["C"] == "Functional Location"
    assert reg["sheets"]["levels"]["rows"][0]["fl"] == "PS1"
    pumps = reg["sheets"]["pumps"]["rows"]
    assert pumps[0]["v"] == pumps[1]["v"] and pumps[2]["v"] == pumps[3]["v"]


def test_demo_toml_matches_generator():
    toml = DEMO_XLSX.with_suffix(".toml")
    assert toml.read_text(encoding="utf-8") == demo.toml_text()
    cfg = pump_register.load_config(str(toml))
    for sk, (name, cols) in demo.COLUMNS.items():
        assert cfg["sheets"][sk]["name"] == name
        assert cfg["sheets"][sk]["key"] == cols["key"]
        assert cfg["sheets"][sk]["cols"] == {k: v for k, v in cols.items() if k != "key"}
