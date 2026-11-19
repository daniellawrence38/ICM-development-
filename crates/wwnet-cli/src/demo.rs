//! Synthetic demo catchment near MGA50 (392000, 6466000).
use wwnet_core::geom::{LineString, MultiPolygon, Point};
use wwnet_core::*;

const DN150_GRADE: f64 = 100.0; // 1 in N
const DN225_GRADE: f64 = 200.0;

fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> MultiPolygon {
    let p = |x, y| Point::new(x, y);
    MultiPolygon(vec![vec![vec![
        p(x0, y0),
        p(x1, y0),
        p(x1, y1),
        p(x0, y1),
        p(x0, y0),
    ]]])
}

pub fn model() -> Model {
    let mut m = Model::default();
    let (x0, y0) = (392000.0, 6466000.0);
    // (id, x, y)
    let mut pos: Vec<(String, f64, f64)> = (1..=8)
        .map(|i| (format!("N{i:02}"), x0 + 70.0 * (i - 1) as f64, y0))
        .collect();
    pos.push(("OUT1".into(), x0 + 70.0 * 8.0, y0));
    pos.push(("B1".into(), x0 + 140.0, y0 + 240.0));
    pos.push(("B2".into(), x0 + 140.0, y0 + 170.0));
    pos.push(("B3".into(), x0 + 140.0, y0 + 90.0));
    let at = |id: &str| {
        pos.iter()
            .find(|p| p.0 == id)
            .map(|p| Point::new(p.1, p.2))
            .unwrap()
    };

    // (id, us, ds, dn)
    let links = [
        ("P01", "N01", "N02", 150),
        ("P02", "N02", "N03", 150),
        ("P03", "N03", "N04", 225),
        ("P04", "N04", "N05", 225),
        ("P05", "N05", "N06", 225),
        ("P06", "N06", "N07", 225),
        ("P07", "N07", "N08", 225),
        ("P08", "N08", "OUT1", 225),
        ("P09", "B1", "B2", 150),
        ("P10", "B2", "B3", 150),
        ("P11", "B3", "N03", 150),
    ];
    let len = |u: &str, d: &str| at(u).distance(&at(d));
    let grade = |dn: i64| if dn == 150 { DN150_GRADE } else { DN225_GRADE };

    // Spine inverts forward from N01; the branch is solved backwards from N03.
    let mut inv: std::collections::HashMap<&str, f64> = Default::default();
    let mut ds_inv: std::collections::HashMap<&str, f64> = Default::default();
    let mut us_inv: std::collections::HashMap<&str, f64> = Default::default();
    let mut cur = 26.0;
    for (id, u, d, dn) in &links[..8] {
        // deliberate 0.3 m drop through N06 (drop_ac)
        if *u == "N06" {
            cur -= 0.3;
        }
        us_inv.insert(id, cur);
        if *u == "N06" {
            inv.insert(u, cur); // node invert is the (lower) outlet invert
        } else {
            inv.entry(u).or_insert(cur);
        }
        cur -= len(u, d) / grade(*dn);
        ds_inv.insert(id, cur);
        inv.entry(d).or_insert(cur);
    }
    let mut cur = inv["N03"];
    for (id, u, d, dn) in links[8..].iter().rev() {
        ds_inv.insert(id, cur);
        cur += len(u, d) / grade(*dn);
        us_inv.insert(id, cur);
        inv.insert(u, cur);
        let _ = d;
    }

    for (i, (id, x, y)) in pos.iter().enumerate() {
        let mut n = Node::new(id, *x, *y);
        n.invert_level = Some(inv[id.as_str()]);
        n.surface_level = Some(inv[id.as_str()] + 2.0 + 0.05 * (i % 3) as f64);
        n.data_source = DataSource::Design;
        n.node_type = match id.as_str() {
            "OUT1" => NodeType::Outfall,
            "N06" => NodeType::DropAc,
            _ => NodeType::Ac,
        };
        if n.node_type != NodeType::Outfall {
            n.chamber_diameter_mm = Some(1050);
        }
        m.nodes.push(n);
    }
    for (id, u, d, dn) in links {
        let mut c = Conduit::new(id, u, d, dn);
        c.geom = Some(LineString(vec![at(u), at(d)]));
        c.length_m = Some((len(u, d) * 100.0).round() / 100.0);
        c.us_invert = Some((us_inv[id] * 1000.0).round() / 1000.0);
        c.ds_invert = Some((ds_inv[id] * 1000.0).round() / 1000.0);
        m.conduits.push(c);
    }
    // Round node inverts the same way so they agree with conduit inverts.
    for n in &mut m.nodes {
        n.invert_level = n.invert_level.map(|v| (v * 1000.0).round() / 1000.0);
        n.surface_level = n.surface_level.map(|v| (v * 1000.0).round() / 1000.0);
    }

    let subs = [
        (
            "S1",
            "B1",
            "R20",
            rect(x0 + 10.0, y0 + 170.0, x0 + 130.0, y0 + 290.0),
        ),
        (
            "S2",
            "N02",
            "R20",
            rect(x0 - 10.0, y0 - 130.0, x0 + 110.0, y0 - 10.0),
        ),
        (
            "S3",
            "B2",
            "R30",
            rect(x0 + 150.0, y0 + 100.0, x0 + 270.0, y0 + 220.0),
        ),
        (
            "S4",
            "N05",
            "R30",
            rect(x0 + 260.0, y0 - 130.0, x0 + 400.0, y0 - 10.0),
        ),
    ];
    for (id, outlet, rcode, g) in subs {
        let mut s = Subcatchment::new(id, outlet);
        let gross = g.area() / 10_000.0;
        s.gross_area_ha = Some((gross * 100.0).round() / 100.0);
        s.net_area_ha = Some((gross * 0.85 * 100.0).round() / 100.0);
        s.rcode = rcode.into();
        s.geom = Some(g);
        m.subcatchments.push(s);
    }
    m
}
