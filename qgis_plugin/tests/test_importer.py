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


def note_codes(rep):
    """Codes of the info-severity rows (TEE_JOINED, END_SNAPPED): kept apart from the warnings."""
    return [w.code for w in rep.notes]


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
    # both sample sewers disagree with FLOW_GRADE by more than 10% (1 in 100 vs 120, 1 in 172 vs 150) but the
    # fall differs by only 2 mm / 9 mm: not listed per sewer, counted in one summary line
    assert "GRADE_MISMATCH" not in codes_of(rep) and rep.counts["grade_rounding"] == 2
    assert any("2 sewer(s) have a levels / FLOW_GRADE grade difference > 10 % but < 50 mm of fall (rounding); "
               "not listed" in x for x in rep.diagnostics)
    _, loose = build_model(acs, [], pipes, FieldMap(diameter="HEIGHT"), ImportOptions(grade_tolerance_pct=20))
    assert not [w for w in loose.warnings if w.code == "GRADE_MISMATCH"]
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
    assert c["P3"]["dn"] == 150 and "DN_ASSUMED" in codes_of(rep)
    assert sum(w.code == "DN_ASSUMED" for w in rep.warnings) == 2
    assert any("P1" in i for i in rep.infos)
    # field not mapped at all: loud single warning with the count
    model, rep = build_model(acs, [], rows, FieldMap())
    assert all(x["dn"] == 150 for x in model["conduits"])
    w = [w for w in rep.warnings if w.code == "DN_NOT_MAPPED"]
    assert len(w) == 1 and "5 conduit" in w[0].message and "ASSUMED" in w[0].message
    assert rep.counts["dn_assumed"] == 5


def test_direction_reversal():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    p = pipe("P", [(10, 0), (0, 0)], "A", "B", LENGTH=10, UPSTREAM=5, DOWNSTREAM=4)
    model, rep = build_model(acs, [], [p])
    c = model["conduits"][0]
    assert c["geom"] == [[0, 0], [10, 0]] and rep.counts["reversed"] == 1
    assert "direction_reversed" not in codes_of(rep) and "geometry reversed" in c["notes"]
    assert any(x.startswith("Reversed digitising direction of 1") for x in rep.summary_lines(0))
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
    msgs = " ".join(str(w) for w in rep.notes)
    assert "blank" in msgs and "unmatched" in msgs and note_codes(rep).count("END_SNAPPED") == 2
    assert "END_SNAPPED" not in codes_of(rep) and {i["severity"] for i in rep.issues() if i["code"] == "END_SNAPPED"} == {"info"}
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
    assert "estimated" in c["notes"] and "INVERT_ESTIMATED" in codes_of(rep)
    n = by_id(model["nodes"], "node_id")
    assert n["B"]["invert_level"] == pytest.approx(4.7)
    # without FLOW_GRADE (and no neighbours) the DS 50 minimum grade is assumed (DN150: 1 in 200)
    p = pipe("P", [(0, 0), (30, 0)], "A", "B", LENGTH=30, UPSTREAM=5.0, DOWNSTREAM=None, FLOW_GRADE=None)
    model, rep = build_model(acs, [], [p])
    assert model["conduits"][0]["ds_invert"] == pytest.approx(4.85)
    assert "assumed DS 50 minimum grade 1 in 200" in [w for w in rep.warnings if w.code == "INVERT_ESTIMATED"][0].message


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
    assert sorted(k for k in c if "geometry reversed" in (c[k].get("notes") or "")) == ["BR1", "S2"]
    # segment levels are used as is after orientation
    assert (c["S2"]["us_invert"], c["S2"]["ds_invert"]) == (9.7, 9.4)
    # node invert: lowest outgoing us invert, else lowest incoming ds invert
    assert n["A"]["invert_level"] == 10.0 and n["J1"]["invert_level"] == 9.7 and n["J2"]["invert_level"] == 9.4
    assert n["X"]["invert_level"] == 9.9 and n["B"]["invert_level"] == 9.1 and n["E1"]["invert_level"] == 9.6
    assert "incoming" in n["B"]["notes"] and "outgoing" in n["J1"]["notes"]
    # the inspection shaft is skipped silently (counted), ids are not required for it
    codes = codes_of(rep)
    assert "AC_NO_ID" not in codes and rep.counts["inspection_shafts_skipped"] == 1
    assert "skipped 1 chamber(s) of a type mapped to skip" in "\n".join(rep.summary_lines()).lower()
    assert "ID_MISMATCH" not in codes and "END_FAR_FROM_NODE" not in codes
    assert codes.count("DIRECTION_UNKNOWN") == 0      # BR2: no usable id, but the dead-end rule orients it
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
    assert codes_of(rep).count("UNKNOWN_CODE") == 1 and codes_of(rep).count("AC_NO_ID") == 1
    # field role unmapped: everything is an access chamber, no warnings about types
    model, rep = build_model(acs[:4], [], [], FieldMap(chamber_type=""))
    assert {x["node_type"] for x in model["nodes"]} == {"ac"} and "UNKNOWN_CODE" not in codes_of(rep)
    assert FieldMap.from_config().chamber_type == "CHAMBER_TY"


def test_id_mismatch_warned_once_per_segment():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    p = seg("P", [(0, 0), (10, 0)], "A", "C", 5.0, 4.9)      # ends at A and B; B is neither US nor DS id
    model, rep = build_model(acs, [], [p])
    mm = [w for w in rep.warnings if w.code == "ID_MISMATCH"]
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
    assert "DIRECTION_UNKNOWN" not in codes_of(rep) and rep.counts["reversed"] == 1
    # isolated segment without ids: digitised direction kept with a warning
    model, rep = build_model([], [], [seg("Z", [(0, 0), (10, 0)], None, None, 5.0, 4.0)])
    assert model["conduits"][0]["geom"][0] == [0, 0] and "DIRECTION_UNKNOWN" in codes_of(rep)
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
        rep.warn("END_FAR_FROM_NODE", "conduit", f"P{i}", "far")
    for i in range(3):
        rep.warn("GRADE_MISMATCH", "conduit", f"G{i}", "grade")
    rep.warn("DN_NOT_MAPPED", "import", "", "no dn")
    lines = rep.summary_lines(max_warnings=40)
    text = "\n".join(lines)
    assert "32 warning(s): DN_NOT_MAPPED x1, END_FAR_FROM_NODE x28, GRADE_MISMATCH x3" in text
    ex = rep.example_warnings(40)
    assert len(ex) == 5 + 3 + 1 and [w.code for w in ex].count("END_FAR_FROM_NODE") == 5
    assert ex[0].code == "DN_NOT_MAPPED"
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
    assert c["ds_node_id"] == "A" and c["geom"][0] == [30, 0] and "DIRECTION_UNKNOWN" not in codes_of(rep)
    assert rep.counts["direction_dead_end"] == 1 and "dead-end rule" in c["notes"]
    assert rep.counts["dangling_dead_end"] == 1 and rep.counts["dangling_other"] == 0
    assert any("0 IS/IO upstream ends (expected), 1 dead-end upstream starts, 0 other" in x
               for x in rep.summary_lines(0))
    # propagates through a junction: P2 joins P1's dangling-free end via a shared junction and is oriented
    sew = [seg("Q1", [(0, 0), (30, 0)], None, None, 5.0, 4.5), seg("Q2", [(30, 0), (60, 0)], None, None, 5.5, 5.0)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert c["Q1"]["ds_node_id"] == "A" and c["Q2"]["ds_node_id"] == c["Q1"]["us_node_id"]
    assert "DIRECTION_UNKNOWN" not in codes_of(rep)


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
    assert "DIRECTION_UNKNOWN" in codes_of(rep) and rep.counts["direction_unknown"] == 1


def test_dangling_other_listed_with_coordinates():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    # P1 ends at A (id says A is upstream) and dangles downstream at (50, 0): suspicious
    sew = [seg("P1", [(0, 0), (50, 0)], "A", None, 5.0, 4.5), seg("P2", [(70, 0), (100, 0)], "A", "B", 4.4, 4.0)]
    model, rep = build_model(acs, [], sew)
    d = [w for w in rep.warnings if w.code == "DANGLING_END"]
    assert rep.counts["dangling_other"] >= 1 and d and any(w.object_id == "P1" and "50.00, 0.00" in w.message
                                                           for w in d)
    many = [seg(f"M{i}", [(1000 + 10 * i, 0), (1005 + 10 * i, 0)], "A", "B", 5.0, 4.0) for i in range(30)]
    _m, rep = build_model(acs, [], many)
    assert len([w for w in rep.warnings if w.code == "DANGLING_END"]) <= 20
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
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", tee_at, 20, top=30.0)]
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
    assert "TEE_JOINED" in note_codes(rep) and "TEE_ADVERSE" not in codes_of(rep)
    assert "TEE_JOINED" not in codes_of(rep) and {i["severity"] for i in rep.issues() if i["code"] == "TEE_JOINED"} == {"info"}
    assert "DIRECTION_UNKNOWN" not in codes_of(rep)
    assert any(x.startswith("Tees: 1 branch end(s) joined mid-line to 1 sewer(s)") for x in rep.summary_lines(0))
    _check_clean(model, tmp_path)


def test_tee_adverse_branch_invert_warns():
    acs, sew = tee_world(lambda x, off: [(x, 20), (x, off)], branch_levels=(19.5, 19.0))
    _m, rep = build_model(acs, [], sew)
    assert "TEE_ADVERSE" in codes_of(rep)


def test_two_tees_on_one_main_and_reversed_main(tmp_path):
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", 25, 20, top=30.0), ac("D", 70, -20, top=30.0)]
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
        assert rep.counts["dangling_other"] == 0 and "DIRECTION_UNKNOWN" not in codes_of(rep)
        assert c["M"]["length_m"] == pytest.approx(60) and c["M~2"]["length_m"] == pytest.approx(40)


def test_is_sewers_not_imported_when_option_off():
    """import_is_sewers=False: IS stubs (a free end) are dropped before any joining, so the main is not split."""
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    is1 = tseg("IS1", [(60, 10), (60, 0.3)], None, None, 20.5, 19.8, MAINTYPE="E- inspection shaft")
    is2 = tseg("IS2", [(30, 10), (30, 0.2)], None, None, 20.5, 19.8, MAINTYPE="IS")
    model, rep = build_model(acs, [], [main, is1, is2], options=ImportOptions(import_is_sewers=False))
    assert [c["conduit_id"] for c in model["conduits"]] == ["M"]
    assert sorted(n["node_id"] for n in model["nodes"]) == ["A", "B"]
    assert rep.counts["is_sewers_skipped"] == 2 and rep.counts["conduits_read"] == 1
    assert "is_sewers_kept" not in rep.counts
    lines = rep.summary_lines()
    assert any("Skipped 2 IS (inspection shaft) stub sewer(s) with a free end: not imported "
               "(option 'Import IS sewers' off)." in line for line in lines)
    assert not any(line.startswith("Kept") for line in lines)
    model, rep = build_model(acs, [], [main, is1, is2])                       # default: imported (unchanged)
    assert len(model["conduits"]) == 5 and "is_sewers_skipped" not in rep.counts


OFF = ImportOptions(import_is_sewers=False)


def test_is_pipe_between_chambers_kept_when_option_off():
    acs = [ac("A", 0, 0), ac("B", 50, 0), ac("C", 100, 0)]
    retic = tseg("R1", [(0, 0), (50, 0)], "A", "B", 20.0, 19.5)
    isp = tseg("IS1", [(50, 0), (100, 0)], "B", "C", 19.5, 19.0, MAINTYPE="E- inspection shaft")
    model, rep = build_model(acs, [], [retic, isp], options=OFF)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["IS1", "R1"]
    assert c["IS1"]["us_node_id"] == "B" and c["IS1"]["ds_node_id"] == "C"
    assert rep.counts["is_sewers_kept"] == 1 and "is_sewers_skipped" not in rep.counts
    assert rep.is_kept_ids == ["IS1"]
    assert any("Kept 1 IS-coded sewer(s) connected at both ends (they carry flow): IS1" in x
               for x in rep.summary_lines())


def test_is_pipe_teeing_into_main_kept_when_option_off():
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 40, 20)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    isp = tseg("IS1", [(40, 20), (40, 0.2)], "C", None, 21.0, 20.5, MAINTYPE="IS")
    model, rep = build_model(acs, [], [main, isp], options=OFF)
    # kept because both ends connect, and its downstream end is teed into the main (not left as a free end)
    assert sorted(c["conduit_id"] for c in model["conduits"]) == ["IS1", "M", "M~2"]
    c = by_id(model["conduits"], "conduit_id")
    assert c["IS1"]["us_node_id"] == "C" and c["IS1"]["ds_node_id"] == c["M"]["ds_node_id"] == c["M~2"]["us_node_id"]
    assert "is_sewers_skipped" not in rep.counts
    assert rep.counts["is_sewers_kept"] == 1 and rep.is_kept_ids == ["IS1"]


