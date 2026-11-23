//! Sewage pump stations and rising mains for the SWMM model.
//!
//! * Wet well: the station's wet-well node becomes a `[STORAGE]` node with a constant plan
//!   area (FUNCTIONAL curve `a0 + a1*d^a2` with `a1 = a2 = 0`).
//! * Pumps: one `[PUMPS]` link per `duty`/`assist` pump from the wet well to a new
//!   `<ps_id>__DIS` junction. A pump curve is a SWMM `PUMP3` curve (x = head in m, y = flow
//!   in L/s: `pump_getInflow` in `link.c`, `table_lookup(&Curve[m], head)` with
//!   `head = (H_outlet - H_inlet)`); a station rate without a curve is a one-point `PUMP2`
//!   curve (flow independent of head).
//! * Start/stop: columns 6/7 of a `[PUMPS]` line are the *startup* and *shutoff* depths at
//!   the inlet node (`pump_readParams` / `link_setTargetSetting` in `link.c`).
//! * Rising main: a chain of `FORCE_MAIN` conduits from `<ps_id>__DIS` to the discharge
//!   node, with intermediate junctions at the profile points.
//!
//! All tunable defaults are the constants below.
#![allow(clippy::neg_cmp_op_on_partial_ord)] // deliberate: NaN must fail the checks
use crate::types::*;
use std::collections::{HashMap, HashSet};
use wwnet_core::{Model, PumpStation, RisingMain};

/// Wet-well plan area when the station has none: 3.0 m diameter circular well.
pub const DEFAULT_WET_WELL_AREA_M2: f64 = 7.07;
/// Pump stop level above the wet-well invert when the station gives none, m.
pub const DEFAULT_STOP_ABOVE_INVERT_M: f64 = 0.3;
/// Pump start level above the wet-well invert when the station gives none, m.
pub const DEFAULT_START_ABOVE_INVERT_M: f64 = 1.0;
/// Assist pumps start this far above the duty start level, m.
pub const ASSIST_START_OFFSET_M: f64 = 0.2;
/// Assist pumps stop this far above the duty stop level, m.
pub const ASSIST_STOP_OFFSET_M: f64 = 0.1;
/// Minimum stop depth: SWMM ignores shutoff depths of 0 (`link_setTargetSetting`), m.
pub const MIN_STOP_DEPTH_M: f64 = 0.05;
/// Absolute roughness of a rising main without roughness data, mm.
pub const DEFAULT_RM_KS_MM: f64 = 1.5;
/// Surcharge depth of junctions on a rising main so they never flood, m.
pub const FM_NODE_SUR_DEPTH_M: f64 = 1000.0;
/// Placeholder Manning n in the `[CONDUITS]` line of a force main (SWMM derives the real
/// equivalent n from the force-main equation, `forcemain_getEquivN`).
pub const FM_PLACEHOLDER_N: f64 = 0.013;
/// Reference velocity for converting Hazen-Williams C to an equivalent ks, m/s.
const HW_REF_VELOCITY_MPS: f64 = 1.0;
/// Kinematic viscosity used in the C -> ks conversion (SWMM: 1.1e-5 ft2/s at 20 C), m2/s.
const NU_20C: f64 = 1.0219e-6;

/// Force-main head-loss equation (SWMM `FORCE_MAIN_EQUATION`, global).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ForceMainEq {
    DarcyWeisbach,
    HazenWilliams,
}

