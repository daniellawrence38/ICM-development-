//! Python bindings (`import wwnet`). Flows in L/s, diameters in mm, grades as "1 in x".

use pyo3::exceptions::{PyIOError, PyKeyError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::Path;
use wwnet_design::service;
use wwnet_hydraulics as hyd;
use wwnet_rules::{
    check_conduit as rules_check, CheckResult, ConduitDesignInput, Ground, LandUseClass, Location,
    Region, RuleSet,
};

/// Map library errors onto Python exceptions with the library's message.
fn lib_err(e: wwnet_core::Error) -> PyErr {
    use wwnet_core::Error as E;
    match &e {
        E::NodeNotFound(_) => PyKeyError::new_err(e.to_string()),
        E::Io(_) => PyIOError::new_err(e.to_string()),
        E::Sqlite(_) | E::Cycle(_) | E::Other(_) => PyRuntimeError::new_err(e.to_string()),
        _ => PyValueError::new_err(e.to_string()),
    }
}

/// Parse JSON text produced by wwnet-design's service layer into Python objects.
fn loads<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import("json")?.call_method1("loads", (text,))
}

fn val<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// Part-full Colebrook-White flow in L/s.
#[pyfunction]
#[pyo3(signature = (dn_mm, grade_1in, depth_ratio, ks_mm=1.0))]
fn part_full_flow_lps(dn_mm: f64, grade_1in: f64, depth_ratio: f64, ks_mm: f64) -> f64 {
    hyd::part_full_flow(
        dn_mm / 1000.0,
        hyd::slope_from_1in(grade_1in),
        depth_ratio,
        ks_mm / 1000.0,
    ) * 1000.0
}

/// Depth ratio d/D for a flow in L/s, or None if it exceeds part-full capacity.
#[pyfunction]
#[pyo3(signature = (q_lps, dn_mm, grade_1in, ks_mm=1.0))]
fn depth_ratio_for_flow(q_lps: f64, dn_mm: f64, grade_1in: f64, ks_mm: f64) -> Option<f64> {
    hyd::depth_ratio_for_flow(
        q_lps / 1000.0,
        dn_mm / 1000.0,
        hyd::slope_from_1in(grade_1in),
        ks_mm / 1000.0,
    )
}

/// Residential loading rate (DS50 Tables 4.1/4.2) as a dict, including GSDF for `net_area_ha`.
#[pyfunction]
#[pyo3(signature = (net_area_ha, rcode, ground="dry", region="south"))]
fn residential_gsdf<'py>(
    py: Python<'py>,
    net_area_ha: f64,
    rcode: &str,
    ground: &str,
    region: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let rs = RuleSet::ds50();
    let r = rs
        .residential_rate(
            region.parse::<Region>().map_err(val)?,
            ground.parse::<Ground>().map_err(val)?,
            rcode,
        )
        .map_err(val)?;
    let d = PyDict::new(py);
    d.set_item("rcode_used", r.rcode_used)?;
    d.set_item("persons_per_net_ha", r.persons_per_net_ha)?;
    d.set_item("lpd", r.lpd)?;
    d.set_item("gsdf_lps_per_ha", r.gsdf_lps_per_ha)?;
    d.set_item("net_area_ha", net_area_ha)?;
    d.set_item("persons", r.persons_per_net_ha * net_area_ha)?;
    d.set_item(
        "daily_flow_l_per_day",
        r.persons_per_net_ha * r.lpd * net_area_ha,
    )?;
    d.set_item("gsdf_lps", r.gsdf_lps_per_ha * net_area_ha)?;
    d.set_item("flagged", r.flagged)?;
    d.set_item("note", r.note)?;
    d.set_item("clause", r.clause)?;
    Ok(d)
}

/// Run DS50 conduit checks; returns a list of dicts (rule, clause, value, limit, result, message).
#[pyfunction]
#[pyo3(signature = (
    dn, grade_1in, gsdf_lps, length_m=0.0, us_cover_m=10.0, ds_cover_m=10.0,
    pumped_inflow_lps=0.0, land_use_class="residential", location="elsewhere",
    levels_uncertain=false
))]
#[allow(clippy::too_many_arguments)]
fn check_conduit<'py>(
    py: Python<'py>,
    dn: u32,
    grade_1in: f64,
    gsdf_lps: f64,
    length_m: f64,
    us_cover_m: f64,
    ds_cover_m: f64,
    pumped_inflow_lps: f64,
    land_use_class: &str,
    location: &str,
    levels_uncertain: bool,
) -> PyResult<Bound<'py, PyList>> {
    let input = ConduitDesignInput {
        dn,
        grade_1in,
        gsdf_lps,
        pumped_inflow_lps,
        land_use_class: land_use_class.parse::<LandUseClass>().map_err(val)?,
        length_m,
        us_cover_m,
        ds_cover_m,
        location: location.parse::<Location>().map_err(val)?,
        levels_uncertain,
    };
    let out = PyList::empty(py);
    for c in rules_check(&RuleSet::ds50(), &input) {
        let d = PyDict::new(py);
        d.set_item("rule", c.rule)?;
        d.set_item("clause", c.clause)?;
        d.set_item("value", c.value)?;
        d.set_item("limit", c.limit)?;
        d.set_item(
            "result",
            match c.result {
                CheckResult::Pass => "pass",
                CheckResult::Warn => "warn",
                CheckResult::Fail => "fail",
                CheckResult::Info => "info",
            },
        )?;
        d.set_item("message", c.message)?;
        out.append(d)?;
    }
    Ok(out)
}

