//! Runs the generated model in the embedded SWMM engine and reduces the results.
use crate::inp::build_inp;
use crate::types::*;
use std::collections::BTreeMap;
use std::ffi::CString;
use std::os::raw::c_char;
use wwnet_swmm_sys as sys;

fn last_error() -> (i32, String) {
    let mut buf = vec![0 as c_char; 512];
    let code = unsafe { sys::swmm_getError(buf.as_mut_ptr(), buf.len() as i32) };
    let bytes: Vec<u8> = buf
        .iter()
        .take_while(|c| **c != 0)
        .map(|c| *c as u8)
        .collect();
    (code, String::from_utf8_lossy(&bytes).trim().to_string())
}

fn swmm_err(code: i32) -> SimError {
    let (c, m) = last_error();
    SimError::Swmm {
        code: if c != 0 { c } else { code },
        message: m,
    }
}

/// Closes the engine on drop so every error path leaves SWMM reusable.
struct Open;
impl Drop for Open {
    fn drop(&mut self) {
        unsafe {
            sys::swmm_close();
        }
    }
}

struct LinkAcc {
    ix: i32,
    r: LinkResult,
    flow_dt: f64,
}
struct NodeAcc {
    ix: i32,
    r: NodeResult,
    /// Minutes since start at which `r.max_hgl` occurred.
    max_hgl_t_min: f64,
}
/// Flow below this (L/s) counts as a stopped pump.
const PUMP_ON_LPS: f64 = 0.01;
struct PumpAcc {
    ix: i32,
    on: bool,
    on_s: f64,
    r: PumpResult,
}
struct FmAcc {
    ixs: Vec<i32>,
    r: RisingMainResult,
}

