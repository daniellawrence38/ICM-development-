//! File-level services shared by the `wwnet` CLI and the `wwnet_native` Python module.
//!
//! Each function opens a GeoPackage, runs the operation and (optionally) persists results
//! exactly as the CLI does. JSON helpers produce the text that `wwnet check --json` /
//! `wwnet design --json` print and that the front ends parse.
use crate::{run_design, to_conduit_results, DesignOutput};
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::Path;
use wwnet_core::profile::{parse_profile, ProfileImport};
use wwnet_core::{gpkg, validate, Error, Issue, Model, Network, Point, Result, Severity};
use wwnet_rules::RuleSet;

fn loading<T>(path: &Path, r: Result<T>) -> Result<T> {
    r.map_err(|e| Error::Other(format!("loading {}: {e}", path.display())))
}

fn to_json<T: Serialize>(v: &T) -> Result<String> {
    serde_json::to_string_pretty(v).map_err(|e| Error::Other(e.to_string()))
}

/// Create an empty model GeoPackage (current schema).
pub fn new_model(path: impl AsRef<Path>) -> Result<()> {
    gpkg::create(path)
}

/// Upgrade a model GeoPackage to the current schema (idempotent).
pub fn upgrade_model(path: impl AsRef<Path>) -> Result<()> {
    gpkg::upgrade(path)
}

/// Build a new model GeoPackage from JSON (the serde form of [`Model`]: nodes, conduits,
/// subcatchments, pump_stations, pumps, rising_mains; geometry as `[x, y]` / `[[x, y], ...]`).
/// Fails if the file exists and is not empty unless `overwrite`. On any failure after the
/// file was created it is removed again, so no half-written model is left behind.
pub fn write_model_json(path: impl AsRef<Path>, model_json: &str, overwrite: bool) -> Result<()> {
    let path = path.as_ref();
    let model: Model = serde_json::from_str(model_json)
        .map_err(|e| Error::Other(format!("invalid model JSON: {e}")))?;
    if overwrite && path.is_file() {
        std::fs::remove_file(path)?;
    }
    gpkg::create(path)?;
    if let Err(e) = model.write(path) {
        let _ = std::fs::remove_file(path);
        return Err(e);
    }
    Ok(())
}

/// Validate a model. With `write`, replaces the `check` rows of `wwn_issues`.
pub fn check_file(path: impl AsRef<Path>, write: bool) -> Result<Vec<Issue>> {
    let path = path.as_ref();
    let model = loading(path, Model::load(path))?;
    let net = Network::build(&model)?;
    let issues = validate(&model, &net);
    if write {
        Model::store_issues(path, "check", &issues)?;
    }
    Ok(issues)
}

/// Number of error-severity issues (the CLI exits 1 when this is non-zero).
pub fn error_count(issues: &[Issue]) -> usize {
    issues
        .iter()
        .filter(|i| i.severity == Severity::Error)
        .count()
}

/// Pretty JSON exactly as `wwnet check --json` prints it.
pub fn issues_json(issues: &[Issue]) -> Result<String> {
    to_json(&issues)
}

/// An issue from a front end (e.g. the importer), optionally with its own location.
#[derive(Debug, Clone, Deserialize)]
pub struct IssueIn {
    #[serde(flatten)]
    pub issue: Issue,
    #[serde(default)]
    pub x: Option<f64>,
    #[serde(default)]
    pub y: Option<f64>,
}

/// Replace the `source` rows of `wwn_issues` with `issues_json` (a JSON list of issues:
/// severity, code, object_type, object_id, clause, message, optional x / y). Issues without x / y
/// are placed at their object. Returns the number stored.
pub fn store_issues_json(path: impl AsRef<Path>, source: &str, issues_json: &str) -> Result<usize> {
    let items: Vec<IssueIn> = serde_json::from_str(issues_json)
        .map_err(|e| Error::Other(format!("invalid issues JSON: {e}")))?;
    let located: Vec<(Issue, Option<Point>)> = items
        .into_iter()
        .map(|i| {
            let p = match (i.x, i.y) {
                (Some(x), Some(y)) if x.is_finite() && y.is_finite() => Some(Point::new(x, y)),
                _ => None,
            };
            (i.issue, p)
        })
        .collect();
    Model::store_located_issues(path, source, &located)?;
    Ok(located.len())
}

