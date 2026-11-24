"""WWNet ArcGIS Pro toolbox logic. Everything here is importable without arcpy
(arcpy is imported lazily, inside functions), so it can be unit tested anywhere."""
__version__ = "0.1.0"

from pathlib import Path as _Path

from . import vendor as _vendor

# No-install bundle: put <toolbox>/vendor first on sys.path before anything imports wwnet_native.
_vendor.activate(_Path(__file__).resolve().parent.parent)
