"""Export To ICM of a model imported with several branch ends meeting at one point on a main (junction fitting)."""
import pytest

from wwnet_arcgis import icm_export, shared


def _ac(pid, x, y, top):
    imp = shared.core("importer")
    return {"PACID": pid, "TOPLEV": top, imp.GEOM: (x, y)}


def _sew(pid, line, us, ds, ui, di):
    imp = shared.core("importer")
    return {"ID": pid, "STATUS": "A- actual", "MAINTYPE": "R- retic", "PURPOSE": "L- local", "DOWNSTREAM": di,
            "UPSTREAM": ui, "FLOW_GRADE": None, "LENGTH": None, "MATERIAL": "PVC- polyvinyl chloride",
            "PLANSET": "PS1", "DATEINST": None, "US_MANHOLE_ID": us, "DS_MANHOLE_ID": ds, imp.GEOM: line}


def test_two_branches_at_one_point_on_main_export_connected_to_outfall(tmp_path):
    native = pytest.importorskip("wwnet_native")
    if not hasattr(native, "write_model"):
        pytest.skip("wwnet_native too old (no write_model)")
    imp = shared.core("importer")
    acs = [_ac("A", 0, 0, 25.0), _ac("B", 100, 0, 25.0), _ac("C", 40, 20, 30.0), _ac("D", 40, -20, 30.0)]
    sew = [_sew("M", [(0, 0), (100, 0)], "A", "B", 20.0, 19.0),
           _sew("B1", [(40, 20), (40, 0.2)], "C", None, 21.0, 20.5),
           _sew("B2", [(40, -20), (40, -0.2)], "D", None, 21.0, 20.5)]
    model, rep = imp.build_model(acs, [], sew)
    assert rep.counts["tee_junctions"] == 1 and rep.counts["tee_branch_ends"] == 2
    gpkg = str(tmp_path / "tees.gpkg")
    native.write_model(gpkg, imp.model_json(model))
    d = icm_export.build(gpkg)
    assert not any("sewer end" in w for w in d["warnings"]), d["warnings"]
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["B"]["node_type"] == "Outfall" and nodes["T1"]["node_type"] == "Break"
    adj = {}
    for c in d["conduits"]:
        adj.setdefault(c["us_node_id"], []).append(c["ds_node_id"])

    def reaches_outfall(n, seen=()):
        return nodes[n]["node_type"] == "Outfall" or any(
            reaches_outfall(m, seen + (n,)) for m in adj.get(n, ()) if m not in seen)

    assert [n for n in nodes if not reaches_outfall(n)] == []
