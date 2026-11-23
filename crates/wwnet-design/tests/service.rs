use std::path::PathBuf;
use wwnet_core::Model;
use wwnet_design::service;

fn demo() -> (tempfile::TempDir, PathBuf) {
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/demo/demo.gpkg");
    let dir = tempfile::tempdir().unwrap();
    let dst = dir.path().join("demo.gpkg");
    std::fs::copy(src, &dst).unwrap();
    (dir, dst)
}

#[test]
fn check_writes_only_when_asked() {
    let (_d, f) = demo();
    let a = service::check_file(&f, false).unwrap();
    let b = service::check_file(&f, true).unwrap();
    assert_eq!(a, b);
    let json: serde_json::Value = serde_json::from_str(&service::issues_json(&b).unwrap()).unwrap();
    assert_eq!(json.as_array().unwrap().len(), b.len());
    assert_eq!(service::error_count(&b), 0);
}

#[test]
fn design_json_has_expected_shape_and_write_flag() {
    let (_d, f) = demo();
    let out = service::design_file(&f, None, false).unwrap();
    let v: serde_json::Value = serde_json::from_str(&service::design_json(&out).unwrap()).unwrap();
    for k in ["conduit_results", "node_flows", "issues", "summary"] {
        assert!(v.get(k).is_some(), "missing {k}");
    }
    let wrote = service::design_file(&f, None, true).unwrap();
    assert_eq!(wrote.summary.conduits, out.summary.conduits);
}

