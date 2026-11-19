//! Structural and data-quality validation of a model.
use crate::model::{Model, Node};
use crate::network::{EdgeKind, Network};
use petgraph::algo::tarjan_scc;
use petgraph::Direction;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap, HashSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Info,
    Warning,
    Error,
}

impl Severity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Severity::Error => "error",
            Severity::Warning => "warning",
            Severity::Info => "info",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Issue {
    pub severity: Severity,
    pub code: String,
    pub object_type: String,
    pub object_id: String,
    pub clause: Option<String>,
    pub message: String,
}

const ENDPOINT_TOL_M: f64 = 0.5;
const INVERT_TOL_M: f64 = 0.01;

struct Sink(Vec<Issue>);

impl Sink {
    fn add(&mut self, severity: Severity, code: &str, ot: &str, id: &str, msg: String) {
        self.0.push(Issue {
            severity,
            code: code.into(),
            object_type: ot.into(),
            object_id: id.into(),
            clause: None,
            message: msg,
        });
    }
    fn clause(&mut self, clause: &str) {
        if let Some(l) = self.0.last_mut() {
            l.clause = Some(clause.into());
        }
    }
}

fn duplicates<'a>(ids: impl Iterator<Item = &'a str>, out: &mut Sink, code: &str, ot: &str) {
    let mut seen = HashSet::new();
    let mut reported = HashSet::new();
    for id in ids {
        if !seen.insert(id) && reported.insert(id) {
            out.add(
                Severity::Error,
                code,
                ot,
                id,
                format!("duplicate {ot} id '{id}'"),
            );
        }
    }
}

