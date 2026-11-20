import datetime
import json
import math
import sqlite3

import pytest

from wwnet.core import importer as imp
from wwnet.core.importer import GEOM, FieldMap, ImportOptions, build_model


def ac(pid, x, y, top=20.0):
    return {"PACID": pid, "TOPLEV": top, GEOM: (x, y)}


def pipe(pid, line, us=None, ds=None, **kw):
    row = {"ID": pid, "STATUS": "A- actual", "MAINTYPE": "R- retic", "PURPOSE": "L- local",
           "DOWNSTREAM": 17.450001, "UPSTREAM": 17.459999, "FLOW_GRADE": 120, "LENGTH": 1.000555,
           "MATERIAL": "PVC- polyvinyl chloride", "PLANSET": "PS1", "DATEINST": None,
           "US_MANHOLE_ID": us, "DS_MANHOLE_ID": ds, GEOM: line}
    row.update(kw)
    return row


def sample_rows():
    """The two sample sewers from the Water Corporation data, each between two chambers."""
    acs = [ac("A1", 0, 0, 20.0), ac("A2", 1.000555, 0, 19.9), ac("A3", 100, 0, 5.0), ac("A4", 110.326591, 0, 4.0)]
    p1 = pipe(22030208, [(0, 0), (1.000555, 0)], "A1", "A2")
    p2 = pipe(22030223, [(100, 0), (110.326591, 0)], "A3", "A4", STATUS="A- actual", MAINTYPE="M- main",
              DOWNSTREAM=0.43, UPSTREAM=0.49, FLOW_GRADE=150, LENGTH=10.326591,
              MATERIAL="VC- vitrified clay", PLANSET="PS2")
    return acs, [p1, p2]


def codes_of(rep):
    return [w.code for w in rep.warnings]


def by_id(items, key):
    return {x[key]: x for x in items}


def test_code_helpers():
    assert imp.code_of("A- actual") == "A"
    assert imp.code_of(" pvc -polyvinyl") == "PVC"
    assert imp.code_of("R") == "R" and imp.code_of(None) == "" and imp.code_of("  ") == ""
    assert imp.to_id(22030208.0) == "22030208" and imp.to_id(" X1 ") == "X1" and imp.to_id(None) == ""
    assert imp.year_of(datetime.datetime(1987, 3, 4)) == 1987
    assert imp.year_of("1999-05-12") == 1999 and imp.year_of("12/05/2003") == 2003
    assert imp.year_of("20030512") == 2003 and imp.year_of("n/a") is None and imp.year_of(1500) is None


def test_template_defaults():
    cfg = imp.load_template()
    fm = FieldMap.from_config(cfg)
    assert fm.ac_id == "PACID" and fm.us_node == "US_MANHOLE_ID" and fm.diameter == "HEIGHT"
    o = ImportOptions.from_config(cfg)
    assert (o.snap_tolerance_m, o.grade_source, o.grade_tolerance_pct) == (0.5, "levels", 10.0)
    assert cfg["codes"]["material"]["CI"] == "other" and cfg["codes"]["sewer_type"]["IO"] == "io"
    assert fm.with_overrides(diameter="HEIGHT", status=None).diameter == "HEIGHT"
    with pytest.raises(KeyError):
        fm.with_overrides(nope="x")


def test_sample_rows_basic_mapping():
    acs, pipes = sample_rows()
    pipes[0]["HEIGHT"], pipes[1]["HEIGHT"] = 150, 225
    model, rep = build_model(acs, [], pipes, FieldMap().with_overrides(diameter="HEIGHT"))
    c = by_id(model["conduits"], "conduit_id")
    p1, p2 = c["22030208"], c["22030223"]
    assert (p1["us_node_id"], p1["ds_node_id"]) == ("A1", "A2")
    assert p1["us_invert"] == 17.459999 and p1["ds_invert"] == 17.450001 and p1["length_m"] == 1.000555
    assert p1["material"] == "pvc" and p1["sewer_type"] == "reticulation" and p1["status"] == "existing"
    assert p2["material"] == "vc" and p2["sewer_type"] == "main" and p2["dn"] == 225
    assert p1["asset_id"] == "22030208" and p1["data_source"] == "gis"
    assert p1["notes"] == "PURPOSE=L; PLANSET=PS1"
    n = by_id(model["nodes"], "node_id")
    assert n["A1"]["invert_level"] == 17.459999 and n["A2"]["invert_level"] == 17.450001
    assert n["A1"]["surface_level"] == 20.0 and n["A1"]["node_type"] == "ac" and n["A1"]["data_source"] == "gis"
    assert "invert derived from lowest incoming sewer ds invert" in n["A2"]["notes"]
    assert "lowest outgoing sewer us invert" in n["A1"]["notes"]
    # both sample sewers disagree with FLOW_GRADE by more than 10%: 1 in 100 vs 120 and 1 in 172 vs 150
    gm = {w.object_id: w.message for w in rep.warnings if w.code == "grade_mismatch"}
    assert set(gm) == {"22030208", "22030223"}
    assert "1 in 100.1" in gm["22030208"] and "120" in gm["22030208"]
    assert "1 in 172.1" in gm["22030223"] and "150" in gm["22030223"]
    _, loose = build_model(acs, [], pipes, FieldMap(diameter="HEIGHT"), ImportOptions(grade_tolerance_pct=20))
    assert not [w for w in loose.warnings if w.code == "grade_mismatch"]
    assert rep.counts["nodes"] == 4 and rep.counts["conduits"] == 2
    assert not rep.unknown_codes