def test_is_stub_chain_dropped_from_free_end_inwards():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    outer = tseg("OUT", [(60, 20), (60, 10)], None, None, 21.0, 20.8, MAINTYPE="IS")
    inner = tseg("INN", [(60, 10), (60, 0.2)], None, None, 20.8, 20.5, MAINTYPE="IS")
    model, rep = build_model(acs, [], [main, outer, inner], options=OFF)
    assert [c["conduit_id"] for c in model["conduits"]] == ["M"]
    assert rep.counts["is_sewers_skipped"] == 2 and "is_sewers_kept" not in rep.counts


def test_is_stub_near_but_beyond_snap_of_another_stub_dropped():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    s1 = tseg("S1", [(60, 20), (60, 0.2)], None, None, 21.0, 20.5, MAINTYPE="IS")
    # free end 0.4 m from S1's free end, tolerance is 0.1 m: not connected, both dropped (S2's downstream is free too)
    s2 = tseg("S2", [(60.4, 20), (60.4, 40)], None, None, 21.0, 20.5, MAINTYPE="IS")
    model, rep = build_model(acs, [], [main, s1, s2], options=OFF)
    assert [c["conduit_id"] for c in model["conduits"]] == ["M"]
    assert rep.counts["is_sewers_skipped"] == 2


