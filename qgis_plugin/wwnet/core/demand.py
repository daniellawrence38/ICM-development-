"""Existing dry weather demand from land use (pure Python: no arcpy / qgis).

Inputs: Local Planning Scheme zone polygons (DPLH-071, a 'Zone' text field), Local Planning Scheme R-Code
polygons (DPLH-070, an R-code field) and the model's sewers. Output: DS 50 loading (Tables 4.1-4.3) per node,
as ``wwn_subcatchments`` rows the engine already loads (Run Design: GSDF; Run Simulation: daily flow / 86400).

Method (one zone polygon at a time):
1. Classify the zone text with the template rules (first rule whose words occur in the zone text wins):
   residential, residential_or_commercial, commercial, hotel_motel, perth_cba, industrial or zero (no flow:
   roads, open space, conservation, rural ...). No rule matched -> ZONE_UNMATCHED, loaded as residential R15.
2. Sample the polygon on a square grid (~50 points, spacing 2-20 m); each sample point carries an equal
   share of the polygon's exact area. Zero-flow polygons are not sampled.
3. Residential samples take the R-code of the R-Code polygon they fall in (base code of a dual code; DS 50
   4.2.1(c): R15 when there is no code or it is below R15; otherwise the next higher DS 50 table row).
   residential_or_commercial samples are residential where an R-code exists, else commercial.
4. Each sample goes to the nearest model sewer within the maximum distance and loads that sewer's UPSTREAM
   node (the rule Import Sewer Data uses for connection points). Samples further away are not loaded.
5. Areas are summed per node and DS 50 class -> one sub-catchment row each, id ``LU-<node>-<class>``.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import paths, tomlmini

TEMPLATE_NAME = "dplh_land_use.toml"
ID_PREFIX = "LU-"
CLASSES = ("residential", "residential_or_commercial", "commercial", "hotel_motel", "perth_cba", "industrial",
           "zero")
#: class -> (wwn_subcatchments.land_use, DS 50 Table 4.3 key, id suffix)
NON_RES = {"commercial": ("commercial", "suburban_commercial_school_hospital_public", "COM"),
           "hotel_motel": ("hotel_motel", "hotel_motel", "HOT"),
           "perth_cba": ("perth_cba", "perth_cba", "CBA"),
           "industrial": ("industrial", "industrial", "IND")}
MIN_RCODE = 15.0
#: MGA zone 50 northing of latitude 26 deg S on the central meridian (DS 50 Tables 4.1 / 4.2 split)
_K0, _FALSE_N = 0.9996, 10_000_000.0


# ------------------------------------------------------------------ template

def template_path():
    return paths.find_resource("templates/import", TEMPLATE_NAME)


def load_template(path=None) -> dict:
    p = path or template_path()
    text = p.read_text(encoding="utf-8") if hasattr(p, "read_text") else open(p, encoding="utf-8").read()
    return tomlmini.loads(text)


def _words(text) -> str:
    """Lower case, every run of non-alphanumerics -> one space, padded: ' light industry '."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower())) + " "


@dataclass
class Rules:
    rules: list                                  # [(padded words, class)]
    rcode_aliases: dict = field(default_factory=dict)   # normalised code text -> code, e.g. 'RAC3' -> 'R80'
    blank_class: str = "zero"

    @classmethod
    def from_template(cls, cfg: dict | None = None) -> "Rules":
        cfg = load_template() if cfg is None else cfg
        rules = []
        for item in cfg.get("rules", []):
            pat, klass = str(item[0]), str(item[1]).strip().lower()
            if klass not in CLASSES:
                raise ValueError(f"zone rule {pat!r}: unknown class {klass!r} (use one of {', '.join(CLASSES)})")
            rules.append((_words(pat), klass))
        aliases = {_norm_code(k): str(v).strip() for k, v in (cfg.get("rcode_aliases") or {}).items()}
        blank = str((cfg.get("options") or {}).get("blank_zone_class", "zero")).strip().lower()
        if blank not in CLASSES:
            raise ValueError(f"options.blank_zone_class: unknown class {blank!r}")
        return cls(rules, aliases, blank)

    def classify(self, zone) -> tuple:
        """(class, rule words or '' ) for a zone text; ('', '') when no rule matches; blank text -> blank_class."""
        w = _words(zone)
        if not w.strip():
            return self.blank_class, "(blank)"
        for pat, klass in self.rules:
            if pat in w:
                return klass, pat.strip()
        return "", ""


