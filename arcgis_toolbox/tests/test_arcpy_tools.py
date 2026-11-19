import pytest

import fake_arcpy as fa
from conftest import rows
from wwnet_arcgis import arcpy_tools, gpkg, shared

ToolError = None


def setup_layers(a_factory, gpkg_path, sel_conduits=()):
    layers = [fa.make_layer("Nodes", gpkg.table_path(gpkg_path, "wwn_nodes")),
              fa.make_layer("Gravity sewers", gpkg.table_path(gpkg_path, "wwn_conduits"),
                            selection=sel_conduits),
              fa.make_layer("Design results", gpkg.table_path(gpkg_path, "wwn_results_conduits"),
                            feature=False)]
    a = a_factory(layers)
    return a, layers


@pytest.fixture
def tools(fake_arcpy, demo_gpkg):
    a, layers = setup_layers(fake_arcpy, demo_gpkg, sel_conduits=[1, 3])
    t = arcpy_tools.ArcpyTools(a, apply=False, layers_fn=lambda: layers)
    return a, t


def tool_error():
    return shared.assistant("engine").ToolError


def test_list_layers(tools):
    _, t = tools
    out = {l["wwnet_table"]: l for l in t.list_layers({})}
    assert set(out) == {"wwn_nodes", "wwn_conduits", "wwn_results_conduits"}
    assert out["wwn_nodes"]["feature_count"] == 12
    assert out["wwn_conduits"]["selected"] == 2 and out["wwn_conduits"]["id_field"] == "conduit_id"
    assert "geom" not in out["wwn_conduits"]["fields"] and "dn" in out["wwn_conduits"]["fields"]


def test_get_selected_features(tools):
    _, t = tools
    r = t.get_selected_features({"layer": "wwn_conduits"})
    assert r["selected_count"] == 2
    assert [f["conduit_id"] for f in r["features"]] == ["P01", "P03"]
    assert t.get_selected_features({"layer": "Nodes"})["selected_count"] == 0


def test_query_features_where_clause_and_limit(tools):
    _, t = tools
    r = t.query_features({"layer": "Gravity sewers", "expression": "dn = 150", "limit": 3})
    assert r["returned"] == 3 and all(f["dn"] == 150 for f in r["features"])
    assert t.query_features({"layer": "wwn_nodes", "expression": "node_type = 'outfall'"})["returned"] == 1
    with pytest.raises(tool_error(), match="Query failed"):
        t.query_features({"layer": "wwn_nodes", "expression": "nonsense ((("})
    with pytest.raises(tool_error(), match="not found"):
        t.query_features({"layer": "nope", "expression": ""})


def test_query_uses_datasource_not_selection(tools):
    a, t = tools
    # selection of 2 conduits must not narrow an all-rows query
    assert t.query_features({"layer": "wwn_conduits", "expression": "", "limit": 200})["returned"] == 11


def test_get_feature_derives_grade(tools):
    _, t = tools
    f = t.get_feature({"layer": "wwn_conduits", "id_value": "P01"})
    assert f["conduit_id"] == "P01" and f["dn"] == 150
    assert f["derived"]["length_m_used"] == 70.0 and f["derived"]["grade_1in"] == 100.0
    assert "SHAPE@LENGTH" not in f
    n = t.get_feature({"layer": "wwn_nodes", "id_value": "N06"})
    assert n["node_type"] == "drop_ac"
    with pytest.raises(tool_error(), match="No feature"):
        t.get_feature({"layer": "wwn_nodes", "id_value": "x' OR '1'='1"})


def test_get_feature_quotes_value(tools):
    a, t = tools
    with pytest.raises(tool_error()):
        t.get_feature({"layer": "wwn_nodes", "id_value": "a'b"})
    assert any("'a''b'" in q for q in a.queries)


def edit(i, field, value):
    return {"id": i, "field": field, "value": value}


def test_propose_without_apply_never_writes(tools, demo_gpkg):
    a, t = tools
    r = t.propose_edits({"layer": "wwn_conduits", "reason": "bigger",
                         "edits": [edit("P01", "dn", 225), edit("P01", "notes", "upsized")]})
    assert r["applied"] is False and r["outcome"] == "proposed_not_applied" and r["count"] == 2
    assert a.writes == [] and not any(c for e in fa.FakeEditor.instances for c in e.calls)
    assert rows(demo_gpkg, "select dn, notes from wwn_conduits where conduit_id='P01'") == [(150, None)]
    assert [(p.id, p.field, p.old, p.value) for p in t.proposals] == \
           [("P01", "dn", 150, 225), ("P01", "notes", None, "upsized")]
    with pytest.raises(PermissionError):
        t.apply_proposals(t.proposals)                     # apply flag off: hard refusal
    assert a.writes == []
    lines = arcpy_tools.proposal_table(t.proposals)
    assert "P01" in lines[2] and "150" in lines[2] and "225" in lines[2]


