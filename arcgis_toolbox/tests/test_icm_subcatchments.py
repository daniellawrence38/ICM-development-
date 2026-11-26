"""Export To ICM: foul sub-catchments, one per loaded node (land-use LU-* rows, other sub-catchments, node loads)."""
import csv
import os
import sqlite3

import pytest

import fake_arcpy as fa
import test_demand as td
from wwnet_arcgis import icm_export, tools_impl
from wwnet_arcgis.messages import Messages


def q(gp, sql, args=()):
    con = sqlite3.connect(gp)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def flow(r, per_capita=180.0):
    return (r["population"] or 0) * per_capita / 86400 + (r["trade_flow"] or 0) + (r["additional_foul_flow"] or 0)


def test_land_use_loading_reaches_icm_with_the_engine_flow(td_world, tmp_path):
    wn = pytest.importorskip("wwnet_native")
    a, lyr, gp, _layers = td_world
    td.run(a, lyr, gp)                                    # Calculate Existing Demand on the demo model
    engine = wn.simulate(gp, duration_h=2.0, warmup_h=1.0, write=False)["summary"]["inflow_adwf_lps"]
    r = tools_impl.export_icm(a, Messages(a), gp, str(tmp_path / "icm"))
    rows = r["subcatchments"]
    assert rows and len({x["subcatchment_id"] for x in rows}) == len(rows)
    assert all(x["subcatchment_id"] == x["node_id"] for x in rows)       # one per loaded node
    lu_nodes = {n for (n,) in q(gp, "SELECT DISTINCT outlet_node_id FROM wwn_subcatchments")}
    assert {x["node_id"] for x in rows} == lu_nodes
    assert sum(x["wwn_adwf_lps"] for x in rows) == pytest.approx(engine, rel=1e-4)
    assert all(flow(x) == pytest.approx(x["wwn_adwf_lps"], abs=1e-4) for x in rows)
    ind = [x for x in rows if x["wwn_industrial_ha"]]
    assert ind and all(x["trade_flow"] > 0 and x["trade_profile"] == 1 for x in ind)
    res = [x for x in rows if x["wwn_residential_ha"]]
    assert res and all(x["population"] > 0 and x["wastewater_profile"] == 1 for x in res)
    assert all(x["contributing_area"] == 0 and x["connectivity"] == 100 and x["system_type"] == "foul" for x in rows)
    # geodatabase point table at the nodes, CSV with x / y = the node location
    gdb = r["gdb"]
    fc = a.memory[os.path.join(gdb, "ICM_Subcatchments")]
    assert fc["geom"] == "POINT" and [f for f, _ in fc["fields"]] == [f for f, _ in icm_export.SUBCATCHMENT_FIELDS]
    assert len(fc["rows"]) == len(rows) and all(row["SHAPE@"] is not None for row in fc["rows"])
    with open(r["nodes_csv"], newline="") as fh:
        node_xy = {n["node_id"]: (float(n["x"]), float(n["y"])) for n in csv.DictReader(fh)}
    with open(r["subcatchments_csv"], newline="") as fh:
        sc = list(csv.DictReader(fh))
    assert len(sc) == len(rows)
    for x in sc:
        assert (float(x["x"]), float(x["y"])) == pytest.approx(node_xy[x["node_id"]], abs=1e-3)
    notes = open(r["notes"], encoding="utf-8").read()
    for word in ("Sub-catchment table", "per capita flow 180 l/hd/d", "Trade Waste event", "contributing_area = 0",
                 "then sub-catchments", "set the ODIC unit to l/s"):
        assert word in notes
    info = "\n".join(t for _, t in a.log)
    assert f"{len(rows)} sub-catchment(s)" in info and "Waste Water / Trade Waste events" in info


@pytest.fixture
def td_world(fake_arcpy, demo_gpkg):
    layers = []
    a = fake_arcpy(layers)
    a.sources["C:/d/zones"] = {"fields": [("OBJECTID", "OID"), ("ZONE", "String"), ("Shape", "Geometry")],
                               "sr": td.GDA2020, "rows": [dict(r) for r in td.ZONE_ROWS]}
    a.sources["C:/d/rcodes"] = {"fields": [("OBJECTID", "OID"), ("R_CODE_NO", "String"), ("Shape", "Geometry")],
                                "sr": td.GDA2020, "rows": [dict(r) for r in td.RCODE_ROWS]}
    lyr = {k: fa.make_layer(k, f"C:/d/{k}") for k in ("zones", "rcodes")}
    return a, lyr, demo_gpkg, layers


def _subs(**kw):
    base = {"subcatchment_id": "S", "outlet_node_id": "N1", "net_area_ha": 1.0, "gross_area_ha": None,
            "broadacre": 0, "land_use": "residential", "rcode": "R20", "ground": "dry", "region": "south",
            "extra_flow_lps": 0.0, "adwf_lps": None, "profile_id": None, "status": "existing"}
    base.update(kw)
    return base


