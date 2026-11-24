mod demo;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use wwnet_core::{gpkg, Severity};
use wwnet_design::service;
use wwnet_sim::service::{simulate_file, simulate_json, ServiceOptions};

#[derive(Parser)]
#[command(name = "wwnet", version, about = "Wastewater network modelling tool")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Create an empty model GeoPackage
    New { file: PathBuf },
    /// Validate a model; writes issues to wwn_issues. Exit code 1 if any error.
    Check {
        file: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Trace upstream and/or downstream of a node or a conduit, with the upstream loading
    Trace {
        file: PathBuf,
        /// Node id or conduit id
        node_id: String,
        #[arg(long)]
        up: bool,
        #[arg(long)]
        down: bool,
    },
    /// Run DS 50 design checks; writes wwn_results_conduits and appends to wwn_issues.
    /// Exit code 1 if any conduit fails.
    Design {
        file: PathBuf,
        /// Print the full design output as JSON
        #[arg(long)]
        json: bool,
        /// Rule set TOML (default: embedded DS 50 Ver 2 Rev 4)
        #[arg(long)]
        ruleset: Option<PathBuf>,
    },
    /// Run the SWMM dynamic-wave simulation; writes wwn_sim_* tables.
    /// Exit code 0 unless the engine reports an error.
    Simulate {
        file: PathBuf,
        /// Total simulated hours including warm-up
        #[arg(long, default_value_t = 48.0)]
        duration_h: f64,
        /// Warm-up hours excluded from the results
        #[arg(long, default_value_t = 24.0)]
        warmup_h: f64,
        /// ks (equivalent n per conduit from the model's Colebrook ks, as the design) or manning (one n)
        #[arg(long, default_value = "ks", value_parser = ["manning", "ks"])]
        roughness: String,
        /// Manning n (with --roughness manning)
        #[arg(long = "n", default_value_t = 0.013)]
        n: f64,
        /// Multiplier on all inflows (e.g. 1.5 to test the peak)
        #[arg(long, default_value_t = 1.0)]
        flow_scale: f64,
        /// Print the full result as JSON
        #[arg(long)]
        json: bool,
        /// Also write the generated SWMM input file here
        #[arg(long)]
        write_inp: Option<PathBuf>,
        /// Run label stored with the results (e.g. "existing x1.0")
        #[arg(long, default_value = "")]
        label: String,
    },
    /// List the stored simulation runs, newest first
    Runs {
        file: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Compare two stored simulation runs (id, label, latest or previous); writes wwn_sim_compare
    Compare {
        file: PathBuf,
        run_a: String,
        run_b: String,
        #[arg(long)]
        json: bool,
    },
    /// Diurnal profile tools
    Profile {
        #[command(subcommand)]
        cmd: ProfileCmd,
    },
    /// Write a synthetic demo catchment
    #[command(hide = true)]
    Demo {
        file: PathBuf,
        /// Add connection-point node loads and the example diurnal profile (flow-modelling demo)
        #[arg(long)]
        flows: bool,
    },
}

#[derive(Subcommand)]
enum ProfileCmd {
    /// Import a diurnal profile from a CSV / tab-separated file (time as hh:mm or minutes, factor).
    /// Upgrades the model to schema v2 if needed; the profile becomes the default unless --no-default.
    Import {
        file: PathBuf,
        profile_id: String,
        csv: PathBuf,
        #[arg(long, default_value = "")]
        description: String,
        /// Do not set the profile as the model's default profile
        #[arg(long)]
        no_default: bool,
        /// Divide the factors by their mean so the mean is exactly 1
        #[arg(long)]
        normalise: bool,
    },
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("error: {e:#}");
            std::process::exit(2);
        }
    }
}

