"""Export To ICM: field mapping, link suffixes, chamber-floor fallback, geodatabase + CSV output."""
import csv
import math
import os
import sqlite3

import pytest

from wwnet_arcgis import icm_export, tools_impl
from wwnet_arcgis.messages import Messages


def test_build_maps_demo_model(demo_gpkg):
    d = icm_export.build(demo_gpkg)
    nodes = {n["node_id"]: n for n in d["nodes"]}
    links = {c["wwn_conduit_id"]: c for c in d["conduits"]}
    assert len(nodes) == 12 and len(links) == 11
    assert nodes["N01"]["node_type"] == "Manhole" and nodes["OUT1"]["node_type"] == "Outfall"
    assert nodes["N01"]["ground_level"] == 28.0 and nodes["N01"]["chamber_floor"] == 26.0
    assert nodes["N01"]["chamber_area"] == pytest.approx(math.pi * 1.05 ** 2 / 4, abs=1e-4)
    assert nodes["OUT1"]["chamber_area"] is None
    p = links["P03"]
    assert (p["us_node_id"], p["link_suffix"], p["ds_node_id"]) == ("N03", "1", "N04")
    assert p["conduit_width"] == p["conduit_height"] == 225 and p["conduit_shape"] == "CIRC"
    assert p["roughness_type"] == "CW" and p["bottom_roughness_CW"] == 1.0 and p["conduit_length"] == 70.0
    assert any("wwn_subcatchments: 4" in w for w in d["warnings"])


def test_suffixes_unique_and_floor_fallback(demo_gpkg):
    con = sqlite3.connect(demo_gpkg)
    # second conduit leaving N03 (a branch), a node without invert, a conduit without length
    con.execute("INSERT INTO wwn_conduits (geom, conduit_id, us_node_id, ds_node_id, dn, us_invert, ds_invert, "
                "length_m, ks_mm, internal_diameter_mm) SELECT geom, 'P99', 'N03', 'N08', 150, 24.6, 22.6, NULL, "
                "0.6, 142.0 FROM wwn_conduits WHERE conduit_id='P03'")
    con.execute("UPDATE wwn_nodes SET invert_level=NULL, surface_level=NULL WHERE node_id='N05'")
    con.execute("UPDATE wwn_conduits SET status='abandoned' WHERE conduit_id='P10'")
    con.commit()
    con.close()
    d = icm_export.build(demo_gpkg)
    keys = [(c["us_node_id"], c["link_suffix"]) for c in d["conduits"]]
    assert len(keys) == len(set(keys)) == 11          # P10 abandoned -> not exported
    by = {c["wwn_conduit_id"]: c for c in d["conduits"]}
    assert {by["P03"]["link_suffix"], by["P99"]["link_suffix"]} == {"1", "2"}
    assert by["P99"]["conduit_width"] == 142.0 and by["P99"]["bottom_roughness_CW"] == 0.6
    assert by["P99"]["conduit_length"] == pytest.approx(70.0, abs=1.0)     # from geometry
    n5 = next(n for n in d["nodes"] if n["node_id"] == "N05")
    assert n5["chamber_floor"] == 23.9 and n5["wwn_floor_source"] == "lowest connected invert"
    assert n5["ground_level"] is None
    text = " ".join(d["warnings"])
    assert "more than one outgoing" in text and "no surface level" in text and "lowest connected" in text