def test_node_invert_is_min_of_connected_pipes():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=5.0, DOWNSTREAM=4.9, LENGTH=10)
    p2 = pipe("P2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=4.85, DOWNSTREAM=4.7, LENGTH=10)
    model, _ = build_model(acs, [], [p1, p2])
    n = by_id(model["nodes"], "node_id")
    assert n["B"]["invert_level"] == 4.85 and n["A"]["invert_level"] == 5.0 and n["C"]["invert_level"] == 4.7


def test_dn_normalisation_and_unmapped():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    rows = []
    for i, v in enumerate([150, 152.0, 240, "abc", None]):
        rows.append(pipe(f"P{i}", [(0, 0), (10, 0)], "A", "B", HEIGHT=v, UPSTREAM=5.0, DOWNSTREAM=4.0, LENGTH=10))
    model, rep = build_model(acs, [], rows, FieldMap(diameter="HEIGHT"))
    c = by_id(model["conduits"], "conduit_id")
    assert c["P0"]["dn"] == 150 and "internal_diameter_mm" not in c["P0"]
    assert c["P1"]["dn"] == 150 and c["P1"]["internal_diameter_mm"] == 152.0
    assert c["P2"]["dn"] == 225 and c["P2"]["internal_diameter_mm"] == 240.0 and "source diameter 240" in c["P2"]["notes"]
    assert c["P3"]["dn"] == 150 and "dn_assumed" in codes_of(rep)
    assert sum(w.code == "dn_assumed" for w in rep.warnings) == 2
    assert any("P1" in i for i in rep.infos)
    # field not mapped at all: loud single warning with the count
    model, rep = build_model(acs, [], rows, FieldMap())
    assert all(x["dn"] == 150 for x in model["conduits"])
    w = [w for w in rep.warnings if w.code == "dn_not_mapped"]
    assert len(w) == 1 and "5 conduit" in w[0].message and "ASSUMED" in w[0].message
    assert rep.counts["dn_assumed"] == 5


def test_direction_reversal():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    p = pipe("P", [(10, 0), (0, 0)], "A", "B", LENGTH=10, UPSTREAM=5, DOWNSTREAM=4)
    model, rep = build_model(acs, [], [p])
    c = model["conduits"][0]
    assert c["geom"] == [[0, 0], [10, 0]] and rep.counts["reversed"] == 1
    assert "direction_reversed" in codes_of(rep) and "geometry reversed" in c["notes"]
    # correct direction untouched
    model, rep = build_model(acs, [], [pipe("P", [(0, 0), (10, 0)], "A", "B", LENGTH=10)])
    assert model["conduits"][0]["geom"] == [[0, 0], [10, 0]] and "reversed" not in rep.counts


def test_snapping_blank_and_unmatched_ids():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    p = pipe("P", [(0.3, 0), (10.2, 0)], None, "NOPE", LENGTH=10, UPSTREAM=5, DOWNSTREAM=4)
    model, rep = build_model(acs, [], [p])
    c = model["conduits"][0]
    assert (c["us_node_id"], c["ds_node_id"]) == ("A", "B")
    assert rep.counts["ends_snapped"] == 2 and len(model["nodes"]) == 2
    msgs = " ".join(str(w) for w in rep.warnings)
    assert "blank" in msgs and "unmatched" in msgs
    # outside tolerance: option respected
    p = pipe("P", [(0.3, 0), (10.2, 0)], None, None, LENGTH=10)
    model, rep = build_model(acs, [], [p], options=ImportOptions(snap_tolerance_m=0.1))
    assert rep.counts["dangling_ends"] == 2 and "placeholder_nodes" not in rep.counts


def test_dangling_ends_and_shared_junction():
    acs = [ac("A", 0, 0)]
    p1 = pipe("P1", [(0, 0), (50, 0)], "A", None, LENGTH=50, UPSTREAM=5, DOWNSTREAM=4)
    p2 = pipe("P2", [(50.2, 0), (80, 0)], None, None, LENGTH=30, UPSTREAM=4, DOWNSTREAM=3)
    model, rep = build_model(acs, [], [p1, p2])
    n = by_id(model["nodes"], "node_id")
    assert not [k for k in n if k.startswith("X")]
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["ds_node_id"] == "J1" and c["P2"]["us_node_id"] == "J1"      # coincident ends share a junction
    assert n["J1"]["node_type"] == "junction" and n["J1"]["notes"].startswith("segment junction")
    assert c["P2"]["ds_node_id"] == "E1" and n["E1"]["notes"].startswith("dangling end")
    assert rep.counts["junction_nodes"] == 1 and rep.counts["dangling_ends"] == 1
    assert n["J1"]["invert_level"] == 4      # outgoing P2 us invert


def test_connection_points():
    acs = [ac("A", 0, 0), ac("B", 20, 0)]
    cps = [{"ID": "C1", GEOM: (10, 0)}, {"ID": "C2", GEOM: (20, 5)}, {"ID": "C3", GEOM: (99, 99)}]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "C1", LENGTH=10, UPSTREAM=5, DOWNSTREAM=4.9)
    p2 = pipe("P2", [(10, 0), (20, 0)], "C1", "B", LENGTH=10, UPSTREAM=4.9, DOWNSTREAM=4.8)
    p3 = pipe("P3", [(20, 5), (20, 0)], "C2", "B", LENGTH=5, UPSTREAM=4.9, DOWNSTREAM=4.8, MAINTYPE="IO- x")
    model, rep = build_model(acs, cps, [p1, p2, p3])
    n = by_id(model["nodes"], "node_id")
    assert n["C1"]["node_type"] == "junction" and n["C1"]["notes"].startswith("connection point")
    assert n["C2"]["node_type"] == "io_end" and "C3" not in n
    assert rep.counts["connection_points_used"] == 2 and rep.counts["connection_points_skipped"] == 1
    assert n["C1"]["invert_level"] == 4.9
    assert by_id(model["conduits"], "conduit_id")["P3"]["sewer_type"] == "io"