fn run() -> Result<i32> {
    match Cli::parse().cmd {
        Cmd::New { file } => {
            gpkg::create(&file)?;
            println!("created {}", file.display());
        }
        Cmd::Demo { file, flows } => {
            gpkg::create(&file)?;
            let m = if flows {
                demo::model_with_flows()
            } else {
                demo::model()
            };
            m.write(&file)?;
            println!(
                "wrote demo catchment to {} ({} nodes, {} conduits, {} subcatchments)",
                file.display(),
                m.nodes.len(),
                m.conduits.len(),
                m.subcatchments.len()
            );
        }
        Cmd::Check { file, json } => {
            let issues = service::check_file(&file, true)?;
            if json {
                println!("{}", service::issues_json(&issues)?);
            } else if issues.is_empty() {
                println!("no issues");
            } else {
                println!(
                    "{:<8} {:<28} {:<14} {:<12} MESSAGE",
                    "SEVERITY", "CODE", "OBJECT", "ID"
                );
                for i in &issues {
                    let clause = i
                        .clause
                        .as_ref()
                        .map(|c| format!(" [{c}]"))
                        .unwrap_or_default();
                    println!(
                        "{:<8} {:<28} {:<14} {:<12} {}{}",
                        i.severity.as_str(),
                        i.code,
                        i.object_type,
                        i.object_id,
                        i.message,
                        clause
                    );
                }
            }
            let count = |s| issues.iter().filter(|i| i.severity == s).count();
            if !json {
                println!(
                    "{} error(s), {} warning(s), {} info",
                    count(Severity::Error),
                    count(Severity::Warning),
                    count(Severity::Info)
                );
            }
            if service::error_count(&issues) > 0 {
                return Ok(1);
            }
        }
        Cmd::Trace {
            file,
            node_id,
            up,
            down,
        } => {
            let t = service::trace_file(&file, &node_id, up, down)?;
            for (label, v) in [("upstream", &t.upstream), ("downstream", &t.downstream)] {
                if let Some(v) = v {
                    println!("{label} of {node_id} ({}):", v.len());
                    v.iter().for_each(|n| println!("  {n}"));
                }
            }
            let s = &t.summary;
            println!("conduits on the traced path(s): {}", t.conduits.len());
            println!(
                "upstream of {}: {} node(s), {} conduit(s), {} connection(s), {} sub-catchment(s); ADWF {} L/s, GSDF {} L/s, pumped {} L/s",
                s.start_node,
                s.upstream_nodes,
                s.upstream_conduits,
                s.upstream_connections,
                s.upstream_subcatchments,
                opt(s.adwf_lps, 3),
                opt(s.gsdf_lps, 3),
                opt(s.pumped_lps, 3)
            );
        }
        Cmd::Runs { file, json } => {
            let runs = wwnet_sim::service::list_runs(&file)?;
            if json {
                println!("{}", wwnet_sim::service::to_json(&runs)?);
            } else if runs.is_empty() {
                println!("no stored simulation runs");
            } else {
                for r in &runs {
                    println!(
                        "{}{}  peak outlet {} L/s",
                        if r.shown { "* " } else { "  " },
                        r.title(),
                        r.summary
                            .get("peak_outlet_flow_lps")
                            .and_then(|v| v.as_f64())
                            .map(|v| format!("{v:.3}"))
                            .unwrap_or_else(|| "-".into())
                    );
                }
            }
        }
        Cmd::Compare {
            file,
            run_a,
            run_b,
            json,
        } => {
            let c = wwnet_sim::service::compare_runs(&file, &run_a, &run_b, true)?;
            if json {
                println!("{}", wwnet_sim::service::to_json(&c)?);
            } else {
                println!("{} -> {}", c.run_a.title(), c.run_b.title());
                for (k, n) in &c.counts {
                    println!("  {k}: {n}");
                }
            }
        }
        Cmd::Design {
            file,
            json,
            ruleset,
        } => return design(&file, json, ruleset.as_deref()),
        Cmd::Simulate {
            file,
            duration_h,
            warmup_h,
            roughness,
            n,
            flow_scale,
            json,
            write_inp,
            label,
        } => {
            let opts = ServiceOptions {
                duration_h,
                warmup_h,
                roughness,
                manning_n: n,
                flow_scale,
                label,
                ..ServiceOptions::default()
            };
            return simulate(&file, &opts, json, write_inp.as_deref());
        }
        Cmd::Profile {
            cmd:
                ProfileCmd::Import {
                    file,
                    profile_id,
                    csv,
                    description,
                    no_default,
                    normalise,
                },
        } => {
            let text = std::fs::read_to_string(&csv)
                .map_err(|e| anyhow::anyhow!("reading {}: {e}", csv.display()))?;
            let r = service::import_profile(
                &file,
                &profile_id,
                &text,
                &description,
                !no_default,
                normalise,
            )?;
            println!(
                "imported profile {} ({} rows, interval {} min, mean {:.4}, min {:.3}, max {:.3}{}{})",
                r.profile_id,
                r.points,
                r.interval_min.map(|i| i.to_string()).unwrap_or_else(|| "-".into()),
                r.mean_factor,
                r.min_factor,
                r.max_factor,
                if r.normalised { ", normalised" } else { "" },
                if r.is_default { ", default" } else { "" }
            );
            r.warnings.iter().for_each(|w| eprintln!("warning: {w}"));
        }
    }
    Ok(0)
}

fn opt(v: Option<f64>, prec: usize) -> String {
    v.map(|x| format!("{x:.prec$}"))
        .unwrap_or_else(|| "-".into())
}

