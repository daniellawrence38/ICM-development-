//! SWMM `.inp` writer. Output ordering follows model order, so it is stable.
#![allow(clippy::neg_cmp_op_on_partial_ord)] // deliberate: NaN must fail positivity checks
use crate::types::*;
use std::collections::{HashMap, HashSet};
use std::fmt::Write;
use wwnet_core::Node;

#[derive(Debug, Clone)]
pub struct NodeMeta {
    pub orig: String,
    pub swmm: String,
    pub invert: f64,
    /// Highest connected pipe crown level.
    pub soffit: f64,
    pub is_outfall: bool,
}

#[derive(Debug, Clone)]
pub struct LinkMeta {
    pub orig: String,
    pub swmm: String,
    pub diameter_m: f64,
}

#[derive(Debug, Clone)]
pub struct BuiltInp {
    pub text: String,
    pub nodes: Vec<NodeMeta>,
    pub links: Vec<LinkMeta>,
    pub warnings: Vec<String>,
    pub id_map: Vec<IdMapEntry>,
}

/// Make ids SWMM-safe (no whitespace/semicolons/quotes) and unique within `used`.
fn safe_id(orig: &str, used: &mut HashSet<String>) -> String {
    let mut s: String = orig
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.') {
                c
            } else {
                '_'
            }
        })
        .collect();
    if s.is_empty() {
        s.push('_');
    }
    let base = s.clone();
    let mut k = 1;
    while !used.insert(s.clone()) {
        k += 1;
        s = format!("{base}_{k}");
    }
    s
}

/// Linear, cyclic (24 h) interpolation of the profile factor at `minute` of the day.
pub(crate) fn profile_factor(pts: &[(u32, f64)], minute: f64) -> f64 {
    match pts.len() {
        0 => 1.0,
        1 => pts[0].1,
        n => {
            let day = 1440.0;
            // find the last point at or before `minute` (wrapping to the previous day)
            let mut prev = (pts[n - 1].0 as f64 - day, pts[n - 1].1);
            let mut next = (pts[0].0 as f64 + day, pts[0].1);
            for (i, &(t, f)) in pts.iter().enumerate() {
                let t = t as f64;
                if t <= minute {
                    prev = (t, f);
                    next = if i + 1 < n {
                        (pts[i + 1].0 as f64, pts[i + 1].1)
                    } else {
                        (pts[0].0 as f64 + day, pts[0].1)
                    };
                } else {
                    break;
                }
            }
            let span = next.0 - prev.0;
            if span <= 0.0 {
                prev.1
            } else {
                prev.1 + (next.1 - prev.1) * (minute - prev.0) / span
            }
        }
    }
}

fn hms(total_s: f64) -> String {
    let t = total_s.round().max(0.0) as u64;
    format!("{:02}:{:02}:{:02}", t / 3600, (t / 60) % 60, t % 60)
}

fn date_after(days: u64) -> String {
    let (mut y, mut m, mut d) = (2001u32, 1u32, 1u64 + days);
    loop {
        let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
        let dim = match m {
            2 => {
                if leap {
                    29
                } else {
                    28
                }
            }
            4 | 6 | 9 | 11 => 30,
            _ => 31,
        };
        if d <= dim {
            return format!("{m:02}/{d:02}/{y}");
        }
        d -= dim;
        m += 1;
        if m > 12 {
            m = 1;
            y += 1;
        }
    }
}

fn node_invert_hint(n: &Node) -> Option<f64> {
    n.invert_level
}

/// Equivalent Manning n from full-bore Colebrook-White velocity.
fn n_from_ks(d_m: f64, slope: f64, ks_mm: f64) -> f64 {
    let s = slope.max(0.002);
    let r = d_m / 4.0;
    let v =
        wwnet_hydraulics::colebrook_velocity(r, s, ks_mm / 1000.0, wwnet_hydraulics::NU_WASTEWATER);
    if v <= 0.0 {
        0.013
    } else {
        (r.powf(2.0 / 3.0) * s.sqrt() / v).clamp(0.006, 0.05)
    }
}