/// Simulate the network: gravity conduits, pump stations (wet well, pumps, rising main);
/// every other terminal node is a free outfall (hence the alias [`simulate_gpkg_free`]).
pub fn simulate(input: &SimInput<'_>) -> Result<SimResult> {
    let built = build_inp(input)?;
    let o = &input.options;
    let dir = tempfile::tempdir()?;
    let (inp_p, rpt_p, out_p) = (
        dir.path().join("model.inp"),
        dir.path().join("model.rpt"),
        dir.path().join("model.out"),
    );
    std::fs::write(&inp_p, &built.text)?;
    let cs = |p: &std::path::Path| {
        CString::new(p.to_string_lossy().as_bytes())
            .map_err(|_| SimError::BadOptions("path contains NUL".into()))
    };
    let (c_inp, c_rpt, c_out) = (cs(&inp_p)?, cs(&rpt_p)?, cs(&out_p)?);

    let _lock = sys::lock();
    let version = unsafe { sys::swmm_getVersion() };
    let rc = unsafe { sys::swmm_open(c_inp.as_ptr(), c_rpt.as_ptr(), c_out.as_ptr()) };
    let _open = Open; // close even if open failed (SWMM tolerates it)
    if rc != 0 {
        return Err(swmm_err(rc));
    }
    let rc = unsafe { sys::swmm_start(0) };
    if rc != 0 {
        return Err(swmm_err(rc));
    }

    let mut links: Vec<LinkAcc> = Vec::new();
    for l in &built.links {
        let name = CString::new(l.swmm.clone()).unwrap();
        let ix = unsafe { sys::swmm_getIndex(sys::SWMM_LINK, name.as_ptr()) };
        let full = unsafe { sys::swmm_getValue(sys::SWMM_LINK_FULLFLOW, ix) };
        links.push(LinkAcc {
            ix,
            flow_dt: 0.0,
            r: LinkResult {
                conduit_id: l.orig.clone(),
                max_flow_lps: 0.0,
                max_flow_time_min: 0.0,
                mean_flow_lps: 0.0,
                max_depth_m: 0.0,
                max_d_over_d: 0.0,
                max_velocity_mps: 0.0,
                full_flow_lps: full,
                max_capacity_ratio: 0.0,
                surcharged_h: 0.0,
            },
        });
    }
    let mut nodes: Vec<NodeAcc> = Vec::new();
    for n in built.nodes.iter().filter(|n| !n.internal) {
        let name = CString::new(n.swmm.clone()).unwrap();
        let ix = unsafe { sys::swmm_getIndex(sys::SWMM_NODE, name.as_ptr()) };
        nodes.push(NodeAcc {
            ix,
            max_hgl_t_min: 0.0,
            r: NodeResult {
                node_id: n.orig.clone(),
                invert_level: n.invert,
                max_hgl: f64::NEG_INFINITY,
                max_depth_m: 0.0,
                soffit_level: n.soffit,
                is_outfall: n.is_outfall,
                flood_volume_m3: 0.0,
                flood_duration_h: 0.0,
                max_flood_lps: 0.0,
                surcharged_h: 0.0,
                max_surcharge_m: 0.0,
            },
        });
    }

    let idx = |kind, name: &str| {
        let c = CString::new(name).unwrap();
        unsafe { sys::swmm_getIndex(kind, c.as_ptr()) }
    };
    let mut pumps: Vec<PumpAcc> = built
        .pumps
        .iter()
        .map(|p| PumpAcc {
            ix: idx(sys::SWMM_LINK, &p.swmm),
            on: false,
            on_s: 0.0,
            r: PumpResult {
                pump_id: p.orig.clone(),
                ps_id: p.ps_id.clone(),
                role: p.role.clone(),
                max_flow_lps: 0.0,
                starts: 0,
                starts_per_hour: 0.0,
                run_time_pct: 0.0,
            },
        })
        .collect();
    let mut fms: Vec<FmAcc> = built
        .rising_mains
        .iter()
        .map(|m| FmAcc {
            ixs: m.segments.iter().map(|s| idx(sys::SWMM_LINK, s)).collect(),
            r: RisingMainResult {
                rm_id: m.rm_id.clone(),
                ps_id: m.ps_id.clone(),
                length_m: m.length_m,
                diameter_mm: m.diameter_m * 1000.0,
                max_flow_lps: 0.0,
                max_velocity_mps: 0.0,
            },
        })
        .collect();

    let want = |id: &str| o.series_ids.is_empty() || o.series_ids.iter().any(|s| s == id);
    let mut series = o.keep_series.then(|| {
        let mut s = SimSeries::default();
        for l in &links {
            if want(&l.r.conduit_id) {
                s.links.insert(
                    l.r.conduit_id.clone(),
                    LinkSeries {
                        flow_lps: vec![],
                        depth_m: vec![],
                        velocity_mps: vec![],
                    },
                );
            }
        }
        for n in &nodes {
            if want(&n.r.node_id) {
                s.nodes.insert(
                    n.r.node_id.clone(),
                    NodeSeries {
                        head_m: vec![],
                        depth_m: vec![],
                        overflow_lps: vec![],
                    },
                );
            }
        }
        s
    });

    let warm_s = o.warmup_h * 3600.0;
    let rep_s = o.report_step_s as f64;
    let mut next_rep = 0.0f64;
    let mut prev_s = 0.0f64;
    let mut stat_s = 0.0f64;
    let gv = |p, i| unsafe { sys::swmm_getValue(p, i) };
    loop {
        let mut el = 0.0f64;
        let rc = unsafe { sys::swmm_step(&mut el) };
        if rc != 0 {
            let e = swmm_err(rc);
            unsafe { sys::swmm_end() };
            return Err(e);
        }
        if el <= 0.0 {
            break;
        }
        let t = el * 86400.0;
        let dt = (t - prev_s.max(warm_s)).max(0.0);
        let counted = t > warm_s;
        prev_s = t;
        if counted {
            stat_s += dt;
        }
        let record = t + 1e-6 >= next_rep;
        if record {
            while next_rep <= t + 1e-6 {
                next_rep += rep_s;
            }
            if let Some(s) = series.as_mut() {
                s.times_min.push(t / 60.0);
            }
        }
        for l in links.iter_mut() {
            let q = gv(sys::SWMM_LINK_FLOW, l.ix);
            let y = gv(sys::SWMM_LINK_DEPTH, l.ix);
            let v = gv(sys::SWMM_LINK_VELOCITY, l.ix);
            if counted {
                let r = &mut l.r;
                if q.abs() > r.max_flow_lps {
                    r.max_flow_lps = q.abs();
                    r.max_flow_time_min = t / 60.0;
                }
                l.flow_dt += q * dt;
                r.max_depth_m = r.max_depth_m.max(y);
                r.max_velocity_mps = r.max_velocity_mps.max(v);
                if y >= 0.999 * gv(sys::SWMM_LINK_FULLDEPTH, l.ix) {
                    r.surcharged_h += dt / 3600.0;
                }
            }
            if record {
                if let Some(ls) = series
                    .as_mut()
                    .and_then(|s| s.links.get_mut(&l.r.conduit_id))
                {
                    ls.flow_lps.push(q);
                    ls.depth_m.push(y);
                    ls.velocity_mps.push(v);
                }
            }
        }
        for p in pumps.iter_mut() {
            let q = gv(sys::SWMM_LINK_FLOW, p.ix).abs();
            let on = q > PUMP_ON_LPS;
            if counted {
                if on && !p.on {
                    p.r.starts += 1;
                }
                if on {
                    p.on_s += dt;
                }
                p.r.max_flow_lps = p.r.max_flow_lps.max(q);
            }
            // a pump running at the end of the warm-up is not a start
            p.on = on;
        }
        for m in fms.iter_mut() {
            if counted {
                for &ix in &m.ixs {
                    m.r.max_flow_lps = m.r.max_flow_lps.max(gv(sys::SWMM_LINK_FLOW, ix).abs());
                    m.r.max_velocity_mps =
                        m.r.max_velocity_mps
                            .max(gv(sys::SWMM_LINK_VELOCITY, ix).abs());
                }
            }
        }
        for n in nodes.iter_mut() {
            let h = gv(sys::SWMM_NODE_HEAD, n.ix);
            let y = gv(sys::SWMM_NODE_DEPTH, n.ix);
            let of = gv(sys::SWMM_NODE_OVERFLOW, n.ix);
            if counted {
                let r = &mut n.r;
                if h > r.max_hgl {
                    r.max_hgl = h;
                    n.max_hgl_t_min = t / 60.0;
                }
                r.max_depth_m = r.max_depth_m.max(y);
                if of > 1e-9 {
                    r.flood_volume_m3 += of * dt / 1000.0;
                    r.flood_duration_h += dt / 3600.0;
                    r.max_flood_lps = r.max_flood_lps.max(of);
                }
                if !r.is_outfall && h > r.soffit_level + 1e-3 {
                    r.surcharged_h += dt / 3600.0;
                    r.max_surcharge_m = r.max_surcharge_m.max(h - r.soffit_level);
                }
            }
            if record {
                if let Some(ns) = series.as_mut().and_then(|s| s.nodes.get_mut(&n.r.node_id)) {
                    ns.head_m.push(h);
                    ns.depth_m.push(y);
                    ns.overflow_lps.push(of);
                }
            }
        }
    }
    let rc = unsafe { sys::swmm_end() };
    if rc != 0 {
        return Err(swmm_err(rc));
    }
    let (mut re, mut fe, mut qe) = (0f32, 0f32, 0f32);
    unsafe { sys::swmm_getMassBalErr(&mut re, &mut fe, &mut qe) };
    let nwarn = unsafe { sys::swmm_getWarnings() };
    let mut warnings = built.warnings.clone();
    if nwarn > 0 {
        warnings.push(format!("SWMM issued {nwarn} warning(s)"));
    }

    let dia: BTreeMap<&str, f64> = built
        .links
        .iter()
        .map(|l| (l.orig.as_str(), l.diameter_m))
        .collect();
    let links: Vec<LinkResult> = links
        .into_iter()
        .map(|mut l| {
            if stat_s > 0.0 {
                l.r.mean_flow_lps = l.flow_dt / stat_s;
            }
            let d = dia[l.r.conduit_id.as_str()];
            l.r.max_d_over_d = l.r.max_depth_m / d;
            if l.r.full_flow_lps > 0.0 {
                l.r.max_capacity_ratio = l.r.max_flow_lps / l.r.full_flow_lps;
            }
            l.r
        })
        .collect();
    let pump_results: Vec<PumpResult> = pumps
        .into_iter()
        .map(|mut p| {
            if stat_s > 0.0 {
                p.r.run_time_pct = 100.0 * p.on_s / stat_s;
                p.r.starts_per_hour = p.r.starts as f64 / (stat_s / 3600.0);
            }
            p.r
        })
        .collect();
    let station_results: Vec<PumpStationResult> = built
        .stations
        .iter()
        .filter_map(|st| {
            let n = nodes.iter().find(|n| n.r.node_id == st.wet_well_node_id)?;
            Some(PumpStationResult {
                ps_id: st.ps_id.clone(),
                wet_well_node_id: st.wet_well_node_id.clone(),
                max_level_m: if n.r.max_hgl.is_finite() {
                    n.r.max_hgl
                } else {
                    n.r.invert_level
                },
                max_depth_m: n.r.max_depth_m,
                overflow_volume_m3: n.r.flood_volume_m3,
                time_of_max_level_min: n.max_hgl_t_min,
            })
        })
        .collect();
    let nodes: Vec<NodeResult> = nodes
        .into_iter()
        .map(|mut n| {
            if !n.r.max_hgl.is_finite() {
                n.r.max_hgl = n.r.invert_level;
            }
            n.r
        })
        .collect();
    Ok(SimResult {
        swmm_version: version,
        links,
        nodes,
        pumps: pump_results,
        pump_stations: station_results,
        rising_mains: fms.into_iter().map(|m| m.r).collect(),
        continuity_runoff_error_pct: re as f64,
        continuity_flow_error_pct: fe as f64,
        warnings,
        inp_text: built.text,
        id_map: built.id_map,
        series,
    })
}

/// Same as [`simulate`]: a network whose terminal nodes drain to free outfalls.
pub fn simulate_gpkg_free(input: &SimInput<'_>) -> Result<SimResult> {
    simulate(input)
}