def test_is_prune_is_fast_on_large_grid():
    import time
    segs, acs = [], []
    for i in range(5600):
        x, y = (i % 80) * 20.0, (i // 80) * 20.0
        segs.append(tseg(f"R{i}", [(x, y), (x + 10, y)], None, None, 20.0, 19.0))
    for i in range(1400):
        x, y = (i % 80) * 20.0 + 5, (i // 80) * 20.0 + 6
        segs.append(tseg(f"IS{i}", [(x, y), (x, y + 4.9)], None, None, 20.0, 19.0, MAINTYPE="IS"))
    prepared = [dict(s, line=s[GEOM], is_coded=s["MAINTYPE"] == "IS", pid=s["ID"]) for s in segs]
    t0 = time.perf_counter()
    out = imp._prune_is_stubs(prepared, imp._Grid(0.1), 0.1, 0.5, imp.ImportReport())
    dt = time.perf_counter() - t0
    assert len(out) == 5600 and dt < 2.0


def test_import_is_sewers_option_from_template():
    assert ImportOptions.from_config({"options": {"import_is_sewers": False}}).import_is_sewers is False
    assert ImportOptions.from_config({"options": {}}).import_is_sewers is True
    assert ImportOptions.from_config().import_is_sewers is True                # bundled template


# ---------------------------------------------------------------- surface interpolation

def test_surface_interpolated_at_segment_junction():
    acs = [ac("A", 0, 0, 20.0), ac("B", 80, 0, 12.0)]
    p1 = pipe("P1", [(0, 0), (50, 0)], "A", None, LENGTH=50, UPSTREAM=5, DOWNSTREAM=4)
    p2 = pipe("P2", [(50, 0), (80, 0)], None, "B", LENGTH=30, UPSTREAM=4, DOWNSTREAM=3)
    model, rep = build_model(acs, [], [p1, p2])
    n = by_id(model["nodes"], "node_id")
    assert n["J1"]["surface_level"] == pytest.approx(20.0 - 8.0 * 50 / 80, abs=1e-3)
    assert n["J1"]["notes"].startswith("segment junction")
    assert "+ surface interpolated between A and B" in n["J1"]["notes"]
    assert n["J1"]["data_source"] == "gis"
    assert rep.counts["surface_interpolated"] == 1
    assert any("Interpolated surface level at 1" in x for x in rep.summary_lines(0))


def test_surface_not_interpolated_with_one_chamber_or_dangling_end():
    acs = [ac("A", 0, 0, 20.0)]
    p1 = pipe("P1", [(0, 0), (50, 0)], "A", None, LENGTH=50, UPSTREAM=5, DOWNSTREAM=4)
    p2 = pipe("P2", [(50, 0), (80, 0)], None, None, LENGTH=30, UPSTREAM=4, DOWNSTREAM=3)
    model, rep = build_model(acs, [], [p1, p2])
    n = by_id(model["nodes"], "node_id")
    assert all("surface_level" not in n[k] for k in ("J1", "E1"))
    assert "surface_interpolated" not in rep.counts
    # a chamber without TOPLEV at one end: still no interpolation
    acs = [ac("A", 0, 0, 20.0), {"PACID": "B", GEOM: (80, 0)}]
    p2 = pipe("P2", [(50, 0), (80, 0)], None, "B", LENGTH=30, UPSTREAM=4, DOWNSTREAM=3)
    model, rep = build_model(acs, [], [p1, p2])
    assert "surface_level" not in by_id(model["nodes"], "node_id")["J1"]
    assert "surface_interpolated" not in rep.counts


def test_surface_interpolated_at_tee_on_split_main():
    acs = [ac("A", 0, 0, 20.0), ac("B", 100, 0, 10.0), ac("C", 40, 20, top=30.0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    br = tseg("BR", [(40, 20), (40, 0.3)], None, None, 21.0, 20.5, LENGTH=19.7)
    model, rep = build_model(acs, [], [main, br])
    n = by_id(model["nodes"], "node_id")
    assert n["T1"]["surface_level"] == pytest.approx(16.0, abs=1e-3)
    assert "+ surface interpolated between A and B" in n["T1"]["notes"]
    assert n["T1"]["notes"].startswith("tee (mid-line junction)")
    assert n["C"]["surface_level"] == 30.0 and rep.counts["surface_interpolated"] == 1


def test_surface_interpolated_two_tees_reversed_main():
    acs = [ac("A", 0, 0, 20.0), ac("B", 100, 0, 10.0), ac("C", 25, 20, 30.0), ac("D", 70, -20, 30.0)]
    main = tseg("M", [(100, 0), (0, 0)], "A", "B", 20.0, 19.0, LENGTH=100.0)
    sew = [main, tseg("B1", [(25, 20), (25, 0.2)], None, None, 22.0, 21.0, LENGTH=20.0),
           tseg("B2", [(70, -0.1), (70, -20)], None, None, 21.0, 20.0, LENGTH=20.0)]
    model, rep = build_model(acs, [], sew)
    n = by_id(model["nodes"], "node_id")
    levels = sorted(n[k]["surface_level"] for k in ("T1", "T2"))
    assert levels == pytest.approx([13.0, 17.5], abs=1e-3)
    assert rep.counts["surface_interpolated"] == 2


# ---------------------------------------------------------------- connection point loads

def cp(cid, x, y):
    return {"ID": cid, GEOM: (x, y)}


def two_pipe_rows():
    """A1 -> A2 -> A3 along y=0 (50 m each), flowing east."""
    acs = [ac("A1", 0, 0, 20.0), ac("A2", 50, 0, 19.0), ac("A3", 100, 0, 18.0)]
    pipes = [pipe("P1", [(0, 0), (50, 0)], "A1", "A2", UPSTREAM=19.5, DOWNSTREAM=18.6, LENGTH=50),
             pipe("P2", [(50, 0), (100, 0)], "A2", "A3", UPSTREAM=18.5, DOWNSTREAM=17.6, LENGTH=50)]
    return acs, pipes


def test_cp_loads_nearest_segment_upstream_node_and_aggregation():
    acs, pipes = two_pipe_rows()
    cps = [cp("C1", 10, 3), cp("C2", 40, -4), cp("C3", 75, 2), cp("C4", 60, 9.9)]
    model, rep = build_model(acs, cps, pipes, FieldMap(diameter="HEIGHT"))
    loads = by_id(model["node_loads"], "node_id")
    assert set(loads) == {"A1", "A2"}                      # P1 -> upstream A1, P2 -> upstream A2
    assert loads["A1"]["connections"] == 2 and loads["A1"]["load_id"] == "CP-A1"
    assert loads["A2"]["connections"] == 2 and loads["A2"]["source"] == "connection_points"
    assert "adwf_lps" not in loads["A1"] and "2 connection point" in loads["A1"]["notes"]
    assert rep.counts["cp_loads_assigned"] == 4 and rep.counts["cp_load_nodes"] == 2
    line = [x for x in rep.summary_lines() if x.startswith("Loads:")][0]
    assert line == ("Loads: 4 connection point(s) assigned to 2 node(s); 0 unassigned; "
                    "ADWF total 0.029 L/s at 630 L/d/connection")


def test_cp_radius_unassigned_and_example_cap():
    acs, pipes = two_pipe_rows()
    cps = [cp("N%d" % i, 20 + i, 30) for i in range(15)] + [cp("NEAR", 20, 10.0)]
    model, rep = build_model(acs, cps, pipes, FieldMap(diameter="HEIGHT"))
    assert rep.counts["cp_loads_unassigned"] == 15 and rep.counts["cp_loads_assigned"] == 1
    assert sum(w.code == "CP_NOT_ASSIGNED" for w in rep.warnings) == 10
    assert any("15 unassigned" in x for x in rep.summary_lines(0))
    # a wider radius picks them all up
    model, rep = build_model(acs, cps, pipes, FieldMap(diameter="HEIGHT"), ImportOptions(cp_load_search_m=40))
    assert rep.counts["cp_loads_unassigned"] == 0


def test_cp_loads_option_off_and_no_cps():
    acs, pipes = two_pipe_rows()
    model, rep = build_model(acs, [cp("C1", 10, 3)], pipes, FieldMap(diameter="HEIGHT"),
                             ImportOptions(load_connection_points=False))
    assert "node_loads" not in model
    model, rep = build_model(acs, [], pipes, FieldMap(diameter="HEIGHT"))
    assert "node_loads" not in model


def test_cp_used_as_io_end_still_counts_as_load_at_that_node():
    acs = [ac("A2", 20, 0, 19.0)]
    pipes = [pipe("IO1", [(0, 0), (20, 0)], None, "A2", MAINTYPE="IO", UPSTREAM=19.5, DOWNSTREAM=19.0, LENGTH=20)]
    model, rep = build_model(acs, [cp("C1", 0, 0)], pipes, FieldMap(diameter="HEIGHT"))
    n = [n for n in model["nodes"] if n["node_type"] == "io_end"]
    assert len(n) == 1
    loads = model["node_loads"]
    assert loads == [{"load_id": f"CP-{n[0]['node_id']}", "node_id": n[0]["node_id"], "connections": 1,
                      "source": "connection_points", "notes": loads[0]["notes"]}]


def test_cp_loads_roundtrip_to_native_design():
    native = pytest.importorskip("wwnet_native")
    acs, pipes = two_pipe_rows()
    model, rep = build_model(acs, [cp("C1", 10, 3)] * 0 + [cp("C%d" % i, 10, 3) for i in range(10)], pipes,
                             FieldMap(diameter="HEIGHT"))
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "m.gpkg")
        native.write_model(f, imp.model_json(model), False)
        out = native.design(f, None, False)
        adwf = out["conduit_results"][0]["adwf_lps"]
        assert abs(adwf - 10 * 630 / 86400) < 1e-9
        assert abs(out["conduit_results"][0]["gsdf_lps"] - adwf * 1.5) < 1e-9


def test_possible_missed_tee_diagnostic():
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 40, 20)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0)
    # 1.3 m off the main (> tee 0.5): stays dangling, reported as a possible missed tee
    model, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 1.3)], None, None, 21.0, 20.5)])
    assert rep.counts["possible_missed_tees"] == 1 and rep.counts.get("tee_junctions", 0) == 0
    lines = [x for x in rep.summary_lines(0) if "possible missed tee" in x]
    assert any("possible missed tee: E1 1.3 m from M" in x and "Join Dangling Sewer Ends" in x for x in lines), lines
    assert any(x.startswith("Possible missed tees: 1 lone sewer end(s) lie within 5 m") for x in rep.summary_lines(0))
    # joined by a larger tolerance: no diagnostic
    _m, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 1.3)], None, None, 21.0, 20.5)],
                          options=ImportOptions(tee_tolerance_m=2.0))
    assert "possible_missed_tees" not in rep.counts
    # 8 m away: not reported
    _m, rep = build_model(acs, [], [main, tseg("BR", [(40, 20), (40, 8.0)], None, None, 21.0, 20.5)])
    assert "possible_missed_tees" not in rep.counts


# ---------------------------------------------------------------- several branch ends at one point on a main

def _tee_main_world(extra_acs=(), main_line=((0, 0), (100, 0))):
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", 40, 20, top=30.0), ac("D", 40, -20, top=30.0),
           *extra_acs]
    return acs, tseg("M", list(main_line), "A", "B", 20.0, 19.0, LENGTH=100.0)


def _no_dangling(rep):
    assert rep.counts.get("dangling_ends", 0) == 0 and rep.counts.get("dangling_other", 0) == 0
    assert "DIRECTION_UNKNOWN" not in codes_of(rep)


@pytest.mark.parametrize("rev1,rev2,rev_main", [(False, False, False), (True, False, False), (False, True, True),
                                                (True, True, True)])
def test_two_branches_opposite_sides_split_main(rev1, rev2, rev_main, tmp_path):
    ml = [(100, 0), (0, 0)] if rev_main else [(0, 0), (100, 0)]
    acs, main = _tee_main_world(main_line=ml)
    l1, l2 = [(40, 20), (40, 0.2)], [(40, -20), (40, -0.2)]
    sew = [main, tseg("B1", l1[::-1] if rev1 else l1, None, None, 21.0, 20.5, LENGTH=20.0),
           tseg("B2", l2[::-1] if rev2 else l2, None, None, 21.0, 20.5, LENGTH=20.0)]
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert sorted(c) == ["B1", "B2", "M", "M~2"] and sorted(n) == ["A", "B", "C", "D", "T1"]
    assert c["M"]["us_node_id"] == "A" and c["M"]["ds_node_id"] == c["M~2"]["us_node_id"] == "T1"
    assert c["M~2"]["ds_node_id"] == "B"
    assert (c["B1"]["us_node_id"], c["B1"]["ds_node_id"]) == ("C", "T1")
    assert (c["B2"]["us_node_id"], c["B2"]["ds_node_id"]) == ("D", "T1")
    assert [c[k]["length_m"] for k in ("M", "M~2")] == pytest.approx([40, 60])
    assert c["M"]["ds_invert"] == pytest.approx(19.6) and c["M~2"]["us_invert"] == pytest.approx(19.6)
    assert (c["B1"]["us_invert"], c["B1"]["ds_invert"]) == (21.0, 20.5)      # branch inverts kept
    assert n["T1"]["notes"].startswith("tee (mid-line junction)") and n["T1"]["invert_level"] == pytest.approx(19.6)
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 2
    assert rep.counts.get("junction_nodes", 0) == 0 and rep.counts["direction_tee"] == 2
    assert note_codes(rep).count("TEE_JOINED") == 2 and "TEE_ADVERSE" not in codes_of(rep)
    _no_dangling(rep)
    assert any(x.startswith("Tees: 2 branch end(s) joined mid-line to 1 sewer(s)") for x in rep.summary_lines(0))
    assert "0 segment junction(s)" in rep.summary_lines(0)[0]
    _check_clean(model, tmp_path)


