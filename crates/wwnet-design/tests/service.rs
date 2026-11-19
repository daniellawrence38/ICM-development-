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