pub fn validate(model: &Model, net: &Network) -> Vec<Issue> {
    let mut o = Sink(Vec::new());

    duplicates(
        model.nodes.iter().map(|n| n.node_id.as_str()),
        &mut o,
        "DUP_NODE_ID",
        "node",
    );
    duplicates(
        model.conduits.iter().map(|n| n.conduit_id.as_str()),
        &mut o,
        "DUP_CONDUIT_ID",
        "conduit",
    );
    duplicates(
        model.pump_stations.iter().map(|n| n.ps_id.as_str()),
        &mut o,
        "DUP_PS_ID",
        "pump_station",
    );
    duplicates(
        model
            .subcatchments
            .iter()
            .map(|n| n.subcatchment_id.as_str()),
        &mut o,
        "DUP_SUBCATCHMENT_ID",
        "subcatchment",
    );

    let mut nodes: HashMap<&str, &Node> = HashMap::new();
    for n in &model.nodes {
        nodes.entry(n.node_id.as_str()).or_insert(n);
    }

    // Conduit checks
    let mut missing_by_src: BTreeMap<&'static str, usize> = BTreeMap::new();
    for c in &model.conduits {
        let id = c.conduit_id.as_str();
        let us = nodes.get(c.us_node_id.as_str());
        let ds = nodes.get(c.ds_node_id.as_str());
        if us.is_none() {
            o.add(
                Severity::Error,
                "CONDUIT_NODE_MISSING",
                "conduit",
                id,
                format!("upstream node '{}' not found", c.us_node_id),
            );
        }
        if ds.is_none() {
            o.add(
                Severity::Error,
                "CONDUIT_NODE_MISSING",
                "conduit",
                id,
                format!("downstream node '{}' not found", c.ds_node_id),
            );
        }

        match c.effective_length() {
            Some(l) if l > 0.0 => {}
            Some(l) => o.add(
                Severity::Error,
                "LENGTH_INVALID",
                "conduit",
                id,
                format!("length {l:.2} m is zero or negative"),
            ),
            None => o.add(
                Severity::Error,
                "LENGTH_INVALID",
                "conduit",
                id,
                "no length and no geometry".into(),
            ),
        }

        match (c.us_invert, c.ds_invert) {
            (Some(u), Some(d)) => {
                if d > u {
                    o.add(
                        Severity::Error,
                        "ADVERSE_GRADE",
                        "conduit",
                        id,
                        format!("adverse grade: ds invert {d:.3} is above us invert {u:.3}"),
                    );
                } else if d == u {
                    o.add(
                        Severity::Warning,
                        "FLAT_GRADE",
                        "conduit",
                        id,
                        format!("zero grade (inverts both {u:.3})"),
                    );
                }
            }
            (u, d) => {
                *missing_by_src.entry(c.data_source.as_str()).or_default() += 1;
                let what = match (u, d) {
                    (None, None) => "us and ds inverts",
                    (None, _) => "us invert",
                    _ => "ds invert",
                };
                o.add(
                    Severity::Warning,
                    "MISSING_INVERT",
                    "conduit",
                    id,
                    format!("missing {what} (data source: {})", c.data_source),
                );
            }
        }

        for (inv, node, end) in [(c.us_invert, us, "us"), (c.ds_invert, ds, "ds")] {
            let (Some(inv), Some(n)) = (inv, node) else {
                continue;
            };
            if let Some(sl) = n.surface_level {
                if inv > sl {
                    o.add(
                        Severity::Error,
                        "INVERT_ABOVE_SURFACE",
                        "conduit",
                        id,
                        format!(
                            "{end} invert {inv:.3} is above surface level {sl:.3} of node '{}'",
                            n.node_id
                        ),
                    );
                }
            }
            if let Some(ni) = n.invert_level {
                let diff = inv - ni;
                if diff < -INVERT_TOL_M {
                    o.add(
                        Severity::Warning,
                        "INVERT_BELOW_NODE",
                        "conduit",
                        id,
                        format!(
                            "{end} invert {inv:.3} is {:.3} m below invert {ni:.3} of node '{}'",
                            -diff, n.node_id
                        ),
                    );
                } else if diff > INVERT_TOL_M {
                    o.add(Severity::Info, "INVERT_MISMATCH", "conduit", id,
                        format!("{end} invert {inv:.3} is {diff:.3} m above invert {ni:.3} of node '{}'", n.node_id));
                }
            }
        }

        if let (Some(g), Some(un), Some(dn)) = (&c.geom, us, ds) {
            if let (Some(f), Some(p)) = (g.first(), un.geom.as_ref()) {
                let d = f.distance(p);
                if d > ENDPOINT_TOL_M {
                    o.add(
                        Severity::Warning,
                        "GEOM_ENDPOINT_MISMATCH",
                        "conduit",
                        id,
                        format!(
                            "start of geometry is {d:.2} m from us node '{}'",
                            un.node_id
                        ),
                    );
                }
            }
            if let (Some(l), Some(p)) = (g.last(), dn.geom.as_ref()) {
                let d = l.distance(p);
                if d > ENDPOINT_TOL_M {
                    o.add(
                        Severity::Warning,
                        "GEOM_ENDPOINT_MISMATCH",
                        "conduit",
                        id,
                        format!("end of geometry is {d:.2} m from ds node '{}'", dn.node_id),
                    );
                }
            }
        }
    }
    if !missing_by_src.is_empty() {
        let total: usize = missing_by_src.values().sum();
        let parts: Vec<String> = missing_by_src
            .iter()
            .map(|(k, v)| format!("{k}: {v}"))
            .collect();
        o.add(
            Severity::Info,
            "MISSING_INVERT_SUMMARY",
            "model",
            "-",
            format!(
                "{total} conduit(s) missing inverts by data source ({})",
                parts.join(", ")
            ),
        );
    }

    // Graph checks
    for i in net.graph.node_indices() {
        let info = &net.graph[i];
        let deg = net.graph.edges_directed(i, Direction::Incoming).count()
            + net.graph.edges_directed(i, Direction::Outgoing).count();
        if deg == 0 && info.node_type != crate::model::NodeType::Outfall {
            o.add(
                Severity::Warning,
                "ORPHAN_NODE",
                "node",
                &info.id,
                "node has no connected links".into(),
            );
        }
        let outs: Vec<_> = net
            .graph
            .edges_directed(i, Direction::Outgoing)
            .filter(|e| e.weight().kind == EdgeKind::Gravity)
            .collect();
        if outs.len() > 1 {
            let ids: Vec<&str> = outs.iter().map(|e| e.weight().id.as_str()).collect();
            o.add(
                Severity::Warning,
                "BIFURCATION",
                "node",
                &info.id,
                format!(
                    "{} gravity outlets ({}): flow split is undefined",
                    outs.len(),
                    ids.join(", ")
                ),
            );
        }
    }
    for scc in tarjan_scc(&net.graph) {
        let cyclic = scc.len() > 1 || net.graph.edges_connecting(scc[0], scc[0]).next().is_some();
        if cyclic {
            let mut ids: Vec<&str> = scc.iter().map(|&i| net.graph[i].id.as_str()).collect();
            ids.sort();
            o.add(
                Severity::Error,
                "NETWORK_CYCLE",
                "node",
                ids[0],
                format!("cycle through nodes: {}", ids.join(", ")),
            );
        }
    }

    // DN reduction downstream
    for c in &model.conduits {
        for d in &model.conduits {
            if c.ds_node_id == d.us_node_id && d.dn < c.dn && c.conduit_id != d.conduit_id {
                o.add(
                    Severity::Warning,
                    "DN_REDUCTION",
                    "conduit",
                    &d.conduit_id,
                    format!(
                        "DN{} downstream of DN{} (conduit '{}')",
                        d.dn, c.dn, c.conduit_id
                    ),
                );
                o.clause("DS50 4.2.1(v)");
            }
        }
    }

    for s in &model.subcatchments {
        if !nodes.contains_key(s.outlet_node_id.as_str()) {
            o.add(
                Severity::Error,
                "SUBCATCHMENT_OUTLET_MISSING",
                "subcatchment",
                &s.subcatchment_id,
                format!("outlet node '{}' not found", s.outlet_node_id),
            );
        }
    }

    for p in &model.pump_stations {
        for (what, v) in [
            ("wet well", &p.wet_well_node_id),
            ("discharge", &p.discharge_node_id),
        ] {
            match v {
                None => o.add(
                    Severity::Error,
                    "PS_NODE_MISSING",
                    "pump_station",
                    &p.ps_id,
                    format!("no {what} node specified"),
                ),
                Some(n) if !nodes.contains_key(n.as_str()) => o.add(
                    Severity::Error,
                    "PS_NODE_MISSING",
                    "pump_station",
                    &p.ps_id,
                    format!("{what} node '{n}' not found"),
                ),
                _ => {}
            }
        }
    }
    o.0
}