def test_two_branches_same_side_and_three_branches():
    acs, main = _tee_main_world()
    sew = [main, tseg("B1", [(40, 20), (40, 0.2)], None, None, 21.0, 20.5),
           tseg("B2", [(40.3, 20), (40.3, 0.2)], None, None, 21.0, 20.5)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["B1", "B2", "M", "M~2"] and c["B1"]["ds_node_id"] == c["B2"]["ds_node_id"] == "T1"
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 2
    _no_dangling(rep)
    acs, main = _tee_main_world()
    sew = [main, tseg("B1", [(40, 20), (40, 0.2)], None, None, 21.0, 20.5),
           tseg("B2", [(40, -20), (40, -0.2)], None, None, 21.0, 20.5),
           tseg("B3", [(40.3, 20), (40.3, 0.2)], None, None, 21.0, 20.5)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["B1", "B2", "B3", "M", "M~2"]
    assert {c[k]["ds_node_id"] for k in ("B1", "B2", "B3")} == {"T1"}
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 3 and rep.counts["conduits"] == 5
    assert rep.counts["direction_tee"] == 3
    _no_dangling(rep)


def test_two_branches_on_segmented_main_tee_mid_segment():
    acs, _ = _tee_main_world()
    segs = [tseg("M1", [(0, 0), (30, 0)], "A", "B", 20.0, 19.7), tseg("M2", [(30, 0), (60, 0)], "A", "B", 19.7, 19.4),
            tseg("M3", [(60, 0), (100, 0)], "A", "B", 19.4, 19.0)]
    sew = segs + [tseg("B1", [(40, 20), (40, 0.2)], None, None, 21.0, 20.5),
                  tseg("B2", [(40, -20), (40, -0.2)], None, None, 21.0, 20.5)]
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert sorted(c) == ["B1", "B2", "M1", "M2", "M2~2", "M3"]
    assert c["M2"]["ds_node_id"] == c["M2~2"]["us_node_id"] == "T1" and c["M2"]["us_node_id"] == "J1"
    assert c["B1"]["ds_node_id"] == c["B2"]["ds_node_id"] == "T1"
    assert c["M2"]["ds_invert"] == pytest.approx(19.6)              # 19.7 -> 19.4 over 30 m, 10 m in
    assert n["T1"]["geom"] == [40.0, 0.0] and "J1" in n and "J2" in n
    assert rep.counts["tee_junctions"] == 1 and rep.counts["junction_nodes"] == 2
    _no_dangling(rep)


def test_segment_joint_with_third_branch_end_stays_a_junction():
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", 30, 20, top=30.0)]
    segs = [tseg("M1", [(0, 0), (30, 0)], "A", "B", 20.0, 19.7), tseg("M2", [(30, 0), (100, 0)], "A", "B", 19.7, 19.0),
            tseg("BR", [(30.2, 20), (30.2, 0.3)], "C", None, 21.0, 20.5)]
    model, rep = build_model(acs, [], segs)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert sorted(c) == ["BR", "M1", "M2"] and sorted(n) == ["A", "B", "C", "J1"]
    assert c["M1"]["ds_node_id"] == c["M2"]["us_node_id"] == c["BR"]["ds_node_id"] == "J1"
    assert rep.counts["junction_nodes"] == 1 and rep.counts.get("tee_junctions", 0) == 0
    assert rep.counts.get("dangling_ends", 0) == 0 and "inflow_only_junctions" not in rep.counts
    # a plain two-segment joint too
    model, rep = build_model(acs, [], segs[:2])
    assert rep.counts["junction_nodes"] == 1 and rep.counts.get("tee_junctions", 0) == 0


def test_two_ends_of_a_run_meeting_on_a_main_are_joined_to_it(tmp_path):
    """Decision: a cluster on another sewer's interior is a junction fitting whatever its members are; two
    segments of a branch run that meet on a main are both joined to it (a crossing without a connection
    cannot be told from two branches by geometry)."""
    acs, main = _tee_main_world()
    sew = [main, tseg("R1", [(40, 20), (40, 0.1)], "C", None, 21.0, 20.5),
           tseg("R2", [(40, -0.1), (40, -20)], None, "D", 20.5, 21.0)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 2
    assert c["R1"]["ds_node_id"] == "T1" and "T1" in {c["R2"]["us_node_id"], c["R2"]["ds_node_id"]}
    assert "M~2" in c


def test_is_sewer_member_of_cluster_stays_free_while_branch_joins_main():
    acs, main = _tee_main_world()
    # IS1 runs from chamber D to the cluster point: its end there is the free upstream end by design
    is_sew = tseg("IS1", [(40, -20), (40, -0.2)], None, None, 20.5, 20.0, MAINTYPE="E- inspection shaft")
    sew = [main, tseg("B1", [(40, 20), (40, 0.2)], None, None, 21.0, 20.5), is_sew]
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert c["B1"]["ds_node_id"] == "T1" and rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 1
    assert c["IS1"]["ds_node_id"] == "D"
    us = n[c["IS1"]["us_node_id"]]
    assert us["node_type"] == "is_end" and us["node_id"] not in ("T1", "J1")
    assert rep.counts["dangling_is_io"] == 1 and rep.counts.get("junction_nodes", 0) == 0
    assert rep.counts["dangling_ends"] == 1 and rep.counts["dangling_other"] == 0


def test_end_beside_main_end_node_beyond_snap_joins_that_node():
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", 0.45, 20, top=30.0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0)
    # end 0.54 m from chamber A (> snap 0.5) but only 0.3 m off the main, projecting 0.45 m from A
    sew = [main, tseg("BR", [(0.45, 20), (0.45, 0.3)], "C", None, 21.0, 20.5)]
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["BR", "M"] and c["BR"]["ds_node_id"] == "A" and c["BR"]["us_node_id"] == "C"
    assert rep.counts.get("dangling_ends", 0) == 0 and rep.counts.get("tee_junctions", 0) == 0
    assert "END_FAR_FROM_NODE" in codes_of(rep) and len(model["nodes"]) == 3
    # a lone end near the main's end node but beyond the tee tolerance stays dangling
    sew = [main, tseg("BR", [(0.45, 20), (0.45, 0.8)], "C", None, 21.0, 20.5)]
    _m, rep = build_model(acs, [], sew)
    assert rep.counts["dangling_ends"] == 1 and "END_FAR_FROM_NODE" not in codes_of(rep)


def test_inflow_only_cluster_is_reported_as_dangling_and_possible_missed_tee():
    acs = [ac("A", 0, 0, 25.0), ac("B", 100, 0, 25.0), ac("C", 40, 20, top=30.0), ac("D", 40, 30, top=30.0)]
    main = tseg("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0)
    # two sewers end together 2.0 m from the main (> tee 0.5, < 5 m): nothing flows out
    sew = [main, tseg("B1", [(40, 20), (40.2, 2.0)], "C", None, 21.0, 20.5),
           tseg("B2", [(39.8, 2.0), (40, 30)], None, "D", 20.5, 21.0)]
    sew[2] = tseg("B2", [(40, 30), (39.8, 2.0)], "D", None, 21.0, 20.5)
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert rep.counts.get("tee_junctions", 0) == 0 and rep.counts["junction_nodes"] == 1
    j = n[c["B1"]["ds_node_id"]]
    assert j["notes"].startswith("segment junction; inflow only") and j["node_id"] == "J1"
    assert rep.counts["dangling_ends"] == 1 and rep.counts["dangling_other"] == 2    # one per inflow-only sewer
    assert codes_of(rep).count("DANGLING_END") == 2
    lines = rep.summary_lines(0)
    assert "1 dangling end(s)" in lines[0]
    assert rep.counts["possible_missed_tees"] == 1
    assert any("possible missed tee: J1 2 m from M" in x for x in lines), lines


def test_inflow_only_junction_far_from_any_main_is_dangling_without_missed_tee():
    acs = [ac("C", 40, 20, top=30.0), ac("D", 40, 60, top=30.0)]
    sew = [tseg("B1", [(40, 20), (40, 39.9)], "C", None, 21.0, 20.5),
           tseg("B2", [(40, 60), (40, 40.1)], "D", None, 21.0, 20.5)]
    _m, rep = build_model(acs, [], sew)
    assert rep.counts["dangling_ends"] == 1 and rep.counts["dangling_other"] == 2
    assert "1 dangling end(s)" in rep.summary_lines(0)[0] and "possible_missed_tees" not in rep.counts


# ---------------------------------------------------------------- a run split where it crosses a main

def _crossing_world(us1, ds1, us2, ds2, inv_hi=21.0, inv_lo=20.5):
    acs, main = _tee_main_world()
    sew = [main, tseg("R1", [(40, 20), (40, 0.1)], us1, ds1, inv_hi, inv_hi - 0.25),
           tseg("R2", [(40, -0.1), (40, -20)], us2, ds2, inv_hi - 0.25, inv_lo - 0.5)]
    return acs, sew


def test_run_split_at_crossing_with_same_ids_stays_a_continuing_junction():
    acs, sew = _crossing_world("C", "D", "C", "D")
    model, rep = build_model(acs, [], sew)
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert sorted(c) == ["M", "R1", "R2"] and "M~2" not in c
    assert rep.counts.get("tee_junctions", 0) == 0 and rep.counts["junction_nodes"] == 1
    assert c["R1"]["us_node_id"] == "C" and c["R1"]["ds_node_id"] == c["R2"]["us_node_id"] == "J1"
    assert c["R2"]["ds_node_id"] == "D" and (c["M"]["us_node_id"], c["M"]["ds_node_id"]) == ("A", "B")
    assert "possible_crossing_tees" not in rep.counts and rep.counts.get("dangling_ends", 0) == 0


def test_blank_ids_crossing_far_from_main_invert_is_tee_with_diagnostic():
    acs, sew = _crossing_world(None, None, None, None, inv_hi=23.0, inv_lo=22.5)    # main invert ~19.6 at 40 m
    model, rep = build_model(acs, [], sew)
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 2
    assert rep.counts["possible_crossing_tees"] == 1
    assert any(x.startswith("possible crossing joined as a tee: J1 on M") for x in rep.diagnostics), rep.diagnostics
    # inverts near the main's: a junction fitting, no diagnostic
    acs, sew = _crossing_world(None, None, None, None, inv_hi=20.1, inv_lo=19.6)
    _m, rep = build_model(acs, [], sew)
    assert rep.counts["tee_junctions"] == 1 and "possible_crossing_tees" not in rep.counts


def test_pass_through_pair_stays_junction_while_genuine_branch_tees():
    acs, sew = _crossing_world("C", "D", "C", "D")
    acs.append(ac("E", 40.2, 25, top=30.0))
    sew.append(tseg("BR", [(40.2, 25), (40.2, 0.2)], "E", None, 21.0, 20.5))
    model, rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["BR", "M", "M~2", "R1", "R2"]
    assert c["BR"]["ds_node_id"] == "T1" and c["M"]["ds_node_id"] == "T1"
    assert c["R1"]["ds_node_id"] == c["R2"]["us_node_id"] == "J1" and "T1" not in (c["R1"]["ds_node_id"],)
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 1
    assert rep.counts["junction_nodes"] == 1


# ---------------------------------------------------------------------------------------------
# Import Sewer Data fixes from a real bulk import (placeholder levels, estimated inverts, status, codes, ids,
# false tees, severity of notes, GRADE_MISMATCH threshold)

def test_placeholder_levels_are_missing_not_levels():
    acs = [ac("A", 0, 0, -99.9), ac("B", 10, 0, 5.0), ac("C", 20, 0, -9996)]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=-99.9, DOWNSTREAM=4.0, FLOW_GRADE=None, LENGTH=10)
    p2 = pipe("P2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=-9998, DOWNSTREAM=-99.9, FLOW_GRADE=None, LENGTH=10)
    model, rep = build_model(acs, [], [p1, p2])
    c, n = by_id(model["conduits"], "conduit_id"), by_id(model["nodes"], "node_id")
    assert c["P1"]["us_invert"] == pytest.approx(4.05) and c["P1"]["ds_invert"] == 4.0   # us: DS 50 grade
    assert c["P2"]["us_invert"] == 4.0 and c["P2"]["ds_invert"] == pytest.approx(3.95)   # us from P1, ds DS 50 grade
    assert "surface_level" not in n["A"] and "surface_level" not in n["C"] and n["B"]["surface_level"] == 5.0
    assert n["A"]["invert_level"] == pytest.approx(4.05) and n["B"]["invert_level"] == 4.0 and n["C"]["invert_level"] == pytest.approx(3.95)
    assert not [w for w in rep.warnings if w.code in ("TEE_ADVERSE", "GRADE_MISMATCH")]
    for x in model["nodes"] + model["conduits"]:
        assert all(not isinstance(v, float) or v > -99 for v in x.values()), x
    assert rep.counts["placeholder_levels"] == 5 and any("placeholder level" in d for d in rep.diagnostics)
    assert imp.to_level(-99.0) is None and imp.to_level(-98.9) == -98.9 and imp.to_level("-9996") is None


def test_missing_invert_filled_from_grade_one_end():
    acs = [ac("A", 0, 0), ac("B", 32.1, 0)]
    p = pipe("P", [(0, 0), (32.1, 0)], "A", "B", LENGTH=32.1, UPSTREAM=19.0, DOWNSTREAM=-99.9, FLOW_GRADE=150)
    model, rep = build_model(acs, [], [p])
    c = model["conduits"][0]
    assert c["ds_invert"] == pytest.approx(19.0 - 32.1 / 150, abs=1e-3) and c["data_source"] == "assumed"
    w = [x for x in rep.warnings if x.code == "INVERT_ESTIMATED"]
    assert len(w) == 1 and w[0].object_id == "P"
    assert w[0].message == "ds invert 18.786 estimated from us invert and FLOW_GRADE 1 in 150 over 32.1 m"
    assert by_id(model["nodes"], "node_id")["B"]["invert_level"] == pytest.approx(18.786)
    # the other way round: downstream known, upstream = known + grade x length
    p = pipe("P", [(0, 0), (30, 0)], "A", "B", LENGTH=30, UPSTREAM=None, DOWNSTREAM=5.0, FLOW_GRADE=100)
    model, rep = build_model([ac("A", 0, 0), ac("B", 30, 0)], [], [p])
    assert model["conduits"][0]["us_invert"] == pytest.approx(5.3)
    assert "us invert 5.3 estimated from ds invert and FLOW_GRADE" in rep.warnings[-1].message or \
        any("us invert 5.3 estimated from ds invert and FLOW_GRADE" in x.message for x in rep.warnings)


def test_both_inverts_missing_node_side_then_grade():
    acs = [ac(k, 10 * i, 0) for i, k in enumerate("ABCD")]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=10.1, DOWNSTREAM=10.0, LENGTH=10)
    p2 = pipe("P2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100)
    p3 = pipe("P3", [(20, 0), (30, 0)], "C", "D", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100)
    model, rep = build_model(acs, [], [p1, p2, p3])
    c = by_id(model["conduits"], "conduit_id")
    assert (c["P2"]["us_invert"], c["P2"]["ds_invert"]) == (10.0, 9.9)           # us from P1, ds from grade
    assert (c["P3"]["us_invert"], c["P3"]["ds_invert"]) == (9.9, 9.8)            # iterated
    msgs = {w.object_id: w.message for w in rep.warnings if w.code == "INVERT_ESTIMATED"}
    assert set(msgs) == {"P2", "P3"}                                              # one warning per sewer
    assert "us invert 10 estimated from the incoming sewer P1 ds invert at node B" in msgs["P2"]
    assert "ds invert 9.9 estimated from us invert and FLOW_GRADE 1 in 100 over 10 m" in msgs["P2"]
    n = by_id(model["nodes"], "node_id")
    assert n["B"]["invert_level"] == 10.0 and n["C"]["invert_level"] == 9.9 and n["D"]["invert_level"] == 9.8
    assert rep.counts["inverts_estimated"] == 4 and rep.counts["inverts_estimated_grade"] == 2
    assert rep.counts["inverts_estimated_node"] == 2
    assert any(d.startswith("Estimated 4 missing invert(s) in 2 sewer(s)") for d in rep.diagnostics)
    # downstream end from the single outgoing sewer, lowest ds invert of several incoming for the upstream end
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0), ac("X", 10, 10)]
    q1 = pipe("Q1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=10.4, DOWNSTREAM=10.2, LENGTH=10)
    q2 = pipe("Q2", [(10, 10), (10, 0)], "X", "B", UPSTREAM=10.5, DOWNSTREAM=10.1, LENGTH=10)
    q3 = pipe("Q3", [(10, 0), (20, 0)], "B", "C", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=None)
    model, rep = build_model(acs, [], [q1, q2, q3])
    q3c = by_id(model["conduits"], "conduit_id")["Q3"]
    assert q3c["us_invert"] == 10.1 and q3c["ds_invert"] == pytest.approx(10.05)
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    r1 = pipe("R1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=None)
    r2 = pipe("R2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=7.0, DOWNSTREAM=6.9, LENGTH=10)
    model, rep = build_model(acs, [], [r1, r2])
    r1c = by_id(model["conduits"], "conduit_id")["R1"]
    assert r1c["ds_invert"] == 7.0 and r1c["us_invert"] == pytest.approx(7.05)    # ds from node, us DS 50 grade


def est_msgs(rep):
    return {w.object_id: w.message for w in rep.warnings if w.code == "INVERT_ESTIMATED"}


def test_ds_missing_from_outgoing_sewer_same_dn_and_obvert_lift():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=6.0, DOWNSTREAM=None, LENGTH=10, HEIGHT=150)
    p2 = pipe("P2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=5.0, DOWNSTREAM=4.9, LENGTH=10, HEIGHT=150)
    model, rep = build_model(acs, [], [p1, p2], FieldMap(diameter="HEIGHT"))
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["ds_invert"] == 5.0 and c["P1"]["data_source"] == "assumed"
    assert "ds invert 5 estimated from the outgoing sewer P2 us invert at node B" in est_msgs(rep)["P1"]
    p2["HEIGHT"] = 225
    model, rep = build_model(acs, [], [p1, p2], FieldMap(diameter="HEIGHT"))
    assert by_id(model["conduits"], "conduit_id")["P1"]["ds_invert"] == pytest.approx(5.075)   # DN150 into DN225
    assert "lifted 0.075 m" in est_msgs(rep)["P1"]
    p2["HEIGHT"] = 100
    model, rep = build_model(acs, [], [p1, p2], FieldMap(diameter="HEIGHT"))
    assert by_id(model["conduits"], "conduit_id")["P1"]["ds_invert"] == 5.0                    # reducing: invert match


def test_us_missing_from_incoming_min_obvert_matched():
    acs = [ac("A", 0, 0), ac("X", 10, 10), ac("B", 10, 0), ac("C", 20, 0)]
    q1 = pipe("Q1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=10.4, DOWNSTREAM=10.2, LENGTH=10, HEIGHT=150)
    q2 = pipe("Q2", [(10, 10), (10, 0)], "X", "B", UPSTREAM=10.5, DOWNSTREAM=10.15, LENGTH=10, HEIGHT=100)
    q3 = pipe("Q3", [(10, 0), (20, 0)], "B", "C", UPSTREAM=None, DOWNSTREAM=10.0, LENGTH=10, HEIGHT=225)
    model, rep = build_model(acs, [], [q1, q2, q3], FieldMap(diameter="HEIGHT"))
    # Q1: 10.2 - 0.075 = 10.125; Q2: 10.15 - 0.125 = 10.025 -> min
    assert by_id(model["conduits"], "conduit_id")["Q3"]["us_invert"] == pytest.approx(10.025)
    assert "lowest of 2 incoming sewers' ds inverts at node B" in est_msgs(rep)["Q3"]


def test_branch_ds_missing_at_tee_uses_main_invert_lifted():
    acs, sew = tee_world(lambda x, off: [(x, 20), (x, off)], branch_levels=(21.0, None))
    sew[0]["HEIGHT"] = 300
    sew[1]["HEIGHT"] = 150
    model, rep = build_model(acs, [], sew, FieldMap(diameter="HEIGHT"))
    c = by_id(model["conduits"], "conduit_id")
    assert c["M"]["ds_invert"] == pytest.approx(19.6) and c["M~2"]["us_invert"] == pytest.approx(19.6)
    assert c["BR"]["ds_invert"] == pytest.approx(19.75)                      # tee invert + (300 - 150) mm
    assert "main M invert at tee node" in est_msgs(rep)["BR"] and "M" not in est_msgs(rep)
    assert not [w for w in rep.warnings if w.code == "TEE_ADVERSE"]


def test_head_of_line_us_missing_falls_to_grade_then_ds50():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    p = pipe("P", [(0, 0), (10, 0)], "A", "B", UPSTREAM=None, DOWNSTREAM=5.0, LENGTH=10, FLOW_GRADE=100)
    model, rep = build_model(acs, [], [p], FieldMap(diameter="HEIGHT"))
    assert model["conduits"][0]["us_invert"] == pytest.approx(5.1)
    assert "us invert 5.1 estimated from ds invert and FLOW_GRADE 1 in 100 over 10 m" in est_msgs(rep)["P"]
    p = pipe("P", [(0, 0), (10, 0)], "A", "B", UPSTREAM=None, DOWNSTREAM=5.0, LENGTH=10, FLOW_GRADE=None, HEIGHT=225)
    model, rep = build_model(acs, [], [p], FieldMap(diameter="HEIGHT"))
    assert model["conduits"][0]["us_invert"] == pytest.approx(5.0 + 10 / 300)          # DN225: 1 in 300
    assert "assumed DS 50 minimum grade 1 in 300" in est_msgs(rep)["P"]
    assert rep.counts["inverts_estimated_ds50"] == 1


def test_node_side_value_adverse_is_rejected_for_grade():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0)]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=5.0, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100, HEIGHT=150)
    p2 = pipe("P2", [(10, 0), (20, 0)], "B", "C", UPSTREAM=5.4, DOWNSTREAM=5.3, LENGTH=10, HEIGHT=150)
    model, rep = build_model(acs, [], [p1, p2], FieldMap(diameter="HEIGHT"))
    assert by_id(model["conduits"], "conduit_id")["P1"]["ds_invert"] == pytest.approx(4.9)
    m = est_msgs(rep)["P1"]
    assert "FLOW_GRADE 1 in 100" in m and "node-side value 5.4 rejected (adverse)" in m


def test_cascade_along_run_of_missing_inverts():
    acs = [ac(k, 10 * i, 0) for i, k in enumerate("ABCDEF")]
    k1 = pipe("K1", [(0, 0), (10, 0)], "A", "B", UPSTREAM=10.2, DOWNSTREAM=10.0, LENGTH=10)
    k2 = pipe("K2", [(40, 0), (50, 0)], "E", "F", UPSTREAM=9.6, DOWNSTREAM=9.5, LENGTH=10)
    run = [pipe("P1", [(10, 0), (20, 0)], "B", "C", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100),
           pipe("P2", [(20, 0), (30, 0)], "C", "D", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100),
           pipe("P3", [(30, 0), (40, 0)], "D", "E", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100)]
    model, rep = build_model(acs, [], [k1] + run + [k2], FieldMap(diameter="HEIGHT"))
    c = by_id(model["conduits"], "conduit_id")
    assert (c["P1"]["us_invert"], c["P1"]["ds_invert"]) == (10.0, pytest.approx(9.9))
    assert c["P2"]["us_invert"] == pytest.approx(9.9) and c["P2"]["ds_invert"] == pytest.approx(9.7)
    assert c["P3"]["us_invert"] == pytest.approx(9.7) and c["P3"]["ds_invert"] == 9.6


def test_real_inverts_never_overwritten_and_nothing_to_estimate_from_is_skipped():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    p = pipe("P", [(0, 0), (10, 0)], "A", "B", UPSTREAM=5.0, DOWNSTREAM=5.5, LENGTH=10, FLOW_GRADE=100)   # adverse but real
    model, rep = build_model(acs, [], [p])
    c = model["conduits"][0]
    assert (c["us_invert"], c["ds_invert"]) == (5.0, 5.5) and "INVERT_ESTIMATED" not in codes_of(rep)
    lone = pipe("P", [(0, 0), (10, 0)], "A", "B", UPSTREAM=None, DOWNSTREAM=None, LENGTH=10, FLOW_GRADE=100)
    model, rep = build_model(acs, [], [lone])
    assert "us_invert" not in model["conduits"][0] and "INVERT_ESTIMATED" not in codes_of(rep)


def test_status_n_and_d_skipped_a_imported():
    acs = [ac("A", 0, 0), ac("B", 10, 0)]
    ok = pipe("P1", [(0, 0), (10, 0)], "A", "B", LENGTH=10)
    nu = pipe("P2", [(0, 0), (10, 0)], "A", "B", LENGTH=10, STATUS="N- not in use")
    dd = [pipe(f"D{i}", [(0, 0), (10, 0)], "A", "B", STATUS="D- dead") for i in range(2)]
    model, rep = build_model(acs, [], [ok, nu] + dd)
    assert [c["conduit_id"] for c in model["conduits"]] == ["P1"]
    assert rep.counts["status_skipped"] == 3 and rep.status_skipped == {"N": 1, "D": 2}
    assert "UNKNOWN_CODE" not in codes_of(rep) and not rep.unknown_codes
    assert not [w for w in rep.warnings if w.object_id in ("P2", "D0", "D1")]
    assert any("Skipped 3 sewer(s) by status (not imported): D x2, N x1." == x for x in rep.summary_lines(0))
    # the unknown-code fallback is unchanged
    _, rep = build_model(acs, [], [pipe("P1", [(0, 0), (10, 0)], "A", "B", STATUS="Q- odd")])
    assert rep.unknown_codes == {"STATUS": {"Q": 1}}


def test_new_codes_are_mapped_silently():
    acs = [chamber(k, 10 * i, 0, f"{k}- thing") for i, k in enumerate(["F", "OS", "O", "DT"])]
    acs = [dict(a, PACID=f"C{i}") for i, a in enumerate(acs)]
    rows = [pipe(f"P{i}", [(10 * i, 0), (10 * i + 10, 0)], f"C{i}", f"C{i + 1}", MAINTYPE=f"{t}- x", MATERIAL="SU- steel",
                 LENGTH=10, UPSTREAM=5 - i * .1, DOWNSTREAM=4.9 - i * .1)
            for i, t in enumerate(["C", "I", "O"])]
    rows.append(pipe("P3", [(30, 0), (40, 0)], "C3", None, MAINTYPE="OS- x", MATERIAL="SU- steel"))
    model, rep = build_model(acs, [], rows)
    assert {n["node_type"] for n in model["nodes"] if n["node_id"].startswith("C")} == {"ac"}
    assert len([n for n in model["nodes"] if n["node_id"].startswith("C")]) == 4
    assert {c["sewer_type"] for c in model["conduits"]} == {"reticulation"}
    assert {c["material"] for c in model["conduits"]} == {"other"}
    assert "UNKNOWN_CODE" not in codes_of(rep) and not rep.unknown_codes and not rep.assumed_codes


def test_deleted_manhole_and_pit_access_skipped_sewers_still_connect():
    acs = [chamber("A", 0, 0, "M- standard manhole"), chamber("DM1", 10, 0, "E- deleted manhole"),
           chamber("B", 20, 0, "M- standard manhole"), chamber("PX", 30, 0, "P- pit access"),
           {"PACID": "", "CHAMBER_TY": "E- deleted manhole", GEOM: (40, 0)}]
    p1 = pipe("P1", [(0, 0), (10, 0)], "A", "DM1", LENGTH=10, UPSTREAM=5.0, DOWNSTREAM=4.9)
    p2 = pipe("P2", [(10, 0), (20, 0)], "DM1", "B", LENGTH=10, UPSTREAM=4.9, DOWNSTREAM=4.8)
    p3 = pipe("P3", [(20, 0), (30, 0)], "B", "PX", LENGTH=10, UPSTREAM=4.8, DOWNSTREAM=4.7)
    model, rep = build_model(acs, [], [p1, p2, p3])
    n = by_id(model["nodes"], "node_id")
    assert "DM1" not in n and "PX" not in n and rep.counts["inspection_shafts_skipped"] == 3
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["ds_node_id"] == c["P2"]["us_node_id"] and c["P1"]["ds_node_id"] in n   # joined by geometry
    assert (c["P1"]["us_node_id"], c["P2"]["ds_node_id"], c["P3"]["us_node_id"]) == ("A", "B", "B")
    assert "AC_NO_ID" not in codes_of(rep) and "UNKNOWN_CODE" not in codes_of(rep)


def test_zero_pacid_and_zero_manhole_refs_are_no_id():
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac(0, 20, 0), ac("0000", 30, 0), ac(0.0, 40, 0), ac("0.0", 50, 0), ac("  ", 60, 0)]
    p = pipe("P", [(0, 0), (10, 0)], "0000", 0, LENGTH=10, UPSTREAM=5.0, DOWNSTREAM=4.9)
    model, rep = build_model(acs, [], [p])
    assert sorted(n["node_id"] for n in model["nodes"]) == ["A", "B"]
    assert codes_of(rep).count("AC_NO_ID") == 5 and "AC_DUPLICATE_ID" not in codes_of(rep)
    assert rep.counts["access_chambers_skipped"] == 5
    c = model["conduits"][0]
    assert (c["us_node_id"], c["ds_node_id"]) == ("A", "B")
    assert "ID_MISMATCH" not in codes_of(rep)
    assert [w.message.split(";")[0].split()[0] for w in rep.notes] == ["US", "DS"] and "blank" in rep.notes[0].message
    assert imp.to_ref_id("0000") == "" and imp.to_ref_id(0.0) == "" and imp.to_ref_id("0.0") == "" \
        and imp.to_ref_id("0A") == "0A" and imp.to_ref_id("10") == "10" and imp.to_ref_id(" M1 ") == "M1"


def test_false_tee_guard_stub_from_the_mains_own_manhole():
    acs = [ac("M5183", 0, 0), ac("M2", 100, 0)]
    main = pipe("MAIN", [(0, 0), (100, 0)], "M5183", "M2", LENGTH=100, UPSTREAM=10.0, DOWNSTREAM=9.0)
    stub = pipe("STUB", [(0, 0), (1.3, 0)], "M5183", None, LENGTH=1.3, UPSTREAM=None, DOWNSTREAM=None)
    model, rep = build_model(acs, [], [main, stub])
    assert rep.counts.get("tee_junctions", 0) == 0 and "TEE_JOINED" not in note_codes(rep)
    assert {c["conduit_id"] for c in model["conduits"]} == {"MAIN", "STUB"}          # main not split
    assert not any(n["node_id"].startswith("T") for n in model["nodes"])
    sus = [w for w in rep.warnings if w.code == "SUSPECT_TEE"]
    assert len(sus) == 1 and sus[0].object_id == "STUB" and "MAIN" in sus[0].message and "STUB" in sus[0].message
    # no cycle: the stub's free end stays a dangling end, flow M5183 side -> nowhere is not a loop
    c = by_id(model["conduits"], "conduit_id")
    assert c["MAIN"]["us_node_id"] == "M5183" and c["MAIN"]["ds_node_id"] == "M2"
    assert "STUB" not in rep.diagnostics and not any("possible missed tee" in d for d in rep.diagnostics)
    # a real branch from elsewhere still tees
    branch = pipe("BR", [(50, 30), (50, 0)], "X", None, LENGTH=30, UPSTREAM=12.0, DOWNSTREAM=9.4)
    model, rep = build_model(acs + [ac("X", 50, 30)], [], [main, branch])
    assert rep.counts["tee_junctions"] == 1 and "SUSPECT_TEE" not in codes_of(rep)


def test_directed_cycle_through_a_tee_is_reported():
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("D", 100, 50)]
    main = pipe("M", [(0, 0), (100, 0)], "A", "B", LENGTH=100, UPSTREAM=10.0, DOWNSTREAM=9.0)
    q = pipe("Q", [(100, 0), (100, 50)], "B", "D", LENGTH=50, UPSTREAM=9.0, DOWNSTREAM=8.5)
    br = pipe("BR", [(100, 50), (50, 0)], "D", None, LENGTH=70, UPSTREAM=8.5, DOWNSTREAM=9.5)
    model, rep = build_model(acs, [], [main, q, br])
    assert rep.counts["tee_junctions"] == 1
    sus = [w for w in rep.warnings if w.code == "SUSPECT_TEE"]
    assert len(sus) == 1 and sus[0].object_type == "node" and sus[0].object_id == "T1"
    assert "directed cycle through tee T1" in sus[0].message and "BR" in sus[0].message
    assert all(f" {k} " in f" {sus[0].message} " or k in sus[0].message for k in ("B", "D"))
    # a clean tee has no such warning
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 50, 30)]
    main = pipe("M", [(0, 0), (100, 0)], "A", "B", LENGTH=100, UPSTREAM=10.0, DOWNSTREAM=9.0)
    br = pipe("BR", [(50, 30), (50, 0)], "C", None, LENGTH=30, UPSTREAM=12.0, DOWNSTREAM=9.4)
    _, rep = build_model(acs, [], [main, br])
    assert rep.counts["tee_junctions"] == 1 and "SUSPECT_TEE" not in codes_of(rep)


