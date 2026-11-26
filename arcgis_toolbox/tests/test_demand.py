"""Calculate Existing Demand: DPLH-071 zones + DPLH-070 R-codes -> wwn_subcatchments LU-* rows and the
Land use loading review layer, on the demo model (sewers along y = 6466000, branch at x = 392140)."""
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import fake_arcpy as fa
from wwnet_arcgis import demand_import as di, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parent.parent
GEOM = "_geom"
GDA2020 = NS(factoryCode=7850, name="GDA2020 / MGA zone 50")
Y = 6466000.0


def cw(x0, y0, x1, y1):
    return [(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]


def ccw(x0, y0, x1, y1):
    return list(reversed(cw(x0, y0, x1, y1)))


ZONE_ROWS = [
    {"OID@": 11, "ZONE": "Residential", GEOM: [cw(392000, Y + 10, 392280, Y + 80)]},
    {"OID@": 12, "ZONE": "Residential", GEOM: [cw(392150, Y + 80, 392250, Y + 240), ccw(392190, Y + 150, 392210, Y + 170)]},
    {"OID@": 13, "ZONE": "Local Road", GEOM: [cw(392000, Y - 10, 392560, Y + 10)]},
    {"OID@": 14, "ZONE": "Light Industry", GEOM: [cw(392280, Y - 110, 392560, Y - 10)]},
    {"OID@": 15, "ZONE": "Helipad", GEOM: [cw(392400, Y + 10, 392440, Y + 50)]},
    {"OID@": 16, "ZONE": "Residential", GEOM: [cw(399000, Y, 399100, Y + 100)]},       # far away: not read
]
RCODE_ROWS = [
    {"OID@": 1, "R_CODE_NO": "R20/40", GEOM: [cw(391900, Y, 392200, Y + 300)]},
    {"OID@": 2, "R_CODE_NO": "R30", GEOM: [cw(392200, Y, 392600, Y + 300)]},
]


@pytest.fixture
def world(fake_arcpy, demo_gpkg):
    layers = []
    a = fake_arcpy(layers)
    a.sources["C:/d/zones"] = {"fields": [("OBJECTID", "OID"), ("ZONE", "String"), ("ZONE_NO", "Integer"),
                                          ("Shape", "Geometry")], "sr": GDA2020, "rows": [dict(r) for r in ZONE_ROWS]}
    a.sources["C:/d/rcodes"] = {"fields": [("OBJECTID", "OID"), ("R_CODE_NO", "String"), ("Shape", "Geometry")],
                                "sr": GDA2020, "rows": [dict(r) for r in RCODE_ROWS]}
    lyr = {k: fa.make_layer(k, f"C:/d/{k}") for k in ("zones", "rcodes")}
    return a, lyr, demo_gpkg, layers


def run(a, lyr, gp, **kw):
    kw.setdefault("add_to_map", False)
    return tools_impl.calculate_existing_demand(a, Messages(a), gp, lyr["zones"], "ZONE", lyr["rcodes"],
                                                "R_CODE_NO", **kw)


def q(gp, sql, args=()):
    con = sqlite3.connect(gp)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def log_text(a):
    return "\n".join(t for _, t in a.log)


def test_writes_subcatchments_review_layer_and_issues(world):
    a, lyr, gp, _layers = world
    res = run(a, lyr, gp)
    assert res is not None
    rows = q(gp, "SELECT subcatchment_id, outlet_node_id, land_use, rcode, ground, region, status, net_area_ha, "
                 "geom FROM wwn_subcatchments WHERE subcatchment_id LIKE 'LU-%' ORDER BY subcatchment_id")
    ids = [r[0] for r in rows]
    assert ids == sorted(s["subcatchment_id"] for s in res.subcatchments) and len(ids) == len(set(ids))
    assert all(r[6] == "existing" and r[4] == "dry" and r[5] == "south" and r[8] is None for r in rows)
    lu = {r[2] for r in rows}
    assert lu == {"residential", "industrial"}
    codes = {r[3] for r in rows if r[2] == "residential"}
    assert codes == {"R15", "R20", "R30"}                   # R20/40 -> R20; R30; Helipad (unmatched) -> R15
    # the demo's own 4 sub-catchments are kept, with a double counting warning
    assert q(gp, "SELECT count(*) FROM wwn_subcatchments WHERE subcatchment_id NOT LIKE 'LU-%'")[0][0] == 4
    text = log_text(a)
    assert "4 other sub-catchment(s)" in text and "Helipad" in text and "Existing ADWF" in text
    # review layer: registered feature table, one row per polygon read (the far one is filtered out)
    cont = q(gp, "SELECT data_type, identifier, srs_id, min_x FROM gpkg_contents WHERE table_name='wwn_land_use'")
    assert cont[0][:3] == ("features", "Land use loading", 7850) and cont[0][3] == pytest.approx(392000)
    assert q(gp, "SELECT geometry_type_name FROM gpkg_geometry_columns WHERE table_name='wwn_land_use'") == \
        [("MULTIPOLYGON",)]
    rev = {r[0]: r[1:] for r in q(gp, "SELECT polygon_id, zone_class, ds50_rcode, area_ha, loaded_ha, flags, "
                                      "geom IS NOT NULL FROM wwn_land_use")}
    assert set(rev) == {"11", "12", "13", "14", "15"}
    assert rev["12"][2] == pytest.approx((100 * 160 - 400) / 1e4)          # hole subtracted
    assert rev["13"][0] == "zero" and rev["13"][3] == 0
    assert rev["15"][4] == "ZONE_UNMATCHED" and all(r[5] for r in rev.values())
    iss = q(gp, "SELECT code, object_type, geom IS NOT NULL FROM wwn_issues WHERE source='demand'")
    assert ("ZONE_UNMATCHED", "land_use", 1) in iss
    # zones read with a spatial filter (never the whole layer), R-codes too
    reads = [c for c in a.cursor_log if c["source"] in ("C:/d/zones", "C:/d/rcodes")]
    assert len(reads) == 2 and all(c["spatial_filter"] is not None and not c["full_scan"] for c in reads)


def test_engine_loads_the_subcatchments_with_the_same_adwf(world):
    wn = pytest.importorskip("wwnet_native")
    a, lyr, gp, _layers = world
    before = wn.simulate(gp, duration_h=2.0, warmup_h=1.0, write=False)["summary"]["inflow_adwf_lps"]
    res = run(a, lyr, gp)
    after = wn.simulate(gp, duration_h=2.0, warmup_h=1.0, write=False)["summary"]["inflow_adwf_lps"]
    assert res.totals["adwf_total_lps"] > 0.1
    assert after - before == pytest.approx(res.totals["adwf_total_lps"], rel=1e-6)
    chk = wn.check(gp)
    assert not [i for i in chk if i.get("object_type") == "subcatchment" and i.get("severity") == "error"]


def test_rerun_replaces_and_preview_writes_nothing(world):
    a, lyr, gp, _layers = world
    run(a, lyr, gp)
    n1 = q(gp, "SELECT count(*) FROM wwn_subcatchments")[0][0]
    n_lu = q(gp, "SELECT count(*) FROM wwn_land_use")[0][0]
    run(a, lyr, gp)
    assert q(gp, "SELECT count(*) FROM wwn_subcatchments")[0][0] == n1
    assert q(gp, "SELECT count(*) FROM wwn_land_use")[0][0] == n_lu
    assert "replaced" in log_text(a)


def test_preview_on_a_fresh_model(world):
    a, lyr, gp, _layers = world
    res = run(a, lyr, gp, preview=True)
    assert res is not None and res.subcatchments
    assert q(gp, "SELECT count(*) FROM wwn_subcatchments WHERE subcatchment_id LIKE 'LU-%'")[0][0] == 0
    assert not q(gp, "SELECT name FROM sqlite_master WHERE name='wwn_land_use'")
    assert "PREVIEW" in log_text(a)


def _add_cp_loads(gp):
    con = sqlite3.connect(gp)
    con.executemany("INSERT INTO wwn_node_loads (load_id, node_id, connections, source) VALUES (?,?,?,?)",
                    [("CP-N01", "N01", 12, "connection_points"), ("CP-N02", "N02", 8, "connection_points"),
                     ("M1", "N03", None, "manual")])
    con.execute("UPDATE wwn_node_loads SET adwf_lps=0.5 WHERE load_id='M1'")
    con.commit()
    con.close()


def test_connection_point_loads_replaced_or_kept(world):
    a, lyr, gp, _layers = world
    _add_cp_loads(gp)
    run(a, lyr, gp, replace_cp_loads=False)
    assert q(gp, "SELECT count(*) FROM wwn_node_loads")[0][0] == 3
    assert "KEPT (both load the network" in log_text(a)
    run(a, lyr, gp)
    assert q(gp, "SELECT load_id FROM wwn_node_loads") == [("M1",)]        # manual loads stay
    text = log_text(a)
    assert "2 node load(s), 20 connection(s) x 630 L/d = 0.15 L/s" in text and "2 removed" in text


def test_selection_is_used_and_no_rcode_layer(world):
    a, lyr, gp, _layers = world
    lyr["zones"] = fa.make_layer("zones", "C:/d/zones", selection=[1])
    res = tools_impl.calculate_existing_demand(a, Messages(a), gp, lyr["zones"], "ZONE", add_to_map=False)
    assert [r["polygon_id"] for r in res.polygons] == ["11"]
    assert {s["rcode"] for s in res.subcatchments} == {"R15"}
    assert "No R-Codes layer" in log_text(a)


def test_errors(world, monkeypatch, tmp_path):
    a, lyr, gp, _layers = world
    with pytest.raises(RuntimeError, match="does not exist"):
        run(a, lyr, str(tmp_path / "nope.gpkg"))
    with pytest.raises(RuntimeError, match="zone field"):
        tools_impl.calculate_existing_demand(a, Messages(a), gp, lyr["zones"], "")
    with pytest.raises(RuntimeError, match="field 'NOPE' does not exist"):
        tools_impl.calculate_existing_demand(a, Messages(a), gp, lyr["zones"], "NOPE", add_to_map=False)
    bad = tmp_path / "bad.toml"
    bad.write_text('rules = [["x", "houses"]]\n', encoding="utf-8")
    assert run(a, lyr, gp, config=str(bad)) is None
    assert a.log[-1][1].startswith("ERROR: zone rules template")

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(di, "write_result", locked)
    a.log.clear()
    assert run(a, lyr, gp) is None
    assert a.log[-1][1].startswith("ERROR: The model GeoPackage is locked")
    a.sources["C:/d/zones"]["rows"] = []
    assert run(a, lyr, gp) is None and "no zone polygons" in a.log[-1][1]


def test_layer_added_to_map(world):
    a, lyr, gp, layers = world
    run(a, lyr, gp, add_to_map=True)
    paths = [p for _k, p in a.added]
    assert any(p.endswith("main.wwn_land_use") for p in paths)
    assert any(p.endswith("main.wwn_issues") for p in paths)


def test_field_defaults():
    assert di.default_zone_field(["OBJECTID", "ZONE_NO", "Zone", "Shape"]) == "Zone"
    assert di.default_zone_field(["OBJECTID", "ZONE_NO", "SCHEME_ZONE_NAME"]) == "SCHEME_ZONE_NAME"
    assert di.default_zone_field(["OBJECTID", "ZONE_NO"]) == ""
    assert di.default_rcode_field(["OBJECTID", "RCODE", "RCODE_NO"]) == "RCODE_NO"
    assert di.default_rcode_field(["R_Code_Number"]) == "R_Code_Number"
    assert di.default_rcode_field(["OBJECTID", "NAME"]) == ""


def test_polygon_rings_from_pro_style_parts():
    shape = fa.FakePolygonShape([cw(0, 0, 10, 10), ccw(2, 2, 4, 4), cw(20, 0, 30, 10)])
    assert shape.partCount == 2 and None in list(shape.getPart(0))
    rings = di.polygon_rings(shape)
    assert len(rings) == 3 and rings[1][0] == (2, 2)
    assert di.polygon_rings(None) == []


# ------------------------------------------------------------------ the .pyt tool

@pytest.fixture
def pyt(fake_arcpy):
    fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_ced", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_ced", loader))
    loader.exec_module(mod)
    return mod


