use std::collections::HashMap;
use std::path::Path;
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Errors from loading or querying a rule set.
#[derive(Debug, Error)]
pub enum RulesError {
    #[error("cannot read rule set: {0}")]
    Io(#[from] std::io::Error),
    #[error("cannot parse rule set: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("unknown region '{0}' (expected 'north' or 'south')")]
    UnknownRegion(String),
    #[error("unknown ground condition '{0}' (expected 'dry' or 'wet')")]
    UnknownGround(String),
    #[error("residential code '{0}' is above the highest row in the table for this region")]
    RcodeTooHigh(String),
    #[error("land use '{land_use}' not in {table} for region {region:?}")]
    UnknownLandUse {
        land_use: String,
        table: &'static str,
        region: Region,
    },
    #[error("no Table 4.4 entry for DN{0}")]
    UnknownDn(u32),
}

pub type Result<T> = std::result::Result<T, RulesError>;

/// Region split at 26 degrees S (Tables 4.1-4.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Region {
    North,
    South,
}

impl FromStr for Region {
    type Err = RulesError;
    fn from_str(s: &str) -> Result<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "north" => Ok(Region::North),
            "south" => Ok(Region::South),
            _ => Err(RulesError::UnknownRegion(s.into())),
        }
    }
}

/// Ground water condition: dry ground or wet ground (infiltration) rates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Ground {
    Dry,
    Wet,
}

impl FromStr for Ground {
    type Err = RulesError;
    fn from_str(s: &str) -> Result<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "dry" => Ok(Ground::Dry),
            "wet" => Ok(Ground::Wet),
            _ => Err(RulesError::UnknownGround(s.into())),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Loading {
    pub sdf_factor: f64,
    pub min_rcode: String,
    pub broadacre_pos_minor_road_deduction: f64,
    pub target_subcatchment_ha: f64,
    pub region_split_latitude_deg_s: f64,
    pub residential: RegionTables<ResidentialRaw>,
    pub non_residential: RegionTables<NonResidentialRaw>,
}

/// Per-region row tables; `north` is absent for none of the current tables.
#[derive(Debug, Clone, Deserialize)]
pub struct RegionTables<T> {
    pub south: T,
    pub north: T,
}

/// Table 4.1/4.2 residential rows as stored in the TOML.
#[derive(Debug, Clone, Deserialize)]
pub struct ResidentialRaw {
    pub rows: Vec<ResidentialRow>,
}

/// `[rcode, persons/dwelling, persons/net ha, dry L/p/d, dry GSDF, wet L/p/d, wet GSDF]`.
#[derive(Debug, Clone, Deserialize)]
pub struct ResidentialRow(
    pub String,
    pub f64,
    pub f64,
    pub f64,
    pub f64,
    pub f64,
    pub f64,
);

#[derive(Debug, Clone, Deserialize)]
pub struct NonResidentialRaw {
    pub rows: Vec<NonResidentialRow>,
}

/// `[landuse, dry L/ha/d, dry GSDF, wet L/ha/d, wet GSDF]`.
#[derive(Debug, Clone, Deserialize)]
pub struct NonResidentialRow(pub String, pub f64, pub f64, pub f64, pub f64);

#[derive(Debug, Clone, Deserialize)]
pub struct Hydraulics {
    pub formula: String,
    pub ks_mm: f64,
    /// Keys are DN strings plus `default`.
    #[serde(rename = "max_d_over_D")]
    pub max_d_over_d: HashMap<String, f64>,
    pub pressure_main_discharge_refer_ratio: f64,
    pub min_dn_receiving_pressure_main: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct IndustrialLowFlow {
    pub gsdf_lt_lps: f64,
    pub min_grade_1in: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Sizing {
    pub min_dn_residential: u32,
    pub min_dn_industrial_large_commercial: u32,
    pub small_commercial_max_ha: f64,
    pub dn225_at_1in300_permitted_if_gsdf_lps_ge: f64,
    pub industrial_dn225_low_flow: IndustrialLowFlow,
    pub no_downstream_reduction: bool,
}

/// One Table 4.4 band: `[steepest_1in, flattest_1in, max_flow_lps, conditional]`.
#[derive(Debug, Clone, Deserialize)]
pub struct Band(pub f64, pub f64, pub f64, pub bool);

/// Table 4.4 row for one DN.
#[derive(Debug, Clone, Deserialize)]
pub struct GradeSpec {
    pub dn: u32,
    pub min_grade_1in: f64,
    pub max_grade_1in: f64,
    pub max_ac_spacing_m: f64,
    pub bands: Vec<Band>,
    pub conditional_min_grade_1in: Option<f64>,
    pub conditional_min_pumped_lps: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct IoIs {
    pub io_max_length_m: f64,
    pub io_min_grade_1in: f64,
    pub is_max_length_m: f64,
    pub is_min_grade_1in: f64,
    pub is_permitted_dn: Vec<u32>,
    pub is_max_parent_dn: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Cover {
    pub road_reserve_trafficable_row_m: f64,
    pub road_parallel_below_crown_m: f64,
    pub elsewhere_m: f64,
    pub uncertainty_increment_m: f64,
}

/// `[ds_dn, us_dn, fall_m]` (Table 4.8).
#[derive(Debug, Clone, Deserialize)]
pub struct FallRow(pub u32, pub u32, pub f64);

/// `[drop_dn, sewer_dn, vc_m, pvc_m]` (Table 4.5).
#[derive(Debug, Clone, Deserialize)]
pub struct DropRow(pub u32, pub u32, pub f64, pub f64);

#[derive(Debug, Clone, Deserialize)]
pub struct AccessChambers {
    pub required_at: Vec<String>,
    pub flood_freeboard_cover_m: f64,
    pub falls: Vec<FallRow>,
    pub drops: Vec<DropRow>,
}

/// A complete rule set for one standard revision.
#[derive(Debug, Clone, Deserialize)]
pub struct RuleSet {
    pub standard: String,
    pub revision: String,
    pub year: u32,
    pub loading: Loading,
    pub hydraulics: Hydraulics,
    pub sizing: Sizing,
    pub grades: Vec<GradeSpec>,
    pub io_is_sewers: IoIs,
    pub cover: Cover,
    pub access_chambers: AccessChambers,
}

/// Pipe material for drop-AC minimums (Table 4.5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Material {
    Vc,
    Pvc,
}

/// Residential loading rate lookup result (Tables 4.1/4.2).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ResidentialRate {
    /// Row actually used, e.g. "R15".
    pub rcode_used: String,
    pub persons_per_net_ha: f64,
    pub lpd: f64,
    pub gsdf_lps_per_ha: f64,
    /// True if the requested code was substituted (below minimum, or between rows).
    pub flagged: bool,
    pub note: Option<String>,
    pub clause: String,
}

/// Non-residential loading rate lookup result (Table 4.3).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NonResidentialRate {
    pub land_use: String,
    pub l_per_net_ha_day: f64,
    pub gsdf_lps_per_ha: f64,
    pub clause: String,
}

const DS50_TOML: &str = include_str!("../../../rulesets/ds50_v2r4.toml");

fn rcode_number(s: &str) -> Option<u32> {
    s.trim()
        .strip_prefix(['R', 'r'])
        .unwrap_or(s.trim())
        .parse()
        .ok()
}

impl RuleSet {
    /// The embedded DS50 Ver 2 Rev 4 rule set.
    pub fn ds50() -> RuleSet {
        Self::from_toml_str(DS50_TOML).expect("embedded DS50 ruleset must parse")
    }