def test_export_icm_writes_gdb_csv_and_notes(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    out = tmp_path / "icm"
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    gdb = str(out / "demo_ICM.gdb")
    assert r["gdb"] == gdb and gdb in a.gdbs
    nodes_fc, links_fc = a.memory[os.path.join(gdb, "ICM_Nodes")], a.memory[os.path.join(gdb, "ICM_Conduits")]
    assert nodes_fc["geom"] == "POINT" and links_fc["geom"] == "POLYLINE"
    assert [f for f, _ in nodes_fc["fields"]] == [f for f, _ in icm_export.NODE_FIELDS]
    assert len(nodes_fc["rows"]) == 12 and len(links_fc["rows"]) == 11
    assert all(row["SHAPE@"] is not None for row in links_fc["rows"])
    with open(r["nodes_csv"], newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 12 and rows[0]["x"] and rows[0]["y"]
    assert next(x for x in rows if x["node_id"] == "OUT1")["chamber_area"] == ""     # blank, not 0
    with open(r["conduits_csv"], newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 11
    notes = open(r["notes"], encoding="utf-8").read()
    assert "NODES FIRST" in notes and "wwn_subcatchments" in notes
    assert a.env.geographicTransformations is None                 # GDA2020 out: no transformation
    # second run replaces the feature classes
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    assert len(a.memory[os.path.join(gdb, "ICM_Nodes")]["rows"]) == 12
    assert any("Replacing" in t for _, t in a.log)


def test_export_icm_gda94_sets_transformation(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), "GDA94 / MGA zone 50 (EPSG:28350)")
    assert a.env.geographicTransformations == "GDA_1994_To_GDA2020_NTv2_Conformal_and_Distortion"
    assert a.memory[os.path.join(str(tmp_path / "demo_ICM.gdb"), "ICM_Nodes")]["sr"].factoryCode == 28350


def test_export_icm_missing_model(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    with pytest.raises(RuntimeError, match="not found"):
        tools_impl.export_icm(a, Messages(a), str(tmp_path / "nope.gpkg"), str(tmp_path))


def test_field_names_avoid_reserved():
    names = [n for n, _ in icm_export.NODE_FIELDS + icm_export.CONDUIT_FIELDS]
    assert not [n for n in names if n.lower() in icm_export.RESERVED_FIELDS]
    assert "shape" not in [n.lower() for n, _ in icm_export.CONDUIT_FIELDS]


def test_fake_add_field_rejects_reserved_and_duplicates():
    from fake_arcpy import make_fake
    a = make_fake()
    a.management.CreateFeatureclass("memory", "t", "POINT")
    for bad in ("Shape", "OBJECTID", "x"):
        if bad == "x":
            a.management.AddField("memory\\t", "x", "TEXT")
        with pytest.raises(Exception, match="ERROR 000012.*already exists"):
            a.management.AddField("memory\\t", bad, "TEXT")


def test_write_gdb_uses_one_addfields_per_fc(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    assert a.addfields_calls == 2
    assert not [x for x in a.added if x[0] == "field"]


def test_write_gdb_falls_back_to_addfield(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    del a.management.AddFields
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    nodes_fc = a.memory[os.path.join(str(tmp_path / "demo_ICM.gdb"), "ICM_Nodes")]
    assert [f for f, _ in nodes_fc["fields"]] == [f for f, _ in icm_export.NODE_FIELDS]


def _null_two(demo_gpkg):
    from fake_arcpy import gpkg_point_xy
    con = sqlite3.connect(demo_gpkg)
    con.execute("UPDATE wwn_nodes SET surface_level=NULL WHERE node_id IN ('N02','N03')")
    con.commit()
    xy = {nid: gpkg_point_xy(g) for nid, g in con.execute(
        "SELECT node_id, geom FROM wwn_nodes WHERE node_id IN ('N02','N03')")}
    con.close()
    return xy


def _icm_nodes(a, tmp_path):
    return {r["node_id"]: r for r in
            a.memory[os.path.join(str(tmp_path / "demo_ICM.gdb"), "ICM_Nodes")]["rows"]}


def test_dem_fill_ground_levels(fake_arcpy, demo_gpkg, tmp_path):
    xy = _null_two(demo_gpkg)
    a = fake_arcpy([])

    def dem(x, y):
        px, py = xy["N02"]
        return "30,5" if abs(x - px) < 1e-6 and abs(y - py) < 1e-6 else "NoData"
    a.dem_fn = dem
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), dem="C:/dem.tif")
    rows = _icm_nodes(a, tmp_path)
    assert rows["N02"]["ground_level"] == 30.5 and rows["N02"]["wwn_ground_source"] == "DEM"
    assert rows["N03"]["ground_level"] is None and rows["N03"]["wwn_ground_source"] == ""
    assert rows["N01"]["wwn_ground_source"] == "model"
    assert len(a.array_calls) == 2 and a.cell_calls == []      # fast path, no GP tool calls
    warns = " ".join(t for k, t in a.log if k == "warn")
    assert "1 node(s) had no surface level" in warns and "1 still blank: N03" in warns
    assert "have no surface level" not in " ".join(r["warnings"])


def test_dem_low_ground_warning(fake_arcpy, demo_gpkg, tmp_path):
    _null_two(demo_gpkg)
    a = fake_arcpy([])
    a.dem_fn = lambda x, y: "1.0"
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), dem="C:/dem.tif")
    warns = [t for k, t in a.log if k == "warn"]
    assert any("less than 0.3 m above the chamber floor at 2 node(s)" in t and "N02" in t for t in warns)


def test_export_icm_timing_line(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    line = next(t for k, t in a.log if t.startswith("Timing:"))
    for k in ("read model", "DEM", "nodes", "conduits", "CSV/notes", "total"):
        assert k + " " in line


def test_dem_fill_falls_back_to_getcellvalue(fake_arcpy, demo_gpkg, tmp_path):
    _null_two(demo_gpkg)
    a = fake_arcpy([])
    a.dem_numpy = False
    a.dem_fn = lambda x, y: "31.25"
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), dem="C:/dem.tif")
    rows = _icm_nodes(a, tmp_path)
    assert rows["N02"]["ground_level"] == rows["N03"]["ground_level"] == 31.25
    assert len(a.cell_calls) == 2


