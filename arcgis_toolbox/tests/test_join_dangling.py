"""Join Dangling Sewer Ends: tools_impl wrapper and WWNet.pyt tool class."""
import importlib.machinery
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from wwnet_arcgis import shared, tools_impl
from wwnet_arcgis.messages import Messages

TOOLBOX = Path(__file__).resolve().parents[1]


@pytest.fixture
def repair():
    return shared.core("repair")


@pytest.fixture
def model(demo_gpkg, repair):
    """Demo + branch BF -> D1, D1 being 1.5 m off the middle of P04."""
    con = sqlite3.connect(demo_gpkg)
    a = repair.decode_blob(con.execute("select geom from wwn_nodes where node_id='N04'").fetchone()[0])[1]
    x, y = a[0] + 35.0, a[1]
    con.execute("insert into wwn_nodes (geom,node_id,node_type,surface_level,invert_level,status,data_source) "
                "values (?,'BF','ac',27.0,25.0,'existing','gis')", (repair.point_blob((x, y + 21.5)),))
    con.execute("insert into wwn_nodes (geom,node_id,node_type,status,data_source) values (?,'D1','junction',"
                "'existing','gis')", (repair.point_blob((x, y + 1.5)),))
    con.execute("insert into wwn_conduits (geom,conduit_id,us_node_id,ds_node_id,dn,us_invert,ds_invert,length_m,"
                "status,data_source) values (?,'BR1','BF','D1',150,25.0,24.5,20.0,'existing','gis')",
                (repair.line_blob([(x, y + 21.5), (x, y + 1.5)]),))
    con.commit()
    con.close()
    return demo_gpkg


def texts(a, kind=None):
    return [t for k, t in a.log if kind is None or k == kind]


def test_preview_prints_table_and_changes_nothing(fake_arcpy, model):
    a = fake_arcpy([])
    before = sqlite3.connect(model).execute("select count(*) from wwn_nodes").fetchone()[0]
    res = tools_impl.join_dangling_ends(a, Messages(a), model, 5.0, True)
    assert res["preview"] and len(res["joins"]) == 1
    assert sqlite3.connect(model).execute("select count(*) from wwn_nodes").fetchone()[0] == before
    out = "\n".join(texts(a))
    assert "PREVIEW ONLY" in out and "Dangling node | Branch | Joined to" in out and "| Distance m | Invert at tee | Note" in out
    print(out)
    assert "D1" in out and "BR1" in out and "P04 (tee T_D1, new pipe P04~t)" in out and "1.50" in out
    assert "24.075" in out and "Untick 'Preview only'" in out
    assert texts(a)[-1] == "Re-run Check Model / Export To ICM."


def test_apply_and_skipped_report(fake_arcpy, model):
    a = fake_arcpy([])
    res = tools_impl.join_dangling_ends(a, Messages(a), model, 5.0, False)
    assert res["applied"]["nodes_added"] == 1
    c = sqlite3.connect(model)
    assert c.execute("select ds_node_id from wwn_conduits where conduit_id='BR1'").fetchone()[0] == "T_D1"
    out = "\n".join(texts(a))
    assert "1 dangling end(s) joined, 0 not joined" in out and "refresh or re-add its layers" in out
    # nothing left: second run reports it
    a2 = fake_arcpy([])
    tools_impl.join_dangling_ends(a2, Messages(a2), model, 5.0, False)
    assert any("No dangling sewer ends found" in t for t in texts(a2))


def test_too_far_is_reported_as_skipped(fake_arcpy, model):
    a = fake_arcpy([])
    res = tools_impl.join_dangling_ends(a, Messages(a), model, 1.0, False)
    assert res["joins"] == [] and "Not joined:" in "\n".join(texts(a))
    assert any("nearest is P04 at 1.50 m" in t for t in texts(a, "warn"))


def test_locked_and_bad_inputs_are_error_warnings(fake_arcpy, model, repair, monkeypatch, tmp_path):
    a = fake_arcpy([])
    assert tools_impl.join_dangling_ends(a, Messages(a), str(tmp_path / "none.gpkg"), 5.0, True) is None
    assert tools_impl.join_dangling_ends(a, Messages(a), model, 0, True) is None

    def locked(*args, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(repair, "join_dangling_ends", locked)
    assert tools_impl.join_dangling_ends(a, Messages(a), model, 5.0, False) is None
    assert any(t.startswith("ERROR: The model GeoPackage is locked by ArcGIS Pro") for t in texts(a, "warn"))
    assert not texts(a, "error")


def test_pyt_tool_parameters_and_execute(fake_arcpy, model):
    a = fake_arcpy([])
    loader = importlib.machinery.SourceFileLoader("wwnet_pyt_jd", str(TOOLBOX / "WWNet.pyt"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("wwnet_pyt_jd", loader))
    loader.exec_module(mod)
    t = mod.JoinDanglingEnds()
    assert t.label == "Join Dangling Sewer Ends" and t.category == "Model"
    params = t.getParameterInfo()
    p = {x.name: x for x in params}
    assert list(p) == ["gpkg", "max_distance", "preview", "out_gpkg"]
    assert p["gpkg"].datatype == "DEFile" and p["max_distance"].datatype == "GPDouble"
    assert p["max_distance"].value == 5.0 and p["preview"].datatype == "GPBoolean" and p["preview"].value is True
    p["gpkg"].value = model
    t.execute(params, None)                                  # preview by default: nothing changes
    assert sqlite3.connect(model).execute("select count(*) from wwn_nodes where node_id='D1'").fetchone()[0] == 1
    p["preview"].value = False
    t.execute(params, None)
    assert sqlite3.connect(model).execute("select count(*) from wwn_nodes where node_id='D1'").fetchone()[0] == 0
    assert mod.JoinDanglingEnds in mod.Toolbox().tools


def test_import_tee_join_passed_to_importer(fake_arcpy):
    # ImportSewerData forwards the Advanced parameter (last) as tee_tolerance
    src = (TOOLBOX / "WWNet.pyt").read_text()
    assert 'tee_tolerance=p.num("tee_join")' in src
