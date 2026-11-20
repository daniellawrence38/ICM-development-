//! Public input/output types.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use wwnet_core::Model;

#[derive(Debug, thiserror::Error)]
pub enum SimError {
    /// An error reported by the SWMM engine (code + SWMM's own message).
    #[error("SWMM error {code}: {message}")]
    Swmm { code: i32, message: String },
    #[error("missing data: {0}")]
    MissingData(String),
    #[error("inflow refers to unknown or unconnected node '{0}'")]
    UnknownNode(String),
    #[error("conduit '{conduit}' refers to unknown node '{node}'")]
    UnknownConduitNode { conduit: String, node: String },
    #[error("invalid options: {0}")]
    BadOptions(String),
    #[error("i/o error: {0}")]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, SimError>;

/// How the SWMM Manning `n` of each conduit is chosen.
///
/// SWMM's dynamic-wave solver uses Manning for gravity conduits (Darcy-Weisbach is only
/// available for force mains), so Colebrook `ks` must be mapped to an equivalent `n`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "mode", content = "value", rename_all = "snake_case")]
pub enum Roughness {
    /// One Manning n for every conduit (default 0.013, typical for PVC / vitrified clay).
    Manning(f64),
    /// Per conduit: the n that gives the same full-bore Colebrook-White velocity at the
    /// pipe's grade, using the conduit's `ks_mm` (or [`SimOptions::ks_mm`] when NULL).
    FromKs,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SimOptions {
    /// Total simulated hours, including warm-up.
    pub duration_h: f64,
    /// Hours at the start excluded from the reported statistics.
    pub warmup_h: f64,
    /// Interval of stored time-series samples, seconds.
    pub report_step_s: u32,
    /// Maximum (and initial) routing step, seconds; SWMM varies it downwards.
    pub routing_step_s: f64,
    /// Default Colebrook ks (mm) used by [`Roughness::FromKs`] when a conduit has none.
    pub ks_mm: f64,
    pub roughness: Roughness,
    /// Conduits shorter than this (m) are lengthened to it, with a warning.
    pub min_length_m: f64,
    /// Junction depth (m) when surface/invert levels are unknown.
    pub default_max_depth_m: f64,
    /// Keep full time series (see `series_ids`).
    pub keep_series: bool,
    /// Ids (nodes and/or conduits) whose series are kept; empty = all.
    pub series_ids: Vec<String>,
}

impl Default for SimOptions {
    fn default() -> Self {
        SimOptions {
            duration_h: 48.0,
            warmup_h: 24.0,
            report_step_s: 300,
            routing_step_s: 5.0,
            ks_mm: 1.0,
            roughness: Roughness::Manning(0.013),
            min_length_m: 5.0,
            default_max_depth_m: 3.0,
            keep_series: false,
            series_ids: Vec::new(),
        }
    }
}

/// A dry-weather inflow at a node. With `profile`, the flow is `base_lps * factor`, the
/// factor interpolated linearly (cyclically) between `(minute_of_day, factor)` points and
/// repeated every day; otherwise `base_lps` is constant.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NodeInflow {
    pub node_id: String,
    pub base_lps: f64,
    #[serde(default)]
    pub profile: Option<Vec<(u32, f64)>>,
}

/// Only `model.nodes` and `model.conduits` are used.
#[derive(Debug, Clone)]
pub struct SimInput<'a> {
    pub model: &'a Model,
    pub inflows: Vec<NodeInflow>,
    pub options: SimOptions,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LinkResult {
    pub conduit_id: String,
    pub max_flow_lps: f64,
    /// Minutes since simulation start at which `max_flow_lps` occurred.
    pub max_flow_time_min: f64,
    pub mean_flow_lps: f64,
    pub max_depth_m: f64,
    /// Max depth / diameter.
    pub max_d_over_d: f64,
    pub max_velocity_mps: f64,
    /// Full-bore capacity as computed by SWMM (Manning), L/s.
    pub full_flow_lps: f64,
    /// `max_flow / full_flow`.
    pub max_capacity_ratio: f64,
    /// Hours with depth >= 0.999 D.
    pub surcharged_h: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NodeResult {
    pub node_id: String,
    pub invert_level: f64,
    pub max_hgl: f64,
    pub max_depth_m: f64,
    /// Maximum connected pipe crown (soffit) level.
    pub soffit_level: f64,
    pub is_outfall: bool,
    /// Flooding (overflow) volume after warm-up, m3.
    pub flood_volume_m3: f64,
    pub flood_duration_h: f64,
    pub max_flood_lps: f64,
    /// Hours with HGL above the soffit.
    pub surcharged_h: f64,
    /// Maximum HGL height above the soffit (0 when never surcharged), m.
    pub max_surcharge_m: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LinkSeries {
    pub flow_lps: Vec<f64>,
    pub depth_m: Vec<f64>,
    pub velocity_mps: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NodeSeries {
    pub head_m: Vec<f64>,
    pub depth_m: Vec<f64>,
    pub overflow_lps: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct SimSeries {
    /// Sample times, minutes since simulation start (includes the warm-up).
    pub times_min: Vec<f64>,
    pub links: BTreeMap<String, LinkSeries>,
    pub nodes: BTreeMap<String, NodeSeries>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IdMapEntry {
    /// "node" or "conduit".
    pub kind: String,
    pub original: String,
    pub swmm: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SimResult {
    pub swmm_version: i32,
    pub links: Vec<LinkResult>,
    pub nodes: Vec<NodeResult>,
    /// Runoff continuity error, % (0 here: no hydrology).
    pub continuity_runoff_error_pct: f64,
    /// Flow-routing continuity error, %.
    pub continuity_flow_error_pct: f64,
    pub warnings: Vec<String>,
    /// The generated SWMM input file, for export.
    pub inp_text: String,
    pub id_map: Vec<IdMapEntry>,
    pub series: Option<SimSeries>,
}