/// Run the DS 50 design. With `write`, saves `wwn_results_conduits` (a map layer) and replaces the
/// `design` rows of `wwn_issues`. `ruleset` is a rule set TOML path (default: embedded DS 50 Ver 2 Rev 4).
pub fn design_file(
    path: impl AsRef<Path>,
    ruleset: Option<&Path>,
    write: bool,
) -> Result<DesignOutput> {
    let path = path.as_ref();
    let rules = match ruleset {
        Some(p) => RuleSet::from_path(p)
            .map_err(|e| Error::Other(format!("loading {}: {e}", p.display())))?,
        None => RuleSet::ds50(),
    };
    let model = loading(path, Model::load(path))?;
    let out = run_design(&model, &rules)?;
    if write {
        Model::save_results(path, &to_conduit_results(&out, &rules)?)?;
        Model::store_issues(path, "design", &out.issues)?;
    }
    Ok(out)
}

/// Pretty JSON exactly as `wwnet design --json` prints it.
pub fn design_json(out: &DesignOutput) -> Result<String> {
    to_json(out)
}

/// Import a diurnal profile (CSV / tab-separated `time,factor` text) into a model file, upgrading
/// the file to the current schema first if needed. An existing profile with the same id is replaced.
/// With `set_default` the profile becomes `wwn_meta.default_profile`. The values are only normalised
/// to mean 1 when `normalise` is set; otherwise a mean far from 1 is reported in `warnings`.
pub fn import_profile(
    path: impl AsRef<Path>,
    profile_id: &str,
    csv_text: &str,
    description: &str,
    set_default: bool,
    normalise: bool,
) -> Result<ProfileImport> {
    let path = path.as_ref();
    let (profile, mut info) = parse_profile(profile_id, csv_text, description, normalise)?;
    gpkg::upgrade(path)?;
    let mut c = gpkg::open(path)?;
    let tx = c.transaction()?;
    gpkg::put_profile(&tx, &profile)?;
    if set_default {
        gpkg::set_default_profile(&tx, Some(&profile.profile_id))?;
    }
    tx.commit()?;
    info.is_default = set_default;
    Ok(info)
}

/// Pretty JSON of a profile import result.
pub fn profile_import_json(p: &ProfileImport) -> Result<String> {
    to_json(p)
}

// ------------------------------------------------------------------------- trace

/// Loading upstream of the trace start (from an in-memory design run; flows are `None` when the
/// design cannot run, e.g. the network has a cycle).
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct TraceSummary {
    /// Node the trace started from (the upstream node of `start_conduit` for upstream traces).
    pub start_node: String,
    /// Set when the trace started from a conduit id.
    pub start_conduit: Option<String>,
    pub upstream_nodes: usize,
    pub upstream_conduits: usize,
    /// Sum of node-load connections at the start node and every node upstream of it.
    pub upstream_connections: i64,
    /// Sub-catchments draining to the start node or upstream of it.
    pub upstream_subcatchments: usize,
    /// ADWF of the node loads and sub-catchments at the start node and upstream of it (the dry
    /// weather inflow the simulation uses; manual or DS 50 daily flow / 86400).
    pub adwf_lps: Option<f64>,
    /// Accumulated design GSDF and pumped flow at the start (the design flow in `start_conduit`).
    pub gsdf_lps: Option<f64>,
    pub pumped_lps: Option<f64>,
}

/// Trace result; a direction that was not requested is omitted from the JSON.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct TraceResult {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub upstream: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub downstream: Option<Vec<String>>,
    /// Gravity conduits on the traced paths (including the start conduit).
    pub conduits: Vec<String>,
    pub summary: TraceSummary,
}

/// Trace upstream and/or downstream (neither flag set means both, as in the CLI) of `start`, a
/// node id or a conduit id. From a conduit, the upstream trace starts at its upstream node and the
/// downstream trace at its downstream node; the conduit itself is included.
pub fn trace_file(
    path: impl AsRef<Path>,
    start: &str,
    up: bool,
    down: bool,
) -> Result<TraceResult> {
    let path = path.as_ref();
    let model = loading(path, Model::load(path))?;
    trace_model(&model, start, up, down)
}

