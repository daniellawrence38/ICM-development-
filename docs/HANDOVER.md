# WWNet — session handover (2026-11-20)

Branch: `ccr-974cdaeb-c682le` (all work pushed; last commit `d900db8`). Plan: `docs/PLAN.md`. OSS survey: `docs/reference-survey.md`.

## 1. User and working style

- Water utility engineer (Water Corporation, WA). Australian spelling. Direct, no fluff. For procedures: one step at a time, wait for confirmation.
- Asked for Sonnet/Haiku sub-agents wherever possible (Opus orchestrates, verifies, commits).
- **Priority changed:** flow modelling first. DS 50 compliance is secondary ("don't really care about DS50 compliance, as long as it can run the flow modelling").
- A stop hook requires every change to be committed **and pushed** before ending a turn. Use clear `WIP ... do not use this build` commits while agents are mid-flight.

## 2. The user's work PC (hard constraints)

- ArcGIS Pro 3.x, Python **3.11.8**. Pro's env has `anyio 4.2.0`, `typing_extensions 4.9.0`, `sniffio 1.3.0`, `idna 3.4`, `certifi 2024.2.2`, `truststore 0.7.0`. pydantic/httpx missing.
- **Cannot run .exe files.** **Cannot clone conda envs** (proxy stalls at ~72 MB). → Everything ships as the no-install bundle: `WWNet-ArcGIS/` with `vendor/` (wwnet_native .pyd + anthropic + deps) put on `sys.path` by `wwnet_arcgis/vendor.py`.
- Install path: `C:\Users\lawrend0\Desktop\WWNet\WWNet-ArcGIS` (Desktop may be OneDrive-redirected; models kept in `...\Desktop\WWNet\models\`).
- Install routine the user knows: close Pro → download CI artifact **`wwnet-arcgis-bundle-win-py311`** from the latest green run → Unblock outer zip → extract → Unblock inner `wwnet-arcgis-0.1.0.zip` → extract → replace folder → reopen Pro.
- Source GIS layers are **GDA94 geographic (EPSG:4283)**; ArcGIS has no NTv2 grids installed, so the import uses `GDA_1994_To_GDA2020_1`.
- AI assistant: user has **not** set an API key yet (skipped). Verify Setup shows `anthropic` and `wwnet_native` import OK.

## 3. Architecture (what exists)

| Path | Role |
|---|---|
| `crates/wwnet-core` | Model structs, GeoPackage I/O (schema v2, `gpkg::upgrade` for v1), topology (petgraph), validation |
| `crates/wwnet-hydraulics` | Colebrook-White / Manning part-full |
| `crates/wwnet-rules` | DS 50 rule set (`rulesets/ds50_v2r4.toml`), clause-referenced checks, `CheckResult::{Pass,Info,Warn,Fail}` |
| `crates/wwnet-design` | Steady design: loading (subcatchments, node loads, manual ADWF), GSDF = ADWF × 1.5, checks; `service.rs` = JSON services shared by CLI + Python |
| `crates/wwnet-swmm-sys` | Vendored EPA **SWMM 5.2.4** C (public domain), built with `cc` (MSVC + gcc), hand-written FFI |
| `crates/wwnet-sim` | .inp writer, runner (global mutex), results; `service.rs` simulate → `wwn_sim_*` tables |
| `crates/wwnet-cli` | `wwnet new/check/trace/design/simulate/profile import` (optional; exe can't run at work) |
| `crates/wwnet-py` | PyO3 abi3 module **`wwnet_native`**: check, design, trace, simulate, write_model, import_profile, new_model, part_full_flow_lps, ... |
| `qgis_plugin/wwnet/core/` | Pure-Python shared code (importer, rules fallback, native probe) — reused by ArcGIS as `wwnet_shared` |
| `qgis_plugin/wwnet/assistant/` | Claude assistant engine (manual tool loop, approval-gated edits) |
| `arcgis_toolbox/WWNet.pyt` + `wwnet_arcgis/` | ArcGIS Pro toolbox (11 tools, below) |
| `templates/schema.sql` | GeoPackage schema v2 (EPSG:7850) |
| `templates/import/watercorp_sewer.toml` | Field-role defaults + code maps for Water Corp import |
| `templates/profiles/example_residential.csv` | **APPROXIMATE** diurnal profile digitised from a screenshot |

ArcGIS tools: Import Pressure Data (2026-11-23, `wwnet_arcgis/pressure_import.py`: SewerPumpStation + SewerPressureMain -> wwn_pump_stations / wwn_rising_mains, chains segments from the PS, discharge = nearest gravity node, wet well = terminal gravity node; UPSTREAM/DOWNSTREAM levels ignored (user: unreliable); re-run keeps hand-entered pump data; pump hydraulics are in a separate corporate system -> manual entry), Self Test (2026-11-23, `wwnet_arcgis/selftest.py`: runs every tool on templates/demo/demo.gpkg in a temp folder, writes WWNet_selftest_report.txt — ask the user to run it after each update and paste the report), Update WWNet (2026-11-23, `wwnet_arcgis/updater.py`: applies the `wwnet-arcgis-update` CI artifact in place, modules purged per run via `_impl()` in WWNet.pyt, locked native files staged as `.pending` and swapped in `vendor.activate`; commit c3e5f7a is complete despite its WIP title), Export To ICM (new 2026-11-23, `wwnet_arcgis/icm_export.py`; not yet tried in Pro/ICM), New Model, Import Sewer Data, Import Diurnal Profile, Add Model To Map, Check Model, Run Design, Run Simulation, Trace, Pipe Capacity, Set Assistant API Key, Ask WWNet Assistant, Verify Setup.

## 4. Verified in real ArcGIS Pro vs not

Verified by the user: toolbox loads; Verify Setup (vendor imports OK); Add Model To Map (demo); Run Design (calc + messages); **Import Sewer Data** on a 61-sewer selection (final run: 0 unknown directions, 25 tees, model check clean); **Run Simulation** engine (86 conduits, 7.7 s, continuity 0.07 %, ADWF 1.12 L/s, peak 3.07 L/s @ 07:40).

Not yet working / unverified in Pro:
- **FIXED 2026-11-23 (user confirmed Trace works):** real Pro 3.x GeoPackage `layer.dataSource` is `Instance=C:\...\import_test.gpkg,Dataset=main.%wwn_nodes` (pasted by the user). `gpkg.split_datasource` now parses this (plus the SQLite connection-string form seen on mobile-gdb layers); arcpy reads go through `gpkg.dataset_path`. Regression tests use the real string.
- (was) **OPEN BUG:** after the latest build (`d900db8`), Trace/Run Simulation complete but report the model layer is "not in the active map", so nothing is selected / no results layer. Likely `maplayers.find_layer_for` not matching Pro's `layer.dataSource` string for GeoPackage layers (format differs from `gpkg.split_datasource` regex `...\.gpkg\main.<table>`), or `layer.supports("DATASOURCE")` returning False. **First step next session:** ask the user to paste the output of this one-liner in Pro's Python window, then fix `gpkg.split_datasource` / `maplayers.layer_source` and add a test with the real string:
  ```python
  import arcpy; m=arcpy.mp.ArcGISProject("CURRENT").activeMap; print([(l.name, l.supports("DATASOURCE"), l.dataSource if l.supports("DATASOURCE") else None) for l in m.listLayers() if l.supports("NAME")])
  ```
- Design/Simulation results layers (memory FC + symbology), Import Diurnal Profile tool, assistant live API call, DPAPI key storage, edit sessions — all fake-arcpy tested only.

## 5. Water Corporation data facts (learned from the user)

- Access chambers: `PACID` (id), `TOPLEV` (lid, m AHD), `CHAMBER_TY`: M = manhole → `ac`, J = maintenance shaft → `ms`, I = inspection shaft → skipped (no PACID).
- Connection points: `ID`. 846,956 in the layer — never full-scan (id + extent filters).
- Gravity sewers: `ID`, `US_MANHOLE_ID`/`DS_MANHOLE_ID` = PACID, `UPSTREAM`/`DOWNSTREAM` = invert levels m AHD, `HEIGHT` = diameter mm, `LENGTH` m (= geometry length), `FLOW_GRADE` 1-in-x (informational; calculated grade from levels takes preference; median ratio levels/FLOW_GRADE ≈ 1.089, cause unknown), `MAINTYPE`: R retic, B branch, M main, E = **inspection shaft (IS) sewer** (no upstream chamber), `STATUS` A = actual, `MATERIAL` "PVC- ...", "VC- ...".
- Runs are **segmented** between chambers (all segments carry the run's US/DS ids); IS sewers often **tee mid-line** into another sewer with no chamber; the GIS is drawn **downstream → upstream** (all 61 reversed). Falls through manholes are normal.
- Loading agreed: **630 L/day per connection** (3.5 × 180) as ADWF; **× 1.5 peaking** = GSDF for capacity; diurnal profile for dynamic runs. In ICM the user enters a **manually calculated ADWF per catchment** → supported via `wwn_subcatchments.adwf_lps` + `profile_id`, and `wwn_node_loads` (source `manual` / `connection_points`). Lot-size/zoning loading = future revision.

## 6. Pending from the user

1. Paste of the layer dataSource one-liner above (for the open bug).
2. Real ICM diurnal profile export ("1 Dunkley Ave Applecross PS4", Design, 5-min, 288 rows; copy the Time/Factor table).
3. **Pressure side** data (rising mains, pump stations: wet well, pumps/curves, start/stop levels) — schema tables exist (`wwn_pump_stations`, `wwn_pumps`, `wwn_rising_mains`); importer and SWMM pump/force-main export not built yet.
4. DS 51 (pump stations/pressure mains) if still wanted — `rulesets/ds51_TODO.toml` placeholder.

## 7. Suggested next steps (in order)

1. Fix the "not in the active map" bug (section 4) → re-test Trace + Run Simulation results layer.
2. Import the real ICM profile; compare one catchment's simulated peak against ICM.
3. Pressure side: import tool for rising mains + pump stations; SWMM export with wet well storage nodes, pump curves (type 3/4), on/off level controls, force mains (D-W/H-W); pump starts/hr and wet-well results.
4. Long-section view (HGL from simulation/design) in ArcGIS.
5. Lots + zoning loading (DS 50 4.2.1(j) net area) and the "select pipe → upstream connections / flow" trace summary the user asked for.
6. DS 50 extras (IS sewer rules 4.2.2, max-grade exemption) — low priority now.

## 8. Gotchas (ArcGIS Pro, learned the hard way)

- Pro **resets `sys.path` before each tool run** → `_vendor.activate(_HERE)` at the top of every `execute`; never cache a failed native import.
- `parameterDependencies` must be parameter **names**, not indices (else "tool has failed to open").
- `arcpy.AddError` marks the tool **failed** → data issues go out as warnings (`ERROR:` prefix).
- Unsupported Layer properties raise (`AttributeError: The attribute 'name' is not supported on this instance of Layer`) — always use `maplayers.layer_name/layer_source`.
- Pro's Python is 3.11: check new Python with `/usr/bin/python3.11 -c "import ast; ast.parse(...)"`.
- The fake arcpy is permissive — when a real-Pro failure is found, make the fake stricter and add a regression test.
- Standalone map tables: `Map.removeTable()`, never `removeLayer()` (fake now enforces it; `maplayers.remove_item`).
- Each geoprocessing tool call costs 2-6 s on the user's PC (Self Test 2026-11-23: CreateFeatureclass 2.3-2.8 s, AddFields 5.5-6.2 s, row copy 0.5 s); minimise GP calls. Suspect: their project has ~90 network-share layers — blank-project test pending.
- Multi-line code in Pro's Python window needs an extra Enter; give users one-liners.

## 9. Dev commands

```bash
cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
# Python venvs in the session scratchpad (recreate if gone): venv (maturin), venv-plugin (pytest, anthropic, wwnet_native wheel)
(cd crates/wwnet-py && maturin build --release) && pip install --force-reinstall target/wheels/wwnet_native-*.whl
pytest -q qgis_plugin/tests
(cd arcgis_toolbox && WWNET_EXE=../target/debug/wwnet python -m pytest -q tests)
python -m pyflakes arcgis_toolbox/WWNet.pyt arcgis_toolbox/wwnet_arcgis   # CI lint gate: run it before pushing (and tell agents to)
```
CI (`.github/workflows/ci.yml`): Rust (ubuntu + windows), plugin, arcgis-toolbox, Windows wheel, and **arcgis-bundle** (builds `wwnet-arcgis-bundle-win-py311` + smoke test with Pro's pinned package versions). Check runs with the GitHub MCP `actions_list` (owner `daniellawrence38`, repo `icm-development-`, branch filter).
