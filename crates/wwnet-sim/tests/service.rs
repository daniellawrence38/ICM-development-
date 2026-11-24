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

#[test]
fn labelled_runs_show_compare_and_series() {
    use wwnet_sim::service::{compare_runs, list_runs, read_series, show_run};
    let (_d, f) = demo();
    add_loads(&f);
    let o = ServiceOptions {
        duration_h: 12.0,
        warmup_h: 6.0,
        label: "base".into(),
        ..ServiceOptions::default()
    };
    let a = simulate_file(&f, &o, true).unwrap();
    assert_eq!(a.label, "base");
    // displayed layer = latest run, with geometry and a d/D class for every conduit
    let shown = count(
        &f,
        "select count(*) from wwn_sim_conduit_results where geom is not null and d_class is not null",
    );
    assert_eq!(shown, a.links.len() as i64);
    assert_eq!(
        count(&f, "select count(*) from wwn_sim_node_results"),
        a.nodes.len() as i64
    );
    let b = simulate_file(
        &f,
        &ServiceOptions {
            flow_scale: 2.0,
            label: "x2".into(),
            ..o.clone()
        },
        true,
    )
    .unwrap();
    let runs = list_runs(&f).unwrap();
    assert_eq!(runs.len(), 2);
    assert_eq!(runs[0].label, "x2");
    assert!(runs[0].shown && !runs[1].shown);
    assert!(runs[0].summary.get("peak_outlet_flow_lps").is_some());
    // show the older run by label
    let s = show_run(&f, "base").unwrap();
    assert_eq!(s.run_id, a.run_id.clone().unwrap());
    let lbl: String = gpkg::open(&f)
        .unwrap()
        .query_row(
            "select label from wwn_sim_conduit_results limit 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(lbl, "base");
    assert!(show_run(&f, "nope").is_err());
    // compare: doubling the flow makes conduits worse
    let cmp = compare_runs(&f, "base", "x2", true).unwrap();
    assert_eq!(cmp.rows.len(), b.links.len());
    let worse = cmp.counts.get("worse").copied().unwrap_or(0)
        + cmp.counts.get("new_surcharge").copied().unwrap_or(0);
    assert!(worse > 0, "{:?}", cmp.counts);
    assert_eq!(
        count(
            &f,
            "select count(*) from wwn_sim_compare where geom is not null"
        ),
        cmp.rows.len() as i64
    );
    assert!(compare_runs(&f, "latest", "x2", false).is_err());
    // series of the latest run
    let sr = read_series(&f, "latest", &[]).unwrap();
    assert_eq!(sr.series.len(), 5);
    assert!(sr.series[0].minutes.len() > 10);
    let sr = read_series(&f, "previous", &["ZZZ".to_string()]).unwrap();
    assert_eq!(sr.missing, vec!["ZZZ".to_string()]);
    // simulate issues replace their own rows
    let before = count(
        &f,
        "select count(*) from wwn_issues where source='simulate'",
    );
    simulate_file(&f, &o, true).unwrap();
    assert_eq!(
        count(
            &f,
            "select count(*) from wwn_issues where source='simulate'"
        ),
        before
    );
}

#[test]
fn pump_station_results_are_stored() {
    use wwnet_core::{Conduit, Node, NodeLoad, NodeType, Pump, PumpStation, RisingMain};
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("ps.gpkg");
    gpkg::create(&f).unwrap();
    let c = gpkg::open(&f).unwrap();
    let mk = |id: &str, x: f64, inv: f64, sf: f64| {
        let mut n = Node::new(id, x, 0.0);
        n.invert_level = Some(inv);
        n.surface_level = Some(sf);
        n
    };
    let mut ww = mk("WW", 100.0, 18.0, 21.0);
    ww.node_type = NodeType::WetWell;
    for n in [
        mk("A", 0.0, 19.0, 22.0),
        ww,
        mk("D", 300.0, 25.0, 28.0),
        mk("OUT", 350.0, 24.5, 27.5),
    ] {
        gpkg::insert_node(&c, &n).unwrap();
    }
    for (id, u, d, len, ui, di) in [
        ("G1", "A", "WW", 100.0, 19.0, 18.0),
        ("G2", "D", "OUT", 50.0, 25.0, 24.5),
    ] {
        let mut k = Conduit::new(id, u, d, 225);
        k.length_m = Some(len);
        k.us_invert = Some(ui);
        k.ds_invert = Some(di);
        gpkg::insert_conduit(&c, &k).unwrap();
    }
    let mut ps = PumpStation::new("PS1");
    ps.wet_well_node_id = Some("WW".into());
    ps.rising_main_id = Some("RM1".into());
    ps.wet_well_area_m2 = Some(7.07);
    ps.stop_level = Some(18.5);
    ps.start_level = Some(19.5);
    ps.overflow_level = Some(20.5);
    gpkg::insert_pump_station(&c, &ps).unwrap();
    let mut p = Pump {
        pump_id: "PU1".into(),
        ps_id: "PS1".into(),
        role: "duty".into(),
        curve_json: Some("[[0, 20], [10, 16.5], [20, 11.5], [30, 5]]".into()),
        speed_pct: 100.0,
        notes: None,
    };
    gpkg::insert_pump(&c, &p).unwrap();
    p.pump_id = "PU2".into();
    p.role = "standby".into();
    gpkg::insert_pump(&c, &p).unwrap();
    let rm = RisingMain {
        rm_id: "RM1".into(),
        geom: None,
        ps_id: Some("PS1".into()),
        discharge_node_id: Some("D".into()),
        dn: Some(150),
        internal_diameter_mm: None,
        material: None,
        roughness_type: "ks".into(),
        roughness: Some(1.0),
        length_m: Some(200.0),
        profile_json: None,
        status: Default::default(),
        data_source: Default::default(),
        notes: None,
    };
    gpkg::insert_rising_main(&c, &rm).unwrap();
    let l = NodeLoad {
        load_id: "L1".into(),
        node_id: "A".into(),
        connections: None,
        adwf_lps: Some(8.0),
        profile_id: None,
        source: None,
        notes: None,
    };
    gpkg::insert_node_load(&c, &l).unwrap();
    drop(c);

    let o = ServiceOptions {
        duration_h: 12.0,
        warmup_h: 6.0,
        ..ServiceOptions::default()
    };
    let out = simulate_file(&f, &o, true).unwrap();
    assert_eq!(out.pumps.len(), 1);
    assert!(out.pumps[0].starts > 3);
    assert_eq!(out.pump_stations[0].overflow_volume_m3, 0.0);
    assert_eq!(out.rising_mains.len(), 1);
    assert!(out.series_ids.contains(&"WW".to_string()));
    assert!(simulate_json(&out).unwrap().contains("starts_per_hour"));
    assert_eq!(count(&f, "SELECT count(*) FROM wwn_sim_pumps"), 1);
    assert_eq!(count(&f, "SELECT count(*) FROM wwn_sim_pump_stations"), 1);
    assert_eq!(count(&f, "SELECT count(*) FROM wwn_sim_rising_mains"), 1);
    assert!(
        count(
            &f,
            "SELECT count(*) FROM wwn_sim_series WHERE object_id='WW'"
        ) > 0
    );
}
