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
- Loading: equivalent population (EP) / lot-based ADWF, peaking, groundwater infiltration and
  storm-allowance per Water Corporation method; diurnal profiles for time-varying runs.
- Two analysis modes:
  1. **Design (steady-state)** — PDWF/PWWF accumulated down the tree, pipe sizing and
     constraint checks (capacity, velocity, grade, depth, d/D, pump duty point).
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
CI, and a desktop GUI on top. No Python in the runtime.

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
| GUI | **Tauri 2** desktop app: Rust backend + MapLibre GL JS map with editing (TypeScript front end). Alternative: `egui`/`eframe` (pure Rust, but map editing/snapping is far less mature) |
| Charts / long sections | `plotters` for report images; a JS chart lib in the GUI |
| Reports | `rust_xlsxwriter` for Excel; Typst templates for PDF reports |
| Tests | `cargo test`, `approx` for float tolerance, `insta` snapshot tests for compliance tables and .inp output, `proptest` for hydraulic invariants (e.g. Q monotonic with depth up to ~0.94D) |

**Why the C engines are still worth keeping:** SWMM 5.2 and EPANET 2.2 are small, plain C,
public domain and compile cleanly with the `cc` crate, so FFI is low risk. Put them behind a
`HydraulicEngine` trait; a native Rust dynamic-wave solver can then replace SWMM later
(v2+) and be validated against it run for run.

**QGIS fallback:** if staff want to keep working in QGIS, expose the core with `PyO3`/`maturin`
as a Python wheel and write a thin QGIS plugin. The core design doesn't change — this is why
the core stays a GUI-free library.

**Learning curve:** Rust is a big step up from beginner-to-intermediate Python — the borrow
checker, traits and FFI will slow Phase 0–1 noticeably. The core calculation code is a good
fit for Rust (pure functions over data); the GUI and FFI layers are the hard parts. Durations
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
so a new DS revision is a new file, not a code change.

```toml
standard = "DS51"
revision = "<to confirm>"

[loading]
adwf_per_ep_lps = "<value>"
peaking_factor  = { method = "<formula/table>" }
infiltration    = {}
storm_allowance = {}

[gravity]
min_velocity_mps    = "<value>"   # self-cleansing at PDWF or nominated flow
max_velocity_mps    = "<value>"
max_d_over_D_at_pwwf = "<value>"
min_grade_by_dn     = { 150 = "<1 in x>", 225 = "<1 in x>" }
min_cover_m         = { road = "<value>", verge = "<value>", lot = "<value>" }

[pump_station]
emergency_storage_hours_at_adwf = "<value>"
max_starts_per_hour             = "<value>"
min_cycle_time_min              = "<value>"

[rising_main]
min_velocity_mps      = "<value>"
max_velocity_mps      = "<value>"
max_detention_time_hr = "<value>"
```

Each check returns pass/fail/warn, the computed value, the limit, and the **clause reference**,
so outputs can go straight into a design report or variation request.

> **Action before Phase 2:** populate the rule set from your controlled copies of DS 50 / DS 51
> (and the sewer pump station standard). I couldn't retrieve DS 51 itself, so no numeric values
> are assumed in this plan. WSA 02 / WSA 04 defaults can be added as a second rule set for
> comparison.

---

## 6. Calculation methods

**Flow accumulation (design mode):** build a directed tree with `petgraph`; accumulate EP
downstream (not flows — peaking factor must be applied to accumulated EP, otherwise you
over-estimate). At pump stations, the downstream contribution becomes the pumped rate (duty
flow), not the accumulated PWWF.

**Gravity capacity:** Colebrook-White full-bore capacity; part-full via circular-section
geometry (solve depth for Q by bisection). Manning as an option. Report Q/Qfull, d/D,
velocity at PDWF and PWWF, and critical/supercritical flags.

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
| **1. Asset input** | GIS import with field-mapping templates (Water Corp extracts); Tauri app with MapLibre map: create/edit nodes, pipes, pump stations, rising mains, snapping; data confidence flags; long-section viewer. | 10–12 wks |
| **2. Design mode – gravity** | DS rule set (TOML), EP loading, accumulation, Colebrook/part-full, compliance table and long section with HGL at PWWF. Benchmark vs hand calcs. | 5–6 wks |
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

- **Peaking-factor application** on accumulated EP vs flows — easy to get wrong, big effect on
  sizing. Encode once in the loading engine, test hard.
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
  wwnet-rules/            # rules engine + rulesets/ds50.toml, ds51.toml, wsa02.toml
  wwnet-swmm-sys/         # vendored SWMM 5.2 C source, build.rs (cc + bindgen)
  wwnet-epanet-sys/       # vendored EPANET 2.2 C source, build.rs
  wwnet-engines/          # safe HydraulicEngine trait, .inp writer, results reader
  wwnet-io/               # shapefile/CSV import mapping templates, ICM CSV export
  wwnet-reports/          # Excel, Typst PDF, long-section plots
  wwnet-cli/              # `wwnet check`, `wwnet design`, `wwnet simulate`
app/                      # Tauri 2 desktop app (src-tauri + MapLibre front end)
tests/benchmarks/         # hand-calc fixtures (TOML/CSV)
docs/
```

---

## 11. Immediate next steps

1. Confirm the DS documents and revisions to encode (DS 50, DS 51, pump station standard) and
   supply the parameter tables.
2. Pick a pilot catchment with an existing ICM model and GIS extract for validation.
3. Confirm front end (Tauri desktop app recommended; QGIS via PyO3 as fallback) and start Phase 0.
