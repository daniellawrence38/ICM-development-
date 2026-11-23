//! File-level simulation service shared by the `wwnet simulate` CLI and the `wwnet_native`
//! Python module: loads a model GeoPackage, builds the dry-weather inflows, runs the SWMM
//! engine, optionally stores the results in the `wwn_sim_*` tables and returns a JSON-able
//! [`SimulateOutput`].
use crate::inp::profile_factor;
use crate::{simulate, NodeInflow, Roughness, SimInput, SimOptions, SimResult};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use wwnet_core::{gpkg, Error, Issue, Model, Network, Result, Severity};
use wwnet_design::loads::{resolve_node_loads, resolve_subcatchment_loads};
use wwnet_rules::RuleSet;

/// Runs kept in the `wwn_sim_*` tables (oldest are deleted).
pub const RUNS_KEPT: usize = 10;

/// Options of [`simulate_file`] (the JSON `options` of a run).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ServiceOptions {
    pub duration_h: f64,
    pub warmup_h: f64,
    pub report_step_s: u32,
    pub routing_step_s: f64,
    /// `"manning"` (one n for all conduits) or `"ks"` (equivalent n from the model's ks).
    pub roughness: String,
    pub manning_n: f64,
    /// Multiplier on every inflow (e.g. 1.5 to test the peak).
    pub flow_scale: f64,
    /// Extra node / conduit ids whose time series are stored (besides the downstream-most 5 conduits).
    pub series_ids: Vec<String>,
    /// Free-text run label stored with the run (e.g. "existing x1.0").
    pub label: String,
}

