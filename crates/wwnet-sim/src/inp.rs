//! SWMM `.inp` writer. Output ordering follows model order, so it is stable.
#![allow(clippy::neg_cmp_op_on_partial_ord)] // deliberate: NaN must fail positivity checks
use crate::pumps::{self, ExtraNode, PumpMeta, RisingMainMeta, StationBuild, StationMeta};
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
    /// Plan area (m2) when the node is a pump-station wet well (a SWMM storage node).
    pub storage_area_m2: Option<f64>,
    /// SWMM junction surcharge depth, m.
    pub sur_depth_m: f64,
    /// Created for a rising main (not a model node); excluded from the results.
    pub internal: bool,
    /// Position of an internal node.
    pub xy: Option<(f64, f64)>,
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
    pub pumps: Vec<PumpMeta>,
    pub stations: Vec<StationMeta>,
    pub rising_mains: Vec<RisingMainMeta>,
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

    // pump stations: the wet well gets an outgoing (pumped) link, the discharge node an
    // incoming one
    let (plans, fm_eq) = pumps::plan_stations(model, &node_ix, &mut warnings);
    let mut wet_of: HashMap<usize, usize> = HashMap::new();
    for (pi, p) in plans.iter().enumerate() {
        wet_of.insert(p.wet_ix, pi);
        has_out[p.wet_ix] = true;
        connected[p.wet_ix] = true;
        connected[p.disch_ix] = true;
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
        let wet = wet_of.get(&i).map(|&pi| &plans[pi]);
        let top = match wet {
            Some(p) => p.ps.overflow_level.or(n.surface_level),
            None => n.surface_level,
        };
        let mut depth = match top {
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
            storage_area_m2: wet.map(|p| p.area_m2),
            sur_depth_m: 0.0,
            internal: false,
            xy: None,
        });
        max_depths.push(depth);
    }

    // pump stations: internal junctions + stored build info (links are written below)
    let mut used_links = HashSet::new();
    let mut builds: Vec<StationBuild> = Vec::new();
    for p in &plans {
        let wi = node_swmm_ix[&p.wet_ix];
        let di = node_swmm_ix[&p.disch_ix];
        let (wet, wet_depth) = (nodes[wi].clone(), max_depths[wi]);
        let disch = nodes[di].clone();
        let dis_xy = p.ps.geom.or(model.nodes[p.wet_ix].geom).map(|g| (g.x, g.y));
        let mut extra: Vec<ExtraNode> = Vec::new();
        let b = pumps::build_station(
            p,
            (&wet.swmm, wet.invert),
            (&disch.swmm, disch.invert),
            wet_depth,
            dis_xy,
            fm_eq,
            o,
            &mut used_nodes,
            &mut used_links,
            &safe_id,
            &mut extra,
            &mut warnings,
        );
        for e in extra {
            nodes.push(NodeMeta {
                orig: e.orig,
                swmm: e.swmm,
                invert: e.invert,
                soffit: e.invert + p.dia_m,
                is_outfall: false,
                storage_area_m2: None,
                sur_depth_m: pumps::FM_NODE_SUR_DEPTH_M,
                internal: true,
                xy: e.xy,
            });
            max_depths.push(e.max_depth);
        }
        builds.push(b);
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
FORCE_MAIN_EQUATION  {fm_eq}\n\
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
        fm_eq = fm_eq.keyword(),
        end_date = date_after(end_days),
        end_time = hms(end_rem),
        report = hms(o.report_step_s as f64),
        rstep = o.routing_step_s,
    );

    out.push_str("[JUNCTIONS]\n;;Name  Invert  MaxDepth  InitDepth  SurDepth  Aponded\n");
    for (nm, md) in nodes.iter().zip(&max_depths) {
        if !nm.is_outfall && nm.storage_area_m2.is_none() {
            let _ = writeln!(
                out,
                "{}  {:.4}  {:.4}  0  {}  0",
                nm.swmm, nm.invert, md, nm.sur_depth_m
            );
        }
    }
    // wet wells: constant plan area, a0 + a1*d^a2 with a1 = a2 = 0 (node.c storage_readParams)
    if nodes.iter().any(|n| n.storage_area_m2.is_some()) {
        out.push_str(
            "\n[STORAGE]\n;;Name  Invert  MaxDepth  InitDepth  Shape  a1  a2  a0  SurDepth  Fevap\n",
        );
    }
    for (nm, md) in nodes.iter().zip(&max_depths) {
        if let Some(a) = nm.storage_area_m2 {
            let _ = writeln!(
                out,
                "{}  {:.4}  {:.4}  0  FUNCTIONAL  0  0  {:.4}  0  0",
                nm.swmm, nm.invert, md, a
            );
        }
    }
    out.push_str("\n[OUTFALLS]\n;;Name  Invert  Type  Gated\n");
    for nm in nodes.iter().filter(|n| n.is_outfall) {
        let _ = writeln!(out, "{}  {:.4}  FREE  NO", nm.swmm, nm.invert);
    }

    // conduits
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
    // rising mains (force mains): roughness is ks in mm (D-W) or C (H-W), Geom2 of the
    // FORCE_MAIN cross section (xsect.c xsect_setParams stores p[1] in rBot; link.c
    // conduit_validate divides D-W ks by UCF(RAINDEPTH) = 304.8 mm/ft, swmm5.c Ucf table)
    for b in &builds {
        for sg in &b.segs {
            let _ = writeln!(
                cond,
                "{}  {}  {}  {:.3}  {:.4}  {:.4}  {:.4}  0  0",
                sg.swmm,
                sg.from,
                sg.to,
                sg.length_m,
                pumps::FM_PLACEHOLDER_N,
                sg.in_offset,
                sg.out_offset
            );
            let _ = writeln!(
                xs,
                "{}  FORCE_MAIN  {:.4}  {:.4}  0  0  1",
                sg.swmm, sg.dia_m, sg.roughness
            );
        }
    }
    out.push_str(&cond);
    out.push_str(&xs);

    // pumps: Name From To Curve Status Startup Shutoff (link.c pump_readParams)
    if builds.iter().any(|b| !b.pumps.is_empty()) {
        out.push_str(
            "\n[PUMPS]\n;;Name  From  To  Curve  Status  Startup(depth)  Shutoff(depth)\n",
        );
        for b in &builds {
            for p in &b.pumps {
                let _ = writeln!(
                    out,
                    "{}  {}  {}  {}  OFF  {:.4}  {:.4}",
                    p.swmm, p.from, p.to, p.curve, p.on_depth, p.off_depth
                );
            }
        }
        out.push_str("\n[CURVES]\n;;Name  Type  X-Value  Y-Value\n");
        for b in &builds {
            for c in &b.curves {
                for (i, (x, y)) in c.points.iter().enumerate() {
                    if i == 0 {
                        let _ = writeln!(out, "{}  {}  {:.4}  {:.4}", c.name, c.kind, x, y);
                    } else {
                        let _ = writeln!(out, "{}  {:.4}  {:.4}", c.name, x, y);
                    }
                }
            }
        }
    }

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
        let xy = match node_ix.get(nm.orig.as_str()) {
            Some(&ni) if !nm.internal => model.nodes[ni].geom.map(|g| (g.x, g.y)),
            _ => nm.xy,
        };
        if let Some((x, y)) = xy {
            let _ = writeln!(out, "{}  {:.3}  {:.3}", nm.swmm, x, y);
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
    let mut pump_meta = Vec::new();
    let mut stations = Vec::new();
    let mut rising_mains = Vec::new();
    for (p, b) in plans.iter().zip(&builds) {
        stations.push(StationMeta {
            ps_id: p.ps.ps_id.clone(),
            wet_well_node_id: model.nodes[p.wet_ix].node_id.clone(),
        });
        for pm in &b.pumps {
            id_map.push(IdMapEntry {
                kind: "pump".into(),
                original: pm.orig.clone(),
                swmm: pm.swmm.clone(),
            });
            pump_meta.push(PumpMeta {
                orig: pm.orig.clone(),
                swmm: pm.swmm.clone(),
                ps_id: pm.ps_id.clone(),
                role: pm.role.clone(),
            });
        }
        if let Some(rm) = &b.rm {
            id_map.push(IdMapEntry {
                kind: "force_main".into(),
                original: rm.rm_id.clone(),
                swmm: rm.segments.join(","),
            });
            rising_mains.push(rm.clone());
        }
    }
    Ok(BuiltInp {
        text: out,
        nodes,
        links,
        warnings,
        id_map,
        pumps: pump_meta,
        stations,
        rising_mains,
    })
}