# ------------------------------------------------------------------ R-codes

def _norm_code(text) -> str:
    return re.sub(r"[^A-Z0-9./]", "", str(text or "").upper())


def parse_rcode(value, aliases: dict | None = None) -> tuple:
    """(R number or None, note) for an R-code field value: 20, '20', 'R20', 'R12.5', 'R20/40' or 'R20/R40'
    (dual code: the base code), an alias from the template (e.g. R-AC3). note: '' | 'dual' | 'unknown' | 'none'."""
    if value is None:
        return None, "none"
    if isinstance(value, (int, float)):
        return (float(value), "") if value > 0 else (None, "none")
    raw = str(value).strip()
    if not raw:
        return None, "none"
    code = _norm_code(raw)
    alias = (aliases or {}).get(code)
    if alias:
        n, _ = parse_rcode(alias)
        return n, "" if n is not None else "unknown"
    m = re.fullmatch(r"R?(\d+(?:\.\d+)?)(?:/R?(\d+(?:\.\d+)?))?", code)
    if not m:
        return None, "unknown"
    n = float(m.group(1))
    if n <= 0:
        return None, "none"
    return n, "dual" if m.group(2) else ""


def fmt_code(n: float) -> str:
    return f"R{n:g}"


# ------------------------------------------------------------------ DS 50 rates

@dataclass
class Rates:
    residential: list            # [(R number, code, persons per net ha, L/person/day)] ascending
    non_residential: dict        # Table 4.3 key -> L/net ha/day
    region: str
    ground: str

    def residential_row(self, n: float | None) -> tuple:
        """(row, note) for an R number: R15 when None / below R15, the next higher row otherwise, the
        highest row when above the table. note: '' | 'below_min' | 'next_higher' | 'above_table' | 'none'."""
        if n is None:
            return self.row_for(MIN_RCODE), "none"
        if n < MIN_RCODE:
            return self.row_for(MIN_RCODE), "below_min"
        for row in self.residential:
            if row[0] >= n - 1e-9:
                return row, "" if abs(row[0] - n) < 1e-9 else "next_higher"
        return self.residential[-1], "above_table"

    def row_for(self, n: float) -> tuple:
        for row in self.residential:
            if row[0] >= n - 1e-9:
                return row
        return self.residential[-1]

    def residential_lps_per_ha(self, row) -> float:
        return row[2] * row[3] / 86400.0

    def non_residential_lps_per_ha(self, key: str):
        v = self.non_residential.get(key)
        return None if v is None else v / 86400.0


def load_rates(region: str = "south", ground: str = "dry", rs: dict | None = None) -> Rates:
    """DS 50 Tables 4.1 / 4.2 (residential) and 4.3 (non-residential) for the region and ground condition."""
    if rs is None:
        from . import ruleset
        rs = ruleset.load_ruleset()
    region, ground = region.lower(), ground.lower()
    if region not in ("south", "north") or ground not in ("dry", "wet"):
        raise ValueError(f"region must be south / north and ground dry / wet (got {region!r}, {ground!r})")
    load = rs["loading"]

    def dicts(block):
        return [dict(zip(block["columns"], r)) for r in block["rows"]]

    res = []
    for r in dicts(load["residential"][region]):
        n, _ = parse_rcode(r["rcode"])
        res.append((n, str(r["rcode"]), float(r["persons_per_net_ha"]), float(r[f"{ground}_lpd"])))
    res.sort()
    nonres = {str(r["landuse"]): float(r[f"{ground}_l_ha_d"]) for r in dicts(load["non_residential"][region])}
    return Rates(res, nonres, region, ground)


