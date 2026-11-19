"""Pure-Python Colebrook-White for partially full circular pipes.

Used as a fallback when the Rust `wwnet` extension module is unavailable.
Basis (DS 50 cl. 4.2.1(p)): ks = 1.0 mm, nu = 1.31e-6 m2/s, internal diameter = DN.
"""
from __future__ import annotations

import math

G = 9.80665
NU = 1.31e-6
# Depth ratio at which open-channel discharge of a circular pipe peaks.
Y_QMAX = 0.9382


def geometry(diameter_m: float, depth_ratio: float):
    """Return (area_m2, wetted_perimeter_m) for a circular section."""
    y = min(max(depth_ratio, 0.0), 1.0)
    theta = 2.0 * math.acos(1.0 - 2.0 * y)
    area = diameter_m ** 2 / 8.0 * (theta - math.sin(theta))
    perim = diameter_m * theta / 2.0
    return area, perim


def velocity_mps(dn_mm: float, grade_1in: float, depth_ratio: float = 1.0,
                 ks_mm: float = 1.0, id_mm: float | None = None) -> float:
    d = (id_mm if id_mm else dn_mm) / 1000.0
    area, perim = geometry(d, depth_ratio)
    if area <= 0 or perim <= 0 or grade_1in <= 0:
        return 0.0
    dh = 4.0 * area / perim
    s = 1.0 / grade_1in
    root = math.sqrt(2.0 * G * dh * s)
    return -2.0 * root * math.log10(ks_mm / 1000.0 / (3.7 * dh) + 2.51 * NU / (dh * root))


def part_full_flow_lps(dn_mm: float, grade_1in: float, depth_ratio: float = 1.0,
                       ks_mm: float = 1.0, id_mm: float | None = None) -> float:
    d = (id_mm if id_mm else dn_mm) / 1000.0
    area, _ = geometry(d, depth_ratio)
    return velocity_mps(dn_mm, grade_1in, depth_ratio, ks_mm, id_mm) * area * 1000.0


def depth_ratio_for_flow(q_lps: float, dn_mm: float, grade_1in: float,
                         ks_mm: float = 1.0, id_mm: float | None = None):
    """d/D at which the pipe carries q_lps in open-channel flow; None if q exceeds
    the open-channel maximum (pipe would surcharge)."""
    if q_lps <= 0:
        return 0.0
    if q_lps > part_full_flow_lps(dn_mm, grade_1in, Y_QMAX, ks_mm, id_mm):
        return None
    lo, hi = 1e-6, Y_QMAX
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if part_full_flow_lps(dn_mm, grade_1in, mid, ks_mm, id_mm) < q_lps:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
