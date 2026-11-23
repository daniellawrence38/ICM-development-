use wwnet_core::geom::{LineString, MultiPolygon, Point};
use wwnet_core::gpkg::{self, Geometry};
use wwnet_core::*;

fn p(x: f64, y: f64) -> Point {
    Point::new(x, y)
}

/// N1->N2->N3->N4->N6(outfall); branch N5->N3 with adverse grade.
fn tree() -> Model {
    let mut m = Model::default();
    let pos = [
        ("N1", 0.0, 0.0),
        ("N2", 60.0, 0.0),
        ("N3", 120.0, 0.0),
        ("N4", 180.0, 0.0),
        ("N5", 120.0, 70.0),
        ("N6", 240.0, 0.0),
    ];
    let inv = [10.0, 9.7, 9.4, 9.1, 9.9, 8.8];
    for (i, (id, x, y)) in pos.iter().enumerate() {
        let mut n = Node::new(id, *x, *y);
        n.invert_level = Some(inv[i]);
        n.surface_level = Some(inv[i] + 2.0);
        if *id == "N6" {
            n.node_type = NodeType::Outfall;
        }
        m.nodes.push(n);
    }
    let links = [
        ("C1", "N1", "N2", 150, 10.0, 9.7),
        ("C2", "N2", "N3", 150, 9.7, 9.4),
        ("C3", "N3", "N4", 225, 9.4, 9.1),
        ("C4", "N4", "N6", 225, 9.1, 8.8),
        ("C5", "N5", "N3", 150, 9.9, 9.95),
    ];
    for (id, u, d, dn, ui, di) in links {
        let mut c = Conduit::new(id, u, d, dn);
        let find = |n: &str| pos.iter().find(|x| x.0 == n).map(|x| p(x.1, x.2)).unwrap();
        c.geom = Some(LineString(vec![find(u), find(d)]));
        c.us_invert = Some(ui);
        c.ds_invert = Some(di);
        m.conduits.push(c);
    }
    m
}

fn run(m: &Model) -> Vec<Issue> {
    validate(m, &Network::build(m).unwrap())
}
fn codes(v: &[Issue]) -> Vec<&str> {
    v.iter().map(|i| i.code.as_str()).collect()
}

#[test]
fn tree_has_only_adverse_grade() {
    let issues = run(&tree());
    let errs: Vec<_> = issues
        .iter()
        .filter(|i| i.severity == Severity::Error)
        .collect();
    assert_eq!(errs.len(), 1, "{issues:?}");
    assert_eq!(errs[0].code, "ADVERSE_GRADE");
    assert_eq!(errs[0].object_id, "C5");
    // C5 ds invert 9.95 is above N3 invert 9.4: a fall into the node is normal, no issue
    assert_eq!(issues.len(), 1, "{issues:?}");
}

#[test]
fn falls_and_steps_at_nodes() {
    // fall into an ac: no issue
    let mut m = tree();
    m.conduits[0].ds_invert = Some(9.9); // N2 invert 9.7: 0.2 m fall
    m.conduits[1].us_invert = Some(9.7);
    assert!(!codes(&run(&m)).contains(&"INVERT_MISMATCH"));
    assert!(!codes(&run(&m)).contains(&"FALL_THROUGH_MS"));
    // same fall into a maintenance shaft: info
    m.nodes[1].node_type = NodeType::Ms;
    let r = run(&m);
    let i = r.iter().find(|i| i.code == "FALL_THROUGH_MS").unwrap();
    assert_eq!((i.severity, i.object_id.as_str()), (Severity::Info, "C1"));
    // small fall (<= 0.05) through a shaft: no issue
    m.conduits[0].ds_invert = Some(9.74);
    assert!(!codes(&run(&m)).contains(&"FALL_THROUGH_MS"));
    // adverse step: incoming ds invert below the node invert
    m.conduits[0].ds_invert = Some(9.5);
    let r = run(&m);
    let i = r.iter().find(|i| i.code == "INVERT_BELOW_NODE").unwrap();
    assert_eq!(i.severity, Severity::Warning);
    // outgoing us invert above the node invert: info
    let mut m = tree();
    m.conduits[1].us_invert = Some(9.8); // N2 invert 9.7
    let r = run(&m);
    let i = r.iter().find(|i| i.code == "INVERT_MISMATCH").unwrap();
    assert_eq!((i.severity, i.object_id.as_str()), (Severity::Info, "C2"));
}

