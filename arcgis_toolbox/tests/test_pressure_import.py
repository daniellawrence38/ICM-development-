import sqlite3
import struct
from types import SimpleNamespace as NS

import pytest

import fake_arcpy as fa
from wwnet_arcgis import pressure_import as pi, tools_impl
from wwnet_arcgis.messages import Messages

GEOM = "_geom"
GDA2020 = NS(factoryCode=7850, name="GDA2020 / MGA zone 50")
PS_FIELDS = [("OBJECTID", "OID"), ("SAP_ID", "String"), ("ID", "Double"), ("SAP_NAME", "String"),
             ("STATUS", "String"), ("PSNAME", "String"), ("Shape", "Geometry")]
MAIN_FIELDS = [("OBJECTID", "OID"), ("ID", "Double"), ("STATUS", "String"), ("DIAMETER", "Double"),
               ("MATERIAL", "String"), ("INDIAM1", "Double"), ("UPSTREAM", "Double"), ("DOWNSTREAM", "Double"),
               ("LENGTH", "Double"), ("MAINNAME", "String"), ("Shape", "Geometry")]

OUT1 = (392560.0, 6466000.0)
B1 = (392140.0, 6466240.0)
P1, P2, P3 = (392562.0, 6466005.0), (392562.0, 6466100.0), (392300.0, 6466240.0)
END = (392141.0, 6466240.0)


def seg(i, coords, **kw):
    r = {"ID": float(i), "STATUS": "A- actual", "DIAMETER": 300.0, "MATERIAL": "AC- asbestos cement",
         "INDIAM1": -9996.0, "UPSTREAM": -99.89999, "DOWNSTREAM": -99.89999, "LENGTH": 100.0, "MAINNAME": "SPS NO2",
         GEOM: coords}
    r.update(kw)
    return r


def ps(sap, xy, **kw):
    r = {"SAP_ID": sap, "ID": 1.0, "SAP_NAME": f"SPS {sap}", "STATUS": "A- actual", "PSNAME": "", GEOM: xy}
    r.update(kw)
    return r


@pytest.fixture
def world(fake_arcpy, demo_gpkg):
    layers = []
    a = fake_arcpy(layers)
    a.sources["C:/d/ps"] = {"fields": PS_FIELDS, "sr": GDA2020, "rows": [
        ps("S0037310", (OUT1[0] + 0.3, OUT1[1] + 0.4), SAP_NAME="SPS 2 ROBINSON ST CARNARVON", PSNAME="ROBINSON"),
        ps("S0099999", (392300.0, 6466100.0), STATUS="R- removed"),
        ps("", (392280.0, 6466060.0), ID=4567.0),
        ps("S0000001", (500000.0, 6400000.0))]}                       # far outside the model: not read
    a.sources["C:/d/main"] = {"fields": MAIN_FIELDS, "sr": GDA2020, "rows": [
        seg(1, [P1, P2], INDIAM1=-9996.0, LENGTH=95.0),
        seg(2, [P3, P2], INDIAM1=290.0, LENGTH=297.07),                 # digitised against the chain
        seg(3, [P3, END], DIAMETER=-9996.0, LENGTH=0.0),
        seg(4, [(392000.0, 6466500.0), (392010.0, 6466500.0)], STATUS="R- removed"),
        seg(5, [(392100.0, 6466600.0), (392110.0, 6466600.0)])]}      # unrelated segment: ignored
    lyr = {k: fa.make_layer(k, f"C:/d/{k}") for k in ("ps", "main")}
    return a, lyr, demo_gpkg, layers


def run(a, lyr, gp, **kw):
    kw.setdefault("add_to_map", False)
    return tools_impl.import_pressure_data(a, Messages(a), gp, lyr["ps"], lyr["main"], **kw)


def q(gp, sql, *args):
    c = sqlite3.connect(gp)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def line_pts(blob):
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 7]
    wkb = blob[8 + env:]
    n = struct.unpack("<I", wkb[5:9])[0]
    return [struct.unpack("<dd", wkb[9 + 16 * i:25 + 16 * i]) for i in range(n)]


def warns(a):
    return [t for lv, t in a.log if lv == "warn"]


