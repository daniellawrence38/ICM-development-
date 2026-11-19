import csv
import json
import sqlite3
import sys

import pytest

from conftest import BENCH, REPO
from wwnet.core import checks, hydraulics, native, rules, ruleset, schema, tomlmini


def _bench_rows():
    with open(BENCH, newline="") as fh:
        return list(csv.DictReader(l for l in fh if not l.startswith("#")))


@pytest.mark.parametrize("row", _bench_rows(), ids=lambda r: f"DN{r['dn']}-1in{r['grade_1in']}")
def test_colebrook_matches_benchmark(row):
    q = hydraulics.part_full_flow_lps(int(row["dn"]), float(row["grade_1in"]),
                                      float(row["d_over_D"]), ks_mm=1.0)
    assert q == pytest.approx(float(row["colebrook_lps"]), abs=0.15)


def test_colebrook_within_5pct_of_table44():
    for r in _bench_rows():
        q = hydraulics.part_full_flow_lps(int(r["dn"]), float(r["grade_1in"]), float(r["d_over_D"]))
        assert abs(q - float(r["table44_lps"])) / float(r["table44_lps"]) < 0.05


def test_depth_ratio_roundtrip_and_surcharge():
    q = hydraulics.part_full_flow_lps(300, 400, 0.5)
    assert hydraulics.depth_ratio_for_flow(q, 300, 400) == pytest.approx(0.5, abs=1e-4)
    assert hydraulics.depth_ratio_for_flow(1e6, 300, 400) is None
    assert hydraulics.depth_ratio_for_flow(0, 300, 400) == 0.0


def test_toml_fallback_parser_matches_tomllib():
    tomllib = pytest.importorskip("tomllib")
    for name in ("ds50_v2r4.toml", "ds51_TODO.toml"):
        text = (REPO / "rulesets" / name).read_text()
        assert tomlmini.loads_fallback(text) == tomllib.loads(text)


def test_native_detection():
    # the plugin package itself is named `wwnet` and must not be mistaken for the extension
    nat = native.get(refresh=True)
    try:
        import wwnet_native  # noqa: F401
    except ImportError:
        assert nat is None
    else:
        assert nat is not None and hasattr(nat, "part_full_flow_lps")


def test_native_check_conduit_kwargs():
    pytest.importorskip("wwnet_native")
    from wwnet.assistant import tools
    r = tools.check_conduit({"dn_mm": 150, "grade_1in": 200, "gsdf_lps": 7, "length_m": 80,
                             "land_use": "industrial"})
    assert r["source"] == "wwnet", r.get("note")
    by_rule = {c["rule"]: c["result"] for c in r["results"]}
    assert by_rule["max_flow"] == "fail" and by_rule["min_dn"] == "fail"


def test_lookup_and_gsdf():
    assert ruleset.lookup("grades", "225")["min_grade_1in"] == 300
    assert ruleset.lookup("residential", "south:R20")[0]["dry_gsdf_lps_ha"] == 0.219
    with pytest.raises(KeyError):
        ruleset.lookup("nope")
    r = ruleset.residential_gsdf(10, "R20", "dry", "south")
    assert r["gsdf_lps"] == pytest.approx(2.19)
    low = ruleset.residential_gsdf(1, "R10", "dry", "south") if "R10" in {
        x["rcode"] for x in ruleset._rows_as_dicts(ruleset.load_ruleset()["loading"]["residential"]["south"])
    } else None
    assert low is None  # R10 is not in the tables


def test_check_conduit_fallback():
    res = {r["rule"]: r for r in rules.check_conduit(150, 100, gsdf_lps=9)}
    assert res["max_d_over_D"]["result"] == "fail" and res["max_d_over_D"]["limit"] == 0.5
    assert res["table_4_4_max_flow"]["result"] == "fail"
    flat = {r["rule"]: r for r in rules.check_conduit(150, 250)}
    assert flat["min_grade"]["result"] == "warn" and flat["min_grade"]["clause"] == "4.2.1(q)"
    assert {r["rule"]: r for r in rules.check_conduit(225, 400)}["min_grade"]["result"] == "fail"
    assert rules.check_conduit(100, 100)[0]["result"] == "fail"


# ---------------------------------------------------------------- schema
def test_create_model(tmp_path):
    p = schema.create_model(str(tmp_path / "m.gpkg"))
    con = sqlite3.connect(p)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(schema.ALL_TABLES) <= names
    assert con.execute("PRAGMA application_id").fetchone()[0] == 1196444487
    assert con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='wwn_conduits'"
                       ).fetchone()[0] == 7850
    con.execute("INSERT INTO wwn_nodes(node_id) VALUES ('N1')")
    assert con.execute("SELECT status, data_source, node_type FROM wwn_nodes").fetchone() == (
        "proposed", "design", "ac")
    con.close()
    assert set(schema.table_identifiers(p)) >= set(schema.ALL_TABLES)
    with pytest.raises(FileExistsError):
        schema.create_model(p)
    schema.create_model(p, overwrite=True)


def test_enums_and_summary():
    e = schema.parse_enums()
    assert e["node_type"][0] == "ac" and "wet_well" in e["node_type"]
    assert e["region"] == ["south", "north"] and e["ground"] == ["dry", "wet"]
    fe = schema.field_enums()
    assert fe[("wwn_conduits", "material")][0] == "pvc"
    assert fe[("wwn_pumps", "role")] == ["duty", "assist", "standby"]
    s = schema.column_summary()
    for t in schema.ALL_TABLES:
        assert t in s
    assert "{ac|ms|" in s


# ---------------------------------------------------------------- checks / install
def test_parse_issues_variants():
    a = checks.parse_issues(json.dumps([{"severity": "Error", "code": "E1", "object_type": "conduit",
                                         "object_id": "C1", "clause": "4.2.1(m)", "message": "m"}]))
    assert a[0]["table"] == "wwn_conduits" and a[0]["severity"] == "error"
    b = checks.parse_issues(json.dumps({"issues": [{"level": "warning", "rule": "W", "kind": "node",
                                                    "id": "N1", "msg": "x"}]}))
    assert b[0]["table"] == "wwn_nodes" and b[0]["code"] == "W" and b[0]["object_id"] == "N1"
    assert checks.parse_issues("{}") == []


def test_read_issue_table(tmp_path):
    p = schema.create_model(str(tmp_path / "m.gpkg"))
    con = sqlite3.connect(p)
    con.execute("INSERT INTO wwn_issues(severity,code,object_type,object_id,clause,message) "
                "VALUES ('error','X','conduit','C9','c','msg')")
    con.commit()
    con.close()
    assert checks.read_issue_table(p)[0]["object_id"] == "C9"


def test_install_python_executable(monkeypatch, tmp_path):
    from wwnet.assistant import install

    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "qgis-bin.exe"))
    monkeypatch.setattr(sys, "exec_prefix", str(tmp_path))
    assert install.python_executable() == str(tmp_path / "python.exe")
    cmd = install.pip_command()
    assert cmd[1:] == ["-m", "pip", "install", "--user", "--upgrade", "anthropic"]
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.12")
    assert install.python_executable() == "/usr/bin/python3.12"