def _meridian_arc(lat_deg: float) -> float:
    a, f = 6378137.0, 1 / 298.257222101            # GRS80
    n = f / (2 - f)
    A = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    p = math.radians(lat_deg)
    return A * (p - (3 * n / 2 - 9 * n ** 3 / 16) * math.sin(2 * p) + (15 * n ** 2 / 16 - 15 * n ** 4 / 32)
                * math.sin(4 * p) - 35 * n ** 3 / 48 * math.sin(6 * p) + 315 * n ** 4 / 512 * math.sin(8 * p))


NORTHING_26S = _FALSE_N - _K0 * _meridian_arc(26.0)


def region_from_northing(northing: float) -> str:
    """DS 50 region from an MGA (zone 50) northing: north of 26 deg S has the larger northing."""
    return "north" if northing > NORTHING_26S else "south"


# ------------------------------------------------------------------ polygon geometry

class Rings:
    """A polygon as a set of closed rings (exteriors and holes alike; even-odd rule), with edges bucketed
    in horizontal bands so scanlines and point tests only look at nearby edges."""

    def __init__(self, rings):
        self.rings = []
        for r in rings or ():
            pts = [(float(x), float(y)) for x, y in r]
            if len(pts) >= 2 and pts[0] == pts[-1]:
                pts = pts[:-1]
            if len(pts) >= 3:
                self.rings.append(pts)
        xs = [p[0] for r in self.rings for p in r]
        ys = [p[1] for r in self.rings for p in r]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        self.edges = []
        for r in self.rings:
            for a, b in zip(r, r[1:] + r[:1]):
                if a[1] != b[1]:
                    self.edges.append((a[0], a[1], b[0], b[1]))
        self._band = {}
        if self.bbox:
            span = self.bbox[3] - self.bbox[1]
            self._h = max(span / max(1, len(self.edges) // 4), 1.0)
            for i, (_x0, y0, _x1, y1) in enumerate(self.edges):
                for k in range(self._bk(min(y0, y1)), self._bk(max(y0, y1)) + 1):
                    self._band.setdefault(k, []).append(i)
        self._area = None

    def __bool__(self):
        return bool(self.rings)

    def _bk(self, y: float) -> int:
        return math.floor((y - self.bbox[1]) / self._h)

    def crossings(self, y: float) -> list:
        """Sorted x of the edges crossing the horizontal line at y (half-open in y)."""
        if not self.bbox or y < self.bbox[1] or y > self.bbox[3]:
            return []
        xs = []
        for i in self._band.get(self._bk(y), ()):
            x0, y0, x1, y1 = self.edges[i]
            if min(y0, y1) <= y < max(y0, y1):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        return xs

    def intervals(self, y: float) -> list:
        xs = self.crossings(y)
        return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]

    def contains(self, x: float, y: float) -> bool:
        if not self.bbox or not (self.bbox[0] <= x <= self.bbox[2] and self.bbox[1] <= y <= self.bbox[3]):
            return False
        return sum(1 for cx in self.crossings(y) if cx > x) % 2 == 1

    def ring_depths(self) -> list:
        """How many other rings contain each ring (even = exterior, odd = hole)."""
        out = []
        for i, r in enumerate(self.rings):
            x, y = r[0]
            out.append(sum(1 for j, o in enumerate(self.rings) if j != i and _ring_contains(o, x, y)))
        return out

    def area(self) -> float:
        """Exact area (m2): exterior rings minus holes (orientation independent)."""
        if self._area is None:
            self._area = sum(abs(_signed_area(r)) * (1 if d % 2 == 0 else -1)
                             for r, d in zip(self.rings, self.ring_depths()))
            self._area = max(self._area, 0.0)
        return self._area

    def polygons(self) -> list:
        """[[exterior, hole, ...], ...] with OGC orientation (exterior counter-clockwise, holes clockwise),
        rings closed: for writing a MULTIPOLYGON."""
        depths = self.ring_depths()
        ext = [i for i, d in enumerate(depths) if d % 2 == 0]
        polys = {i: [_orient(self.rings[i], True)] for i in ext}
        for i, d in enumerate(depths):
            if d % 2 == 0:
                continue
            x, y = self.rings[i][0]
            owners = [j for j in ext if depths[j] == d - 1 and _ring_contains(self.rings[j], x, y)]
            if owners:
                j = min(owners, key=lambda k: abs(_signed_area(self.rings[k])))
                polys[j].append(_orient(self.rings[i], False))
        return [[r + r[:1] for r in polys[i]] for i in ext]


def _signed_area(r) -> float:
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(r, r[1:] + r[:1]))