def test_export_icm_timing_lines_and_log_settings_restored(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path / "icm"))
    nodes = [t for k, t in a.log if t.startswith("Timing ICM_Nodes:")]
    conduits = [t for k, t in a.log if t.startswith("Timing ICM_Conduits:")]
    assert len(nodes) == 1 and len(conduits) == 1
    for k in ("create gdb", "exists", "delete", "create", "fields", "copy"):
        assert k in nodes[0]
    assert nodes[0].startswith("Timing ICM_Nodes: create gdb ")
    for k in ("exists", "delete", "create", "fields", "copy"):
        assert k in conduits[0]
    assert "create gdb" not in conduits[0]
    assert a.log_settings[:2] and set(a.log_settings[:2]) == {("history", False), ("metadata", False)}
    assert a.log_history is True and a.log_metadata is True


def _edit(demo_gpkg, *sql):
    con = sqlite3.connect(demo_gpkg)
    for s in sql:
        con.execute(s)
    con.commit()
    con.close()


def test_demo_outfall_type_is_not_a_terminal_conversion(demo_gpkg):
    d = icm_export.build(demo_gpkg)
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["OUT1"]["node_type"] == "Outfall" and nodes["OUT1"]["wwn_outfall_reason"] == ""
    assert all(n["wwn_outfall_reason"] == "" for n in d["nodes"])
    assert not any("terminal node" in w or "wet well" in w for w in d["warnings"])


def test_terminal_ac_node_exported_as_outfall(demo_gpkg):
    _edit(demo_gpkg, "UPDATE wwn_nodes SET node_type='ac' WHERE node_id='OUT1'")
    d = icm_export.build(demo_gpkg)
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["OUT1"]["node_type"] == "Outfall" and nodes["OUT1"]["wwn_node_type"] == "ac"
    assert nodes["OUT1"]["wwn_outfall_reason"] == "terminal"
    assert nodes["N08"]["node_type"] == "Manhole" and nodes["N08"]["wwn_outfall_reason"] == ""
    assert "1 terminal node(s) exported as Outfall: OUT1" in d["warnings"]
    assert not any("sewer end" in w for w in d["warnings"])


def test_dangling_junction_not_made_outfall(demo_gpkg, fake_arcpy, tmp_path):
    # a junction fed from B1 (pipe in, nothing out): a sewer that should have joined another pipe
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, surface_level, invert_level) "
          "SELECT geom, 'J1', 'junction', 27.5, 26.5 FROM wwn_nodes WHERE node_id='N08'",
          "INSERT INTO wwn_conduits (geom, conduit_id, us_node_id, ds_node_id, dn, us_invert, ds_invert, "
          "length_m, ks_mm, internal_diameter_mm) SELECT geom, 'P98', 'B1', 'J1', 150, 27.0, 26.5, 50.0, 1.0, "
          "150.0 FROM wwn_conduits WHERE conduit_id='P09'")
    d = icm_export.build(demo_gpkg)
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["J1"]["node_type"] == "Break" and nodes["J1"]["wwn_node_type"] == "junction"
    assert nodes["J1"]["wwn_outfall_reason"] == "dangling end"
    assert not any("terminal node" in w for w in d["warnings"])
    expected = ("1 sewer end(s) have no downstream pipe and are not chambers (possible missed tee / main outside "
                "the selection). NOT made outfalls (their flow would leave the network): ICM will report them as "
                "not connected to an outfall until the join is fixed: J1")
    assert expected in d["warnings"]

    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    row = _icm_nodes(a, tmp_path)["J1"]
    assert row["node_type"] == "Break" and row["wwn_outfall_reason"] == "dangling end"
    assert [f for f, _ in icm_export.NODE_FIELDS].index("wwn_outfall_reason") == \
        [f for f, _ in icm_export.NODE_FIELDS].index("wwn_ground_source") + 1


