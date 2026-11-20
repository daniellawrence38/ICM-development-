//! File-level simulation service shared by the `wwnet simulate` CLI and the `wwnet_native`
//! Python module: loads a model GeoPackage, builds the dry-weather inflows, runs the SWMM
//! engine, optionally stores the results in the `wwn_sim_*` tables and returns a JSON-able
//! [`SimulateOutput`].
use crate::inp::profile_factor;
use crate::{simulate, NodeInflow, Roughness, SimInput, SimOptions, SimResult};
use rusqlite::params;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use wwnet_core::{gpkg, Error, Model, Result};
use wwnet_design::subcatchment_adwf_lps;
use wwnet_rules::RuleSet;

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
}

impl Default for ServiceOptions {
    fn default() -> Self {
        ServiceOptions {
            duration_h: 48.0,
            warmup_h: 24.0,
            report_step_s: 300,
            routing_step_s: 5.0,
            roughness: "manning".into(),
            manning_n: 0.013,
            flow_scale: 1.0,
            series_ids: Vec::new(),
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
    pub options: ServiceOptions,
    pub summary: SimSummary,
    pub links: Vec<SimLinkOut>,
    pub nodes: Vec<SimNodeOut>,
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

/// Collect the dry-weather inflows per node (node loads and sub-catchments), each with its
/// profile. Returns the inflows (at flow_scale 1), the total ADWF and warnings.
pub fn build_inflows(model: &Model) -> (Vec<NodeInflow>, f64, Vec<String>) {
    let mut warnings = Vec::new();
    let rules = RuleSet::ds50();
    let ls = &model.load_settings;
    let node_ids: HashSet<&str> = model.nodes.iter().map(|n| n.node_id.as_str()).collect();
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
            warnings.push(format!("default profile '{d}' does not exist"));
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

    for l in &model.node_loads {
        let adwf = l.effective_adwf_lps(ls.adwf_per_connection_lpd);
        if l.adwf_lps.is_none() && l.connections.is_none() {
            warnings.push(format!(
                "node load {} has no connections or ADWF; ignored",
                l.load_id
            ));
        } else if !adwf.is_finite() || adwf < 0.0 {
            warnings.push(format!("node load {} has invalid ADWF; ignored", l.load_id));
        } else if !node_ids.contains(l.node_id.as_str()) {
            warnings.push(format!(
                "node load {} refers to unknown node {}; ignored",
                l.load_id, l.node_id
            ));
        } else if adwf > 0.0 {
            add(&l.node_id, adwf, l.profile_id.as_ref());
        }
    }
    for s in &model.subcatchments {
        let (adwf, issues) = subcatchment_adwf_lps(model, s, &rules);
        for i in issues
            .iter()
            .filter(|i| i.severity != wwnet_core::Severity::Info)
        {
            warnings.push(format!(
                "sub-catchment {}: {}",
                s.subcatchment_id, i.message
            ));
        }
        match adwf {
            Some(a) if a.is_finite() && a > 0.0 => {
                if node_ids.contains(s.outlet_node_id.as_str()) {
                    add(&s.outlet_node_id, a, s.profile_id.as_ref());
                } else {
                    warnings.push(format!(
                        "sub-catchment {} outlet {} is not a node; its flow is ignored",
                        s.subcatchment_id, s.outlet_node_id
                    ));
                }
            }
            Some(_) => {}
            None => warnings.push(format!(
                "sub-catchment {}: no ADWF could be determined; ignored",
                s.subcatchment_id
            )),
        }
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
        warnings.push(format!("profile '{id}' not found: constant ADWF used"));
    }
    if constant > 0 {
        warnings.push("no diurnal profile: constant ADWF".into());
    }
    (inflows, total, warnings)
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
    let (mut inflows, total_adwf, mut warnings) = build_inflows(&model);
    for f in &mut inflows {
        f.base_lps *= opts.flow_scale;
    }
    if inflows.is_empty() {
        warnings.push("no inflows: the model has no node loads or sub-catchment loading".into());
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
    warnings.extend(res.warnings.iter().cloned());

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
        options: opts.clone(),
        summary,
        links,
        nodes,
        warnings,
        series_ids: Vec::new(),
        inp_text: res.inp_text.clone(),
    };
    if write {
        out.run_id = Some(write_results(path, &out, &res, &series_ids)?);
        out.series_ids = series_ids;
    }
    Ok(out)
}

fn write_results(
    path: &Path,
    out: &SimulateOutput,
    res: &SimResult,
    series_ids: &[String],
) -> Result<String> {
    gpkg::upgrade(path)?;
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
    tx.execute(
        "INSERT INTO wwn_sim_runs (run_id,options_json,continuity_error_pct,warnings_json) VALUES (?,?,?,?)",
        params![run_id, options_json, out.summary.continuity_error_pct, warnings_json],
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
    // keep the 10 most recent runs
    for t in ["wwn_sim_links", "wwn_sim_nodes", "wwn_sim_series"] {
        tx.execute(
            &format!("DELETE FROM {t} WHERE run_id NOT IN (SELECT run_id FROM wwn_sim_runs ORDER BY run_id DESC LIMIT 10)"),
            [],
        )?;
    }
    tx.execute(
        "DELETE FROM wwn_sim_runs WHERE run_id NOT IN (SELECT run_id FROM wwn_sim_runs ORDER BY run_id DESC LIMIT 10)",
        [],
    )?;
    tx.commit()?;
    Ok(run_id)
}

/// Pretty JSON of a simulation result (includes `options`, `summary`, `links`, `nodes`, `warnings`).
pub fn simulate_json(out: &SimulateOutput) -> Result<String> {
    serde_json::to_string_pretty(out).map_err(|e| other(e.to_string()))
}