#[test]
fn network_traces() {
    let m = tree();
    let n = Network::build(&m).unwrap();
    let mut up = n.upstream_trace("N4").unwrap();
    up.sort();
    assert_eq!(up, ["N1", "N2", "N3", "N5"]);
    assert_eq!(n.downstream_trace("N3").unwrap(), ["N4", "N6"]);
    assert!(n.upstream_trace("nope").is_err());
    let topo = n.topological_order().unwrap();
    let pos = |id: &str| topo.iter().position(|x| *x == id).unwrap();
    assert!(pos("N1") < pos("N3") && pos("N5") < pos("N3") && pos("N3") < pos("N6"));
    assert_eq!(n.terminal_nodes(), ["N6"]);
}

#[test]
fn pumped_edge_links_nodes() {
    let mut m = tree();
    m.nodes.push(Node::new("W1", -50.0, 0.0));
    let mut ps = PumpStation::new("PS1");
    ps.wet_well_node_id = Some("W1".into());
    ps.discharge_node_id = Some("N1".into());
    m.pump_stations.push(ps);
    let n = Network::build(&m).unwrap();
    assert_eq!(n.upstream_trace("N2").unwrap().len(), 2);
    assert!(n.upstream_trace("N1").unwrap().contains(&"W1"));
    assert!(!codes(&run(&m)).contains(&"ORPHAN_NODE"));
}

#[test]
fn structural_errors() {
    let mut m = tree();
    m.nodes.push(Node::new("N1", 1.0, 1.0)); // duplicate
    m.nodes.push(Node::new("LONELY", 500.0, 500.0)); // orphan
    m.conduits.push(Conduit::new("C9", "N6", "GHOST", 225)); // missing node, no length, no inverts
    m.subcatchments.push(Subcatchment::new("S1", "NOPE"));
    m.pump_stations.push(PumpStation::new("PS1"));
    let issues = run(&m);
    let c = codes(&issues);
    for want in [
        "DUP_NODE_ID",
        "ORPHAN_NODE",
        "CONDUIT_NODE_MISSING",
        "LENGTH_INVALID",
        "MISSING_INVERT",
        "MISSING_INVERT_SUMMARY",
        "SUBCATCHMENT_OUTLET_MISSING",
        "PS_NODE_MISSING",
    ] {
        assert!(c.contains(&want), "missing {want}: {c:?}");
    }
    let summary = issues
        .iter()
        .find(|i| i.code == "MISSING_INVERT_SUMMARY")
        .unwrap();
    assert!(summary.message.contains("design: 1"), "{}", summary.message);
}

#[test]
fn cycle_bifurcation_dn_and_geometry() {
    let mut m = tree();
    // loop N6 -> N4 creates a cycle; N3 gets second outlet; DN225 -> DN150 reduction
    let mut c = Conduit::new("C6", "N6", "N4", 225);
    c.length_m = Some(10.0);
    c.us_invert = Some(8.8);
    c.ds_invert = Some(8.7);
    m.conduits.push(c);
    let mut b = Conduit::new("C7", "N3", "N5", 100);
    b.length_m = Some(10.0);
    m.conduits.push(b);
    m.conduits[0].geom = Some(LineString(vec![p(5.0, 0.0), p(60.0, 0.0)]));
    let issues = run(&m);
    let c = codes(&issues);
    for want in [
        "NETWORK_CYCLE",
        "BIFURCATION",
        "DN_REDUCTION",
        "GEOM_ENDPOINT_MISMATCH",
    ] {
        assert!(c.contains(&want), "missing {want}: {c:?}");
    }
    let dn = issues.iter().find(|i| i.code == "DN_REDUCTION").unwrap();
    assert_eq!(dn.clause.as_deref(), Some("DS50 4.2.1(v)"));
    assert!(Network::build(&m).unwrap().topological_order().is_err());
}