pub fn build_inp(input: &SimInput<'_>) -> Result<BuiltInp> {
    let o = &input.options;
    if !(o.duration_h > 0.0) || o.warmup_h < 0.0 || o.warmup_h >= o.duration_h {
        return Err(SimError::BadOptions(
            "need duration_h > 0 and 0 <= warmup_h < duration_h".into(),
        ));
    }
    if o.report_step_s == 0 || !(o.routing_step_s > 0.0) {
        return Err(SimError::BadOptions("steps must be positive".into()));
    }
    let model = input.model;
    let mut warnings = Vec::new();
    if !model.pump_stations.is_empty() || !model.rising_mains.is_empty() {
        warnings.push("pump stations and rising mains are not modelled".to_string());
    }

    let node_ix: HashMap<&str, usize> = {
        let mut m = HashMap::new();
        for (i, n) in model.nodes.iter().enumerate() {
            m.entry(n.node_id.as_str()).or_insert(i);
        }
        m
    };
    for c in &model.conduits {
        for id in [&c.us_node_id, &c.ds_node_id] {
            if !node_ix.contains_key(id.as_str()) {
                return Err(SimError::UnknownConduitNode {
                    conduit: c.conduit_id.clone(),
                    node: id.clone(),
                });
            }
        }
    }

    // Per node: lowest connected invert, highest crown, has outgoing conduit, connected.
    let nn = model.nodes.len();
    let mut min_inv = vec![f64::INFINITY; nn];
    let mut max_crown = vec![f64::NEG_INFINITY; nn];
    let mut has_out = vec![false; nn];
    let mut connected = vec![false; nn];
    // conduit inverts (us, ds) resolved after node inverts are known; first pass uses
    // stated conduit inverts only.
    for c in &model.conduits {
        let (u, d) = (
            node_ix[c.us_node_id.as_str()],
            node_ix[c.ds_node_id.as_str()],
        );
        has_out[u] = true;
        connected[u] = true;
        connected[d] = true;
        let dia = c.effective_id_mm() / 1000.0;
        if let Some(v) = c.us_invert {
            min_inv[u] = min_inv[u].min(v);
            max_crown[u] = max_crown[u].max(v + dia);
        }
        if let Some(v) = c.ds_invert {
            min_inv[d] = min_inv[d].min(v);
            max_crown[d] = max_crown[d].max(v + dia);
        }
    }

    let mut used_nodes = HashSet::new();
    let mut nodes: Vec<NodeMeta> = Vec::new();
    let mut node_swmm_ix: HashMap<usize, usize> = HashMap::new();
    let mut max_depths = Vec::new();
    for (i, n) in model.nodes.iter().enumerate() {
        if !connected[i] || node_swmm_ix.contains_key(&i) {
            continue;
        }
        // first occurrence of a duplicated id wins
        if node_ix[n.node_id.as_str()] != i {
            continue;
        }
        let mut invert = match (node_invert_hint(n), min_inv[i].is_finite()) {
            (Some(v), true) => {
                if min_inv[i] < v {
                    warnings.push(format!(
                        "node {}: invert {:.3} above connected pipe invert {:.3}; lowered",
                        n.node_id, v, min_inv[i]
                    ));
                    min_inv[i]
                } else {
                    v
                }
            }
            (Some(v), false) => v,
            (None, true) => min_inv[i],
            (None, false) => {
                warnings.push(format!("node {}: no invert level, assumed 0", n.node_id));
                0.0
            }
        };
        if !invert.is_finite() {
            invert = 0.0;
        }
        let crown = if max_crown[i].is_finite() {
            max_crown[i]
        } else {
            invert
        };
        let mut depth = match n.surface_level {
            Some(s) if s > invert => s - invert,
            Some(_) => {
                warnings.push(format!(
                    "node {}: surface level not above invert; using default depth",
                    n.node_id
                ));
                o.default_max_depth_m
            }
            None => o.default_max_depth_m,
        };
        if depth < crown - invert + 0.01 {
            warnings.push(format!(
                "node {}: max depth {:.3} below connected crown; raised",
                n.node_id, depth
            ));
            depth = crown - invert + 0.01;
        }
        node_swmm_ix.insert(i, nodes.len());
        nodes.push(NodeMeta {
            orig: n.node_id.clone(),
            swmm: safe_id(&n.node_id, &mut used_nodes),
            invert,
            soffit: crown,
            is_outfall: !has_out[i],
        });
        max_depths.push(depth);
    }

    let mut out = String::new();
    let end_s = o.duration_h * 3600.0;
    let end_days = (end_s / 86400.0).floor() as u64;
    let end_rem = end_s - end_days as f64 * 86400.0;
    out.push_str("[TITLE]\n;;WWNet dynamic hydraulic simulation (EPA SWMM 5.2.4)\n\n");
    let _ = write!(
        out,
        "[OPTIONS]\n\
FLOW_UNITS           LPS\n\
INFILTRATION         HORTON\n\
FLOW_ROUTING         DYNWAVE\n\
LINK_OFFSETS         ELEVATION\n\
FORCE_MAIN_EQUATION  D-W\n\
IGNORE_RAINFALL      YES\n\
IGNORE_SNOWMELT      YES\n\
IGNORE_GROUNDWATER   YES\n\
IGNORE_RDII          YES\n\
IGNORE_ROUTING       NO\n\
IGNORE_QUALITY       YES\n\
ALLOW_PONDING        NO\n\
SKIP_STEADY_STATE    NO\n\
START_DATE           01/01/2001\n\
START_TIME           00:00:00\n\
REPORT_START_DATE    01/01/2001\n\
REPORT_START_TIME    00:00:00\n\
END_DATE             {end_date}\n\
END_TIME             {end_time}\n\
SWEEP_START          01/01\n\
SWEEP_END            12/31\n\
DRY_DAYS             0\n\
REPORT_STEP          {report}\n\
WET_STEP             {report}\n\
DRY_STEP             {report}\n\
ROUTING_STEP         {rstep}\n\
LENGTHENING_STEP     0\n\
VARIABLE_STEP        0.75\n\
INERTIAL_DAMPING     PARTIAL\n\
NORMAL_FLOW_LIMITED  BOTH\n\
MIN_SURFAREA         0\n\
MIN_SLOPE            0\n\
SURCHARGE_METHOD     SLOT\n\
\n",
        end_date = date_after(end_days),
        end_time = hms(end_rem),
        report = hms(o.report_step_s as f64),
        rstep = o.routing_step_s,
    );

    out.push_str("[JUNCTIONS]\n;;Name  Invert  MaxDepth  InitDepth  SurDepth  Aponded\n");
    for (nm, md) in nodes.iter().zip(&max_depths) {
        if !nm.is_outfall {
            let _ = writeln!(out, "{}  {:.4}  {:.4}  0  0  0", nm.swmm, nm.invert, md);
        }
    }
    out.push_str("\n[OUTFALLS]\n;;Name  Invert  Type  Gated\n");
    for nm in nodes.iter().filter(|n| n.is_outfall) {
        let _ = writeln!(out, "{}  {:.4}  FREE  NO", nm.swmm, nm.invert);
    }

    // conduits
    let mut used_links = HashSet::new();
    let mut links = Vec::new();
    let mut cond = String::from(
        "\n[CONDUITS]\n;;Name  From  To  Length  Roughness  InOffset  OutOffset  InitFlow  MaxFlow\n",
    );
    let mut xs =
        String::from("\n[XSECTIONS]\n;;Link  Shape  Geom1  Geom2  Geom3  Geom4  Barrels\n");
    for c in &model.conduits {
        let (u, d) = (
            node_ix[c.us_node_id.as_str()],
            node_ix[c.ds_node_id.as_str()],
        );
        let (un, dn) = (&nodes[node_swmm_ix[&u]], &nodes[node_swmm_ix[&d]]);
        let dia = c.effective_id_mm() / 1000.0;
        if !(dia > 0.0) {
            return Err(SimError::MissingData(format!(
                "conduit {}: no diameter",
                c.conduit_id
            )));
        }
        let mut len = c.effective_length().filter(|l| *l > 0.0).ok_or_else(|| {
            SimError::MissingData(format!("conduit {}: no length or geometry", c.conduit_id))
        })?;
        if len < o.min_length_m {
            warnings.push(format!(
                "conduit {}: length {:.2} m lengthened to {:.1} m",
                c.conduit_id, len, o.min_length_m
            ));
            len = o.min_length_m;
        }
        let in_inv = c.us_invert.unwrap_or(un.invert);
        let out_inv = c.ds_invert.unwrap_or(dn.invert);
        if c.us_invert.is_none() || c.ds_invert.is_none() {
            warnings.push(format!(
                "conduit {}: missing invert(s); node invert used",
                c.conduit_id
            ));
        }
        let n = match o.roughness {
            Roughness::Manning(n) => n,
            Roughness::FromKs => {
                n_from_ks(dia, (in_inv - out_inv) / len, c.ks_mm.unwrap_or(o.ks_mm))
            }
        };
        let sid = safe_id(&c.conduit_id, &mut used_links);
        let _ = writeln!(
            cond,
            "{sid}  {}  {}  {len:.3}  {n:.4}  {in_inv:.4}  {out_inv:.4}  0  0",
            un.swmm, dn.swmm
        );
        let _ = writeln!(xs, "{sid}  CIRCULAR  {dia:.4}  0  0  0  1");
        links.push(LinkMeta {
            orig: c.conduit_id.clone(),
            swmm: sid,
            diameter_m: dia,
        });
    }
    out.push_str(&cond);
    out.push_str(&xs);

    // inflows + time series
    let by_orig: HashMap<&str, &NodeMeta> = nodes.iter().map(|n| (n.orig.as_str(), n)).collect();
    let mut inf = String::from(
        "\n[INFLOWS]\n;;Node  Constituent  TimeSeries  Type  Mfactor  Sfactor  Baseline  Pattern\n",
    );
    let mut ts = String::from("\n[TIMESERIES]\n;;Name  Hours  Value\n");
    let end_min = o.duration_h * 60.0;
    let mut used_ts = HashSet::new();
    for f in &input.inflows {
        let nm = by_orig
            .get(f.node_id.as_str())
            .ok_or_else(|| SimError::UnknownNode(f.node_id.clone()))?;
        let tsid = safe_id(&format!("TS_{}", nm.swmm), &mut used_ts);
        let _ = writeln!(inf, "{}  FLOW  {tsid}  FLOW  1  1  0", nm.swmm);
        let mut pts: Vec<(f64, f64)> = Vec::new();
        match f.profile.as_ref().filter(|p| !p.is_empty()) {
            None => {
                pts.push((0.0, f.base_lps));
                pts.push((end_min + 1440.0, f.base_lps));
            }
            Some(p) => {
                let mut p = p.clone();
                p.sort_by_key(|x| x.0);
                let days = (end_min / 1440.0).ceil() as u32 + 1;
                for day in 0..days {
                    let base = day as f64 * 1440.0;
                    let mut ms: Vec<f64> = p.iter().map(|x| (x.0 % 1440) as f64).collect();
                    ms.push(0.0);
                    ms.sort_by(|a, b| a.partial_cmp(b).unwrap());
                    ms.dedup();
                    for m in ms {
                        pts.push((base + m, f.base_lps * profile_factor(&p, m)));
                    }
                }
            }
        }
        for (m, v) in pts {
            let _ = writeln!(ts, "{tsid}  {:.4}  {:.6}", m / 60.0, v);
        }
    }
    out.push_str(&inf);
    out.push_str(&ts);

    out.push_str("\n[REPORT]\nINPUT NO\nCONTROLS NO\nSUBCATCHMENTS NONE\nNODES NONE\nLINKS NONE\n");
    out.push_str("\n[COORDINATES]\n;;Node  X  Y\n");
    for nm in &nodes {
        let ni = node_ix[nm.orig.as_str()];
        if let Some(g) = model.nodes[ni].geom {
            let _ = writeln!(out, "{}  {:.3}  {:.3}", nm.swmm, g.x, g.y);
        }
    }
    out.push_str("\n[VERTICES]\n;;Link  X  Y\n");
    for (c, lm) in model.conduits.iter().zip(&links) {
        if let Some(g) = &c.geom {
            if g.0.len() > 2 {
                for p in &g.0[1..g.0.len() - 1] {
                    let _ = writeln!(out, "{}  {:.3}  {:.3}", lm.swmm, p.x, p.y);
                }
            }
        }
    }

    let mut id_map = Vec::new();
    for n in &nodes {
        id_map.push(IdMapEntry {
            kind: "node".into(),
            original: n.orig.clone(),
            swmm: n.swmm.clone(),
        });
    }
    for l in &links {
        id_map.push(IdMapEntry {
            kind: "conduit".into(),
            original: l.orig.clone(),
            swmm: l.swmm.clone(),
        });
    }
    Ok(BuiltInp {
        text: out,
        nodes,
        links,
        warnings,
        id_map,
    })
}