@pytest.mark.parametrize("how", ["wet_well_type", "pump_station"])
def test_terminal_wet_well_stays_manhole(demo_gpkg, how):
    if how == "wet_well_type":
        _edit(demo_gpkg, "UPDATE wwn_nodes SET node_type='wet_well' WHERE node_id='OUT1'")
    else:       # typed as 'ac' but it is the wet well of a pump station
        _edit(demo_gpkg, "UPDATE wwn_nodes SET node_type='ac' WHERE node_id='OUT1'",
              "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id) "
              "VALUES ('PS9', 'OUT1', 'N08')")
    d = icm_export.build(demo_gpkg)
    out1 = next(n for n in d["nodes"] if n["node_id"] == "OUT1")
    assert out1["node_type"] == "Manhole" and out1["wwn_outfall_reason"] == ""
    assert any(w.startswith("OUT1: wet well exported as Manhole with no pump out of it") for w in d["warnings"])
    assert not any("terminal node" in w for w in d["warnings"])
    if how == "pump_station":
        assert "Pump station PS9: no rising main exported, so its pumps are NOT exported." in d["warnings"]
    assert d["pumps"] == [] and not any(c["wwn_link_kind"] == "rising_main" for c in d["conduits"])


# ------------------------------------------------------------------ pressure side (rising mains, pumps)

def _pump_model(demo_gpkg, curve=True, rm_sql=None):
    """The Self Test pump scenario: P08 ends in wet well WW; PS1 pumps through rising main RM1 to D, and a
    gravity sewer D -> OUT1 (WW and D share OUT1's position)."""
    from wwnet_arcgis import pumpdata
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'WW', 'wet_well', 22.2, 25.5 FROM wwn_nodes WHERE node_id='OUT1'",
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'D', 'ac', 25.0, 28.0 FROM wwn_nodes WHERE node_id='OUT1'",
          "UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'",
          "INSERT INTO wwn_conduits (conduit_id, us_node_id, ds_node_id, dn, length_m, us_invert, ds_invert) "
          "VALUES ('PSG1','D','OUT1',225,50.0,25.0,22.2)",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id) "
          "VALUES ('PS1','WW','D','RM1')",
          rm_sql or "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, roughness_type, "
                    "roughness, material) VALUES ('RM1','PS1','D',150,200.0,'ks',1.0,'pvc')")
    pumpdata.set_station(demo_gpkg, "PS1", wet_well_area_m2=1.0, stop=22.6, start=23.0, overflow=25.0, n_duty=1,
                         n_assist=1, n_standby=1, duty_rate_lps=None if curve else 12.5,
                         curve=[(0, 20), (10, 16.5), (20, 11.5), (30, 5)] if curve else None)


def test_rising_main_exported_as_force_main_conduit(demo_gpkg):
    _pump_model(demo_gpkg)
    d = icm_export.build(demo_gpkg)
    rm = next(c for c in d["conduits"] if c["wwn_conduit_id"] == "RM1")
    assert (rm["us_node_id"], rm["link_suffix"], rm["ds_node_id"]) == ("PS1_DEL", "1", "D")
    assert rm["solution_model"] == "ForceMain" and rm["wwn_link_kind"] == "rising_main"
    assert rm["us_headloss_type"] == rm["ds_headloss_type"] == "None"     # ICM E2075 with ForceMain
    assert all(c["us_headloss_type"] == c["ds_headloss_type"] == "Normal"
               for c in d["conduits"] if c["wwn_link_kind"] == "gravity")
    assert (rm["us_invert"], rm["ds_invert"]) == (22.2, 25.0)          # wet well floor -> discharge floor
    assert rm["conduit_width"] == rm["conduit_height"] == 150 and rm["conduit_length"] == 200.0
    assert rm["roughness_type"] == "CW" and rm["bottom_roughness_CW"] == rm["top_roughness_CW"] == 1.0
    assert rm["material"] == "PVC"
    assert all(c["solution_model"] == "Full" for c in d["conduits"] if c["wwn_link_kind"] == "gravity")
    nodes = {n["node_id"]: n for n in d["nodes"]}
    dn = nodes["PS1_DEL"]
    assert dn["node_type"] == "Break" and dn["flood_type"] == "Sealed" and dn["wwn_node_type"] == "pump_delivery"
    assert dn["chamber_floor"] == 22.2 and dn["ground_level"] == 25.5
    assert nodes["WW"]["node_type"] == "Manhole" and nodes["WW"]["chamber_area"] == nodes["WW"]["shaft_area"] == 1.0
    assert nodes["D"]["node_type"] == "Manhole" and nodes["D"]["wwn_outfall_reason"] == ""
    assert "wwn_rising_mains" not in d["skipped"] and "wwn_pump_stations" not in d["skipped"]
    assert not any("wet well exported as Manhole" in w or "NOT exported" in w and "Rising" in w
                   for w in d["warnings"])
    assert d["counts"]["rising_mains"] == 1 and d["counts"]["delivery_nodes"] == 1
    keys = [(c["us_node_id"], c["link_suffix"]) for c in d["conduits"] + d["pumps"]]
    assert len(keys) == len(set(keys))                                  # ICM link ids unique across tables


