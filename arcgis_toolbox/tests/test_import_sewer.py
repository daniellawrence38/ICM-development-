import os
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import fake_arcpy as fa
from wwnet_arcgis import shared, sewer_import, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]
GEOM = "_geom"
GDA2020 = NS(factoryCode=7850, name="GDA2020 / MGA zone 50")
GDA94 = NS(factoryCode=28350, name="GDA 1994 MGA Zone 50")

AC_FIELDS = [("OBJECTID", "OID"), ("PACID", "String"), ("TOPLEV", "Double"), ("Shape", "Geometry")]
CP_FIELDS = [("OBJECTID", "OID"), ("ID", "String"), ("Shape", "Geometry")]
SEWER_FIELDS = [(n, "String") for n in (
    "ID", "STATUS", "MAINTYPE", "PURPOSE", "DOWNSTREAM", "UPSTREAM", "FLOW_GRADE", "LENGTH", "PLANSET",
    "MATERIAL", "DATEINST", "HEIGHT", "DS_MANHOLE_ID", "US_MANHOLE_ID")] + [("Shape", "Geometry")]


def sewer(pid, line, us, ds, **kw):
    r = {"ID": pid, "STATUS": "A- actual", "MAINTYPE": "R- retic", "PURPOSE": "L- local", "DOWNSTREAM": 17.450001,
         "UPSTREAM": 17.459999, "FLOW_GRADE": 120, "LENGTH": 1.000555, "PLANSET": "PS1",
         "MATERIAL": "PVC- polyvinyl chloride", "DATEINST": None, "HEIGHT": 150, "US_MANHOLE_ID": us,
         "DS_MANHOLE_ID": ds, GEOM: line}
    r.update(kw)
    return r


@pytest.fixture
def world(fake_arcpy):
    layers = []
    a = fake_arcpy(layers)
    a.sources["C:/d/ac"] = {"fields": AC_FIELDS, "sr": GDA2020, "rows": [
        {"PACID": "A1", "TOPLEV": 20.0, GEOM: (385000.0, 6460000.0)},
        {"PACID": "A2", "TOPLEV": 19.9, GEOM: (385001.000555, 6460000.0)},
        {"PACID": "A3", "TOPLEV": 5.0, GEOM: (385100.0, 6460000.0)},
        {"PACID": "A4", "TOPLEV": 4.0, GEOM: (385110.326591, 6460000.0)}]}
    a.sources["C:/d/cp"] = {"fields": CP_FIELDS, "sr": GDA2020, "rows": [
        {"ID": "C9", GEOM: (385500.0, 6460000.0)}]}
    a.sources["C:/d/sew"] = {"fields": SEWER_FIELDS, "sr": GDA2020, "rows": [
        # digitised against the flow: must be reversed
        sewer("22030208", [(385001.000555, 6460000.0), (385000.0, 6460000.0)], "A1", "A2"),
        sewer("22030223", [(385100.0, 6460000.0), (385110.326591, 6460000.0)], "A3", "A4",
              MAINTYPE="M- main", DOWNSTREAM=0.43, UPSTREAM=0.49, FLOW_GRADE=150, LENGTH=10.326591,
              MATERIAL="VC- vitrified clay", HEIGHT=225, DATEINST="1999-01-01")]}
    lyr = {k: fa.make_layer(k, f"C:/d/{k}") for k in ("ac", "cp", "sew")}
    return a, lyr, layers


def run(a, lyr, tmp_path, **kw):
    kw.setdefault("fields", {"diameter": "HEIGHT"})
    kw.setdefault("cp_layer", lyr["cp"])
    return tools_impl.import_sewer_data(a, Messages(a), lyr["ac"], kw.pop("cp_layer"), lyr["sew"],
                                        str(tmp_path / "out.gpkg"), **kw)


def con(res):
    return sqlite3.connect(res["path"])


