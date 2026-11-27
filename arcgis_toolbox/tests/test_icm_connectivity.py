"""Export To ICM: downstream connectivity to an outfall (what ICM reports as 'not connected to an outfall'),
the connectivity report and the gravity-only export."""
import csv
import os
import sqlite3

import pytest

from wwnet_arcgis import icm_export, pumpdata, tools_impl
from wwnet_arcgis.messages import Messages


def _edit(gp, *sql):
    con = sqlite3.connect(gp)
    for s in sql:
        con.execute(s)
    con.commit()
    con.close()


def _node_copy(new_id, like, node_type="ac"):
    return (f"INSERT INTO wwn_nodes (geom, node_id, node_type, surface_level, invert_level) "
            f"SELECT geom, '{new_id}', '{node_type}', surface_level, invert_level FROM wwn_nodes WHERE node_id='{like}'")


def _conduit(cid, us, ds, like="P09"):
    return (f"INSERT INTO wwn_conduits (geom, conduit_id, us_node_id, ds_node_id, dn, us_invert, ds_invert, "
            f"length_m, ks_mm, internal_diameter_mm) SELECT geom, '{cid}', '{us}', '{ds}', 150, 27.0, 26.5, 50.0, "
            f"1.0, 150.0 FROM wwn_conduits WHERE conduit_id='{like}'")


def _by(rows, key):
    return {r[key]: r for r in rows}


def test_demo_all_sewers_reach_the_outfall(demo_gpkg):
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert (cn["sewers"], cn["sewers_ok"]) == (11, 11)
    assert cn["dead_ends"] == [] and cn["loops"] == [] and cn["isolated"] == [] and cn["false_outfalls"] == []
    assert all(c["wwn_outfall"] == "OUT1" and c["wwn_connectivity"] == "ok" for c in d["conduits"])
    assert all(n["wwn_outfall"] == "OUT1" and n["wwn_connectivity"] == "ok" for n in d["nodes"])
    assert cn["lines"][0].endswith("11 of 11 gravity sewer(s) reach an outfall.")
    assert "Outfalls: 1 (1 model outfall); gravity sewers draining to each, most first: OUT1 11." in cn["lines"]
    out = [r for r in cn["rows"] if r["kind"] == "outfall"]
    assert len(out) == 1 and out[0]["node_id"] == "OUT1" and out[0]["sewers"] == 11


def test_dangling_junction_is_a_dead_end(demo_gpkg):
    # B1 -> J1 (junction, nothing downstream) and J1's upstream branch U1 -> J1
    _edit(demo_gpkg, _node_copy("J1", "N08", "junction"), _node_copy("U1", "B1"),
          _conduit("P98", "B1", "J1"), _conduit("P97", "U1", "J1"))
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert (cn["sewers"], cn["sewers_ok"]) == (13, 11) and cn["dead_ends"] == ["J1"]
    links = _by(d["conduits"], "wwn_conduit_id")
    assert links["P98"]["wwn_connectivity"] == "no outfall: dead end at J1" and links["P98"]["wwn_outfall"] == ""
    assert links["P09"]["wwn_connectivity"] == "ok"            # B1 still drains through P09
    nodes = _by(d["nodes"], "node_id")
    assert nodes["U1"]["wwn_connectivity"] == "no outfall: dead end at J1"
    assert nodes["B1"]["wwn_outfall"] == "OUT1"
    line = next(x for x in cn["lines"] if x.startswith("  J1:"))
    assert "2 sewer(s); dangling end (junction, no chamber, nothing downstream); nearest other sewer" in line
    row = next(r for r in cn["rows"] if r["node_id"] == "J1")
    assert row["kind"] == "dead_end" and row["sewers"] == 2 and row["nearest_sewer"] in ("P07", "P08")
    assert row["nearest_m"] == 0.0 and row["x"] is not None
    assert "2 do not." in cn["lines"][0]


