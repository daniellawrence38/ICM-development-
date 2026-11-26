"""Existing demand from land use (core/demand.py): zone rules, R-codes, DS 50 rates, polygon sampling and
allocation to the nearest sewer's upstream node."""
import math

import pytest

from wwnet.core import demand as dm


def cw(x0, y0, x1, y1):
    """Rectangle ring, clockwise (Esri exterior)."""
    return [(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]


def ccw(x0, y0, x1, y1):
    return list(reversed(cw(x0, y0, x1, y1)))


@pytest.fixture(scope="module")
def rules():
    return dm.Rules.from_template()


@pytest.mark.parametrize("zone,klass", [
    ("Residential", "residential"), ("RESIDENTIAL", "residential"), ("Urban Development", "residential"),
    ("Rural Residential", "zero"), ("Rural Smallholdings", "zero"), ("Rural", "zero"), ("General Farming", "zero"),
    ("Rural Townsite", "residential"), ("Local Road", "zero"), ("Primary Regional Roads", "zero"),
    ("Railways", "zero"), ("Public Open Space", "zero"), ("Parks and Recreation", "zero"),
    ("Environmental Conservation", "zero"), ("Light Industry", "industrial"), ("General Industry", "industrial"),
    ("Industrial Development", "industrial"), ("Commercial", "commercial"), ("Service Commercial", "commercial"),
    ("Public Purposes - Hospital", "commercial"), ("Private Clubs, Institutions and Recreation", "commercial"),
    ("Tourism", "hotel_motel"), ("District Centre", "residential_or_commercial"),
    ("Mixed Use", "residential_or_commercial"), ("Special Use", "commercial"), ("Unzoned", "zero"),
    (None, "zero"), ("", "zero"), ("Broadacre", ""), ("Something Else", ""),
    ("Special purpose", "commercial"), ("Service station", "commercial"), ("General commercial", "commercial"),
    ("Medium and high density residential", "residential"), ("Residential development", "residential"),
    ("Private clubs and institutions", "commercial"), ("Public purposes", "commercial"), ("Business", "commercial"),
])
def test_bundled_zone_rules(rules, zone, klass):
    assert rules.classify(zone)[0] == klass


def test_rules_whole_words_first_match_and_bad_class():
    r = dm.Rules.from_template({"rules": [["road", "zero"], ["residential", "residential"]]})
    assert r.classify("Broadacre Residential")[0] == "residential"     # 'road' is not a word of 'Broadacre'
    assert r.classify("Road / Residential")[0] == "zero"               # first rule wins
    with pytest.raises(ValueError, match="unknown class"):
        dm.Rules.from_template({"rules": [["x", "houses"]]})
    with pytest.raises(ValueError, match="blank_zone_class"):
        dm.Rules.from_template({"rules": [], "options": {"blank_zone_class": "nope"}})


def test_parse_rcode():
    assert dm.parse_rcode(20) == (20.0, "")
    assert dm.parse_rcode(12.5) == (12.5, "")
    assert dm.parse_rcode(0) == (None, "none")
    assert dm.parse_rcode(None) == (None, "none")
    assert dm.parse_rcode("  ") == (None, "none")
    assert dm.parse_rcode("R30") == (30.0, "")
    assert dm.parse_rcode("r 17.5") == (17.5, "")
    assert dm.parse_rcode("R20/40") == (20.0, "dual")
    assert dm.parse_rcode("R20/R60") == (20.0, "dual")
    assert dm.parse_rcode("R-AC3") == (None, "unknown")
    assert dm.parse_rcode("R-AC3", {"RAC3": "R80"}) == (80.0, "")


def test_rates_from_the_ruleset():
    r = dm.load_rates("south", "dry")
    assert r.residential_row(20)[0][1] == "R20" and r.residential_row(20)[1] == ""
    assert r.residential_row(17.5) == (r.row_for(20), "next_higher")      # conservative, as the engine
    assert r.residential_row(10) == (r.row_for(15), "below_min")           # DS 50 4.2.1(c)
    assert r.residential_row(None) == (r.row_for(15), "none")
    assert r.residential_row(9999)[1] == "above_table"
    assert r.residential_lps_per_ha(r.row_for(20)) == pytest.approx(70 * 180 / 86400)
    assert r.non_residential_lps_per_ha("industrial") == pytest.approx(14976 / 86400)
    assert dm.load_rates("north", "wet").residential_lps_per_ha(dm.load_rates("north", "wet").row_for(20)) \
        == pytest.approx(70 * 270 / 86400)
    assert dm.load_rates("north").non_residential_lps_per_ha("perth_cba") is None
    with pytest.raises(ValueError):
        dm.load_rates("east")


def test_region_from_northing():
    assert 7.10e6 < dm.NORTHING_26S < 7.15e6
    assert dm.region_from_northing(6_466_000) == "south"      # Perth
    assert dm.region_from_northing(7_700_000) == "north"      # Karratha


def test_rings_area_hole_contains_and_polygons():
    p = dm.Rings([cw(0, 0, 100, 100), ccw(40, 40, 60, 60)])
    assert p.area() == pytest.approx(10000 - 400)
    assert p.contains(10, 10) and not p.contains(50, 50) and not p.contains(150, 50)
    polys = p.polygons()
    assert len(polys) == 1 and len(polys[0]) == 2
    ext, hole = polys[0]
    assert ext[0] == ext[-1] and dm._signed_area(ext[:-1]) > 0 and dm._signed_area(hole[:-1]) < 0
    # orientation does not matter for the area
    assert dm.Rings([ccw(0, 0, 10, 10), cw(2, 2, 4, 4)]).area() == pytest.approx(96)
    two = dm.Rings([cw(0, 0, 10, 10), cw(20, 0, 30, 10)])
    assert two.area() == pytest.approx(200) and len(two.polygons()) == 2
    assert not dm.Rings([[(0, 0), (1, 1)]])


def test_sample_points_cover_the_polygon_and_slivers_get_one():
    p = dm.Rings([cw(0, 0, 100, 100), ccw(40, 40, 60, 60)])
    pts, s = dm.sample_points(p)
    assert 2.0 <= s <= 20.0 and 35 <= len(pts) <= 70
    assert all(p.contains(*q) for q in pts)
    pts, _s = dm.sample_points(p, target=200)
    assert 150 <= len(pts) <= 260 and all(p.contains(*q) for q in pts)
    big, _s = dm.sample_points(dm.Rings([cw(0, 0, 2000, 2000)]))
    assert _s == 20.0 and len(big) == 100 * 100            # spacing capped at 20 m on large polygons
    sliver = dm.Rings([cw(0, 0, 500, 0.5)])
    pts, _s = dm.sample_points(sliver)
    assert len(pts) >= 1 and all(0 <= q[1] <= 0.5 for q in pts)


def test_sewer_index_nearest():
    ix = dm.SewerIndex([("A", [(0, 0), (100, 0)]), ("B", [(0, 300), (100, 300)])])
    assert ix.nearest((50, 10), 150) == ("A", pytest.approx(10))
    assert ix.nearest((50, 290), 150)[0] == "B"
    assert ix.nearest((50, 150), 149) == (None, None)
    assert ix.nearest((50, 150), 151)[0] in ("A", "B")


def sewers():
    # two sewers 100 m apart along a 100 x 100 block: node A gets the south half, node B the north half
    return [{"conduit_id": "S1", "us_node_id": "A", "coords": [(0, 0), (100, 0)]},
            {"conduit_id": "S2", "us_node_id": "B", "coords": [(0, 100), (100, 100)]}]


def test_calculate_shares_a_block_between_two_sewers(rules):
    zones = [{"id": 1, "zone": "Residential", "rings": [cw(0, 0, 100, 100)]}]
    rc = [{"id": "r", "rcode": "R30", "rings": [cw(-10, -10, 110, 110)]}]
    res = dm.calculate(zones, rc, sewers(), rules)
    by = {s["subcatchment_id"]: s for s in res.subcatchments}
    assert set(by) == {"LU-A-R30", "LU-B-R30"}
    # ~50 samples per polygon: the split between neighbouring nodes is resolved to about one grid row
    assert by["LU-A-R30"]["net_area_ha"] == pytest.approx(0.5, abs=0.08)
    assert by["LU-A-R30"]["net_area_ha"] + by["LU-B-R30"]["net_area_ha"] == pytest.approx(1.0)
    s = by["LU-A-R30"]
    assert (s["land_use"], s["rcode"], s["ground"], s["region"], s["status"]) == \
        ("residential", "R30", "dry", "south", "existing")
    assert res.totals["adwf_total_lps"] == pytest.approx(1.0 * 105 * 180 / 86400)
    assert sum(x["adwf_lps"] for x in res.subcatchments) == pytest.approx(res.totals["adwf_total_lps"])
    row = res.polygons[0]
    assert row["zone_class"] == "residential" and row["rcode_source"] == "R30" and row["ds50_rcode"] == "R30"
    assert row["loaded_ha"] == pytest.approx(1.0) and row["flags"] is None and "A 0." in row["nodes"]
    assert res.issues == []


def test_calculate_classes_rcodes_and_flags(rules):
    far = 5000.0
    zones = [
        {"id": 1, "zone": "Residential", "rings": [cw(0, 0, 50, 100)]},          # R20/40 -> R20 (dual)
        {"id": 2, "zone": "Residential", "rings": [cw(50, 0, 100, 100)]},        # no R-code polygon -> R15
        {"id": 3, "zone": "Local Road", "rings": [cw(100, 0, 110, 100)]},        # zero: not sampled
        {"id": 4, "zone": "Light Industry", "rings": [cw(110, 0, 210, 100)]},
        {"id": 5, "zone": "District Centre", "rings": [cw(0, 0, 10, 10)]},       # under R20/40 -> residential
        {"id": 6, "zone": "Mixed Use", "rings": [cw(60, 0, 70, 10)]},            # no code -> commercial
        {"id": 7, "zone": "Airfield Odd", "rings": [cw(0, 0, 20, 20)]},          # unmatched -> R15, flagged
        {"id": 8, "zone": "Residential", "rings": [cw(far, far, far + 50, far + 50)]},   # beyond 150 m
        {"id": 9, "zone": "Residential", "rings": []},                            # no geometry
        {"id": 10, "zone": "Residential", "rings": [cw(20, 0, 40, 10)]},          # R-AC3 unknown -> R15
        {"id": 11, "zone": "Residential", "rings": [cw(0, 300, 50, 320)]},       # 200 m from S2: near miss
    ]
    rc = [{"id": "a", "rcode": "R20/40", "rings": [cw(-5, -5, 50, 105)]},
          {"id": "b", "rcode": "R-AC3", "rings": [cw(20, 0, 40, 10)]}]
    sew = sewers() + [{"conduit_id": "S3", "us_node_id": "C", "coords": [(110, 50), (210, 50)]}]
    res = dm.calculate(zones, rc, sew, rules)
    rows = {r["polygon_id"]: r for r in res.polygons}
    assert "9" not in rows and res.totals["empty_geometry"] == 1
    assert rows["1"]["ds50_rcode"] == "R20" and rows["1"]["rcode_source"] == "R20/40"
    assert rows["2"]["ds50_rcode"] == "R15" and rows["2"]["rcode_source"] is None
    assert rows["3"]["zone_class"] == "zero" and rows["3"]["loaded_ha"] == 0 and rows["3"]["adwf_lps"] == 0
    assert rows["4"]["ds50_rcode"] == "IND" and rows["4"]["nodes"].startswith("C ")
    assert rows["5"]["ds50_rcode"] == "R20" and rows["6"]["ds50_rcode"] == "COM"
    assert rows["7"]["flags"] == "ZONE_UNMATCHED" and rows["7"]["ds50_rcode"] == "R15"
    assert rows["8"]["flags"] == "OUTSIDE_MODEL" and rows["8"]["loaded_ha"] == 0     # no warning: model edge
    assert rows["11"]["flags"] == "NOT_LOADED"
    assert rows["10"]["ds50_rcode"] == "R20"     # overlapping: R20/40 also covers it, the highest code wins
    codes = {i["code"] for i in res.issues}
    assert codes == {"ZONE_UNMATCHED", "LAND_USE_NOT_LOADED"}
    near = next(i for i in res.issues if i["code"] == "LAND_USE_NOT_LOADED")
    assert near["object_id"] == "11" and "nearest model sewer 20" in near["message"]   # from the sample points
    assert all("x" in i and "y" in i for i in res.issues)
    t = res.totals
    assert t["unmatched"] == {"Airfield Odd": [1, pytest.approx(0.04)]}
    # R-code notes count loaded area only: polygon 2 (polygons 8 and 11 are not loaded)
    assert t["dual_ha"] > 0 and t["no_rcode_ha"] == pytest.approx(0.5, abs=0.02) and t["overlap_ha"] > 0
    assert t["not_loaded_ha"] == pytest.approx(0.25 + 0.1)
    assert (t["outside_n"], t["near_n"]) == (1, 1) and t["outside_ha"] == pytest.approx(0.25)
    assert t["rcode_values"] == {"R20/40": 1, "R-AC3": 1}
    ids = [s["subcatchment_id"] for s in res.subcatchments]
    assert len(ids) == len(set(ids)) and "LU-C-IND" in ids
    ind = next(s for s in res.subcatchments if s["subcatchment_id"] == "LU-C-IND")
    assert ind["land_use"] == "industrial" and ind["rcode"] is None and 0.8 < ind["net_area_ha"] < 1.0
    # the west edge of the block is nearer the end of sewer S2 (node B) than S3: nearest sewer wins
    assert sum(s["net_area_ha"] for s in res.subcatchments if s["subcatchment_id"].endswith("-IND")) \
        == pytest.approx(1.0, abs=1e-4)
    text = "\n".join(res.lines)
    for word in ("Zones: 11 polygon(s)", "'Airfield Odd'", "dual coded", "outside every R-Codes polygon",
                 "Existing ADWF", "0.25 ha in 1 polygon(s) outside the model area", "150-300 m from a model sewer",
                 "  industrial by zone (area loaded): 'Light Industry' 1.00 ha",
                 "  zero by zone (area read): 'Local Road' 0.10 ha", "R-Codes layer values (2 polygon(s)): "):
        assert word in text


def test_calculate_unknown_code_listed_and_alias(rules):
    zones = [{"id": 1, "zone": "Residential", "rings": [cw(0, 0, 100, 100)]}]
    rc = [{"id": "a", "rcode": "R-AC3", "rings": [cw(-5, -5, 105, 105)]}]
    res = dm.calculate(zones, rc, sewers(), rules)
    assert res.totals["unknown_codes"] == {"R-AC3": pytest.approx(1.0)} and "'R-AC3'" in "\n".join(res.lines)
    assert {s["rcode"] for s in res.subcatchments} == {"R15"}
    aliased = dm.Rules(rules.rules, {"RAC3": "R80"})
    res = dm.calculate(zones, rc, sewers(), aliased)
    assert {s["rcode"] for s in res.subcatchments} == {"R80"}


def test_calculate_region_ground_and_north_cba_fallback(rules):
    north = 7_700_000.0
    zones = [{"id": 1, "zone": "Residential", "rings": [cw(0, north, 100, north + 100)]},
             {"id": 2, "zone": "City", "rings": [cw(0, north, 100, north + 100)]}]
    sew = [{"conduit_id": "S", "us_node_id": "A", "coords": [(0, north + 50), (100, north + 50)]}]
    cba = dm.Rules([(dm._words("city"), "perth_cba")] + rules.rules)
    res = dm.calculate(zones, [], sew, cba, dm.Options(ground="wet"))
    assert res.region == "north" and res.ground == "wet"
    assert {s["subcatchment_id"] for s in res.subcatchments} == {"LU-A-R15", "LU-A-COM"}
    assert next(r for r in res.polygons if r["polygon_id"] == "2")["flags"] == "RATE_MISSING"
    res = dm.calculate(zones[:1], [], sew, rules, dm.Options(region="south"))
    assert res.region == "south"


def test_multipolygon_blob_round_trip():
    import struct
    p = dm.Rings([cw(0, 0, 100, 100), ccw(40, 40, 60, 60), cw(200, 0, 210, 10)])
    blob = dm.multipolygon_blob(p.polygons())
    assert blob[:2] == b"GP" and struct.unpack("<i", blob[4:8])[0] == 7850
    x0, x1, y0, y1 = struct.unpack("<4d", blob[8:40])
    assert (x0, x1, y0, y1) == (0, 210, 0, 100)
    bo, typ, n = struct.unpack("<BII", blob[40:49])
    assert (bo, typ, n) == (1, 6, 2)
    assert dm.multipolygon_blob([]) is None


def test_template_is_found_and_listed_classes_are_valid():
    cfg = dm.load_template()
    assert cfg["rules"] and all(len(r) == 2 for r in cfg["rules"])
    assert {r[1] for r in cfg["rules"]} <= set(dm.CLASSES)
    assert math.isclose(dm.Options().max_distance_m, 150.0)
