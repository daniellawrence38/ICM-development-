# Wastewater Network Modelling Tool — Development Plan

Working name: **WWNet** (placeholder). Scope: an InfoWorks ICM-style modelling tool restricted to
wastewater collection networks — gravity sewers, pump stations and pressure (rising) mains — with
Water Corporation design criteria (DS 50 / DS 51 series) built in as checkable rule sets, and a
proper asset register so existing networks and pump stations can be entered and analysed.

---

## 1. Scope

### In scope
- Network data model: manholes, gravity pipes, pump stations (wet well, pumps, controls),
  rising mains, valves, discharge/outfall points, catchments/sub-catchments.
- Loading: DS 50 method — net area × R-Code density × L/person/day (dry or wet ground, north or
  south of 26°S), Sewer Design Flow = 1.5 × Daily Flow; non-residential by L/net ha/day; pumped
  inflows at duty rate; diurnal profiles for time-varying runs.
- Two analysis modes:
  1. **Design (steady-state)** — GSDF accumulated down the tree, pipe sizing and constraint
     checks (Table 4.4 grade/flow bands, d/D, cover, AC spacing and falls, pump duty point).
  2. **Hydraulic simulation (unsteady)** — extended-period dynamic-wave run with pump cycling,
     wet-well levels, surcharge and overflow (EOS) detection.
- Constraint / compliance reporting against DS rules, with clause references.
- Import/export: shapefile/GeoPackage/CSV (Water Corp GIS extracts), SWMM .inp, ICM CSV export.
- Scenarios (existing vs ultimate development, augmentation options).

### Out of scope (v1)
- Stormwater / 2D overland flow, rainfall-runoff RTK hydrographs (can be added later).
- Water quality, septicity/H₂S modelling (possible v2 — detention time is cheap to add early).
- Multi-user database server, licensing.

---

## 2. Key architectural decision: don't write the dynamic solver

Writing a robust St Venant / Preissmann-slot solver that handles surcharge, pump cycling and
dry/wet transitions is the bulk of what ICM's licence fee buys. Recommendation:

| Component | Approach |
|---|---|
| Gravity steady-state design calcs | **Own code** (Colebrook-White + Manning, part-full relationships). Simple, fully auditable, matches DS hand calcs. |
| Dynamic simulation (gravity + pumps + rising mains) | **EPA SWMM 5.2 C engine via Rust FFI**. Dynamic wave, force mains (H-W or D-W), pump curves, on/off level controls, storage nodes for wet wells. Public domain, well validated. |
| Pressure network steady-state (pump duty point, shared rising mains, multiple stations into a common main) | **Own system-curve solver** for single-station cases; **EPANET 2.2 C engine via Rust FFI** for manifolded rising-main networks. |

The tool is therefore: *data model + DS rules engine + loading engine + translators to/from
SWMM/EPANET + results/compliance UI*. That's achievable for a small team; a bespoke dynamic
solver isn't (at least not in v1). The engine sits behind an interface so it can be swapped later.

---

## 3. Tech stack (Rust)

Core is a Rust workspace: a pure library (`wwnet-core` and friends), a CLI for batch runs and
CI, and **QGIS as the front end** via a Python wheel built from the Rust core (PyO3/maturin).
All hydraulics, loading and rules live in Rust; the QGIS plugin is thin PyQGIS glue (forms,
layer styling, run buttons, results display).

