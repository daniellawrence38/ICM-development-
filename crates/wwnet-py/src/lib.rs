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

/// Colebrook-White pipe capacity calculator (internal diameter = DN unless `id_mm`): dict with
/// dn_mm, grade_1in, ks_mm, full_flow_lps, full_velocity_mps and, with `depth_ratio`,
/// capacity_lps / velocity_mps at that d/D; with `flow_lps`, depth_ratio_at_flow /
/// velocity_at_flow_mps (None and a note when the flow exceeds the open-channel capacity).
/// ValueError for non-positive DN / grade / ks or a d/D outside 0..1.
#[pyfunction]
#[pyo3(signature = (dn_mm, grade_1in, ks_mm=1.0, depth_ratio=None, flow_lps=None, id_mm=None))]
fn pipe_capacity<'py>(
    py: Python<'py>,
    dn_mm: f64,
    grade_1in: f64,
    ks_mm: f64,
    depth_ratio: Option<f64>,
    flow_lps: Option<f64>,
    id_mm: Option<f64>,
) -> PyResult<Bound<'py, PyDict>> {
    if !(dn_mm > 0.0 && grade_1in > 0.0 && ks_mm > 0.0) {
        return Err(PyValueError::new_err(
            "DN, grade (1 in x) and ks must be positive",
        ));
    }
    let d = id_mm.filter(|v| *v > 0.0).unwrap_or(dn_mm) / 1000.0;
    let (slope, ks) = (hyd::slope_from_1in(grade_1in), ks_mm / 1000.0);
    let vel = |y: f64, q_m3s: f64| {
        let a = hyd::geometry(d, y).area;
        if a > 0.0 {
            q_m3s / a
        } else {
            0.0
        }
    };
    let r = |x: f64, n: i32| (x * 10f64.powi(n)).round() / 10f64.powi(n);
    let out = PyDict::new(py);
    out.set_item("dn_mm", dn_mm)?;
    out.set_item("grade_1in", grade_1in)?;
    out.set_item("ks_mm", ks_mm)?;
    out.set_item("internal_diameter_mm", d * 1000.0)?;
    let qf = hyd::part_full_flow(d, slope, 1.0, ks);
    out.set_item("full_flow_lps", r(qf * 1000.0, 3))?;
    out.set_item("full_velocity_mps", r(vel(1.0, qf), 3))?;
    if let Some(y) = depth_ratio {
        if !(0.0..=1.0).contains(&y) {
            return Err(PyValueError::new_err("depth_ratio must be between 0 and 1"));
        }
        let q = hyd::part_full_flow(d, slope, y, ks);
        out.set_item("depth_ratio", y)?;
        out.set_item("capacity_lps", r(q * 1000.0, 3))?;
        out.set_item("velocity_mps", r(vel(y, q), 3))?;
    }
    if let Some(q) = flow_lps {
        out.set_item("flow_lps", q)?;
        match hyd::depth_ratio_for_flow(q / 1000.0, d, slope, ks) {
            Some(y) => {
                out.set_item("depth_ratio_at_flow", r(y, 4))?;
                out.set_item("velocity_at_flow_mps", r(vel(y, q / 1000.0), 3))?;
            }
            None => {
                out.set_item("depth_ratio_at_flow", py.None())?;
                out.set_item(
                    "note",
                    "flow exceeds open-channel capacity (pipe would surcharge)",
                )?;
            }
        }
    }
    out.set_item("source", "wwnet")?;
    Ok(out)
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
/// (severity, code, object_type, object_id, message, clause). With `write`, replaces the `check`
/// rows of `wwn_issues` (a point layer).
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
/// issues and summary. With `write`, saves the wwn_results_conduits layer and replaces the `design`
/// rows of wwn_issues.
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

