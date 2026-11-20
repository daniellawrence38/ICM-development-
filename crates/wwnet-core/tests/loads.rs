use rusqlite::Connection;
use wwnet_core::gpkg;
use wwnet_core::profile::parse_profile;
use wwnet_core::*;

fn model() -> Model {
    let mut m = Model::default();
    m.nodes.push(Node::new("A", 0.0, 0.0));
    m.nodes.push(Node::new("B", 10.0, 0.0));
    m.node_loads.push(NodeLoad {
        load_id: "CP-A".into(),
        node_id: "A".into(),
        connections: Some(10),
        adwf_lps: None,
        profile_id: None,
        source: Some("connection_points".into()),
        notes: None,
    });
    let mut s = Subcatchment::new("S1", "B");
    s.adwf_lps = Some(1.25);
    s.profile_id = Some("P".into());
    m.subcatchments.push(s);
    m
}

#[test]
fn v2_roundtrip_and_meta_defaults() {
    let d = tempfile::tempdir().unwrap();
    let f = d.path().join("m.gpkg");
    gpkg::create(&f).unwrap();
    let mut m = model();
    m.profiles.push(Profile {
        profile_id: "P".into(),
        description: Some("d".into()),
        interval_min: Some(720),
        mean_factor: Some(1.0),
        points: vec![
            ProfilePoint {
                minute_of_day: 0,
                factor: 0.5,
            },
            ProfilePoint {
                minute_of_day: 720,
                factor: 1.5,
            },
        ],
    });
    m.write(&f).unwrap();
    let l = Model::load(&f).unwrap();
    assert_eq!(l, m);
    assert_eq!(l.load_settings.adwf_per_connection_lpd, 630.0);
    assert_eq!(l.load_settings.peaking_factor, 1.5);
    assert_eq!(l.load_settings.default_profile, None);
    let c = Connection::open(&f).unwrap();
    let v: String = c
        .query_row(
            "SELECT value FROM wwn_meta WHERE key='schema_version'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(v, "2");
}

/// Build a v1-shaped file by dropping the v2 tables / columns.
fn make_v1(f: &std::path::Path) {
    gpkg::create(f).unwrap();
    model().write(f).unwrap();
    let c = Connection::open(f).unwrap();
    c.execute_batch(
        "DROP TABLE wwn_node_loads; DROP TABLE wwn_profiles; DROP TABLE wwn_profile_info;
         ALTER TABLE wwn_subcatchments DROP COLUMN adwf_lps; ALTER TABLE wwn_subcatchments DROP COLUMN profile_id;
         DELETE FROM wwn_meta WHERE key IN ('adwf_per_connection_lpd','peaking_factor');
         UPDATE wwn_meta SET value='1' WHERE key='schema_version';",
    )
    .unwrap();
}

#[test]
fn v1_file_loads_and_upgrades() {
    let d = tempfile::tempdir().unwrap();
    let f = d.path().join("v1.gpkg");
    make_v1(&f);
    let m = Model::load(&f).unwrap();
    assert!(m.node_loads.is_empty() && m.profiles.is_empty());
    assert_eq!(m.subcatchments[0].adwf_lps, None);
    assert_eq!(m.load_settings, LoadSettings::default());
    gpkg::upgrade(&f).unwrap();
    gpkg::upgrade(&f).unwrap(); // idempotent
    let c = Connection::open(&f).unwrap();
    for t in ["wwn_node_loads", "wwn_profiles", "wwn_profile_info"] {
        let n: i64 = c
            .query_row(&format!("SELECT count(*) FROM {t}"), [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0);
    }
    let v: String = c
        .query_row(
            "SELECT value FROM wwn_meta WHERE key='schema_version'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(v, "2");
    drop(c);
    let m = Model::load(&f).unwrap();
    assert_eq!(m.subcatchments.len(), 1);
}

#[test]
fn parse_profile_cases() {
    let mut csv = String::from("Time,Factor\n");
    for i in 0..288 {
        csv.push_str(&format!(
            "{:02}:{:02},{}\n",
            i * 5 / 60,
            i * 5 % 60,
            if i < 144 { 0.5 } else { 1.5 }
        ));
    }
    let (p, info) = parse_profile("X", &csv, "", false).unwrap();
    assert_eq!(p.points.len(), 288);
    assert_eq!(info.interval_min, Some(5));
    assert!((info.mean_factor - 1.0).abs() < 1e-9 && info.warnings.is_empty());
    // tab-separated, minutes, no header, mean far from 1
    let (_, info) = parse_profile("X", "0\t2\n720\t4\n", "", false).unwrap();
    assert!(info.warnings.iter().any(|w| w.contains("mean factor")));
    let (p, info) = parse_profile("X", "0\t2\n720\t4\n", "", true).unwrap();
    assert!(info.normalised && (info.mean_factor - 1.0).abs() < 1e-9);
    assert!((p.points[0].factor - 2.0 / 3.0).abs() < 1e-9);
    assert!(parse_profile("X", "00:00,1\n00:00,1\n", "", false).is_err());
    assert!(parse_profile("X", "00:00,1\n24:00,1\n", "", false).is_err());
    assert!(parse_profile("X", "00:00,1\nfoo,1\n", "", false).is_err());
}