def test_tool_parameters_update_and_execute(pyt, world):
    a, lyr, gp, _layers = world
    pyt.arcpy = a
    t = pyt.CalculateExistingDemand()
    assert t.label == "Calculate Existing Demand" and t.category == "Data"
    ps = t.getParameterInfo()
    p = {x.name: x for x in ps}
    assert list(p) == ["gpkg", "zones_layer", "zone_field", "rcodes_layer", "rcode_field", "use_selected", "preview",
                       "add_to_map", "max_distance", "ground", "region", "replace_cp", "config", "out_gpkg"]
    assert p["zone_field"].parameterDependencies == ["zones_layer"]
    assert p["rcode_field"].parameterDependencies == ["rcodes_layer"]
    assert p["max_distance"].value == 150.0 and p["replace_cp"].value is True and p["preview"].value is False
    assert p["ground"].filter.list == list(pyt.DEMAND_GROUND) and p["region"].filter.list == list(pyt.DEMAND_REGION)
    for word in ("DPLH-071", "DPLH-070", "DS 50", "R15", "LU-<node>-<class>"):
        assert word in t.description
    p["gpkg"].value = gp
    p["zones_layer"].value, p["rcodes_layer"].value = lyr["zones"], lyr["rcodes"]
    t.updateParameters(ps)
    assert p["zone_field"].value == "ZONE" and p["rcode_field"].value == "R_CODE_NO"
    p["add_to_map"].value = False
    t.execute(ps, None)
    assert p["out_gpkg"].value == gp
    assert q(gp, "SELECT count(*) FROM wwn_subcatchments WHERE subcatchment_id LIKE 'LU-%'")[0][0] > 0
