use std::path::PathBuf;
use wwnet_core::{gpkg, Model, NodeLoad};
use wwnet_design::service::import_profile;
use wwnet_sim::service::{simulate_file, simulate_json, ServiceOptions};

fn demo() -> (tempfile::TempDir, PathBuf) {
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/demo/demo.gpkg");
    let dir = tempfile::tempdir().unwrap();
    let dst = dir.path().join("demo.gpkg");
    std::fs::copy(src, &dst).unwrap();
    (dir, dst)
}

fn profile_csv() -> String {
    std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../templates/profiles/example_residential.csv"
    ))
    .unwrap()
}

fn add_loads(f: &PathBuf) {
    gpkg::upgrade(f).unwrap();
    let c = gpkg::open(f).unwrap();
    let model = Model::load(f).unwrap();
    for (i, n) in model
        .nodes
        .iter()
        .filter(|n| n.node_type != wwnet_core::NodeType::Outfall)
        .skip(2)
        .step_by(3)
        .enumerate()
    {
        gpkg::insert_node_load(
            &c,
            &NodeLoad {
                load_id: format!("L{i}"),
                node_id: n.node_id.clone(),
                connections: Some(20),
                adwf_lps: None,
                profile_id: None,
                source: Some("connection_points".into()),
                notes: None,
            },
        )
        .unwrap();
    }
}

fn count(f: &PathBuf, sql: &str) -> i64 {
    gpkg::open(f)
        .unwrap()
        .query_row(sql, [], |r| r.get(0))
        .unwrap()
}

#[test]
fn simulate_writes_results_and_matches_profile() {
    let (_d, f) = demo();
    add_loads(&f);
    import_profile(&f, "res", &profile_csv(), "test", true, false).unwrap();
    let out = simulate_file(&f, &ServiceOptions::default(), true).unwrap();
    let s = &out.summary;
    assert!(s.inflow_adwf_lps > 0.5, "adwf {}", s.inflow_adwf_lps);
    assert!(
        s.continuity_error_pct.abs() < 1.0,
        "{}",
        s.continuity_error_pct
    );
    let max_factor = 2.763;
    let ratio = s.peak_outlet_flow_lps / (s.inflow_adwf_lps * max_factor);
    assert!((ratio - 1.0).abs() < 0.15, "peak ratio {ratio}");
    assert!(!out.warnings.iter().any(|w| w.contains("constant ADWF")));

    // tables
    let run = out.run_id.clone().unwrap();
    assert_eq!(count(&f, "select count(*) from wwn_sim_runs"), 1);
    assert_eq!(
        count(&f, "select count(*) from wwn_sim_links"),
        out.links.len() as i64
    );
    assert_eq!(
        count(&f, "select count(*) from wwn_sim_nodes"),
        out.nodes.len() as i64
    );
    // series: 5 conduits, 24 h of 5-min samples each
    let n_series = count(&f, "select count(distinct object_id) from wwn_sim_series");
    assert_eq!(n_series, 5);

    // mean outlet flow over the reported day ~ total ADWF
    let model = Model::load(&f).unwrap();
    let c = gpkg::open(&f).unwrap();
    let mut mean = 0.0;
    for outlet in model
        .conduits
        .iter()
        .filter(|c| !model.conduits.iter().any(|d| d.us_node_id == c.ds_node_id))
    {
        let m: f64 = c
            .query_row(
                "select avg(flow_lps) from wwn_sim_series where run_id=? and object_id=?",
                rusqlite::params![run, outlet.conduit_id],
                |r| r.get(0),
            )
            .unwrap();
        mean += m;
    }
    assert!(
        (mean / s.inflow_adwf_lps - 1.0).abs() < 0.05,
        "mean {mean} vs {}",
        s.inflow_adwf_lps
    );

    // JSON shape
    let v: serde_json::Value = serde_json::from_str(&simulate_json(&out).unwrap()).unwrap();
    for k in ["summary", "links", "nodes", "warnings", "run_id", "options"] {
        assert!(v.get(k).is_some(), "missing {k}");
    }
}

#[test]
fn flow_scale_and_no_profile_warning() {
    let (_d, f) = demo();
    add_loads(&f);
    let o = ServiceOptions {
        duration_h: 12.0,
        warmup_h: 6.0,
        ..ServiceOptions::default()
    };
    let a = simulate_file(&f, &o, false).unwrap();
    assert!(a
        .warnings
        .iter()
        .any(|w| w == "no diurnal profile: constant ADWF"));
    assert!(a.run_id.is_none());
    assert_eq!(count(&f, "select count(*) from wwn_sim_runs"), 0);
    let b = simulate_file(
        &f,
        &ServiceOptions {
            flow_scale: 1.5,
            ..o.clone()
        },
        false,
    )
    .unwrap();
    let r = b.summary.peak_outlet_flow_lps / a.summary.peak_outlet_flow_lps;
    assert!((r - 1.5).abs() < 0.05, "{r}");
    assert!((b.summary.inflow_simulated_lps / b.summary.inflow_adwf_lps - 1.5).abs() < 1e-9);
}

#[test]
fn bad_options_are_errors() {
    let (_d, f) = demo();
    let e = simulate_file(
        &f,
        &ServiceOptions {
            roughness: "chezy".into(),
            ..ServiceOptions::default()
        },
        false,
    )
    .unwrap_err();
    assert!(e.to_string().contains("roughness must"), "{e}");
}