def test_connection_point_snapped_by_geometry():
    acs = [ac("A", 0, 0)]
    cps = [{"ID": "C1", GEOM: (10, 0)}]
    p = pipe("P", [(0, 0), (10.1, 0)], "A", None, LENGTH=10)
    model, rep = build_model(acs, cps, [p])
    assert model["conduits"][0]["ds_node_id"] == "C1" and rep.counts["connection_points_used"] == 1


def test_grade_source_field_derives_missing_ds_invert():
    acs = [ac("A", 0, 0), ac("B", 30, 0)]
    p = pipe("P", [(0, 0), (30, 0)], "A", "B", LENGTH=30, UPSTREAM=5.0, DOWNSTREAM=None, FLOW_GRADE=100)
    model, rep = build_model(acs, [], [p], options=ImportOptions(grade_source="field"))
    c = model["conduits"][0]
    assert c["ds_invert"] == pytest.approx(4.7) and c["data_source"] == "assumed"
    assert "derived" in c["notes"] and "invert_derived" in codes_of(rep)
    n = by_id(model["nodes"], "node_id")
    assert n["B"]["invert_level"] == pytest.approx(4.7)
    # levels source leaves it missing and warns
    model, rep = build_model(acs, [], [p])
    assert "ds_invert" not in model["conduits"][0] and "missing_invert" in codes_of(rep)
    assert "invert_level" not in by_id(model["nodes"], "node_id")["B"]


def test_unknown_codes_listed_with_counts():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    rows = [pipe("P1", [(0, 0), (10, 0)], "A", "B", STATUS="X- weird", MATERIAL="ZZZ- odd", MAINTYPE="Q- ?"),
            pipe("P2", [(0, 0), (10, 0)], "A", "B", STATUS="X- weird", MATERIAL="CI- cast iron", MAINTYPE="IS- is"),
            pipe("P3", [(0, 0), (10, 0)], "A", "B", MATERIAL=None, MAINTYPE="IO")]
    model, rep = build_model(acs, [], rows)
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["status"] == "existing" and c["P1"]["material"] == "other" and c["P1"]["sewer_type"] == "reticulation"
    assert c["P2"]["material"] == "other" and c["P2"]["sewer_type"] == "is" and c["P3"]["sewer_type"] == "io"
    assert rep.unknown_codes == {"STATUS": {"X": 2}, "MATERIAL": {"ZZZ": 1, "(blank)": 1}, "MAINTYPE": {"Q": 1}}
    text = "\n".join(rep.summary_lines())
    assert "Unknown STATUS codes: 'X' x2" in text


def test_material_map_and_install_year():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    mats = {"PVC": "pvc", "UPVC": "pvc", "VC": "vc", "AC": "ac_pipe", "DICL": "di", "CI": "other", "HDPE": "pe",
            "PP": "pe", "GRP": "grp", "RCP": "rcpl", "CONC": "rcpl"}
    rows = [pipe(f"P{i}", [(0, 0), (10, 0)], "A", "B", MATERIAL=f"{k}- desc", DATEINST="1975-02-03")
            for i, k in enumerate(mats)]
    model, rep = build_model(acs, [], rows)
    assert [c["material"] for c in model["conduits"]] == list(mats.values())
    assert model["conduits"][0]["install_year"] == 1975 and not rep.unknown_codes


def test_skips_bad_rows_and_duplicates():
    acs = [ac("A", 0, 0), ac("A", 1, 1), {"PACID": "", GEOM: (1, 1)}, {"PACID": "N", GEOM: None}, ac("B", 10, 0)]
    rows = [pipe("P", [(0, 0), (10, 0)], "A", "B"), pipe("P", [(0, 0), (10, 0)], "A", "B"),
            pipe("Q", [(0, 0)], "A", "B"), pipe("", [(0, 0), (1, 1)], "A", "B")]
    model, rep = build_model(acs, [], rows)
    assert len(model["nodes"]) == 2 and len(model["conduits"]) == 1
    assert rep.counts["access_chambers_skipped"] == 3 and rep.counts["conduits_skipped"] == 3