#[test]
fn invert_checks() {
    let mut m = tree();
    m.conduits[0].us_invert = Some(9.5); // 0.5 below N1 invert 10.0
    m.conduits[1].ds_invert = Some(20.0); // above surface (and adverse)
    let c = run(&m);
    let c = codes(&c);
    assert!(c.contains(&"INVERT_BELOW_NODE") && c.contains(&"INVERT_ABOVE_SURFACE"));
    m.conduits[2].length_m = Some(0.0);
    assert!(codes(&run(&m)).contains(&"LENGTH_INVALID"));
}

#[test]
fn geometry_roundtrip() {
    let pt = p(392000.5, 6466000.25);
    assert_eq!(
        gpkg::decode_point(&gpkg::encode_point(&pt)).unwrap(),
        Some(pt)
    );
    let ls = LineString(vec![p(0.0, 0.0), p(3.0, 4.0), p(3.0, 10.0)]);
    assert!((ls.length() - 11.0).abs() < 1e-12);
    assert_eq!(
        gpkg::decode_linestring(&gpkg::encode_linestring(&ls)).unwrap(),
        Some(ls)
    );
    let outer = vec![
        p(0.0, 0.0),
        p(10.0, 0.0),
        p(10.0, 10.0),
        p(0.0, 10.0),
        p(0.0, 0.0),
    ];
    let hole = vec![
        p(2.0, 2.0),
        p(4.0, 2.0),
        p(4.0, 4.0),
        p(2.0, 4.0),
        p(2.0, 2.0),
    ];
    let mp = MultiPolygon(vec![
        vec![outer, hole],
        vec![vec![p(20.0, 0.0), p(21.0, 0.0), p(21.0, 1.0), p(20.0, 0.0)]],
    ]);
    assert!((mp.area() - (100.0 - 4.0 + 0.5)).abs() < 1e-12);
    assert_eq!(
        gpkg::decode_multipolygon(&gpkg::encode_multipolygon(&mp)).unwrap(),
        Some(mp)
    );
}

#[test]
fn decode_big_endian_z_m_and_iso() {
    // GP header big-endian (flags 0, no envelope), WKB big-endian PointZ ISO 1001
    let mut b = vec![b'G', b'P', 0, 0, 0, 0, 0x1E, 0xAA];
    b.push(0);
    b.extend_from_slice(&1001u32.to_be_bytes());
    for v in [1.5f64, 2.5, 99.0] {
        b.extend_from_slice(&v.to_be_bytes());
    }
    assert_eq!(
        gpkg::decode(&b).unwrap(),
        Some(Geometry::Point(p(1.5, 2.5)))
    );
    // little-endian LineStringZM (3002) with envelope type 1
    let mut b = vec![b'G', b'P', 0, 0b11, 0xAA, 0x1E, 0, 0];
    b.extend_from_slice(&[0u8; 32]);
    b.push(1);
    b.extend_from_slice(&3002u32.to_le_bytes());
    b.extend_from_slice(&2u32.to_le_bytes());
    for v in [1.0f64, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0] {
        b.extend_from_slice(&v.to_le_bytes());
    }
    assert_eq!(
        gpkg::decode(&b).unwrap(),
        Some(Geometry::LineString(LineString(vec![
            p(1.0, 2.0),
            p(5.0, 6.0)
        ])))
    );
    // EWKB-flag MultiPolygon-less: PointM via flag
    let mut b = vec![b'G', b'P', 0, 1, 0xAA, 0x1E, 0, 0, 1];
    b.extend_from_slice(&(1u32 | 0x4000_0000).to_le_bytes());
    for v in [7.0f64, 8.0, 9.0] {
        b.extend_from_slice(&v.to_le_bytes());
    }
    assert_eq!(
        gpkg::decode(&b).unwrap(),
        Some(Geometry::Point(p(7.0, 8.0)))
    );
    assert!(gpkg::decode(b"XX").is_err());
}