| Concern | Crate / approach |
|---|---|
| Data model, serialisation | `serde`; rule sets in **TOML** (`toml` crate — `serde_yaml` is unmaintained) |
| Units safety | `uom` for internal quantities (L/s vs m³/s and m vs mm errors become compile errors), plain `f64` at hot loops |
| Topology / tracing | `petgraph` (DAG for accumulation, cycle detection, upstream/downstream trace) |
| Geometry | `geo` / `geo-types`; `proj` only if reprojection is needed (MGA2020 Zone 50 ↔ GDA2020 lat/long) |
| Storage | GeoPackage via `rusqlite` (bundled SQLite) — write the GPKG tables directly; schema migrations with `refinery` |
| GIS import | `shapefile`, `csv`; `gdal` crate optional (pulls in the GDAL C library — keep behind a cargo feature) |
| Numerics | own Colebrook/part-full solvers (bisection/Brent); `nalgebra` if a native network solver is added later |
| SWMM / EPANET | FFI to the C engines: vendor EPA SWMM 5.2 and EPANET 2.2 source, build with `cc`, bindings with `bindgen`, wrapped in a safe `Engine` trait |
| Parallel scenarios | `rayon` (independent SWMM runs per scenario in parallel) |
| Errors / logging | `thiserror` (library), `anyhow` (CLI/app), `tracing` |
| Python bindings | `pyo3` + `maturin`, built as an **abi3** wheel so one build works across the QGIS-bundled Python versions (OSGeo4W on Windows ships its own Python) |
| GUI | **QGIS 3.40 LTR plugin** (PyQGIS / Qt): GeoPackage layers edited natively in QGIS, custom dock widgets for properties, run manager, compliance table, long sections |
| Charts / long sections | `plotters` for report images; in QGIS, matplotlib (bundled with QGIS) or pyqtgraph in a dock widget |
| Reports | `rust_xlsxwriter` for Excel; Typst templates for PDF reports |
| Tests | `cargo test`, `approx` for float tolerance, `insta` snapshot tests for compliance tables and .inp output, `proptest` for hydraulic invariants (e.g. Q monotonic with depth up to ~0.94D) |

**Why the C engines are still worth keeping:** SWMM 5.2 and EPANET 2.2 are small, plain C,
public domain and compile cleanly with the `cc` crate, so FFI is low risk. Put them behind a
`HydraulicEngine` trait; a native Rust dynamic-wave solver can then replace SWMM later
(v2+) and be validated against it run for run.

**Why QGIS works well here:** mapping, editing, snapping, attribute forms, symbology, print
layouts and GIS import come free, and the model *is* a GeoPackage QGIS edits directly — the Rust
core reads the same file. The plugin code is ordinary Python, which suits your existing skills;
Rust is confined to the calculation crates. Keep the core GUI-free so a CLI (and any later
standalone app) uses the same code.

**Learning curve:** Rust is a big step up from beginner-to-intermediate Python — the borrow
checker, traits and FFI will slow Phase 0–1 noticeably. The core calculation code is a good
fit for Rust (pure functions over data); the C FFI and PyO3 boundary are the hard parts. Durations
below allow for this.

---

## 4. Data model (core entities)

**Node** — id, x/y, type (manhole, junction, wet well, outfall, inspection opening), invert
level, cover/surface level, diameter, drop structure flag, flood/overflow level, status
(existing/proposed/abandoned), asset ID (Water Corp), install year, source/confidence flags.

**Gravity link** — id, us/ds node, us/ds invert, length, internal diameter (from nominal + class
lookup), material, roughness (ks or n), construction year, condition grade, status.

**Pump station** — id, wet well node, wet-well geometry (area vs level table), start/stop
levels per pump, duty/assist/standby configuration, emergency storage volume, overflow level,
pumps[ ], control mode (fixed speed levels, VSD level setpoint).
  - **Pump** — curve (Q–H points), speed, efficiency curve, NPSHr optional, status.

**Rising main** — id, from pump station, to discharge node, profile points (chainage, level —
needed for high points, air valves, and static head), internal diameter, material, roughness
(H-W C or ks), valves (air, scour, NRV, isolating).

**Catchment / load** — polygon or point, linked node, lots by zoning, EP, trade waste points,
infiltration area, development stage/year.

**Scenario** — overrides on any entity + load set + rule-set version.

Every field gets a **data-source/confidence flag** (as-con, GIS, survey, assumed). Existing
network input is where most real-world effort goes, so the model must tolerate partial data
and report what's assumed.

---

## 5. DS rules engine

Design criteria are **data, not code**: a versioned TOML rule set per standard revision,
so a new DS revision is a new file, not a code change. Each check returns pass/fail/warn, the
computed value, the limit, and the **clause reference**, so outputs can go straight into a
design report or variation request.

