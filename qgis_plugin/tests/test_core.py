import csv
import json
import sqlite3
import sys

import pytest

from conftest import BENCH, REPO
from wwnet.core import calc, checks, native, ruleset, schema, style, summary, tomlmini


def _bench_rows():
    with open(BENCH, newline="") as fh:
        return list(csv.DictReader(l for l in fh if not l.startswith("#")))


@pytest.mark.parametrize("row", _bench_rows(), ids=lambda r: f"DN{r['dn']}-1in{r['grade_1in']}")
def test_capacity_matches_benchmark(row):
    """The calculator (engine) reproduces the Table 4.4 Colebrook benchmark."""
    pytest.importorskip("wwnet_native")
    r = calc.pipe_capacity(int(row["dn"]), float(row["grade_1in"]), 1.0, float(row["d_over_D"]))
    assert r["capacity_lps"] == pytest.approx(float(row["colebrook_lps"]), abs=0.15)


def test_capacity_any_dn_flow_and_surcharge():
    pytest.importorskip("wwnet_native")
    r = calc.pipe_capacity(100, 60, flow_lps=2.0)          # outside DS 50 Table 4.4: still calculated
    assert r["full_flow_lps"] > 2.0 and 0 < r["depth_ratio_at_flow"] < 1
    big = calc.pipe_capacity(300, 400, flow_lps=1e4)
    assert big["depth_ratio_at_flow"] is None and "surcharge" in big["note"]
    assert calc.pipe_capacity(225, 300)["capacity_lps"] == calc.pipe_capacity(225, 300)["full_flow_lps"]
    with pytest.raises(ValueError):
        calc.pipe_capacity(225, 0)
    chk = calc.check_conduit(100, 60, gsdf_lps=1)
    assert chk["in_ds50_range"] is False and chk["results"][0]["result"] == "fail"
    lines = calc.capacity_lines({**r, "ds50_checks": chk})
    assert any("outside DS 50 Table 4.4" in ln for ln in lines)


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
    assert r["source"] == "wwnet" and r["in_ds50_range"]
    by_rule = {c["rule"]: c["result"] for c in r["results"]}
    assert by_rule["max_flow"] == "fail" and by_rule["min_dn"] == "fail"


def test_lookup_and_gsdf():
    pytest.importorskip("wwnet_native")
    assert ruleset.lookup("grades", "225")["min_grade_1in"] == 300
    assert ruleset.lookup("residential", "south:R20")[0]["dry_gsdf_lps_ha"] == 0.219
    with pytest.raises(KeyError):
        ruleset.lookup("nope")
    r = ruleset.residential_gsdf(10, "R20", "dry", "south")
    assert r["gsdf_lps"] == pytest.approx(2.19, rel=0.01) and r["source"] == "wwnet"
    with pytest.raises(ValueError):
        ruleset.residential_gsdf(1, "R1000", "dry", "south")


def test_shared_palette_and_summary_text():
    assert style.rgb("#2e9e44") == [46, 158, 68]
    assert set(style.SIM_CLASS) == {"low", "medium", "high", "surcharged"}
    assert style.dn_colour(525) != style.DEFAULT and style.dn_colour("x") == style.DEFAULT
    d = {"conduit_results": [
        {"conduit_id": "C1", "dn": 150, "grade_1in": 100, "total_lps": 9, "capacity_lps": 8, "d_over_d": 0.6,
         "result": "fail", "checks": [{"rule": "max_flow", "clause": "T4.4", "result": "fail", "message": "too much"}]},
        {"conduit_id": "C2", "dn": 150, "grade_1in": 100, "result": "pass", "checks": []}],
        "summary": {"conduits": 2, "pass": 1, "warn": 0, "fail": 1, "terminal_gsdf_lps": 9,
                    "total_local_gsdf_lps": 9}}
    assert summary.failed_conduits(d) == ["C1"]
    w = summary.design_worst_lines(d["conduit_results"])
    assert len(w) == 1 and "C1 DN150 1:100: FAIL" in w[0] and "too much" in w[0]
    assert summary.design_detail_lines(d["conduit_results"])[0].startswith("max_flow (T4.4): 1 conduit(s)")
    assert "1 fail" in summary.design_summary_lines(d)[0]
    t = summary.trace_lines({"upstream": ["A", "B"], "conduits": ["P1", "P2"],
                             "summary": {"start_node": "N", "start_conduit": "P0", "upstream_nodes": 2,
                                         "adwf_lps": 1.5, "gsdf_lps": 2.25}}, "P0")
    assert t[0].startswith("Upstream of P0: 2 node(s)") and "ADWF 1.500 L/s" in t[-1]
    assert summary.runs_lines([])[0].startswith("No stored")
    assert summary.issue_line({"code": "X", "object_type": "node", "object_id": "N1", "clause": None,
                               "message": "m"}) == "X node N1: m"


# ---------------------------------------------------------------- schema
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
def test_normalise_issue_variants():
    a = checks.normalise_issue({"severity": "Error", "code": "E1", "object_type": "conduit",
                                "object_id": "C1", "clause": "4.2.1(m)", "message": "m"})
    assert a["table"] == "wwn_conduits" and a["severity"] == "error"
    b = checks.normalise_issue({"level": "warn", "rule": "W", "kind": "node", "id": "N1", "msg": "x",
                                "source": "design"})
    assert b["table"] == "wwn_nodes" and b["code"] == "W" and b["severity"] == "warning"
    assert b["source"] == "design"
    assert checks.count_by_severity([a, b, {"severity": "odd"}]) == {"error": 1, "warning": 1, "info": 1}


def test_read_issue_table(tmp_path):
    pytest.importorskip("wwnet_native")
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


def test_native_miss_is_not_cached(monkeypatch):
    """A failed probe must be retried later (the module may become importable)."""
    calls = []
    real = native.importlib.import_module

    def fake(name):
        calls.append(name)
        raise ImportError(name)
    monkeypatch.setattr(native, "_cache", {})
    monkeypatch.setattr(native.importlib, "import_module", fake)
    assert native.get() is None
    n = len(calls)
    assert native.get() is None and len(calls) > n
    monkeypatch.setattr(native.importlib, "import_module", real)