def test_model_is_json_and_accepted_by_native(tmp_path):
    native = pytest.importorskip("wwnet_native")
    if not hasattr(native, "write_model"):
        pytest.skip("wwnet_native too old (no write_model)")
    acs, pipes = sample_rows()
    cps = [{"ID": "C1", GEOM: (50, 50)}]
    for p in pipes:
        p["HEIGHT"] = 150
    model, rep = build_model(acs, cps, pipes, FieldMap(diameter="HEIGHT"))
    out = tmp_path / "imp.gpkg"
    native.write_model(str(out), imp.model_json(model))
    con = sqlite3.connect(out)
    assert con.execute("select count(*) from wwn_nodes").fetchone()[0] == 4
    assert con.execute("select material, dn from wwn_conduits order by conduit_id").fetchall() == [("pvc", 150), ("vc", 150)]
    con.close()
    assert isinstance(native.check(str(out), False), list)
    json.loads(imp.model_json(model))


def test_native_write_model_errors(tmp_path):
    native = pytest.importorskip("wwnet_native")
    if not hasattr(native, "write_model"):
        pytest.skip("wwnet_native too old (no write_model)")
    out = str(tmp_path / "w.gpkg")
    with pytest.raises(ValueError, match="invalid model JSON"):
        native.write_model(out, '{"nodes":[{"node_id":"A","node_type":"nope"}]}')
    with pytest.raises(ValueError):
        native.write_model(out, "not json")
    assert not (tmp_path / "w.gpkg").exists()
    native.write_model(out, '{"nodes":[{"node_id":"A","geom":[1,2]}]}')
    with pytest.raises(RuntimeError, match="already exists"):
        native.write_model(out, "{}")
    native.write_model(out, '{"nodes":[{"node_id":"B"}]}', overwrite=True)
    c = sqlite3.connect(out)
    assert c.execute("select node_id from wwn_nodes").fetchall() == [("B",)]
    c.close()


# ------------------------------------------------------------------ segmented runs (real-data shape)

def chamber(pid, x, y, ctype, top=20.0):
    return {"PACID": pid, "CHAMBER_TY": ctype, "TOPLEV": top, GEOM: (x, y)}


def seg(pid, line, us, ds, ui, di, **kw):
    return pipe(pid, line, us, ds, UPSTREAM=ui, DOWNSTREAM=di, LENGTH=None, FLOW_GRADE=None, **kw)


def segmented_world():
    """A -> S1 -> J1 -> S2 -> J2 -> S3 -> B (one run A->B in three segments); branch run X -> J1 (X is a
    maintenance shaft); an inspection-shaft end feeding J2. S2 and BR1 are digitised against the flow."""
    acs = [chamber("A", 0, 0, "M- standard manhole"), chamber("B", 300, 0, "M- standard manhole", 15.0),
           chamber("X", 100, 80, "J- maintenance shaft"),
           {"PACID": None, "CHAMBER_TY": "I- inspection shaft", "TOPLEV": 19.0, GEOM: (200, 60)}]
    sew = [seg("S1", [(0, 0), (100, 0)], "A", "B", 10.0, 9.7),
           seg("S2", [(200, 0), (100, 0)], "A", "B", 9.7, 9.4),
           seg("S3", [(200, 0), (300, 0)], "A", "B", 9.4, 9.1),
           seg("BR1", [(100.2, 0), (100, 80)], "X", "B", 9.9, 9.75),
           seg("BR2", [(200, 60), (200, 0.3)], None, "B", 9.6, 9.45)]
    return acs, sew


def test_segmented_run_junctions_direction_and_inverts():
    acs, sew = segmented_world()
    model, rep = build_model(acs, [], sew)
    n, c = by_id(model["nodes"], "node_id"), by_id(model["conduits"], "conduit_id")
    assert not [k for k in n if k.startswith("X") and k != "X"]            # no per-pipe placeholders
    assert sorted(n) == ["A", "B", "E1", "J1", "J2", "X"]
    assert rep.counts["junction_nodes"] == 2 and rep.counts["dangling_ends"] == 1
    assert n["J1"]["notes"].startswith("segment junction") and n["E1"]["notes"].startswith("dangling end")
    assert n["X"]["node_type"] == "ms" and n["A"]["node_type"] == "ac"
    assert [(c[k]["us_node_id"], c[k]["ds_node_id"]) for k in ("S1", "S2", "S3", "BR1", "BR2")] == [
        ("A", "J1"), ("J1", "J2"), ("J2", "B"), ("X", "J1"), ("E1", "J2")]
    # S2 and BR1 were digitised against the flow and are reversed; the others are untouched
    assert c["S2"]["geom"][0] == [100, 0] and c["BR1"]["geom"][0] == [100, 80] and c["S1"]["geom"][0] == [0, 0]
    assert rep.counts["reversed"] == 2
    assert sorted(w.object_id for w in rep.warnings if w.code == "direction_reversed") == ["BR1", "S2"]
    # segment levels are used as is after orientation
    assert (c["S2"]["us_invert"], c["S2"]["ds_invert"]) == (9.7, 9.4)
    # node invert: lowest outgoing us invert, else lowest incoming ds invert
    assert n["A"]["invert_level"] == 10.0 and n["J1"]["invert_level"] == 9.7 and n["J2"]["invert_level"] == 9.4
    assert n["X"]["invert_level"] == 9.9 and n["B"]["invert_level"] == 9.1 and n["E1"]["invert_level"] == 9.6
    assert "incoming" in n["B"]["notes"] and "outgoing" in n["J1"]["notes"]
    # the inspection shaft is skipped silently (counted), ids are not required for it
    codes = codes_of(rep)
    assert "ac_no_id" not in codes and rep.counts["inspection_shafts_skipped"] == 1
    assert "skipped 1 inspection shaft(s)" in "\n".join(rep.summary_lines()).lower()
    assert "id_mismatch" not in codes and "end_far_from_node" not in codes
    assert codes.count("direction_unknown") == 0      # BR2: no usable id, but the dead-end rule orients it
    assert rep.counts["direction_dead_end"] == 1 and "dead-end rule" in c["BR2"]["notes"]


