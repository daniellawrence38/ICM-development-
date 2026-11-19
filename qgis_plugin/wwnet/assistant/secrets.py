"""API-key storage: QGIS authentication database (encrypted), QgsSettings fallback.

UNVERIFIED without QGIS: the QgsAuthManager calls below follow the PyQGIS 3.x API but
could not be exercised in this build environment.
"""
from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

CFG_ID_KEY = "wwnet/ai/auth_cfg_id"
PLAIN_KEY = "wwnet/ai/api_key_plaintext"
HEADER = "x-api-key"


def _unpack(res):
    return res[0] if isinstance(res, tuple) else res


def auth_available() -> bool:
    am = QgsApplication.authManager()
    if am is None or am.isDisabled():
        return False
    if am.masterPasswordIsSet():
        return True
    try:
        return bool(am.setMasterPassword(True))   # prompts the user if needed
    except Exception:
        return False


def save_api_key(key: str):
    """Store the key. Returns (location, warning_or_None); location is 'authdb' or 'settings'."""
    s = QgsSettings()
    if not key:
        delete_api_key()
        return "none", None
    if auth_available():
        am = QgsApplication.authManager()
        cfg = QgsAuthMethodConfig()
        existing = s.value(CFG_ID_KEY, "")
        if existing:
            am.loadAuthenticationConfig(existing, cfg, True)
        cfg.setName("WWNet Anthropic API key")
        cfg.setMethod("APIHeader")
        cfg.setConfigMap({HEADER: key})
        res = am.updateAuthenticationConfig(cfg) if existing and cfg.id() else \
            am.storeAuthenticationConfig(cfg)
        if _unpack(res) and cfg.id():
            s.setValue(CFG_ID_KEY, cfg.id())
            s.remove(PLAIN_KEY)
            return "authdb", None
    s.setValue(PLAIN_KEY, key)
    return "settings", ("The QGIS master password is not set (or the auth database is "
                        "unavailable), so the API key was stored UNENCRYPTED in your QGIS "
                        "settings. Set a master password and re-save the key to encrypt it.")


def load_api_key() -> str:
    s = QgsSettings()
    cfg_id = s.value(CFG_ID_KEY, "")
    if cfg_id and auth_available():
        am = QgsApplication.authManager()
        cfg = QgsAuthMethodConfig()
        if _unpack(am.loadAuthenticationConfig(cfg_id, cfg, True)):
            m = cfg.configMap()
            key = m.get(HEADER) or m.get("password") or ""
            if key:
                return key
    key = s.value(PLAIN_KEY, "")
    return key or os.environ.get("ANTHROPIC_API_KEY", "")


def has_stored_key() -> bool:
    s = QgsSettings()
    return bool(s.value(CFG_ID_KEY, "") or s.value(PLAIN_KEY, ""))


def delete_api_key():
    s = QgsSettings()
    cfg_id = s.value(CFG_ID_KEY, "")
    if cfg_id:
        try:
            QgsApplication.authManager().removeAuthenticationConfig(cfg_id)
        except Exception:
            pass
    s.remove(CFG_ID_KEY)
    s.remove(PLAIN_KEY)
