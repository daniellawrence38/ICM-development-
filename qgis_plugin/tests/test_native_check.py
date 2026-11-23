import shutil
import sqlite3

import pytest

from conftest import REPO
from wwnet.core import checks, native, schema

pytest.importorskip("wwnet_native")


@pytest.fixture
def gpkg(tmp_path):
    dst = tmp_path / "demo.gpkg"
    shutil.copy(REPO / "examples" / "demo" / "demo.gpkg", dst)
    return str(dst)


def test_native_check_stores_issues_as_points(gpkg):
    native.get(refresh=True)
    issues, msg = checks.run_check(gpkg)
    assert msg.startswith("Model check:") and isinstance(issues, list)
    assert all(i["table"] in (None, "wwn_nodes", "wwn_conduits", "wwn_subcatchments", "wwn_pump_stations",
                              "wwn_pumps", "wwn_rising_mains") for i in issues)
    stored = checks.read_issue_table(gpkg)
    assert len(stored) == len(issues) and all(i.get("source") == "check" for i in stored)


def test_without_native_shows_stored_issues_and_hint(monkeypatch, gpkg):
    monkeypatch.setattr(native, "service", lambda: None)
    issues, msg = checks.run_check(gpkg)
    assert "wwnet_native" in msg and "Verify Setup" in msg


def test_require_reports_missing_and_old(monkeypatch):
    assert native.require("check") is not None
    with pytest.raises(native.NativeMissing, match="too old"):
        native.require("no_such_function")
    monkeypatch.setattr(native, "get", lambda refresh=False: None)
    with pytest.raises(native.NativeMissing, match="not installed"):
        native.require()
    assert native.missing_functions(object()) == list(native.SERVICE_FUNCTIONS)


def test_create_and_upgrade_through_engine(tmp_path, gpkg):
    p = schema.create_model(str(tmp_path / "m.gpkg"))
    assert schema.schema_version(p) == schema.SCHEMA_VERSION
    with pytest.raises(FileExistsError):
        schema.create_model(p)
    schema.create_model(p, overwrite=True)
    # an older file is upgraded: result tables become layers
    con = sqlite3.connect(gpkg)
    con.execute("UPDATE wwn_meta SET value='2' WHERE key='schema_version'")
    con.commit()
    con.close()
    assert schema.upgrade_model(gpkg) is True
    assert schema.upgrade_model(gpkg) is False
    ids = schema.table_identifiers(gpkg)
    assert ids["wwn_issues"][1] == "features" and ids["wwn_results_conduits"][1] == "features"