def test_end_to_end_valid_gpkg_and_messages(world, tmp_path):
    a, lyr, layers = world
    # 22030223 gets a FLOW_GRADE that disagrees with its levels by 0.15 m of fall (listed); 22030208 differs by only
    # 2 mm of fall (counted in a summary line, not listed: Import Sewer Data fix)
    a.sources["C:/d/sew"]["rows"][1]["FLOW_GRADE"] = 50
    res = run(a, lyr, tmp_path)
    c = con(res)
    assert c.execute("select count(*) from wwn_nodes").fetchone()[0] == 4
    assert c.execute("select conduit_id, us_node_id, ds_node_id, dn, material, sewer_type, install_year "
                     "from wwn_conduits order by conduit_id").fetchall() == [
        ("22030208", "A1", "A2", 150, "pvc", "reticulation", None),
        ("22030223", "A3", "A4", 225, "vc", "main", 1999)]
    assert c.execute("select invert_level, notes from wwn_nodes where node_id='A2'").fetchone()[0] == 17.450001
    c.close()
    assert [i for i in res["issues"] if i["severity"] == "error"] == []
    assert res["report"].counts["reversed"] == 1 and res["report"].counts.get("connection_points_used", 0) == 0
    cp_reads = [r for r in a.cursor_log if r["source"] == "C:/d/cp"]
    assert cp_reads and not any(r["full_scan"] for r in cp_reads)      # far-away C9 is never read
    log = a.log
    assert any(lv == "warn" and "GRADE_MISMATCH" in t and "22030223" in t for lv, t in log)
    assert not any("GRADE_MISMATCH" in t and "22030208" in t for lv, t in log)
    assert any(lv == "info" and "1 sewer(s) have a levels / FLOW_GRADE grade difference" in t for lv, t in log)
    assert any(t.startswith("Reversed digitising direction of 1") for lv, t in log if lv == "info")
    assert not any("direction_reversed" in t for lv, t in log)
    assert not [t for lv, t in log if lv == "error"]
    assert any(t.startswith("Model check:") for _, t in log)
    # read with the target SR, SHAPE@XY for points / SHAPE@ for lines
    assert all(sr.factoryCode == 7850 for _, sr in a.cursor_calls)
    assert {f[-1] for f, _ in a.cursor_calls} == {"SHAPE@XY", "SHAPE@"}
    # added to the map
    # 5 model layers + the Issues layer (import warnings + check) + the node loads and pumps tables
    added = [os.path.basename(p) for k, p in a.added if k == "data"]
    assert len(added) == 8 and "main.wwn_issues" in added and "main.wwn_pumps" in added
    c = con(res)
    assert c.execute("select count(*) from wwn_issues where source='import'").fetchone()[0] == \
        len(res["report"].warnings)
    c.close()
    assert a.env.geographicTransformations is None


def test_add_to_map_false_and_output_suffix(world, tmp_path):
    a, lyr, layers = world
    res = tools_impl.import_sewer_data(a, Messages(a), lyr["ac"], None, lyr["sew"], str(tmp_path / "x"),
                                       {"diameter": "HEIGHT"}, add_to_map=False)
    assert res["path"].endswith("x.gpkg") and not a.added
    assert res["report"].counts.get("connection_points_used", 0) == 0


def test_existing_output_needs_overwrite(world, tmp_path):
    a, lyr, _ = world
    run(a, lyr, tmp_path)
    with pytest.raises(RuntimeError, match="already exists"):
        run(a, lyr, tmp_path)
    a.env.overwriteOutput = True
    res = run(a, lyr, tmp_path)
    assert res["report"].counts["conduits"] == 2


def test_gda94_sets_transformation(world, tmp_path):
    a, lyr, _ = world
    for k in ("ac", "sew"):
        a.sources[f"C:/d/{k}"]["sr"] = GDA94
    run(a, lyr, tmp_path)
    best = "GDA_1994_To_GDA2020_NTv2_Conformal_and_Distortion"
    assert a.env.geographicTransformations == best
    assert any(best in t for _, t in a.log)
    assert sewer_import.is_gda94(NS(factoryCode=4283, name="GCS_GDA_1994"))
    assert not sewer_import.is_gda94(NS(factoryCode=4326, name="GCS_WGS_1984"))


def test_transformation_failure_only_warns(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/sew"]["sr"] = GDA94

    class Env:
        overwriteOutput = False

        def __setattr__(self, k, v):
            if k == "geographicTransformations":
                raise RuntimeError("bad transformation")
            object.__setattr__(self, k, v)
    a.env = Env()
    run(a, lyr, tmp_path)
    assert any(lv == "warn" and "Could not set the geographic transformation" in t for lv, t in a.log)


def test_selected_only_and_unselected(world, tmp_path):
    a, lyr, _ = world
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[2])
    res = run(a, lyr, tmp_path, use_selected=True)
    assert res["report"].counts["conduits"] == 1
    assert [c["conduit_id"] for c in res["model"]["conduits"]] == ["22030223"]
    assert any("using 1 selected" in t for _, t in a.log)
    a.env.overwriteOutput = True
    res = run(a, lyr, tmp_path, use_selected=False)       # selection ignored: reads the data source
    assert res["report"].counts["conduits"] == 2