### DS 50 Ver 2 Rev 4 (2023) — transcribed: `rulesets/ds50_v2r4.toml`

What DS 50 actually specifies for gravity sewers DN150–DN600 (this changes some assumptions
in the first draft of this plan):

- **Loading (4.2.1, Tables 4.1–4.3):** Daily Flow = net area × persons/net ha × L/person/day
  (residential) or net area × L/net ha/day (non-residential). **GSDF = 1.5 × Daily Flow** — a
  flat factor with no peaking curve that depends on population, and infiltration is built in via
  the dry/wet ground rates. Minimum R15. Broadacre net area = (gross − large reserves − major
  roads) × 0.75. Separate tables north/south of 26°S.
- **Hydraulics (4.2.1 l, m, p):** Colebrook-White, ks = 1.0 mm. Max depth at GSDF: ½ full DN150,
  ⅔ full DN225+. Pumped inflow = pump rate + gravity GSDF; receiving sewer ≥ DN225; refer to
  Water Corp if the pumped rate exceeds ⅔ of receiving capacity.
- **Grades (Table 4.4):** min/max grade, max AC/MS spacing and max allowable flow by grade
  band per DN, plus the conditional reductions (DN150 to 1:250, DN225 to 1:350 with ≥ 14 L/s
  pumped; DN225 industrial at 1:200 if GSDF < 3 L/s).
- **No velocity criteria** — self-cleansing is handled by minimum grades, not a minimum
  velocity check. Velocity is reported for information only.
- **Cover (4.9):** 0.9 m road reserve/trafficable ROW, 0.75 m elsewhere, +0.3 m where
  contours are unconfirmed or road design levels are not available.
- **Access chambers (4.10, Tables 4.5, 4.8):** required at changes of grade/direction/DN/class,
  at intersections and at the end of a pressure main; falls through ACs and minimum drop heights.
- **IO/IS sewers (4.2.2):** length and grade limits.

**Check:** Table 4.4 flows reproduce from Colebrook-White (ks 1.0 mm, ID = nominal DN) at the
d/D limits to within ±5% (`tests/benchmarks/ds50_table44_colebrook.csv`). Two entries sit
slightly above the computed capacity (DN450 at 1:500: 110 vs 105 L/s; DN600 at 1:800: 180 vs
177 L/s), so the engine checks **both** the Table 4.4 band limit (governing) and the computed
d/D, and reports any difference between the two.

### Still needed
- **DS 51 / pump station and pressure main standard:** wet-well sizing, starts per hour,
  emergency storage, rising-main velocity and detention limits. Placeholders in
  `rulesets/ds51_TODO.toml` until you supply the document.
- Optional WSA 02 / WSA 04 rule sets for comparison.

---

## 6. Calculation methods

**Flow accumulation (design mode):** build a directed tree with `petgraph`; compute GSDF per
sub-catchment (net area × Table 4.1–4.3 rate) and accumulate downstream. Because DS 50 uses a
linear 1.5 factor, summing GSDF is equivalent to summing Daily Flow — no non-linear peaking
issue. Keep a `LoadingMethod` trait so a non-linear method (WSA, or DS 51 if it differs) can
be plugged in and accumulate the right quantity. At a pump station, the downstream input becomes
the pump rate, not the accumulated GSDF (4.2.1 l). Report flows at each design point,
cumulative, in the 3.5(c) design data format (e.g. `1.3-52.5/180-0.21`).

**Gravity capacity:** Colebrook-White (ks 1.0 mm per DS 50) full-bore capacity; part-full via
circular-section geometry (solve depth for Q by bisection). Report Q/Qfull, d/D, velocity
(information only), and critical/supercritical flags. Manning available for non-DS work.

**Pump station design:** system curve = static (wet-well bottom/stop level to rising-main high
point or discharge) + friction (H-W or Darcy-Colebrook) + minor losses; intersect with pump
curve(s) for single and parallel operation; check duty ≥ design flow, velocity range in the
main, cycle time from wet-well active volume (Vmin = T·Q/4 for single pump), emergency storage.

