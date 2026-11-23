# WWNet: session handover (2026-11-23)

Branch: `ccr-7bd083b1-j0qlhq` (all work pushed). Earlier branch: `ccr-974cdaeb-c682le`. Plan: `docs/PLAN.md`.
This session: project review (`docs/REVIEW-2026-11.md`), then the review's recommendations implemented
(sections 2 and 3). **QGIS: the user said to disregard it** (frozen; see section 6).

## 1. User and working style

- Water utility engineer (Water Corporation, WA). Australian spelling. Direct, no fluff. For procedures: one step at a
  time, wait for confirmation.
- **Priority: flow modelling first.** DS 50 compliance is secondary.
- Commit **and push** before ending a turn. Use `WIP ... do not use this build` commits while work is mid-flight.
- Earlier sessions: the user asked for Sonnet/Haiku sub-agents where possible (Opus orchestrates, verifies, commits).

## 2. Work PC constraints (unchanged)

- ArcGIS Pro 3.x, Python **3.11.8**; **cannot run .exe**; cannot clone conda envs. Everything ships as the
  no-install bundle `wwnet-arcgis-bundle-win-py311` (CI artifact): `WWNet-ArcGIS/` + `vendor/` (wwnet_native .pyd,
  anthropic + deps) put on `sys.path` by `wwnet_arcgis/vendor.py`.
- Install routine: close Pro, download latest green CI artifact, Unblock outer zip, extract, Unblock inner
  `wwnet-arcgis-0.1.0.zip`, extract, replace `C:\Users\lawrend0\Desktop\WWNet\WWNet-ArcGIS`, reopen Pro.
- Source GIS is GDA94 geographic (EPSG:4283); import uses `GDA_1994_To_GDA2020_1`. No API key set yet.

## 3. What changed this session (commits `2573433`, `b73681d`, + docs commit)

**Engine (Rust), schema v3** (`templates/schema.sql`, `gpkg::upgrade` / `ensure_current` upgrade v1/v2 files):
- `wwn_issues` has `source` (check | design | import | simulate) + POINT geometry; each run replaces only its own rows.
  **Fixes design issues accumulating on every run** (verified 10 -> 20 -> 30 before).
- `wwn_results_conduits` is a feature table (conduit geometry copied in) with every displayed field (dn, adwf, gsdf,
  pumped, total, capacity, table44, hydraulic, d/D, velocity, result, worst_message). Old layout is dropped and
  recreated on upgrade (derived data).
- Simulation: run `label` + `summary_json`; map-layer tables `wwn_sim_conduit_results` (d_class), `wwn_sim_node_results`
  (freeboard), `wwn_sim_compare`; services `list_runs`, `show_run`, `compare_runs`, `read_series`
  (`crates/wwnet-sim/src/service.rs`); structured `issues` (plus `warnings` strings kept); per-pump-station
  `PUMP_STATION_NOT_SIMULATED` warning with the ADWF lost. **Default roughness is now `ks`** (was Manning 0.013).
- Shared loads module `crates/wwnet-design/src/loads.rs` (node loads + sub-catchments) used by design and sim.
- Trace from a node **or conduit**: returns `conduits` on the path and `summary` (upstream nodes / conduits /
  connections / sub-catchments, ADWF as simulated, design GSDF, pumped).
- Settings services: `get_settings`, `set_settings`, `assign_profile`; `store_issues_json` (import issues).
- Issue codes UPPER_SNAKE everywhere (design `design_x` -> `X`, importer codes upper-cased).
- `wwnet_native` new functions: `pipe_capacity` (any DN), `store_issues`, `sim_runs`, `show_sim_run`,
  `compare_sim_runs`, `sim_series`, `get_settings`, `set_settings`, `assign_profile`, `schema_version`,
  `SCHEMA_VERSION`; `simulate(..., label="")`; `trace` returns conduits + summary.
- CLI (dev only): `simulate --label`, `runs`, `compare`, `trace` summary, hidden `demo --flows`.
- `examples/demo/demo.gpkg` regenerated (v3); new `examples/demo/demo_flows.gpkg` (12 connections per manhole +
  example profile as default) for flow tests.

