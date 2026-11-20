use wwnet_core::{Conduit, Model, Node};
use wwnet_hydraulics::manning_flow;
use wwnet_sim::{inp::build_inp, simulate, NodeInflow, Roughness, SimError, SimInput, SimOptions};

fn node(id: &str, x: f64, inv: f64, surface: f64) -> Node {
    let mut n = Node::new(id, x, 0.0);
    n.invert_level = Some(inv);
    n.surface_level = Some(surface);
    n
}

fn pipe(id: &str, us: &str, ds: &str, dn: i64, len: f64, ui: f64, di: f64) -> Conduit {
    let mut c = Conduit::new(id, us, ds, dn);
    c.length_m = Some(len);
    c.us_invert = Some(ui);
    c.ds_invert = Some(di);
    c
}

fn opts() -> SimOptions {
    SimOptions {
        duration_h: 12.0,
        warmup_h: 6.0,
        ..SimOptions::default()
    }
}

fn constant(id: &str, q: f64) -> NodeInflow {
    NodeInflow {
        node_id: id.into(),
        base_lps: q,
        profile: None,
    }
}

fn single_pipe(dn: i64, slope: f64) -> Model {
    let len = 100.0;
    Model {
        nodes: vec![
            node("A", 0.0, 100.0, 103.0),
            node("B", len, 100.0 - slope * len, 103.0 - slope * len),
        ],
        conduits: vec![pipe("P1", "A", "B", dn, len, 100.0, 100.0 - slope * len)],
        ..Model::default()
    }
}

