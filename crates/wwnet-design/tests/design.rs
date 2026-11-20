use approx::assert_relative_eq;
use wwnet_core::{Conduit, Model, Node, NodeType, PumpStation, Subcatchment};
use wwnet_design::{run_design, to_conduit_results, DesignOutput};
use wwnet_rules::{CheckResult, RuleSet};

fn node(id: &str, x: f64, inv: f64) -> Node {
    let mut n = Node::new(id, x, 0.0);
    n.invert_level = Some(inv);
    n.surface_level = Some(inv + 2.5);
    n
}

fn conduit(id: &str, us: &str, ds: &str, dn: i64, us_inv: f64, ds_inv: f64) -> Conduit {
    let mut c = Conduit::new(id, us, ds, dn);
    c.length_m = Some(60.0);
    c.us_invert = Some(us_inv);
    c.ds_invert = Some(ds_inv);
    c
}

fn sub(id: &str, outlet: &str, ha: f64) -> Subcatchment {
    let mut s = Subcatchment::new(id, outlet);
    s.net_area_ha = Some(ha);
    s
}

/// 60 m at 1:200 falls 0.3 m.
fn single(ha: f64) -> Model {
    let mut m = Model::default();
    m.nodes.push(node("A", 0.0, 10.0));
    let mut o = node("OUT", 60.0, 9.7);
    o.node_type = NodeType::Outfall;
    m.nodes.push(o);
    m.conduits.push(conduit("C1", "A", "OUT", 150, 10.0, 9.7));
    m.subcatchments.push(sub("S1", "A", ha));
    m
}

fn conduit_of<'a>(o: &'a DesignOutput, id: &str) -> &'a wwnet_design::ConduitDesign {
    o.conduit_results
        .iter()
        .find(|c| c.conduit_id == id)
        .unwrap()
}

#[test]
fn hand_calc_pass() {
    let o = run_design(&single(10.0), &RuleSet::ds50()).unwrap();
    let c = conduit_of(&o, "C1");
    assert_relative_eq!(c.gsdf_lps, 2.19, epsilon = 1e-9);
    assert_relative_eq!(c.grade_1in.unwrap(), 200.0, epsilon = 1e-6);
    assert_eq!(c.result, CheckResult::Pass, "{:#?}", c.checks);
    assert!(c.d_over_d.unwrap() < 0.5);
    assert!(c.capacity_lps.unwrap() > 2.19);
    let a = o.node_flows.iter().find(|n| n.node_id == "A").unwrap();
    assert_eq!(a.design_data, vec!["10-70/180-0.22".to_string()]);
}

#[test]
fn table_44_flow_fail() {
    let o = run_design(&single(30.0), &RuleSet::ds50()).unwrap();
    let c = conduit_of(&o, "C1");
    assert_relative_eq!(c.gsdf_lps, 6.57, epsilon = 1e-9);
    assert_eq!(c.result, CheckResult::Fail);
    let f = c.checks.iter().find(|k| k.rule == "max_flow").unwrap();
    assert_eq!(f.result, CheckResult::Fail);
    assert_eq!(f.limit, 6.0);
    assert!(o.issues.iter().any(|i| i.code == "design_max_flow"));
}

#[test]
fn two_branches_accumulate() {
    let mut m = Model::default();
    for (id, x, inv) in [("A", 0.0, 10.0), ("B", 0.0, 10.0), ("C", 60.0, 9.7)] {
        m.nodes.push(node(id, x, inv));
    }
    let mut o = node("OUT", 120.0, 9.4);
    o.node_type = NodeType::Outfall;
    m.nodes.push(o);
    m.conduits.push(conduit("C1", "A", "C", 150, 10.0, 9.7));
    m.conduits.push(conduit("C2", "B", "C", 150, 10.0, 9.7));
    m.conduits.push(conduit("C3", "C", "OUT", 225, 9.6, 9.3));
    m.subcatchments.push(sub("S1", "A", 4.0));
    m.subcatchments.push(sub("S2", "B", 6.0));
    m.subcatchments.push(sub("S3", "C", 5.0));
    let o = run_design(&m, &RuleSet::ds50()).unwrap();
    assert_relative_eq!(conduit_of(&o, "C1").gsdf_lps, 0.876, epsilon = 1e-9);
    assert_relative_eq!(conduit_of(&o, "C2").gsdf_lps, 1.314, epsilon = 1e-9);
    assert_relative_eq!(conduit_of(&o, "C3").gsdf_lps, 15.0 * 0.219, epsilon = 1e-9);
    assert_relative_eq!(o.summary.terminal_gsdf_lps, 15.0 * 0.219, epsilon = 1e-9);
}

