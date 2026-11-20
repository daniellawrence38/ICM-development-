//! Diurnal profile import: parse a CSV / tab-separated block of (time, factor) rows.
use crate::error::{Error, Result};
use crate::model::{Profile, ProfilePoint};
use serde::Serialize;

/// Warn when the time-weighted mean factor differs from 1 by more than this.
pub const MEAN_TOLERANCE: f64 = 0.02;

/// Outcome of parsing / importing a profile.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ProfileImport {
    pub profile_id: String,
    pub points: usize,
    pub interval_min: Option<i64>,
    /// Mean factor of the stored values (after normalisation, if requested).
    pub mean_factor: f64,
    /// Mean factor of the input values.
    pub input_mean_factor: f64,
    pub min_factor: f64,
    pub max_factor: f64,
    pub normalised: bool,
    pub is_default: bool,
    pub warnings: Vec<String>,
}

fn bad(msg: String) -> Error {
    Error::Other(msg)
}

/// Minute of day from `hh:mm`, `hh:mm:ss`, `d/m/y hh:mm[:ss]` or plain minutes.
fn parse_time(tok: &str) -> Option<f64> {
    let t = tok.trim();
    if t.is_empty() {
        return None;
    }
    if let Some(part) = t.split_whitespace().rev().find(|p| p.contains(':')) {
        let mut it = part.split(':');
        let h: f64 = it.next()?.trim().parse().ok()?;
        let m: f64 = it.next()?.trim().parse().ok()?;
        let sec: f64 = match it.next() {
            Some(s) => s.trim().parse().ok()?,
            None => 0.0,
        };
        if it.next().is_some()
            || h < 0.0
            || !(0.0..60.0).contains(&m)
            || !(0.0..60.0).contains(&sec)
        {
            return None;
        }
        return Some(h * 60.0 + m + sec / 60.0);
    }
    t.parse::<f64>().ok().filter(|v| v.is_finite())
}

fn parse_factor(tok: &str) -> Option<f64> {
    tok.trim().parse::<f64>().ok().filter(|v| v.is_finite())
}

