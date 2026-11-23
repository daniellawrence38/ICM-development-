"""DS 50 ruleset loading and lookup (pure Python)."""
from __future__ import annotations

from . import native, paths, tomlmini

_CACHE: dict = {}


def ruleset_text() -> str:
    """TOML text: prefer the wwnet extension's embedded copy, else bundled file."""
    try:
        fn = getattr(native.get(), "ruleset_toml", None)
        if callable(fn):
            txt = fn()
            if isinstance(txt, str) and txt.strip():
                return txt
    except Exception:
        pass
    return paths.ruleset_path().read_text(encoding="utf-8")


def load_ruleset(refresh: bool = False) -> dict:
    if refresh or "rs" not in _CACHE:
        _CACHE["rs"] = tomlmini.loads(ruleset_text())
    return _CACHE["rs"]


def _rows_as_dicts(block: dict) -> list:
    cols = block["columns"]
    return [dict(zip(cols, row)) for row in block["rows"]]


LOOKUP_TABLES = ("meta", "loading", "residential", "non_residential",
                 "hydraulics", "sizing", "grades", "io_is_sewers")


def lookup(table: str, key: str | None = None, rs: dict | None = None):
    """Look up a DS 50 table. Raises KeyError with a helpful message."""
    rs = rs or load_ruleset()
    t = (table or "").strip().lower()
    k = (key or "").strip()
    if t == "meta":
        return {x: rs.get(x) for x in ("standard", "revision", "year")}
    if t in ("hydraulics", "sizing", "io_is_sewers"):
        return rs[t]
    if t == "loading":
        return {x: v for x, v in rs["loading"].items() if not isinstance(v, dict)}
    if t in ("residential", "non_residential"):
        regions = rs["loading"][t]
        region, _, sel = k.partition(":")
        if not sel:
            region, sel = "", k
        out = []
        for reg, block in regions.items():
            if region and region.lower() != reg:
                continue
            for row in _rows_as_dicts(block):
                first = next(iter(row.values()))
                if not sel or sel.lower() in str(first).lower():
                    out.append({"region": reg, **row})
        if not out:
            raise KeyError(f"no {t} row for key {key!r}; use 'region:rcode' e.g. 'south:R20'")
        return out
    if t == "grades":
        if not k:
            return rs["grades"]
        for g in rs["grades"]:
            if str(g["dn"]) == k.lower().lstrip("dn"):
                return g
        raise KeyError(f"no DN {key!r} in Table 4.4; DNs: {[g['dn'] for g in rs['grades']]}")
    raise KeyError(f"unknown table {table!r}; choose from {list(LOOKUP_TABLES)}")


def residential_gsdf(net_area_ha: float, rcode: str, ground: str = "dry",
                     region: str = "south") -> dict:
    """GSDF (L/s) for a residential net area (DS 50 Tables 4.1 / 4.2), from the engine."""
    res = native.require("residential_gsdf").residential_gsdf(net_area_ha, rcode, ground, region)
    res = dict(res)
    res.setdefault("source", "wwnet")
    return res
