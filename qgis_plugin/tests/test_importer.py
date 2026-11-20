import datetime
import json
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
    assert fm.ac_id == "PACID" and fm.us_node == "US_MANHOLE_ID" and fm.diameter == ""
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
    assert "invert derived from connected pipes" in n["A2"]["notes"]
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
    assert rep.counts["placeholder_nodes"] == 2


def test_placeholder_nodes_and_sharing():
    acs = [ac("A", 0, 0)]
    p1 = pipe("P1", [(0, 0), (50, 0)], "A", None, LENGTH=50, UPSTREAM=5, DOWNSTREAM=4)
    p2 = pipe("P2", [(50.2, 0), (80, 0)], None, None, LENGTH=30, UPSTREAM=4, DOWNSTREAM=3)
    model, rep = build_model(acs, [], [p1, p2])
    n = by_id(model["nodes"], "node_id")
    assert "XP1_DS" in n and n["XP1_DS"]["node_type"] == "junction" and n["XP1_DS"]["data_source"] == "assumed"
    c = by_id(model["conduits"], "conduit_id")
    assert c["P1"]["ds_node_id"] == "XP1_DS" and c["P2"]["us_node_id"] == "XP1_DS"   # P2 snaps to the placeholder
    assert c["P2"]["ds_node_id"] == "XP2_DS"
    assert rep.counts["placeholder_nodes"] == 2 and rep.counts["ends_snapped"] == 1
    assert n["XP1_DS"]["invert_level"] == 4  # min(4.0 from P1 ds, 4 from P2 us)


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