def test_end_to_end(world):
    a, lyr, gp, _ = world
    res = run(a, lyr, gp)
    assert q(gp, "select ps_id, wet_well_node_id, discharge_node_id, rising_main_id, status, data_source, notes, "
                 "pump_rate_lps, start_level from wwn_pump_stations order by ps_id") == [
        ("4567", "WW_4567", None, None, "existing", "gis", "SPS", None, None),
        ("S0037310", "OUT1", "B1", "RM_S0037310", "existing", "gis",
         "SPS 2 ROBINSON ST CARNARVON | ROBINSON", None, None)]
    rm = q(gp, "select rm_id, ps_id, discharge_node_id, dn, internal_diameter_mm, material, roughness_type, "
               "roughness, length_m, profile_json, status, data_source, notes, geom from wwn_rising_mains")
    assert len(rm) == 1
    r = rm[0]
    assert r[:8] == ("RM_S0037310", "S0037310", "B1", 300, 290.0, "ac_pipe", "ks", None)
    assert r[10:12] == ("existing", "gis")
    assert r[8] == pytest.approx(95.0 + 297.07 + 159.0, abs=0.01) and r[9] is None
    assert r[12].startswith("SPS NO2; segments: 1, 2, 3")
    pts = line_pts(r[13])
    assert pts == [P1, P2, P3, END]                                  # oriented from the pump station end
    # wet well: gravity terminal OUT1 retyped; a node is created for the PS with no gravity node near
    assert q(gp, "select node_type from wwn_nodes where node_id='OUT1'") == [("wet_well",)]
    assert q(gp, "select node_type, status, data_source, surface_level from wwn_nodes where node_id='WW_4567'") == [
        ("wet_well", "existing", "gis", None)]
    assert q(gp, "select count(*) from wwn_nodes") == [(13,)]
    assert q(gp, "select count(*) from wwn_pumps") == [(0,)]
    ws = warns(a)
    assert any("Pump station 4567" in w and "no pressure main" in w for w in ws)
    assert any("Pump station 4567" in w and "WW_4567" in w for w in ws)
    assert any(w.startswith("Enter wet_well_area_m2, start_level, stop_level, pump_rate_lps") and "4567" in w
               and "S0037310" in w for w in ws)
    assert not any("S0099999" in w for w in ws)                       # removed station ignored
    assert len(res["result"].pump_stations) == 2
    infos = [t for lv, t in a.log if lv == "info"]
    assert any("Imported 2 pump station(s) and 1 rising main(s)" in t for t in infos)
    assert any("skipped by status: R x1" in t for t in infos)
    # spatial filters, never a full scan
    assert not [c for c in a.cursor_log if c["full_scan"]]
    assert all(sr.factoryCode == 7850 for _, sr in a.cursor_calls)


def test_discharge_node_created_when_no_gravity_node_near(world):
    a, lyr, gp, _ = world
    run(a, lyr, gp, discharge_distance=0.5)
    assert q(gp, "select discharge_node_id from wwn_rising_mains") == [("D_RM_S0037310",)]
    assert q(gp, "select node_type, status, data_source from wwn_nodes where node_id='D_RM_S0037310'") == [
        ("junction", "existing", "gis")]
    assert any("discharge end is not at a gravity node" in w for w in warns(a))


def test_idempotent_rerun_keeps_user_data(world):
    a, lyr, gp, _ = world
    run(a, lyr, gp, discharge_distance=0.5)
    c = sqlite3.connect(gp)
    c.execute("update wwn_pump_stations set wet_well_area_m2=12, start_level=1, stop_level=0.5, pump_rate_lps=20 "
              "where ps_id='S0037310'")
    c.execute("insert into wwn_pumps (pump_id, ps_id, curve_json) values ('S0037310_1','S0037310','[[0,30]]')")
    c.commit()
    c.close()
    snap = q(gp, "select count(*) from wwn_nodes"), q(gp, "select count(*) from wwn_rising_mains"), \
        q(gp, "select count(*) from wwn_pump_stations")
    a.log.clear()
    run(a, lyr, gp, discharge_distance=0.5)
    assert snap == (q(gp, "select count(*) from wwn_nodes"), q(gp, "select count(*) from wwn_rising_mains"),
                    q(gp, "select count(*) from wwn_pump_stations"))
    assert q(gp, "select wet_well_area_m2, start_level, stop_level, pump_rate_lps from wwn_pump_stations "
                 "where ps_id='S0037310'") == [(12.0, 1.0, 0.5, 20.0)]
    assert q(gp, "select count(*) from wwn_pumps") == [(1,)]
    assert q(gp, "select count(*) from wwn_nodes where node_id in ('D_RM_S0037310','WW_4567')") == [(2,)]
    # S0037310 is complete now; only 4567 is still listed
    msg = [w for w in warns(a) if w.startswith("Enter wet_well_area_m2")]
    assert msg and msg[0].endswith("for: 4567")
    assert any("Replaced earlier import: 2 pump station(s), 1 rising main(s), 2 node(s)" in t
               for lv, t in a.log if lv == "info")


