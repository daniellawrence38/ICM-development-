mod demo;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use wwnet_core::{gpkg, Severity};
use wwnet_design::service;

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
    /// Trace upstream and/or downstream of a node
    Trace {
        file: PathBuf,
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
    /// Write a synthetic demo catchment
    #[command(hide = true)]
    Demo { file: PathBuf },
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
        Cmd::Demo { file } => {
            gpkg::create(&file)?;
            let m = demo::model();
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
        }
        Cmd::Design {
            file,
            json,
            ruleset,
        } => return design(&file, json, ruleset.as_deref()),
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
            "{:<10} {:>4} {:>9} {:>8} {:>8} {:>7} {:>6} {:>6} {:>5} {:>5} {:<6} WORST MESSAGE",
            "CONDUIT", "DN", "GRADE", "GSDF", "PUMPED", "Cap", "T4.4", "Hyd", "D/D", "V", "RESULT"
        );
        for c in &out.conduit_results {
            let grade = c
                .grade_1in
                .map(|g| format!("1:{g:.0}"))
                .unwrap_or_else(|| "-".into());
            println!(
                "{:<10} {:>4} {:>9} {:>8.2} {:>8.2} {:>7} {:>6} {:>6} {:>5} {:>5} {:<6} {}",
                c.conduit_id,
                c.dn,
                grade,
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
        if s.cover_not_checked_ends > 0 {
            println!(
                "cover not checked at {} conduit end(s): no surface level",
                s.cover_not_checked_ends
            );
        }
    }
    Ok(if out.summary.fail > 0 { 1 } else { 0 })
}
