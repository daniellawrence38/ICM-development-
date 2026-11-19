import os

import pytest

from wwnet_arcgis import cli, gpkg

TRACE_TEXT = "upstream of N06 (3):\n  N05\n  N04\n  N03\ndownstream of N06 (1):\n  N07\n"


def test_parse_trace_text():
    assert cli.parse_trace(TRACE_TEXT) == {"upstream": ["N05", "N04", "N03"], "downstream": ["N07"]}
    assert cli.parse_trace("error: node 'ZZ' not found") == {}


def test_find_exe_order(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    bundled = root / "bin" / "wwnet.exe"
    bundled.write_text("x")
    onpath = tmp_path / "pathdir"
    onpath.mkdir()
    pexe = onpath / ("wwnet.exe" if os.name == "nt" else "wwnet")
    pexe.write_text("x")
    pexe.chmod(0o755)
    cfg = tmp_path / "configured.exe"
    cfg.write_text("x")
    monkeypatch.setenv("PATH", str(onpath))
    assert cli.find_exe(str(cfg), root) == str(cfg)                       # 1. setting
    assert cli.find_exe("", root) == str(pexe)                            # 2. PATH
    monkeypatch.setenv("PATH", "")
    assert cli.find_exe("", root) == str(bundled)                         # 3. bundled
    with pytest.raises(cli.CliError, match="bin"):
        cli.find_exe("", tmp_path / "empty")
    with pytest.raises(cli.CliError, match="Configured"):
        cli.find_exe(str(tmp_path / "missing.exe"), root)


def test_run_check_real(no_native, wwnet_exe, demo_gpkg):
    issues, rc = cli.run_check(demo_gpkg, wwnet_exe)
    assert rc == 0
    assert issues and issues[0]["code"] == "INVERT_MISMATCH"
    assert issues[0]["table"] == "wwn_conduits" and issues[0]["severity"] == "info"


def test_run_design_real(no_native, wwnet_exe, demo_gpkg):
    d = cli.run_design(demo_gpkg, wwnet_exe)
    assert set(d) >= {"conduit_results", "node_flows", "issues", "summary"}
    assert d["summary"]["conduits"] == len(d["conduit_results"]) == 11
    assert cli.failed_conduits(d) == []
    c = d["conduit_results"][0]
    assert c["checks"][0]["clause"].startswith("DS50")


def test_run_trace_real(no_native, wwnet_exe, demo_gpkg):
    up = cli.run_trace(demo_gpkg, "N06", up=True, exe=wwnet_exe)
    assert "N05" in up["upstream"] and "downstream" not in up
    both = cli.run_trace(demo_gpkg, "N03", up=True, down=True, exe=wwnet_exe)
    assert "OUT1" in both["downstream"] and "N02" in both["upstream"]
    with pytest.raises(cli.CliError, match="not found"):
        cli.run_trace(demo_gpkg, "ZZ", exe=wwnet_exe)
    conduits = gpkg.conduits_between(demo_gpkg, {"N03", "N04", "N05"})
    assert conduits == ["P03", "P04"] or len(conduits) == 2


def test_unparseable_output(no_native, tmp_path):
    script = tmp_path / "fake.py"
    script.write_text("import sys; print('not json'); sys.exit(3)")
    import sys
    wrapper = tmp_path / ("w.bat" if os.name == "nt" else "w.sh")
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)
    if os.name == "nt":
        pytest.skip("shell wrapper test is POSIX only")
    with pytest.raises(cli.CliError, match="unparseable"):
        cli.run_check("x.gpkg", str(wrapper))
