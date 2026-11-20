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
    assert!(j.contains("downstream") && !j.contains("upstream"));
    let e = service::trace_file(&f, "ZZ", true, true).unwrap_err();
    assert!(matches!(e, wwnet_core::Error::NodeNotFound(_)));
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
