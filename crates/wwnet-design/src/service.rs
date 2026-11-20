//! File-level services shared by the `wwnet` CLI and the `wwnet_native` Python module.
//!
//! Each function opens a GeoPackage, runs the operation and (optionally) persists results
//! exactly as the CLI does. JSON helpers produce the text that `wwnet check --json` /
//! `wwnet design --json` print and that the front ends parse.
use crate::{run_design, to_conduit_results, DesignOutput};
use serde::Serialize;
use std::path::Path;
use wwnet_core::{gpkg, validate, Error, Issue, Model, Network, Result, Severity};
use wwnet_rules::RuleSet;

fn loading<T>(path: &Path, r: Result<T>) -> Result<T> {
    r.map_err(|e| Error::Other(format!("loading {}: {e}", path.display())))
}

/// Create an empty model GeoPackage.
pub fn new_model(path: impl AsRef<Path>) -> Result<()> {
    gpkg::create(path)
}

/// Build a new model GeoPackage from JSON (the serde form of [`Model`]: nodes, conduits,
/// subcatchments, pump_stations, pumps, rising_mains; geometry as `[x, y]` / `[[x, y], ...]`).
/// Fails if the file exists and is not empty unless `overwrite`. On any failure after the
/// file was created it is removed again, so no half-written model is left behind.
pub fn write_model_json(path: impl AsRef<Path>, model_json: &str, overwrite: bool) -> Result<()> {
    let path = path.as_ref();
    let model: Model = serde_json::from_str(model_json)
        .map_err(|e| Error::Other(format!("invalid model JSON: {e}")))?;
    if overwrite && path.is_file() {
        std::fs::remove_file(path)?;
    }
    gpkg::create(path)?;
    if let Err(e) = model.write(path) {
        let _ = std::fs::remove_file(path);
        return Err(e);
    }
    Ok(())
}

/// Validate a model. With `write`, replaces `wwn_issues` in the file.
pub fn check_file(path: impl AsRef<Path>, write: bool) -> Result<Vec<Issue>> {
    let path = path.as_ref();
    let model = loading(path, Model::load(path))?;
    let net = Network::build(&model)?;
    let issues = validate(&model, &net);
    if write {
        Model::write_issues(path, &issues)?;
    }
    Ok(issues)
}

/// Number of error-severity issues (the CLI exits 1 when this is non-zero).
pub fn error_count(issues: &[Issue]) -> usize {
    issues
        .iter()
        .filter(|i| i.severity == Severity::Error)
        .count()
}

/// Pretty JSON exactly as `wwnet check --json` prints it.
pub fn issues_json(issues: &[Issue]) -> Result<String> {
    serde_json::to_string_pretty(issues).map_err(|e| Error::Other(e.to_string()))
}

/// Run the DS 50 design. With `write`, saves `wwn_results_conduits` and appends to `wwn_issues`.
/// `ruleset` is a rule set TOML path (default: embedded DS 50 Ver 2 Rev 4).
pub fn design_file(
    path: impl AsRef<Path>,
    ruleset: Option<&Path>,
    write: bool,
) -> Result<DesignOutput> {
    let path = path.as_ref();
    let rules = match ruleset {
        Some(p) => RuleSet::from_path(p)
            .map_err(|e| Error::Other(format!("loading {}: {e}", p.display())))?,
        None => RuleSet::ds50(),
    };
    let model = loading(path, Model::load(path))?;
    let out = run_design(&model, &rules)?;
    if write {
        Model::save_results(path, &to_conduit_results(&out, &rules)?)?;
        Model::append_issues(path, &out.issues)?;
    }
    Ok(out)
}

/// Pretty JSON exactly as `wwnet design --json` prints it.
pub fn design_json(out: &DesignOutput) -> Result<String> {
    serde_json::to_string_pretty(out).map_err(|e| Error::Other(e.to_string()))
}

/// Trace result; a direction that was not requested is omitted from the JSON.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct TraceResult {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub upstream: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub downstream: Option<Vec<String>>,
}

/// Trace upstream and/or downstream of a node (neither flag set means both, as in the CLI).
pub fn trace_file(
    path: impl AsRef<Path>,
    node_id: &str,
    up: bool,
    down: bool,
) -> Result<TraceResult> {
    let path = path.as_ref();
    let model = Model::load(path)?;
    let net = Network::build(&model)?;
    let (up, down) = if !up && !down {
        (true, true)
    } else {
        (up, down)
    };
    let own = |v: Vec<&str>| v.into_iter().map(String::from).collect::<Vec<_>>();
    Ok(TraceResult {
        upstream: if up {
            Some(own(net.upstream_trace(node_id)?))
        } else {
            None
        },
        downstream: if down {
            Some(own(net.downstream_trace(node_id)?))
        } else {
            None
        },
    })
}

/// Pretty JSON of a trace result.
pub fn trace_json(t: &TraceResult) -> Result<String> {
    serde_json::to_string_pretty(t).map_err(|e| Error::Other(e.to_string()))
}