impl ForceMainEq {
    pub fn keyword(self) -> &'static str {
        match self {
            ForceMainEq::DarcyWeisbach => "D-W",
            ForceMainEq::HazenWilliams => "H-W",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Rough {
    /// Absolute roughness, mm.
    Ks(f64),
    /// Hazen-Williams C.
    Hc(f64),
}

#[derive(Debug, Clone)]
pub struct PumpDef {
    pub id: String,
    pub role: String,
    pub curve: Option<Vec<(f64, f64)>>,
    pub speed_pct: f64,
}

#[derive(Debug, Clone)]
pub struct StationPlan<'a> {
    pub ps: &'a PumpStation,
    pub rm: &'a RisingMain,
    pub wet_ix: usize,
    pub disch_ix: usize,
    pub area_m2: f64,
    pub dia_m: f64,
    pub length_m: f64,
    pub rough: Rough,
    pub profile: Vec<(f64, f64)>,
    pub pumps: Vec<PumpDef>,
}

/// SWMM-side description of one force-main conduit.
#[derive(Debug, Clone)]
pub struct FmSeg {
    pub swmm: String,
    pub from: String,
    pub to: String,
    pub length_m: f64,
    pub in_offset: f64,
    pub out_offset: f64,
    pub dia_m: f64,
    pub roughness: f64,
}

#[derive(Debug, Clone)]
pub struct PumpSpec {
    pub swmm: String,
    pub orig: String,
    pub ps_id: String,
    pub role: String,
    pub from: String,
    pub to: String,
    pub curve: String,
    pub on_depth: f64,
    pub off_depth: f64,
}

#[derive(Debug, Clone)]
pub struct CurveSpec {
    pub name: String,
    /// `PUMP2` or `PUMP3`.
    pub kind: &'static str,
    pub points: Vec<(f64, f64)>,
}

#[derive(Debug, Clone)]
pub struct RisingMainMeta {
    pub rm_id: String,
    pub ps_id: String,
    pub segments: Vec<String>,
    pub length_m: f64,
    pub diameter_m: f64,
}

#[derive(Debug, Clone)]
pub struct StationMeta {
    pub ps_id: String,
    pub wet_well_node_id: String,
}

#[derive(Debug, Clone)]
pub struct PumpMeta {
    pub orig: String,
    pub swmm: String,
    pub ps_id: String,
    pub role: String,
}

/// Everything the writer needs for one station, after SWMM ids are assigned.
#[derive(Debug, Clone, Default)]
pub struct StationBuild {
    pub segs: Vec<FmSeg>,
    pub pumps: Vec<PumpSpec>,
    pub curves: Vec<CurveSpec>,
    pub rm: Option<RisingMainMeta>,
}

/// An internal SWMM junction created for a rising main.
#[derive(Debug, Clone)]
pub struct ExtraNode {
    pub orig: String,
    pub swmm: String,
    pub invert: f64,
    pub max_depth: f64,
    pub xy: Option<(f64, f64)>,
}

fn parse_pairs(s: &str) -> Option<Vec<(f64, f64)>> {
    let v: Vec<Vec<f64>> = serde_json::from_str(s).ok()?;
    let mut out = Vec::new();
    for p in v {
        if p.len() < 2 || !p[0].is_finite() || !p[1].is_finite() {
            return None;
        }
        out.push((p[0], p[1]));
    }
    Some(out)
}

/// Equivalent Darcy-Weisbach absolute roughness (mm) giving the same friction slope as
/// Hazen-Williams `c` at a reference velocity of 1 m/s in a pipe of diameter `d_m`.
pub fn ks_from_hw_c(c: f64, d_m: f64) -> f64 {
    let v = HW_REF_VELOCITY_MPS;
    let r = d_m / 4.0;
    // H-W (SI): V = 0.849 C R^0.63 S^0.54
    let s = (v / (0.849 * c * r.powf(0.63))).powf(1.0 / 0.54);
    let f = 2.0 * 9.80665 * d_m * s / (v * v);
    let re = v * d_m / NU_20C;
    // Swamee-Jain inverted for e/D: f = 0.25 / log10(e/3.7D + 5.74/Re^0.9)^2
    let e_over_3_7d = 10f64.powf(-0.5 / f.sqrt()) - 5.74 / re.powf(0.9);
    (e_over_3_7d * 3.7 * d_m * 1000.0).clamp(0.001, 100.0)
}

/// Decide, per station, whether it can be modelled; collect data and warnings.
pub fn plan_stations<'a>(
    model: &'a Model,
    node_ix: &HashMap<&str, usize>,
    warnings: &mut Vec<String>,
) -> (Vec<StationPlan<'a>>, ForceMainEq) {
    let mut plans: Vec<StationPlan<'a>> = Vec::new();
    let mut wet_used: HashSet<usize> = HashSet::new();
    for ps in &model.pump_stations {
        let id = &ps.ps_id;
        let Some(wet) = ps.wet_well_node_id.as_deref() else {
            warnings.push(format!("pump station {id}: no wet well node; not modelled"));
            continue;
        };
        let Some(&wet_ix) = node_ix.get(wet) else {
            warnings.push(format!(
                "pump station {id}: wet well node {wet} not found; not modelled"
            ));
            continue;
        };
        if !wet_used.insert(wet_ix) {
            warnings.push(format!(
                "pump station {id}: wet well node {wet} already used by another station; not modelled"
            ));
            continue;
        }
        let rm = ps
            .rising_main_id
            .as_deref()
            .and_then(|r| model.rising_mains.iter().find(|m| m.rm_id == r))
            .or_else(|| {
                model
                    .rising_mains
                    .iter()
                    .find(|m| m.ps_id.as_deref() == Some(id.as_str()))
            });
        let Some(rm) = rm else {
            warnings.push(format!(
                "pump station {id}: no rising main; not modelled (wet well acts as a free outfall)"
            ));
            continue;
        };
        let dn_node = rm
            .discharge_node_id
            .as_deref()
            .or(ps.discharge_node_id.as_deref());
        let Some(disch_ix) = dn_node.and_then(|d| node_ix.get(d).copied()) else {
            warnings.push(format!(
                "rising main {}: discharge node missing or unknown; station {id} not modelled",
                rm.rm_id
            ));
            continue;
        };
        if disch_ix == wet_ix {
            warnings.push(format!(
                "rising main {}: discharges to its own wet well; station {id} not modelled",
                rm.rm_id
            ));
            continue;
        }
        let dia_mm = rm
            .internal_diameter_mm
            .or(rm.dn.map(|d| d as f64))
            .filter(|d| *d > 0.0);
        let Some(dia_mm) = dia_mm else {
            warnings.push(format!(
                "rising main {}: no diameter; station {id} not modelled",
                rm.rm_id
            ));
            continue;
        };
        let mut profile = rm
            .profile_json
            .as_deref()
            .and_then(|p| {
                let r = parse_pairs(p);
                if r.is_none() {
                    warnings.push(format!(
                        "rising main {}: profile_json is not [[chainage_m, level_m], ...]; ignored",
                        rm.rm_id
                    ));
                }
                r
            })
            .unwrap_or_default();
        profile.sort_by(|a, b| a.0.total_cmp(&b.0));
        profile.dedup_by(|a, b| (a.0 - b.0).abs() < 1e-9);
        if profile.len() == 1 {
            warnings.push(format!(
                "rising main {}: profile needs at least 2 points; ignored",
                rm.rm_id
            ));
            profile.clear();
        }
        let length = rm
            .length_m
            .filter(|l| *l > 0.0)
            .or_else(|| rm.geom.as_ref().map(|g| g.length()).filter(|l| *l > 0.0))
            .or_else(|| {
                (profile.len() >= 2)
                    .then(|| profile[profile.len() - 1].0 - profile[0].0)
                    .filter(|l| *l > 0.0)
            });
        let Some(length_m) = length else {
            warnings.push(format!(
                "rising main {}: no length, geometry or profile; station {id} not modelled",
                rm.rm_id
            ));
            continue;
        };
        let rough = match (
            rm.roughness_type.to_ascii_lowercase().as_str(),
            rm.roughness,
        ) {
            ("ks", Some(v)) if v > 0.0 => Rough::Ks(v),
            ("hw_c", Some(v)) if v > 0.0 => Rough::Hc(v),
            (t, Some(_)) if t != "ks" && t != "hw_c" => {
                warnings.push(format!(
                    "rising main {}: unknown roughness_type '{t}'; ks {DEFAULT_RM_KS_MM} mm assumed",
                    rm.rm_id
                ));
                Rough::Ks(DEFAULT_RM_KS_MM)
            }
            _ => {
                warnings.push(format!(
                    "rising main {}: no roughness; ks {DEFAULT_RM_KS_MM} mm assumed",
                    rm.rm_id
                ));
                Rough::Ks(DEFAULT_RM_KS_MM)
            }
        };
        let area = match ps.wet_well_area_m2.filter(|a| *a > 0.0) {
            Some(a) => a,
            None => {
                warnings.push(format!(
                    "pump station {id}: no wet well area; {DEFAULT_WET_WELL_AREA_M2} m2 (3.0 m dia) assumed"
                ));
                DEFAULT_WET_WELL_AREA_M2
            }
        };
        if ps.emergency_storage_m3.is_some_and(|v| v > 0.0) {
            warnings.push(format!(
                "pump station {id}: emergency storage is not modelled; overflow above the overflow level is lost"
            ));
        }
        let mut pumps: Vec<PumpDef> = Vec::new();
        for p in model.pumps.iter().filter(|p| &p.ps_id == id) {
            let role = p.role.to_ascii_lowercase();
            if role == "standby" {
                warnings.push(format!(
                    "pump {}: standby pump is not modelled (info)",
                    p.pump_id
                ));
                continue;
            }
            if role != "duty" && role != "assist" {
                warnings.push(format!(
                    "pump {}: unknown role '{}'; treated as duty",
                    p.pump_id, p.role
                ));
            }
            let role = if role == "assist" { "assist" } else { "duty" };
            let curve = p.curve_json.as_deref().and_then(|c| {
                let pts = parse_pairs(c).filter(|v| !v.is_empty());
                if pts.is_none() {
                    warnings.push(format!(
                        "pump {}: curve_json is not [[q_lps, head_m], ...]; ignored",
                        p.pump_id
                    ));
                }
                pts
            });
            let speed = if p.speed_pct > 0.0 && p.speed_pct.is_finite() {
                p.speed_pct
            } else {
                warnings.push(format!(
                    "pump {}: invalid speed {}%; 100% used",
                    p.pump_id, p.speed_pct
                ));
                100.0
            };
            pumps.push(PumpDef {
                id: p.pump_id.clone(),
                role: role.into(),
                curve,
                speed_pct: speed,
            });
        }
        if model.pumps.iter().all(|p| &p.ps_id != id) {
            if ps.pump_rate_lps.is_some_and(|r| r > 0.0) {
                warnings.push(format!(
                    "pump station {id}: no pumps listed; one duty pump at the station rate assumed"
                ));
                pumps.push(PumpDef {
                    id: format!("{id}__P1"),
                    role: "duty".into(),
                    curve: None,
                    speed_pct: 100.0,
                });
            } else {
                warnings.push(format!("pump station {id}: no pumps; not modelled"));
                continue;
            }
        }
        if !pumps.iter().any(|p| p.role == "duty") {
            warnings.push(format!(
                "pump station {id}: no duty pump (only assist/standby); not modelled"
            ));
            continue;
        }
        plans.push(StationPlan {
            ps,
            rm,
            wet_ix,
            disch_ix,
            area_m2: area,
            dia_m: dia_mm / 1000.0,
            length_m,
            rough,
            profile,
            pumps,
        });
    }
    let eq = if !plans.is_empty() && plans.iter().all(|p| matches!(p.rough, Rough::Hc(_))) {
        ForceMainEq::HazenWilliams
    } else {
        ForceMainEq::DarcyWeisbach
    };
    if eq == ForceMainEq::DarcyWeisbach && plans.iter().any(|p| matches!(p.rough, Rough::Hc(_))) {
        warnings.push(
            "rising mains mix Hazen-Williams C and ks; SWMM has one global equation, so \
             Darcy-Weisbach is used and C is converted to an equivalent ks (at 1 m/s)"
                .into(),
        );
    }
    (plans, eq)
}