/// Trace upstream and/or downstream of a node or a conduit: {"upstream": [node ids],
/// "downstream": [node ids], "conduits": [conduit ids], "summary": {start_node, start_conduit,
/// upstream_nodes, upstream_conduits, upstream_connections, upstream_subcatchments, adwf_lps,
/// gsdf_lps, pumped_lps}}; only the requested directions are present (neither flag means both).
/// KeyError if the id is neither a node nor a conduit.
#[pyfunction]
#[pyo3(signature = (gpkg_path, node_id, up=true, down=false))]
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

/// Import a diurnal profile from CSV / tab-separated text (time as hh:mm or minutes, factor; header
/// optional) into a model GeoPackage, upgrading it to schema v2 if needed. Returns a dict
/// (profile_id, points, interval_min, mean_factor, input_mean_factor, min_factor, max_factor,
/// normalised, is_default, warnings). ValueError for unreadable / non-monotonic / out-of-range rows.
/// The values are not normalised unless `normalise`; a mean far from 1 is returned in `warnings`.
#[pyfunction]
#[pyo3(signature = (gpkg_path, profile_id, csv_text, description="", set_default=true, normalise=false))]
fn import_profile<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    profile_id: &str,
    csv_text: &str,
    description: &str,
    set_default: bool,
    normalise: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let text = py
        .allow_threads(|| {
            let r = service::import_profile(
                gpkg_path,
                profile_id,
                csv_text,
                description,
                set_default,
                normalise,
            )?;
            service::profile_import_json(&r)
        })
        .map_err(|e| match &e {
            wwnet_core::Error::Other(m)
                if m.starts_with("line ")
                    || m.starts_with("profile ")
                    || m.starts_with("cannot normalise") =>
            {
                PyValueError::new_err(m.clone())
            }
            _ => lib_err(e),
        })?;
    loads(py, &text)
}

/// Run the SWMM dynamic-wave simulation (same as `wwnet simulate --json`): dict with run_id,
/// label, options, summary, links, nodes, issues, warnings, series_ids. With `write`, results go to
/// the wwn_sim_* tables and the wwn_sim_conduit_results / wwn_sim_node_results map layers (series
/// for the downstream-most 5 conduits plus `series_ids`), and the issues to wwn_issues (source
/// `simulate`). `roughness` "ks" (default, the design's Colebrook ks as a per-conduit n) or
/// "manning" (one `n`). With `write_inp`, the generated SWMM input file is saved there.
/// RuntimeError if the engine fails; ValueError for bad options.
#[pyfunction]
#[pyo3(signature = (
    gpkg_path, duration_h=48.0, warmup_h=24.0, roughness="ks", n=0.013, flow_scale=1.0,
    write=true, series_ids=None, write_inp=None, label=""
))]
#[allow(clippy::too_many_arguments)]
fn simulate<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    duration_h: f64,
    warmup_h: f64,
    roughness: &str,
    n: f64,
    flow_scale: f64,
    write: bool,
    series_ids: Option<Vec<String>>,
    write_inp: Option<&str>,
    label: &str,
) -> PyResult<Bound<'py, PyAny>> {
    use wwnet_sim::service::{simulate_file, simulate_json, ServiceOptions};
    let opts = ServiceOptions {
        duration_h,
        warmup_h,
        roughness: roughness.to_string(),
        manning_n: n,
        flow_scale,
        series_ids: series_ids.unwrap_or_default(),
        label: label.to_string(),
        ..ServiceOptions::default()
    };
    let text = py
        .allow_threads(|| {
            let out = simulate_file(gpkg_path, &opts, write)?;
            if let Some(p) = write_inp {
                std::fs::write(p, &out.inp_text)?;
            }
            simulate_json(&out)
        })
        .map_err(|e| match &e {
            wwnet_core::Error::Other(m)
                if m.starts_with("roughness must")
                    || m.starts_with("invalid Manning")
                    || m.starts_with("invalid flow scale")
                    || m.contains("invalid options") =>
            {
                PyValueError::new_err(m.clone())
            }
            _ => lib_err(e),
        })?;
    loads(py, &text)
}

