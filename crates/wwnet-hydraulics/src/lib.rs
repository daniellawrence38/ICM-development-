//! Gravity sewer hydraulics for circular pipes.
//!
//! All quantities are SI internally: metres, cubic metres per second, metres per
//! second, and slope as a dimensionless fall/length. Convenience conversions to
//! "1 in x" grades and L/s are provided. The internal diameter is taken as the
//! nominal DN (DS50 4.2.1(p)); callers pass `d` in metres.
//!
//! Part-full velocity uses the explicit Colebrook-White form with hydraulic
//! radius `R` in place of `D/4`:
//! `v = -2 sqrt(8gRS) log10(ks/(14.8R) + 2.51 nu / (4R sqrt(8gRS)))`.

use thiserror::Error;

/// Gravitational acceleration, m/s².
pub const G: f64 = 9.81;
/// Default kinematic viscosity for wastewater, m²/s.
pub const NU_WASTEWATER: f64 = 1.31e-6;
/// Depth ratio at which part-full discharge of a circular pipe peaks (~0.938).
pub const MAX_Q_DEPTH_RATIO: f64 = 0.938;

/// Errors from grade helpers.
#[derive(Debug, Error, PartialEq)]
pub enum HydraulicsError {
    /// Pipe length must be positive.
    #[error("pipe length must be > 0, got {0}")]
    BadLength(f64),
}

/// Flow cross-section of a circular pipe.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Section {
    /// Flow area, m².
    pub area: f64,
    /// Wetted perimeter, m.
    pub wetted_perimeter: f64,
    /// Hydraulic radius A/P, m.
    pub hydraulic_radius: f64,
    /// Free-surface width, m.
    pub top_width: f64,
}

/// Geometry of a circular pipe of diameter `d` (m) flowing at `depth_ratio` = y/D (clamped to 0..=1).
pub fn geometry(d: f64, depth_ratio: f64) -> Section {
    let y = depth_ratio.clamp(0.0, 1.0);
    let theta = 2.0 * (1.0 - 2.0 * y).clamp(-1.0, 1.0).acos(); // central angle, rad
    let area = d * d / 8.0 * (theta - theta.sin());
    let wetted_perimeter = d * theta / 2.0;
    let hydraulic_radius = if wetted_perimeter > 0.0 {
        area / wetted_perimeter
    } else {
        0.0
    };
    Section {
        area,
        wetted_perimeter,
        hydraulic_radius,
        top_width: d * (theta / 2.0).sin(),
    }
}

/// Colebrook-White velocity (m/s) for hydraulic radius `r_h` (m), slope (m/m),
/// roughness `ks` (m) and viscosity `nu` (m²/s). Returns 0 for non-positive inputs.
pub fn colebrook_velocity(r_h: f64, slope: f64, ks: f64, nu: f64) -> f64 {
    if r_h <= 0.0 || slope <= 0.0 {
        return 0.0;
    }
    let s = (8.0 * G * r_h * slope).sqrt();
    -2.0 * s * (ks / (14.8 * r_h) + 2.51 * nu / (4.0 * r_h * s)).log10()
}

/// Manning velocity (m/s) for hydraulic radius, slope and Manning `n`.
pub fn manning_velocity(r_h: f64, slope: f64, n: f64) -> f64 {
    if r_h <= 0.0 || slope <= 0.0 {
        return 0.0;
    }
    r_h.powf(2.0 / 3.0) * slope.sqrt() / n
}

/// Part-full Colebrook-White discharge (m³/s) at `depth_ratio`, using [`NU_WASTEWATER`].
pub fn part_full_flow(d: f64, slope: f64, depth_ratio: f64, ks: f64) -> f64 {
    let s = geometry(d, depth_ratio);
    s.area * colebrook_velocity(s.hydraulic_radius, slope, ks, NU_WASTEWATER)
}

/// Full-bore Colebrook-White capacity (m³/s).
pub fn full_bore_capacity(d: f64, slope: f64, ks: f64) -> f64 {
    part_full_flow(d, slope, 1.0, ks)
}

/// Part-full Manning discharge (m³/s).
pub fn manning_flow(d: f64, slope: f64, depth_ratio: f64, n: f64) -> f64 {
    let s = geometry(d, depth_ratio);
    s.area * manning_velocity(s.hydraulic_radius, slope, n)
}