def test_segmented_run_passes_model_check(tmp_path):
    native = pytest.importorskip("wwnet_native")
    if not hasattr(native, "write_model"):
        pytest.skip("wwnet_native too old (no write_model)")
    acs, sew = segmented_world()
    model, _ = build_model(acs, [], sew)
    out = tmp_path / "seg.gpkg"
    native.write_model(str(out), imp.model_json(model))
    issues = native.check(str(out), False)
    assert [i for i in issues if i["severity"] == "error"] == [], issues
    got = {i["code"] for i in issues}
    assert not got & {"INVERT_MISMATCH", "BIFURCATION", "GEOM_ENDPOINT_MISMATCH", "ORPHAN_NODE"}, issues


def test_chamber_type_codes():
    acs = [chamber("M1", 0, 0, "M- standard manhole"), chamber("J1x", 10, 0, "J- maintenance shaft"),
           chamber("I1", 20, 0, "I- inspection shaft"), chamber("Q1", 30, 0, "Q- strange"),
           {"PACID": "", "CHAMBER_TY": "I- inspection shaft", GEOM: (40, 0)},
           {"PACID": "", "CHAMBER_TY": "M- standard manhole", GEOM: (50, 0)}]
    model, rep = build_model(acs, [], [])
    n = by_id(model["nodes"], "node_id")
    assert sorted(n) == ["J1x", "M1", "Q1"]
    assert (n["M1"]["node_type"], n["J1x"]["node_type"], n["Q1"]["node_type"]) == ("ac", "ms", "ac")
    assert rep.counts["inspection_shafts_skipped"] == 2
    assert rep.unknown_codes == {"CHAMBER_TY": {"Q": 1}}
    assert codes_of(rep).count("unknown_code") == 1 and codes_of(rep).count("ac_no_id") == 1
    # field role unmapped: everything is an access chamber, no warnings about types
    model, rep = build_model(acs[:4], [], [], FieldMap(chamber_type=""))
    assert {x["node_type"] for x in model["nodes"]} == {"ac"} and "unknown_code" not in codes_of(rep)
    assert FieldMap.from_config().chamber_type == "CHAMBER_TY"


def test_id_mismatch_warned_once_per_segment():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    p = seg("P", [(0, 0), (10, 0)], "A", "C", 5.0, 4.9)      # ends at A and B; B is neither US nor DS id
    model, rep = build_model(acs, [], [p])
    mm = [w for w in rep.warnings if w.code == "id_mismatch"]
    assert len(mm) == 1 and mm[0].object_id == "P"
    c = model["conduits"][0]
    assert (c["us_node_id"], c["ds_node_id"]) == ("A", "B")        # geometry wins


