use std::process::Command;

#[test]
fn simulate_smoke() {
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/demo/demo.gpkg");
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("demo.gpkg");
    std::fs::copy(src, &f).unwrap();
    let inp = dir.path().join("out.inp");
    let out = Command::new(env!("CARGO_BIN_EXE_wwnet"))
        .args(["simulate", f.to_str().unwrap(), "--duration-h", "12"])
        .args(["--warmup-h", "6", "--write-inp", inp.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        text.contains("CONDUIT") && text.contains("continuity error"),
        "{text}"
    );
    assert!(std::fs::read_to_string(&inp)
        .unwrap()
        .contains("[CONDUITS]"));

    let out = Command::new(env!("CARGO_BIN_EXE_wwnet"))
        .args(["simulate", f.to_str().unwrap(), "--duration-h", "12"])
        .args(["--warmup-h", "6", "--json", "--roughness", "ks"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert!(v["summary"]["conduits"].as_u64().unwrap() > 0);
}