def test_missing_source_field_is_clear_error(world, tmp_path):
    a, lyr, _ = world
    with pytest.raises(RuntimeError, match="field 'DIAMETER' .* does not exist"):
        run(a, lyr, tmp_path, fields={"diameter": "DIAMETER"})
    assert not (tmp_path / "out.gpkg").exists()
    with pytest.raises(RuntimeError, match="connection point ID"):
        run(a, lyr, tmp_path, fields={"cp_id": ""})


def test_diameter_not_mapped_is_loud(world, tmp_path):
    a, lyr, _ = world
    res = run(a, lyr, tmp_path, fields={"diameter": ""})
    assert any(lv == "warn" and "NO DIAMETER FIELD MAPPED" in t for lv, t in a.log)
    assert {c["dn"] for c in res["model"]["conduits"]} == {150}


def test_grade_source_and_snap_options(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/sew"]["rows"][0].update({"DOWNSTREAM": None, "US_MANHOLE_ID": None, "DS_MANHOLE_ID": None})
    res = run(a, lyr, tmp_path, grade_source="field", snap_tolerance=0.5)
    p1 = [c for c in res["model"]["conduits"] if c["conduit_id"] == "22030208"][0]
    assert p1["data_source"] == "assumed" and p1["ds_invert"] == pytest.approx(17.459999 - 1.000555 / 120)
    assert res["report"].counts["ends_snapped"] == 2


def test_old_native_gives_clear_error(world, tmp_path, monkeypatch):
    a, lyr, _ = world
    from wwnet_arcgis import shared
    nat = shared.core("native")
    monkeypatch.setattr(nat, "get", lambda refresh=False: NS(check=lambda *a: [], part_full_flow_lps=abs,
                                                             depth_ratio_for_flow=abs, version=lambda: "0.0"))
    with pytest.raises(RuntimeError, match="too old.*write_model"):
        run(a, lyr, tmp_path)
    monkeypatch.setattr(nat, "get", lambda refresh=False: None)
    with pytest.raises(RuntimeError, match="wwnet_native"):
        run(a, lyr, tmp_path)


def test_field_defaults_from_layer(world):
    a, lyr, _ = world
    d = tools_impl.sewer_field_defaults(a, lyr["sew"])
    assert d["pipe_id"] == "ID" and d["us_invert"] == "UPSTREAM" and d["diameter"] == "HEIGHT"
    assert d["us_node"] == "US_MANHOLE_ID" and d["ac_id"] == "" and d["cp_id"] == "ID"
    d = tools_impl.sewer_field_defaults(a, lyr["ac"])
    assert d["ac_id"] == "PACID" and d["ac_surface_level"] == "TOPLEV" and d["us_invert"] == ""
    assert sewer_import.pick_defaults(["pacid"], {"ac_id": "PACID"}) == {"ac_id": "pacid"}


def load_pyt():
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_imp", str(TOOLBOX / "WWNet.pyt"))
    spec = importlib.util.spec_from_loader("wwnet_pyt_imp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_pyt_tool_parameters_and_execute(world, tmp_path):
    a, lyr, layers = world
    mod = load_pyt()
    t = mod.ImportSewerData()
    assert t.label == "Import Sewer Data" and t.category == "Data"
    params = t.getParameterInfo()
    names = [p.name for p in params]
    assert names[:12] == ["ac_layer", "cp_layer", "sewer_layer", "out_gpkg", "use_selected", "add_to_map",
                          "load_cps", "cp_radius", "snap", "grade_source", "tee_join", "import_is"]
    assert names[12:] == ["f_" + r for r, *_ in sewer_import.FIELD_ROLES]
    byn = {p.name: p for p in params}
    assert byn["snap"].value == 0.5 and byn["grade_source"].value == "levels"
    assert byn["use_selected"].value is False and byn["add_to_map"].value is True
    assert byn["import_is"].value is False and byn["import_is"].category == "Options"
    assert byn["cp_layer"].parameterType == "Optional" and byn["f_pipe_id"].parameterType == "Required"
    assert byn["f_diameter"].parameterDependencies == ["sewer_layer"]
    assert byn["f_ac_id"].parameterDependencies == ["ac_layer"]
    assert byn["f_chamber_type"].parameterDependencies == ["ac_layer"] and names.index("f_chamber_type") == 14
    assert byn["f_chamber_type"].parameterType == "Optional"
    # picking layers fills the field parameters with defaults present in the layer
    byn["ac_layer"].value, byn["sewer_layer"].value = lyr["ac"], lyr["sew"]
    t.updateParameters(params)
    assert byn["f_ac_id"].value == "PACID" and byn["f_us_invert"].value == "UPSTREAM"
    assert byn["f_diameter"].value == "HEIGHT"
    # user picks the diameter field and runs
    byn["f_diameter"].value = "HEIGHT"
    byn["out_gpkg"].value = str(tmp_path / "pyt.gpkg")
    t.execute(params, None)
    c = sqlite3.connect(tmp_path / "pyt.gpkg")
    assert c.execute("select count(*) from wwn_conduits").fetchone()[0] == 2
    c.close()
    assert shared.core("importer").GEOM == GEOM


COMPOSITE = "AGD_1966_To_GDA_1994_11_NTv2 + AGD_1966_To_GDA2020_1"


def test_transformation_choice_and_none_available(world, tmp_path):
    a, lyr, _ = world
    choose = sewer_import.choose_gda_transformation
    a.transformations = ["GDA_1994_To_GDA2020_1", "GDA_1994_To_GDA2020_NTv2_Conformal"]
    assert choose(a, GDA94) == "GDA_1994_To_GDA2020_NTv2_Conformal"
    a.transformations = ["GDA_1994_To_GDA2020_1"]
    assert choose(a, GDA94) == "GDA_1994_To_GDA2020_1"
    a.transformations = []
    a.sources["C:/d/sew"]["sr"] = GDA94
    run(a, lyr, tmp_path)
    assert a.env.geographicTransformations is None
    assert any(lv == "warn" and "No GDA94 -> GDA2020 transformation" in t for lv, t in a.log)


def test_transformation_rejects_composites_and_agd():
    a = fa.make_fake([])
    choose = sewer_import.choose_gda_transformation
    # the exact composite from the real run (matched "NTV2" before) is never chosen
    a.transformations = ["AGD_1966_To_GDA_1994_11_NTv2 + AGD_1966_To_GDA2020_1"]
    assert choose(a, GDA94) == ""
    a.transformations = [COMPOSITE, "AGD_1984_To_GDA2020_1", "GDA_1994_To_GDA2020_1"]
    assert choose(a, GDA94) == "GDA_1994_To_GDA2020_1"
    # preference: conformal-and-distortion > conformal > other, composites ignored wherever they are listed
    a.transformations = [COMPOSITE, "GDA_1994_To_GDA2020_1", "GDA_1994_To_GDA2020_NTv2_Conformal",
                         "GDA_1994_To_GDA2020_NTv2_Conformal_and_Distortion"]
    assert choose(a, GDA94) == "GDA_1994_To_GDA2020_NTv2_Conformal_and_Distortion"
    a.transformations = ["gda 1994 to gda2020 ntv2 cd", "GDA_1994_To_GDA2020_NTv2_Conformal"]
    assert choose(a, GDA94) == "gda 1994 to gda2020 ntv2 cd"
    a.transformations = [COMPOSITE, "GDA_1994_To_GDA2020_1", "GDA_1994_To_GDA2020_NTv2_Conformal"]
    assert choose(a, GDA94) == "GDA_1994_To_GDA2020_NTv2_Conformal"
    a.transformations = ["GDA2020_To_GDA_1994_1", "GDA_1994_To_WGS_1984_1"]
    assert choose(a, GDA94) == ""


def test_composite_transformation_not_set_in_import(world, tmp_path):
    a, lyr, _ = world
    a.transformations = [COMPOSITE]
    for k in ("ac", "sew"):
        a.sources[f"C:/d/{k}"]["sr"] = NS(factoryCode=4283, name="GCS_GDA_1994")
    run(a, lyr, tmp_path)
    assert a.env.geographicTransformations is None
    assert any(lv == "warn" and "No GDA94 -> GDA2020 transformation" in t for lv, t in a.log)


# ------------------------------------------------------------------ connection point / chamber reads

def cp_reads(a):
    return [r for r in a.cursor_log if r["source"] == "C:/d/cp"]


def test_cp_read_by_id_and_extent_never_full_scan(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/cp"]["rows"] = [
        {"ID": "C7", GEOM: (386000.0, 6461000.0)},        # far away, but named by a sewer end id
        {"ID": "C1", GEOM: (385001.9, 6460001.0)},        # inside the sewer extent (+2 m)
        {"ID": "C9", GEOM: (385500.0, 6460000.0)},        # elsewhere: must not be read
        {"ID": "C8", GEOM: (385050.0, 6460050.0)}]        # near, but outside the extent
    a.sources["C:/d/sew"]["rows"][1]["DS_MANHOLE_ID"] = "C7"
    res = run(a, lyr, tmp_path)
    reads = cp_reads(a)
    assert len(reads) == 2 and not any(r["full_scan"] for r in reads)
    by_id, by_ext = reads
    assert by_id["where"] == "\"ID\" IN ('C7')" and by_id["spatial_filter"] is None
    assert by_ext["spatial_filter"] is not None and by_ext["relationship"] == "INTERSECTS"
    x0, y0, x1, y1 = by_ext["spatial_filter"].bbox       # sewer extent grown by the 10 m load search radius
    assert (x0, y0, x1, y1) == pytest.approx((385000.0 - 10, 6460000.0 - 10, 385110.326591 + 10, 6460000.0 + 10))
    assert by_ext["spatial_filter"].spatialReference.factoryCode == 7850
    assert by_id["rows"] == 1 and by_ext["rows"] == 1
    assert any("not the whole layer" in t and "1 matching 1 sewer end id" in t for _, t in a.log)
    # neither read CP touches a sewer end, so none becomes a node
    assert res["report"].counts["connection_points_skipped"] == 2


def test_cp_spatial_filter_unsupported_falls_back_to_where_only(world, tmp_path):
    a, lyr, _ = world
    a.spatial_filter_supported = False
    a.sources["C:/d/sew"]["rows"][1]["DS_MANHOLE_ID"] = "C9"
    run(a, lyr, tmp_path)
    reads = cp_reads(a)
    assert len(reads) == 1 and reads[0]["where"] == "\"ID\" IN ('C9')" and not reads[0]["full_scan"]
    assert any(lv == "warn" and "does not support a spatial filter" in t for lv, t in a.log)
    assert any(lv == "warn" and "by id only" in t for lv, t in a.log)


def test_cp_id_lookup_is_chunked_and_quoted(world, tmp_path):
    a, lyr, _ = world
    rows = a.sources["C:/d/sew"]["rows"]
    for i in range(2000):
        rows.append(sewer(f"Z{i}", [(385200.0 + i * 0.01, 6460000.0), (385200.0 + i * 0.01, 6460005.0)],
                          f"U'{i}", f"V{i}"))
    run(a, lyr, tmp_path)
    where = [r["where"] for r in cp_reads(a) if r["where"]]
    assert len(where) == 5 and all(w.count("', '") + 1 <= 900 for w in where)     # 4000 ids / 900
    assert any("'U''0'" in w for w in where)                                       # quote doubled
    assert not any(r["full_scan"] for r in cp_reads(a))


def test_cp_selection_is_honoured(world, tmp_path):
    a, lyr, _ = world
    lyr["cp"] = fa.make_layer("cp", "C:/d/cp", selection=[1])
    run(a, lyr, tmp_path, use_selected=True)
    reads = cp_reads(a)
    assert len(reads) == 1 and reads[0]["selected"] and reads[0]["rows"] == 1


def test_chambers_read_by_extent_when_only_sewers_selected(world, tmp_path):
    a, lyr, _ = world
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[1])        # only the first sewer (A1-A2)
    res = run(a, lyr, tmp_path, use_selected=True)
    ac_reads = [r for r in a.cursor_log if r["source"] == "C:/d/ac"]
    assert len(ac_reads) == 1 and ac_reads[0]["spatial_filter"] is not None and not ac_reads[0]["full_scan"]
    assert {n["node_id"] for n in res["model"]["nodes"]} == {"A1", "A2"}
    assert any("Access chambers: nothing selected; read 2 feature(s)" in t for _, t in a.log)
    # selected chambers win; with no selection and no sewer selection everything is read
    a.env.overwriteOutput = True
    del a.cursor_log[:]
    lyr["ac"] = fa.make_layer("ac", "C:/d/ac", selection=[1, 2])
    run(a, lyr, tmp_path, use_selected=True)
    assert [r for r in a.cursor_log if r["source"] == "C:/d/ac"][0]["selected"]
    del a.cursor_log[:]
    lyr["ac"] = fa.make_layer("ac", "C:/d/ac")
    run(a, lyr, tmp_path, use_selected=False)
    assert [r for r in a.cursor_log if r["source"] == "C:/d/ac"][0]["full_scan"]


def ac_reads(a):
    return [r for r in a.cursor_log if r["source"] == "C:/d/ac"]


def test_context_chamber_referenced_by_id_is_fetched(world, tmp_path):
    a, lyr, _ = world
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[2])         # A3 -> A4
    lyr["ac"] = fa.make_layer("ac", "C:/d/ac", selection=[3])            # only A3 selected; A4 is not
    # A4 sits more than 2 m beyond the extent, so only the id lookup can find it
    a.sources["C:/d/sew"]["rows"][1]["DS_MANHOLE_ID"] = "A4"
    a.sources["C:/d/ac"]["rows"].append({"PACID": "A9", "TOPLEV": 1.0, GEOM: (385300.0, 6460000.0)})
    res = run(a, lyr, tmp_path, use_selected=True)
    reads = ac_reads(a)
    assert not any(r["full_scan"] for r in reads)
    assert reads[0]["selected"]
    by_id = [r for r in reads if r["where"]]
    assert by_id and by_id[0]["where"] == '"PACID" IN (\'A4\')' and by_id[0]["spatial_filter"] is None
    assert any(r["spatial_filter"] is not None for r in reads)
    nodes = {n["node_id"] for n in res["model"]["nodes"]}
    assert nodes == {"A3", "A4"}                                         # A9 (not referenced/near) never read
    cnd = res["model"]["conduits"][0]
    assert (cnd["us_node_id"], cnd["ds_node_id"]) == ("A3", "A4")
    assert res["report"].counts["context_chambers"] == 1 and res["report"].counts["access_chambers"] == 1
    assert any("Access chambers: 1 selected + 1 referenced/nearby read (not the whole layer)" in t for _, t in a.log)
    assert any("1 context chamber(s)" in t for lv, t in a.log if lv == "info")