def test_direction_from_neighbours_and_unknown():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    # middle segment has no ids at all, digitised against the flow; neighbours give the hint
    sew = [seg("P1", [(0, 0), (50, 0)], "A", "B", 5.0, 4.5), seg("P3", [(75, 0), (100, 0)], "A", "B", 4.2, 4.0),
           seg("P2", [(75, 0), (50, 0)], None, None, 4.5, 4.2)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert c["P2"]["geom"][0] == [50, 0] and "direction from neighbouring" in c["P2"]["notes"]
    assert "direction_unknown" not in codes_of(rep) and rep.counts["reversed"] == 1
    # isolated segment without ids: digitised direction kept with a warning
    model, rep = build_model([], [], [seg("Z", [(0, 0), (10, 0)], None, None, 5.0, 4.0)])
    assert model["conduits"][0]["geom"][0] == [0, 0] and "direction_unknown" in codes_of(rep)
    assert "reversed" not in rep.counts


def test_maintype_codes_e_is_branch_and_main():
    """Water Corporation MAINTYPE: E = inspection shaft (IS) sewer, B = branch, M = main, R = retic."""
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    rows = [pipe("P1", [(0, 0), (10, 0)], "A", "B", MAINTYPE="B- branch"),
            pipe("P2", [(10, 0), (20, 0)], "B", None, MAINTYPE="M- main"),
            # IS sewer: no upstream chamber, discharges into B
            seg("S1", [(10, 12), (10, 0)], None, "B", 21.0, 19.5, MAINTYPE="E- inspection shaft")]
    model, rep = build_model(acs, [], rows)
    c = by_id(model["conduits"], "conduit_id")
    assert (c["P1"]["sewer_type"], c["P2"]["sewer_type"], c["S1"]["sewer_type"]) == ("reticulation", "main", "is")
    assert not rep.unknown_codes and not rep.assumed_codes
    up = by_id(model["nodes"], "node_id")[c["S1"]["us_node_id"]]
    assert up["node_type"] == "is_end" and "inspection shaft" in up["notes"]
    assert c["S1"]["ds_node_id"] == "B"


def test_warning_summary_spread_across_codes():
    rep = imp.ImportReport()
    for i in range(28):
        rep.warn("end_far_from_node", "conduit", f"P{i}", "far")
    for i in range(3):
        rep.warn("grade_mismatch", "conduit", f"G{i}", "grade")
    rep.warn("dn_not_mapped", "import", "", "no dn")
    lines = rep.summary_lines(max_warnings=40)
    text = "\n".join(lines)
    assert "32 warning(s): dn_not_mapped x1, end_far_from_node x28, grade_mismatch x3" in text
    ex = rep.example_warnings(40)
    assert len(ex) == 5 + 3 + 1 and [w.code for w in ex].count("end_far_from_node") == 5
    assert ex[0].code == "dn_not_mapped"
    assert "... 23 more warning(s)" in text
    many = imp.ImportReport()
    for k in range(12):
        for i in range(9):
            many.warn(f"code{k}", "conduit", str(i), "m")
    assert len(many.example_warnings(40)) == 40
    assert max(sum(w.code == f"code{k}" for w in many.example_warnings(40)) for k in range(12)) <= 5


def test_dead_end_rule_orients_from_dangling_end():
    acs = [ac("A", 0, 0)]
    # no ids; the end at chamber A is attached, the other end dangles -> the dangling end is upstream
    sew = [seg("P1", [(0, 0), (30, 0)], None, None, 5.0, 4.5)]
    model, rep = build_model(acs, [], sew)
    c = model["conduits"][0]
    assert c["ds_node_id"] == "A" and c["geom"][0] == [30, 0] and "direction_unknown" not in codes_of(rep)
    assert rep.counts["direction_dead_end"] == 1 and "dead-end rule" in c["notes"]
    assert rep.counts["dangling_dead_end"] == 1 and rep.counts["dangling_other"] == 0
    assert any("0 IS/IO upstream ends (expected), 1 dead-end upstream starts, 0 other" in x
               for x in rep.summary_lines(0))
    # propagates through a junction: P2 joins P1's dangling-free end via a shared junction and is oriented
    sew = [seg("Q1", [(0, 0), (30, 0)], None, None, 5.0, 4.5), seg("Q2", [(30, 0), (60, 0)], None, None, 5.5, 5.0)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert c["Q1"]["ds_node_id"] == "A" and c["Q2"]["ds_node_id"] == c["Q1"]["us_node_id"]
    assert "direction_unknown" not in codes_of(rep)


def test_is_sewer_blank_us_orientation_and_breakdown():
    acs = [ac("B", 10, 0)]
    # IS sewer digitised downstream-first, blank US id, DS chamber named only by geometry (DS id blank too)
    sew = [seg("S1", [(10, 0), (10, 12)], None, None, 21.0, 19.5, MAINTYPE="E- inspection shaft")]
    model, rep = build_model(acs, [], sew)
    c = model["conduits"][0]
    assert c["ds_node_id"] == "B" and c["geom"][0] == [10, 12]
    assert rep.counts["direction_dead_end"] + rep.counts["direction_is_io"] == 1
    assert rep.counts["dangling_is_io"] == 1 and rep.counts["dangling_other"] == 0
    # IS sewer with both ends dangling is not oriented by the rules; IS rule needs exactly one chamber end
    model, rep = build_model([], [], [seg("S2", [(0, 0), (5, 0)], None, None, 2.0, 1.0, MAINTYPE="IS")])
    assert "direction_unknown" in codes_of(rep) and rep.counts["direction_unknown"] == 1


def test_dangling_other_listed_with_coordinates():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    # P1 ends at A (id says A is upstream) and dangles downstream at (50, 0): suspicious
    sew = [seg("P1", [(0, 0), (50, 0)], "A", None, 5.0, 4.5), seg("P2", [(70, 0), (100, 0)], "A", "B", 4.4, 4.0)]
    model, rep = build_model(acs, [], sew)
    d = [w for w in rep.warnings if w.code == "dangling_end"]
    assert rep.counts["dangling_other"] >= 1 and d and any(w.object_id == "P1" and "50.00, 0.00" in w.message
                                                           for w in d)
    many = [seg(f"M{i}", [(1000 + 10 * i, 0), (1005 + 10 * i, 0)], "A", "B", 5.0, 4.0) for i in range(30)]
    _m, rep = build_model(acs, [], many)
    assert len([w for w in rep.warnings if w.code == "dangling_end"]) <= 20
    assert rep.counts["dangling_other"] >= 20


def test_grade_ratio_summary_line():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    # levels grade = length/drop = 100/4 = 25 vs FLOW_GRADE 20 -> ratio 1.25; geometry length 90 -> 22.5/20
    sew = [pipe("P1", [(0, 0), (90, 0)], "A", "B", UPSTREAM=10.0, DOWNSTREAM=6.0, LENGTH=100.0, FLOW_GRADE=20,
                Shape_Length=80.0)]
    model, rep = build_model(acs, [], sew)
    line = next(x for x in rep.summary_lines(0) if x.startswith("Grade check"))
    assert "1 sewer(s)" in line and "LENGTH: 1.25" in line and "geometry length: 1.125" in line
    assert "Shape_Length: 1" in line and "Levels grade is used" in line
    # the grade used is still the levels: no derived inverts / no change of levels
    assert model["conduits"][0]["us_invert"] == 10.0 and model["conduits"][0]["ds_invert"] == 6.0
    _m, rep = build_model(acs, [], [pipe("P2", [(0, 0), (90, 0)], "A", "B", FLOW_GRADE=None)])
    assert not any(x.startswith("Grade check") for x in rep.summary_lines(0))


def test_context_chambers_counted_and_unused_dropped():
    rows = [ac("A", 0, 0), dict(ac("C1", 50, 0), **{imp.CONTEXT: True}), dict(ac("C2", 500, 500), **{imp.CONTEXT: True})]
    sew = [seg("P1", [(0, 0), (50, 0)], "A", "C1", 5.0, 4.5)]
    model, rep = build_model(rows, [], sew)
    assert sorted(n["node_id"] for n in model["nodes"]) == ["A", "C1"]
    assert rep.counts["access_chambers"] == 1 and rep.counts["context_chambers"] == 1
    assert rep.counts["context_chambers_unused"] == 1
    assert "1 context chamber(s)" in rep.summary_lines(0)[0]


def test_grade_ratio_ignores_shape_length_in_degrees():
    """Real data: GDA94 geographic layers give Shape_Length in degrees (13.99 m pipe -> 0.000142)."""
    acs = [ac("A", 0, 0), ac("B", 14, 0)]
    sew = [pipe("565141650", [(0, 0), (14, 0)], "A", "B", UPSTREAM=31.0, DOWNSTREAM=30.72, LENGTH=13.99,
                FLOW_GRADE=44.64, Shape_Length=0.000142)]
    _m, rep = build_model(acs, [], sew)
    line = next(x for x in rep.summary_lines(0) if x.startswith("Grade check"))
    assert "LENGTH: 1.119" in line and "Shape_Length ignored (source layer in degrees)" in line


# ---------------------------------------------------------------- T-junction (tee) snapping

def tseg(pid, line, us, ds, ui, di, **kw):
    kw.setdefault("LENGTH", None)
    return pipe(pid, line, us, ds, UPSTREAM=ui, DOWNSTREAM=di, FLOW_GRADE=None, **kw)


def tee_world(branch_line, branch_levels=(21.0, 20.5), tee_at=40.0, off=0.3):
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", tee_at, 20, top=30.0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    line = branch_line(tee_at, off)
    branch = tseg("BR", line, None, None, *branch_levels, LENGTH=19.7)
    return acs, [main, branch]


def _check_clean(model, tmp_path):
    native = pytest.importorskip("wwnet_native")
    if not hasattr(native, "write_model"):
        pytest.skip("wwnet_native too old (no write_model)")
    out = tmp_path / "tee.gpkg"
    native.write_model(str(out), imp.model_json(model))
    issues = native.check(str(out), False)
    assert [i for i in issues if i["severity"] in ("error", "warning")] == [], issues


@pytest.mark.parametrize("reverse", [False, True])
def test_tee_splits_main_and_orients_branch(reverse, tmp_path):
    def bl(x, off):
        line = [(x, 20), (x, off)]            # digitised from the chamber to the tee, or the other way
        return line[::-1] if reverse else line
    acs, sew = tee_world(bl)
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert sorted(c) == ["BR", "M", "M~2"]
    assert c["M"]["length_m"] == pytest.approx(40) and c["M~2"]["length_m"] == pytest.approx(60)
    assert c["M"]["us_node_id"] == "A" and c["M"]["ds_node_id"] == c["M~2"]["us_node_id"] == "T1"
    assert c["M~2"]["ds_node_id"] == "B" and c["M"]["asset_id"] == c["M~2"]["asset_id"] == "M"
    assert "split at tee for BR" in c["M"]["notes"]
    assert (c["M"]["us_invert"], c["M"]["ds_invert"]) == (20.0, pytest.approx(19.6))
    assert (c["M~2"]["us_invert"], c["M~2"]["ds_invert"]) == (pytest.approx(19.6), 19.0)
    t = n["T1"]
    assert t["node_type"] == "junction" and t["notes"].startswith("tee (mid-line junction)")
    assert t["invert_level"] == pytest.approx(19.6) and t["geom"] == [40.0, 0.0]
    assert c["BR"]["ds_node_id"] == "T1" and c["BR"]["us_node_id"] == "C"
    assert c["BR"]["us_invert"] == 21.0 and c["BR"]["ds_invert"] == 20.5
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 1
    assert rep.counts.get("dangling_ends", 0) == 0 and rep.counts["dangling_other"] == 0
    assert rep.counts["direction_tee"] == 1 and rep.counts["conduits"] == 3
    assert "tee_joined" in codes_of(rep) and "tee_adverse" not in codes_of(rep)
    assert "direction_unknown" not in codes_of(rep)
    assert any(x.startswith("Tees: 1 branch end(s) joined mid-line to 1 sewer(s)") for x in rep.summary_lines(0))
    _check_clean(model, tmp_path)


def test_tee_adverse_branch_invert_warns():
    acs, sew = tee_world(lambda x, off: [(x, 20), (x, off)], branch_levels=(19.5, 19.0))
    _m, rep = build_model(acs, [], sew)
    assert "tee_adverse" in codes_of(rep)


def test_two_tees_on_one_main_and_reversed_main(tmp_path):
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 25, 20, top=30.0), ac("D", 70, -20, top=30.0)]
    main = tseg("M", [(100, 0), (0, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)    # digitised against flow
    sew = [main, tseg("B1", [(25, 20), (25, 0.2)], None, None, 22.0, 21.0, LENGTH=20.0),
           tseg("B2", [(70, -0.1), (70, -20)], None, None, 21.0, 20.0, LENGTH=20.0)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["B1", "B2", "M", "M~2", "M~3"] and rep.counts["tee_junctions"] == 2
    # the upstream piece keeps the id; orientation is from A (us) to B (ds)
    assert c["M"]["us_node_id"] == "A" and c["M~3"]["ds_node_id"] == "B"
    assert [c[k]["length_m"] for k in ("M", "M~2", "M~3")] == pytest.approx([25, 45, 30])
    assert c["M"]["ds_invert"] == pytest.approx(19.75) and c["M~2"]["ds_invert"] == pytest.approx(19.3)
    assert c["B1"]["ds_node_id"] == c["M"]["ds_node_id"] and c["B2"]["us_node_id"] == "D"
    assert c["B2"]["ds_node_id"] == c["M~2"]["ds_node_id"]
    assert rep.counts["tee_mains"] == 1 and rep.counts["direction_ids"] == 1
    assert any("Tees: 2 branch end(s) joined mid-line to 1 sewer(s)" in x for x in rep.summary_lines(0))
    assert rep.counts["reversed"] == 2          # the main and B2
    _check_clean(model, tmp_path)


def test_tee_near_endpoint_is_endpoint_case_and_far_end_stays_dangling():
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 0.2, 20)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0)
    # within snap tolerance of the main's end A: connects to chamber A, no split
    model, rep = build_model(acs, [], [main, tseg("BR", [(0.2, 20), (0.3, 0.2)], None, None, 21.0, 20.5)])
    assert rep.counts.get("tee_junctions", 0) == 0 and len(model["conduits"]) == 2
    assert by_id(model["conduits"], "conduit_id")["BR"]["ds_node_id"] == "A"
    # 0.8 m off the line (tee tolerance 0.5): stays dangling
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 40, 20)]
    model, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 0.8)], None, None, 21.0, 20.5)])
    assert rep.counts.get("tee_junctions", 0) == 0 and len(model["conduits"]) == 2
    assert rep.counts["dangling_ends"] == 1
    _m, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 0.8)], None, None, 21.0, 20.5)],
                          options=ImportOptions(tee_tolerance_m=1.0))
    assert rep.counts["tee_junctions"] == 1 and rep.counts.get("dangling_ends", 0) == 0
    _m, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 0.3)], None, None, 21.0, 20.5)],
                          options=ImportOptions(tee_tolerance_m=0))
    assert rep.counts.get("tee_junctions", 0) == 0
    assert ImportOptions.from_config(imp.load_template()).tee_tolerance_m == 0.5


