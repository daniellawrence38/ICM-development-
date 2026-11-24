import shutil
import sqlite3

import pytest

from conftest import REPO
from wwnet.core import repair

PYT_SRC = REPO / "examples" / "demo" / "demo.gpkg"


def xy(con, nid):
    g = repair.decode_blob(con.execute("SELECT geom FROM wwn_nodes WHERE node_id=?", (nid,)).fetchone()[0])
    return g[1]


def add_node(con, nid, pt, ntype="junction", surface=None, invert=None):
    con.execute("INSERT INTO wwn_nodes (geom,node_id,node_type,surface_level,invert_level,status,data_source) "
                "VALUES (?,?,?,?,?,'existing','gis')", (repair.point_blob(pt), nid, ntype, surface, invert))


def add_conduit(con, cid, us, ds, pts, ui, di, length=None):
    con.execute("INSERT INTO wwn_conduits (geom,conduit_id,us_node_id,ds_node_id,dn,us_invert,ds_invert,length_m,"
                "status,data_source,material,asset_id) VALUES (?,?,?,?,150,?,?,?,'existing','gis','vc','A1')",
                (repair.line_blob(pts), cid, us, ds, ui, di, length))


@pytest.fixture
def model(tmp_path):
    """Demo model + a branch F -> D where D (junction) is 1.5 m off the middle of P04 (N04 -> N05)."""
    dst = tmp_path / "m.gpkg"
    shutil.copy(PYT_SRC, dst)
    con = sqlite3.connect(dst)
    a, b = xy(con, "N04"), xy(con, "N05")
    mid = ((a[0] + b[0]) / 2, a[1])             # P04 runs along x: chainage 35 of 70
    add_node(con, "BF", (mid[0], mid[1] + 21.5), "ac", 27.0, 25.0)
    add_node(con, "D1", (mid[0], mid[1] + 1.5), "junction")
    add_conduit(con, "BR1", "BF", "D1", [(mid[0], mid[1] + 21.5), (mid[0], mid[1] + 1.5)], 25.0, 24.5, 20.0)
    con.commit()
    con.close()
    return str(dst), mid


def rows(gpkg, sql, *args):
    c = sqlite3.connect(gpkg)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def test_blob_roundtrip_matches_rust_layout():
    b = repair.point_blob((1.5, 2.5))
    assert b[:8] == bytes.fromhex("47500001AA1E0000") and len(b) == 29       # no envelope, srs 7850
    assert repair.decode_blob(b) == ("point", (1.5, 2.5))
    lb = repair.line_blob([(0, 0), (3, 4)])
    assert lb[3] == 3 and lb[:2] == b"GP" and repair.decode_blob(lb) == ("line", [(0, 0), (3, 4)])
    demo = sqlite3.connect(PYT_SRC)
    g = demo.execute("SELECT geom FROM wwn_conduits WHERE conduit_id='P04'").fetchone()[0]
    kind, pts = repair.decode_blob(g)
    assert repair.line_blob(pts) == g                                       # identical bytes to the Rust writer
    g = demo.execute("SELECT geom FROM wwn_nodes WHERE node_id='N04'").fetchone()[0]
    assert repair.point_blob(repair.decode_blob(g)[1]) == g


def test_find_demo_has_no_dangling():
    assert repair.find_dangling_joins(str(PYT_SRC)) == []