#[test]
fn single_pipe_steady_matches_manning() {
    let m = single_pipe(150, 0.01);
    let r = simulate(&SimInput {
        model: &m,
        inflows: vec![constant("A", 5.0)],
        options: opts(),
    })
    .unwrap();
    let l = &r.links[0];
    assert!(
        (l.mean_flow_lps - 5.0).abs() < 0.05,
        "mean {}",
        l.mean_flow_lps
    );
    assert!(
        (l.max_flow_lps - 5.0).abs() < 0.05,
        "max {}",
        l.max_flow_lps
    );
    // Manning normal depth by bisection
    let (mut lo, mut hi) = (0.0, 0.93);
    for _ in 0..60 {
        let mid = 0.5 * (lo + hi);
        if manning_flow(0.15, 0.01, mid, 0.013) * 1000.0 < 5.0 {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let theory = 0.5 * (lo + hi);
    assert!(
        (l.max_d_over_d - theory).abs() < 0.05 * theory,
        "d/D {} vs theory {}",
        l.max_d_over_d,
        theory
    );
    assert!(r.continuity_flow_error_pct.abs() < 1.0);
    assert_eq!(r.swmm_version, 52004);
}

#[test]
fn tree_accumulates() {
    let m = Model {
        nodes: vec![
            node("A", 0.0, 100.0, 103.0),
            node("B", 0.0, 100.0, 103.0),
            node("C", 50.0, 99.5, 102.5),
            node("D", 100.0, 99.0, 102.0),
        ],
        conduits: vec![
            pipe("P1", "A", "C", 150, 50.0, 100.0, 99.5),
            pipe("P2", "B", "C", 150, 50.0, 100.0, 99.5),
            pipe("P3", "C", "D", 225, 50.0, 99.5, 99.0),
        ],
        ..Model::default()
    };
    let r = simulate(&SimInput {
        model: &m,
        inflows: vec![constant("A", 2.0), constant("B", 3.0), constant("C", 1.0)],
        options: opts(),
    })
    .unwrap();
    let q = |id: &str| {
        r.links
            .iter()
            .find(|l| l.conduit_id == id)
            .unwrap()
            .mean_flow_lps
    };
    assert!((q("P1") - 2.0).abs() < 0.05);
    assert!((q("P2") - 3.0).abs() < 0.05);
    assert!((q("P3") - 6.0).abs() < 0.1);
    assert!(
        r.nodes
            .iter()
            .find(|n| n.node_id == "D")
            .unwrap()
            .is_outfall
    );
    assert!(r.continuity_flow_error_pct.abs() < 1.0);
}

#[test]
fn diurnal_profile_peak_and_mean() {
    let m = single_pipe(225, 0.01);
    let prof: Vec<(u32, f64)> = (0..24)
        .map(|h| {
            let f = 1.0 + 0.8 * (2.0 * std::f64::consts::PI * (h as f64 - 3.0) / 24.0).sin();
            (h * 60, f)
        })
        .collect();
    let peak_f = prof.iter().map(|p| p.1).fold(0.0, f64::max);
    let mean_f = prof.iter().map(|p| p.1).sum::<f64>() / 24.0;
    let r = simulate(&SimInput {
        model: &m,
        inflows: vec![NodeInflow {
            node_id: "A".into(),
            base_lps: 4.0,
            profile: Some(prof),
        }],
        options: SimOptions {
            duration_h: 48.0,
            warmup_h: 24.0,
            keep_series: true,
            series_ids: vec!["P1".into()],
            ..SimOptions::default()
        },
    })
    .unwrap();
    let l = &r.links[0];
    assert!(
        l.max_flow_lps <= 4.0 * peak_f * 1.01,
        "peak {}",
        l.max_flow_lps
    );
    assert!(
        l.max_flow_lps >= 4.0 * peak_f * 0.9,
        "peak {}",
        l.max_flow_lps
    );
    assert!((l.mean_flow_lps - 4.0 * mean_f).abs() < 0.04 * 4.0 * mean_f);
    let s = r.series.as_ref().unwrap();
    assert!(s.links.contains_key("P1") && !s.nodes.contains_key("A"));
    assert_eq!(s.links["P1"].flow_lps.len(), s.times_min.len());
    assert!(s.times_min.len() > 500);
    // json round trip
    let j = serde_json::to_string(&r).unwrap();
    assert!(j.contains("\"max_flow_lps\""));
}

#[test]
fn undersized_pipe_surcharges_and_floods() {
    let m = single_pipe(150, 0.002);
    let r = simulate(&SimInput {
        model: &m,
        inflows: vec![constant("A", 60.0)],
        options: opts(),
    })
    .unwrap();
    let l = &r.links[0];
    assert!(l.surcharged_h > 0.0, "surcharge {}", l.surcharged_h);
    assert!(l.max_capacity_ratio > 1.0);
    let a = r.nodes.iter().find(|n| n.node_id == "A").unwrap();
    assert!(a.flood_volume_m3 > 0.0, "flood {}", a.flood_volume_m3);
    assert!(a.surcharged_h > 0.0 && a.max_surcharge_m > 0.0);
    assert!(r.continuity_flow_error_pct.abs() < 1.0);
}

#[test]
fn errors_are_typed() {
    let m = single_pipe(150, 0.01);
    let e = simulate(&SimInput {
        model: &m,
        inflows: vec![constant("ZZ", 1.0)],
        options: opts(),
    })
    .unwrap_err();
    assert!(matches!(e, SimError::UnknownNode(_)));
    let e = simulate(&SimInput {
        model: &m,
        inflows: vec![],
        options: SimOptions {
            warmup_h: 99.0,
            ..opts()
        },
    })
    .unwrap_err();
    assert!(matches!(e, SimError::BadOptions(_)));
}

fn snapshot_model() -> Model {
    let mut m = Model {
        nodes: vec![
            node("MH 1", 0.0, 10.0, 12.0),
            node("MH2", 40.0, 9.6, 11.6),
            Node::new("unused", 5.0, 5.0),
        ],
        conduits: vec![pipe("P 1", "MH 1", "MH2", 150, 40.0, 10.0, 9.6)],
        ..Model::default()
    };
    m.conduits[0].internal_diameter_mm = Some(153.0);
    m
}

#[test]
fn inp_snapshot_is_stable() {
    let m = snapshot_model();
    let input = SimInput {
        model: &m,
        inflows: vec![NodeInflow {
            node_id: "MH 1".into(),
            base_lps: 2.0,
            profile: Some(vec![(0, 1.0), (720, 2.0)]),
        }],
        options: SimOptions {
            duration_h: 26.0,
            warmup_h: 2.0,
            roughness: Roughness::Manning(0.013),
            ..SimOptions::default()
        },
    };
    let a = build_inp(&input).unwrap();
    let b = build_inp(&input).unwrap();
    assert_eq!(a.text, b.text);
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/snapshots/simple.inp");
    if std::env::var_os("UPDATE_SNAPSHOT").is_some() {
        std::fs::write(path, &a.text).unwrap();
    }
    let expected = std::fs::read_to_string(path).unwrap().replace("\r\n", "\n");
    assert_eq!(a.text, expected);
    assert!(a.text.contains("MH_1") && !a.text.contains("MH 1"));
    assert_eq!(
        a.id_map.iter().find(|e| e.swmm == "MH_1").unwrap().original,
        "MH 1"
    );
}

#[test]
fn short_conduit_is_lengthened_with_warning() {
    let mut m = single_pipe(150, 0.01);
    m.conduits[0].length_m = Some(2.0);
    let b = build_inp(&SimInput {
        model: &m,
        inflows: vec![],
        options: opts(),
    })
    .unwrap();
    assert!(b.warnings.iter().any(|w| w.contains("lengthened")));
}

#[test]
fn ks_roughness_gives_plausible_n() {
    let m = single_pipe(300, 0.005);
    let b = build_inp(&SimInput {
        model: &m,
        inflows: vec![],
        options: SimOptions {
            roughness: Roughness::FromKs,
            ..opts()
        },
    })
    .unwrap();
    let line = b.text.lines().find(|l| l.starts_with("P1  A  B")).unwrap();
    let n: f64 = line.split_whitespace().nth(4).unwrap().parse().unwrap();
    assert!((0.009..0.016).contains(&n), "n = {n}");
}