**Rising main:** detention time at ADWF (septicity), velocity, high points/air valves from
profile, transient surge — flag only in v1 (integration with a surge package is a v2 item).

**Dynamic simulation:** translate model → SWMM .inp (junctions, conduits, storage for wet
wells, pumps type 3/4 curves, force mains, controls from start/stop levels, DWF with diurnal
patterns), run, read back time series. Compare peak HGL vs cover (surcharge/flood), wet-well
max level vs overflow, pump starts/hour, rising-main velocities.

---

## 7. Phased delivery

Each phase ends with something usable. Durations assume one developer part-time-plus; adjust.

| Phase | Deliverable | Indicative |
|---|---|---|
| **0. Foundations** | Cargo workspace, GitHub Actions CI (fmt, clippy, test), core entity structs, GeoPackage schema v1, validation (connectivity, inverts, adverse grades, orphans), CLI `wwnet check`. | 4–6 wks |
| **1. Asset input + QGIS plugin** | PyO3/maturin wheel; QGIS plugin skeleton; GeoPackage template project (layers, styles, attribute forms, value maps for DN/material/R-Code); GIS import with field-mapping templates (Water Corp extracts); auto-connect pipes to nodes by snapping; data confidence flags; long-section dock. Build it around **your example catchment**. | 8–10 wks |
| **2. Design mode – gravity (DS 50)** | Load the DS 50 rule set, Table 4.1–4.3 loading, GSDF accumulation, Colebrook/part-full, all DS 50 checks with clause refs, compliance table back into QGIS as attributes + styled layer, design data plan labels, long section with HGL at GSDF. Benchmarks: Table 4.4 fixture + example catchment hand calcs. | 5–6 wks |
| **3. Pump stations & rising mains (steady)** | Pump curve library, system curve solver, duty point, cycle/storage checks, rising-main checks; EPANET FFI crate for manifolded mains. | 5–6 wks |
| **4. Dynamic simulation** | SWMM FFI crate (cc + bindgen), .inp translator, run manager, results reader, time-series graphs, animated long sections, overflow/surcharge reporting. Validate against an existing ICM model of a known catchment. | 8–10 wks |
| **5. Scenarios & reporting** | Scenario overrides, existing vs ultimate comparison, Excel/PDF report generation, ICM CSV export. | 4–6 wks |
| **6. v2 candidates** | Native Rust dynamic-wave solver behind the engine trait (validated against SWMM), Rainfall-dependent inflow (RTK), calibration to flow-gauge data, H₂S/septicity, surge, VSD control strategies, web front end, PostGIS multi-user. | — |

MVP that's genuinely useful = Phases 0–3 (~6–8 months in Rust; the CLI is usable for design checks from Phase 2, before the GUI is finished): asset register + DS-compliant
steady-state design of gravity and pumped systems.

---

## 8. Validation strategy

1. **Unit benchmarks** — hand calcs for pipe capacity, part-full, system curves (spreadsheet
   reference values committed as test fixtures).
2. **Engine cross-check** — same network in our design mode vs SWMM steady-flow run.
3. **ICM comparison** — at least one existing Water Corp catchment modelled in ICM; compare
   peak flows, HGL, wet-well levels, pump runtime. Agree tolerance up front (e.g. ±5% flow,
   ±50 mm HGL).
4. **DS checks** — one test per rule, citing clause, so an auditor can trace every check.

---

## 9. Risks / issues to flag

- **Loading method differences:** DS 50's flat 1.5× factor accumulates linearly; if DS 51 or
  existing-system planning work uses a non-linear peaking curve, it must be applied to
  accumulated population, not summed flows. Handled by the `LoadingMethod` trait — test hard.
- **QGIS Python ABI / distribution:** the plugin's Rust wheel must load in QGIS's bundled
  Python (Windows OSGeo4W, macOS bundle). Use abi3 wheels, build in CI for Windows x64 first,
  and test inside QGIS LTR, not just system Python.