/// Upgrade a model GeoPackage to the current schema (creates missing tables / columns).
#[pyfunction]
fn upgrade_model(py: Python<'_>, gpkg_path: &str) -> PyResult<()> {
    py.allow_threads(|| wwnet_core::gpkg::upgrade(gpkg_path))
        .map_err(lib_err)
}

/// The file's schema version (1 when unknown); `SCHEMA_VERSION` is the current one.
#[pyfunction]
fn schema_version(py: Python<'_>, gpkg_path: &str) -> PyResult<i64> {
    py.allow_threads(|| wwnet_core::gpkg::schema_version(gpkg_path))
        .map_err(lib_err)
}

/// User-facing service errors (no such run / profile, bad value) become ValueError.
fn svc_err(e: wwnet_core::Error) -> PyErr {
    match &e {
        wwnet_core::Error::Other(m)
            if m.starts_with("no ")
                || m.starts_with("profile ")
                || m.starts_with("both runs")
                || m.starts_with("invalid ")
                || m.starts_with("issue source")
                || m.starts_with("table must")
                || m.contains(" must be between ") =>
        {
            PyValueError::new_err(m.clone())
        }
        _ => lib_err(e),
    }
}

fn json_call<'py, F>(py: Python<'py>, f: F) -> PyResult<Bound<'py, PyAny>>
where
    F: FnOnce() -> wwnet_core::Result<String> + Send,
{
    let text = py.allow_threads(f).map_err(svc_err)?;
    loads(py, &text)
}

/// Replace the `source` rows ("check", "design", "import", "simulate") of wwn_issues with the
/// issues in `issues_json` (list of {severity, code, object_type, object_id, clause, message,
/// x?, y?}); issues without x / y are drawn at their object. Returns the number stored.
#[pyfunction]
fn store_issues(
    py: Python<'_>,
    gpkg_path: &str,
    source: &str,
    issues_json: &str,
) -> PyResult<usize> {
    py.allow_threads(|| service::store_issues_json(gpkg_path, source, issues_json))
        .map_err(svc_err)
}

/// Stored simulation runs, newest first: list of {run_id, label, options, summary,
/// continuity_error_pct, warnings, shown}.
#[pyfunction]
fn sim_runs<'py>(py: Python<'py>, gpkg_path: &str) -> PyResult<Bound<'py, PyAny>> {
    json_call(py, || {
        wwnet_sim::service::to_json(&wwnet_sim::service::list_runs(gpkg_path)?)
    })
}

/// Show a stored run (run id, label, "latest" or "previous") in the wwn_sim_conduit_results /
/// wwn_sim_node_results layers. Returns the run's info dict. ValueError if there is no such run.
#[pyfunction]
#[pyo3(signature = (gpkg_path, run="latest"))]
fn show_sim_run<'py>(py: Python<'py>, gpkg_path: &str, run: &str) -> PyResult<Bound<'py, PyAny>> {
    json_call(py, || {
        wwnet_sim::service::to_json(&wwnet_sim::service::show_run(gpkg_path, run)?)
    })
}

/// Compare stored runs `run_a` (baseline) and `run_b` per conduit: {run_a, run_b, rows, counts};
/// with `write`, fills the wwn_sim_compare layer.
#[pyfunction]
#[pyo3(signature = (gpkg_path, run_a="previous", run_b="latest", write=true))]
fn compare_sim_runs<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    run_a: &str,
    run_b: &str,
    write: bool,
) -> PyResult<Bound<'py, PyAny>> {
    json_call(py, || {
        wwnet_sim::service::to_json(&wwnet_sim::service::compare_runs(
            gpkg_path, run_a, run_b, write,
        )?)
    })
}

