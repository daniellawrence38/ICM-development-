//! Pump station + rising main tests: .inp writer and end-to-end SWMM runs.
use wwnet_core::{Conduit, Model, Node, NodeType, Pump, PumpStation, RisingMain};
use wwnet_sim::{inp::build_inp, simulate, NodeInflow, SimInput, SimOptions};

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

/// A -> WW (wet well, pumped) -> rising main -> D -> OUT.
fn station_model(profile: Option<&str>, curve: Option<&str>, rm_rough: (&str, f64)) -> Model {
    let mut ww = node("WW", 100.0, 18.0, 21.0);
    ww.node_type = NodeType::WetWell;
    let mut ps = PumpStation::new("PS1");
    ps.wet_well_node_id = Some("WW".into());
    ps.rising_main_id = Some("RM1".into());
    ps.wet_well_area_m2 = Some(7.07);
    ps.stop_level = Some(18.5);
    ps.start_level = Some(19.5);
    ps.overflow_level = Some(20.5);
    ps.pump_rate_lps = Some(20.0);
    let pump = Pump {
        pump_id: "PU1".into(),
        ps_id: "PS1".into(),
        role: "duty".into(),
        curve_json: curve.map(|s| s.to_string()),
        speed_pct: 100.0,
        notes: None,
    };
    let rm = RisingMain {
        rm_id: "RM1".into(),
        geom: None,
        ps_id: Some("PS1".into()),
        discharge_node_id: Some("D".into()),
        dn: Some(150),
        internal_diameter_mm: Some(150.0),
        material: None,
        roughness_type: rm_rough.0.into(),
        roughness: Some(rm_rough.1),
        length_m: Some(200.0),
        profile_json: profile.map(|s| s.to_string()),
        status: Default::default(),
        data_source: Default::default(),
        notes: None,
    };
    Model {
        nodes: vec![
            node("A", 0.0, 19.0, 22.0),
            ww,
            node("D", 300.0, 25.0, 28.0),
            node("OUT", 350.0, 24.5, 27.5),
        ],
        conduits: vec![
            pipe("G1", "A", "WW", 225, 100.0, 19.0, 18.0),
            pipe("G2", "D", "OUT", 225, 50.0, 25.0, 24.5),
        ],
        pump_stations: vec![ps],
        pumps: vec![pump],
        rising_mains: vec![rm],
        ..Model::default()
    }
}

const CURVE: &str = "[[0, 20], [10, 16.5], [20, 11.5], [30, 5]]";
const PROFILE: &str = "[[0, 18.0], [100, 22.0], [200, 25.0]]";

fn inflow(q: f64) -> Vec<NodeInflow> {
    vec![NodeInflow {
        node_id: "A".into(),
        base_lps: q,
        profile: None,
    }]
}

fn section<'a>(text: &'a str, name: &str) -> &'a str {
    let start = text.find(&format!("[{name}]")).expect(name);
    let rest = &text[start..];
    let end = rest[1..].find("\n[").map(|i| i + 1).unwrap_or(rest.len());
    &rest[..end]
}

fn input(m: &Model) -> SimInput<'_> {
    SimInput {
        model: m,
        inflows: inflow(8.0),
        options: SimOptions {
            duration_h: 12.0,
            warmup_h: 6.0,
            ..SimOptions::default()
        },
    }
}

#[test]
fn inp_has_pump_sections() {
    let m = station_model(Some(PROFILE), Some(CURVE), ("ks", 1.0));
    let b = build_inp(&input(&m)).unwrap();
    let t = &b.text;
    assert!(
        !b.warnings.iter().any(|w| w.contains("not modelled")),
        "{:?}",
        b.warnings
    );
    assert!(t.contains("FORCE_MAIN_EQUATION  D-W"));
    // wet well is storage with constant area, depth to the overflow level (20.5 - 18)
    let st = section(t, "STORAGE");
    assert!(
        st.contains("WW  18.0000  2.5000  0  FUNCTIONAL  0  0  7.0700  0  0"),
        "{st}"
    );
    assert!(!section(t, "JUNCTIONS").contains("WW  "));
    // DIS and a mid-profile junction with a large surcharge depth
    let j = section(t, "JUNCTIONS");
    assert!(j.contains("PS1__DIS  18.0000"), "{j}");
    assert!(
        j.contains("PS1__RM1  22.0000") && j.contains("  1000  "),
        "{j}"
    );
    // chain of two force mains
    let c = section(t, "CONDUITS");
    assert!(c.contains("RM1__1  PS1__DIS  PS1__RM1  100.000"), "{c}");
    assert!(c.contains("RM1__2  PS1__RM1  D  100.000"), "{c}");
    let x = section(t, "XSECTIONS");
    assert!(
        x.contains("RM1__1  FORCE_MAIN  0.1500  1.0000  0  0  1"),
        "{x}"
    );
    // pump: start 19.5-18 = 1.5, stop 0.5
    let p = section(t, "PUMPS");
    assert!(
        p.contains("PU1  WW  PS1__DIS  PC_PU1  OFF  1.5000  0.5000"),
        "{p}"
    );
    // curve: (head, flow) ascending head
    let cv = section(t, "CURVES");
    assert!(cv.contains("PC_PU1  PUMP3  5.0000  30.0000"), "{cv}");
    assert!(cv.contains("PC_PU1  20.0000  0.0000"), "{cv}");
    assert_eq!(b.pumps.len(), 1);
    assert_eq!(b.rising_mains[0].segments.len(), 2);
}