def test_pumps_exported_with_head_discharge_tables(demo_gpkg):
    _pump_model(demo_gpkg)
    d = icm_export.build(demo_gpkg)
    pumps = {p["wwn_pump_id"]: p for p in d["pumps"]}
    assert set(pumps) == {"PS1_P1", "PS1_P2"}                           # duty + assist; standby P3 skipped
    duty, assist = pumps["PS1_P1"], pumps["PS1_P2"]
    assert (duty["us_node_id"], duty["ds_node_id"]) == ("WW", "PS1_DEL")
    assert {duty["link_suffix"], assist["link_suffix"]} == {"1", "2"}
    assert duty["link_type"] == "Rotpmp" and duty["head_discharge_id"] == "HD_PS1_P1" and duty["discharge"] is None
    assert (duty["switch_on_level"], duty["switch_off_level"]) == (23.0, 22.6)
    assert (assist["switch_on_level"], assist["switch_off_level"]) == (23.2, 22.7)
    hd = [(r["head"], r["discharge"]) for r in d["head_discharge"] if r["head_discharge_id"] == "HD_PS1_P1"]
    assert hd == [(20.0, 0.0), (16.5, 10.0), (11.5, 20.0), (5.0, 30.0)]  # decreasing head, closed valve first
    assert any("standby pump(s) not exported" in w and "PS1_P3" in w for w in d["warnings"])
    assert not any(n["node_id"] == "WW" and n["node_type"] == "Outfall" for n in d["nodes"])


def test_fixed_rate_pump_and_hazen_williams_main(demo_gpkg):
    _pump_model(demo_gpkg, curve=False,
                rm_sql="INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, internal_diameter_mm, "
                       "length_m, roughness_type, roughness) VALUES ('RM1','PS1','D',150,141.0,NULL,'hw_c',120)")
    d = icm_export.build(demo_gpkg)
    rm = next(c for c in d["conduits"] if c["wwn_conduit_id"] == "RM1")
    assert rm["roughness_type"] == "HW" and rm["bottom_roughness_HW"] == rm["top_roughness_HW"] == 120
    assert rm["bottom_roughness_CW"] is None and rm["conduit_width"] == 141.0
    assert rm["conduit_length"] is None and any("RM1: no length" in w for w in d["warnings"])
    duty = next(p for p in d["pumps"] if p["wwn_role"] == "duty")
    assert duty["link_type"] == "Fixpmp" and duty["discharge"] == 12.5 and duty["head_discharge_id"] is None
    assert d["head_discharge"] == []


def test_rising_main_defaults_and_unconnected_discharge(demo_gpkg):
    # station without hydraulics, a main without roughness discharging to a junction with nothing downstream
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level) "
          "SELECT geom, 'D_RM_PS2', 'junction', 30.0, 31.0 FROM wwn_nodes WHERE node_id='N01'",
          "UPDATE wwn_nodes SET node_type='wet_well' WHERE node_id='OUT1'",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id, pump_rate_lps) "
          "VALUES ('PS2','OUT1','D_RM_PS2','RM_PS2', 8.0)",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m) "
          "VALUES ('RM_PS2','PS2','D_RM_PS2',100,80.0)")
    d = icm_export.build(demo_gpkg)
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["D_RM_PS2"]["node_type"] == "Outfall"
    assert nodes["D_RM_PS2"]["wwn_outfall_reason"] == "rising main discharge"
    assert nodes["OUT1"]["chamber_area"] == icm_export.DEFAULT_WET_WELL_AREA_M2
    rm = next(c for c in d["conduits"] if c["wwn_conduit_id"] == "RM_PS2")
    assert rm["bottom_roughness_CW"] == icm_export.DEFAULT_RM_KS_MM
    (p,) = d["pumps"]
    assert p["wwn_pump_id"] == "PS2_P1" and p["link_type"] == "Fixpmp" and p["discharge"] == 8.0
    floor = nodes["OUT1"]["chamber_floor"]
    assert p["switch_off_level"] == pytest.approx(floor + 0.3) and p["switch_on_level"] == pytest.approx(floor + 1.0)
    text = " ".join(d["warnings"])
    for frag in ("no roughness", "no wet well area", "no stop level", "no start level", "no pumps listed",
                 "rising main discharge node(s) have no downstream pipe"):
        assert frag in text, frag
    assert "dangling" not in text