def _orient(r, ccw: bool):
    return list(r) if (_signed_area(r) > 0) == ccw else list(reversed(r))


def _ring_contains(r, x, y) -> bool:
    inside = False
    for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]):
        if (y0 <= y < y1) or (y1 <= y < y0):
            if x0 + (y - y0) * (x1 - x0) / (y1 - y0) > x:
                inside = not inside
    return inside


def sample_points(poly: Rings, min_spacing: float = 2.0, max_spacing: float = 20.0, target: int = 50) -> tuple:
    """(grid points inside the polygon, spacing). At least one point for a non-empty polygon (a thin sliver
    gets the middle of its widest interval, else its first vertex)."""
    if not poly:
        return [], 0.0
    a = poly.area()
    s = min(max(math.sqrt(a / max(target, 1)) if a > 0 else min_spacing, min_spacing), max_spacing)
    x0, y0, x1, y1 = poly.bbox
    pts = []
    y = y0 + s / 2
    while y < y1:
        for lo, hi in poly.intervals(y):
            j = math.ceil((lo - x0 - s / 2) / s)
            x = x0 + s / 2 + j * s
            while x < hi:
                if x >= lo:
                    pts.append((x, y))
                x += s
        y += s
    if not pts:
        best = None
        for k in range(1, 8):
            yy = y0 + (y1 - y0) * k / 8
            for lo, hi in poly.intervals(yy):
                if best is None or hi - lo > best[0]:
                    best = (hi - lo, ((lo + hi) / 2, yy))
        pts = [best[1] if best else poly.rings[0][0]]
    return pts, s


class PolygonIndex:
    """Polygons (e.g. R-Code areas) in a uniform grid of their bounding boxes, for point lookups."""

    def __init__(self, items, cell: float = 250.0):
        self.cell, self.items, self.cells = cell, [], {}
        for ref, poly in items:
            if not poly:
                continue
            k = len(self.items)
            self.items.append((ref, poly))
            bx0, by0, bx1, by1 = poly.bbox
            for kx in range(math.floor(bx0 / cell), math.floor(bx1 / cell) + 1):
                for ky in range(math.floor(by0 / cell), math.floor(by1 / cell) + 1):
                    self.cells.setdefault((kx, ky), []).append(k)

    def find(self, x: float, y: float) -> list:
        return [self.items[k][0] for k in self.cells.get((math.floor(x / self.cell), math.floor(y / self.cell)), ())
                if self.items[k][1].contains(x, y)]


class SewerIndex:
    """Nearest sewer segment to a point (expanding ring search over a uniform grid)."""

    def __init__(self, sewers, cell: float = 25.0):
        """sewers: [(ref, [(x, y), ...])]."""
        self.cell, self.cells, self.n = cell, {}, 0
        for ref, pts in sewers:
            for a, b in zip(pts, pts[1:]):
                self.n += 1
                for kx in range(math.floor(min(a[0], b[0]) / cell), math.floor(max(a[0], b[0]) / cell) + 1):
                    for ky in range(math.floor(min(a[1], b[1]) / cell), math.floor(max(a[1], b[1]) / cell) + 1):
                        self.cells.setdefault((kx, ky), []).append((ref, a, b))

    def nearest(self, p, max_d: float):
        """(ref, distance) of the nearest segment within max_d, else (None, None)."""
        c = self.cell
        kx, ky = math.floor(p[0] / c), math.floor(p[1] / c)
        best, bd, r = None, None, 0
        while True:
            for ix in range(kx - r, kx + r + 1):
                for iy in (range(ky - r, ky + r + 1) if ix in (kx - r, kx + r) else (ky - r, ky + r)):
                    for ref, a, b in self.cells.get((ix, iy), ()):
                        d = _seg_dist(p, a, b)
                        if d <= max_d and (bd is None or d < bd or (d == bd and str(ref) < str(best))):
                            best, bd = ref, d
            if (bd is not None and bd <= r * c) or r * c > max_d:
                return best, bd
            r += 1


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