def test_context_chambers_apply_type_rule_and_chunk_ids(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/ac"]["fields"] = AC_FIELDS[:3] + [("CHAMBER_TY", "String")] + AC_FIELDS[3:]
    for r in a.sources["C:/d/ac"]["rows"]:
        r["CHAMBER_TY"] = "M- standard manhole"
    a.sources["C:/d/ac"]["rows"][3]["CHAMBER_TY"] = "I- inspection shaft"     # A4 is an inspection shaft
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[2])
    lyr["ac"] = fa.make_layer("ac", "C:/d/ac", selection=[3])
    res = run(a, lyr, tmp_path, use_selected=True)
    assert res["report"].counts["inspection_shafts_skipped"] == 1
    assert {n["node_id"] for n in res["model"]["nodes"]} == {"A3", "E1"} or "A4" not in {
        n["node_id"] for n in res["model"]["nodes"]}
    # many referenced ids -> chunked <= 900, quoted
    a.env.overwriteOutput = True
    del a.cursor_log[:]
    rows = a.sources["C:/d/sew"]["rows"]
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=list(range(2, 2 + 1)))
    for i in range(2000):
        rows.append(sewer(f"Z{i}", [(385200.0 + i * 0.01, 6460000.0), (385200.0 + i * 0.01, 6460005.0)],
                          f"U'{i}", f"V{i}"))
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[2] + list(range(3, 2003)))
    run(a, lyr, tmp_path, use_selected=True)
    where = [r["where"] for r in ac_reads(a) if r["where"]]
    assert len(where) == 5 and all(w.count("', '") + 1 <= 900 for w in where)
    assert any("'U''0'" in w for w in where) and not any(r["full_scan"] for r in ac_reads(a))