/// Start/stop depths (above the wet-well invert) of the duty pumps; also the warnings.
pub fn duty_depths(
    ps: &PumpStation,
    invert: f64,
    max_depth: f64,
    warnings: &mut Vec<String>,
) -> (f64, f64) {
    let id = &ps.ps_id;
    let mut stop = match ps.stop_level {
        Some(l) => l - invert,
        None => {
            warnings.push(format!(
                "pump station {id}: no stop level; {DEFAULT_STOP_ABOVE_INVERT_M} m above invert assumed"
            ));
            DEFAULT_STOP_ABOVE_INVERT_M
        }
    };
    if !(stop >= MIN_STOP_DEPTH_M) {
        warnings.push(format!(
            "pump station {id}: stop level not above the wet well invert; {MIN_STOP_DEPTH_M} m above invert used"
        ));
        stop = MIN_STOP_DEPTH_M;
    }
    let mut start = match ps.start_level {
        Some(l) => l - invert,
        None => {
            warnings.push(format!(
                "pump station {id}: no start level; {DEFAULT_START_ABOVE_INVERT_M} m above invert assumed"
            ));
            DEFAULT_START_ABOVE_INVERT_M
        }
    };
    if !(start > stop) {
        warnings.push(format!(
            "pump station {id}: start level not above stop level; start set to stop + 0.5 m"
        ));
        start = stop + 0.5;
    }
    if start >= max_depth {
        warnings.push(format!(
            "pump station {id}: start level at or above the overflow level; the pumps may never start before overflow"
        ));
    }
    (start, stop)
}