/// Repeated design runs replace their issues (B1) and keep the check's; results are a map layer.
#[test]
fn design_issues_do_not_accumulate_and_results_have_geometry() {
    let (_d, f) = demo();
    let checked = service::check_file(&f, true).unwrap();
    let d1 = service::design_file(&f, None, true).unwrap();
    let n1 = Model::read_issue_count(&f).unwrap();
    service::design_file(&f, None, true).unwrap();
    service::design_file(&f, None, true).unwrap();
    assert_eq!(Model::read_issue_count(&f).unwrap(), n1);
    assert_eq!(n1 as usize, checked.len() + d1.issues.len());
    let c = wwnet_core::gpkg::open(&f).unwrap();
    let (rows, with_geom, sources): (i64, i64, i64) = c
        .query_row(
            "SELECT (SELECT count(*) FROM wwn_results_conduits),
                    (SELECT count(*) FROM wwn_results_conduits WHERE geom IS NOT NULL AND total_lps IS NOT NULL),
                    (SELECT count(DISTINCT source) FROM wwn_issues)",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(rows as usize, d1.summary.conduits);
    assert_eq!(with_geom, rows);
    assert!(sources >= 1);
}

#[test]
fn store_issues_json_places_issues() {
    let (_d, f) = demo();
    let n = service::store_issues_json(
        &f,
        "import",
        r#"[{"severity":"warning","code":"TEE_JOINED","object_type":"conduit","object_id":"C01","message":"m"},
            {"severity":"warn","code":"X","object_type":"source","object_id":"9","message":"m","x":1.0,"y":2.0}]"#,
    )
    .unwrap();
    assert_eq!(n, 2);
    let c = wwnet_core::gpkg::open(&f).unwrap();
    let placed: i64 = c
        .query_row(
            "SELECT count(*) FROM wwn_issues WHERE source='import' AND geom IS NOT NULL",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(placed >= 1);
    assert!(service::store_issues_json(&f, "bogus", "[]").is_err());
    assert!(service::store_issues_json(&f, "import", "{").is_err());
}

#[test]
fn design_bad_ruleset_is_clear_error() {
    let (_d, f) = demo();
    let e = service::design_file(&f, Some(std::path::Path::new("/nonexistent.toml")), false)
        .unwrap_err();
    assert!(e.to_string().contains("loading /nonexistent.toml"), "{e}");
}

#[test]
fn trace_directions() {
    let (_d, f) = demo();
    let up = service::trace_file(&f, "N06", true, false).unwrap();
    assert!(up.downstream.is_none() && !up.upstream.unwrap().is_empty());
    let both = service::trace_file(&f, "N03", false, false).unwrap();
    assert!(both.upstream.is_some() && both.downstream.is_some());
    let j = service::trace_json(&service::trace_file(&f, "N03", false, true).unwrap()).unwrap();
    let v: serde_json::Value = serde_json::from_str(&j).unwrap();
    assert!(v.get("downstream").is_some() && v.get("upstream").is_none());
    assert!(v["summary"].get("upstream_nodes").is_some() && v["conduits"].is_array());
    let e = service::trace_file(&f, "ZZ", true, true).unwrap_err();
    assert!(matches!(e, wwnet_core::Error::NodeNotFound(_)));
}

#[test]
fn trace_from_conduit_reports_paths_and_flows() {
    let (_d, f) = demo();
    let m = Model::load(&f).unwrap();
    let last = m.conduits.last().unwrap().clone();
    let t = service::trace_file(&f, &last.conduit_id, true, false).unwrap();
    assert_eq!(
        t.summary.start_conduit.as_deref(),
        Some(last.conduit_id.as_str())
    );
    assert_eq!(t.summary.start_node, last.us_node_id);
    assert!(t.conduits.contains(&last.conduit_id));
    assert_eq!(t.upstream.as_ref().unwrap()[0], last.us_node_id);
    // the flow at the start equals the design flow in that conduit
    let d = service::design_file(&f, None, false).unwrap();
    let c = d
        .conduit_results
        .iter()
        .find(|c| c.conduit_id == last.conduit_id)
        .unwrap();
    assert!((t.summary.gsdf_lps.unwrap() - c.gsdf_lps).abs() < 1e-9);
    assert!(t.summary.upstream_conduits + 1 >= t.conduits.len());
    // node trace: every conduit listed joins traced nodes
    let n = service::trace_file(&f, &last.us_node_id, true, false).unwrap();
    assert_eq!(n.summary.start_conduit, None);
    assert_eq!(n.conduits.len(), n.summary.upstream_conduits);
}

#[test]
fn settings_and_profile_assignment() {
    let (_d, f) = demo();
    let s = service::get_settings(&f).unwrap();
    assert_eq!(s.schema_version, 3);
    assert_eq!(s.peaking_factor, 1.5);
    let s = service::set_settings(&f, Some(600.0), Some(2.0), None).unwrap();
    assert_eq!((s.adwf_per_connection_lpd, s.peaking_factor), (600.0, 2.0));
    assert!(service::set_settings(&f, None, Some(0.5), None).is_err());
    assert!(service::set_settings(&f, None, None, Some("nope")).is_err());
    service::import_profile(&f, "RES", "00:00,0.5\n12:00,1.5\n", "", false, false).unwrap();
    let s = service::set_settings(&f, None, None, Some("RES")).unwrap();
    assert_eq!(s.default_profile.as_deref(), Some("RES"));
    assert_eq!(s.profiles.len(), 1);
    let s = service::set_settings(&f, None, None, Some("")).unwrap();
    assert_eq!(s.default_profile, None);
    let m = Model::load(&f).unwrap();
    let ids: Vec<String> = m
        .subcatchments
        .iter()
        .map(|s| s.subcatchment_id.clone())
        .collect();
    assert_eq!(
        service::assign_profile(&f, "subcatchments", &ids, "RES").unwrap(),
        ids.len()
    );
    assert!(Model::load(&f)
        .unwrap()
        .subcatchments
        .iter()
        .all(|s| s.profile_id.as_deref() == Some("RES")));
    assert!(service::assign_profile(&f, "subcatchments", &ids, "nope").is_err());
    assert!(service::assign_profile(&f, "nodes", &ids, "RES").is_err());
}

#[test]
fn new_model_creates_loadable_file() {
    let d = tempfile::tempdir().unwrap();
    let f = d.path().join("n.gpkg");
    service::new_model(&f).unwrap();
    assert!(Model::load(&f).unwrap().nodes.is_empty());
    assert!(service::new_model(&f).is_err(), "must not overwrite");
}

#[test]
fn write_model_json_round_trips_and_cleans_up() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("m.gpkg");
    let json = r#"{
      "nodes": [
        {"node_id": "A", "geom": [0.0, 0.0], "node_type": "ac", "surface_level": 10.0, "invert_level": 8.0,
         "status": "existing", "data_source": "gis"},
        {"node_id": "B", "geom": [10.0, 0.0], "invert_level": 7.9}
      ],
      "conduits": [
        {"conduit_id": "P1", "geom": [[0.0, 0.0], [10.0, 0.0]], "us_node_id": "A", "ds_node_id": "B",
         "dn": 150, "material": "vc", "us_invert": 8.0, "ds_invert": 7.9, "sewer_type": "main"}
      ]
    }"#;
    service::write_model_json(&f, json, false).unwrap();
    let m = Model::load(&f).unwrap();
    assert_eq!(m.nodes.len(), 2);
    assert_eq!(m.nodes[0].status.as_str(), "existing");
    assert_eq!(m.nodes[1].node_type.as_str(), "ac");
    assert_eq!(m.conduits[0].material.as_str(), "vc");
    assert_eq!(m.conduits[0].geom.as_ref().unwrap().0.len(), 2);
    assert_eq!(
        service::error_count(&service::check_file(&f, false).unwrap()),
        0
    );
    // exists -> refused unless overwrite
    assert!(service::write_model_json(&f, json, false).is_err());
    service::write_model_json(&f, json, true).unwrap();
    // bad enum / unknown field: nothing created
    let g = dir.path().join("bad.gpkg");
    let e = service::write_model_json(
        &g,
        r#"{"nodes":[{"node_id":"A","node_type":"zzz"}]}"#,
        false,
    )
    .unwrap_err();
    assert!(e.to_string().contains("invalid model JSON"), "{e}");
    assert!(
        service::write_model_json(&g, r#"{"nodes":[{"node_id":"A","bogus":1}]}"#, false).is_err()
    );
    assert!(!g.exists());
    // duplicate ids fail at insert and the partial file is removed
    let dup = r#"{"nodes":[{"node_id":"A"},{"node_id":"A"}]}"#;
    assert!(service::write_model_json(&g, dup, false).is_err());
    assert!(!g.exists());
}