def test_apply_true_writes_only_proposed(fake_arcpy, demo_gpkg):
    a, layers = setup_layers(fake_arcpy, demo_gpkg)
    log = []
    t = arcpy_tools.ArcpyTools(a, apply=True, layers_fn=lambda: layers, emit=log.append)
    r = t.propose_edits({"layer": "wwn_conduits", "reason": "r",
                         "edits": [edit("P01", "dn", 225), edit("P01", "notes", "upsized")]})
    assert r == {"applied": True, "outcome": "applied_and_saved", "count": 2}
    assert rows(demo_gpkg, "select dn, notes from wwn_conduits where conduit_id='P01'") == [(225, "upsized")]
    assert rows(demo_gpkg, "select count(*) from wwn_conduits where dn=225") [0][0] >= 1
    # other conduits untouched
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P02'") == [(150,)]
    assert len(a.writes) == 1 and set(a.writes[0][2]) == {"conduit_id", "dn", "notes"}
    assert any("APPLIED wwn_conduits P01.dn: 150 -> 225" in l for l in log)
    ed = fa.FakeEditor.instances[-1]
    assert [c[0] for c in ed.calls] == ["startEditing", "startOperation", "stopOperation", "stopEditing"]
    assert ed.calls[-1][1] == (True,) and ed.workspace == demo_gpkg


def test_foreign_proposal_is_refused(fake_arcpy, demo_gpkg):
    a, layers = setup_layers(fake_arcpy, demo_gpkg)
    t = arcpy_tools.ArcpyTools(a, apply=True, layers_fn=lambda: layers)
    foreign = arcpy_tools.Proposal("Gravity sewers", "wwn_conduits",
                                   gpkg.table_path(demo_gpkg, "wwn_conduits"), demo_gpkg,
                                   "conduit_id", "P02", "dn", 150, 300)
    with pytest.raises(PermissionError, match="not proposed in this run"):
        t.apply_proposals([foreign])
    assert a.writes == []
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P02'") == [(150,)]


def test_invalid_proposals_rejected(tools, demo_gpkg):
    a, t = tools
    for bad, msg in [(edit("P01", "conduit_id", "X"), "cannot be edited"),
                     (edit("P01", "geom", None), "unknown field"),
                     (edit("P01", "no_such", 1), "unknown field"),
                     (edit("P01", "dn", "abc"), "not a number"),
                     (edit("P01", "status", "bogus"), "not in"),
                     (edit("NOPE", "dn", 225), "no feature")]:
        with pytest.raises(tool_error(), match=msg):
            t.propose_edits({"layer": "wwn_conduits", "edits": [bad], "reason": ""})
    with pytest.raises(tool_error(), match="read-only"):
        t.propose_edits({"layer": "wwn_results_conduits", "edits": [edit("P01", "result", "pass")],
                         "reason": ""})
    assert t.proposals == [] and a.writes == []


def test_apply_failure_rolls_back_editor(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = setup_layers(fake_arcpy, demo_gpkg)
    t = arcpy_tools.ArcpyTools(a, apply=True, layers_fn=lambda: layers)

    def boom(*args, **kw):
        raise RuntimeError("locked")
    monkeypatch.setattr(a.da, "UpdateCursor", boom)
    with pytest.raises(tool_error(), match="Edit failed"):
        t.propose_edits({"layer": "wwn_conduits", "reason": "", "edits": [edit("P01", "dn", 225)]})
    ed = fa.FakeEditor.instances[-1]
    assert ("abortOperation", ()) in ed.calls and ed.calls[-1] == ("stopEditing", (False,))
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P01'") == [(150,)]
    assert not t.proposals[0].applied


def test_editor_start_failure_writes_nothing(fake_arcpy, demo_gpkg, monkeypatch):
    a, layers = setup_layers(fake_arcpy, demo_gpkg)
    t = arcpy_tools.ArcpyTools(a, apply=True, layers_fn=lambda: layers)

    def no_editor(*args, **kw):
        raise RuntimeError("workspace locked")
    monkeypatch.setattr(a.da, "Editor", no_editor)
    with pytest.raises(Exception, match="No edits were applied"):
        t.propose_edits({"layer": "wwn_conduits", "reason": "", "edits": [edit("P01", "dn", 225)]})
    assert a.writes == []
    assert rows(demo_gpkg, "select dn from wwn_conduits where conduit_id='P01'") == [(150,)]
    assert not t.proposals[0].applied
