"""Writes examples/demo/pump_register_demo.xlsx: a small pump station register laid out like the real one
(title row, header row, data; SAP functional location in column C) for the Self Test pump station PS1 plus the
register-only station PS9, two pump rows per station.

    python arcgis_toolbox/tests/make_pump_register_demo.py [output.xlsx]

Levels are depths below the top of slab, consistent with the Self Test pump scenario (wet well node WW:
invert 22.2, ground 25.5 m AHD): floor 25.5 - 3.6 = 21.9 (below the node invert), stop 22.5, start 23.0,
alarm 24.0, overflow 24.5.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xlsx_fixture import write_xlsx  # noqa: E402

DEFAULT_OUT = HERE.parents[1] / "examples" / "demo" / "pump_register_demo.xlsx"
FL = "Functional Location"

# (functional location, ground m AHD, overflow, stop, alarm, start, base) depths m below top of slab
LEVELS = [("PS1", 25.5, 1.0, 3.0, 1.5, 2.5, 3.6),
          ("PS9", 31.2, 0.8, 2.6, 1.4, 2.1, 3.1)]
# (functional location, wet well storage m3 alarm -> overflow, emergency storage m3)
CIVIL = [("PS1", 3.5, 50.0), ("PS9", 4.0, 60.0)]
# (functional location, design flow L/s, design head m): two rows per station, second row = same data
PUMPS = [("PS1-PMP01", 25.0, 30.0), ("PS1-PMP02", 25.0, 30.0),
         ("PS9-PMP01", 12.5, 22.0), ("PS9-PMP02", 12.5, 22.0)]


def sheets() -> dict:
    lv = {1: {"A": "Pump station register - Level Settings (demo data)"},
          3: {"C": FL, "E": "Ground level (m AHD)", "F": "Overflow (m below TOS)", "Q": "Stop (m below TOS)",
              "L": "High level alarm (m below TOS)", "P": "Start (m below TOS)", "T": "Base (m below TOS)"}}
    for i, (fl, ground, ovf, stop, alarm, start, base) in enumerate(LEVELS, 4):
        lv[i] = {"C": fl, "E": ground, "F": ovf, "Q": stop, "L": alarm, "P": start, "T": base}
    cv = {1: {"A": "Pump station register - Civil (demo data)"},
          3: {"C": FL, "R": "Wet well storage (m3)", "AB": "Emergency storage (m3)"}}
    for i, (fl, storage, emerg) in enumerate(CIVIL, 4):
        cv[i] = {"C": fl, "R": storage, "AB": emerg}
    pm = {1: {"A": "Pump station register - Motor and Pump Attributes (demo data)"},
          3: {"C": FL, "AF": "Design flow (L/s)", "AG": "Design head (m)"}}
    for i, (fl, q, h) in enumerate(PUMPS, 4):
        pm[i] = {"C": fl, "AF": q, "AG": h}
    return {"Level Settings": lv, "Civil": cv, "Motor and Pump Attributes": pm}


def build(path=DEFAULT_OUT) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(path, sheets())
    return path


if __name__ == "__main__":
    print(f"wrote {build(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT)}")