def test_rising_main_with_unknown_discharge_not_exported(demo_gpkg):
    _edit(demo_gpkg,
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, rising_main_id, pump_rate_lps) "
          "VALUES ('PS3','OUT1','RM3', 5.0)",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m) "
          "VALUES ('RM3','PS3','NOPE',100,50.0)")
    d = icm_export.build(demo_gpkg)
    assert d["skipped"]["wwn_rising_mains"] == 1 and d["pumps"] == []
    assert any(w.startswith("Rising main RM3: discharge node NOPE is not in the model; NOT exported")
               for w in d["warnings"])
    assert not any(n["node_id"].startswith("PS3") for n in d["nodes"])


def test_export_icm_writes_pumps_and_rising_mains(fake_arcpy, demo_gpkg, tmp_path):
    from fake_arcpy import gpkg_point_xy
    _pump_model(demo_gpkg)
    con = sqlite3.connect(demo_gpkg)
    ww = gpkg_point_xy(con.execute("SELECT geom FROM wwn_nodes WHERE node_id='WW'").fetchone()[0])
    con.close()
    a = fake_arcpy([])
    out = tmp_path / "icm"
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    gdb = str(out / "demo_ICM.gdb")
    nodes = {x["node_id"]: x for x in a.memory[os.path.join(gdb, "ICM_Nodes")]["rows"]}
    links = {x["wwn_conduit_id"]: x for x in a.memory[os.path.join(gdb, "ICM_Conduits")]["rows"]}
    pumps_fc = a.memory[os.path.join(gdb, "ICM_Pumps")]
    assert pumps_fc["geom"] == "POLYLINE" and [f for f, _ in pumps_fc["fields"]] == \
        [f for f, _ in icm_export.PUMP_FIELDS]
    assert len(pumps_fc["rows"]) == 2 and all(x["SHAPE@"] is not None for x in pumps_fc["rows"])
    # no PS / main geometry here: the delivery node sits 1 m east of the wet well, the main runs from it to D
    pt = nodes["PS1_DEL"]["SHAPE@"].firstPoint
    assert (pt.X, pt.Y) == pytest.approx((ww[0] + icm_export.DELIVERY_OFFSET_M, ww[1]))
    line = links["RM1"]["SHAPE@"]
    assert (line.firstPoint.X, line.lastPoint.X) == pytest.approx((ww[0] + 1.0, ww[0]))
    assert links["RM1"]["solution_model"] == "ForceMain"
    with open(r["pumps_csv"], newline="") as fh:
        assert {x["link_type"] for x in csv.DictReader(fh)} == {"Rotpmp"}
    with open(r["head_discharge_csv"], newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 8 and rows[0] == {"head_discharge_id": "HD_PS1_P1", "head": "20.0", "discharge": "0.0"}
    with open(r["nodes_csv"], newline="") as fh:
        dn = next(x for x in csv.DictReader(fh) if x["node_id"] == "PS1_DEL")
    assert dn["x"] and dn["flood_type"] == "Sealed"
    notes = open(r["notes"], encoding="utf-8").read()
    for frag in ("then pumps", "ForceMain", "MAP THIS FIELD", "Rotpmp", "HD_PS1_P1", "ICM_Pumps"):
        assert frag in notes, frag
    assert "Not exported: pump stations" not in notes
    info = " ".join(t for k, t in a.log if k == "info")
    assert "1 rising main(s) and 2 pump(s)" in info and "Timing ICM_Pumps:" in info
    assert a.addfields_calls == 3

    # re-export after the station is removed: stale pump table and CSVs go
    _edit(demo_gpkg, "DELETE FROM wwn_pumps", "DELETE FROM wwn_pump_stations", "DELETE FROM wwn_rising_mains")
    r2 = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    assert os.path.join(gdb, "ICM_Pumps") not in a.memory
    assert r2["pumps_csv"] is None and not os.path.exists(r["pumps_csv"])
    assert not os.path.exists(r["head_discharge_csv"])


def test_delivery_node_geometry_prefers_rising_main_start(fake_arcpy):
    from fake_arcpy import FakePolyline
    a = fake_arcpy([])
    rm = FakePolyline([a.Point(10.0, 20.0), a.Point(50.0, 20.0)])
    fn = icm_export.node_geometry_fn(a, None, {"rm": {"RM1": rm}, "ps": {"PS1": (11.0, 21.0)}})
    row = {"_geom": [("rm_start", "RM1"), ("ps", "PS1"), ("node", "WW")], "_offset_from": "WW"}
    g = fn(row, {"WW": (0.0, 0.0)})
    assert (g.firstPoint.X, g.firstPoint.Y) == (10.0, 20.0)
    g = icm_export.node_geometry_fn(a, None, {"rm": {}, "ps": {"PS1": (11.0, 21.0)}})(row, {"WW": (0.0, 0.0)})
    assert (g.firstPoint.X, g.firstPoint.Y) == (11.0, 21.0)
    g = icm_export.node_geometry_fn(a, None, {"rm": {}, "ps": {}})(row, {"WW": (5.0, 5.0)})
    assert (g.firstPoint.X, g.firstPoint.Y) == (6.0, 5.0)                 # moved off the wet well
    assert icm_export.line_geometry(a, [(1.0, 1.0), (1.0, 1.0)], None) is None


def test_dem_fill_skips_delivery_nodes_and_copies_wet_well_ground(fake_arcpy, demo_gpkg, tmp_path):
    _pump_model(demo_gpkg)
    _edit(demo_gpkg, "UPDATE wwn_nodes SET surface_level=NULL WHERE node_id='WW'")
    a = fake_arcpy([])
    a.dem_fn = lambda x, y: "26.0"
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), dem="C:/dem.tif")
    rows = _icm_nodes(a, tmp_path)
    assert rows["WW"]["ground_level"] == 26.0 and rows["WW"]["wwn_ground_source"] == "DEM"
    assert rows["PS1_DEL"]["ground_level"] == 26.0 and rows["PS1_DEL"]["wwn_ground_source"] == "wet well"
    assert not any("outside the DEM" in w for w in r["warnings"])