# ------------------------------------------------------------------ the calculation

@dataclass
class Options:
    max_distance_m: float = 150.0
    region: str | None = None            # None = from the model's mean northing
    ground: str = "dry"
    min_spacing_m: float = 2.0
    max_spacing_m: float = 20.0
    target_samples: int = 50             # per polygon: 200 gave the same totals at 4x the run time


@dataclass
class Result:
    subcatchments: list                  # wwn_subcatchments rows (dicts)
    polygons: list                       # review rows (dicts with 'polygons' for the geometry)
    issues: list                         # {severity, code, object_type, object_id, message, x, y}
    lines: list                          # summary messages
    totals: dict
    region: str
    ground: str


def _issue(sev, code, oid, msg, xy=None):
    d = {"severity": sev, "code": code, "object_type": "land_use", "object_id": str(oid), "message": msg}
    if xy is not None:
        d["x"], d["y"] = xy
    return d


def calculate(zones, rcodes, sewers, rules: Rules, opts: Options | None = None, rs: dict | None = None) -> Result:
    """zones: [{'id', 'zone', 'rings'}]; rcodes: [{'id', 'rcode', 'rings'}];
    sewers: [{'conduit_id', 'us_node_id', 'coords'}] (EPSG:7850, metres)."""
    opts = opts or Options()
    sew = [s for s in sewers if s.get("coords") and len(s["coords"]) >= 2]
    region = (opts.region or "").lower() or None
    if region is None:
        ys = [p[1] for s in sew for p in s["coords"]]
        region = region_from_northing(sum(ys) / len(ys)) if ys else "south"
    rates = load_rates(region, opts.ground, rs)
    us_node = {s["conduit_id"]: s["us_node_id"] for s in sew}
    sindex = SewerIndex([(s["conduit_id"], s["coords"]) for s in sew])
    rpolys = []
    for r in rcodes or ():
        n, note = parse_rcode(r.get("rcode"), rules.rcode_aliases)
        rpolys.append(((n, note, r.get("rcode"), r.get("id")), Rings(r.get("rings"))))
    rindex = PolygonIndex(rpolys)

    loads: dict = {}          # (node, suffix) -> {'area': m2, 'land_use', 'rcode', 'zones': set, 'codes': set}
    review, issues = [], []
    t = {"zone_polygons": len(zones), "area_ha": {}, "loaded_ha": 0.0, "not_loaded_ha": 0.0, "adwf_lps": {},
         "unmatched": {}, "blank": 0, "rcode_ha": {}, "no_rcode_ha": 0.0, "below_min_ha": 0.0,
         "next_higher_ha": 0.0, "above_table_ha": 0.0, "unknown_codes": {}, "dual_ha": 0.0, "overlap_ha": 0.0,
         "rcode_polygons": len(rpolys), "empty_geometry": 0}

    for z in zones:
        zid, ztext = z.get("id"), z.get("zone")
        poly = Rings(z.get("rings"))
        klass, rule = rules.classify(ztext)
        flags = []
        if not poly:
            t["empty_geometry"] += 1
            continue
        area = poly.area()
        centre = ((poly.bbox[0] + poly.bbox[2]) / 2, (poly.bbox[1] + poly.bbox[3]) / 2)
        if rule == "(blank)":
            t["blank"] += 1
            flags.append("ZONE_BLANK")
        unmatched = klass == ""
        if unmatched:
            klass = "residential"
            flags.append("ZONE_UNMATCHED")
            u = t["unmatched"].setdefault(str(ztext).strip(), [0, 0.0])
            u[0] += 1
            u[1] += area / 1e4
        t["area_ha"][klass] = t["area_ha"].get(klass, 0.0) + area / 1e4
        row = {"polygon_id": str(zid), "zone": None if ztext is None else str(ztext), "zone_class": klass,
               "rcode_source": None, "ds50_rcode": None, "area_ha": area / 1e4, "loaded_ha": 0.0, "adwf_lps": 0.0,
               "nodes": None, "flags": None, "polygons": poly.polygons()}
        review.append(row)
        if klass == "zero":
            row["flags"] = "; ".join(flags) or None
            continue
        pts, _s = sample_points(poly, opts.min_spacing_m, opts.max_spacing_m, opts.target_samples)
        share = area / len(pts)
        per_node: dict = {}
        src_codes, rows_used = set(), set()
        lost = 0.0
        sha = share / 1e4
        for p in pts:
            raw = None
            if unmatched:
                trow = rates.row_for(MIN_RCODE)
                suffix, land_use, rcode = trow[1], "residential", trow[1]
                lps_ha = rates.residential_lps_per_ha(trow)
            elif klass in ("residential", "residential_or_commercial"):
                found = rindex.find(*p)
                if len(found) > 1:
                    t["overlap_ha"] += sha
                coded = [f for f in found if f[0] is not None]
                n, note, raw, _rid = (max(coded, key=lambda f: f[0]) if coded
                                      else found[0] if found else (None, "none", None, None))
                if klass == "residential_or_commercial" and n is None:
                    land_use, key, suffix = NON_RES["commercial"]
                    rcode, raw = None, None
                    lps_ha = rates.non_residential_lps_per_ha(key)
                else:
                    if note == "unknown":
                        k = str(raw).strip()
                        t["unknown_codes"][k] = t["unknown_codes"].get(k, 0.0) + sha
                    elif n is None:
                        t["no_rcode_ha"] += sha
                    if note == "dual":
                        t["dual_ha"] += sha
                    trow, rnote = rates.residential_row(n)
                    if rnote in ("below_min", "next_higher", "above_table"):
                        t[f"{rnote}_ha"] += sha
                    suffix, land_use, rcode = trow[1], "residential", trow[1]
                    lps_ha = rates.residential_lps_per_ha(trow)
                    if raw is not None and str(raw).strip():
                        src_codes.add(str(raw).strip())
            else:
                land_use, key, suffix = NON_RES[klass]
                rcode = None
                lps_ha = rates.non_residential_lps_per_ha(key)
                if lps_ha is None:        # e.g. Perth CBA north of 26 deg S: not in Table 4.2 / 4.3 north
                    land_use, key, suffix = NON_RES["commercial"]
                    lps_ha = rates.non_residential_lps_per_ha(key)
                    if "RATE_MISSING" not in flags:
                        flags.append("RATE_MISSING")
            ref, _d = sindex.nearest(p, opts.max_distance_m)
            if ref is None:
                lost += share
                continue
            node = us_node[ref]
            per_node[node] = per_node.get(node, 0.0) + share
            rows_used.add(rcode or suffix)
            ld = loads.setdefault((node, suffix), {"area": 0.0, "land_use": land_use, "rcode": rcode,
                                                   "zones": set(), "codes": set(), "lps": 0.0})
            ld["area"] += share
            ld["lps"] += sha * lps_ha
            ld["zones"].add(str(ztext).strip() if ztext is not None else "(blank)")
            if rcode and raw is not None and str(raw).strip():
                ld["codes"].add(str(raw).strip())
            if rcode:
                t["rcode_ha"][rcode] = t["rcode_ha"].get(rcode, 0.0) + sha
            row["adwf_lps"] += sha * lps_ha
            t["adwf_lps"][land_use] = t["adwf_lps"].get(land_use, 0.0) + sha * lps_ha
        loaded = max(area - lost, 0.0)
        row["loaded_ha"] = loaded / 1e4
        t["loaded_ha"] += loaded / 1e4
        t["not_loaded_ha"] += lost / 1e4
        row["rcode_source"] = "; ".join(sorted(src_codes)) or None
        row["ds50_rcode"] = "; ".join(sorted(rows_used)) or None
        if per_node:
            parts = sorted(per_node.items(), key=lambda kv: -kv[1])
            text = "; ".join(f"{n} {a / 1e4:.2f} ha" for n, a in parts[:12])
            row["nodes"] = text + (f"; ... (+{len(parts) - 12} more)" if len(parts) > 12 else "")
        if lost >= area - 1e-6:
            flags.append("NOT_LOADED")
            issues.append(_issue("warning", "LAND_USE_NOT_LOADED", zid,
                                 f"zone '{ztext}' ({area / 1e4:.2f} ha): no model sewer within "
                                 f"{opts.max_distance_m:g} m; not loaded", centre))
        elif lost > 0.05 * area:
            flags.append("PARTLY_LOADED")
            issues.append(_issue("info", "LAND_USE_PARTLY_LOADED", zid,
                                 f"zone '{ztext}': {lost / 1e4:.2f} of {area / 1e4:.2f} ha is further than "
                                 f"{opts.max_distance_m:g} m from a model sewer; that part is not loaded", centre))
        if unmatched:
            issues.append(_issue("warning", "ZONE_UNMATCHED", zid,
                                 f"zone '{ztext}' matches no rule in {TEMPLATE_NAME}; loaded as residential R15 "
                                 f"({area / 1e4:.2f} ha)", centre))
        row["flags"] = "; ".join(flags) or None

    subs = []
    for (node, suffix), ld in sorted(loads.items()):
        zones_txt = ", ".join(sorted(ld["zones"]))
        note = f"Existing demand (land use), DS 50 {rates.ground} / {rates.region}: zone {zones_txt}"
        if ld["codes"]:
            note += f"; LPS code {', '.join(sorted(ld['codes']))} -> DS 50 {ld['rcode']}"
        subs.append({"subcatchment_id": f"{ID_PREFIX}{node}-{suffix}", "outlet_node_id": node,
                     "net_area_ha": round(ld["area"] / 1e4, 6), "land_use": ld["land_use"], "rcode": ld["rcode"],
                     "ground": rates.ground, "region": rates.region, "status": "existing",
                     "adwf_lps": ld["lps"], "notes": note[:1000]})
    t["subcatchments"] = len(subs)
    t["nodes"] = len({s["outlet_node_id"] for s in subs})
    t["adwf_total_lps"] = sum(t["adwf_lps"].values())
    return Result(subs, review, issues, summary_lines(t, rates, opts), t, rates.region, rates.ground)