def test_closed_loop(demo_gpkg):
    # B1 -> X1 -> X2 -> X1: every path downstream of X1 circles back, no outfall and no dead end
    _edit(demo_gpkg, _node_copy("X1", "B2"), _node_copy("X2", "B3"),
          _conduit("L1", "B1", "X1"), _conduit("L2", "X1", "X2"), _conduit("L3", "X2", "X1"))
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert cn["loops"] == [["X1", "X2"]] and cn["dead_ends"] == []
    assert cn["sewers"] - cn["sewers_ok"] == 3
    links = _by(d["conduits"], "wwn_conduit_id")
    assert {links[k]["wwn_connectivity"] for k in ("L1", "L2", "L3")} == {"no outfall: closed loop at X1"}
    assert any(x.startswith("  X1: 3 sewer(s); closed loop of 2 node(s)") and x.endswith("X1 X2") for x in cn["lines"])
    assert next(r for r in cn["rows"] if r["node_id"] == "X1")["kind"] == "closed_loop"


def test_sink_loops_self_loop_and_chain():
    down = {"a": ["b"], "b": ["c"], "c": ["b"], "s": ["s"], "t": ["s"]}
    assert icm_export._sink_loops(["a", "b", "c", "s", "t"], down) == [["b", "c"], ["s"]]


def test_wet_well_without_pump_is_a_dead_end(demo_gpkg):
    _edit(demo_gpkg, "UPDATE wwn_nodes SET node_type='wet_well' WHERE node_id='OUT1'")
    cn = icm_export.build(demo_gpkg)["connectivity"]
    assert cn["dead_ends"] == ["OUT1"] and cn["sewers_ok"] == 0
    assert any(x.startswith("  OUT1: 11 sewer(s); wet well, no pump exported out of it") for x in cn["lines"])


def test_isolated_node_and_link_to_a_missing_node(demo_gpkg):
    _edit(demo_gpkg, _node_copy("ISO", "B1"), _conduit("PX", "B1", "NOPE"))
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert cn["isolated"] == ["ISO"] and cn["bad_links"] == 1
    assert _by(d["nodes"], "node_id")["ISO"]["wwn_connectivity"] == "no outfall: isolated node"
    px = _by(d["conduits"], "wwn_conduit_id")["PX"]
    assert px["wwn_connectivity"] == "not imported by ICM: node NOPE not in the export"
    assert any("1 node(s) with no link attached" in x for x in cn["lines"])
    assert any("1 link(s) reference a node not in the export" in x and "B1 -> NOPE" in x for x in cn["lines"])


def test_possible_false_outfall(demo_gpkg):
    # Z: a second chamber on N05's position; P04 now ends there, so Z is terminal (an Outfall) right next to
    # P05, which leaves N05: a missed join that silently splits the network in ICM
    _edit(demo_gpkg, _node_copy("Z", "N05"), "UPDATE wwn_conduits SET ds_node_id='Z' WHERE conduit_id='P04'")
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert cn["sewers_ok"] == cn["sewers"] and cn["false_outfalls"] == ["Z"]
    z = _by(d["nodes"], "node_id")["Z"]
    assert z["node_type"] == "Outfall" and z["wwn_outfall_reason"] == "terminal" and z["wwn_outfall"] == "Z"
    line = next(x for x in cn["lines"] if x.startswith("Possible false outfalls"))
    assert "Z (" in line and "P05 at 0.0 m" in line
    assert "OUT1 " in next(x for x in cn["lines"] if x.startswith("Outfalls: 2"))


def _pumped(gp):
    """P08 ends in wet well WW; PS1 pumps through RM1 to D; D -> OUT1 by gravity; isolated D2 (hanging)."""
    _edit(gp, _node_copy("WW", "OUT1", "wet_well"), _node_copy("D", "OUT1"), _node_copy("D2", "B1"),
          "UPDATE wwn_nodes SET invert_level=22.2 WHERE node_id='WW'",
          "UPDATE wwn_conduits SET ds_node_id='WW' WHERE conduit_id='P08'",
          "INSERT INTO wwn_conduits (conduit_id, us_node_id, ds_node_id, dn, length_m, us_invert, ds_invert) "
          "VALUES ('PSG1','D','OUT1',225,50.0,25.0,22.2)",
          "INSERT INTO wwn_pump_stations (ps_id, wet_well_node_id, discharge_node_id, rising_main_id) "
          "VALUES ('PS1','WW','D','RM1')",
          "INSERT INTO wwn_rising_mains (rm_id, ps_id, discharge_node_id, dn, length_m, roughness_type, roughness) "
          "VALUES ('RM1','PS1','D',150,200.0,'ks',1.0)")
    pumpdata.set_station(gp, "PS1", wet_well_area_m2=1.0, stop=22.6, start=23.0, overflow=25.0, n_duty=1,
                         duty_rate_lps=12.5)