#[test]
fn pump_station_pumped_inflow() {
    let mut m = Model::default();
    m.nodes.push(node("U", 0.0, 10.0));
    let mut w = node("W", 60.0, 9.7);
    w.node_type = NodeType::WetWell;
    m.nodes.push(w);
    m.nodes.push(node("D", 200.0, 10.0));
    let mut o = node("OUT", 260.0, 9.7);
    o.node_type = NodeType::Outfall;
    m.nodes.push(o);
    m.conduits.push(conduit("C1", "U", "W", 150, 10.0, 9.7));
    m.conduits.push(conduit("C2", "D", "OUT", 150, 10.0, 9.7));
    let mut ps = PumpStation::new("PS1");
    ps.wet_well_node_id = Some("W".into());
    ps.discharge_node_id = Some("D".into());
    ps.pump_rate_lps = Some(5.0);
    m.pump_stations.push(ps);
    m.subcatchments.push(sub("S1", "U", 10.0));
    m.subcatchments.push(sub("S2", "D", 2.0));
    let o = run_design(&m, &RuleSet::ds50()).unwrap();
    let c2 = conduit_of(&o, "C2");
    assert_relative_eq!(c2.pumped_lps, 5.0);
    assert_relative_eq!(c2.gsdf_lps, 0.438, epsilon = 1e-9);
    assert_eq!(c2.result, CheckResult::Fail);
    let k = c2.checks.iter().find(|k| k.rule == "min_dn").unwrap();
    assert_eq!(k.result, CheckResult::Fail);
    assert!(k.clause.contains("4.2.1(l)"));
    // Gravity flow stops at the wet well.
    assert_relative_eq!(conduit_of(&o, "C1").gsdf_lps, 2.19, epsilon = 1e-9);

    // Missing pump rate: accumulated GSDF used, with a warning.
    m.pump_stations[0].pump_rate_lps = None;
    let o = run_design(&m, &RuleSet::ds50()).unwrap();
    assert_relative_eq!(conduit_of(&o, "C2").pumped_lps, 2.19, epsilon = 1e-9);
    assert!(o
        .issues
        .iter()
        .any(|i| i.code == "design_pump_rate_missing"));
}

#[test]
fn missing_levels_are_skipped_with_warnings() {
    let mut m = single(10.0);
    m.conduits[0].us_invert = None;
    m.nodes[0].invert_level = None;
    m.nodes[0].surface_level = None;
    m.nodes[1].surface_level = None;
    let o = run_design(&m, &RuleSet::ds50()).unwrap();
    let c = conduit_of(&o, "C1");
    assert!(c.grade_1in.is_none());
    assert_eq!(c.result, CheckResult::Warn);
    assert!(c.checks.iter().all(|k| k.rule != "min_cover"));
    assert!(o.issues.iter().any(|i| i.code == "design_missing_data"));
}

#[test]
fn demo_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("demo.gpkg");
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/demo/demo.gpkg");
    std::fs::copy(src, &f).unwrap();
    let model = Model::load(&f).unwrap();
    let rules = RuleSet::ds50();
    let o = run_design(&model, &rules).unwrap();
    assert_eq!(o.conduit_results.len(), model.conduits.len());
    let sum: f64 = model
        .subcatchments
        .iter()
        .map(|s| {
            let r = rules
                .residential_rate(
                    wwnet_rules::Region::South,
                    wwnet_rules::Ground::Dry,
                    &s.rcode,
                )
                .unwrap();
            r.gsdf_lps_per_ha * s.net_area_ha.unwrap()
        })
        .sum();
    assert!(sum > 0.0);
    let out = o.node_flows.iter().find(|n| n.node_id == "OUT1").unwrap();
    assert_relative_eq!(out.accumulated_gsdf_lps, sum, epsilon = 1e-9);
    assert_relative_eq!(o.summary.total_local_gsdf_lps, sum, epsilon = 1e-9);
    assert_relative_eq!(o.summary.terminal_gsdf_lps, sum, epsilon = 1e-9);

    Model::save_results(&f, &to_conduit_results(&o, &rules).unwrap()).unwrap();
    let before = Model::read_issue_count(&f).unwrap();
    Model::append_issues(&f, &o.issues).unwrap();
    let n = Model::read_issue_count(&f).unwrap();
    assert_eq!((n - before) as usize, o.issues.len());
}