/// Create the SWMM objects of one station. `wet`/`disch` are the SWMM names and inverts of
/// the wet-well and discharge nodes; `dis_xy` the position of the new discharge junction.
#[allow(clippy::too_many_arguments)]
pub fn build_station(
    plan: &StationPlan<'_>,
    wet: (&str, f64),
    disch: (&str, f64),
    wet_max_depth: f64,
    dis_xy: Option<(f64, f64)>,
    eq: ForceMainEq,
    o: &SimOptions,
    used_nodes: &mut HashSet<String>,
    used_links: &mut HashSet<String>,
    safe_id: &dyn Fn(&str, &mut HashSet<String>) -> String,
    extra: &mut Vec<ExtraNode>,
    warnings: &mut Vec<String>,
) -> StationBuild {
    let ps_id = &plan.ps.ps_id;
    let rm_id = &plan.rm.rm_id;
    let (wet_name, wet_inv) = wet;
    let (dis_name, dis_inv) = disch;
    let mut b = StationBuild::default();

    // ---- rising main: levels along the chain (chainage as a fraction of the length)
    let prof = &plan.profile;
    let (first_level, last_level) = if prof.len() >= 2 {
        let mut last = prof[prof.len() - 1].1;
        if last < dis_inv {
            warnings.push(format!(
                "rising main {rm_id}: last profile level {last:.3} below the discharge node invert {dis_inv:.3}; raised to the invert"
            ));
            last = dis_inv;
        }
        (prof[0].1, last)
    } else {
        (wet_inv, dis_inv)
    };
    // (chainage m along the main, level) of every node on the chain, DIS first
    let mut chain: Vec<(f64, f64)> = vec![(0.0, first_level)];
    if prof.len() >= 2 {
        let span = prof[prof.len() - 1].0 - prof[0].0;
        let scale = plan.length_m / span;
        let min_len = o.min_length_m;
        let mut kept = 0.0;
        for p in &prof[1..prof.len() - 1] {
            let c = (p.0 - prof[0].0) * scale;
            if c - kept >= min_len && plan.length_m - c >= min_len {
                chain.push((c, p.1));
                kept = c;
            } else {
                warnings.push(format!(
                    "rising main {rm_id}: profile point at chainage {:.1} m is closer than {min_len} m to a neighbour; dropped",
                    p.0
                ));
            }
        }
    }
    let mut len_total = plan.length_m;
    if chain.len() == 1 && len_total < o.min_length_m {
        warnings.push(format!(
            "rising main {rm_id}: length {len_total:.2} m lengthened to {:.1} m",
            o.min_length_m
        ));
        len_total = o.min_length_m;
    }
    chain.push((len_total, last_level));

    // nodes: DIS + interior
    let mut names: Vec<String> = Vec::new();
    let dis_orig = format!("{ps_id}__DIS");
    let dis_swmm = safe_id(&dis_orig, used_nodes);
    extra.push(ExtraNode {
        orig: dis_orig,
        swmm: dis_swmm.clone(),
        invert: chain[0].1,
        max_depth: plan.dia_m + 1.0,
        xy: dis_xy,
    });
    names.push(dis_swmm.clone());
    for (k, (_, lvl)) in chain.iter().enumerate().take(chain.len() - 1).skip(1) {
        let orig = format!("{ps_id}__RM{k}");
        let swmm = safe_id(&orig, used_nodes);
        extra.push(ExtraNode {
            orig,
            swmm: swmm.clone(),
            invert: *lvl,
            max_depth: plan.dia_m + 1.0,
            xy: None,
        });
        names.push(swmm);
    }
    names.push(dis_name.to_string());

    // roughness in the unit of the global equation
    let roughness = match (eq, plan.rough) {
        (ForceMainEq::DarcyWeisbach, Rough::Ks(k)) => k,
        (ForceMainEq::DarcyWeisbach, Rough::Hc(c)) => ks_from_hw_c(c, plan.dia_m),
        (ForceMainEq::HazenWilliams, Rough::Hc(c)) => c,
        (ForceMainEq::HazenWilliams, Rough::Ks(_)) => unreachable!("H-W only if all are C"),
    };
    let nseg = chain.len() - 1;
    for k in 0..nseg {
        let orig = if nseg == 1 {
            rm_id.clone()
        } else {
            format!("{rm_id}__{}", k + 1)
        };
        let swmm = safe_id(&orig, used_links);
        b.segs.push(FmSeg {
            swmm,
            from: names[k].clone(),
            to: names[k + 1].clone(),
            length_m: chain[k + 1].0 - chain[k].0,
            in_offset: chain[k].1,
            out_offset: chain[k + 1].1,
            dia_m: plan.dia_m,
            roughness,
        });
    }
    b.rm = Some(RisingMainMeta {
        rm_id: rm_id.clone(),
        ps_id: ps_id.clone(),
        segments: b.segs.iter().map(|s| s.swmm.clone()).collect(),
        length_m: len_total,
        diameter_m: plan.dia_m,
    });

    // ---- pumps
    let (duty_start, duty_stop) = duty_depths(plan.ps, wet_inv, wet_max_depth, warnings);
    for p in &plan.pumps {
        let swmm = safe_id(&p.id, used_links);
        let (on, off) = if p.role == "assist" {
            (
                duty_start + ASSIST_START_OFFSET_M,
                duty_stop + ASSIST_STOP_OFFSET_M,
            )
        } else {
            (duty_start, duty_stop)
        };
        let s = p.speed_pct / 100.0;
        let cname = format!("PC_{swmm}");
        let curve = match &p.curve {
            Some(pts) => {
                // (q, h) -> PUMP3 (h, q); affinity laws for speed: Q ~ n, H ~ n^2
                let mut v: Vec<(f64, f64)> = pts.iter().map(|&(q, h)| (h * s * s, q * s)).collect();
                v.sort_by(|a, b| a.0.total_cmp(&b.0));
                v.dedup_by(|a, b| (a.0 - b.0).abs() < 1e-9);
                if (s - 1.0).abs() > 1e-9 {
                    warnings.push(format!(
                        "pump {}: speed {}% applied to the curve by the affinity laws (SWMM has no initial speed for fixed-speed curves)",
                        p.id, p.speed_pct
                    ));
                }
                Some(CurveSpec {
                    name: cname.clone(),
                    kind: "PUMP3",
                    points: v,
                })
            }
            None => match plan.ps.pump_rate_lps.filter(|r| *r > 0.0) {
                Some(r) => {
                    warnings.push(format!(
                        "pump {}: no curve; constant {:.2} L/s (station pump_rate_lps) assumed",
                        p.id,
                        r * s
                    ));
                    Some(CurveSpec {
                        name: cname.clone(),
                        kind: "PUMP2",
                        points: vec![(0.0, r * s)],
                    })
                }
                None => {
                    warnings.push(format!(
                        "pump {}: no curve and no station rate; ideal pump (outflow = inflow) used",
                        p.id
                    ));
                    None
                }
            },
        };
        let curve_name = match curve {
            Some(c) => {
                let n = c.name.clone();
                b.curves.push(c);
                n
            }
            None => "*".to_string(),
        };
        b.pumps.push(PumpSpec {
            swmm,
            orig: p.id.clone(),
            ps_id: ps_id.clone(),
            role: p.role.clone(),
            from: wet_name.to_string(),
            to: dis_swmm.clone(),
            curve: curve_name,
            on_depth: on,
            off_depth: off,
        });
    }
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hw_to_ks_round_trips_friction_slope() {
        for (c, d) in [(100.0, 0.1), (120.0, 0.15), (140.0, 0.3)] {
            let ks = ks_from_hw_c(c, d);
            assert!(ks > 0.001 && ks < 20.0, "ks {ks} for C {c}");
            // friction slope at 1 m/s by Swamee-Jain with that ks vs H-W
            let re = d / NU_20C;
            let f = 0.25
                / (ks / 1000.0 / (3.7 * d) + 5.74 / re.powf(0.9))
                    .log10()
                    .powi(2);
            let s_dw = f / d / (2.0 * 9.80665);
            let r = d / 4.0;
            let s_hw = (1.0 / (0.849 * c * r.powf(0.63))).powf(1.0 / 0.54);
            assert!((s_dw - s_hw).abs() / s_hw < 0.02, "{s_dw} vs {s_hw}");
        }
        // rougher pipe (lower C) gives larger ks
        assert!(ks_from_hw_c(90.0, 0.2) > ks_from_hw_c(140.0, 0.2));
    }
}
