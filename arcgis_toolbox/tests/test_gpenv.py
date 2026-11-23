"""Geoprocessing history / metadata logging control (wwnet_arcgis.gpenv.quiet) and its use by the tools."""
import types

import pytest

from wwnet_arcgis import gpenv, tools_impl
from wwnet_arcgis.messages import Messages


class _Arc:
    """Minimal arcpy stand-in with the logging functions; records every Set call."""

    def __init__(self, hist=True, meta=True):
        self.hist, self.meta = hist, meta
        self.sets = []

    def GetLogHistory(self):
        return self.hist

    def GetLogMetadata(self):
        return self.meta

    def SetLogHistory(self, v):
        self.sets.append(("history", v))
        self.hist = bool(v)

    def SetLogMetadata(self, v):
        self.sets.append(("metadata", v))
        self.meta = bool(v)


class _Broken(_Arc):
    def SetLogHistory(self, v):
        raise OSError("history cannot be set")

    def SetLogMetadata(self, v):
        raise OSError("metadata cannot be set")


def test_quiet_turns_both_off_and_restores_originals():
    a = _Arc(hist=True, meta=False)
    with gpenv.quiet(a):
        assert a.hist is False and a.meta is False
    assert a.hist is True and a.meta is False
    assert ("history", False) in a.sets and ("metadata", False) in a.sets


def test_quiet_nested_inner_restores_outer_state_and_outer_restores_original():
    a = _Arc(hist=True, meta=True)
    with gpenv.quiet(a):
        with gpenv.quiet(a):
            assert a.hist is False and a.meta is False
        assert a.hist is False and a.meta is False      # inner exit must not re-enable logging
    assert a.hist is True and a.meta is True


def test_quiet_arcpy_without_get_set_functions_works():
    body = []
    with gpenv.quiet(types.SimpleNamespace()):
        body.append(1)
    assert body == [1]


def test_quiet_arcpy_with_only_getters_works_and_sets_nothing():
    a = types.SimpleNamespace(GetLogHistory=lambda: True, GetLogMetadata=lambda: True)
    with gpenv.quiet(a):
        pass                                     # no Set functions: nothing to call, must not raise


def test_quiet_unreadable_setting_is_left_alone():
    a = _Arc()
    a.GetLogMetadata = lambda: None              # arcpy reports no value: leave metadata alone
    with gpenv.quiet(a):
        assert a.hist is False and a.meta is True
    assert a.hist is True and a.meta is True
    assert ("metadata", False) not in a.sets and ("metadata", True) not in a.sets


def test_quiet_get_raising_is_treated_as_unreadable():
    a = _Arc()

    def boom():
        raise RuntimeError("no log settings here")
    a.GetLogHistory = boom
    with gpenv.quiet(a):
        assert a.meta is False
    assert a.meta is True
    assert not [s for s in a.sets if s[0] == "history"]


def test_quiet_exception_inside_block_still_restores_and_propagates():
    a = _Arc(hist=True, meta=True)
    with pytest.raises(ValueError, match="boom"):
        with gpenv.quiet(a):
            assert a.hist is False
            raise ValueError("boom")
    assert a.hist is True and a.meta is True


def test_quiet_exceptions_from_set_calls_are_swallowed():
    a = _Broken(hist=True, meta=True)
    ran = []
    with gpenv.quiet(a):                         # must not raise although both Set calls fail
        ran.append(True)
    assert ran == [True]
    assert a.hist is True and a.meta is True     # nothing was changed, nothing raised


def test_quiet_exception_from_restore_is_swallowed_and_body_exception_wins():
    a = _Arc(hist=True, meta=True)
    real_set = a.SetLogHistory

    def flaky(v):
        if v is True:                            # fails only on the restore
            raise OSError("restore failed")
        real_set(v)
    a.SetLogHistory = flaky
    with pytest.raises(KeyError):                # the body's exception, not the restore's OSError
        with gpenv.quiet(a):
            raise KeyError("body")
    assert a.hist is False                       # history restore failed (swallowed): left off
    assert a.meta is True                        # the other setting is still restored


# ----------------------------------------------------------------- tool-level

def _assert_logging_quiet_then_restored(a):
    assert ("history", False) in a.log_settings and ("metadata", False) in a.log_settings
    assert a.log_history is True and a.log_metadata is True


def test_new_model_add_to_map_is_quiet(fake_arcpy, tmp_path):
    a = fake_arcpy([])
    tools_impl.new_model(a, Messages(a), str(tmp_path / "q"), True)
    _assert_logging_quiet_then_restored(a)


def test_add_model_to_map_is_quiet(fake_arcpy, demo_gpkg):
    a = fake_arcpy([])
    tools_impl.add_model_to_map(a, Messages(a), demo_gpkg)
    _assert_logging_quiet_then_restored(a)


def test_run_design_is_quiet(fake_arcpy, wwnet_exe, demo_gpkg):
    from test_tools_impl import mk
    a, _ = mk(fake_arcpy, demo_gpkg)
    tools_impl.run_design(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    _assert_logging_quiet_then_restored(a)


def test_trace_is_quiet(fake_arcpy, wwnet_exe, demo_gpkg):
    from test_tools_impl import mk
    a, layers = mk(fake_arcpy, demo_gpkg)
    tools_impl.trace(a, Messages(a), layers[0], "N03", "Downstream", True, wwnet_exe)
    _assert_logging_quiet_then_restored(a)


def test_check_model_table_write_is_quiet(fake_arcpy, wwnet_exe, demo_gpkg, tmp_path):
    a = fake_arcpy([])
    tools_impl.check_model(a, Messages(a), demo_gpkg, str(tmp_path / "o.gdb" / "issues"), wwnet_exe)
    _assert_logging_quiet_then_restored(a)


def test_run_simulation_layers_are_quiet(fake_arcpy, wwnet_exe, demo_gpkg):
    a = fake_arcpy([])
    tools_impl.run_simulation(a, Messages(a), demo_gpkg, exe=wwnet_exe)
    _assert_logging_quiet_then_restored(a)