#[test]
fn constant_rate_and_hw_equation_and_defaults() {
    // no curve -> PUMP2 one-point curve; all C -> H-W equation
    let mut m = station_model(None, None, ("hw_c", 110.0));
    m.pump_stations[0].wet_well_area_m2 = None;
    m.pump_stations[0].start_level = None;
    m.pump_stations[0].stop_level = None;
    let b = build_inp(&input(&m)).unwrap();
    assert!(b.text.contains("FORCE_MAIN_EQUATION  H-W"));
    assert!(section(&b.text, "CURVES").contains("PC_PU1  PUMP2  0.0000  20.0000"));
    assert!(section(&b.text, "XSECTIONS").contains("RM1  FORCE_MAIN  0.1500  110.0000"));
    assert!(section(&b.text, "STORAGE").contains("7.0700"));
    assert!(section(&b.text, "PUMPS").contains("OFF  1.0000  0.3000"));
    for k in ["wet well area", "start level", "stop level"] {
        assert!(
            b.warnings.iter().any(|w| w.contains(k)),
            "{k}: {:?}",
            b.warnings
        );
    }
}

#[test]
fn mixed_roughness_converts_to_darcy_weisbach() {
    let mut m = station_model(None, Some(CURVE), ("hw_c", 120.0));
    // a second station with ks roughness
    let mut ww2 = node("WW2", 500.0, 10.0, 13.0);
    ww2.node_type = NodeType::WetWell;
    m.nodes.push(ww2);
    m.nodes.push(node("A2", 450.0, 11.0, 14.0));
    m.conduits
        .push(pipe("G3", "A2", "WW2", 225, 50.0, 11.0, 10.0));
    let mut ps2 = PumpStation::new("PS2");
    ps2.wet_well_node_id = Some("WW2".into());
    ps2.rising_main_id = Some("RM2".into());
    m.pump_stations.push(ps2);
    let mut rm2 = m.rising_mains[0].clone();
    rm2.rm_id = "RM2".into();
    rm2.ps_id = Some("PS2".into());
    rm2.roughness_type = "ks".into();
    rm2.roughness = Some(0.6);
    m.rising_mains.push(rm2);
    let mut p2 = m.pumps[0].clone();
    p2.pump_id = "PU2".into();
    p2.ps_id = "PS2".into();
    m.pumps.push(p2);
    let b = build_inp(&input(&m)).unwrap();
    assert!(b.text.contains("FORCE_MAIN_EQUATION  D-W"));
    assert!(b.warnings.iter().any(|w| w.contains("Hazen-Williams")));
    let x = section(&b.text, "XSECTIONS");
    let line = x
        .lines()
        .find(|l| l.starts_with("RM1  FORCE_MAIN"))
        .unwrap();
    let ks: f64 = line.split_whitespace().nth(3).unwrap().parse().unwrap();
    assert!(ks > 0.01 && ks < 10.0, "{line}");
}