    /// Raw TOML text of the embedded DS50 rule set.
    pub fn ds50_toml() -> &'static str {
        DS50_TOML
    }

    pub fn from_toml_str(s: &str) -> Result<RuleSet> {
        Ok(toml::from_str(s)?)
    }

    pub fn from_path(path: impl AsRef<Path>) -> Result<RuleSet> {
        Self::from_toml_str(&std::fs::read_to_string(path)?)
    }

    /// Residential rate for a Local Authority density code (4.2.1(c), Tables 4.1/4.2).
    ///
    /// Codes below the minimum (R15) or unparseable use R15; codes between rows use the
    /// next higher row (conservative). Substitutions set `flagged`.
    pub fn residential_rate(
        &self,
        region: Region,
        ground: Ground,
        rcode: &str,
    ) -> Result<ResidentialRate> {
        let (rows, table) = match region {
            Region::South => (&self.loading.residential.south.rows, "Table 4.1"),
            Region::North => (&self.loading.residential.north.rows, "Table 4.2"),
        };
        let min = rcode_number(&self.loading.min_rcode).unwrap_or(15);
        let mut note = None;
        let mut want = match rcode_number(rcode) {
            Some(n) if n >= min => n,
            Some(_) => {
                note = Some(format!(
                    "{rcode} is below {}; {} used per 4.2.1(c)",
                    self.loading.min_rcode, self.loading.min_rcode
                ));
                min
            }
            None => {
                note = Some(format!(
                    "'{rcode}' not recognised; {} used per 4.2.1(c)",
                    self.loading.min_rcode
                ));
                min
            }
        };
        if want < min {
            want = min;
        }
        let row = rows
            .iter()
            .find(|r| rcode_number(&r.0).is_some_and(|n| n >= want))
            .ok_or_else(|| RulesError::RcodeTooHigh(rcode.into()))?;
        if rcode_number(&row.0) != Some(want) && note.is_none() {
            note = Some(format!(
                "{rcode} is not a table row; next higher code {} used (conservative)",
                row.0
            ));
        }
        let (lpd, gsdf) = match ground {
            Ground::Dry => (row.3, row.4),
            Ground::Wet => (row.5, row.6),
        };
        Ok(ResidentialRate {
            rcode_used: row.0.clone(),
            persons_per_net_ha: row.2,
            lpd,
            gsdf_lps_per_ha: gsdf,
            flagged: note.is_some(),
            note,
            clause: format!("DS50 {table}, 4.2.1(c)"),
        })
    }