def test_split_position_inverts_pieces_and_branch(model):
    g, mid = model
    plan = repair.find_dangling_joins(g)
    assert len(plan) == 1
    j = plan[0]
    assert j["action"] == "split" and j["dangling_node"] == "D1" and j["main_conduit"] == "P04"
    assert j["distance_m"] == pytest.approx(1.5) and j["chainage_m"] == pytest.approx(35.0)
    assert j["invert_at_tee"] == pytest.approx(24.075) and j["tee_node"] == "T_D1"   # planned id
    # preview changes nothing
    before = rows(g, "select count(*) from wwn_nodes")[0][0]
    res = repair.join_dangling_ends(g, 5.0, preview=True)
    assert res["applied"] is None and res["joins"] and rows(g, "select count(*) from wwn_nodes")[0][0] == before
    res = repair.join_dangling_ends(g, 5.0)
    j = res["joins"][0]
    assert j["tee_node"] == "T_D1" and j["new_conduit"] == "P04~t" and res["skipped"] == []
    ap = res["applied"]
    assert (ap["nodes_added"], ap["nodes_deleted"], ap["conduits_added"]) == (1, 1, 1)
    con = sqlite3.connect(g)
    t = con.execute("select node_type,status,data_source,invert_level,surface_level,notes from wwn_nodes "
                    "where node_id='T_D1'").fetchone()
    assert t[:3] == ("junction", "proposed", "design")                     # copied from P04
    assert t[3] == pytest.approx(24.075) and t[4] == pytest.approx((26.25 + 25.95) / 2)
    assert t[5] == "tee inserted by Join Dangling Sewer Ends (d = 1.50 m from D1)"
    assert xy(con, "T_D1") == pytest.approx(mid)
    assert con.execute("select count(*) from wwn_nodes where node_id='D1'").fetchone()[0] == 0
    c = {r[0]: r for r in con.execute("select conduit_id,us_node_id,ds_node_id,us_invert,ds_invert,length_m,"
                                      "dn,material,asset_id,status,data_source,notes,geom from wwn_conduits")}
    p1, p2, br = c["P04"], c["P04~t"], c["BR1"]
    assert p1[1:3] == ("N04", "T_D1") and p2[1:3] == ("T_D1", "N05")
    assert (p1[3], p1[4]) == (24.25, pytest.approx(24.075)) and (p2[3], p2[4]) == (pytest.approx(24.075), 23.9)
    assert p1[5] == pytest.approx(35.0) and p2[5] == pytest.approx(35.0)
    assert p2[6:11] == p1[6:11] and p2[6] == p1[6]
    assert p2[11] == "split at tee for BR1" and "split at tee for BR1" in p1[11]
    a, b = xy(con, "N04"), xy(con, "N05")
    assert repair.decode_blob(p1[12])[1][-1] == pytest.approx(mid)
    assert repair.decode_blob(p1[12])[1][0] == a and repair.decode_blob(p2[12])[1][-1] == b
    assert repair.decode_blob(p2[12])[1][0] == pytest.approx(mid)
    assert br[2] == "T_D1" and br[4] == 24.5                                # ds invert kept
    assert br[5] == pytest.approx(20.0 + 1.5)                               # gap added
    assert repair.decode_blob(br[12])[1][-1] == pytest.approx(mid)
    ext = con.execute("select min_x,max_x from gpkg_contents where table_name='wwn_nodes'").fetchone()
    assert ext[0] <= a[0] and ext[1] >= b[0]
    # nothing dangling afterwards, second run is a no-op
    assert repair.find_dangling_joins(g) == []
    # native check reports no topology problem
    native = pytest.importorskip("wwnet_native")
    issues = native.check(g, False)
    bad = [i for i in issues if i["severity"] == "error" and ("dangling" in str(i).lower() or "topolog" in str(i).lower())]
    assert bad == [], bad


def test_reversed_main_geometry(model):
    g, mid = model
    con = sqlite3.connect(g)
    pts = repair.decode_blob(con.execute("select geom from wwn_conduits where conduit_id='P04'").fetchone()[0])[1]
    con.execute("update wwn_conduits set geom=? where conduit_id='P04'", (repair.line_blob(pts[::-1]),))
    con.commit()
    con.close()
    j = repair.find_dangling_joins(g)[0]
    assert j["chainage_m"] == pytest.approx(35.0) and j["invert_at_tee"] == pytest.approx(24.075)
    # off-centre so the orientation matters: move D1 near N05 (chainage 60)
    con = sqlite3.connect(g)
    a = xy(con, "N04")
    con.execute("update wwn_nodes set geom=? where node_id='D1'", (repair.point_blob((a[0] + 60, a[1] + 1.0)),))
    con.commit()
    con.close()
    j = repair.find_dangling_joins(g)[0]
    assert j["chainage_m"] == pytest.approx(60.0) and j["invert_at_tee"] == pytest.approx(24.25 - 0.35 * 60 / 70)
    repair.join_dangling_ends(g)
    con = sqlite3.connect(g)
    cs = {r[0]: r for r in con.execute("select conduit_id,us_node_id,ds_node_id,geom,length_m from wwn_conduits")}
    first = repair.decode_blob(cs["P04"][3])[1]
    assert cs["P04"][1:3] == ("N04", "T_D1") and first[0] == a                 # stored upstream -> downstream
    assert cs["P04"][4] == pytest.approx(60.0) and cs["P04~t"][4] == pytest.approx(10.0)


def test_join_existing_end_node_when_near(model):
    g, mid = model
    con = sqlite3.connect(g)
    n5 = xy(con, "N05")
    con.execute("update wwn_nodes set geom=? where node_id='D1'", (repair.point_blob((n5[0] - 0.3, n5[1] + 1.0)),))
    con.commit()
    con.close()
    j = repair.find_dangling_joins(g)[0]
    assert j["action"] == "join_node" and j["tee_node"] == "N05"
    res = repair.join_dangling_ends(g)
    assert res["applied"]["nodes_added"] == 0 and res["applied"]["conduits_added"] == 0
    con = sqlite3.connect(g)
    assert con.execute("select ds_node_id from wwn_conduits where conduit_id='BR1'").fetchone()[0] == "N05"
    assert con.execute("select count(*) from wwn_conduits").fetchone()[0] == 11 + 1
    assert con.execute("select count(*) from wwn_nodes where node_id='D1'").fetchone()[0] == 0
    last = repair.decode_blob(con.execute("select geom from wwn_conduits where conduit_id='BR1'").fetchone()[0])[1][-1]
    assert last == n5