fn graded(dn: i64, grade: f64) -> Model {
    let mut m = Model::default();
    let fall = 60.0 / grade;
    m.nodes.push(node("A", 0.0, 10.0));
    let mut o = node("OUT", 60.0, 10.0 - fall);
    o.node_type = NodeType::Outfall;
    m.nodes.push(o);
    m.conduits
        .push(conduit("C1", "A", "OUT", dn, 10.0, 10.0 - fall));
    m
}

#[test]
fn capacity_is_governed_by_table_44() {
    let rs = RuleSet::ds50();
    let o = run_design(&graded(150, 29.0), &rs).unwrap();
    let c = conduit_of(&o, "C1");
    assert_relative_eq!(c.capacity_lps.unwrap(), 8.0, epsilon = 1e-9);
    assert_relative_eq!(c.table44_lps.unwrap(), 8.0, epsilon = 1e-9);
    assert_relative_eq!(c.hydraulic_capacity_lps.unwrap(), 15.4, epsilon = 0.3);

    let o = run_design(&graded(150, 150.0), &rs).unwrap();
    let c = conduit_of(&o, "C1");
    assert_relative_eq!(c.table44_lps.unwrap(), 6.0, epsilon = 1e-9);
    assert_relative_eq!(
        c.capacity_lps.unwrap(),
        6.0f64.min(c.hydraulic_capacity_lps.unwrap()),
        epsilon = 1e-9
    );

    // DN225 at 1:250 is in the 201-300 band (22 L/s).
    let o = run_design(&graded(225, 250.0), &rs).unwrap();
    let c = conduit_of(&o, "C1");
    assert_relative_eq!(c.table44_lps.unwrap(), 22.0, epsilon = 1e-9);
    assert!(c.capacity_lps.unwrap() <= 22.0);

    // Flatter than the last band: no Table 4.4 value, hydraulic capacity governs.
    let o = run_design(&graded(150, 300.0), &rs).unwrap();
    let c = conduit_of(&o, "C1");
    assert!(c.table44_lps.is_none());
    assert_eq!(c.capacity_lps, c.hydraulic_capacity_lps);

    // Stored messages carry the extra capacities.
    let rows = to_conduit_results(&run_design(&graded(150, 29.0), &rs).unwrap(), &rs).unwrap();
    let v: serde_json::Value = serde_json::from_str(rows[0].messages.as_ref().unwrap()).unwrap();
    assert_eq!(v["table44_lps"], 8.0);
    assert!(v["hydraulic_capacity_lps"].as_f64().unwrap() > 15.0);
    assert!(v["checks"].is_array());
}

#[test]
fn missing_surface_level_is_info_not_warn() {
    let mut m = graded(150, 200.0);
    m.nodes[0].surface_level = None;
    m.nodes[1].surface_level = None;
    let o = run_design(&m, &RuleSet::ds50()).unwrap();
    let c = conduit_of(&o, "C1");
    assert_eq!(c.result, CheckResult::Pass, "{:#?}", c.checks);
    let infos: Vec<_> = c
        .checks
        .iter()
        .filter(|k| k.result == CheckResult::Info)
        .collect();
    assert_eq!(infos.len(), 2);
    assert!(infos[0]
        .message
        .contains("cover not checked (no surface level at A)"));
    assert_eq!(o.summary.cover_not_checked_ends, 2);
    assert_eq!(o.summary.warn, 0);
    assert!(o.issues.iter().all(|i| i.code != "design_missing_data"));
}
