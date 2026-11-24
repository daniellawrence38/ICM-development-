"""Smoke test of an extracted bundle (used by CI): the vendor folder activates and wwnet_native imports.

usage: python bundle_smoke.py <extracted WWNet-ArcGIS dir> [native|no]
"""
import os
import sys

BUNDLE = sys.argv[1]
NATIVE = len(sys.argv) > 2 and sys.argv[2] == "native"
sys.path.insert(0, BUNDLE)
from wwnet_arcgis import vendor  # noqa: E402
vendor.activate(BUNDLE)
assert not os.path.isdir(os.path.join(BUNDLE, "vendor", "anthropic")), "assistant packages must not be shipped"
assert not os.path.isdir(os.path.join(BUNDLE, "wwnet_shared", "assistant")), "assistant modules must not be shipped"
if NATIVE:
    import wwnet_native
    assert wwnet_native.SCHEMA_VERSION >= 3, wwnet_native.SCHEMA_VERSION
    print("wwnet_native", wwnet_native.version())
print("SMOKE-OK")