def test_is_sewer_with_blank_us_id_and_ds_chamber_outside_selection(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/sew"]["rows"].append(sewer(
        "IS1", [(385100.0, 6460012.0), (385100.0, 6460000.0)], None, "A3", MAINTYPE="E- inspection shaft",
        UPSTREAM=5.5, DOWNSTREAM=5.2, FLOW_GRADE=40, LENGTH=12.0))
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[3])         # only the IS sewer; A3 not selected
    res = run(a, lyr, tmp_path, use_selected=True)
    c = res["model"]["conduits"][0]
    assert c["sewer_type"] == "is" and c["ds_node_id"] == "A3"
    up = [n for n in res["model"]["nodes"] if n["node_id"] == c["us_node_id"]][0]
    assert up["node_type"] == "is_end"
    rep = res["report"]
    assert rep.counts["context_chambers"] == 1 and rep.counts["dangling_is_io"] == 1
    assert rep.counts["direction_unknown"] == 0
    assert not any(r["full_scan"] for r in ac_reads(a))
    assert any("1 IS/IO upstream ends (expected), 0 dead-end upstream starts, 0 other" in t for lv, t in a.log)


def test_dead_end_and_grade_ratio_lines_reach_messages(world, tmp_path):
    a, lyr, _ = world
    res = run(a, lyr, tmp_path)
    info = [t for lv, t in a.log if lv == "info"]
    assert any(t.startswith("Direction:") for t in info)
    assert any(t.startswith("Grade check") and "median ratio" in t for t in info)
    assert res["report"].counts["direction_unknown"] == 0