impl Default for ServiceOptions {
    fn default() -> Self {
        ServiceOptions {
            duration_h: 48.0,
            warmup_h: 24.0,
            report_step_s: 300,
            routing_step_s: 5.0,
            // Same basis as the design: Colebrook-White ks (DS 50: 1.0 mm), as a per-conduit n.
            roughness: "ks".into(),
            manning_n: 0.013,
            flow_scale: 1.0,
            series_ids: Vec::new(),
            label: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SimSummary {
    /// Sum of all inflows at flow_scale 1 (L/s).
    pub inflow_adwf_lps: f64,
    /// Sum of all inflows as simulated, i.e. including `flow_scale` (L/s).
    pub inflow_simulated_lps: f64,
    /// Sum of the maximum flows of the conduits discharging to outfalls (L/s).
    pub peak_outlet_flow_lps: f64,
    pub continuity_error_pct: f64,
    pub conduits: usize,
    pub nodes: usize,
    pub surcharged_conduits: usize,
    pub flooded_nodes: usize,
    pub total_flood_volume_m3: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct SimLinkOut {
    pub conduit_id: String,
    pub dn: i64,
    pub max_flow_lps: f64,
    pub max_depth_m: f64,
    pub max_d_over_d: f64,
    pub max_velocity_mps: f64,
    /// Time of day (HH:MM) of the peak flow.
    pub time_of_max_flow: String,
    pub surcharged_min: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct SimNodeOut {
    pub node_id: String,
    pub max_hgl_m: f64,
    pub max_depth_m: f64,
    pub flood_volume_m3: f64,
    pub surcharged_min: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct SimulateOutput {
    /// Run id (UTC timestamp); `None` when nothing was written.
    pub run_id: Option<String>,
    pub label: String,
    pub options: ServiceOptions,
    pub summary: SimSummary,
    pub links: Vec<SimLinkOut>,
    pub nodes: Vec<SimNodeOut>,
    /// Loading / engine findings (stored in `wwn_issues` with source `simulate` when written).
    pub issues: Vec<Issue>,
    /// The issue messages (warning and error severity), for plain-text front ends.
    pub warnings: Vec<String>,
    /// Ids whose series were stored (empty when not written).
    pub series_ids: Vec<String>,
    /// The generated SWMM input file.
    #[serde(skip)]
    pub inp_text: String,
}

fn other(msg: impl Into<String>) -> Error {
    Error::Other(msg.into())
}

fn to_sim_options(o: &ServiceOptions, series: &[String]) -> Result<SimOptions> {
    let roughness = match o.roughness.to_ascii_lowercase().as_str() {
        "manning" => {
            if !(o.manning_n > 0.0 && o.manning_n < 0.2) {
                return Err(other(format!("invalid Manning n {}", o.manning_n)));
            }
            Roughness::Manning(o.manning_n)
        }
        "ks" => Roughness::FromKs,
        r => {
            return Err(other(format!(
                "roughness must be 'manning' or 'ks', not '{r}'"
            )))
        }
    };
    if !(o.flow_scale.is_finite() && o.flow_scale >= 0.0) {
        return Err(other(format!("invalid flow scale {}", o.flow_scale)));
    }
    Ok(SimOptions {
        duration_h: o.duration_h,
        warmup_h: o.warmup_h,
        report_step_s: o.report_step_s,
        routing_step_s: o.routing_step_s,
        roughness,
        keep_series: !series.is_empty(),
        series_ids: series.to_vec(),
        ..SimOptions::default()
    })
}

type Points = Vec<(u32, f64)>;

/// One inflow component: base flow (L/s) and the profile id used.
struct Component {
    base: f64,
    profile: Option<String>,
}

/// Collect the dry-weather inflows per node (node loads and sub-catchments, validated by the shared
/// [`wwnet_design::loads`] module), each with its profile. Returns the inflows (at flow_scale 1),
/// the total ADWF and the issues.
pub fn build_inflows(model: &Model) -> (Vec<NodeInflow>, f64, Vec<Issue>) {
    let mut issues = Vec::new();
    let rules = RuleSet::ds50();
    let ls = &model.load_settings;
    let profiles: HashMap<&str, Vec<(u32, f64)>> = model
        .profiles
        .iter()
        .map(|p| {
            (
                p.profile_id.as_str(),
                p.points
                    .iter()
                    .map(|x| (x.minute_of_day.max(0) as u32, x.factor))
                    .collect(),
            )
        })
        .collect();
    if let Some(d) = &ls.default_profile {
        if !profiles.contains_key(d.as_str()) {
            issues.push(Issue::new(
                Severity::Warning,
                "DEFAULT_PROFILE_MISSING",
                "model",
                "",
                None,
                format!("default profile '{d}' does not exist"),
            ));
        }
    }
    let mut by_node: BTreeMap<String, Vec<Component>> = BTreeMap::new();
    let mut order: Vec<String> = Vec::new();
    let mut add = |node: &str, base: f64, profile: Option<&String>| {
        if !by_node.contains_key(node) {
            order.push(node.to_string());
        }
        by_node
            .entry(node.to_string())
            .or_default()
            .push(Component {
                base,
                profile: profile.cloned().or_else(|| ls.default_profile.clone()),
            });
    };
    let (node_loads, found) = resolve_node_loads(model);
    issues.extend(found);
    for l in node_loads.iter().filter(|l| l.adwf_lps > 0.0) {
        add(&l.load.node_id, l.adwf_lps, l.load.profile_id.as_ref());
    }
    let (subs, found) = resolve_subcatchment_loads(model, &rules);
    issues.extend(found);
    for s in subs {
        add(
            &s.subcatchment.outlet_node_id,
            s.adwf_lps,
            s.subcatchment.profile_id.as_ref(),
        );
    }

    // Resolve profiles; warn once for constant flows and for unknown ids.
    let mut missing: Vec<String> = Vec::new();
    let mut constant = 0usize;
    let mut total = 0.0;
    let mut inflows = Vec::new();
    for node in order {
        let comps = &by_node[&node];
        let mut resolved: Vec<(f64, Option<&Points>)> = Vec::new();
        for c in comps {
            total += c.base;
            let pts = match c.profile.as_deref() {
                Some(id) => match profiles.get(id).filter(|p| !p.is_empty()) {
                    Some(p) => Some(p),
                    None => {
                        if !missing.iter().any(|m| m == id) {
                            missing.push(id.to_string());
                        }
                        None
                    }
                },
                None => None,
            };
            if pts.is_none() {
                constant += 1;
            }
            resolved.push((c.base, pts));
        }
        let same = resolved.windows(2).all(|w| match (w[0].1, w[1].1) {
            (None, None) => true,
            (Some(a), Some(b)) => std::ptr::eq(a, b),
            _ => false,
        });
        if same {
            let base: f64 = resolved.iter().map(|r| r.0).sum();
            inflows.push(NodeInflow {
                node_id: node,
                base_lps: base,
                profile: resolved[0].1.cloned(),
            });
        } else {
            // Different profiles at one node: sum them on the union of their breakpoints.
            let mut mins: Vec<u32> = resolved
                .iter()
                .filter_map(|r| r.1)
                .flat_map(|p| p.iter().map(|x| x.0 % 1440))
                .collect();
            mins.push(0);
            mins.sort_unstable();
            mins.dedup();
            let pts = mins
                .iter()
                .map(|&m| {
                    let q: f64 = resolved
                        .iter()
                        .map(|(b, p)| match p {
                            Some(p) => b * profile_factor(&sorted(p), m as f64),
                            None => *b,
                        })
                        .sum();
                    (m, q)
                })
                .collect();
            inflows.push(NodeInflow {
                node_id: node,
                base_lps: 1.0,
                profile: Some(pts),
            });
        }
    }
    for id in missing {
        issues.push(Issue::new(
            Severity::Warning,
            "PROFILE_MISSING",
            "model",
            "",
            None,
            format!("profile '{id}' not found: constant ADWF used"),
        ));
    }
    if constant > 0 {
        issues.push(Issue::new(
            Severity::Warning,
            "NO_PROFILE",
            "model",
            "",
            None,
            "no diurnal profile: constant ADWF",
        ));
    }
    (inflows, total, issues)
}

/// Pump stations are not simulated yet: one warning per station with the ADWF that reaches its
/// wet well (and so never reaches the discharge node), replacing the engine's generic note.
fn pump_station_issues(model: &Model, inflows: &[NodeInflow], scale: f64) -> Vec<Issue> {
    let Ok(net) = Network::build(model) else {
        return Vec::new();
    };
    let base: HashMap<&str, f64> = inflows
        .iter()
        .map(|f| {
            let mean = match &f.profile {
                Some(p) if !p.is_empty() => {
                    let p = sorted(p);
                    (0..1440)
                        .step_by(5)
                        .map(|m| profile_factor(&p, m as f64))
                        .sum::<f64>()
                        / 288.0
                }
                _ => 1.0,
            };
            (f.node_id.as_str(), f.base_lps * mean)
        })
        .collect();
    let mut out = Vec::new();
    for ps in &model.pump_stations {
        let lost = ps.wet_well_node_id.as_deref().map(|w| {
            let mut nodes: Vec<&str> = net.upstream_trace(w).unwrap_or_default();
            nodes.push(w);
            nodes.iter().filter_map(|n| base.get(n)).sum::<f64>() * scale
        });
        let msg = match (lost, ps.discharge_node_id.as_deref()) {
            (Some(q), Some(d)) => format!(
                "pump station is not simulated yet: {q:.3} L/s ADWF reaching wet well {} is not passed on to {d}; flows downstream of {d} are too low",
                ps.wet_well_node_id.as_deref().unwrap_or("")
            ),
            (Some(q), None) => format!(
                "pump station is not simulated yet: {q:.3} L/s ADWF reaching wet well {} stops there",
                ps.wet_well_node_id.as_deref().unwrap_or("")
            ),
            _ => "pump station is not simulated yet (no wet well node set)".to_string(),
        };
        out.push(Issue::new(
            Severity::Warning,
            "PUMP_STATION_NOT_SIMULATED",
            "pump_station",
            &ps.ps_id,
            None,
            msg,
        ));
    }
    if !model.rising_mains.is_empty() {
        out.push(Issue::new(
            Severity::Warning,
            "RISING_MAIN_NOT_SIMULATED",
            "model",
            "",
            None,
            format!(
                "{} rising main(s) are not simulated yet",
                model.rising_mains.len()
            ),
        ));
    }
    out
}

/// Peak d/D class used for the results layer: low < 0.5, medium < 0.75, high < 1.0, else (or any
/// surcharged time) surcharged.
pub fn d_class(max_d_over_d: f64, surcharged_min: f64) -> &'static str {
    if max_d_over_d >= 1.0 || surcharged_min > 0.0 {
        "surcharged"
    } else if max_d_over_d >= 0.75 {
        "high"
    } else if max_d_over_d >= 0.5 {
        "medium"
    } else {
        "low"
    }
}

fn sorted(p: &[(u32, f64)]) -> Vec<(u32, f64)> {
    let mut v = p.to_vec();
    v.sort_by_key(|x| x.0);
    v
}

fn hhmm(min: f64) -> String {
    let m = (min.max(0.0).round() as u64) % 1440;
    format!("{:02}:{:02}", m / 60, m % 60)
}

/// The five conduits nearest the outfalls (breadth-first upstream from the outfall nodes).
fn downstream_most(model: &Model, n: usize) -> Vec<String> {
    let has_out: HashSet<&str> = model
        .conduits
        .iter()
        .map(|c| c.us_node_id.as_str())
        .collect();
    let outfalls: HashSet<&str> = model
        .conduits
        .iter()
        .map(|c| c.ds_node_id.as_str())
        .filter(|d| !has_out.contains(d))
        .collect();
    let mut into: HashMap<&str, Vec<&str>> = HashMap::new();
    for c in &model.conduits {
        into.entry(c.ds_node_id.as_str())
            .or_default()
            .push(c.conduit_id.as_str());
    }
    let ds_of: HashMap<&str, &str> = model
        .conduits
        .iter()
        .map(|c| (c.conduit_id.as_str(), c.us_node_id.as_str()))
        .collect();
    let mut queue: std::collections::VecDeque<&str> = outfalls.iter().copied().collect();
    let mut seen_nodes: HashSet<&str> = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    while let Some(nd) = queue.pop_front() {
        if !seen_nodes.insert(nd) {
            continue;
        }
        for &c in into.get(nd).map(|v| v.as_slice()).unwrap_or(&[]) {
            if out.len() < n && !out.iter().any(|o| o == c) {
                out.push(c.to_string());
            }
            if let Some(&us) = ds_of.get(c) {
                queue.push_back(us);
            }
        }
        if out.len() >= n {
            break;
        }
    }
    out
}

/// Run the dynamic simulation on a model file. With `write`, the file is upgraded to the
/// current schema and the results are stored in `wwn_sim_*` (keeping the latest 10 runs).
pub fn simulate_file(
    path: impl AsRef<Path>,
    opts: &ServiceOptions,
    write: bool,
) -> Result<SimulateOutput> {
    let path = path.as_ref();
    let model = Model::load(path).map_err(|e| other(format!("loading {}: {e}", path.display())))?;
    let (mut inflows, total_adwf, mut issues) = build_inflows(&model);
    issues.extend(pump_station_issues(&model, &inflows, opts.flow_scale));
    for f in &mut inflows {
        f.base_lps *= opts.flow_scale;
    }
    if inflows.is_empty() {
        issues.push(Issue::new(
            Severity::Warning,
            "NO_INFLOWS",
            "model",
            "",
            None,
            "no inflows: the model has no node loads or sub-catchment loading",
        ));
    }

    let mut series_ids: Vec<String> = Vec::new();
    if write {
        series_ids = downstream_most(&model, 5);
        for s in &opts.series_ids {
            if !series_ids.contains(s) {
                series_ids.push(s.clone());
            }
        }
    }
    let sim_opts = to_sim_options(opts, &series_ids)?;
    let res = simulate(&SimInput {
        model: &model,
        inflows: inflows.clone(),
        options: sim_opts,
    })
    .map_err(|e| other(format!("simulation failed: {e}")))?;
    for w in &res.warnings {
        if w.starts_with("pump stations and rising mains") {
            continue; // replaced by the per-station issues above
        }
        issues.push(Issue::new(
            Severity::Warning,
            "ENGINE",
            "model",
            "",
            None,
            w.clone(),
        ));
    }
    let warnings: Vec<String> = issues
        .iter()
        .filter(|i| i.severity != Severity::Info)
        .map(|i| {
            if i.object_id.is_empty() {
                i.message.clone()
            } else {
                format!("{} {}: {}", i.object_type, i.object_id, i.message)
            }
        })
        .collect();

    let dn: HashMap<&str, i64> = model
        .conduits
        .iter()
        .map(|c| (c.conduit_id.as_str(), c.dn))
        .collect();
    let links: Vec<SimLinkOut> = res
        .links
        .iter()
        .map(|l| SimLinkOut {
            conduit_id: l.conduit_id.clone(),
            dn: dn.get(l.conduit_id.as_str()).copied().unwrap_or(0),
            max_flow_lps: l.max_flow_lps,
            max_depth_m: l.max_depth_m,
            max_d_over_d: l.max_d_over_d,
            max_velocity_mps: l.max_velocity_mps,
            time_of_max_flow: hhmm(l.max_flow_time_min),
            surcharged_min: l.surcharged_h * 60.0,
        })
        .collect();
    let nodes: Vec<SimNodeOut> = res
        .nodes
        .iter()
        .map(|n| SimNodeOut {
            node_id: n.node_id.clone(),
            max_hgl_m: n.max_hgl,
            max_depth_m: n.max_depth_m,
            flood_volume_m3: n.flood_volume_m3,
            surcharged_min: n.surcharged_h * 60.0,
        })
        .collect();
    let outfalls: HashSet<&str> = res
        .nodes
        .iter()
        .filter(|n| n.is_outfall)
        .map(|n| n.node_id.as_str())
        .collect();
    let ds: HashMap<&str, &str> = model
        .conduits
        .iter()
        .map(|c| (c.conduit_id.as_str(), c.ds_node_id.as_str()))
        .collect();
    let peak_outlet: f64 = res
        .links
        .iter()
        .filter(|l| {
            ds.get(l.conduit_id.as_str())
                .is_some_and(|d| outfalls.contains(d))
        })
        .map(|l| l.max_flow_lps)
        .sum();
    let summary = SimSummary {
        inflow_adwf_lps: total_adwf,
        inflow_simulated_lps: total_adwf * opts.flow_scale,
        peak_outlet_flow_lps: peak_outlet,
        continuity_error_pct: res.continuity_flow_error_pct,
        conduits: links.len(),
        nodes: nodes.len(),
        surcharged_conduits: res
            .links
            .iter()
            .filter(|l| l.max_d_over_d >= 1.0 || l.surcharged_h > 0.0)
            .count(),
        flooded_nodes: res.nodes.iter().filter(|n| n.flood_volume_m3 > 0.0).count(),
        total_flood_volume_m3: res.nodes.iter().map(|n| n.flood_volume_m3).sum(),
    };
    let mut out = SimulateOutput {
        run_id: None,
        label: opts.label.trim().to_string(),
        options: opts.clone(),
        summary,
        links,
        nodes,
        issues,
        warnings,
        series_ids: Vec::new(),
        inp_text: res.inp_text.clone(),
    };
    if write {
        out.run_id = Some(write_results(path, &out, &res, &series_ids)?);
        out.series_ids = series_ids;
        Model::store_issues(path, "simulate", &out.issues)?;
    }
    Ok(out)
}

fn write_results(
    path: &Path,
    out: &SimulateOutput,
    res: &SimResult,
    series_ids: &[String],
) -> Result<String> {
    gpkg::ensure_current(path)?;
    let mut c = gpkg::open(path)?;
    let tx = c.transaction()?;
    let base: String = tx.query_row("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')", [], |r| {
        r.get(0)
    })?;
    let mut run_id = base.clone();
    let mut k = 1;
    while tx.query_row(
        "SELECT count(*) FROM wwn_sim_runs WHERE run_id=?",
        [&run_id],
        |r| r.get::<_, i64>(0),
    )? > 0
    {
        k += 1;
        run_id = format!("{base}-{k}");
    }
    let options_json = serde_json::to_string(&out.options).map_err(|e| other(e.to_string()))?;
    let warnings_json = serde_json::to_string(&out.warnings).map_err(|e| other(e.to_string()))?;
    let summary_json = serde_json::to_string(&out.summary).map_err(|e| other(e.to_string()))?;
    tx.execute(
        "INSERT INTO wwn_sim_runs (run_id,options_json,continuity_error_pct,warnings_json,label,summary_json) VALUES (?,?,?,?,?,?)",
        params![run_id, options_json, out.summary.continuity_error_pct, warnings_json,
            out.label, summary_json],
    )?;
    for l in &out.links {
        tx.execute(
            "INSERT INTO wwn_sim_links (run_id,conduit_id,max_flow_lps,max_depth_m,max_d_over_d,max_velocity_mps,time_of_max_flow,surcharged_min) VALUES (?,?,?,?,?,?,?,?)",
            params![run_id, l.conduit_id, l.max_flow_lps, l.max_depth_m, l.max_d_over_d, l.max_velocity_mps, l.time_of_max_flow, l.surcharged_min],
        )?;
    }
    for n in &out.nodes {
        tx.execute(
            "INSERT INTO wwn_sim_nodes (run_id,node_id,max_hgl_m,max_depth_m,flood_volume_m3,surcharged_min) VALUES (?,?,?,?,?,?)",
            params![run_id, n.node_id, n.max_hgl_m, n.max_depth_m, n.flood_volume_m3, n.surcharged_min],
        )?;
    }
    if let Some(s) = &res.series {
        let warm_min = out.options.warmup_h * 60.0;
        let keep: Vec<usize> = s
            .times_min
            .iter()
            .enumerate()
            .filter(|(_, t)| **t >= warm_min - 1e-6)
            .map(|(i, _)| i)
            .collect();
        for id in series_ids {
            if let Some(l) = s.links.get(id) {
                for &i in &keep {
                    tx.execute(
                        "INSERT OR REPLACE INTO wwn_sim_series (run_id,object_type,object_id,minute,flow_lps,depth_m) VALUES (?,?,?,?,?,?)",
                        params![run_id, "conduit", id, s.times_min[i] - warm_min, l.flow_lps.get(i), l.depth_m.get(i)],
                    )?;
                }
            }
            if let Some(n) = s.nodes.get(id) {
                for &i in &keep {
                    tx.execute(
                        "INSERT OR REPLACE INTO wwn_sim_series (run_id,object_type,object_id,minute,flow_lps,depth_m) VALUES (?,?,?,?,?,?)",
                        params![run_id, "node", id, s.times_min[i] - warm_min, n.overflow_lps.get(i), n.depth_m.get(i)],
                    )?;
                }
            }
        }
    }
    // keep the most recent runs
    for t in ["wwn_sim_links", "wwn_sim_nodes", "wwn_sim_series"] {
        tx.execute(
            &format!("DELETE FROM {t} WHERE run_id NOT IN (SELECT run_id FROM wwn_sim_runs ORDER BY run_id DESC LIMIT {RUNS_KEPT})"),
            [],
        )?;
    }
    tx.execute(
        &format!("DELETE FROM wwn_sim_runs WHERE run_id NOT IN (SELECT run_id FROM wwn_sim_runs ORDER BY run_id DESC LIMIT {RUNS_KEPT})"),
        [],
    )?;
    write_display(&tx, &run_id)?;
    tx.commit()?;
    Ok(run_id)
}

/// Fill the map-layer tables `wwn_sim_conduit_results` / `wwn_sim_node_results` from stored run
/// `run_id` (geometry from the model). Returns (conduits, nodes) written.
fn write_display(c: &Connection, run_id: &str) -> Result<(usize, usize)> {
    let label: Option<String> = c
        .query_row(
            "SELECT label FROM wwn_sim_runs WHERE run_id=?",
            [run_id],
            |r| r.get(0),
        )
        .optional()?
        .flatten();
    c.execute("DELETE FROM wwn_sim_conduit_results", [])?;
    c.execute("DELETE FROM wwn_sim_node_results", [])?;
    let mut st = c.prepare(
        "SELECT conduit_id,max_flow_lps,max_depth_m,max_d_over_d,max_velocity_mps,time_of_max_flow,surcharged_min FROM wwn_sim_links WHERE run_id=?",
    )?;
    type LinkRow = (String, f64, f64, f64, f64, Option<String>, f64);
    let links: Vec<LinkRow> = st
        .query_map([run_id], |r| {
            Ok((
                r.get(0)?,
                r.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                r.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                r.get::<_, Option<f64>>(4)?.unwrap_or(0.0),
                r.get(5)?,
                r.get::<_, Option<f64>>(6)?.unwrap_or(0.0),
            ))
        })?
        .collect::<std::result::Result<_, _>>()?;
    for (id, q, d, dd, v, t, s) in &links {
        c.execute(
            "INSERT INTO wwn_sim_conduit_results (geom,run_id,label,conduit_id,dn,max_flow_lps,max_depth_m,max_d_over_d,max_velocity_mps,time_of_max_flow,surcharged_min,d_class)
             VALUES ((SELECT geom FROM wwn_conduits WHERE conduit_id=?3),?1,?2,?3,(SELECT dn FROM wwn_conduits WHERE conduit_id=?3),?4,?5,?6,?7,?8,?9,?10)",
            params![run_id, label, id, q, d, dd, v, t, s, d_class(*dd, *s)],
        )?;
    }
    let n = c.execute(
        "INSERT INTO wwn_sim_node_results (geom,run_id,label,node_id,max_hgl_m,max_depth_m,freeboard_m,flood_volume_m3,surcharged_min)
         SELECT n.geom, s.run_id, ?2, s.node_id, s.max_hgl_m, s.max_depth_m, n.surface_level - s.max_hgl_m,
                s.flood_volume_m3, s.surcharged_min
         FROM wwn_sim_nodes s LEFT JOIN wwn_nodes n ON n.node_id = s.node_id WHERE s.run_id=?1",
        params![run_id, label],
    )?;
    gpkg::copy_extent(c, "wwn_conduits", "wwn_sim_conduit_results")?;
    gpkg::copy_extent(c, "wwn_nodes", "wwn_sim_node_results")?;
    Ok((links.len(), n))
}

// ------------------------------------------------------------------ stored runs

/// One stored run (newest first in [`list_runs`]).
#[derive(Debug, Clone, Serialize)]
pub struct RunInfo {
    pub run_id: String,
    pub label: String,
    pub options: serde_json::Value,
    pub summary: serde_json::Value,
    pub continuity_error_pct: Option<f64>,
    pub warnings: Vec<String>,
    /// True for the run currently shown in `wwn_sim_conduit_results`.
    pub shown: bool,
}

impl RunInfo {
    /// "label (run_id)" or the run id alone.
    pub fn title(&self) -> String {
        if self.label.is_empty() {
            self.run_id.clone()
        } else {
            format!("{} ({})", self.label, self.run_id)
        }
    }
}

fn read_runs(c: &Connection) -> Result<Vec<RunInfo>> {
    let shown: Option<String> = c
        .query_row(
            "SELECT run_id FROM wwn_sim_conduit_results LIMIT 1",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let mut st = c.prepare(
        "SELECT run_id, label, options_json, summary_json, continuity_error_pct, warnings_json FROM wwn_sim_runs ORDER BY run_id DESC",
    )?;
    let rows = st.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?,
            r.get::<_, Option<String>>(2)?,
            r.get::<_, Option<String>>(3)?,
            r.get::<_, Option<f64>>(4)?,
            r.get::<_, Option<String>>(5)?,
        ))
    })?;
    let parse = |t: &Option<String>| -> serde_json::Value {
        t.as_deref()
            .and_then(|t| serde_json::from_str(t).ok())
            .unwrap_or(serde_json::Value::Null)
    };
    let mut out = Vec::new();
    for r in rows {
        let (run_id, label, opts, summ, cont, warns) = r?;
        out.push(RunInfo {
            shown: shown.as_deref() == Some(run_id.as_str()),
            run_id,
            label: label.unwrap_or_default(),
            options: parse(&opts),
            summary: parse(&summ),
            continuity_error_pct: cont,
            warnings: warns
                .as_deref()
                .and_then(|t| serde_json::from_str(t).ok())
                .unwrap_or_default(),
        });
    }
    Ok(out)
}

/// Stored simulation runs, newest first (upgrades older files first).
pub fn list_runs(path: impl AsRef<Path>) -> Result<Vec<RunInfo>> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    read_runs(&gpkg::open(path)?)
}

/// Resolve a run reference: an exact run id, an exact label, `"latest"` / empty (newest), or
/// `"previous"` (the one before the newest).
pub fn resolve_run(runs: &[RunInfo], key: &str) -> Result<RunInfo> {
    let k = key.trim();
    let pick = match k {
        "" | "latest" => runs.first(),
        "previous" => runs.get(1),
        _ => runs
            .iter()
            .find(|r| r.run_id == k)
            .or_else(|| runs.iter().find(|r| r.label == k))
            .or_else(|| runs.iter().find(|r| r.title() == k)),
    };
    pick.cloned().ok_or_else(|| {
        other(if runs.is_empty() {
            "no stored simulation runs: run a simulation first".to_string()
        } else {
            format!(
                "no simulation run '{k}'; stored: {}",
                runs.iter()
                    .map(|r| r.title())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
    })
}

/// Show stored run `run` (id, label, `latest` or `previous`) in the results map layers.
pub fn show_run(path: impl AsRef<Path>, run: &str) -> Result<RunInfo> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let mut c = gpkg::open(path)?;
    let mut info = resolve_run(&read_runs(&c)?, run)?;
    let tx = c.transaction()?;
    write_display(&tx, &info.run_id)?;
    tx.commit()?;
    info.shown = true;
    Ok(info)
}

/// Per-conduit comparison of two runs (b minus a).
#[derive(Debug, Clone, Serialize)]
pub struct CompareRow {
    pub conduit_id: String,
    pub max_flow_a_lps: Option<f64>,
    pub max_flow_b_lps: Option<f64>,
    pub d_flow_lps: Option<f64>,
    pub d_flow_pct: Option<f64>,
    pub max_d_over_d_a: Option<f64>,
    pub max_d_over_d_b: Option<f64>,
    pub d_d_over_d: Option<f64>,
    pub surcharged_a: bool,
    pub surcharged_b: bool,
    /// worse | better | same | new_surcharge | relieved | only_a | only_b
    pub change: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CompareOutput {
    pub run_a: RunInfo,
    pub run_b: RunInfo,
    pub rows: Vec<CompareRow>,
    /// Count of conduits per `change`.
    pub counts: BTreeMap<String, usize>,
}

/// Flow change below this fraction and d/D change below [`SAME_DD`] count as `same`.
pub const SAME_FLOW_PCT: f64 = 1.0;
pub const SAME_DD: f64 = 0.01;

/// Compare stored runs `a` (baseline) and `b`; with `write`, fills the `wwn_sim_compare` layer.
pub fn compare_runs(
    path: impl AsRef<Path>,
    a: &str,
    b: &str,
    write: bool,
) -> Result<CompareOutput> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let mut c = gpkg::open(path)?;
    let runs = read_runs(&c)?;
    let (ra, rb) = (resolve_run(&runs, a)?, resolve_run(&runs, b)?);
    if ra.run_id == rb.run_id {
        return Err(other(format!(
            "both runs are {}; choose two different runs",
            ra.title()
        )));
    }
    type Peak = (f64, f64, f64);
    let read = |id: &str| -> Result<BTreeMap<String, Peak>> {
        let mut st = c.prepare(
            "SELECT conduit_id, max_flow_lps, max_d_over_d, surcharged_min FROM wwn_sim_links WHERE run_id=?",
        )?;
        let v = st
            .query_map([id], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    (
                        r.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                        r.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                        r.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                    ),
                ))
            })?
            .collect::<std::result::Result<BTreeMap<_, _>, _>>()?;
        Ok(v)
    };
    let (pa, pb) = (read(&ra.run_id)?, read(&rb.run_id)?);
    let ids: std::collections::BTreeSet<&String> = pa.keys().chain(pb.keys()).collect();
    let mut rows = Vec::new();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for id in ids {
        let (x, y) = (pa.get(id), pb.get(id));
        let sa = x.is_some_and(|p| p.1 >= 1.0 || p.2 > 0.0);
        let sb = y.is_some_and(|p| p.1 >= 1.0 || p.2 > 0.0);
        let d_flow = x.zip(y).map(|(p, q)| q.0 - p.0);
        let d_pct = x
            .zip(y)
            .and_then(|(p, q)| (p.0.abs() > 1e-9).then(|| (q.0 - p.0) / p.0 * 100.0));
        let d_dd = x.zip(y).map(|(p, q)| q.1 - p.1);
        let change = match (x, y) {
            (Some(_), None) => "only_a",
            (None, Some(_)) => "only_b",
            _ if !sa && sb => "new_surcharge",
            _ if sa && !sb => "relieved",
            _ => {
                let dd = d_dd.unwrap_or(0.0);
                let pct = d_pct.unwrap_or_else(|| {
                    if d_flow.unwrap_or(0.0) > 1e-9 {
                        100.0
                    } else {
                        0.0
                    }
                });
                if dd > SAME_DD || pct > SAME_FLOW_PCT {
                    "worse"
                } else if dd < -SAME_DD || pct < -SAME_FLOW_PCT {
                    "better"
                } else {
                    "same"
                }
            }
        };
        *counts.entry(change.to_string()).or_default() += 1;
        rows.push(CompareRow {
            conduit_id: id.clone(),
            max_flow_a_lps: x.map(|p| p.0),
            max_flow_b_lps: y.map(|p| p.0),
            d_flow_lps: d_flow,
            d_flow_pct: d_pct,
            max_d_over_d_a: x.map(|p| p.1),
            max_d_over_d_b: y.map(|p| p.1),
            d_d_over_d: d_dd,
            surcharged_a: sa,
            surcharged_b: sb,
            change: change.to_string(),
        });
    }
    if write {
        let tx = c.transaction()?;
        tx.execute("DELETE FROM wwn_sim_compare", [])?;
        for r in &rows {
            tx.execute(
                "INSERT INTO wwn_sim_compare (geom,conduit_id,run_a,run_b,max_flow_a_lps,max_flow_b_lps,d_flow_lps,d_flow_pct,max_d_over_d_a,max_d_over_d_b,d_d_over_d,surcharged_a,surcharged_b,change)
                 VALUES ((SELECT geom FROM wwn_conduits WHERE conduit_id=?1),?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
                params![r.conduit_id, ra.title(), rb.title(), r.max_flow_a_lps, r.max_flow_b_lps,
                    r.d_flow_lps, r.d_flow_pct, r.max_d_over_d_a, r.max_d_over_d_b, r.d_d_over_d,
                    r.surcharged_a, r.surcharged_b, r.change],
            )?;
        }
        gpkg::copy_extent(&tx, "wwn_conduits", "wwn_sim_compare")?;
        tx.commit()?;
    }
    Ok(CompareOutput {
        run_a: ra,
        run_b: rb,
        rows,
        counts,
    })
}

/// A stored time series (minutes after the warm-up).
#[derive(Debug, Clone, Serialize)]
pub struct SeriesOut {
    pub object_type: String,
    pub object_id: String,
    pub minutes: Vec<f64>,
    pub flow_lps: Vec<Option<f64>>,
    pub depth_m: Vec<Option<f64>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SeriesResult {
    pub run: RunInfo,
    pub series: Vec<SeriesOut>,
    /// Requested ids with no stored series in that run.
    pub missing: Vec<String>,
    /// Every id with a stored series in that run.
    pub available: Vec<String>,
}

/// Stored series of `ids` (all stored when empty) in run `run` (id, label, `latest`, `previous`).
pub fn read_series(path: impl AsRef<Path>, run: &str, ids: &[String]) -> Result<SeriesResult> {
    let path = path.as_ref();
    gpkg::ensure_current(path)?;
    let c = gpkg::open(path)?;
    let info = resolve_run(&read_runs(&c)?, run)?;
    let mut st = c.prepare(
        "SELECT DISTINCT object_id FROM wwn_sim_series WHERE run_id=? ORDER BY object_id",
    )?;
    let available: Vec<String> = st
        .query_map([&info.run_id], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let want: Vec<String> = if ids.is_empty() {
        available.clone()
    } else {
        ids.to_vec()
    };
    let mut st = c.prepare(
        "SELECT object_type, minute, flow_lps, depth_m FROM wwn_sim_series WHERE run_id=? AND object_id=? ORDER BY object_type, minute",
    )?;
    let mut series = Vec::new();
    let mut missing = Vec::new();
    for id in &want {
        let rows: Vec<(String, f64, Option<f64>, Option<f64>)> = st
            .query_map(params![info.run_id, id], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })?
            .collect::<std::result::Result<_, _>>()?;
        if rows.is_empty() {
            missing.push(id.clone());
            continue;
        }
        let mut by_type: BTreeMap<String, SeriesOut> = BTreeMap::new();
        for (t, m, q, d) in rows {
            let e = by_type.entry(t.clone()).or_insert_with(|| SeriesOut {
                object_type: t,
                object_id: id.clone(),
                minutes: Vec::new(),
                flow_lps: Vec::new(),
                depth_m: Vec::new(),
            });
            e.minutes.push(m);
            e.flow_lps.push(q);
            e.depth_m.push(d);
        }
        series.extend(by_type.into_values());
    }
    Ok(SeriesResult {
        run: info,
        series,
        missing,
        available,
    })
}

/// Pretty JSON of a simulation result (includes `options`, `summary`, `links`, `nodes`, `issues`,
/// `warnings`).
pub fn simulate_json(out: &SimulateOutput) -> Result<String> {
    to_json(out)
}

/// Pretty JSON of any serialisable service result.
pub fn to_json<T: Serialize>(v: &T) -> Result<String> {
    serde_json::to_string_pretty(v).map_err(|e| other(e.to_string()))
}