**Shared Python core** (`qgis_plugin/wwnet/core`, shipped to ArcGIS as `wwnet_shared`):
- Deleted pure-Python `hydraulics.py`, `rules.py`, GSDF fallback and every `wwnet.exe` fallback. `native.require()`
  raises `NativeMissing` with install help; `native.missing_functions()` used by Verify Setup.
- New `calc.py` (pipe capacity / check_conduit via native), `summary.py` (prose result lines: design, sim, runs,
  compare, trace, issues), `style.py` (one palette).
- `schema.create_model` / `upgrade_model` go through the engine. Importer no longer repeats MISSING_INVERT /
  ADVERSE_GRADE / INVERT_ABOVE_SURFACE (the model check reports them); `ImportReport.issues()` for the Issues layer.
- Assistant: flow tools `design_summary`, `get_design_result`, `list_sim_runs`, `get_sim_result`,
  `compare_sim_runs`, `trace_network` (`assistant/tools.py::flow_handlers`); prompt covers flow modelling.

**ArcGIS toolbox** (`arcgis_toolbox/WWNet.pyt`, `wwnet_arcgis/`):
- `_Tool` base class: parameters by name, vendor re-activated in the one `execute`, model parameter `gpkg`
  **defaults to the model in the active map**, derived output `out_gpkg` for ModelBuilder chaining.
- 17 tools. Setup: Verify Setup, Set Assistant API Key (CLI path removed). Model: New Model, Add Model To Map,
  **Model Settings**. Data: Import Sewer Data, Import Diurnal Profile, **Assign Profile**. Analysis: Check Model,
  Run Design, Run Simulation (label, series for selected conduits, ks default), Trace (start blank = selected
  conduit/node; upstream flow summary). Results: **Show Simulation Run, Compare Simulation Runs, Plot Hydrograph**
  (CSV + best-effort `arcpy.charts.Line`). Calculators: Pipe Capacity (any DN). Assistant: Ask WWNet Assistant
  (+ **Apply the proposals from the last run**: deterministic, no API call, refuses if data changed).
- Result layers are the persistent GeoPackage tables (`maplayers.ensure_layer` reuses or adds + styles); memory FCs,
  `cli.py`, `issues_table.py` deleted; `engine.py` wraps native calls (`EngineError`).
- `maplayers.py` is the single layer-resolution module: `layer_source` falls back to `connectionProperties`,
  `same_file` comparison, `models_in_map`, `default_model`, `resolve_model`, `selected_ids`, `select_in_model`.
  Run Design selects failed conduits once (results layer). Messages are prose (no fixed-width tables).
- `package.py --cli` removed.

Tests at last run: Rust workspace all green (fmt + clippy clean); ArcGIS toolbox 115 passed; shared/QGIS-side 92
passed. Python 3.11 `compileall` + `pyflakes` clean.

## 4. Not done / needs the user or real Pro

1. **Docs**: `README.md` and `arcgis_toolbox/README.md` still describe the old tools (CLI fallback, memory layers,
   manning default, old trace). **Update them first next session** (tool table in section 3 above is current).
   `docs/REVIEW-2026-11.md` has no status column yet: add done / partial / open per item (section 5 below).
2. **Open bug from last session (top priority, still unverified):** "model layer not in the active map". Now handled
   in one place (`maplayers.layer_source` with `connectionProperties` fallback + `same_file`). Still need the user's
   paste of: `import arcpy; m=arcpy.mp.ArcGISProject("CURRENT").activeMap; print([(l.name, l.supports("DATASOURCE"), l.dataSource if l.supports("DATASOURCE") else None, getattr(l, "connectionProperties", None)) for l in m.listLayers() if l.supports("NAME")])`
   then add a regression test with the real string.
3. **Untested in real Pro** (fake arcpy only): `ensure_layer` reuse/refresh of GeoPackage feature tables after the
   engine rewrites rows (may need a map refresh / re-add), `definitionQuery` on the nodes results layer,
   `arcpy.charts.Line(... splitCategory=...)` signature, `Parameter.altered` / `enabled`, `SetProgressor`,
   `connectionProperties` shape for GeoPackage layers, upgrade dropping `wwn_results_conduits` while a v2 layer of
   it is in the map (remove old result layers first).
