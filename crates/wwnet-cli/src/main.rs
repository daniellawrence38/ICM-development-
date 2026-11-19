mod demo;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use wwnet_core::{gpkg, validate, Model, Network, Severity};

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
    /// Run DS 50 design checks (not yet implemented)
    Design { file: PathBuf },
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
            let model =
                Model::load(&file).with_context(|| format!("loading {}", file.display()))?;
            let net = Network::build(&model)?;
            let issues = validate(&model, &net);
            Model::write_issues(&file, &issues)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&issues)?);
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
            if count(Severity::Error) > 0 {
                return Ok(1);
            }
        }
        Cmd::Trace {
            file,
            node_id,
            up,
            down,
        } => {
            let model = Model::load(&file)?;
            let net = Network::build(&model)?;
            let (up, down) = if !up && !down {
                (true, true)
            } else {
                (up, down)
            };
            if up {
                let v = net.upstream_trace(&node_id)?;
                println!("upstream of {node_id} ({}):", v.len());
                v.iter().for_each(|n| println!("  {n}"));
            }
            if down {
                let v = net.downstream_trace(&node_id)?;
                println!("downstream of {node_id} ({}):", v.len());
                v.iter().for_each(|n| println!("  {n}"));
            }
        }
        Cmd::Design { .. } => bail!("`design` is not yet implemented"),
    }
    Ok(0)
}
