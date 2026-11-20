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
    assert res["report"].counts["reversed"] == 1 and res["report"].counts["connection_points_skipped"] == 1
    log = a.log
    assert any(lv == "warn" and "grade_mismatch" in t and "22030223" in t for lv, t in log)
    assert any("direction_reversed" in t for lv, t in log if lv == "warn")
    assert any(t.startswith("Model check:") for _, t in log)
    # read with the target SR, SHAPE@XY for points / SHAPE@ for lines
    assert all(sr.factoryCode == 7850 for _, sr in a.cursor_calls)
    assert {f[-1] for f, _ in a.cursor_calls} == {"SHAPE@XY", "SHAPE@"}
    # added to the map
    assert len([k for k, _ in a.added if k == "data"]) == 5
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
    from wwnet_arcgis import cli
    monkeypatch.setattr(cli, "native_service", lambda: NS(check=lambda *a: [], design=None, trace=None))
    with pytest.raises(RuntimeError, match="no write_model"):
        run(a, lyr, tmp_path)
    monkeypatch.setattr(cli, "native_service", lambda: None)
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
    assert names[:8] == ["ac_layer", "cp_layer", "sewer_layer", "out_gpkg", "snap", "grade_source",
                         "use_selected", "add_to_map"]
    assert names[8:] == ["f_" + r for r, *_ in sewer_import.FIELD_ROLES]
    byn = {p.name: p for p in params}
    assert byn["snap"].value == 0.5 and byn["grade_source"].value == "levels"
    assert byn["use_selected"].value is False and byn["add_to_map"].value is True
    assert byn["cp_layer"].parameterType == "Optional" and byn["f_pipe_id"].parameterType == "Required"
    assert byn["f_diameter"].parameterDependencies == ["sewer_layer"]
    assert byn["f_ac_id"].parameterDependencies == ["ac_layer"]
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


def test_transformation_choice_and_none_available(world, tmp_path):
    a, lyr, _ = world
    a.transformations = ["GDA_1994_To_GDA2020_1", "GDA_1994_To_GDA2020_NTv2_Conformal"]
    assert sewer_import.choose_gda_transformation(a, GDA94) == "GDA_1994_To_GDA2020_NTv2_Conformal"
    a.transformations = ["GDA_1994_To_GDA2020_1"]
    assert sewer_import.choose_gda_transformation(a, GDA94) == "GDA_1994_To_GDA2020_1"
    a.transformations = []
    a.sources["C:/d/sew"]["sr"] = GDA94
    run(a, lyr, tmp_path)
    assert a.env.geographicTransformations is None
    assert any(lv == "warn" and "No GDA94 -> GDA2020 transformation" in t for lv, t in a.log)


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