def summary_lines(t: dict, rates: Rates, opts: Options) -> list:
    def ha(v):
        return f"{v:.2f} ha"

    lines = []
    order = ("residential", "residential_or_commercial", "commercial", "hotel_motel", "perth_cba", "industrial",
             "zero")
    parts = [f"{k.replace('_', ' ')} {ha(t['area_ha'][k])}" for k in order if t["area_ha"].get(k)]
    total = sum(t["area_ha"].values())
    lines.append(f"Zones: {t['zone_polygons']} polygon(s), {ha(total)}: " + (", ".join(parts) or "none")
                 + ". Unmatched zones are counted as residential (R15).")
    if t["empty_geometry"]:
        lines.append(f"{t['empty_geometry']} zone polygon(s) without geometry skipped.")
    if t["blank"]:
        lines.append(f"{t['blank']} zone polygon(s) with a blank zone: zero flow (flag ZONE_BLANK; set "
                     "options.blank_zone_class in the template to change).")
    if t["unmatched"]:
        items = sorted(t["unmatched"].items(), key=lambda kv: -kv[1][1])
        shown = "; ".join(f"'{k}' {v[0]} polygon(s) {ha(v[1])}" for k, v in items[:15])
        more = f"; ... (+{len(items) - 15} more names)" if len(items) > 15 else ""
        lines.append(f"Zones matching no rule (loaded as residential R15, flag ZONE_UNMATCHED): {shown}{more}. "
                     f"Add them to {TEMPLATE_NAME} to load them properly.")
    if t["rcode_ha"]:
        def rkey(kv):
            n, _ = parse_rcode(kv[0])
            return n or 0
        lines.append("Residential area loaded, by DS 50 row: " + ", ".join(
            f"{k} {ha(v)}" for k, v in sorted(t["rcode_ha"].items(), key=rkey)) + ".")
    notes = []
    if t["no_rcode_ha"] > 1e-6:
        notes.append(f"{ha(t['no_rcode_ha'])} with no R-code (R15, DS 50 4.2.1(c))")
    if t["below_min_ha"] > 1e-6:
        notes.append(f"{ha(t['below_min_ha'])} coded below R15 (R15 used, DS 50 4.2.1(c))")
    if t["next_higher_ha"] > 1e-6:
        notes.append(f"{ha(t['next_higher_ha'])} with a code between table rows (next higher row used)")
    if t["above_table_ha"] > 1e-6:
        notes.append(f"{ha(t['above_table_ha'])} coded above the table (highest row used)")
    if t["dual_ha"] > 1e-6:
        notes.append(f"{ha(t['dual_ha'])} dual coded (base code used)")
    if t["overlap_ha"] > 1e-6:
        notes.append(f"{ha(t['overlap_ha'])} under overlapping R-code polygons (highest code used)")
    if t["unknown_codes"]:
        notes.append("codes not recognised (R15 used; add them to [rcode_aliases]): "
                     + ", ".join(f"'{k}' {ha(v)}" for k, v in sorted(t["unknown_codes"].items())))
    if notes:
        lines.append("R-codes: " + "; ".join(notes) + ".")
    if not t["rcode_polygons"]:
        lines.append("No R-code polygons were read: every residential zone is loaded as R15.")
    lines.append(f"Loaded {ha(t['loaded_ha'])} on {t['nodes']} node(s) as {t['subcatchments']} sub-catchment "
                 f"row(s); {ha(t['not_loaded_ha'])} not loaded (further than {opts.max_distance_m:g} m from a model "
                 "sewer: outside the model area or unsewered).")
    by = ", ".join(f"{k} {v:.2f}" for k, v in sorted(t["adwf_lps"].items(), key=lambda kv: -kv[1]))
    lines.append(f"Existing ADWF (DS 50 daily flow / 86400, {rates.ground} ground, {rates.region} of 26 deg S): "
                 f"{t['adwf_total_lps']:.2f} L/s" + (f" ({by})" if by else "") + ".")
    return lines


# ------------------------------------------------------------------ GeoPackage blobs

def multipolygon_blob(polygons, srs: int = 7850) -> bytes | None:
    """GeoPackage MULTIPOLYGON blob from [[ring, ...], ...] (rings closed), or None when empty."""
    import struct
    polys = [p for p in polygons or () if p and len(p[0]) >= 4]
    if not polys:
        return None
    xs = [x for p in polys for r in p for x, _y in r]
    ys = [y for p in polys for r in p for _x, y in r]
    head = b"GP" + bytes([0, 1 | (1 << 1)]) + struct.pack("<i", srs) + struct.pack(
        "<4d", min(xs), max(xs), min(ys), max(ys))
    body = b"\x01" + struct.pack("<II", 6, len(polys))
    for p in polys:
        body += b"\x01" + struct.pack("<II", 3, len(p))
        for r in p:
            body += struct.pack("<I", len(r)) + b"".join(struct.pack("<2d", float(x), float(y)) for x, y in r)
    return head + body