- **SWMM limitations vs ICM:** SWMM's surcharge handling (no Preissmann slot by default in older
  builds — use 5.2 with slot option), wet-well/pump interactions at small timesteps, and
  instability with very short steep pipes. Mitigation: auto-lengthening of short conduits and
  timestep controls in the translator; validation in Phase 4.
- **Pressure networks with multiple pump stations into a common main** are where steady-state
  hand methods break down — needs EPANET or the dynamic run, not a single-station system curve.
- **Data quality of existing assets** (missing inverts, unknown pump curves) will dominate
  effort. Build interpolation/inference tools and confidence flags early (Phase 1), not late.
- **DS revisions** — rule sets must be versioned and recorded against each run for audit.
- **Licensing/IP** — this is "ICM-like", not an ICM clone of its UI/branding; avoid copying ICM
  file formats beyond documented CSV import/export.

---

## 10. Repository layout (Cargo workspace)

```
Cargo.toml                # [workspace]
crates/
  wwnet-core/             # entities, IDs, validation, petgraph topology, GeoPackage I/O
  wwnet-loading/          # EP, peaking, infiltration, diurnal patterns
  wwnet-hydraulics/       # gravity part-full, system curves, duty point, rising main checks
  wwnet-rules/            # rules engine (reads rulesets/*.toml)
  wwnet-swmm-sys/         # vendored SWMM 5.2 C source, build.rs (cc + bindgen)
  wwnet-epanet-sys/       # vendored EPANET 2.2 C source, build.rs
  wwnet-engines/          # safe HydraulicEngine trait, .inp writer, results reader
  wwnet-io/               # shapefile/CSV import mapping templates, ICM CSV export
  wwnet-reports/          # Excel, Typst PDF, long-section plots
  wwnet-cli/              # `wwnet check`, `wwnet design`, `wwnet simulate`
  wwnet-py/               # PyO3 bindings -> `wwnet` Python wheel (maturin, abi3)
qgis_plugin/wwnet/        # PyQGIS plugin: metadata.txt, docks, processing provider
templates/                # GeoPackage template + QGIS styles (.qml) + import field maps
rulesets/                 # ds50_v2r4.toml, ds51_TODO.toml, wsa02.toml
tests/benchmarks/         # hand-calc fixtures (TOML/CSV)
examples/<catchment>/     # pilot catchment data + expected results
docs/
```

---

## 11. Example catchment — data to prepare

The example catchment drives Phases 1–2 and becomes the regression test. Ideal contents
(shapefile, GeoPackage or CSV are all fine; MGA2020 Zone 50 / GDA2020 preferred):

| Layer | Fields |
|---|---|
| Access chambers / maintenance shafts | AC no., type (AC/MS/drop), easting, northing, surface level, invert(s), status (existing/proposed) |
| Gravity sewers | US AC, DS AC, DN, material/class, US & DS invert, length, location (road reserve / lot / ROW), status |
| Sub-catchments | polygon, outlet AC, net area (ha), R-Code or land use, dry/wet ground, north/south of 26°S |
| Pump station (if any) | wet-well AC, pump rate (L/s), pressure main discharge AC, inflow area |
| Ground levels | surface levels at ACs at minimum; DEM optional (used for cover checks along the pipe) |
| Expected results | your hand-calc or existing design-data-plan flows and pipe sizes, to compare against |

Ten to forty ACs with one branch junction, one DN change, and ideally a pumped inflow covers
every DS 50 check. Put it in `examples/<name>/` (or keep it out of git if it is
Water Corp-restricted — the tests can point to a local path).

---

## 12. Immediate next steps

1. ~~DS 50 parameters~~ — done (`rulesets/ds50_v2r4.toml`). Supply DS 51 / pump station
   standard when available.
2. Prepare the example catchment (section 11).
3. Phase 0: Cargo workspace, CI, core structs, GeoPackage schema, `wwnet check`; plus the
   `wwnet-hydraulics` Colebrook part-full function passing the Table 4.4 benchmark.
