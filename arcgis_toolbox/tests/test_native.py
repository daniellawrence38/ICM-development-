"""wwnet_native is the first choice; the CLI is only a fallback."""
import json
import shutil
import subprocess

import pytest

from conftest import REPO
from wwnet_arcgis import cli

nat = pytest.importorskip("wwnet_native")


def _copy(demo_gpkg, tmp_path, name):
    dst = tmp_path / name
    shutil.copy(demo_gpkg, dst)
    return str(dst)


def _cli_json(exe, *args):
    return json.loads(subprocess.run([exe, *args], capture_output=True, text=True).stdout)


def test_native_matches_cli(wwnet_exe, demo_gpkg, tmp_path):
    a, b = _copy(demo_gpkg, tmp_path, "a.gpkg"), _copy(demo_gpkg, tmp_path, "b.gpkg")
    assert nat.check(a) == _cli_json(wwnet_exe, "check", b, "--json")
    assert nat.design(a) == _cli_json(wwnet_exe, "design", b, "--json")
    t = subprocess.run([wwnet_exe, "trace", a, "N03"], capture_output=True, text=True).stdout
    assert nat.trace(a, "N03", False, False) == cli.parse_trace(t)
    assert set(nat.trace(a, "N03", True, False)) == {"upstream"}
    assert nat.version()


def test_native_errors(demo_gpkg, tmp_path):
    with pytest.raises(KeyError, match="not found"):
        nat.trace(demo_gpkg, "ZZ", True, True)
    with pytest.raises(RuntimeError, match="loading"):
        nat.check(str(tmp_path / "missing.gpkg"))
    with pytest.raises(Exception, match="loading"):
        nat.design(demo_gpkg, str(tmp_path / "missing.toml"))
    p = str(tmp_path / "new.gpkg")
    nat.new_model(p)
    assert nat.check(p, False) is not None


def test_frontend_prefers_native(monkeypatch, demo_gpkg):
    def boom(*a, **k):
        raise AssertionError("CLI used although native is available")
    monkeypatch.setattr(cli, "_run", boom)
    monkeypatch.setattr(cli, "find_exe", boom)
    issues, rc = cli.run_check(demo_gpkg)
    assert rc == 0 and all("table" in i for i in issues)
    assert cli.run_design(demo_gpkg)["summary"]["conduits"] > 0
    assert cli.run_trace(demo_gpkg, "N03", up=True, down=True)["downstream"]
    with pytest.raises(cli.CliError, match="not found"):
        cli.run_trace(demo_gpkg, "ZZ")


def test_cli_only_as_fallback(monkeypatch, wwnet_exe, demo_gpkg):
    monkeypatch.setattr(cli, "native_service", lambda: None)
    calls = []
    real = cli._run
    monkeypatch.setattr(cli, "_run", lambda args, timeout=600: calls.append(args[1]) or real(args, timeout))
    cli.run_check(demo_gpkg, wwnet_exe)
    cli.run_design(demo_gpkg, wwnet_exe)
    cli.run_trace(demo_gpkg, "N03", exe=wwnet_exe)
    assert calls == ["check", "design", "trace"]


def test_neither_available_explains_wheel(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "native_service", lambda: None)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(cli, "TOOLBOX_ROOT", tmp_path)
    for fn in (lambda: cli.run_check("x.gpkg", None), lambda: cli.run_design("x.gpkg"),
               lambda: cli.run_trace("x.gpkg", "N1")):
        with pytest.raises(cli.CliError) as ei:
            fn()
        msg = str(ei.value)
        assert "wwnet_native" in msg and "pip install" in msg and "no .exe required" in msg