def test_selection_honoured(world):
    a, lyr, gp, _ = world
    lyr["ps"] = fa.make_layer("ps", "C:/d/ps", selection=[3])          # only the station without a main
    run(a, lyr, gp)
    assert q(gp, "select ps_id from wwn_pump_stations") == [("4567",)]
    assert q(gp, "select count(*) from wwn_rising_mains") == [(0,)]
    a2 = a
    a2.cursor_log.clear()
    lyr["ps"] = fa.make_layer("ps", "C:/d/ps", selection=[3])
    run(a2, lyr, gp, use_selected=False)                              # selection ignored: all in the model area
    assert {r[0] for r in q(gp, "select ps_id from wwn_pump_stations")} == {"4567", "S0037310"}


def test_main_selection_limits_chain(world):
    a, lyr, gp, _ = world
    lyr["main"] = fa.make_layer("main", "C:/d/main", selection=[1, 2])       # segment 3 not selected
    run(a, lyr, gp)
    rm = q(gp, "select length_m, discharge_node_id from wwn_rising_mains")
    assert rm[0][0] == pytest.approx(95.0 + 297.07, abs=0.01)
    assert rm[0][1] == "D_RM_S0037310"        # far end P3 is not near a gravity node


def test_include_not_in_use(world):
    a, lyr, gp, _ = world
    a.sources["C:/d/ps"]["rows"][1]["STATUS"] = "N- not in use"
    run(a, lyr, gp)
    assert q(gp, "select count(*) from wwn_pump_stations where ps_id='S0099999'") == [(0,)]
    run(a, lyr, gp, include_not_in_use=True)
    assert q(gp, "select notes from wwn_pump_stations where ps_id='S0099999'") == [("SPS S0099999 (not in use)",)]


def test_errors(world, tmp_path):
    a, lyr, gp, _ = world
    with pytest.raises(RuntimeError, match="does not exist"):
        run(a, lyr, str(tmp_path / "nope.gpkg"))
    bad = tmp_path / "bad.gpkg"
    sqlite3.connect(bad).execute("create table t(a)").connection.commit()
    with pytest.raises(RuntimeError, match="not a WWNet model"):
        run(a, lyr, str(bad))
    with pytest.raises(RuntimeError, match="field 'SAP_ID'|ID field"):
        run(a, lyr, gp, fields={"ps_id": ""})


def test_add_to_map_adds_missing_layers_and_pumps_table(world):
    a, lyr, gp, layers = world
    run(a, lyr, gp, add_to_map=True)
    names = [p for k, p in a.added if k == "data"]
    assert any(p.endswith("main.wwn_pump_stations") for p in names)
    assert any(p.endswith("main.wwn_rising_mains") for p in names)
    assert any(p.endswith("main.wwn_pumps") for p in names)
    n = len(a.added)
    run(a, lyr, gp, add_to_map=True)               # already in the map: nothing more is added
    assert len(a.added) == n


def test_levels_are_ignored(world):
    a, lyr, gp, _ = world
    for r in a.sources["C:/d/main"]["rows"]:
        r["UPSTREAM"], r["DOWNSTREAM"] = 20.0, 10.0
    run(a, lyr, gp)
    assert q(gp, "select profile_json from wwn_rising_mains") == [(None,)]


# ---------------------------------------------------------------- pure functions

def test_sentinels_and_ids():
    assert pi.clean_number(-99.89999999) is None and pi.clean_number(-9996) is None
    assert pi.clean_number(12.5) == 12.5 and pi.clean_number("") is None
    assert pi.clean_positive(0) is None and pi.clean_positive(-9998.0) is None and pi.clean_positive("150") == 150.0
    assert pi.ps_id_of({"ps_id": "S0037310", "ps_alt_id": 5.0}) == "S0037310"
    assert pi.ps_id_of({"ps_id": " ", "ps_alt_id": 4567.0}) == "4567"
    assert pi.ps_id_of({"ps_id": None, "ps_alt_id": None}) == ""
    assert pi.status_importable("A", False) and not pi.status_importable("N", False)
    assert pi.status_importable("N", True) and not pi.status_importable("R", True)