def test_pumped_model_connects_through_the_pump(demo_gpkg):
    _pumped(demo_gpkg)
    d = icm_export.build(demo_gpkg)
    cn = d["connectivity"]
    assert d["pumps"] and cn["sewers_ok"] == cn["sewers"] == 12 and cn["dead_ends"] == []
    assert _by(d["conduits"], "wwn_conduit_id")["P08"]["wwn_outfall"] == "OUT1"
    assert cn["isolated"] == ["D2"]


def test_gravity_only_export(demo_gpkg):
    _pumped(demo_gpkg)
    d = icm_export.build(demo_gpkg, gravity_only=True)
    nodes = _by(d["nodes"], "node_id")
    assert d["pumps"] == [] and d["head_discharge"] == [] and d["gravity_only"] is True
    assert all(c["wwn_link_kind"] == "gravity" for c in d["conduits"]) and len(d["conduits"]) == 12
    assert not any(nid.endswith("_DEL") for nid in nodes) and "D2" not in nodes
    assert nodes["WW"]["node_type"] == "Outfall" and nodes["WW"]["wwn_outfall_reason"] == \
        "wet well (gravity only export)"
    assert nodes["D"]["node_type"] == "Manhole" and nodes["OUT1"]["node_type"] == "Outfall"
    cn = d["connectivity"]
    assert cn["sewers_ok"] == cn["sewers"] == 12 and cn["isolated"] == []
    assert _by(d["conduits"], "wwn_conduit_id")["P08"]["wwn_outfall"] == "WW"
    w = " | ".join(d["warnings"])
    assert "Gravity only: 1 node(s) with no gravity sewer attached not exported" in w and "D2" in w
    assert "Gravity only: rising mains, pumps and pump delivery nodes not exported; 1 wet well(s) exported as " \
           "Outfall: WW." in w
    assert "wet well (gravity only export)" in next(x for x in cn["lines"] if x.startswith("Outfalls: 2"))


def test_export_icm_writes_connectivity_csv_fields_and_messages(fake_arcpy, demo_gpkg, tmp_path):
    _edit(demo_gpkg, _node_copy("J1", "N08", "junction"), _conduit("P98", "B1", "J1"))
    a = fake_arcpy([])
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    gdb = str(tmp_path / "demo_ICM.gdb")
    links = a.memory[os.path.join(gdb, "ICM_Conduits")]["rows"]
    p98 = next(x for x in links if x["wwn_conduit_id"] == "P98")
    assert p98["wwn_connectivity"] == "no outfall: dead end at J1" and p98["wwn_outfall"] == ""
    nodes = {x["node_id"]: x for x in a.memory[os.path.join(gdb, "ICM_Nodes")]["rows"]}
    assert nodes["N01"]["wwn_outfall"] == "OUT1" and nodes["J1"]["wwn_connectivity"] == "no outfall: dead end at J1"
    with open(r["connectivity_csv"], newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == [f for f, _ in icm_export.CONNECTIVITY_FIELDS]
    assert [(x["kind"], x["node_id"], x["sewers"]) for x in rows] == [("dead_end", "J1", "1"), ("outfall", "OUT1", "11")]
    warns = [t for k, t in a.log if k == "warn"]
    assert any(t.startswith("Connectivity (downstream along each link to an Outfall") and "1 do not." in t
               for t in warns)
    info = "\n".join(t for _, t in a.log)
    assert "  J1: 1 sewer(s); dangling end" in info and "Connectivity report" in info
    assert "wwn_connectivity" in open(r["notes"], encoding="utf-8").read()


def test_export_icm_gravity_only_removes_stale_pump_tables(fake_arcpy, demo_gpkg, tmp_path):
    _pumped(demo_gpkg)
    a = fake_arcpy([])
    r1 = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path))
    assert r1["pumps_csv"] and os.path.isfile(r1["pumps_csv"])
    r2 = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path), gravity_only=True)
    assert r2["pumps_csv"] is None and not os.path.isfile(r1["pumps_csv"])
    assert os.path.join(str(tmp_path / "demo_ICM.gdb"), "ICM_Pumps") not in a.memory