4. **Behaviour change to tell the user:** simulation default roughness is now `ks` (Colebrook, as design); their
   verified run (peak 3.07 L/s @ 07:40) used Manning 0.013. Old models upgrade to schema v3 on first use.
5. `.lyrx` symbology files (review U3) not authored (needs Pro); code symbology now covers every layer from the
   shared palette.
6. Pending from the user (unchanged): real ICM diurnal profile export; pressure-side data (rising mains, pump
   stations); DS 51 if wanted.

## 5. Review item status (`docs/REVIEW-2026-11.md`)

Done: B1, B3, B4, B5 (code side), D1-D3, D5-D9, D11-D17, U1, U2, U4, U5, U6, U7, U8, U9, U10, U11, U12, U13, U14, U15,
I2, I3, I4 (warning only; pressure side still not simulated), I5, I6.
Partial: D4 (`tomlmini` already uses `tomllib` on 3.11; fallback kept), D10 (assistants share `conduit_derived`;
labels keep their own expression), U3 (no `.lyrx`), U16 (reminder line only; edit state not detected), I1 (shared
`summary.py` extracted; QGIS frozen, user said disregard).
Open: B2 / README rewrite (section 4.1).

## 6. QGIS plugin

Frozen at the user's request ("disregard qgis"). It still works on the new core: New / Open (upgrades to v3 and
loads the result and issue layers with the shared palette), Check (issues from native, stored-table fallback),
assistant with the flow tools. No import / design / simulate UI. Don't spend time on it unless asked.

## 7. Next steps (suggested order)

1. README updates (4.1) and a status column in the review doc.
2. Ask the user for the layer one-liner (4.2); install the latest bundle and test in Pro: Add Model To Map on an
   existing model (upgrade), Run Design, Run Simulation (label), Show / Compare runs, Plot Hydrograph, Trace from a
   selected pipe, Model Settings, Assign Profile, Import Sewer Data (Issues layer).
3. Real ICM profile import and peak comparison against ICM.
4. Pressure side: import + SWMM pumps / force mains (the loads module and per-PS warning are the hooks).
5. Long-section view.

## 8. Gotchas (ArcGIS Pro)

- Pro resets `sys.path` before each tool run: the `_Tool.execute` base re-activates `vendor/`; never cache a failed
  native import.
- `parameterDependencies` must be parameter **names**.
- `arcpy.AddError` marks the tool failed: data issues go out as warnings (`ERROR:` prefix).
- Unsupported Layer properties raise: always use `maplayers.layer_name` / `layer_source`.
- Parameter validation must not write files: `WWNet.pyt` uses the read-only helpers in `gpkg.py` (`meta`,
  `profile_ids`, `series_ids`, `run_titles`), never engine calls that upgrade.
- Check new Python with Pro's 3.11: `/usr/bin/python3.11 -m py_compile arcgis_toolbox/WWNet.pyt`.
- The fake arcpy is permissive: when a real-Pro failure is found, make the fake stricter and add a test.

## 9. Dev commands

```bash
cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
# venv (py3.11) in the session scratchpad: python3.11 -m venv venv && venv/bin/pip install maturin pytest anthropic pyflakes
(cd crates/wwnet-py && maturin build --release -i <venv>/bin/python) && <venv>/bin/pip install --force-reinstall target/wheels/wwnet_native-*.whl
pytest -q qgis_plugin/tests
(cd arcgis_toolbox && WWNET_EXE=../target/debug/wwnet python -m pytest -q tests)   # WWNET_EXE only for the CLI parity test
python -m pyflakes arcgis_toolbox/WWNet.pyt arcgis_toolbox/wwnet_arcgis
cargo run -p wwnet-cli -- demo --flows /tmp/f.gpkg   # flow-modelling demo model
```
CI (`.github/workflows/ci.yml`): Rust (ubuntu + windows), plugin, arcgis-toolbox, Windows wheel, arcgis-bundle
(`wwnet-arcgis-bundle-win-py311`). CI has not been checked for the two WIP commits yet: check the run for this branch.