def test_notes_are_info_severity_in_issues_and_not_warnings():
    rep = imp.ImportReport()
    rep.warn("TEE_JOINED", "conduit", "P1", "joined", severity="info")
    rep.warn("DN_ASSUMED", "conduit", "P2", "dn")
    assert [w.code for w in rep.warnings] == ["DN_ASSUMED"] and [w.code for w in rep.notes] == ["TEE_JOINED"]
    assert {i["code"]: i["severity"] for i in rep.issues()} == {"TEE_JOINED": "info", "DN_ASSUMED": "warning"}
    text = "\n".join(rep.summary_lines(40))
    assert "1 warning(s): DN_ASSUMED x1" in text and "1 note(s) (info, in the Issues layer): TEE_JOINED x1" in text
    assert "[TEE_JOINED]" not in text


def test_grade_mismatch_listed_only_above_fall_threshold():
    acs = [ac("A", 0, 0), ac("B", 100, 0), ac("C", 110, 0)]
    big = pipe("BIG", [(0, 0), (100, 0)], "A", "B", LENGTH=100, UPSTREAM=10.0, DOWNSTREAM=9.5, FLOW_GRADE=100)  # 0.5 m vs 1 m
    small = pipe("SMALL", [(100, 0), (110, 0)], "B", "C", LENGTH=10, UPSTREAM=9.5, DOWNSTREAM=9.36, FLOW_GRADE=100)  # 0.14 vs 0.1: 40 %, 0.04 m
    model, rep = build_model(acs, [], [big, small])
    gm = [w for w in rep.warnings if w.code == "GRADE_MISMATCH"]
    assert [w.object_id for w in gm] == ["BIG"] and "0.500 m" in gm[0].message
    assert rep.counts["grade_rounding"] == 1
    assert "1 sewer(s) have a levels / FLOW_GRADE grade difference > 10 % but < 50 mm of fall (rounding); not listed" \
        in rep.diagnostics


