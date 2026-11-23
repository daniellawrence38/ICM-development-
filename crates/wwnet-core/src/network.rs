//! Directed graph view of the model.
use crate::error::{Error, Result};
use crate::model::{Model, NodeType};
use petgraph::algo::toposort;
use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::visit::EdgeRef;
use petgraph::Direction;
use std::collections::{HashMap, HashSet, VecDeque};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeKind {
    Gravity,
    /// Pump station wet well -> discharge node (not a conduit).
    Pumped,
}

#[derive(Debug, Clone)]
pub struct NodeInfo {
    pub id: String,
    pub node_type: NodeType,
}

#[derive(Debug, Clone)]
pub struct EdgeInfo {
    pub id: String,
    pub kind: EdgeKind,
}

#[derive(Debug, Clone)]
pub struct Network {
    pub graph: DiGraph<NodeInfo, EdgeInfo>,
    pub index: HashMap<String, NodeIndex>,
}

impl Network {
    /// Build the graph. Duplicate node ids keep the first; links to unknown nodes are
    /// skipped (reported by `validate`).
    pub fn build(model: &Model) -> Result<Network> {
        let mut graph = DiGraph::new();
        let mut index = HashMap::new();
        for n in &model.nodes {
            index.entry(n.node_id.clone()).or_insert_with(|| {
                graph.add_node(NodeInfo {
                    id: n.node_id.clone(),
                    node_type: n.node_type,
                })
            });
        }
        for c in &model.conduits {
            if let (Some(&u), Some(&d)) = (index.get(&c.us_node_id), index.get(&c.ds_node_id)) {
                graph.add_edge(
                    u,
                    d,
                    EdgeInfo {
                        id: c.conduit_id.clone(),
                        kind: EdgeKind::Gravity,
                    },
                );
            }
        }
        for p in &model.pump_stations {
            if let (Some(w), Some(d)) = (&p.wet_well_node_id, &p.discharge_node_id) {
                if let (Some(&u), Some(&d)) = (index.get(w), index.get(d)) {
                    graph.add_edge(
                        u,
                        d,
                        EdgeInfo {
                            id: p.ps_id.clone(),
                            kind: EdgeKind::Pumped,
                        },
                    );
                }
            }
        }
        Ok(Network { graph, index })
    }

    fn idx(&self, id: &str) -> Result<NodeIndex> {
        self.index
            .get(id)
            .copied()
            .ok_or_else(|| Error::NodeNotFound(id.to_string()))
    }

    fn trace(&self, id: &str, dir: Direction) -> Result<Vec<&str>> {
        Ok(self.trace_links(id, dir == Direction::Incoming)?.0)
    }

    /// Nodes (breadth-first, excluding `id`) and the gravity conduits traversed, upstream or
    /// downstream of `id`; pumped links are followed but are not conduits.
    pub fn trace_links(&self, id: &str, upstream: bool) -> Result<(Vec<&str>, Vec<&str>)> {
        let dir = if upstream {
            Direction::Incoming
        } else {
            Direction::Outgoing
        };
        let start = self.idx(id)?;
        let mut seen = HashSet::from([start]);
        let mut q = VecDeque::from([start]);
        let mut out = Vec::new();
        let mut links = Vec::new();
        while let Some(n) = q.pop_front() {
            for e in self.graph.edges_directed(n, dir) {
                if e.weight().kind == EdgeKind::Gravity {
                    links.push(e.weight().id.as_str());
                }
                let other = if upstream { e.source() } else { e.target() };
                if seen.insert(other) {
                    out.push(self.graph[other].id.as_str());
                    q.push_back(other);
                }
            }
        }
        Ok((out, links))
    }

    /// All nodes upstream of `id` (breadth-first, excluding `id`), following pumped links.
    pub fn upstream_trace(&self, id: &str) -> Result<Vec<&str>> {
        self.trace(id, Direction::Incoming)
    }

    /// All nodes downstream of `id` (breadth-first, excluding `id`).
    pub fn downstream_trace(&self, id: &str) -> Result<Vec<&str>> {
        self.trace(id, Direction::Outgoing)
    }

    /// Node ids upstream to downstream; `Err(Cycle)` if the graph has a cycle.
    pub fn topological_order(&self) -> Result<Vec<&str>> {
        match toposort(&self.graph, None) {
            Ok(v) => Ok(v.into_iter().map(|i| self.graph[i].id.as_str()).collect()),
            Err(c) => Err(Error::Cycle(self.graph[c.node_id()].id.clone())),
        }
    }

    /// Outfall-typed nodes.
    pub fn outfalls(&self) -> Vec<&str> {
        self.graph
            .node_indices()
            .filter(|&i| self.graph[i].node_type == NodeType::Outfall)
            .map(|i| self.graph[i].id.as_str())
            .collect()
    }

    /// Nodes with no outgoing link that have at least one incoming link.
    pub fn sinks(&self) -> Vec<&str> {
        self.graph
            .node_indices()
            .filter(|&i| {
                self.graph
                    .edges_directed(i, Direction::Outgoing)
                    .next()
                    .is_none()
                    && self
                        .graph
                        .edges_directed(i, Direction::Incoming)
                        .next()
                        .is_some()
            })
            .map(|i| self.graph[i].id.as_str())
            .collect()
    }

    /// Outfalls and sinks (deduplicated, graph order).
    pub fn terminal_nodes(&self) -> Vec<&str> {
        let sinks: HashSet<&str> = self.sinks().into_iter().collect();
        self.graph
            .node_indices()
            .filter(|&i| {
                self.graph[i].node_type == NodeType::Outfall
                    || sinks.contains(self.graph[i].id.as_str())
            })
            .map(|i| self.graph[i].id.as_str())
            .collect()
    }
}
