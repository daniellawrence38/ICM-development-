use std::str::FromStr;

use serde::Serialize;
use wwnet_hydraulics as hyd;

use crate::ruleset::{Location, RuleSet};

/// Outcome of a single rule check.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CheckResult {
    Pass,
    Warn,
    Fail,
}

/// One clause-referenced check result, suitable for a design report.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Check {
    pub rule: String,
    pub clause: String,
    pub value: f64,
    pub limit: f64,
    pub result: CheckResult,
    pub message: String,
}

/// Land-use class of the contributing catchment (4.2.1(n),(o)).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LandUseClass {
    #[default]
    Residential,
    /// Commercial up to `small_commercial_max_ha`.
    SmallCommercial,
    /// Industrial or large commercial.
    IndustrialLargeCommercial,
}

impl FromStr for LandUseClass {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        match s.trim().to_ascii_lowercase().as_str() {
            "residential" => Ok(Self::Residential),
            "small_commercial" => Ok(Self::SmallCommercial),
            "industrial" | "large_commercial" | "industrial_large_commercial" => {
                Ok(Self::IndustrialLargeCommercial)
            }
            o => Err(format!("unknown land use class '{o}'")),
        }
    }
}

/// Inputs for [`check_conduit`]. Grades are "1 in x" denominators; flows in L/s.
#[derive(Debug, Clone)]
pub struct ConduitDesignInput {
    pub dn: u32,
    pub grade_1in: f64,
    /// Accumulated gravity GSDF at the conduit.
    pub gsdf_lps: f64,
    /// Pump rate discharged into the conduit (0 if none); a non-zero value also means
    /// the conduit receives a pressure main.
    pub pumped_inflow_lps: f64,
    pub land_use_class: LandUseClass,
    pub length_m: f64,
    pub us_cover_m: f64,
    pub ds_cover_m: f64,
    pub location: Location,
    /// Contours unconfirmed / no road design levels (4.9(d)).
    pub levels_uncertain: bool,
}

fn chk(
    rule: &str,
    clause: &str,
    value: f64,
    limit: f64,
    result: CheckResult,
    message: String,
) -> Check {
    Check {
        rule: rule.into(),
        clause: clause.into(),
        value,
        limit,
        result,
        message,
    }
}

fn pf(ok: bool) -> CheckResult {
    if ok {
        CheckResult::Pass
    } else {
        CheckResult::Fail
    }
}