# ---------------------------------------------------------------- self-loops are never emitted

def _no_self_loops(model):
    return [c["conduit_id"] for c in model["conduits"] if c["us_node_id"] == c["ds_node_id"]]


def test_short_segment_between_free_ends_is_dropped_junction_keeps_others():
    # P1 and P2 meet a 0.3 m stub S whose two ends both cluster to the same junction (J114 shape)
    acs = [ac("A", 0, 0), ac("B", 200, 0), ac("C", 100, 50)]
    sew = [tseg("P1", [(0, 0), (100, 0)], "A", None, 10.0, 9.0),
           tseg("S", [(100, 0), (100.3, 0)], None, None, 9.0, 8.99),
           tseg("P2", [(100.3, 0), (200, 0)], None, "B", 9.0, 8.0),
           tseg("P3", [(100, 50), (100.2, 0.1)], "C", None, 10.0, 9.1)]
    model, rep = build_model(acs, [], sew)
    assert _no_self_loops(model) == []
    assert sorted(c["conduit_id"] for c in model["conduits"]) == ["P1", "P2", "P3"]
    assert rep.counts["self_loops_dropped"] == 1
    w = [w for w in rep.warnings if w.code == "SELF_LOOP"]
    assert len(w) == 1 and w[0].object_id == "S" and "both ends at node J1" in w[0].message \
        and "not imported" in w[0].message
    n = by_id(model["nodes"], "node_id")
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["ds_node_id"] == c["P2"]["us_node_id"] == c["P3"]["ds_node_id"] == "J1" and "J1" in n
    assert any("self-loop" in x for x in rep.summary_lines(0))