#[test]
fn enum_parsing() {
    assert_eq!("drop_ac".parse::<NodeType>().unwrap(), NodeType::DropAc);
    let e = "bogus".parse::<Material>().unwrap_err();
    assert!(e.to_string().contains("bogus"));
    for t in NodeType::ALL {
        assert_eq!(t.as_str().parse::<NodeType>().unwrap(), *t);
    }
}

#[test]
fn gpkg_roundtrip_and_issues() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.gpkg");
    gpkg::create(&path).unwrap();
    assert!(gpkg::create(&path).is_err());
    let mut m = tree();
    let mut s = Subcatchment::new("S1", "N1");
    s.geom = Some(MultiPolygon(vec![vec![vec![
        p(0.0, -50.0),
        p(50.0, -50.0),
        p(50.0, 0.0),
        p(0.0, -50.0),
    ]]]));
    s.rcode = "R30".into();
    m.subcatchments.push(s);
    let mut ps = PumpStation::new("PS1");
    ps.geom = Some(p(-10.0, 0.0));
    ps.pump_rate_lps = Some(12.5);
    m.pump_stations.push(ps);
    m.pumps.push(Pump {
        pump_id: "P1".into(),
        ps_id: "PS1".into(),
        role: "duty".into(),
        curve_json: Some("[[0,20],[10,15]]".into()),
        speed_pct: 100.0,
        notes: None,
    });
    m.rising_mains.push(RisingMain {
        rm_id: "RM1".into(),
        geom: Some(LineString(vec![p(0.0, 0.0), p(1.0, 1.0)])),
        ps_id: Some("PS1".into()),
        discharge_node_id: None,
        dn: Some(100),
        internal_diameter_mm: None,
        material: None,
        roughness_type: "ks".into(),
        roughness: Some(0.6),
        length_m: None,
        profile_json: None,
        status: Status::Proposed,
        data_source: DataSource::Design,
        notes: None,
    });
    m.write(&path).unwrap();
    let back = Model::load(&path).unwrap();
    assert_eq!(back, m);

    let issues = run(&back);
    Model::store_issues(&path, "check", &issues).unwrap();
    assert_eq!(Model::read_issue_count(&path).unwrap(), issues.len() as i64);
    // another source is kept; re-storing a source replaces only its own rows (no accumulation)
    let extra = Issue::new(
        Severity::Warning,
        "X",
        "conduit",
        "C1",
        None,
        "design issue",
    );
    Model::store_issues(&path, "design", std::slice::from_ref(&extra)).unwrap();
    Model::store_issues(&path, "design", std::slice::from_ref(&extra)).unwrap();
    assert_eq!(
        Model::read_issue_count(&path).unwrap(),
        issues.len() as i64 + 1
    );
    Model::store_issues(&path, "check", &[]).unwrap();
    assert_eq!(Model::read_issue_count(&path).unwrap(), 1);
    assert!(Model::store_issues(&path, "nonsense", &[]).is_err());
    Model::save_results(
        &path,
        &[gpkg::ConduitResult {
            conduit_id: "C1".into(),
            result: "pass".into(),
            ..Default::default()
        }],
    )
    .unwrap();
    {
        let c = gpkg::open(&path).unwrap();
        // results and issues carry geometry: the conduit line and its midpoint
        let (has_line, has_pt): (bool, bool) = c
            .query_row(
                "SELECT (SELECT geom IS NOT NULL FROM wwn_results_conduits WHERE conduit_id='C1'),
                        (SELECT geom IS NOT NULL FROM wwn_issues WHERE object_id='C1')",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(has_line && has_pt);
    }

    let c = gpkg::open(&path).unwrap();
    let (minx, maxx): (f64, f64) = c
        .query_row(
            "SELECT min_x,max_x FROM gpkg_contents WHERE table_name='wwn_nodes'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!((minx, maxx), (0.0, 240.0));
}
