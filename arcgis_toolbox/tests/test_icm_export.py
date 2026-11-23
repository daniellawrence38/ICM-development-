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