fn split_fields(line: &str) -> Vec<&str> {
    let f: Vec<&str> = line
        .split(['\t', ',', ';'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    if f.len() >= 2 {
        return f;
    }
    // whitespace separated ("0:05 0.32"), but keep "d/m/y hh:mm factor" workable via the last token
    let w: Vec<&str> = line.split_whitespace().collect();
    if w.len() > 2 && w[..w.len() - 1].iter().any(|p| p.contains(':')) {
        return vec![
            &line[..line.rfind(w[w.len() - 1]).unwrap_or(0)],
            w[w.len() - 1],
        ];
    }
    w
}

/// Time-weighted mean of a piecewise-constant circular profile (each factor holds until the next
/// time; the last holds until the first point of the next day).
fn weighted_mean(pts: &[ProfilePoint]) -> f64 {
    let n = pts.len();
    let mut sum = 0.0;
    for i in 0..n {
        let t0 = pts[i].minute_of_day;
        let t1 = if i + 1 < n {
            pts[i + 1].minute_of_day
        } else {
            pts[0].minute_of_day + 1440
        };
        sum += pts[i].factor * (t1 - t0) as f64;
    }
    sum / 1440.0
}

/// Parse profile text into a [`Profile`] plus warnings. A header row is optional. Errors on
/// unparsable rows, times outside 0-1439, non-increasing times and negative factors. The profile
/// is NOT normalised unless `normalise` is set; a mean far from 1 only warns.
pub fn parse_profile(
    profile_id: &str,
    text: &str,
    description: &str,
    normalise: bool,
) -> Result<(Profile, ProfileImport)> {
    if profile_id.trim().is_empty() {
        return Err(bad("profile id must not be empty".into()));
    }
    let mut pts: Vec<ProfilePoint> = Vec::new();
    let mut first = true;
    for (ln, raw) in text.lines().enumerate() {
        let line = raw.trim().trim_start_matches('\u{feff}');
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let f = split_fields(line);
        let (t, v) = (
            f.first().and_then(|x| parse_time(x)),
            f.get(1).and_then(|x| parse_factor(x)),
        );
        let (Some(t), Some(v)) = (t, v) else {
            if first && f.len() >= 2 {
                first = false;
                continue; // header row
            }
            return Err(bad(format!(
                "line {}: cannot read time and factor from {:?}",
                ln + 1,
                raw.trim()
            )));
        };
        first = false;
        if (t - t.round()).abs() > 0.01 {
            return Err(bad(format!(
                "line {}: time {:?} is not a whole minute",
                ln + 1,
                f[0]
            )));
        }
        let m = t.round() as i64;
        if !(0..=1439).contains(&m) {
            return Err(bad(format!(
                "line {}: time {:?} is outside 00:00-23:59 (minute {m}); drop a trailing 24:00 row",
                ln + 1,
                f[0]
            )));
        }
        if v < 0.0 {
            return Err(bad(format!("line {}: negative factor {v}", ln + 1)));
        }
        if let Some(prev) = pts.last() {
            if m <= prev.minute_of_day {
                return Err(bad(format!(
                    "line {}: times must increase (minute {m} after {})",
                    ln + 1,
                    prev.minute_of_day
                )));
            }
        }
        pts.push(ProfilePoint {
            minute_of_day: m,
            factor: v,
        });
    }
    if pts.len() < 2 {
        return Err(bad(format!(
            "profile needs at least 2 rows, found {}",
            pts.len()
        )));
    }
    let mut warnings = Vec::new();
    // Interval: most common step.
    let mut counts: std::collections::BTreeMap<i64, usize> = Default::default();
    for w in pts.windows(2) {
        *counts
            .entry(w[1].minute_of_day - w[0].minute_of_day)
            .or_default() += 1;
    }
    let interval = counts
        .iter()
        .max_by_key(|(d, c)| (**c, std::cmp::Reverse(**d)))
        .map(|(d, _)| *d);
    if counts.len() > 1 {
        warnings.push(format!(
            "time steps are not uniform ({} distinct intervals); values are held until the next row",
            counts.len()
        ));
    }
    if pts[0].minute_of_day != 0 {
        warnings.push(format!(
            "first row is at minute {}, not 00:00; the last value is carried over midnight",
            pts[0].minute_of_day
        ));
    }
    if let (Some(i), Some(last)) = (interval, pts.last()) {
        if counts.len() == 1 && last.minute_of_day + i < 1440 - i {
            warnings.push(format!(
                "profile ends at minute {}: the day is not fully covered (interval {i} min)",
                last.minute_of_day
            ));
        }
    }
    let input_mean = weighted_mean(&pts);
    let mut normalised = false;
    if normalise {
        if input_mean > 0.0 {
            pts.iter_mut().for_each(|p| p.factor /= input_mean);
            normalised = true;
        } else {
            return Err(bad("cannot normalise: mean factor is zero".into()));
        }
    }
    let mean = weighted_mean(&pts);
    if (mean - 1.0).abs() > MEAN_TOLERANCE {
        warnings.push(format!(
            "mean factor is {mean:.4} (not 1.0 +/- {MEAN_TOLERANCE}); the profile will scale daily volume. \
             Re-import with normalise to divide by the mean"
        ));
    }
    let min_f = pts.iter().map(|p| p.factor).fold(f64::INFINITY, f64::min);
    let max_f = pts
        .iter()
        .map(|p| p.factor)
        .fold(f64::NEG_INFINITY, f64::max);
    let info = ProfileImport {
        profile_id: profile_id.trim().to_string(),
        points: pts.len(),
        interval_min: interval,
        mean_factor: mean,
        input_mean_factor: input_mean,
        min_factor: min_f,
        max_factor: max_f,
        normalised,
        is_default: false,
        warnings,
    };
    let profile = Profile {
        profile_id: info.profile_id.clone(),
        description: if description.trim().is_empty() {
            None
        } else {
            Some(description.trim().to_string())
        },
        interval_min: interval,
        mean_factor: Some(mean),
        points: pts,
    };
    Ok((profile, info))
}
