"""Smoke test of an extracted bundle inside a "Pro-like" Python (used by CI and tests/test_vendor.py).

usage: python bundle_smoke.py <extracted WWNet-ArcGIS dir> <site-packages of this python> [native|no]
The interpreter must already contain anyio 4.2.0, typing_extensions 4.9.0, sniffio, idna, certifi (Pro's set).
"""
import os
import sys

BUNDLE, SITE = sys.argv[1], sys.argv[2]
NATIVE = len(sys.argv) > 3 and sys.argv[3] == "native"


def under(mod, root):
    n = os.path.normcase
    return n(os.path.abspath(mod.__file__)).startswith(n(os.path.abspath(root)))


import typing_extensions                       # simulate Pro: the old copy is already loaded
assert under(typing_extensions, SITE), typing_extensions.__file__
sys.path.insert(0, BUNDLE)
from wwnet_arcgis import vendor
vendor.activate(BUNDLE)
import anthropic, anyio, sniffio, idna, certifi
client = anthropic.Anthropic(api_key="x")
import typing_extensions as te
assert under(te, os.path.join(BUNDLE, "vendor")), te.__file__
assert hasattr(te, "Sentinel")
for m in (anyio, sniffio, idna, certifi):       # Pro's copies must stay in use
    assert under(m, SITE), (m.__name__, m.__file__)
if NATIVE:
    import wwnet_native  # noqa: F401
print("SMOKE-OK", anthropic.__version__)