def _segs(*lines, snap=1.0):
    return [{"idx": i, "id": str(i), "coords": list(c), "dn": 300.0, "indiam": None, "material": None,
             "length": None, "name": "", "geom_len": pi._length(c)} for i, c in enumerate(lines)]


def test_chain_orientation_and_snap():
    segs = _segs([(100, 0), (50, 0)], [(0, 0), (49.5, 0.3)], [(0, 0), (-40, 0)])      # mixed directions
    chains, unused = pi.chain_segments(segs, [("P", (101, 1))], pi.Options(search_m=30, snap_m=1.0))
    assert len(chains) == 1 and unused == []
    c = chains[0]
    assert c["order"] == [(0, False), (1, True), (2, False)] or c["order"][0] == (0, False)
    assert c["coords"][0] == (100.0, 0.0) and c["coords"][-1] == (-40.0, 0.0) and c["end_kind"] == "dead_end"


def test_chain_stops_at_junction_and_warns():
    segs = _segs([(0, 0), (50, 0)], [(50, 0), (100, 0)], [(50, 0), (50, 60)])     # tee at (50, 0)
    chains, unused = pi.chain_segments(segs, [("P", (1, 0))], pi.Options())
    assert len(chains) == 1 and chains[0]["end_kind"] == "junction" and chains[0]["coords"] == [(0.0, 0.0), (50.0, 0.0)]
    assert sorted(unused) == [1, 2]
    res = pi.build_pressure(
        [{"ps_id": "P", "ps_status": "A", GEOM: (1.0, 0.0)}],
        [{"main_id": "a", "main_status": "A", "diameter": 300, GEOM: [(0, 0), (50, 0)]},
         {"main_id": "b", "main_status": "A", "diameter": 300, GEOM: [(50, 0), (100, 0)]},
         {"main_id": "c", "main_status": "A", "diameter": 300, GEOM: [(50, 0), (50, 60)]}],
        [], [], pi.Options(), {})
    assert any("junction of three or more" in w for w in res.warnings)
    assert res.rising_mains[0]["discharge_node_id"] == "D_RM_P"
    assert {n["node_type"] for n in res.new_nodes} == {"junction", "wet_well"}


def test_station_gets_only_its_nearest_end_and_multiple_mains():
    segs = _segs([(0, 0), (100, 0)], [(0, 20), (100, 20)])
    # P1 is nearest the west ends, P2 the east ends: each main is claimed once, from its nearest-station end
    chains, _ = pi.chain_segments(segs, [("P1", (-1, 0)), ("P2", (101, 20))], pi.Options())
    by = {c["ps_id"]: c for c in chains}
    assert by["P1"]["coords"][0] == (0.0, 0.0) and by["P2"]["coords"][0] == (100.0, 20.0)
    # two parallel dead-end mains at one station -> RM_P and RM_P_2
    segs = _segs([(0, 0), (100, 0)], [(0, 3), (100, 3)])
    res = pi.build_pressure([{"ps_id": "P", GEOM: (-1.0, 1.0)}],
                            [{"main_id": str(i), "diameter": 150, GEOM: s["coords"]} for i, s in enumerate(segs)],
                            [], [], pi.Options(), {})
    assert [m["rm_id"] for m in res.rising_mains] == ["RM_P", "RM_P_2"]
    assert res.pump_stations[0]["rising_main_id"] == "RM_P"


def test_diameters_differ_warns_and_uses_most_common():
    res = pi.build_pressure(
        [{"ps_id": "P", GEOM: (0.0, 0.0)}],
        [{"main_id": "1", "diameter": 300, GEOM: [(1, 0), (50, 0)]},
         {"main_id": "2", "diameter": 225, GEOM: [(50, 0), (100, 0)]},
         {"main_id": "3", "diameter": 225, "material": "PVC- pvc", GEOM: [(100, 0), (150, 0)]}],
        [], [], pi.Options(), {"PVC": "pvc"})
    rm = res.rising_mains[0]
    assert rm["dn"] == 225 and rm["material"] == "pvc" and rm["profile_json"] is None
    assert any("diameters differ" in w for w in res.warnings)


def test_blob_roundtrip():
    assert pi.blob_xy(pi.point_blob((1.5, 2.5))) == (1.5, 2.5)
    assert line_pts(pi.line_blob([(0, 0), (3, 4)])) == [(0.0, 0.0), (3.0, 4.0)]