def test_orphan_junction_of_dropped_segment_is_removed():
    acs = [ac("A", 0, 0), ac("B", 50, 0)]
    sew = [tseg("P1", [(0, 0), (50, 0)], "A", "B", 10.0, 9.0),
           tseg("S", [(300, 300), (300.2, 300)], None, None, 9.0, 8.99)]
    model, rep = build_model(acs, [], sew)
    assert [c["conduit_id"] for c in model["conduits"]] == ["P1"]
    assert sorted(n["node_id"] for n in model["nodes"]) == ["A", "B"]
    assert rep.counts["self_loops_dropped"] == 1 and rep.counts.get("junction_nodes", 0) == 0


def test_both_ends_snapped_to_same_chamber_is_dropped():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    sew = [tseg("P1", [(0, 0), (100, 0)], "A", "B", 10.0, 9.0),
           tseg("LOOP", [(0, 0), (0, 5), (3, 5), (0.2, 0.1)], "A", "A", 10.0, 9.9),
           tseg("TINY", [(100, 0), (100.2, 0)], "B", "B", 9.0, 8.99)]
    model, rep = build_model(acs, [], sew)
    assert _no_self_loops(model) == [] and [c["conduit_id"] for c in model["conduits"]] == ["P1"]
    assert rep.counts["self_loops_dropped"] == 2
    assert sorted(w.object_id for w in rep.warnings if w.code == "SELF_LOOP") == ["LOOP", "TINY"]


