import shutil
import subprocess

import pytest

from conftest import REPO
from wwnet.core import checks, native

pytest.importorskip("wwnet_native")


@pytest.fixture
def gpkg(tmp_path):
    dst = tmp_path / "demo.gpkg"
    shutil.copy(REPO / "examples" / "demo" / "demo.gpkg", dst)
    return str(dst)


def test_native_first(monkeypatch, gpkg):
    native.get(refresh=True)
    monkeypatch.setattr(checks, "find_cli", lambda: pytest.fail("CLI probed although native present"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("CLI run"))
    issues, msg = checks.run_check(gpkg)
    assert "native" in msg and isinstance(issues, list)
    assert all(i["table"] in (None, "wwn_nodes", "wwn_conduits", "wwn_subcatchments", "wwn_pump_stations",
                              "wwn_pumps", "wwn_rising_mains") for i in issues)


def test_cli_fallback_only_when_native_missing(monkeypatch, gpkg):
    monkeypatch.setattr(native, "service", lambda: None)
    seen = {}

    class P:
        stdout = "[]"
        stderr = ""
        returncode = 0
    monkeypatch.setattr(checks, "find_cli", lambda: "/fake/wwnet")
    monkeypatch.setattr(subprocess, "run", lambda args, **k: seen.setdefault("args", args) and P())
    issues, msg = checks.run_check(gpkg)
    assert seen["args"][:2] == ["/fake/wwnet", "check"] and issues == []


def test_neither_available_hint(monkeypatch, gpkg):
    monkeypatch.setattr(native, "service", lambda: None)
    monkeypatch.setattr(checks, "find_cli", lambda: None)
    issues, msg = checks.run_check(gpkg)
    assert "wwnet_native" in msg and "pip install" in msg and "no .exe needed" in msg