def test_null_pump_station_geometry_is_not_a_location(fake_arcpy, demo_gpkg, tmp_path):
    # Pro regression (Self Test 2026-11-24): SHAPE@XY of a station without geometry is (None, None)
    _pump_model(demo_gpkg)
    a = fake_arcpy([])
    with a.da.SearchCursor(demo_gpkg + "\\wwn_pump_stations", ["ps_id", "SHAPE@XY"]) as cur:
        assert list(cur) == [["PS1", (None, None)]]                       # the fake behaves like Pro
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    assert r["counts"]["pumps"] == 2 and r["counts"]["rising_mains"] == 1


def test_rising_main_geometry_copied_and_delivery_node_at_its_start(fake_arcpy, demo_gpkg, tmp_path):
    from pressure_line import line_blob_between
    _pump_model(demo_gpkg)
    con = sqlite3.connect(demo_gpkg)
    blob = line_blob_between(con, "N08", "D", mid=(5.0, 7.0))
    con.execute("UPDATE wwn_rising_mains SET geom=? WHERE rm_id='RM1'", (blob,))
    con.commit()
    con.close()
    a = fake_arcpy([])
    tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    gdb = str(tmp_path / "demo_ICM.gdb")
    nodes = {x["node_id"]: x for x in a.memory[os.path.join(gdb, "ICM_Nodes")]["rows"]}
    line = next(x for x in a.memory[os.path.join(gdb, "ICM_Conduits")]["rows"] if x["wwn_conduit_id"] == "RM1")
    assert len(line["SHAPE@"].points) == 3                                  # the main's own vertices
    first = line["SHAPE@"].firstPoint
    pt = nodes["PS1_DEL"]["SHAPE@"].firstPoint
    assert (pt.X, pt.Y) == (first.X, first.Y)


def test_node_without_geometry_has_blank_csv_xy(fake_arcpy, demo_gpkg, tmp_path):
    _edit(demo_gpkg, "UPDATE wwn_nodes SET geom=NULL WHERE node_id='N05'")
    a = fake_arcpy([])
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    with open(r["nodes_csv"], newline="") as fh:
        n5 = next(x for x in csv.DictReader(fh) if x["node_id"] == "N05")
    assert n5["x"] == n5["y"] == ""