fn design(file: &std::path::Path, json: bool, ruleset: Option<&std::path::Path>) -> Result<i32> {
    use wwnet_rules::CheckResult;
    let out = service::design_file(file, ruleset, true)?;
    if json {
        println!("{}", service::design_json(&out)?);
    } else {
        println!(
            "{:<10} {:>4} {:>9} {:>8} {:>8} {:>8} {:>7} {:>6} {:>6} {:>5} {:>5} {:<6} WORST MESSAGE",
            "CONDUIT", "DN", "GRADE", "ADWF", "GSDF", "PUMPED", "Cap", "T4.4", "Hyd", "D/D", "V", "RESULT"
        );
        for c in &out.conduit_results {
            let grade = c
                .grade_1in
                .map(|g| format!("1:{g:.0}"))
                .unwrap_or_else(|| "-".into());
            println!(
                "{:<10} {:>4} {:>9} {:>8.2} {:>8.2} {:>8.2} {:>7} {:>6} {:>6} {:>5} {:>5} {:<6} {}",
                c.conduit_id,
                c.dn,
                grade,
                c.adwf_lps,
                c.gsdf_lps,
                c.pumped_lps,
                opt(c.capacity_lps, 1),
                opt(c.table44_lps, 1),
                opt(c.hydraulic_capacity_lps, 1),
                opt(c.d_over_d, 2),
                opt(c.velocity_mps, 2),
                format!("{:?}", c.result).to_uppercase(),
                if c.result == CheckResult::Pass {
                    ""
                } else {
                    c.worst_message().unwrap_or("")
                }
            );
        }
        let s = &out.summary;
        println!(
            "{} conduit(s): {} pass, {} warn, {} fail; {} issue(s) recorded",
            s.conduits,
            s.pass,
            s.warn,
            s.fail,
            out.issues.len()
        );
        if s.total_local_adwf_lps > 0.0 {
            println!(
                "ADWF from node loads: {:.3} L/s total ({:.3} L/s at terminals); GSDF = ADWF x {} peaking factor",
                s.total_local_adwf_lps, s.terminal_adwf_lps, s.peaking_factor
            );
        }
        if s.cover_not_checked_ends > 0 {
            println!(
                "cover not checked at {} conduit end(s): no surface level",
                s.cover_not_checked_ends
            );
        }
    }
    Ok(if out.summary.fail > 0 { 1 } else { 0 })
}

fn simulate(
    file: &std::path::Path,
    opts: &ServiceOptions,
    json: bool,
    write_inp: Option<&std::path::Path>,
) -> Result<i32> {
    let out = simulate_file(file, opts, true)?;
    if let Some(p) = write_inp {
        std::fs::write(p, &out.inp_text)
            .map_err(|e| anyhow::anyhow!("writing {}: {e}", p.display()))?;
    }
    if json {
        println!("{}", simulate_json(&out)?);
        return Ok(0);
    }
    let mut links: Vec<&wwnet_sim::service::SimLinkOut> = out.links.iter().collect();
    links.sort_by(|a, b| b.max_d_over_d.total_cmp(&a.max_d_over_d));
    println!(
        "{:<12} {:>5} {:>11} {:>6} {:>8} {:>10}",
        "CONDUIT", "DN", "Qmax L/s", "d/D", "v m/s", "SURCH min"
    );
    for l in links.iter().take(50) {
        println!(
            "{:<12} {:>5} {:>11.2} {:>6.2} {:>8.2} {:>10.0}",
            l.conduit_id,
            l.dn,
            l.max_flow_lps,
            l.max_d_over_d,
            l.max_velocity_mps,
            l.surcharged_min
        );
    }
    if links.len() > 50 {
        println!(
            "... {} more conduit(s) (use --json for all)",
            links.len() - 50
        );
    }
    let s = &out.summary;
    println!(
        "inflow ADWF {:.3} L/s (x{} = {:.3} L/s simulated); peak outlet flow {:.3} L/s; continuity error {:.3} %",
        s.inflow_adwf_lps, opts.flow_scale, s.inflow_simulated_lps, s.peak_outlet_flow_lps, s.continuity_error_pct
    );
    println!(
        "{} conduit(s): {} surcharged; {} node(s): {} flooded ({:.2} m3)",
        s.conduits, s.surcharged_conduits, s.nodes, s.flooded_nodes, s.total_flood_volume_m3
    );
    if !out.pump_stations.is_empty() {
        println!(
            "{:<10} {:<10} {:>10} {:>9} {:>10} {:>9} {:>8}",
            "PUMP STN", "PUMP", "Qmax L/s", "starts/h", "run %", "max m AHD", "ovfl m3"
        );
        for st in &out.pump_stations {
            let pumps: Vec<_> = out.pumps.iter().filter(|p| p.ps_id == st.ps_id).collect();
            for (i, p) in pumps.iter().enumerate() {
                let (lvl, ov) = if i == 0 {
                    (
                        format!("{:.2}", st.max_level_m),
                        format!("{:.2}", st.overflow_volume_m3),
                    )
                } else {
                    (String::new(), String::new())
                };
                println!(
                    "{:<10} {:<10} {:>10.1} {:>9.2} {:>10.1} {:>9} {:>8}",
                    st.ps_id, p.pump_id, p.max_flow_lps, p.starts_per_hour, p.run_time_pct, lvl, ov
                );
            }
        }
        for m in &out.rising_mains {
            println!(
                "rising main {} (station {}): DN{:.0}, {:.0} m, Qmax {:.1} L/s, vmax {:.2} m/s",
                m.rm_id, m.ps_id, m.diameter_mm, m.length_m, m.max_flow_lps, m.max_velocity_mps
            );
        }
    }
    if let Some(r) = &out.run_id {
        println!("results stored in wwn_sim_* (run {r})");
    }
    out.warnings.iter().for_each(|w| eprintln!("warning: {w}"));
    Ok(0)
}