def test_chambers_extent_unsupported_reads_all(world, tmp_path):
    a, lyr, _ = world
    a.spatial_filter_supported = False
    lyr["sew"] = fa.make_layer("sew", "C:/d/sew", selection=[1])
    res = run(a, lyr, tmp_path, use_selected=True)
    assert any("Access chambers: reading all features instead" in t for _, t in a.log)
    assert res["report"].counts["conduits"] == 1


# ------------------------------------------------------------------ messages and chamber types

def test_error_severity_issue_never_calls_add_error(world, tmp_path):
    a, lyr, _ = world
    # ds invert above us invert -> ADVERSE_GRADE (error severity in the model check)
    a.sources["C:/d/sew"]["rows"][1].update({"UPSTREAM": 0.2, "DOWNSTREAM": 0.9})
    res = run(a, lyr, tmp_path)
    assert [i for i in res["issues"] if i["severity"] == "error"]
    assert not [t for lv, t in a.log if lv == "error"]
    assert any(lv == "warn" and t.startswith("ERROR: ADVERSE_GRADE") for lv, t in a.log)
    assert any(t.startswith("Model check: 1 error(s)") for lv, t in a.log if lv == "info")


def test_chamber_type_field_skips_inspection_shafts(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/ac"]["fields"] = AC_FIELDS[:3] + [("CHAMBER_TY", "String")] + AC_FIELDS[3:]
    rows = a.sources["C:/d/ac"]["rows"]
    for r, t in zip(rows, ("M- standard manhole", "J- maintenance shaft", "M- standard manhole", "M- x")):
        r["CHAMBER_TY"] = t
    rows.append({"PACID": None, "CHAMBER_TY": "I- inspection shaft", "TOPLEV": 1.0, GEOM: (385300.0, 6460000.0)})
    res = run(a, lyr, tmp_path)
    nodes = {n["node_id"]: n["node_type"] for n in res["model"]["nodes"]}
    assert nodes["A2"] == "ms" and nodes["A1"] == "ac"
    assert res["report"].counts["inspection_shafts_skipped"] == 1
    assert not [w for w in res["report"].warnings if w.code == "AC_NO_ID"]
    assert any("inspection shaft" in t for lv, t in a.log if lv == "info")


def test_chamber_type_missing_field_warns_and_imports_all(world, tmp_path):
    a, lyr, _ = world
    res = run(a, lyr, tmp_path)
    assert any(lv == "warn" and "no field 'CHAMBER_TY'" in t for lv, t in a.log)
    assert {n["node_type"] for n in res["model"]["nodes"]} == {"ac"}


def test_warning_summary_by_code_then_examples(world, tmp_path):
    a, lyr, _ = world
    rows = a.sources["C:/d/sew"]["rows"]
    for i in range(30):
        rows.append(sewer(f"Q{i}", [(385300.0 + 10 * i, 6460100.0), (385305.0 + 10 * i, 6460100.0)], "A1", "A2"))
    run(a, lyr, tmp_path)
    info = [t for lv, t in a.log if lv == "info"]
    first_warn = next(n for n, (lv, t) in enumerate(a.log) if lv == "warn" and t.startswith("["))
    summ = next(n for n, (lv, t) in enumerate(a.log) if lv == "info" and "warning(s):" in t)
    assert summ < first_warn and any("DIRECTION_UNKNOWN x30" in t for t in info)
    warns = [t for lv, t in a.log if lv == "warn" and t.startswith("[")]
    assert len(warns) <= 40 and sum("[DIRECTION_UNKNOWN]" in t for t in warns) == 5
    assert any("more import warning(s)" in t for lv, t in a.log if lv == "warn")


def test_all_pyt_parameter_dependencies_are_existing_earlier_names(fake_arcpy):
    """Guards the real-Pro rule that broke the dialog: dependencies are names of earlier params."""
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    fake_arcpy([])
    path = Path(__file__).resolve().parents[1] / "WWNet.pyt"
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_deps", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_deps", loader))
    loader.exec_module(mod)
    for t in mod.Toolbox().tools:
        names = []
        for p in t().getParameterInfo():
            for d in p.parameterDependencies:
                assert d in names, f"{t().label}: {p.name} depends on {d!r}"
            names.append(p.name)


