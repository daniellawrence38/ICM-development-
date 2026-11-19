import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

TOOLBOX = Path(__file__).resolve().parents[1]
REPO = TOOLBOX.parent
sys.path.insert(0, str(TOOLBOX))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def fake_arcpy(monkeypatch):
    import fake_arcpy as fa

    def factory(layers=None):
        a = fa.make_fake(layers)
        monkeypatch.setitem(sys.modules, "arcpy", a)
        return a
    return factory


@pytest.fixture
def demo_gpkg(tmp_path):
    dst = tmp_path / "demo.gpkg"
    shutil.copy(REPO / "examples" / "demo" / "demo.gpkg", dst)
    return str(dst)


def _find_exe():
    env = os.environ.get("WWNET_EXE")
    if env and os.path.isfile(env):
        return env
    for sub in ("debug", "release"):
        p = REPO / "target" / sub / ("wwnet.exe" if os.name == "nt" else "wwnet")
        if p.is_file():
            return str(p)
    return shutil.which("wwnet")


@pytest.fixture
def no_native(monkeypatch):
    """Simulate wwnet_native being unavailable so the CLI fallback is exercised."""
    from wwnet_arcgis import cli
    monkeypatch.setattr(cli, "native_service", lambda: None)


@pytest.fixture(scope="session")
def wwnet_exe():
    exe = _find_exe()
    if not exe:
        pytest.skip("wwnet CLI not built (cargo build -p wwnet-cli, or set WWNET_EXE)")
    return exe


def rows(gpkg, sql, *args):
    con = sqlite3.connect(gpkg)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: builds a vendor dir / venv with pip (needs network)")