    /// Non-residential rate (Table 4.3). `perth_cba` exists only in the south table.
    pub fn non_residential_rate(
        &self,
        region: Region,
        ground: Ground,
        land_use: &str,
    ) -> Result<NonResidentialRate> {
        let rows = match region {
            Region::South => &self.loading.non_residential.south.rows,
            Region::North => &self.loading.non_residential.north.rows,
        };
        let row =
            rows.iter()
                .find(|r| r.0 == land_use)
                .ok_or_else(|| RulesError::UnknownLandUse {
                    land_use: land_use.into(),
                    table: "Table 4.3",
                    region,
                })?;
        let (l, g) = match ground {
            Ground::Dry => (row.1, row.2),
            Ground::Wet => (row.3, row.4),
        };
        Ok(NonResidentialRate {
            land_use: row.0.clone(),
            l_per_net_ha_day: l,
            gsdf_lps_per_ha: g,
            clause: "DS50 Table 4.3".into(),
        })
    }

    /// GSDF (L/s) from a daily flow in L/day: `daily / 86400 * sdf_factor` (4.2.1(i)).
    pub fn gsdf_lps(&self, daily_flow_l_per_day: f64) -> f64 {
        daily_flow_l_per_day / 86_400.0 * self.loading.sdf_factor
    }

    /// Table 4.4 row for a DN.
    pub fn grade_spec(&self, dn: u32) -> Result<&GradeSpec> {
        self.grades
            .iter()
            .find(|g| g.dn == dn)
            .ok_or(RulesError::UnknownDn(dn))
    }

    /// Table 4.4 maximum allowable flow (L/s) and whether the band is conditional.
    /// `None` if the DN is unknown or the grade is flatter than the last band.
    pub fn max_allowable_flow(&self, dn: u32, grade_1in: f64) -> Option<(f64, bool)> {
        let spec = self.grade_spec(dn).ok()?;
        spec.bands
            .iter()
            .find(|b| grade_1in <= b.1)
            .map(|b| (b.2, b.3))
    }

    /// Maximum d/D at design flow (4.2.1(m)).
    pub fn max_d_over_d(&self, dn: u32) -> f64 {
        let h = &self.hydraulics.max_d_over_d;
        h.get(&dn.to_string())
            .or_else(|| h.get("default"))
            .copied()
            .unwrap_or(0.6667)
    }

    /// Minimum fall (m) through an access chamber (Table 4.8).
    pub fn ac_fall(&self, us_dn: u32, ds_dn: u32) -> Option<f64> {
        self.access_chambers
            .falls
            .iter()
            .find(|r| r.0 == ds_dn && r.1 == us_dn)
            .map(|r| r.2)
    }

    /// Minimum invert difference (m) for a drop AC (Table 4.5).
    pub fn drop_min(&self, drop_dn: u32, sewer_dn: u32, material: Material) -> Option<f64> {
        self.access_chambers
            .drops
            .iter()
            .find(|r| r.0 == drop_dn && r.1 == sewer_dn)
            .map(|r| match material {
                Material::Vc => r.2,
                Material::Pvc => r.3,
            })
    }

    /// Minimum cover (m) to top of pipe by location (4.9).
    pub fn min_cover(&self, location: Location, levels_uncertain: bool) -> f64 {
        let c = &self.cover;
        let base = match location {
            Location::RoadReserve | Location::TrafficableRow => c.road_reserve_trafficable_row_m,
            Location::RoadParallel => c.road_parallel_below_crown_m,
            Location::Elsewhere => c.elsewhere_m,
        };
        base + if levels_uncertain {
            c.uncertainty_increment_m
        } else {
            0.0
        }
    }
}

/// Where a conduit lies, for cover minima (4.9).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Location {
    /// Within a road reserve.
    RoadReserve,
    /// Trafficable right of way.
    TrafficableRow,
    /// Parallel to a road (measured below road crown).
    RoadParallel,
    /// Anywhere else.
    Elsewhere,
}

