//! `cargo run -p wwnet-sim --example run_demo [path/to/model.gpkg]`
//! Applies a constant 0.01 L/s inflow to every node and prints a result table.
use wwnet_core::Model;
use wwnet_sim::{simulate_gpkg_free, NodeInflow, SimInput, SimOptions};

fn main() {
    let path = std::env::args().nth(1).unwrap_or_else(|| {
        concat!(env!("CARGO_MANIFEST_DIR"), "/../../examples/demo/demo.gpkg").into()
    });
    let model = Model::load(&path).expect("load gpkg");
    let inflows = model
        .nodes
        .iter()
        .map(|n| NodeInflow {
            node_id: n.node_id.clone(),
            base_lps: 0.01,
            profile: None,
        })
        .collect();
    let input = SimInput {
        model: &model,
        inflows,
        options: SimOptions {
            duration_h: 6.0,
            warmup_h: 2.0,
            ..SimOptions::default()
        },
    };
    let r = match simulate_gpkg_free(&input) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("simulation failed: {e}");
            std::process::exit(1);
        }
    };
    println!(
        "SWMM {} | flow continuity error {:.3} %",
        r.swmm_version, r.continuity_flow_error_pct
    );
    println!(
        "{:<14} {:>9} {:>7} {:>8} {:>7}",
        "conduit", "Qmax L/s", "d/D", "v m/s", "surch h"
    );
    for l in &r.links {
        println!(
            "{:<14} {:>9.3} {:>7.3} {:>8.3} {:>7.2}",
            l.conduit_id, l.max_flow_lps, l.max_d_over_d, l.max_velocity_mps, l.surcharged_h
        );
    }
    println!(
        "{:<14} {:>9} {:>9} {:>9}",
        "node", "HGL m", "flood m3", "surch h"
    );
    for n in &r.nodes {
        println!(
            "{:<14} {:>9.3} {:>9.3} {:>9.2}",
            n.node_id, n.max_hgl, n.flood_volume_m3, n.surcharged_h
        );
    }
    for w in &r.warnings {
        println!("warning: {w}");
    }
}