/// Run all conduit checks against a rule set. Unknown DNs yield a single Fail.
pub fn check_conduit(rs: &RuleSet, i: &ConduitDesignInput) -> Vec<Check> {
    use CheckResult::*;
    let mut out = Vec::new();
    let Ok(spec) = rs.grade_spec(i.dn) else {
        out.push(chk(
            "diameter",
            "DS50 Table 4.4",
            i.dn as f64,
            0.0,
            Fail,
            format!("DN{} is not a DS50 gravity sewer size", i.dn),
        ));
        return out;
    };
    let total = i.gsdf_lps + i.pumped_inflow_lps;
    let industrial = i.land_use_class == LandUseClass::IndustrialLargeCommercial;

    // Minimum grade (Table 4.4, 4.2.1(q),(r),(s)).
    let mut min_grade = spec.min_grade_1in;
    let mut clause = "DS50 Table 4.4";
    if industrial && i.dn == 225 {
        if i.gsdf_lps < rs.sizing.industrial_dn225_low_flow.gsdf_lt_lps {
            min_grade = rs.sizing.industrial_dn225_low_flow.min_grade_1in;
            clause = "DS50 4.2.1(s)";
        } else {
            min_grade = min_grade.max(300.0);
        }
    }
    if i.grade_1in <= min_grade {
        out.push(chk(
            "min_grade",
            clause,
            i.grade_1in,
            min_grade,
            Pass,
            format!(
                "Grade 1 in {} is not flatter than 1 in {}",
                i.grade_1in, min_grade
            ),
        ));
    } else if let Some(cmin) = spec.conditional_min_grade_1in.filter(|c| i.grade_1in <= *c) {
        let (res, why) = match (i.dn, spec.conditional_min_pumped_lps) {
            (225, Some(p)) if i.pumped_inflow_lps >= p && !industrial => (
                Warn,
                format!("1 in {cmin} permitted where a pump station discharges >= {p} L/s; confirm"),
            ),
            (225, Some(p)) => (
                Fail,
                format!("flatter than 1 in {min_grade}; 1 in {cmin} only where a pump station discharges >= {p} L/s"),
            ),
            _ => (
                Warn,
                format!("1 in {cmin} allowed only with justification (fewer pump stations, gravity vs PS, fewer entries to >= DN300)"),
            ),
        };
        let cl = if i.dn == 225 {
            "DS50 4.2.1(r)"
        } else {
            "DS50 4.2.1(q)"
        };
        out.push(chk("min_grade", cl, i.grade_1in, min_grade, res, why));
    } else {
        out.push(chk(
            "min_grade",
            clause,
            i.grade_1in,
            min_grade,
            Fail,
            format!(
                "Grade 1 in {} is flatter than minimum 1 in {}",
                i.grade_1in, min_grade
            ),
        ));
    }

    // Maximum grade (Table 4.4 note 1: IS sewers may be steeper).
    out.push(chk(
        "max_grade",
        "DS50 Table 4.4",
        i.grade_1in,
        spec.max_grade_1in,
        pf(i.grade_1in >= spec.max_grade_1in),
        format!("Maximum grade is 1 in {}", spec.max_grade_1in),
    ));

    // Table 4.4 max allowable flow.
    match rs.max_allowable_flow(i.dn, i.grade_1in) {
        Some((q, cond)) => out.push(chk(
            "max_flow",
            "DS50 Table 4.4",
            total,
            q,
            pf(total <= q),
            format!(
                "Design flow {total:.2} L/s vs Table 4.4 limit {q} L/s{}",
                if cond { " (conditional band)" } else { "" }
            ),
        )),
        None => out.push(chk(
            "max_flow",
            "DS50 Table 4.4",
            total,
            0.0,
            Fail,
            format!("Grade 1 in {} is outside the Table 4.4 bands", i.grade_1in),
        )),
    }

    // d/D by Colebrook-White (4.2.1(m), (p)); ID = DN.
    let d = i.dn as f64 / 1000.0;
    let ks = rs.hydraulics.ks_mm / 1000.0;
    let slope = hyd::slope_from_1in(i.grade_1in);
    let lim = rs.max_d_over_d(i.dn);
    match hyd::depth_ratio_for_flow(total / 1000.0, d, slope, ks) {
        Some(y) => {
            // Table 4.4 governs; where it allows the flow but computed d/D is marginally
            // over the limit (e.g. DN150 at 1:200, DN450 at 1:500), warn rather than fail.
            let table_ok = rs
                .max_allowable_flow(i.dn, i.grade_1in)
                .is_some_and(|(q, _)| total <= q);
            let (res, note) = match (y <= lim, table_ok) {
                (true, _) => (Pass, ""),
                (false, true) => (Warn, "; within Table 4.4 flow, which governs"),
                (false, false) => (Fail, ""),
            };
            out.push(chk(
                "depth_ratio",
                "DS50 4.2.1(m)",
                y,
                lim,
                res,
                format!("d/D {y:.3} at design flow vs limit {lim}{note}"),
            ));
        }
        None => out.push(chk(
            "depth_ratio",
            "DS50 4.2.1(m)",
            1.0,
            lim,
            Fail,
            "Design flow exceeds the maximum part-full capacity".into(),
        )),
    }

    // AC spacing.
    out.push(chk(
        "ac_spacing",
        "DS50 Table 4.4",
        i.length_m,
        spec.max_ac_spacing_m,
        pf(i.length_m <= spec.max_ac_spacing_m),
        format!(
            "Length {} m vs maximum AC spacing {} m",
            i.length_m, spec.max_ac_spacing_m
        ),
    ));

    // Cover.
    let req = rs.min_cover(i.location, i.levels_uncertain);
    let cover = i.us_cover_m.min(i.ds_cover_m);
    out.push(chk(
        "min_cover",
        "DS50 4.9",
        cover,
        req,
        pf(cover >= req - 1e-9),
        format!(
            "Minimum cover {cover:.2} m vs required {req:.2} m ({:?})",
            i.location
        ),
    ));

    // Minimum DN by receiving/serving use (4.2.1(l),(n),(o)).
    let mut min_dn = rs.sizing.min_dn_residential;
    let mut cl = "DS50 4.2.1(n)";
    if industrial {
        min_dn = min_dn.max(rs.sizing.min_dn_industrial_large_commercial);
        cl = "DS50 4.2.1(o)";
    }
    if i.pumped_inflow_lps > 0.0 {
        min_dn = min_dn.max(rs.hydraulics.min_dn_receiving_pressure_main);
        cl = if industrial {
            "DS50 4.2.1(l),(o)"
        } else {
            "DS50 4.2.1(l)"
        };
    }
    out.push(chk(
        "min_dn",
        cl,
        i.dn as f64,
        min_dn as f64,
        pf(i.dn >= min_dn),
        format!("DN{} vs minimum DN{min_dn} for this use", i.dn),
    ));

    // Pressure main discharge vs receiving capacity (4.2.1(u)).
    if i.pumped_inflow_lps > 0.0 {
        let cap = hyd::part_full_flow(d, slope, lim, ks) * 1000.0;
        let ratio = rs.hydraulics.pressure_main_discharge_refer_ratio;
        let over = i.pumped_inflow_lps > ratio * cap;
        out.push(chk(
            "pressure_main_discharge",
            "DS50 4.2.1(u)",
            i.pumped_inflow_lps,
            ratio * cap,
            if over { Warn } else { Pass },
            if over {
                format!("Pumped rate exceeds {ratio:.2} x receiving capacity ({cap:.1} L/s); refer to Water Corporation")
            } else {
                format!("Pumped rate within {ratio:.2} x receiving capacity ({cap:.1} L/s)")
            },
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base(dn: u32, grade: f64, gsdf: f64) -> ConduitDesignInput {
        ConduitDesignInput {
            dn,
            grade_1in: grade,
            gsdf_lps: gsdf,
            pumped_inflow_lps: 0.0,
            land_use_class: LandUseClass::Residential,
            length_m: 60.0,
            us_cover_m: 1.5,
            ds_cover_m: 1.5,
            location: Location::RoadReserve,
            levels_uncertain: false,
        }
    }

    fn get<'a>(c: &'a [Check], rule: &str) -> &'a Check {
        c.iter().find(|c| c.rule == rule).unwrap()
    }

    #[test]
    fn dn150_flow_limit() {
        let rs = RuleSet::ds50();
        let ok = check_conduit(&rs, &base(150, 200.0, 5.0));
        assert!(ok.iter().all(|c| c.result == CheckResult::Pass), "{ok:#?}");
        // 6 L/s: Table 4.4 passes; computed d/D marginally > 0.5 -> warn only
        let six = check_conduit(&rs, &base(150, 200.0, 6.0));
        assert_eq!(get(&six, "max_flow").result, CheckResult::Pass);
        assert_eq!(get(&six, "depth_ratio").result, CheckResult::Warn);
        assert!(six.iter().all(|c| c.result != CheckResult::Fail));
        let bad = check_conduit(&rs, &base(150, 200.0, 7.0));
        assert_eq!(get(&bad, "max_flow").result, CheckResult::Fail);
        assert_eq!(get(&bad, "depth_ratio").result, CheckResult::Fail);
    }

    #[test]
    fn dn150_conditional_grade() {
        let rs = RuleSet::ds50();
        let c = check_conduit(&rs, &base(150, 250.0, 4.0));
        let g = get(&c, "min_grade");
        assert_eq!(g.result, CheckResult::Warn);
        assert!(g.clause.contains("4.2.1(q)"));
        let c = check_conduit(&rs, &base(150, 300.0, 1.0));
        assert_eq!(get(&c, "min_grade").result, CheckResult::Fail);
    }

    #[test]
    fn dn225_cover_and_grade() {
        let rs = RuleSet::ds50();
        let mut i = base(225, 300.0, 0.9);
        i.us_cover_m = 0.9;
        i.ds_cover_m = 1.2;
        assert_eq!(
            get(&check_conduit(&rs, &i), "min_cover").result,
            CheckResult::Pass
        );
        i.us_cover_m = 0.8;
        assert_eq!(
            get(&check_conduit(&rs, &i), "min_cover").result,
            CheckResult::Fail
        );
        // 1:350 only with a >= 14 L/s pumped inflow
        let c = check_conduit(&rs, &base(225, 350.0, 2.0));
        assert_eq!(get(&c, "min_grade").result, CheckResult::Fail);
        let mut p = base(225, 350.0, 2.0);
        p.pumped_inflow_lps = 14.0;
        assert_eq!(
            get(&check_conduit(&rs, &p), "min_grade").result,
            CheckResult::Warn
        );
    }

    #[test]
    fn industrial_and_pressure_main() {
        let rs = RuleSet::ds50();
        let c = check_conduit(&rs, &{
            let mut i = base(150, 100.0, 2.0);
            i.land_use_class = LandUseClass::IndustrialLargeCommercial;
            i
        });
        assert_eq!(get(&c, "min_dn").result, CheckResult::Fail);
        // DN225 industrial, GSDF < 3 at 1:300 -> fails (needs 1:200)
        let c = check_conduit(&rs, &{
            let mut i = base(225, 300.0, 2.0);
            i.land_use_class = LandUseClass::IndustrialLargeCommercial;
            i
        });
        assert_eq!(get(&c, "min_grade").result, CheckResult::Fail);
        // 20 L/s pumped into DN225 @1:200 (limit 27, 2/3 capacity ~17.8): warn
        let mut p = base(225, 200.0, 1.0);
        p.pumped_inflow_lps = 20.0;
        let c = check_conduit(&rs, &p);
        assert_eq!(get(&c, "pressure_main_discharge").result, CheckResult::Warn);
    }

    #[test]
    fn spacing_and_steep() {
        let rs = RuleSet::ds50();
        let mut i = base(150, 100.0, 1.0);
        i.length_m = 120.0;
        assert_eq!(
            get(&check_conduit(&rs, &i), "ac_spacing").result,
            CheckResult::Fail
        );
        let c = check_conduit(&rs, &base(150, 4.0, 1.0));
        assert_eq!(get(&c, "max_grade").result, CheckResult::Fail);
        let json_ok = format!("{:?}", c[0].result);
        assert!(!json_ok.is_empty());
    }
}