def test_beyond_distance_skipped_and_terminal_types_untouched(model):
    g, mid = model
    res = repair.join_dangling_ends(g, max_distance_m=1.0)
    assert res["joins"] == [] and len(res["skipped"]) == 1
    s = res["skipped"][0]
    assert s["dangling_node"] == "D1" and s["nearest_conduit"] == "P04" and s["nearest_distance_m"] == pytest.approx(1.5)
    assert "nearest is P04 at 1.50 m" in s["reason"]
    assert rows(g, "select count(*) from wwn_nodes where node_id='D1'")[0][0] == 1
    # an 'ac' dangling chamber is a real terminal chamber: never joined
    con = sqlite3.connect(g)
    con.execute("update wwn_nodes set node_type='ac' where node_id='D1'")
    con.commit()
    con.close()
    assert repair.find_dangling_joins(g) == []


def test_branch_below_main_invert_warns_but_keeps_level(model):
    g, _ = model
    con = sqlite3.connect(g)
    con.execute("update wwn_conduits set ds_invert=23.0 where conduit_id='BR1'")
    con.commit()
    con.close()
    j = repair.find_dangling_joins(g)[0]
    assert any("arrives below the main invert by 1.075 m" in w for w in j["warnings"])
    repair.join_dangling_ends(g)
    assert rows(g, "select ds_invert from wwn_conduits where conduit_id='BR1'") == [(23.0,)]


def test_two_branches_on_one_main_and_unique_ids(model):
    g, mid = model
    con = sqlite3.connect(g)
    a = xy(con, "N04")
    add_node(con, "BF2", (a[0] + 50, a[1] - 20), "ac", 27.0, 25.0)
    add_node(con, "D2", (a[0] + 50, a[1] - 1.0), "is_end")
    add_conduit(con, "BR2", "BF2", "D2", [(a[0] + 50, a[1] - 20), (a[0] + 50, a[1] - 1.0)], 25.0, 24.4, 19.0)
    con.execute("insert into wwn_conduits (conduit_id,us_node_id,ds_node_id,dn,status,data_source,geom) "
                "values ('P04~t','N01','N02',150,'proposed','design',NULL)")
    con.commit()
    con.close()
    plan = repair.find_dangling_joins(g)
    assert [p["dangling_node"] for p in plan] == ["D1", "D2"]
    assert plan[0]["new_conduit"] == "P04~t2" and plan[1]["main_conduit"] == "P04~t2"
    res = repair.join_dangling_ends(g)
    assert len(res["joins"]) == 2
    con = sqlite3.connect(g)
    c = {r[0]: r[1:] for r in con.execute("select conduit_id,us_node_id,ds_node_id,us_invert,ds_invert from wwn_conduits")}
    assert c["P04"][1] == "T_D1" and c["P04~t2"][:2] == ("T_D1", "T_D2") and c["P04~t2~t"][:2] == ("T_D2", "N05")
    assert c["P04~t2"][3] == pytest.approx(24.25 - 0.35 * 50 / 70) and c["P04~t2~t"][2] == pytest.approx(24.0)
    assert repair.find_dangling_joins(g) == []


def test_rollback_on_error(model, monkeypatch):
    g, _ = model
    snap = rows(g, "select node_id,hex(geom) from wwn_nodes order by node_id"), \
        rows(g, "select conduit_id,ds_node_id,length_m,hex(geom) from wwn_conduits order by conduit_id")

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(repair, "_update_extents", boom)
    with pytest.raises(RuntimeError, match="injected"):
        repair.join_dangling_ends(g)
    assert (rows(g, "select node_id,hex(geom) from wwn_nodes order by node_id"),
            rows(g, "select conduit_id,ds_node_id,length_m,hex(geom) from wwn_conduits order by conduit_id")) == snap


def test_rtree_refused(model):
    g, _ = model
    con = sqlite3.connect(g)
    con.execute("create table rtree_wwn_nodes_geom (id integer)")
    con.commit()
    con.close()
    with pytest.raises(repair.RepairError, match="R-tree"):
        repair.find_dangling_joins(g)


def test_stale_join_skipped(model):
    g, _ = model
    plan = repair.find_dangling_joins(g)
    repair.apply_joins(g, plan)
    res = repair.apply_joins(g, plan)
    assert res["joined"] == [] and "no longer dangling" in res["skipped"][0]["reason"]