/// Depth ratio (0..[`MAX_Q_DEPTH_RATIO`]) at which Colebrook-White discharge equals `q` (m³/s).
/// Returns `None` if `q` exceeds the maximum part-full capacity. Bisection on the
/// rising limb, where Q is monotonic in depth.
pub fn depth_ratio_for_flow(q: f64, d: f64, slope: f64, ks: f64) -> Option<f64> {
    if q <= 0.0 {
        return Some(0.0);
    }
    let (mut lo, mut hi) = (0.0, MAX_Q_DEPTH_RATIO);
    if part_full_flow(d, slope, hi, ks) < q {
        return None;
    }
    for _ in 0..80 {
        let mid = 0.5 * (lo + hi);
        if part_full_flow(d, slope, mid, ks) < q {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    Some(0.5 * (lo + hi))
}

/// Mean velocity (m/s) when carrying `q` (m³/s); `None` if the pipe cannot carry `q` part-full.
pub fn velocity_at_flow(q: f64, d: f64, slope: f64, ks: f64) -> Option<f64> {
    let y = depth_ratio_for_flow(q, d, slope, ks)?;
    let s = geometry(d, y);
    if s.area <= 0.0 {
        return Some(0.0);
    }
    Some(q / s.area)
}

/// Slope (m/m) from a "1 in x" grade.
pub fn slope_from_1in(x: f64) -> f64 {
    1.0 / x
}

/// "1 in x" denominator from a slope (m/m). Infinite for zero slope.
pub fn one_in_from_slope(s: f64) -> f64 {
    1.0 / s
}

/// Slope from upstream/downstream invert levels (m) and pipe length (m).
pub fn slope_from_levels(us_inv: f64, ds_inv: f64, length: f64) -> Result<f64, HydraulicsError> {
    if length <= 0.0 {
        return Err(HydraulicsError::BadLength(length));
    }
    Ok((us_inv - ds_inv) / length)
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_relative_eq;

    #[test]
    fn geometry_full_and_half() {
        let f = geometry(0.3, 1.0);
        assert_relative_eq!(f.area, std::f64::consts::PI * 0.0225, epsilon = 1e-9);
        assert_relative_eq!(f.hydraulic_radius, 0.075, epsilon = 1e-9);
        let h = geometry(0.3, 0.5);
        assert_relative_eq!(h.area, std::f64::consts::PI * 0.0225 / 2.0, epsilon = 1e-9);
        assert_relative_eq!(h.top_width, 0.3, epsilon = 1e-9);
    }

    #[test]
    fn benchmark_table44() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/benchmarks/ds50_table44_colebrook.csv"
        );
        let text = std::fs::read_to_string(path).unwrap();
        let mut n = 0;
        for line in text.lines().filter(|l| !l.starts_with('#')).skip(1) {
            let f: Vec<f64> = line.split(',').map(|c| c.trim().parse().unwrap()).collect();
            let (dn, grade, ratio, table, cole) = (f[0], f[1], f[2], f[3], f[4]);
            let q = part_full_flow(dn / 1000.0, 1.0 / grade, ratio, 0.001) * 1000.0;
            assert!((q - cole).abs() <= 0.15, "DN{dn} 1:{grade}: {q} vs {cole}");
            assert!((q - table).abs() / table <= 0.06, "DN{dn} 1:{grade}: {q} vs table {table}");
            n += 1;
        }
        assert!(n >= 10);
    }

    #[test]
    fn monotonic_to_093() {
        let mut prev = 0.0;
        for i in 1..=93 {
            let q = part_full_flow(0.3, 0.005, i as f64 / 100.0, 0.001);
            assert!(q > prev);
            prev = q;
        }
    }

    #[test]
    fn full_bore_matches_full_depth() {
        let fb = full_bore_capacity(0.225, 1.0 / 300.0, 0.001);
        assert_relative_eq!(fb, part_full_flow(0.225, 1.0 / 300.0, 1.0, 0.001));
        // full-bore Q is slightly below the 0.938 maximum
        assert!(part_full_flow(0.225, 1.0 / 300.0, 0.938, 0.001) > fb);
    }

    #[test]
    fn round_trip_depth() {
        for y in [0.1, 0.3, 0.5, 0.667, 0.9] {
            let q = part_full_flow(0.375, 1.0 / 400.0, y, 0.001);
            let y2 = depth_ratio_for_flow(q, 0.375, 1.0 / 400.0, 0.001).unwrap();
            assert_relative_eq!(y, y2, epsilon = 1e-6);
        }
        assert!(depth_ratio_for_flow(10.0, 0.15, 0.005, 0.001).is_none());
        let v = velocity_at_flow(0.006, 0.15, 0.005, 0.001).unwrap();
        assert!(v > 0.5 && v < 1.5);
    }

    #[test]
    fn manning_and_grades() {
        let q = manning_flow(0.3, 0.01, 1.0, 0.013);
        assert_relative_eq!(q, 0.1006, epsilon = 2e-3);
        assert_relative_eq!(slope_from_1in(200.0), 0.005);
        assert_relative_eq!(one_in_from_slope(0.005), 200.0);
        assert_relative_eq!(slope_from_levels(10.0, 9.5, 100.0).unwrap(), 0.005);
        assert!(slope_from_levels(1.0, 0.0, 0.0).is_err());
    }
}
