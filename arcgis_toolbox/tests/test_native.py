"""The engine (wwnet_native) is the only backend: the toolbox's engine wrapper and the native API."""
import json
import shutil
import sqlite3
import subprocess

import pytest

from conftest import REPO
from wwnet_arcgis import engine

nat = pytest.importorskip("wwnet_native")


def _copy(src, tmp_path, name):
    dst = tmp_path / name
    shutil.copy(src, dst)
    return str(dst)


def test_native_matches_cli(wwnet_exe, demo_gpkg, tmp_path):
    """The binding and the developer CLI are the same Rust services."""
    a, b = _copy(demo_gpkg, tmp_path, "a.gpkg"), _copy(demo_gpkg, tmp_path, "b.gpkg")
    cli = lambda *args: json.loads(subprocess.run([wwnet_exe, *args], capture_output=True, text=True).stdout)  # noqa: E731
    assert nat.check(a) == cli("check", b, "--json")
    assert nat.design(a) == cli("design", b, "--json")
    assert set(nat.trace(a, "N03", True, False)) == {"upstream", "conduits", "summary"}
    assert nat.version() and nat.SCHEMA_VERSION == 3


def test_native_errors(demo_gpkg, tmp_path):
    with pytest.raises(KeyError, match="not found"):
        nat.trace(demo_gpkg, "ZZ", True, True)
    with pytest.raises(RuntimeError, match="loading"):
        nat.check(str(tmp_path / "missing.gpkg"))
    with pytest.raises(Exception, match="loading"):
        nat.design(demo_gpkg, str(tmp_path / "missing.toml"))
    p = str(tmp_path / "new.gpkg")
    nat.new_model(p)
    assert nat.check(p, False) is not None and nat.schema_version(p) == 3


def test_engine_wrapper_maps_errors(demo_gpkg, monkeypatch):
    assert engine.run_design(demo_gpkg)["summary"]["conduits"] > 0
    assert all("table" in i for i in engine.run_check(demo_gpkg))
    assert engine.run_trace(demo_gpkg, "N03", up=True, down=True)["downstream"]
    with pytest.raises(engine.EngineError, match="neither a node nor a conduit"):
        engine.run_trace(demo_gpkg, "ZZ")
    with pytest.raises(engine.EngineError, match="no stored simulation runs"):
        engine.show_sim_run(demo_gpkg)
    from wwnet_arcgis import shared
    monkeypatch.setattr(shared.core("native"), "get", lambda refresh=False: None)
    with pytest.raises(engine.EngineError, match="Verify Setup"):
        engine.run_design(demo_gpkg)
    assert engine.available() is False


def test_native_simulate_runs_and_settings(demo_gpkg, tmp_path):
    inp = tmp_path / "m.inp"
    r = nat.simulate(demo_gpkg, duration_h=12.0, warmup_h=6.0, write_inp=str(inp), series_ids=["P01"],
                     label="base")
    assert r["summary"]["conduits"] == 11 and abs(r["summary"]["continuity_error_pct"]) < 1.0
    assert "P01" in r["series_ids"] and r["run_id"] and r["label"] == "base"
    assert r["options"]["roughness"] == "ks"
    assert "[CONDUITS]" in inp.read_text()
    con = sqlite3.connect(demo_gpkg)
    assert con.execute("select count(*) from wwn_sim_links").fetchone() == (11,)
    assert con.execute("select count(*) from wwn_sim_conduit_results where geom is not null").fetchone() == (11,)
    con.close()
    nowrite = nat.simulate(demo_gpkg, duration_h=12.0, warmup_h=6.0, write=False, flow_scale=2.0)
    assert nowrite["run_id"] is None and nowrite["summary"]["inflow_simulated_lps"] == pytest.approx(
        2 * nowrite["summary"]["inflow_adwf_lps"])
    with pytest.raises(ValueError, match="roughness"):
        nat.simulate(demo_gpkg, roughness="chezy", write=False)
    with pytest.raises(RuntimeError, match="loading"):
        nat.simulate(str(tmp_path / "missing.gpkg"))
    runs = engine.sim_runs(demo_gpkg)
    assert len(runs) == 1 and runs[0]["label"] == "base" and runs[0]["shown"]
    st = engine.set_settings(demo_gpkg, 500.0, None, None)
    assert st["adwf_per_connection_lpd"] == 500.0 and st["peaking_factor"] == 1.5
    with pytest.raises(engine.EngineError, match="profile"):
        engine.set_settings(demo_gpkg, default_profile="nope")
    cap = nat.pipe_capacity(225, 300, 1.0, 0.6667)
    assert cap["capacity_lps"] == pytest.approx(21.7, abs=0.15)
    assert REPO.is_dir()