def test_tee_performance_5000_segments():
    import random
    import time
    rnd = random.Random(7)
    sew = []
    for i in range(5000):
        x, y = rnd.uniform(0, 3000), rnd.uniform(0, 3000)
        a = rnd.uniform(0, 6.28)
        sew.append(seg(f"S{i}", [(x, y), (x + 40 * math.cos(a), y + 40 * math.sin(a))], None, None, 5.0, 4.0))
    t0 = time.time()
    model, rep = build_model([], [], sew)
    assert time.time() - t0 < 5.0
    assert len(model["conduits"]) >= 5000


def test_is_sewer_tee_downstream_free_upstream_is_end(tmp_path):
    """IS sewer, blank ids, upstream end free, downstream end 0.3 m off a main's mid-line (no chamber)."""
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    for line in ([(60, 10), (60, 0.3)], [(60, 0.3), (60, 10)]):
        is_sew = tseg("IS1", line, None, None, 20.5, 19.8, MAINTYPE="E- inspection shaft")
        model, rep = build_model(acs, [], [main, is_sew])
        c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
        assert c["IS1"]["ds_node_id"] == "T1" and n[c["IS1"]["us_node_id"]]["node_type"] == "is_end"
        assert rep.counts["direction_tee"] == 1 and rep.counts["dangling_is_io"] == 1
        assert rep.counts["dangling_other"] == 0 and "direction_unknown" not in codes_of(rep)
        assert c["M"]["length_m"] == pytest.approx(60) and c["M~2"]["length_m"] == pytest.approx(40)
