import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # qgis_plugin/
sys.path.insert(0, str(ROOT))
REPO = ROOT.parent
BENCH = REPO / "tests" / "benchmarks" / "ds50_table44_colebrook.csv"