def test_subcatchments_mapping_rules():
    w = []
    subs = [_subs(subcatchment_id="LU-N1-R20"),
            _subs(subcatchment_id="LU-N1-COM", land_use="commercial", rcode=None, net_area_ha=0.5),
            _subs(subcatchment_id="LU-N1-IND", land_use="industrial", rcode=None, net_area_ha=0.5),
            _subs(subcatchment_id="M1", outlet_node_id="N2", adwf_lps=0.3, profile_id="res"),
            _subs(subcatchment_id="B1", outlet_node_id="N3", net_area_ha=None, gross_area_ha=2.0, broadacre=1,
                  rcode="R12.5"),
            _subs(subcatchment_id="X", outlet_node_id="GONE"),
            _subs(subcatchment_id="P", outlet_node_id="N3", net_area_ha=None),
            _subs(subcatchment_id="E", outlet_node_id="N3", extra_flow_lps=2.0, net_area_ha=0.0)]
    loads = [{"load_id": "CP-N2", "node_id": "N2", "connections": 10, "adwf_lps": None, "profile_id": None},
             {"load_id": "L0", "node_id": "N4", "connections": None, "adwf_lps": 0.0, "profile_id": None}]
    out = icm_export.subcatchments(subs, loads, {"adwf_per_connection_lpd": "600"}, {"N1", "N2", "N3", "N4"}, w)
    rows = {r["subcatchment_id"]: r for r in out["rows"]}
    assert set(rows) == {"N1", "N2", "N3"}                       # N4's load is zero: no sub-catchment
    n1 = rows["N1"]
    assert n1["population"] == pytest.approx(70.0)                # R20, 1 ha
    assert n1["trade_flow"] == pytest.approx(0.5 * 9450 / 86400 + 0.5 * 14976 / 86400, abs=1e-5)  # one trade profile
    assert (n1["wwn_residential_ha"], n1["wwn_commercial_ha"], n1["wwn_industrial_ha"]) == (1.0, 0.5, 0.5)
    assert n1["total_area"] == 2.0 and n1["additional_foul_flow"] is None and n1["notes"] is None
    n2 = rows["N2"]
    assert n2["population"] == 0 and n2["additional_foul_flow"] == pytest.approx(0.3 + 10 * 600 / 86400, abs=1e-5)
    assert n2["wastewater_profile"] == 1 and n2["trade_profile"] is None and n2["total_area"] == 0.01
    assert "M1 manual ADWF" in n2["notes"] and "node load CP-N2" in n2["notes"]
    n3 = rows["N3"]                                               # broadacre: gross x 0.75, R12.5 -> R15
    assert n3["population"] == pytest.approx(1.5 * 52.5) and n3["wwn_rcodes"] == "R15 1.50 ha"
    text = "\n".join(w)
    assert "drain to a node that is not in the model" in text and "X" in text
    assert "no net area" in text and "P" in text
    assert "extra_flow_lps" in text and "E" in text
    assert out["summary"]["profiles"] == ["res"]
    assert out["summary"]["adwf_lps"] == pytest.approx(sum(r["wwn_adwf_lps"] for r in out["rows"]))


def test_wet_ground_population_is_scaled_to_the_profile_per_capita():
    w = []
    subs = [_subs(subcatchment_id="LU-N1-R15", rcode="R15", ground="wet")]
    out = icm_export.subcatchments(subs, [], {}, {"N1"}, w)
    r = out["rows"][0]
    pc = out["summary"]["per_capita_lpd"]                         # wet south: R20 row 220 L/p/d
    assert pc == 220
    assert r["population"] * pc / 86400 == pytest.approx(52.5 * 230 / 86400, abs=1e-5)   # R15 wet: 230 L/p/d
    assert r["wwn_adwf_lps"] == pytest.approx(52.5 * 230 / 86400, abs=1e-5)


def test_no_loads_removes_stale_subcatchment_table(fake_arcpy, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    out = tmp_path / "icm"
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    fc = os.path.join(r["gdb"], "ICM_Subcatchments")
    assert fc in a.memory and os.path.isfile(r["subcatchments_csv"])
    con = sqlite3.connect(demo_gpkg)
    con.execute("DELETE FROM wwn_subcatchments")
    con.commit()
    con.close()
    r2 = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(out))
    assert r2["subcatchments_csv"] is None and fc not in a.memory
    assert not os.path.exists(os.path.join(str(out), "demo_ICM_subcatchments.csv"))
    assert "loads (the model has none)" in open(r2["notes"], encoding="utf-8").read()


def test_every_object_has_the_same_system_type(fake_arcpy, demo_gpkg, tmp_path):
    """ICM engineering validation: 'Node is a different system type than the subcatchment'."""
    a = fake_arcpy([])
    r = tools_impl.export_icm(a, Messages(a), demo_gpkg, str(tmp_path / "icm"))
    rows = r["nodes"] + r["conduits"] + r["pumps"] + r["subcatchments"]
    assert r["subcatchments"] and {x["system_type"] for x in rows} == {"foul"}
    for fields in (icm_export.NODE_FIELDS, icm_export.CONDUIT_FIELDS, icm_export.PUMP_FIELDS,
                   icm_export.SUBCATCHMENT_FIELDS):
        assert ("system_type", "TEXT") in fields
    with open(r["nodes_csv"], newline="") as fh:
        assert {n["system_type"] for n in csv.DictReader(fh)} == {"foul"}
    assert "system_type = foul on every node" in open(r["notes"], encoding="utf-8").read()