def test_cp_loads_assigned_and_written(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/cp"]["rows"] = [
        {"ID": "C1", GEOM: (385000.4, 6460004.0)},        # 4 m off sewer 22030208 (upstream node A1)
        {"ID": "C2", GEOM: (385105.0, 6460006.0)},        # 6 m off sewer 22030223 (US node A3)
        {"ID": "C3", GEOM: (385120.0, 6460009.9)},        # in the read extent but 13.9 m from the sewer
        {"ID": "C9", GEOM: (385500.0, 6460000.0)}]        # never read
    res = run(a, lyr, tmp_path)
    c = con(res)
    rows = c.execute("select load_id, node_id, connections, adwf_lps, source from wwn_node_loads "
                     "order by load_id").fetchall()
    assert rows == [("CP-A1", "A1", 1, None, "connection_points"), ("CP-A3", "A3", 1, None, "connection_points")]
    c.close()
    assert any(t.startswith("Loads: 2 connection point(s) assigned to 2 node(s); 1 unassigned") for _, t in a.log)
    assert any("CP_NOT_ASSIGNED" in t for lv, t in a.log if lv == "warn")
    assert not any(r["full_scan"] for r in cp_reads(a))
    assert any(t.startswith("Connection points: read 2") or "within the sewer extent +10 m" in t
               for _, t in a.log)


