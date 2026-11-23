//! Dry-weather loads shared by the steady design (GSDF) and the dynamic simulation (ADWF with a
//! diurnal profile), so both validate node loads and sub-catchment loading the same way.
use crate::subcatchment_adwf_lps;
use std::collections::HashSet;
use wwnet_core::{Issue, Model, NodeLoad, Severity, Subcatchment};
use wwnet_rules::RuleSet;

/// A valid node load with its ADWF (L/s): `adwf_lps`, else connections x per-connection allowance.
#[derive(Debug, Clone)]
pub struct ResolvedNodeLoad<'a> {
    pub load: &'a NodeLoad,
    pub adwf_lps: f64,
}

/// A sub-catchment whose ADWF could be determined and whose outlet node exists.
#[derive(Debug, Clone)]
pub struct ResolvedSubcatchmentLoad<'a> {
    pub subcatchment: &'a Subcatchment,
    pub adwf_lps: f64,
}

/// Valid node loads (zero ADWF included) and the issues of the rejected ones
/// (`LOAD_NO_DATA`, `LOAD_INVALID`, `LOAD_NODE_MISSING`).
pub fn resolve_node_loads(model: &Model) -> (Vec<ResolvedNodeLoad<'_>>, Vec<Issue>) {
    let per_conn = model.load_settings.adwf_per_connection_lpd;
    let node_ids: HashSet<&str> = model.nodes.iter().map(|n| n.node_id.as_str()).collect();
    let mut out = Vec::new();
    let mut issues = Vec::new();
    for l in &model.node_loads {
        let adwf = l.effective_adwf_lps(per_conn);
        let problem = if l.adwf_lps.is_none() && l.connections.is_none() {
            Some((
                "LOAD_NO_DATA",
                format!(
                    "node load {} has neither connections nor ADWF; ignored",
                    l.load_id
                ),
            ))
        } else if !adwf.is_finite() || adwf < 0.0 {
            Some((
                "LOAD_INVALID",
                format!("node load {} has invalid ADWF {adwf}; ignored", l.load_id),
            ))
        } else if !node_ids.contains(l.node_id.as_str()) {
            Some((
                "LOAD_NODE_MISSING",
                format!(
                    "node load {} refers to node {} that does not exist; its flow is lost",
                    l.load_id, l.node_id
                ),
            ))
        } else {
            None
        };
        match problem {
            Some((code, msg)) => issues.push(Issue::new(
                Severity::Warning,
                code,
                "node",
                &l.node_id,
                None,
                msg,
            )),
            None => out.push(ResolvedNodeLoad {
                load: l,
                adwf_lps: adwf,
            }),
        }
    }
    (out, issues)
}

/// Sub-catchment ADWF (manual `adwf_lps`, else DS 50 daily flow / 86400) for the simulation.
/// Loading warnings are returned (info notes are dropped); a missing outlet node gives
/// `SUBCATCHMENT_OUTLET_MISSING` and an undeterminable ADWF `SUBCATCHMENT_NO_ADWF`.
pub fn resolve_subcatchment_loads<'a>(
    model: &'a Model,
    rules: &RuleSet,
) -> (Vec<ResolvedSubcatchmentLoad<'a>>, Vec<Issue>) {
    let node_ids: HashSet<&str> = model.nodes.iter().map(|n| n.node_id.as_str()).collect();
    let mut out = Vec::new();
    let mut issues = Vec::new();
    for s in &model.subcatchments {
        let (adwf, found) = subcatchment_adwf_lps(model, s, rules);
        issues.extend(found.into_iter().filter(|i| i.severity != Severity::Info));
        match adwf {
            Some(a) if a.is_finite() && a > 0.0 => {
                if node_ids.contains(s.outlet_node_id.as_str()) {
                    out.push(ResolvedSubcatchmentLoad {
                        subcatchment: s,
                        adwf_lps: a,
                    });
                } else {
                    issues.push(Issue::new(
                        Severity::Warning,
                        "SUBCATCHMENT_OUTLET_MISSING",
                        "subcatchment",
                        &s.subcatchment_id,
                        None,
                        format!(
                            "outlet {} is not a node; its flow is lost",
                            s.outlet_node_id
                        ),
                    ));
                }
            }
            Some(_) => {}
            None => issues.push(Issue::new(
                Severity::Warning,
                "SUBCATCHMENT_NO_ADWF",
                "subcatchment",
                &s.subcatchment_id,
                None,
                "no ADWF could be determined; ignored",
            )),
        }
    }
    (out, issues)
}