#[test]
fn assist_and_standby_pumps() {
    let mut m = station_model(None, Some(CURVE), ("ks", 1.0));
    for (id, role) in [("PU2", "assist"), ("PU3", "standby")] {
        let mut p = m.pumps[0].clone();
        p.pump_id = id.into();
        p.role = role.into();
        m.pumps.push(p);
    }
    let b = build_inp(&input(&m)).unwrap();
    let p = section(&b.text, "PUMPS");
    assert!(
        p.contains("PU1  WW  PS1__DIS  PC_PU1  OFF  1.5000  0.5000"),
        "{p}"
    );
    // assist: start +0.2, stop +0.1
    assert!(
        p.contains("PU2  WW  PS1__DIS  PC_PU2  OFF  1.7000  0.6000"),
        "{p}"
    );
    assert!(!p.contains("PU3"));
    assert!(b.warnings.iter().any(|w| w.contains("standby")));
}

#[test]
fn station_without_rising_main_is_skipped_with_warning() {
    let mut m = station_model(None, Some(CURVE), ("ks", 1.0));
    m.rising_mains.clear();
    let b = build_inp(&input(&m)).unwrap();
    assert!(!b.text.contains("[PUMPS]"));
    assert!(b.warnings.iter().any(|w| w.contains("no rising main")));
}

fn run_and_check(m: &Model) -> wwnet_sim::SimResult {
    let r = simulate(&input(m)).unwrap();
    assert!(
        r.continuity_flow_error_pct.abs() < 5.0,
        "continuity {}",
        r.continuity_flow_error_pct
    );
    let st = &r.pump_stations[0];
    assert_eq!(st.ps_id, "PS1");
    assert_eq!(st.wet_well_node_id, "WW");
    assert!(
        st.overflow_volume_m3 == 0.0,
        "overflow {}",
        st.overflow_volume_m3
    );
    assert!(r.nodes.iter().all(|n| n.flood_volume_m3 == 0.0));
    // never above start level (19.5) plus a tolerance, and it did reach the start level
    assert!(
        st.max_level_m > 19.4 && st.max_level_m < 19.8,
        "max level {}",
        st.max_level_m
    );
    assert!((st.max_depth_m - (st.max_level_m - 18.0)).abs() < 1e-6);
    assert!(st.time_of_max_level_min > 6.0 * 60.0);
    let p = &r.pumps[0];
    assert_eq!(p.pump_id, "PU1");
    assert!(p.starts >= 5, "starts {}", p.starts);
    assert!(
        p.starts_per_hour > 1.0 && p.starts_per_hour < 15.0,
        "{}",
        p.starts_per_hour
    );
    assert!(
        p.run_time_pct > 5.0 && p.run_time_pct < 95.0,
        "{}",
        p.run_time_pct
    );
    assert!(p.max_flow_lps > 8.0, "{}", p.max_flow_lps);
    // mean pumped flow equals the inflow: run % x mean running flow ~ 8 L/s
    let rm = &r.rising_mains[0];
    assert!(rm.max_flow_lps > 8.0 && rm.max_velocity_mps > 0.5, "{rm:?}");
    assert!(r.warnings.iter().all(|w| !w.contains("not modelled")));
    r
}

#[test]
fn end_to_end_pump_curve_with_profile() {
    let m = station_model(Some(PROFILE), Some(CURVE), ("ks", 1.0));
    let r = run_and_check(&m);
    // pump flow follows the curve: at least the static-lift intersection, below shut-off
    let q = r.pumps[0].max_flow_lps;
    assert!(q > 15.0 && q < 30.0, "pump flow {q}");
    // all inflow eventually reaches the outfall conduit
    let g2 = r.links.iter().find(|l| l.conduit_id == "G2").unwrap();
    assert!(g2.max_flow_lps > 8.0);
}

#[test]
fn end_to_end_constant_rate_no_profile() {
    let m = station_model(None, None, ("ks", 1.5));
    let r = run_and_check(&m);
    assert!((r.pumps[0].max_flow_lps - 20.0).abs() < 0.5);
    // volume balance: run % x 20 L/s ~ inflow 8 L/s
    let run = r.pumps[0].run_time_pct / 100.0 * 20.0;
    assert!((run - 8.0).abs() < 1.0, "pumped {run} L/s");
}

#[test]
fn undersized_pump_overflows_wet_well() {
    // pump cannot keep up with the inflow -> wet well overflows (lost as flooding)
    let m = station_model(None, None, ("ks", 1.5));
    let mut m = m;
    m.pump_stations[0].pump_rate_lps = Some(3.0);
    let r = simulate(&input(&m)).unwrap();
    assert!(r.pump_stations[0].overflow_volume_m3 > 10.0);
    assert!(r
        .nodes
        .iter()
        .any(|n| n.node_id == "WW" && n.flood_volume_m3 > 0.0));
}