def test_export_icm_long_notes_fit_the_gdb_field(fake_arcpy, demo_gpkg, tmp_path):
    """A merged run lists its segments in notes (user's EdenHill model: 'Field length exceeded. Field: notes');
    text longer than the gdb field is cut to fit, the CSV keeps all of it."""
    long_notes = "BEECHBORO RD/CHEDWORTH WY DIVERSION; segments: " + ", ".join(str(51829000 + i) for i in range(400))
    con = sqlite3.connect(demo_gpkg)
    con.execute("UPDATE wwn_conduits SET notes=? WHERE conduit_id='P01'", (long_notes,))
    con.execute("UPDATE wwn_nodes SET notes=? WHERE node_id='N01'", (long_notes,))
    con.commit()
    con.close()
    assert len(long_notes) > icm_export.NOTES_LENGTH
    a = fake_arcpy([])
    out = tmp_path / "icm"
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    gdb = str(out / "demo_ICM.gdb")
    for table, key, val in (("ICM_Conduits", "wwn_conduit_id", "P01"), ("ICM_Nodes", "node_id", "N01")):
        fc = a.memory[os.path.join(gdb, table)]
        assert fc["lengths"]["notes"] == icm_export.NOTES_LENGTH
        row = next(x for x in fc["rows"] if x[key] == val)
        assert len(row["notes"]) == icm_export.NOTES_LENGTH and row["notes"].endswith(" ...")
    with open(r["conduits_csv"], newline="") as fh:
        assert next(x for x in csv.DictReader(fh) if x["wwn_conduit_id"] == "P01")["notes"] == long_notes


def test_fake_arcpy_rejects_text_longer_than_the_field(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    a.management.CreateFileGDB(str(tmp_path), "t.gdb")
    gdb = os.path.join(str(tmp_path), "t.gdb")
    a.management.CreateFeatureclass(gdb, "T", "POINT")
    fc = os.path.join(gdb, "T")
    a.management.AddFields(fc, [["notes", "TEXT", "", 10]])
    with pytest.raises(RuntimeError, match="Field length exceeded"):
        with a.da.InsertCursor(fc, ["notes", "SHAPE@"]) as ins:
            ins.insertRow(["x" * 11, None])


def test_hanging_rising_main_discharge_outfall_reason(demo_gpkg):
    _edit(demo_gpkg,
          "INSERT INTO wwn_nodes (geom, node_id, node_type, invert_level, surface_level, notes) "
          "SELECT geom, 'D_RM_PS2', 'junction', 30.0, 31.0, "
          "'Created by Import Pressure Data; rising main discharge, not connected: connect manually' "
          "FROM wwn_nodes WHERE node_id='N01'",
          "UPDATE wwn_nodes SET node_type='wet_well' WHERE node_id='OUT1'",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id, pump_rate_lps) "
          "VALUES ('PS2','OUT1','D_RM_PS2','RM_PS2', 8.0)",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m) "
          "VALUES ('RM_PS2','PS2','D_RM_PS2',100,80.0)")
    d = icm_export.build(demo_gpkg)
    n = next(x for x in d["nodes"] if x["node_id"] == "D_RM_PS2")
    assert n["node_type"] == "Outfall"
    assert n["wwn_outfall_reason"] == "rising main discharge, not connected (connect manually)"
    assert any("never connected to the gravity network" in w and "D_RM_PS2" in w for w in d["warnings"])


def test_short_conduits_exported_as_one_metre(demo_gpkg):
    _pump_model(demo_gpkg, rm_sql="INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, "
                                  "roughness_type, roughness) VALUES ('RM1','PS1','D',150,0.8,'ks',1.0)")
    _edit(demo_gpkg, "UPDATE wwn_conduits SET length_m=0.4 WHERE conduit_id='P01'",
          "UPDATE wwn_conduits SET length_m=1.0 WHERE conduit_id='P02'",
          "UPDATE wwn_conduits SET length_m=25.0 WHERE conduit_id='P03'")
    d = icm_export.build(demo_gpkg)
    by = {c["wwn_conduit_id"]: c for c in d["conduits"]}
    assert by["P01"]["conduit_length"] == 1.0 and by["RM1"]["conduit_length"] == 1.0
    assert by["P02"]["conduit_length"] == 1.0 and by["P03"]["conduit_length"] == 25.0
    (w,) = [w for w in d["warnings"] if "shorter than 1 m" in w]
    assert w.startswith("2 conduit(s) shorter than 1 m exported with length 1 m (ICM minimum): ")
    assert "P01" in w and "RM1" in w and "P02" not in w
    assert "shorter than 1 m" in icm_export.notes_text("g", "sr", "gdb", "n", "c", d["warnings"])