def _hang_build(end_offset, discharge_m=1.0):
    node = {"node_id": "G1", "node_type": "ac", "xy": (100.0, 0.0), "notes": None}
    res = pi.build_pressure(
        [{"ps_id": "P", "ps_status": "A", GEOM: (1.0, 0.0)}],
        [{"main_id": "a", "main_status": "A", "diameter": 300, GEOM: [(0, 0), (100.0 - end_offset, 0)]}],
        [node], [], pi.Options(discharge_m=discharge_m), {})
    return res


def test_discharge_joins_only_when_meeting_node():
    res = _hang_build(0.5)
    assert res.rising_mains[0]["discharge_node_id"] == "G1"
    assert not any(n["node_id"] == "D_RM_P" for n in res.new_nodes)


def test_discharge_far_end_hangs_with_note_and_warning():
    res = _hang_build(5.0)
    assert res.rising_mains[0]["discharge_node_id"] == "D_RM_P"
    n = next(n for n in res.new_nodes if n["node_id"] == "D_RM_P")
    assert n["node_type"] == "junction" and "rising main discharge, not connected: connect manually" in n["notes"]
    assert pi.MARKER in n["notes"]
    (w,) = [w for w in res.warnings if "discharge end is not at a gravity node" in w]
    assert "RM_P" in w and "nearest G1 5.0 m" in w and "connect it manually" in w


def test_discharge_configured_larger_distance_still_joins():
    assert _hang_build(5.0, discharge_m=10.0).rising_mains[0]["discharge_node_id"] == "G1"
    assert pi.Options().discharge_m == 1.0


def _ww_build(terms, wet_well_m=1.0):
    """Station at (0, 0); `terms` = {node_id: (x, 0)} gravity terminal nodes (each fed by one conduit)."""
    nodes = [{"node_id": k, "node_type": "ac", "xy": (x, 0.0), "notes": None} for k, x in terms.items()]
    nodes.append({"node_id": "UP", "node_type": "ac", "xy": (0.0, 50.0), "notes": None})
    ends = [("UP", k) for k in terms]
    return pi.build_pressure([{"ps_id": "P", "ps_status": "A", GEOM: (0.0, 0.0)}], [], nodes, ends,
                             pi.Options(wet_well_m=wet_well_m), {})


def test_wet_well_is_sewer_end_at_station_point():
    res = _ww_build({"T1": 0.3, "T2": 8.0})
    assert res.pump_stations[0]["wet_well_node_id"] == "T1" and res.retype_nodes == {"T1": "wet_well"}
    assert not res.new_nodes


def test_wet_well_picks_one_at_the_point_over_one_5m_away():
    res = _ww_build({"A5": 5.0, "B0": 0.0}, wet_well_m=10.0)
    assert res.pump_stations[0]["wet_well_node_id"] == "B0"


def test_wet_well_far_terminal_not_guessed():
    res = _ww_build({"T8": 8.0})
    assert res.pump_stations[0]["wet_well_node_id"] == "WW_P" and not res.retype_nodes
    n = next(n for n in res.new_nodes if n["node_id"] == "WW_P")
    assert n["node_type"] == "wet_well" and n["xy"] == (0.0, 0.0) and pi.MARKER in n["notes"]
    assert "no gravity sewer ends at the pump station point: connect the inlet sewer manually" in n["notes"]
    (w,) = [w for w in res.warnings if "no gravity sewer ends within" in w]
    assert w == ("Pump station P: no gravity sewer ends within 1 m of the station point (nearest dead-end gravity "
                 "node T8 8.0 m); wet well WW_P created at the station point and left unconnected — connect the "
                 "inlet sewer manually or raise the wet well distance.")


def test_wet_well_configured_larger_distance_joins():
    assert _ww_build({"T8": 8.0}, wet_well_m=10.0).pump_stations[0]["wet_well_node_id"] == "T8"
    assert pi.Options().wet_well_m == 1.0


def test_wet_well_stale_ww_removed_on_reimport(world):
    a, lyr, gp, _ = world
    run(a, lyr, gp, wet_well_distance=0.01)                  # OUT1 is 0.5 m from the station: not used
    assert q(gp, "select wet_well_node_id from wwn_pump_stations where ps_id='S0037310'") == [("WW_S0037310",)]
    run(a, lyr, gp)                                          # default 1 m: the stale WW_ node is cleaned up
    assert q(gp, "select count(*) from wwn_nodes where node_id='WW_S0037310'") == [(0,)]
    assert q(gp, "select wet_well_node_id from wwn_pump_stations where ps_id='S0037310'") == [("OUT1",)]