def test_short_branch_with_both_ends_teed_at_one_point_is_dropped():
    acs = [ac("A", 0, 0), ac("B", 100, 0)]
    sew = [tseg("M", [(0, 0), (100, 0)], "A", "B", 10.0, 9.0),
           tseg("BR", [(50, 0.1), (50.2, 0.1)], None, None, 9.6, 9.59)]
    model, rep = build_model(acs, [], sew)
    assert _no_self_loops(model) == []
    assert "BR" not in {c["conduit_id"] for c in model["conduits"]}
    assert rep.counts["self_loops_dropped"] == 1
    c = by_id(model["conduits"], "conduit_id")
    assert c["M"]["us_node_id"] == "A" and c["M~2"]["ds_node_id"] == "B" and c["M"]["ds_node_id"] == c["M~2"]["us_node_id"]


def test_no_self_loop_for_ordinary_model():
    acs, sew = sample_rows()
    model, rep = build_model(acs, [], sew)
    assert rep.counts.get("self_loops_dropped", 0) == 0 and "SELF_LOOP" not in codes_of(rep)
    assert not any("self-loop" in x for x in rep.summary_lines(0))


# ---------------------------------------------------------------- orientation around segment junctions / tees

def _orient_world(branch_ids, s2_reversed=True, s3_reversed=False, tee_x=250.0):
    """Main A -> B in three GIS segments (S2, optionally S3, digitised against the flow). Branches BJ1 / BJ2
    end exactly at the segment joints (3-end clusters), BT mid-segment on S3 (a tee); all are digitised towards
    the main. branch_ids: (us, ds) carried by the branches ("blank" or "same" as the main)."""
    bus, bds = (None, None) if branch_ids == "blank" else ("A", "B")
    acs = [ac("A", 0, 0), ac("B", 300, 0)]
    s2 = [(200, 0), (100, 0)] if s2_reversed else [(100, 0), (200, 0)]
    s3 = [(300, 0), (200, 0)] if s3_reversed else [(200, 0), (300, 0)]
    sew = [seg("S1", [(0, 0), (100, 0)], "A", "B", 10.0, 9.7),
           seg("S2", s2, "A", "B", 9.7, 9.4),
           seg("S3", s3, "A", "B", 9.4, 9.1),
           seg("BJ1", [(100, 50), (100, 0)], bus, bds, 9.9, 9.75),
           seg("BJ2", [(200, -50), (200, 0)], bus, bds, 9.7, 9.5),
           seg("BT", [(tee_x, 40), (tee_x, 0)], bus, bds, 9.5, 9.3)]
    return acs, sew


@pytest.mark.parametrize("branch_ids", ["blank", "same"])
@pytest.mark.parametrize("s2_rev,s3_rev", [(False, False), (True, False), (True, True), (False, True)])
def test_orientation_main_flows_a_to_b_and_branches_flow_into_main(branch_ids, s2_rev, s3_rev):
    acs, sew = _orient_world(branch_ids, s2_rev, s3_rev)
    model, rep = build_model(acs, [], sew)
    assert _no_self_loops(model) == []
    cs = model["conduits"]
    main = [c for c in cs if c["asset_id"] in ("S1", "S2", "S3")]
    assert len(main) == 4          # S3 is split at the tee
    # every main piece flows towards B: walk from A, each piece's ds is the next piece's us
    here, seen = "A", []
    for _ in range(len(main)):
        nxt = [c for c in main if c["us_node_id"] == here and c["conduit_id"] not in seen]
        assert len(nxt) == 1, (here, [(c["conduit_id"], c["us_node_id"], c["ds_node_id"]) for c in main])
        seen.append(nxt[0]["conduit_id"])
        here = nxt[0]["ds_node_id"]
    assert here == "B"
    mnodes = {c["us_node_id"] for c in main} | {c["ds_node_id"] for c in main}
    for c in cs:
        if c["asset_id"].startswith("B"):
            assert c["ds_node_id"] in mnodes and c["us_node_id"] not in mnodes, (c["conduit_id"], c)
    assert "DIRECTION_UNKNOWN" not in codes_of(rep)


def _check_main_and_branches(model, main_ids=("S1", "S2", "S3"), branch_prefix=("BJ", "BT"), n_main=4):
    cs = model["conduits"]
    main = [c for c in cs if c["asset_id"] in main_ids]
    assert len(main) == n_main
    here, seen = "A", []
    for _ in range(len(main)):
        nxt = [c for c in main if c["us_node_id"] == here and c["conduit_id"] not in seen]
        assert len(nxt) == 1, (here, [(c["conduit_id"], c["us_node_id"], c["ds_node_id"]) for c in main])
        seen.append(nxt[0]["conduit_id"])
        here = nxt[0]["ds_node_id"]
    assert here == "B"
    mnodes = {c["us_node_id"] for c in main} | {c["ds_node_id"] for c in main}
    for c in cs:
        if c["asset_id"].startswith(branch_prefix):
            assert c["ds_node_id"] in mnodes and c["us_node_id"] not in mnodes, (c["conduit_id"], c)


def _orders(n):
    import random
    yield list(range(n))
    yield list(range(n))[::-1]
    for k in range(20):
        idx = list(range(n))
        random.Random(k).shuffle(idx)
        yield idx


@pytest.mark.parametrize("branch_ids", ["blank", "same"])
@pytest.mark.parametrize("s2_rev,s3_rev", [(False, False), (True, False), (True, True), (False, True)])
def test_orientation_is_independent_of_read_order(branch_ids, s2_rev, s3_rev):
    acs, sew = _orient_world(branch_ids, s2_rev, s3_rev)
    for order in _orders(len(sew)):
        model, rep = build_model(acs, [], [sew[i] for i in order])
        _check_main_and_branches(model)
        assert "DIRECTION_UNKNOWN" not in codes_of(rep), order


@pytest.mark.parametrize("branch_ids", ["blank", "same"])
def test_orientation_two_segment_branch_into_segment_joint_any_order(branch_ids):
    # the branch is itself digitised in two pieces (one against the flow), so its far end at the joint is
    # attached to another branch segment rather than free
    acs, sew = _orient_world(branch_ids)
    bus, bds = (None, None) if branch_ids == "blank" else ("A", "B")
    sew = [x for x in sew if x["ID"] != "BJ1"] + [
        seg("BL1", [(100, 50), (100, 100)], bus, bds, 9.95, 9.9),
        seg("BL2", [(100, 50), (100, 0)], bus, bds, 9.9, 9.75)]
    for order in _orders(len(sew)):
        model, rep = build_model(acs, [], [sew[i] for i in order])
        _check_main_and_branches(model)
        c = by_id(model["conduits"], "conduit_id")
        assert c["BL2"]["us_node_id"] == c["BL1"]["ds_node_id"] and c["BL2"]["ds_node_id"] == c["S1"]["ds_node_id"], order


def test_overflow_pipes_marked_in_notes_and_summary():
    assert imp.OVERFLOW_NOTE == "overflow pipe"
    acs = [ac("A", 0, 0), ac("B", 10, 0), ac("C", 20, 0), ac("D", 30, 0)]
    rows = [pipe("R1", [(0, 0), (10, 0)], "A", "B"),
            pipe("OS1", [(10, 0), (20, 0)], "B", "C", MAINTYPE="OS- overflow storage"),
            pipe("O1", [(20, 0), (30, 0)], "C", "D", MAINTYPE="O- overflow")]
    model, rep = build_model(acs, [], rows)
    c = by_id(model["conduits"], "conduit_id")
    assert "overflow pipe" not in (c["R1"]["notes"] or "")
    assert "overflow pipe (MAINTYPE OS)" in c["OS1"]["notes"] and "overflow pipe (MAINTYPE O)" in c["O1"]["notes"]
    assert c["OS1"]["sewer_type"] == "reticulation"
    assert rep.counts["overflow_pipes"] == 2
    assert any(l.startswith("2 overflow pipe(s) (MAINTYPE O / OS) marked: they carry flow only when pumps fail")
               for l in rep.summary_lines())


def test_split_overflow_pipe_pieces_all_carry_the_note():
    acs, sew = tee_world(lambda x, off: [(x, 20), (x, off)])
    sew[0]["MAINTYPE"] = "OS- overflow storage"
    model, _rep = build_model(acs, [], sew)
    c = by_id(model["conduits"], "conduit_id")
    assert sorted(c) == ["BR", "M", "M~2"]
    assert all("overflow pipe (MAINTYPE OS)" in c[k]["notes"] for k in ("M", "M~2"))
    assert "overflow pipe" not in (c["BR"]["notes"] or "")