@pytest.mark.parametrize("gt", [1, 1001])
def test_gpkg_points_reads_2d_and_z_points(gt):
    import struct
    wkb = struct.pack("<BI", 1, gt) + struct.pack("<dd", 1.5, 2.5) + (struct.pack("<d", 9.0) if gt == 1001 else b"")
    blob = b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", 7850) + wkb
    assert icm_export._gpkg_points(blob, 1) == [(1.5, 2.5)]
    assert icm_export._gpkg_points(blob, 2) is None and icm_export._gpkg_points(None, 1) is None


def _station(ps_id, wet_well, like):
    return ("INSERT INTO wwn_pump_stations (geom, ps_id, wet_well_node_id) "
            f"SELECT geom, '{ps_id}', '{wet_well}' FROM wwn_nodes WHERE node_id='{like}'")


def _dangling_j1():
    return [_node_copy("J1", "N08", "junction"), _node_copy("U1", "B1"), _conduit("P98", "B1", "J1"),
            _conduit("P97", "U1", "J1")]


def test_dead_end_near_unmatched_pump_station(demo_gpkg):
    _edit(demo_gpkg, *_dangling_j1(), _node_copy("WW_PS7", "B3", "wet_well"), _station("PS7", "WW_PS7", "N08"))
    cn = icm_export.build(demo_gpkg)["connectivity"]
    line = next(x for x in cn["lines"] if x.startswith("  J1:"))
    assert "pump station PS7 at 0.0 m (likely its inlet sewer: re-import with a wet well distance above this)" in line
    row = next(r for r in cn["rows"] if r["node_id"] == "J1")
    assert row["nearest_ps"] == "PS7" and row["nearest_ps_m"] == 0.0
    assert any(x.startswith("1 dead end(s) are within 50 m of a pump station with no wet well connected: raise")
               for x in cn["lines"])
    assert all(r["nearest_ps"] is None for r in cn["rows"] if r["kind"] != "dead_end")


def test_station_with_matched_wet_well_not_reported(demo_gpkg):
    _edit(demo_gpkg, *_dangling_j1(), _node_copy("WWM", "B3", "wet_well"), _conduit("PW", "WWM", "N07"),
          _station("PS8", "WWM", "N08"))
    cn = icm_export.build(demo_gpkg)["connectivity"]
    row = next(r for r in cn["rows"] if r["node_id"] == "J1")
    assert row["nearest_ps"] is None and row["nearest_ps_m"] is None
    assert "pump station" not in next(x for x in cn["lines"] if x.startswith("  J1:"))
    assert not any("pump station with no wet well" in x for x in cn["lines"])


def test_station_beyond_50_m_not_reported(demo_gpkg):
    _edit(demo_gpkg, *_dangling_j1(), _node_copy("WW_PS7", "B3", "wet_well"), _station("PS7", "WW_PS7", "B1"))
    cn = icm_export.build(demo_gpkg)["connectivity"]
    row = next(r for r in cn["rows"] if r["node_id"] == "J1")
    assert row["nearest_ps"] is None
    assert not any("pump station" in x for x in cn["lines"])


def test_gravity_only_still_reports_station(demo_gpkg):
    _edit(demo_gpkg, *_dangling_j1(), _node_copy("WW_PS7", "B3", "wet_well"), _station("PS7", "WW_PS7", "N08"))
    d = icm_export.build(demo_gpkg, gravity_only=True)
    assert "WW_PS7" not in {n["node_id"] for n in d["nodes"]}
    row = next(r for r in d["connectivity"]["rows"] if r["node_id"] == "J1")
    assert row["nearest_ps"] == "PS7"
    assert any("pump station PS7 at" in x for x in d["connectivity"]["lines"])


def test_no_stations_leaves_output_unchanged(demo_gpkg):
    _edit(demo_gpkg, *_dangling_j1())
    cn = icm_export.build(demo_gpkg)["connectivity"]
    assert not any("pump station" in x for x in cn["lines"])
    assert all(r["nearest_ps"] is None and r["nearest_ps_m"] is None for r in cn["rows"])
    _edit(demo_gpkg, "DROP TABLE wwn_pump_stations")
    cn = icm_export.build(demo_gpkg)["connectivity"]
    assert not any("pump station" in x for x in cn["lines"])