/// [`trace_file`] on a loaded model.
pub fn trace_model(model: &Model, start: &str, up: bool, down: bool) -> Result<TraceResult> {
    let net = Network::build(model)?;
    let (up, down) = if !up && !down {
        (true, true)
    } else {
        (up, down)
    };
    let (us_node, ds_node, start_conduit) = if net.index.contains_key(start) {
        (start.to_string(), start.to_string(), None)
    } else if let Some(c) = model.conduits.iter().find(|c| c.conduit_id == start) {
        (
            c.us_node_id.clone(),
            c.ds_node_id.clone(),
            Some(c.conduit_id.clone()),
        )
    } else {
        return Err(Error::NodeNotFound(start.to_string()));
    };
    let own = |v: Vec<&str>| v.into_iter().map(String::from).collect::<Vec<_>>();
    let mut conduits: Vec<String> = start_conduit.iter().cloned().collect();
    let mut out = TraceResult::default();
    let mut upstream_set: HashSet<String> = HashSet::from([us_node.clone()]);
    if up {
        let (nodes, links) = net.trace_links(&us_node, true)?;
        let mut nodes = own(nodes);
        if start_conduit.is_some() {
            nodes.insert(0, us_node.clone());
        }
        upstream_set.extend(nodes.iter().cloned());
        conduits.extend(own(links));
        out.upstream = Some(nodes);
    } else {
        let (nodes, _) = net.trace_links(&us_node, true)?;
        upstream_set.extend(own(nodes));
    }
    if down {
        let (nodes, links) = net.trace_links(&ds_node, false)?;
        let mut nodes = own(nodes);
        if start_conduit.is_some() {
            nodes.insert(0, ds_node.clone());
        }
        conduits.extend(own(links));
        out.downstream = Some(nodes);
    }
    let mut seen = HashSet::new();
    conduits.retain(|c| seen.insert(c.clone()));
    out.conduits = conduits;

    let rules = RuleSet::ds50();
    let flows = run_design(model, &rules).ok();
    let (node_loads, _) = crate::loads::resolve_node_loads(model);
    let (sub_loads, _) = crate::loads::resolve_subcatchment_loads(model, &rules);
    let adwf: f64 = node_loads
        .iter()
        .filter(|l| upstream_set.contains(&l.load.node_id))
        .map(|l| l.adwf_lps)
        .chain(
            sub_loads
                .iter()
                .filter(|s| upstream_set.contains(&s.subcatchment.outlet_node_id))
                .map(|s| s.adwf_lps),
        )
        .sum();
    let at = flows
        .as_ref()
        .and_then(|d| d.node_flows.iter().find(|n| n.node_id == us_node));
    out.summary = TraceSummary {
        start_node: us_node.clone(),
        start_conduit,
        upstream_nodes: upstream_set.len() - 1,
        upstream_conduits: model
            .conduits
            .iter()
            .filter(|c| {
                upstream_set.contains(&c.us_node_id) && upstream_set.contains(&c.ds_node_id)
            })
            .count(),
        upstream_connections: model
            .node_loads
            .iter()
            .filter(|l| upstream_set.contains(&l.node_id))
            .filter_map(|l| l.connections)
            .sum(),
        upstream_subcatchments: model
            .subcatchments
            .iter()
            .filter(|s| upstream_set.contains(&s.outlet_node_id))
            .count(),
        adwf_lps: Some(adwf),
        gsdf_lps: at.map(|n| n.accumulated_gsdf_lps),
        pumped_lps: at.map(|n| n.pumped_lps),
    };
    Ok(out)
}

/// Pretty JSON of a trace result.
pub fn trace_json(t: &TraceResult) -> Result<String> {
    to_json(t)
}

// ------------------------------------------------------------- settings / profiles

/// Loading settings in `wwn_meta` plus the profiles in the file.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelSettings {
    pub schema_version: i64,
    pub adwf_per_connection_lpd: f64,
    pub peaking_factor: f64,
    pub default_profile: Option<String>,
    pub profiles: Vec<ProfileSummary>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ProfileSummary {
    pub profile_id: String,
    pub description: Option<String>,
    pub points: usize,
    pub interval_min: Option<i64>,
    pub mean_factor: Option<f64>,
}