/// The embedded DS50 rule set as TOML text.
#[pyfunction]
fn ruleset_toml() -> &'static str {
    RuleSet::ds50_toml()
}

/// Validate a model (same as `wwnet check --json`): list of issue dicts
/// (severity, code, object_type, object_id, message, clause). With `write`, replaces `wwn_issues`.
#[pyfunction]
#[pyo3(signature = (gpkg_path, write=true))]
fn check<'py>(py: Python<'py>, gpkg_path: &str, write: bool) -> PyResult<Bound<'py, PyAny>> {
    let text = py
        .allow_threads(|| {
            let issues = service::check_file(gpkg_path, write)?;
            service::issues_json(&issues)
        })
        .map_err(lib_err)?;
    loads(py, &text)
}

/// Run the DS 50 design (same as `wwnet design --json`): dict with conduit_results, node_flows,
/// issues and summary. With `write`, saves wwn_results_conduits and appends to wwn_issues.
#[pyfunction]
#[pyo3(signature = (gpkg_path, ruleset_path=None, write=true))]
fn design<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    ruleset_path: Option<&str>,
    write: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let text = py
        .allow_threads(|| {
            let out = service::design_file(gpkg_path, ruleset_path.map(Path::new), write)?;
            service::design_json(&out)
        })
        .map_err(lib_err)?;
    loads(py, &text)
}

/// Trace upstream and/or downstream of a node: {"upstream": [ids], "downstream": [ids]}
/// containing only the requested directions (neither flag means both). KeyError if the node is unknown.
#[pyfunction]
fn trace<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    node_id: &str,
    up: bool,
    down: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let text = py
        .allow_threads(|| {
            let t = service::trace_file(gpkg_path, node_id, up, down)?;
            service::trace_json(&t)
        })
        .map_err(lib_err)?;
    loads(py, &text)
}

/// Create an empty model GeoPackage.
#[pyfunction]
fn new_model(py: Python<'_>, gpkg_path: &str) -> PyResult<()> {
    py.allow_threads(|| service::new_model(gpkg_path))
        .map_err(lib_err)
}

/// Write a new model GeoPackage from JSON text (nodes, conduits, subcatchments, pump_stations,
/// pumps, rising_mains). Geometry is `[x, y]` for points and `[[x, y], ...]` for lines (EPSG:7850).
/// ValueError for bad JSON / enum values; RuntimeError if the file exists (unless `overwrite`)
/// or a constraint (e.g. duplicate id) fails; the partial file is removed on failure.
#[pyfunction]
#[pyo3(signature = (gpkg_path, model_json, overwrite=false))]
fn write_model(py: Python<'_>, gpkg_path: &str, model_json: &str, overwrite: bool) -> PyResult<()> {
    py.allow_threads(|| service::write_model_json(gpkg_path, model_json, overwrite))
        .map_err(|e| match &e {
            wwnet_core::Error::Other(m) if m.starts_with("invalid model JSON") => {
                PyValueError::new_err(m.clone())
            }
            _ => lib_err(e),
        })
}

/// Version of the native module.
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn wwnet_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(part_full_flow_lps, m)?)?;
    m.add_function(wrap_pyfunction!(depth_ratio_for_flow, m)?)?;
    m.add_function(wrap_pyfunction!(residential_gsdf, m)?)?;
    m.add_function(wrap_pyfunction!(check_conduit, m)?)?;
    m.add_function(wrap_pyfunction!(ruleset_toml, m)?)?;
    m.add_function(wrap_pyfunction!(check, m)?)?;
    m.add_function(wrap_pyfunction!(design, m)?)?;
    m.add_function(wrap_pyfunction!(trace, m)?)?;
    m.add_function(wrap_pyfunction!(new_model, m)?)?;
    m.add_function(wrap_pyfunction!(write_model, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    Ok(())
}