def test_cp_loads_off_and_optional_layer(world, tmp_path):
    a, lyr, _ = world
    a.sources["C:/d/cp"]["rows"] = [{"ID": "C1", GEOM: (385000.4, 6460004.0)}]
    res = run(a, lyr, tmp_path, load_connection_points=False)
    c = con(res)
    assert c.execute("select count(*) from wwn_node_loads").fetchone()[0] == 0
    c.close()
    assert not any(t.startswith("Loads:") for _, t in a.log)
    ext = [r for r in cp_reads(a) if r["spatial_filter"] is not None][0]
    assert ext["spatial_filter"].bbox[0] == pytest.approx(385000.0 - 2)


def test_no_cp_layer_gives_no_loads(world, tmp_path):
    a, lyr, _ = world
    res = tools_impl.import_sewer_data(a, Messages(a), lyr["ac"], None, lyr["sew"], str(tmp_path / "n.gpkg"),
                                       fields={"diameter": "HEIGHT"})
    c = con(res)
    assert c.execute("select count(*) from wwn_node_loads").fetchone()[0] == 0
    c.close()
    assert any("No connection point layer" in t for _, t in a.log)


def test_pyt_import_params_pass_cp_options(world, tmp_path):
    a, lyr, _ = world
    mod = load_pyt()
    t = mod.ImportSewerData()
    params = t.getParameterInfo()
    byn = {p.name: p for p in params}
    assert byn["load_cps"].value is True and byn["cp_radius"].value == 10.0
    assert byn["tee_join"].value == 0.5 and byn["tee_join"].category == "Options"
    byn["ac_layer"].value, byn["cp_layer"].value, byn["sewer_layer"].value = lyr["ac"], lyr["cp"], lyr["sew"]
    byn["f_diameter"].value = "HEIGHT"
    byn["f_ac_id"].value, byn["f_pipe_id"].value, byn["f_cp_id"].value = "PACID", "ID", "ID"
    byn["load_cps"].value = False
    byn["out_gpkg"].value = str(tmp_path / "o.gpkg")
    t.execute(params, None)
    assert not any(t_.startswith("Loads:") for _, t_ in a.log)


def test_import_is_sewers_flag_reaches_the_importer(world, tmp_path, monkeypatch):
    a, lyr, _ = world
    seen = []
    imp = sewer_import.importer()
    orig = imp.ImportOptions.from_config

    def spy(cfg=None, **kw):
        out = orig(cfg, **kw)
        seen.append(out.import_is_sewers)
        return out
    monkeypatch.setattr(imp.ImportOptions, "from_config", staticmethod(spy))
    for flag in (False, True):
        tools_impl.import_sewer_data(a, Messages(a), lyr["ac"], None, lyr["sew"], str(tmp_path / f"is{flag}.gpkg"),
                                     {"diameter": "HEIGHT"}, add_to_map=False, import_is_sewers=flag)
    assert seen == [False, True]
    src = open(os.path.join(os.path.dirname(__file__), "..", "WWNet.pyt"), encoding="utf-8").read()
    assert src.count('import_is_sewers=p.flag("import_is")') == 2       # Import Sewer Data + Import Network Data