impl FromStr for Location {
    type Err = String;
    fn from_str(s: &str) -> std::result::Result<Self, String> {
        match s.trim().to_ascii_lowercase().as_str() {
            "road_reserve" | "road" => Ok(Self::RoadReserve),
            "trafficable_row" | "row" => Ok(Self::TrafficableRow),
            "road_parallel" => Ok(Self::RoadParallel),
            "elsewhere" | "other" => Ok(Self::Elsewhere),
            o => Err(format!("unknown location '{o}'")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_relative_eq;

    #[test]
    fn embedded_parses() {
        let rs = RuleSet::ds50();
        assert_eq!(rs.standard, "DS50");
        assert_eq!(rs.grades.len(), 6);
        assert_eq!(rs.access_chambers.falls.len(), 21);
        let p = RuleSet::from_path(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../rulesets/ds50_v2r4.toml"
        ))
        .unwrap();
        assert_eq!(p.year, 2023);
    }

    #[test]
    fn residential() {
        let rs = RuleSet::ds50();
        let r = rs
            .residential_rate(Region::South, Ground::Dry, "R20")
            .unwrap();
        assert_relative_eq!(r.gsdf_lps_per_ha, 0.219);
        assert!(!r.flagged);
        assert!(r.clause.contains("4.1"));
        let w = rs
            .residential_rate(Region::North, Ground::Wet, "R25")
            .unwrap();
        assert_relative_eq!(w.gsdf_lps_per_ha, 0.395);
        assert_relative_eq!(w.lpd, 260.0);
        let low = rs
            .residential_rate(Region::South, Ground::Dry, "R10")
            .unwrap();
        assert_eq!(low.rcode_used, "R15");
        assert!(low.flagged);
        let bad = rs
            .residential_rate(Region::South, Ground::Dry, "xyz")
            .unwrap();
        assert_eq!(bad.rcode_used, "R15");
        let mid = rs
            .residential_rate(Region::South, Ground::Dry, "R35")
            .unwrap();
        assert_eq!(mid.rcode_used, "R40");
        assert!(mid.flagged);
        assert!(rs
            .residential_rate(Region::North, Ground::Dry, "R480")
            .is_err());
    }

    #[test]
    fn non_residential() {
        let rs = RuleSet::ds50();
        let i = rs
            .non_residential_rate(Region::South, Ground::Wet, "industrial")
            .unwrap();
        assert_relative_eq!(i.gsdf_lps_per_ha, 0.295);
        assert!(rs
            .non_residential_rate(Region::South, Ground::Dry, "perth_cba")
            .is_ok());
        assert!(rs
            .non_residential_rate(Region::North, Ground::Dry, "perth_cba")
            .is_err());
    }

    #[test]
    fn gsdf() {
        let rs = RuleSet::ds50();
        // 180 L/p/d x 52.5 p/ha = 9450 L/ha/d -> 0.164 L/s/ha
        assert_relative_eq!(rs.gsdf_lps(9450.0), 0.164, epsilon = 1e-3);
    }

    #[test]
    fn table_lookups() {
        let rs = RuleSet::ds50();
        assert_eq!(rs.max_allowable_flow(150, 100.0), Some((8.0, false)));
        assert_eq!(rs.max_allowable_flow(150, 200.0), Some((6.0, false)));
        assert_eq!(rs.max_allowable_flow(150, 250.0), Some((5.0, true)));
        assert_eq!(rs.max_allowable_flow(150, 300.0), None);
        assert_eq!(rs.max_allowable_flow(600, 800.0), Some((180.0, false)));
        assert_eq!(rs.max_allowable_flow(700, 100.0), None);
        assert_relative_eq!(rs.max_d_over_d(150), 0.5);
        assert_relative_eq!(rs.max_d_over_d(300), 0.6667);
        assert_eq!(rs.ac_fall(150, 300), Some(0.14));
        assert_eq!(rs.ac_fall(300, 150), None);
        assert_eq!(rs.drop_min(225, 300, Material::Pvc), Some(0.92));
        assert_eq!(rs.drop_min(150, 150, Material::Vc), Some(0.46));
        assert_eq!(rs.grade_spec(300).unwrap().max_ac_spacing_m, 150.0);
        assert!(rs.grade_spec(500).is_err());
        assert_relative_eq!(rs.min_cover(Location::RoadReserve, true), 1.2);
    }
}