/// Read the model's loading settings and profile list (upgrading old files first).
pub fn get_settings(path: impl AsRef<Path>) -> Result<ModelSettings> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let m = loading(path, Model::load(path))?;
    Ok(ModelSettings {
        schema_version: gpkg::schema_version(path)?,
        adwf_per_connection_lpd: m.load_settings.adwf_per_connection_lpd,
        peaking_factor: m.load_settings.peaking_factor,
        default_profile: m.load_settings.default_profile.clone(),
        profiles: m
            .profiles
            .iter()
            .map(|p| ProfileSummary {
                profile_id: p.profile_id.clone(),
                description: p.description.clone(),
                points: p.points.len(),
                interval_min: p.interval_min,
                mean_factor: p.mean_factor,
            })
            .collect(),
    })
}

/// Change loading settings; `None` keeps a value. `default_profile` `Some("")` clears it; an
/// unknown profile id is an error. Returns the settings after the change.
pub fn set_settings(
    path: impl AsRef<Path>,
    adwf_per_connection_lpd: Option<f64>,
    peaking_factor: Option<f64>,
    default_profile: Option<&str>,
) -> Result<ModelSettings> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let mut c = gpkg::open(path)?;
    let tx = c.transaction()?;
    for (key, v, lo, hi) in [
        (
            "adwf_per_connection_lpd",
            adwf_per_connection_lpd,
            0.0,
            10_000.0,
        ),
        ("peaking_factor", peaking_factor, 1.0, 10.0),
    ] {
        if let Some(v) = v {
            if !(v.is_finite() && v >= lo && v <= hi) {
                return Err(Error::Other(format!(
                    "{key} must be between {lo} and {hi}, not {v}"
                )));
            }
            tx.execute(
                "INSERT OR REPLACE INTO wwn_meta (key,value) VALUES (?,?)",
                params![key, v.to_string()],
            )?;
        }
    }
    if let Some(d) = default_profile {
        let d = d.trim();
        if d.is_empty() {
            gpkg::set_default_profile(&tx, None)?;
        } else {
            let exists: Option<i64> = tx
                .query_row(
                    "SELECT 1 FROM wwn_profiles WHERE profile_id=? LIMIT 1",
                    [d],
                    |r| r.get(0),
                )
                .optional()?;
            if exists.is_none() {
                return Err(Error::Other(format!("profile '{d}' does not exist")));
            }
            gpkg::set_default_profile(&tx, Some(d))?;
        }
    }
    tx.commit()?;
    get_settings(path)
}

/// Set `profile_id` on node loads (`table` = `node_loads`, ids = load or node ids) or sub-catchments
/// (`table` = `subcatchments`, ids = sub-catchment ids). An empty profile id clears it (the model
/// default applies). Returns the number of rows changed.
pub fn assign_profile(
    path: impl AsRef<Path>,
    table: &str,
    ids: &[String],
    profile_id: &str,
) -> Result<usize> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let mut c = gpkg::open(path)?;
    let pid = profile_id.trim();
    if !pid.is_empty() {
        let exists: Option<i64> = c
            .query_row(
                "SELECT 1 FROM wwn_profiles WHERE profile_id=? LIMIT 1",
                [pid],
                |r| r.get(0),
            )
            .optional()?;
        if exists.is_none() {
            return Err(Error::Other(format!("profile '{pid}' does not exist")));
        }
    }
    let sql = match table {
        "node_loads" | "wwn_node_loads" => {
            "UPDATE wwn_node_loads SET profile_id=?1 WHERE load_id=?2 OR node_id=?2"
        }
        "subcatchments" | "wwn_subcatchments" => {
            "UPDATE wwn_subcatchments SET profile_id=?1 WHERE subcatchment_id=?2"
        }
        t => {
            return Err(Error::Other(format!(
                "table must be 'node_loads' or 'subcatchments', not '{t}'"
            )))
        }
    };
    let value: Option<&str> = if pid.is_empty() { None } else { Some(pid) };
    let tx = c.transaction()?;
    let mut n = 0;
    for id in ids {
        n += tx.execute(sql, params![value, id])?;
    }
    tx.commit()?;
    Ok(n)
}

/// Pretty JSON of any serialisable service result.
pub fn json<T: Serialize>(v: &T) -> Result<String> {
    to_json(v)
}
