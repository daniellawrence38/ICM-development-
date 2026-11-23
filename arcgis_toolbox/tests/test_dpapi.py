import ctypes
import os

import pytest

from wwnet_arcgis import dpapi, settings


class FakeDpapi:
    """Stands in for ctypes.windll: 'encrypts' by XOR so plaintext never appears on disk."""

    def __init__(self):
        self.keep = []
        self.freed = []
        self.flags = []
        self.crypt32 = type("C", (), {})()
        self.kernel32 = type("K", (), {})()
        self.crypt32.CryptProtectData = lambda *a: self._do(a, 0x5A)
        self.crypt32.CryptUnprotectData = lambda *a: self._do(a, 0x5A, unprotect=True)
        self.kernel32.LocalFree = lambda p: self.freed.append(p)

    def _do(self, a, k, unprotect=False):
        src, out = a[0]._obj, a[-1]._obj
        self.flags.append(a[-2])
        data = ctypes.string_at(src.pbData, src.cbData)
        if unprotect and not data.startswith(b"ENC:"):
            return 0
        res = bytes(b ^ k for b in data) if unprotect is False else bytes(b ^ k for b in data[4:])
        if not unprotect:
            res = b"ENC:" + res
        buf = ctypes.create_string_buffer(res, len(res))
        self.keep.append(buf)
        out.cbData = len(res)
        out.pbData = ctypes.cast(buf, ctypes.c_void_p).value
        return 1


@pytest.fixture
def fake_win(monkeypatch):
    f = FakeDpapi()
    monkeypatch.setattr(ctypes, "windll", f, raising=False)
    return f


def test_roundtrip_current_user_scope(fake_win):
    dpapi.save_key("sk-ant-secret-123")
    raw = dpapi.key_path().read_bytes()
    assert b"sk-ant-secret-123" not in raw
    assert dpapi.stored_key() == "sk-ant-secret-123"
    assert dpapi.get_api_key() == ("sk-ant-secret-123", "dpapi")
    assert fake_win.flags and all(f == dpapi.CRYPTPROTECT_UI_FORBIDDEN for f in fake_win.flags)  # no LOCAL_MACHINE
    assert len(fake_win.freed) == 3                      # 1 protect + 2 unprotect buffers released
    assert dpapi.key_path().parent.name == "WWNet"


def test_env_var_takes_precedence(fake_win, monkeypatch):
    dpapi.save_key("stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", " envkey ")
    assert dpapi.get_api_key() == ("envkey", "env")


def test_clear_and_missing(fake_win):
    assert dpapi.get_api_key() == (None, None)
    dpapi.save_key("k")
    assert dpapi.clear_key() is True
    assert dpapi.clear_key() is False
    assert dpapi.get_api_key() == (None, None)


def test_unavailable_off_windows():
    if hasattr(ctypes, "windll"):
        pytest.skip("running on Windows")
    with pytest.raises(dpapi.DpapiError):
        dpapi.save_key("k")
    assert dpapi.stored_key() is None


def test_corrupt_file_is_ignored(fake_win):
    dpapi.key_path().write_bytes(b"garbage")
    assert dpapi.stored_key() is None


def test_empty_key_rejected(fake_win):
    with pytest.raises(dpapi.DpapiError):
        dpapi.save_key("  ")


def test_settings_json_roundtrip():
    assert settings.load()["model"] == "claude-opus-5-5"
    settings.save(model="claude-sonnet-5-5", effort="high")
    cur = settings.load()
    assert cur["model"] == "claude-sonnet-5-5" and cur["effort"] == "high"
    assert settings.settings_path().parent == settings.appdata_dir()
    p = settings.history_path("a|b")
    assert p.parent.name == "history" and p.suffix == ".json" and p == settings.history_path("a|b")
    assert os.path.basename(str(p.parent.parent)) == "WWNet"


def test_set_settings_never_echoes_key(fake_win):
    from wwnet_arcgis import tools_impl
    from wwnet_arcgis.messages import Messages
    import fake_arcpy
    a = fake_arcpy.make_fake()
    tools_impl.set_settings(Messages(a), "sk-ant-TOPSECRET", False, "claude-haiku-5-5", "low")
    text = "\n".join(t for _, t in a.log)
    assert "TOPSECRET" not in text and "stored encrypted" in text
    assert settings.load()["model"] == "claude-haiku-5-5"