/// Stored time series of a run: {run, series: [{object_type, object_id, minutes, flow_lps,
/// depth_m}], missing, available}. `ids` None = every stored series.
#[pyfunction]
#[pyo3(signature = (gpkg_path, run="latest", ids=None))]
fn sim_series<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    run: &str,
    ids: Option<Vec<String>>,
) -> PyResult<Bound<'py, PyAny>> {
    let ids = ids.unwrap_or_default();
    json_call(py, move || {
        wwnet_sim::service::to_json(&wwnet_sim::service::read_series(gpkg_path, run, &ids)?)
    })
}

/// Loading settings and profiles: {schema_version, adwf_per_connection_lpd, peaking_factor,
/// default_profile, profiles: [{profile_id, description, points, interval_min, mean_factor}]}.
#[pyfunction]
fn get_settings<'py>(py: Python<'py>, gpkg_path: &str) -> PyResult<Bound<'py, PyAny>> {
    json_call(py, || service::json(&service::get_settings(gpkg_path)?))
}

/// Change loading settings (None keeps a value; default_profile "" clears it). Returns the
/// settings dict. ValueError for out-of-range values or an unknown profile.
#[pyfunction]
#[pyo3(signature = (gpkg_path, adwf_per_connection_lpd=None, peaking_factor=None, default_profile=None))]
fn set_settings<'py>(
    py: Python<'py>,
    gpkg_path: &str,
    adwf_per_connection_lpd: Option<f64>,
    peaking_factor: Option<f64>,
    default_profile: Option<&str>,
) -> PyResult<Bound<'py, PyAny>> {
    json_call(py, || {
        service::json(&service::set_settings(
            gpkg_path,
            adwf_per_connection_lpd,
            peaking_factor,
            default_profile,
        )?)
    })
}

/// Set profile_id on node loads (`table` "node_loads", ids = load or node ids) or sub-catchments
/// (`table` "subcatchments"); "" clears it. Returns the number of rows changed.
#[pyfunction]
fn assign_profile(
    py: Python<'_>,
    gpkg_path: &str,
    table: &str,
    ids: Vec<String>,
    profile_id: &str,
) -> PyResult<usize> {
    py.allow_threads(|| service::assign_profile(gpkg_path, table, &ids, profile_id))
        .map_err(svc_err)
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
    m.add_function(wrap_pyfunction!(pipe_capacity, m)?)?;
    m.add_function(wrap_pyfunction!(residential_gsdf, m)?)?;
    m.add_function(wrap_pyfunction!(check_conduit, m)?)?;
    m.add_function(wrap_pyfunction!(ruleset_toml, m)?)?;
    m.add_function(wrap_pyfunction!(check, m)?)?;
    m.add_function(wrap_pyfunction!(design, m)?)?;
    m.add_function(wrap_pyfunction!(trace, m)?)?;
    m.add_function(wrap_pyfunction!(new_model, m)?)?;
    m.add_function(wrap_pyfunction!(write_model, m)?)?;
    m.add_function(wrap_pyfunction!(import_profile, m)?)?;
    m.add_function(wrap_pyfunction!(simulate, m)?)?;
    m.add_function(wrap_pyfunction!(upgrade_model, m)?)?;
    m.add_function(wrap_pyfunction!(schema_version, m)?)?;
    m.add_function(wrap_pyfunction!(store_issues, m)?)?;
    m.add_function(wrap_pyfunction!(sim_runs, m)?)?;
    m.add_function(wrap_pyfunction!(show_sim_run, m)?)?;
    m.add_function(wrap_pyfunction!(compare_sim_runs, m)?)?;
    m.add_function(wrap_pyfunction!(sim_series, m)?)?;
    m.add_function(wrap_pyfunction!(get_settings, m)?)?;
    m.add_function(wrap_pyfunction!(set_settings, m)?)?;
    m.add_function(wrap_pyfunction!(assign_profile, m)?)?;
    m.add("SCHEMA_VERSION", wwnet_core::gpkg::SCHEMA_VERSION)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    Ok(())
}
