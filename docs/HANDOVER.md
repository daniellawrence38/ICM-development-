Branch: **`main`** (default; the 2026-11-26 session's `ccr-f18eb556-rbdbtr` is merged into it by PR, section 15;
the 2026-11-25 session's `ccr-153a547e-mtrc3t` before it, section 14). It was restarted from `ccr-7bd083b1-j0qlhq`
(38f46e2), which holds BOTH earlier lines of work: the 2026-11-23/24 ArcGIS session (`ccr-54bf38a8-b92qd9`: pressure
side, Export To ICM, Self Test, Update WWNet; sections 2-11) and the 2026-11-23 review session (schema v3 / toolbox
UX; **section 12**), merged on 2026-11-24. All older branches (`ccr-7bd083b1-j0qlhq`, `ccr-54bf38a8-b92qd9`,
`ccr-472a9a6a-he69zu`, `ccr-974cdaeb-c682le`) are contained in this one: continue here.
**Start here: section 16 (gravity connectivity to outfalls, 2026-11-26, where we stopped), then 15 (existing demand
from land use), 14 (pump station register import), 13 (antivirus, assistant removed), 12 (what the merge changed) and 11.**
**Session workflow (from 2026-11-25):** `main` holds the latest working code (created at 0b59caa; the user is
setting it as the GitHub default branch, replacing `ccr-974cdaeb-c682le`). New sessions start from `main` in the
personal cloud environment `ICM-development-`, whose setup script builds `.venv` (pytest, pyflakes, maturin,
anthropic, openpyxl + the wwnet_native wheel) and `target/debug/wwnet` (gate: `.venv/bin/python -m pytest ...`;
unproven until the first fresh session). End of session: everything pushed, this file's "Where we stopped"
updated, CI green, then open a PR from the session branch into `main` for the user to merge.
Plan: `docs/PLAN.md`. Review: `docs/REVIEW-2026-11.md`. OSS survey: `docs/reference-survey.md`. Deck: `docs/WWNet_functions.pptx`.

## 1. User and working style

- Water utility engineer (Water Corporation, WA). Australian spelling. Direct, no fluff. Procedures: **one step at a time**, wait for confirmation. Give Pro Python-window commands as **one-liners**.
- **Wants Sonnet/Haiku sub-agents wherever possible**; Opus orchestrates, writes precise briefs (files each agent may touch, verify commands), reviews diffs, runs the checks itself and commits. Run parallel agents only on disjoint files.
- Priority: **flow modelling** (and getting models into InfoWorks ICM). DS 50 compliance is secondary.
- A stop hook requires every change committed **and pushed** before a turn ends. While agents are mid-flight push `WIP ... - do not use this build` commits and tell the user not to install from them; after review add a normal commit saying the work is complete.

## 2. The user's work PC (hard constraints)

- ArcGIS Pro **3.3.5**, Python **3.11.8**. Pro env pins `anyio 4.2.0`, `typing_extensions 4.9.0`, `sniffio 1.3.0`, `idna 3.4`, `certifi 2024.2.2`, `truststore 0.7.0`.
- **Cannot run .exe**, **cannot clone conda envs**, **cannot install the Claude desktop app** (so no Remote Control into their PC). Everything ships as the no-install bundle `WWNet-ArcGIS/` (+ `vendor/` with `wwnet_native` .pyd, anthropic, deps).
- Install path `C:\Users\lawrend0\Desktop\WWNet\WWNet-ArcGIS`; models in `...\Desktop\WWNet\models\` (not OneDrive-synced).
- InfoWorks ICM **2024.5.0**. ODIC field mapping works; the user saved ODIC configs.
- Source GIS: GDA94 geographic (EPSG:4283); model CRS GDA2020 / MGA 50 (EPSG:7850).
- Large Pro project (~90 layers on network shares). **Geoprocessing history/metadata logging made every GP call cost 2–6 s** there → all tools now run inside `gpenv.quiet(arcpy)` (Export To ICM 265 s → 23 s).
- AI assistant: API key not set (not needed so far).

## 3. Install / update workflow (new this session)

- **Python-only change** → CI artifact **`wwnet-arcgis-update`** (~120 KB, one zip layer, job `arcgis-update`, < 1 min) → user runs **Update WWNet** on the downloaded zip. No extract, no Unblock, no restart (modules are purged and re-imported on every tool run via `_impl()` in `WWNet.pyt`). If `WWNet.pyt` changed: right-click toolbox › Refresh.
- **Native (Rust) change** → full bundle **`wwnet-arcgis-bundle-win-py311`** (close Pro, Unblock, extract, replace folder). Update WWNet can stage a locked `.pyd` as `.pending`; `vendor.activate` swaps it at the next Pro start.
- After every update ask the user to run **Self Test** (tick map steps) and paste `WWNet_selftest_report.txt`.
- Last build installed by the user: **`60e8cc8`** (Self Test in Pro all PASS/WARN). `e25f28d` (headloss type) and the
  merge (section 12) are pushed but NOT yet installed. **The merge changes Rust (schema v3, new native functions):
  the next install must be the full bundle `wwnet-arcgis-bundle-win-py311`, not Update WWNet.**

## 4. Architecture

| Path | Role |
|---|---|
| `crates/wwnet-core` | Model structs, GeoPackage I/O (**schema v3**, `gpkg::upgrade` / `ensure_current`), topology, validation, issues by source |
| `crates/wwnet-hydraulics` | Colebrook-White / Manning part-full |
| `crates/wwnet-rules`, `rulesets/` | DS 50 clause-referenced checks |
| `crates/wwnet-design` | Steady design (ADWF, GSDF = ADWF × 1.5, checks); `loads.rs` shared with the simulation; `service.rs` trace / settings / profiles |
| `crates/wwnet-swmm-sys` | Vendored EPA SWMM 5.2.4 C + hand-written FFI |
| `crates/wwnet-sim` | .inp writer (`inp.rs`), **pump stations / rising mains (`pumps.rs`)**, runner, results → `wwn_sim_*` tables; run labels, show / compare runs, series reader, result layers (`service.rs`) |
| `crates/wwnet-cli` | `wwnet` CLI (dev/CI only) |
| `crates/wwnet-py` | PyO3 abi3 module `wwnet_native` |
| `qgis_plugin/wwnet/core/` | Shared pure Python (importer, schema, **repair.py**, `calc.py`, `summary.py`, `style.py`) — packaged for ArcGIS as `wwnet_shared`. No Python engine copies (all through `wwnet_native`) |
| `arcgis_toolbox/WWNet.pyt` + `wwnet_arcgis/` | ArcGIS Pro toolbox (**23 tools**, §5; `_Tool` base class, parameters by name) |
| `templates/schema.sql` | GeoPackage schema (incl. `wwn_sim_pumps`, `wwn_sim_pump_stations`, `wwn_sim_rising_mains`) |
| `templates/import/watercorp_sewer.toml` | Field roles + code maps (gravity and `[pressure]`) |
| `templates/demo/demo.gpkg` (from `examples/demo`) | Shipped demo model used by Self Test |

Key ArcGIS modules: `tools_impl.py` (tool logic), `engine.py` (wwnet_native calls; no CLI fallback), `gpkg.py` (`split_datasource` handles Pro's `Instance=<file>.gpkg,Dataset=main.%<table>`), `maplayers.py`, `sewer_import.py`, `pressure_import.py`, `pumpdata.py`, `icm_export.py`, `gpenv.py`, `updater.py`, `selftest.py`, `symbology.py`.

## 5. Toolbox tools and status

| Tool | Category | Status |
|---|---|---|
| New Model | Model | verified |
| Add Model To Map | Model | verified |
| Check Model | Model | verified (now also lists missing pump data) |
| **Join Dangling Sewer Ends** | Model | new: splits the main at a missed tee, inserts a tee node, invert = US invert − grade × distance; preview default. Used by the user to fix E4/E46 |
| Import Sewer Data | Data | verified (61 sewers); new Advanced param *Tee join distance* (0.5 m) + "possible missed tee" report |
| **Import Pressure Data** | Data | new, **not yet run on real data** |
| **Set Pump Station Data** | Data | new, not yet run in Pro (manual entry: wet well dia/area, levels AHD or depth, duty/assist/standby, rate or pasted curve); 2026-11-25: optional wet well floor level (§14) |
| **Import Pump Station Data** | Data | 2026-11-25 (§14): pump station register .xlsx → levels / floor / area / storage / design rate; not yet run in Pro |
| **Calculate Existing Demand** | Data | 2026-11-26 (§15): DPLH-071 zones + DPLH-070 R-codes -> DS 50 loading per node (`LU-*` sub-catchments) + Land use loading layer; not yet run in Pro |
| Import Diurnal Profile | Data | fake-tested; Self Test PASS in Pro |
| **Export To ICM** | Data | **verified: gravity model loads into ICM 2024.5 and runs.** Terminal chambers → Outfall; dangling ends stay Break (warned); DEM fill of missing ground levels; GDA2020 or GDA94 output. **2026-11-24: rising mains + pumps now exported (§11), not yet run in Pro / ICM** |
| Run Design | Analysis | verified |
| Run Simulation | Analysis | verified (gravity); **pump station results new, not yet seen in Pro** |
| Trace | Analysis | verified (after dataSource fix) |
| Pipe Capacity | Analysis | verified (Self Test) |
| **Self Test** | Setup | verified in Pro (before the merge; new steps added for runs / compare / settings) |
| **Update WWNet** | Setup | verified in Pro |
| Verify Setup | Setup | verified (now also lists engine functions the toolbox needs) |
| **Model Settings** | Model | merge: new (ADWF/connection, peaking factor, default profile), not run in Pro |
| **Assign Profile** | Data | merge: new (profile on selected / all node loads or sub-catchments), not run in Pro |
| **Show Simulation Run** / **Compare Simulation Runs** / **Plot Hydrograph** | Results | merge: new, not run in Pro (hydrograph chart best effort; CSV reliable) |
| Set Assistant API Key / Ask WWNet Assistant | Assistant | unverified (no key) |

## 6. Done this session (2026-11-23), in order

1. Fixed "model layer not in the active map": Pro reports GeoPackage sources as `Instance=...,Dataset=main.%table`.
2. Export To ICM (file gdb + CSV + mapping notes), then: `Shape` field clash (ERROR 000012) → `conduit_shape`; AddFields in one call; per-stage timing; DEM fill; GP logging off.
3. Testing workflow: `wwnet-arcgis-update` artifact, Update WWNet, per-run module reload, Self Test; map tables removed with `removeTable`.
4. GP logging off in every tool (`gpenv.quiet`).
5. Pressure side: SWMM pump stations (storage wet well, PUMP3 curve / PUMP2 constant rate, start/stop depths, duty/assist, force mains D-W ks mm or H-W) + results (starts/h, run %, max wet-well level, overflow m³, rising-main Qmax/vmax); Import Pressure Data; Set Pump Station Data; pump results tables, flags and layer in Run Simulation; Self Test pump scenario.
6. ICM "not connected to outfall": terminal nodes exported as Outfall; dangling sewer ends kept as Break (an outfall would silently drop their flow); engine warns when a dangling end becomes a free outfall; Join Dangling Sewer Ends tool; tee join distance parameter.

## 7. Water Corporation data facts

- Access chambers: `PACID`, `TOPLEV` (m AHD), `CHAMBER_TY` M → `ac`, J → `ms`, I (IS) skipped. Some chambers have blank `TOPLEV` → DEM fill in the export.
- Connection points: `ID`; ~847 k features — never full-scan.
- Gravity sewers: `ID`, `US_MANHOLE_ID`/`DS_MANHOLE_ID`, `UPSTREAM`/`DOWNSTREAM` invert m AHD, `HEIGHT` mm, `LENGTH`, `FLOW_GRADE`, `MAINTYPE` (E = IS sewer), `STATUS`, `MATERIAL`. Segmented runs; IS sewers tee mid-line; digitised downstream → upstream.
- **Sewer Pump Station**: `ID`, `SAP_ID` (e.g. S0037310), `SAP_NAME`, `STATUS`, `TYPE`, `PSNAME`, `PSNUMBER`, `ASSET_ID`, `DATEINST` — **no hydraulics**.
- **Sewer Pressure Main**: `ID`, `STATUS`, `DIAMETER`, `INDIAM1/2`, `OUTDIAM`, `MATERIAL`, `LENGTH`, `MAINNAME`, `PRESCLAS`; sentinels −99.9, −9996, −9998; dates 1800-01-01 = unknown. **`UPSTREAM`/`DOWNSTREAM` are unreliable — ignored** (no rising-main profile).
- Station hydraulics (wet well, pumps, curves, levels) live in a **separate corporate system that cannot be linked** → manual entry via Set Pump Station Data.
- Loading: 630 L/day per connection ADWF; × 1.5 = GSDF; diurnal profile for dynamic runs; manual ADWF per catchment supported (`wwn_subcatchments.adwf_lps`, `wwn_node_loads`).

## 8. Next steps (in order)

1. **Verify the pressure side on real data**, one step at a time: (a) select a PS + its pressure main inside a gravity model's area, remove model layers from the map, run Import Pressure Data (use selected) and check wet well / chaining / discharge messages; (b) Set Pump Station Data from a data sheet; (c) Run Simulation → pump tables, flags, pump station layer. Fix what real data breaks (assumptions listed in the importer: mains join end to end within 1 m; main starts within 30 m of the PS; wet well = terminal gravity node; GeoPackage without R-tree tables).
2. **Sub-catchments** (user's stated next item): export `wwn_subcatchments` to ICM (polygons, outlet node, manual ADWF / population, profile) and check ICM's subcatchment import fields; possibly an import tool for catchment polygons.
3. ~~Export pump stations / rising mains to ICM~~ done 2026-11-24 (§11); finish the ICM verification there (run + compare).
4. Real ICM diurnal profile ("1 Dunkley Ave Applecross PS4", 5-min, 288 rows) → compare simulated peaks with ICM.
5. Write the user's saved ODIC `.cfg` files out with Export To ICM (needs the files from the user).
6. R-tree handling in sqlite writers (repair / pressure import refuse or may fail if Pro adds spatial indexes).
7. Long-section (HGL) view; lot/zoning loading; DS 50 extras — lower priority.

## 9. Gotchas (ArcGIS Pro, learned the hard way)

- Pro resets `sys.path` before each tool run → `_vendor.activate(_HERE)` first line of every `execute`, then `impl = _impl()` (tests count this pattern: `tests/test_vendor.py` n_exec, label list in `tests/test_tools_impl.py`).
- `parameterDependencies` must be parameter **names**.
- `arcpy.AddError` marks the tool failed → data issues as warnings with `ERROR:` prefix.
- Unsupported Layer properties raise → use `maplayers.layer_name/layer_source`.
- GeoPackage layer `dataSource` = `Instance=<file>.gpkg,Dataset=main.%<table>`.
- Geodatabase field names are case-insensitive: never name a field `shape`/`objectid`/... (`icm_export.RESERVED_FIELDS`).
- Standalone tables: `Map.removeTable()`, never `removeLayer()`.
- Every GP call is slow in the user's project → wrap in `gpenv.quiet`, prefer one `AddFields`, avoid per-row GP tools (DEM read uses `RasterToNumPyArray`, `GetCellValue` fallback).
- GeoPackage writes via sqlite3 fail while Pro holds a lock → tools tell the user to remove the model's layers / close attribute tables.
- The fake arcpy must be made stricter whenever real Pro fails differently (done for Shape clash, removeTable, tables list).
- Pro's Python window: give one-liners.

## 10. Dev commands and verification gate

```bash
cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
# test venv in the session scratchpad: pytest, pyflakes, maturin, anthropic, wwnet_native wheel (rebuild after Rust changes)
(cd crates/wwnet-py && maturin build --release) && pip install --force-reinstall target/wheels/wwnet_native-*.whl
pytest -q qgis_plugin/tests                                         # 104 passed at handover
(cd arcgis_toolbox && WWNET_EXE=../target/debug/wwnet python -m pytest -q tests)   # 252 passed at handover — must be 100 % green
python -m pyflakes arcgis_toolbox/WWNet.pyt arcgis_toolbox/wwnet_arcgis qgis_plugin/wwnet/core   # CI lint gate
/usr/bin/python3.11 -c "import ast; ast.parse(open('arcgis_toolbox/WWNet.pyt').read())"
```
CI jobs: rust (ubuntu + windows), plugin, arcgis-toolbox (pyflakes + pytest), wheel, **arcgis-bundle** (full bundle + Pro-like smoke test), **arcgis-update** (Python-only zip). Check runs with GitHub MCP `actions_list` (owner `daniellawrence38`, repo `icm-development-`, branch filter).

## 11. 2026-11-24: pressure pipes in Export To ICM

User report: "Pressure pipes are not being included in the export to ICM". Cause: `icm_export.build` never read
`wwn_rising_mains` / `wwn_pump_stations` / `wwn_pumps` (it only warned "NOT exported"). Fix is Python only
(→ `wwnet-arcgis-update` artifact + Update WWNet) and mirrors the engine's network (`crates/wwnet-sim/src/pumps.rs`).

Commits: `c6b4c46` pressure export; `60e8cc8` null-geometry fix; `e25f28d` headloss type; plus handover commits.

### What the export now writes

- **Rising main** → row in `ICM_Conduits`: `<ps_id>_DEL` → discharge node; `solution_model` = `ForceMain` (gravity
  `Full`); `us/ds_headloss_type` = `None` (gravity `Normal`); inverts wet well floor → discharge chamber floor (first /
  last `profile_json` level if present; intermediate points not exported); CW ks (default 1.5 mm) or `HW` C;
  `wwn_link_kind` = gravity / rising_main. Unknown discharge node → not exported (warned, `skipped`).
- **`<ps_id>_DEL` delivery node**: Break, floor = wet well floor, ground = wet well ground (copied after DEM fill),
  `flood_type` = Sealed (ICM Break nodes have no flood type, so it is ignored). Placed at the rising main's first
  vertex, else the PS point, else 1 m east of the wet well.
- **`ICM_Pumps`** (+ `_pumps.csv`): wet well → `_DEL`; `Rotpmp` + `HD_<pump_id>` (curve → `_head_discharge.csv` and the
  notes; decreasing head, speed by affinity laws) or `Fixpmp` (`discharge` L/s = station rate); switch on/off =
  start/stop (engine defaults floor + 1.0 / + 0.3 m); assist +0.2 / +0.1 m; standby skipped. Link suffixes shared
  with conduits. Stale `ICM_Pumps` / pump CSVs removed when a model has no pumps.
- Wet well chamber/shaft area = `wet_well_area_m2` (default 7.07 m²). Rising-main discharge node with nothing
  downstream → Outfall (`wwn_outfall_reason` = rising main discharge).
- New fields (user's saved ODIC configs must map them): nodes `flood_type`; conduits `solution_model`,
  `us_headloss_type`, `ds_headloss_type`, `bottom/top_roughness_HW`, `wwn_link_kind`; whole new pump table.
- Code: `icm_export._pressure` / `_station_pumps` (pure), `read_pressure_geometry`, `node_geometry_fn`,
  `link_geometry_fn`, `write_gdb(..., extra_geom=)` for rows not in the GeoPackage (`_extra`). All `SHAPE@XY` reads go
  through `_xy()`: **Pro returns `(None, None)` for a feature with no geometry** (Self Test crash, fixed `60e8cc8`; fake
  arcpy now does the same and returns geometry objects for `SHAPE@`).
- Tests: 263 toolbox (11 new ICM tests, helper `tests/pressure_line.py`), 104 plugin. Self Test step
  "export to ICM (pump station)".

### Verified in Pro / ICM 2024.5

- Pro Self Test on `60e8cc8`: all PASS/WARN; pump model exports 1 rising main + 1 `Rotpmp` pump.
- ICM ODIC: `ICM_Nodes`, `ICM_Conduits`, `ICM_Pumps` import OK (`Rotpmp` accepted as link type; pump field names work).
- Validation errors E2075 x2 on `PS1_DEL.1` ("Normal or high headlosses are not allowed for ForceMain") → fixed in
  `e25f28d`. I2040 on demo N01 (chamber area below Simulation Parameters minimum) is info only.

### Where we stopped (next session starts here)

1. User was asked (step 5) to set US/DS headloss type = None by hand on `PS1_DEL.1` in the scratch ICM network,
   validate, run a short simulation and report pump starts / peak flow / max wet well level. WWNet engine reference
   for the Self Test pump model: 18 starts (4.5/h), Qmax 30 L/s, max wet well 23.01 m AHD, RM1 Qmax 8.42 L/s.
   Ask for those results first.
2. Not yet known: whether the user created `HD_PS1_P1` by hand or ODIC imported the head-discharge CSV (ask; if ODIC
   can, document the mapping in the notes); `discharge` unit for `Fixpmp` (L/s vs ICM flow unit).
3. Then: install `e25f28d` (Update WWNet), map the new conduit fields in the saved ODIC config, re-export a real model
   with a pump station (needs Import Pressure Data + Set Pump Station Data on real data, §8 item 1).
4. Side issues noticed: export GP calls are slow in the user's project (Delete / CreateFeatureclass / AddFields 6–38 s
   each; Self Test export 67–161 s). Offered, not done: delete and recreate the whole `_ICM.gdb` folder instead of
   per-table Delete (fails if ICM/Pro holds the gdb). Pump station simulation in Self Test shows 15.4 % continuity
   error (engine, tiny 1 m² wet well) — check on a real station.

## 12. 2026-11-24 merge: the review session's work (schema v3 / toolbox UX)

What the user will notice after installing the **full bundle** (Rust changed):
- **Models upgrade to schema v3 on first use.** `wwn_issues` gets `source` + point geometry (each Check / Design /
  Import / Simulate replaces only its own rows: fixes design issues piling up); `wwn_results_conduits` becomes a
  feature table (dropped and recreated: derived data). Remove an old "Design results" layer from the map first.
- **Result layers persist** in the GeoPackage: Design results, Simulation results (conduits / nodes), Simulation
  comparison, Issues. The pump station results layer is still a temporary memory layer (as before).
- **Every tool fills the model from the active map** (`gpkg` parameter); derived output `out_gpkg` for ModelBuilder.
- **Run Simulation default roughness is now `ks`** (Colebrook, as the design). The Self Test pump scenario and the
  engine reference numbers in section 11 were run with Manning 0.013: Self Test still passes "manning" explicitly.
- Run labels; Show / Compare Simulation Runs; Plot Hydrograph; series stored for the conduits selected in the map.
- Trace: model + start id, or blank = the conduit / node selected on the map; reports upstream connections, ADWF
  (as simulated) and design GSDF. The old Nodes-layer parameter is gone.
- Check Model: no output table parameter any more (the Issues layer replaces it).
- Messages are prose lines (no fixed-width tables) except the pump station tables kept from section 6.
- Assistant: flow tools (design summary / result, runs, sim results, compare, trace) and "Apply the proposals from
  the last run" (deterministic, no API call).
- Pipe Capacity: any DN (DS 50 checks only DN150-600). Set Assistant API Key: CLI path removed.

Engine / code facts:
- `wwnet_native` new functions: `pipe_capacity`, `store_issues`, `sim_runs`, `show_sim_run`, `compare_sim_runs`,
  `sim_series`, `get_settings`, `set_settings`, `assign_profile`, `schema_version`, `SCHEMA_VERSION`;
  `simulate(..., label="")`; `trace` returns `conduits` + `summary` and accepts a conduit id.
- Pure-Python engine copies removed (`core/hydraulics.py`, `core/rules.py`, GSDF fallback), and the `wwnet.exe`
  fallback (`wwnet_arcgis/cli.py`, `issues_table.py`). `package.py --cli` kept (harmless, unused).
- Issue codes UPPER_SNAKE everywhere (design `design_x` -> `X`; importer codes upper-cased). Importer no longer
  repeats MISSING_INVERT / ADVERSE_GRADE / INVERT_ABOVE_SURFACE (the model check reports them).
- Engine warnings like "pump station PS1: ..." become ENGINE issues placed at that object in the Issues layer.
  The review session's "pump station not simulated" warning was dropped in the merge (pumps are simulated now).
- `examples/demo/demo.gpkg` regenerated as v3 (same network); `examples/demo/demo_flows.gpkg` = demo + 12
  connections per manhole + the example profile (flow tests).
- `tests/fake_arcpy.py`: `addDataFromPath` now decides table vs layer from `gpkg_contents.data_type`, like Pro.
- `selftest.py` adapted to the new signatures; new steps "simulation runs: show / compare / hydrograph" and
  "model settings / assign profile".

Merge verification (this container): Rust fmt + clippy clean, `cargo test --workspace` 79 passed; toolbox
248 passed; shared core 104 passed; pyflakes clean; Python 3.11 compile OK. Not run in Pro.

Still open from the review (`docs/REVIEW-2026-11.md`): `README.md` / `arcgis_toolbox/README.md` partly describe
the pre-merge tools (the other session updated the toolbox README for its tools; the section 12 changes are not in
it); `.lyrx` symbology (needs Pro); detecting unsaved edits (only a reminder line).

Next steps after the merge (before section 11's list):
1. User installs the full bundle from the CI run for the merge commit, then runs **Self Test** (tick map steps) and
   pastes the report. Expect FAIL/WARN only where Pro behaves differently from the fake (layer refresh after a re-run,
   `definitionQuery`, charts).
2. Then continue section 11 "Where we stopped" (ICM pump run results) and section 8.

## 13. 2026-11-24: antivirus detection, assistant removed

The user reported the merged build "trips the malware detection system" (no detection name / file available).
No new suspicious behaviour had been added; likely causes were the rebuilt, unsigned `wwnet_native.pyd`
(reputation / ML detection) or long-standing code that looks like malware patterns. The user asked to remove the
assistant ("won't be able to use that at work anyway"). Done:
- **ArcGIS assistant removed**: tools Ask WWNet Assistant and Set Assistant API Key; `dpapi.py` (ctypes
  CryptProtect/UnprotectData), `settings.py`, `assistant_run.py`, `arcpy_tools.py`; the bundle no longer ships
  `wwnet_shared/assistant/` (incl. `install.py`, which ran pip in a subprocess) nor the vendored anthropic /
  pydantic / httpx2 / jiter / truststore / typing_extensions. `vendor/` now holds only `wwnet_native`.
  `vendor.py` no longer swaps `typing_extensions`. Verify Setup: engine only (no live API test).
- **Update WWNet never writes binaries** (`.pyd/.dll/.exe/...` in an update zip are listed and skipped; no
  `.pending` staging, no swap at Pro start) and deletes known-obsolete files of earlier versions
  (`updater.OBSOLETE_FILES` / `OBSOLETE_DIRS`).
- **`wwnet_native.pyd` has a Windows version resource** (`crates/wwnet-py/build.rs`, crate `winresource`;
  ProductName WWNet, FileDescription, CompanyName "WWNet developers", version from Cargo). CI checks it
  (`arcgis-bundle` job, PowerShell VersionInfo step). The build fails rather than ship without it.
- Tests: a packaging test asserts no shipped Python contains CryptUnprotectData / windll / subprocess / anthropic.
- The QGIS plugin keeps its assistant (frozen, not shipped to ArcGIS).
- Install for the user: **delete the old WWNet-ArcGIS folder** before extracting (old vendor packages and
  assistant files would otherwise stay and may still be flagged).
- If it is still flagged: ask IT security to treat it as a false positive / allowlist by SHA-256; code signing
  is the permanent fix (needs a certificate from IT). Do not try to evade detection.

## 14. 2026-11-25: pumps not reaching ICM; pump station register import

User report: "Export to ICM mostly works except the pumps are not importing across"; `ICM_Pumps` was not in the
export. Audit (no code bug, pump path byte-identical to 421d23b where ICM accepted it): `ICM_Pumps` is written only
when a station has a pump with a rate or curve. Import Pressure Data writes no hydraulics, so without pump data the
export logs "no pumps and no pump rate; NO pump exported" and the wet well has no outlet in ICM. Other ways pumps
drop out (still true): Set Pump Station Data errors are `ERROR:` warnings (tool shows success, nothing saved;
locked GeoPackage, levels at/below the wet well invert, non-decreasing curve); pumps with counts only (no rate /
curve) are simulated as ideal pumps by the engine but NOT exported; pumps are a separate ODIC import (Pump object;
a nodes + conduits config won't bring them).

**Key finding:** the wet well node is the gravity terminal manhole, so its invert = incoming sewer invert. Real
stop levels sit below it, and `set_station` rejected them. Fixed: `set_station(wet_well_floor=, ground_level=)`
sets the wet well node's invert / surface level in the same transaction (the incoming conduit keeps its ds_invert
= a drop; SWMM offsets and ICM handle it). Set Pump Station Data has an optional "Wet well floor level (m AHD)".

**New tool Import Pump Station Data** (`pump_register.py`, stdlib `xlsx.py` reader, column map in
`templates/import/watercorp_pump_register.toml`). The user found the Water Corp pump station register (.xlsx;
values stored as text with a leading `'`, handled). Matched on `ps_id` (= GIS SAP_ID) = functional location in
column C of every sheet (SAP sub-locations `S0037310-...` also match). Layout from the user:
- Level Settings: E design ground (m AHD, top of slab); depths below slab in **metres**: F overflow, Q stop (corrected 2026-11-25, was G),
  L high level alarm, P start, **T base (wet well floor)**. Order checked base > stop > start > alarm > overflow.
- Civil: R wet well storage = volume **between the high level alarm (L) and overflow (F)** → area = R / (L − F);
  AB emergency storage m³.
- Motor and Pump Attributes: AF design flow (L/s), AG design head (m). One rate; stations are duty/standby →
  1 duty + 1 standby at the design flow (Fixpmp in ICM); head kept in the station notes. Extra pump rows with
  different test data are flagged "… not used …; future enhancement" (**user wants second-pump test data / curves
  as a future enhancement; they may add a curve from one pump's test results by hand**). Existing curves are kept.
- Per-station errors (spreadsheet sheet / column / row in the message) skip only that station; preview option.
- Import Sewer Data (overwrite) rebuilds the model (pump data lost); Import Pressure Data keeps station data but
  recreates the `WW_<ps_id>` nodes it made (floor lost) → re-run the register import last.
- Self Test step "import pump station data (demo register)" (`examples/demo/pump_register_demo.xlsx`, shipped as
  `templates/demo/`, generator `tests/make_pump_register_demo.py`).

Commits: 0927ae3 xlsx reader; 1a3529b core; b65778b text values; 18c0288 WIP, 25a28db tool; 39604b5 stop = Q;
3ae03e0 Self Test own demo column map; 9b8c0af WIP + the tee commit. Tests: toolbox
266, plugin 103 + 1 skipped (needs `anthropic`), pyflakes clean. Toolbox now 22 tools. Python only since 38f46e2.

### Tees: branch ends meeting a main at one point (Import Sewer Data fix)

User report from ICM: "not connected to outfall" where sewers join others without a manhole; "the tool inserts a
break node but doesn't break the conduit". Root cause (`importer.py` step 3): two or more branch ends meeting at
one point on a main formed a multi-end cluster = "segment junction" J<n>; only LONE ends were tested against other
sewers' interiors, so the main stayed whole and J had only inflows (silent in WWNet; Export To ICM wrote J as a
dangling Break). Fixed: multi-end clusters are tee-tested too (main split, inverts interpolated by chainage, all
branch ends on the tee node); inflow-only no-chamber J clusters now count as dangling ends and get the possible
missed tee check; a lone end next to a main's own end node (beyond snap, projection within tol of the node) joins
that node (END_FAR_FROM_NODE). Crossing guard: two pieces of one run (opposite ends, same non-blank US/DS manhole
ids) meeting on a main stay a J junction (not teed); with blank ids they tee but "possible crossing joined as a
tee" is reported when their inverts differ from the main's by > 1 m. Single tees, segmented mains, IS tees and
Join Dangling Sewer Ends were already correct (Join Dangling Sewer Ends also fixes the paired case on existing
models). Tests: qgis 118 + 1 skipped, toolbox 270. **The user must re-import (Import Sewer Data) to get the fix on
an existing model, then re-run Import Pressure Data and Import Pump Station Data** (rebuild loses pump data), or
run Join Dangling Sewer Ends (preview off) on the existing model. Also still open: "unconnected to outfall" caused
by mains outside the selection (expected; select the whole catchment down to the outlet).

### Bulk import issue list (user's Issues table, 4,028 rows, 2026-11-25)

Triage of the user's Issues export (import + check). Fixed in the importer (user's decisions in brackets):
- **−99.9 placeholder levels** (inverts, TOPLEV; any value <= −99) were used as real levels → ~390 issues
  (118 m drops, adverse / flat grades, invert above surface −99.9). Now missing, then estimated ["from grade"]:
  one end from the other ± length / FLOW_GRADE; both missing from adjoining sewers at the shared node (lowest
  incoming ds invert / single outgoing us invert), then grade. `INVERT_ESTIMATED` per sewer, conduit
  `data_source=assumed`; `INVERT_DERIVED` removed.
- STATUS N (not in use) and D (dead) skipped [user]; only A imported. A not-in-use stub made a false tee + cycle.
- MAINTYPE C / I / O / OS = reticulation [overflow pipes kept as sewers]; CHAMBER_TY E and P skipped [user],
  F / OS / O / DT = ac; MATERIAL SU = other. PACID / manhole ids all zeros = no id.
- False-tee guard: a branch whose other end is at one of the main's end nodes is not teed (SUSPECT_TEE);
  directed cycles through a tee node reported (SUSPECT_TEE).
- Noise: TEE_JOINED / END_SNAPPED are info (`rep.notes`, severity info in the Issues layer); GRADE_MISMATCH listed
  only when the fall difference > 50 mm (rest one summary line).
Left as genuine data issues for the user: DN_REDUCTION (23), real INVERT_ABOVE_SURFACE, small TEE_ADVERSE,
ID_MISMATCH (5), DANGLING_END / DIRECTION_UNKNOWN at the selection edge, BIFURCATION at real overflows.
Tests: qgis 130 + 1 skipped, toolbox 270.

### Follow-up requests (2026-11-25, after the bulk import)

- **Missing inverts from the other side of the node** (user: "a realistic value based on the invert on the other
  side of the node"): `_fill_inverts` priority per missing end: (1) ds = largest-DN outgoing sewer's us invert (or
  the main's invert at a tee) + max(0, DN_out − DN_in)/1000 (obverts matched); us = min over incoming of
  ds_in − max(0, DN_out − DN_in)/1000; rejected if it makes the sewer adverse; (2) other end + FLOW_GRADE ×
  length; (3) other end + DS 50 minimum grade for the DN (ruleset `grades`, nearest smaller DN; 1 in 200 if
  unreadable). Counts inverts_estimated_node / _grade / _ds50. Shared core 136 + 1 skipped.
- **Import Pressure Data**: a main's far end joins a gravity node only within 1 m (discharge distance default
  10 → 1 m); otherwise new node `D_<rm_id>` "rising main discharge, not connected: connect manually"; Export To
  ICM writes it as Outfall, reason "… not connected (connect manually)". Main start = nearest segment end within
  30 m of the PS (kept). **Wet well** (user: a gravity sewer always ends at the pump station point) = the terminal
  gravity node within `wet_well_distance_m` (1 m, tool parameter) of the PS point; none -> `WW_<ps>` at the point,
  left unconnected with a warning naming the nearest dead end (was: nearest terminal node within 30 m).
- **Export To ICM**: conduit / rising main lengths 0 < L < 1 m exported as 1 m (ICM minimum; user: "everything
  less than 1 m can be inserted as 1 m" — interpreted as conduit length, not confirmed), one warning.
- **Export To ICM**: gdb notes fields 2000 chars, other text 254; longer text cut (' ...'); fake arcpy enforces
  field lengths (user's EdenHillV5 export failed 'Field length exceeded. Field: notes').

### Import Network Data (one combined import) and the ICM floor fix

- **Import Network Data** (user: "combine the import functions into one"): one tool, steps in this order:
  1 sewers (Import Sewer Data parameters), 2 pressure (optional layers; `pf_` field prefixes), 3 Join Dangling
  Sewer Ends (default on, 5 m; user's choice) — AFTER pressure, so the inlet dead end at each PS point is already a
  wet_well node (repair.py only joins junction / is_end / io_end nodes), 4 pump station register (optional),
  then a final model check (Issues refreshed after the joins) and the model added to the map once (no sqlite
  write while Pro holds a lock). Sewer exception stops all; pressure failure skips the register; join failure
  continues. The single tools are kept. 23 tools.
- **Export To ICM**: chamber_floor lowered to the lowest attached conduit invert (ICM "ds invert is below
  attached node floor level"); wwn_floor_source "lowered to lowest connected invert (was X)".
- User reported the tee / branch-not-joined problem still occurring (without running Join Dangling Sewer Ends):
  the combined import's 5 m join should cover ends beyond the 0.5 m tee tolerance — confirm on EdenHill.

### Where we stopped (next session starts here) — end of the 2026-11-25 session

State: all work merged into `main` (now the GitHub default branch). The user's PC: last confirmed build was from
before c5a612f (Self Test 11:05 with the old demo-register FAIL; EdenHillV5 export failed on long notes, fixed
9f44d44). **Not yet installed / not yet run in Pro: everything from c5a612f to the latest `main`** — the importer
fixes (−99.9, status N/D, codes, tee clusters, invert estimation from the other side of the node), hanging pressure
main ends, wet well at the PS point, 1 m minimum length, chamber floor lowering, Import Network Data.

1. User installs the latest `main` via `wwnet-arcgis-update` + Update WWNet (they are on the post-38f46e2 full
   bundle: no assistant tools), right-click toolbox › Refresh, runs **Self Test**, pastes the summary lines.
   Expect PASS for "import pump station data (demo register)" (own demo TOML now).
2. **Import Network Data** for EdenHill to a NEW GeoPackage (same selection; pump station + pressure main layers;
   the register workbook; join on, 5 m). Ask for the messages: tee / join counts, SUSPECT_TEE, INVERT_ESTIMATED by
   rule, hanging rising mains, wet wells left unconnected (WW_), register matches.
3. **Export To ICM**, then ODIC import incl. the Pump object; ask for the remaining ICM validation errors with
   node / link ids. Things to watch: "not connected to outfall" from the selection edge (expected), any tee still
   unjoined (would need a new reproduction), chamber floor lowering count.
4. Open questions: user's "values < 1 m -> 1 m" was implemented as conduit length (not confirmed); `Fixpmp`
   `discharge` unit in ICM; whether ODIC imports the head-discharge CSV; placeholder pump link for stations with
   no data (offered, not decided); Set Pump Station Data failures more visible; pumps with counts only are
   simulated but not exported; second-pump test data / curves from the register (user: future enhancement);
   README / handover sections 4-5 tables partly stale (tool count now 23).
5. Then §11 "Where we stopped" items (ICM pump run comparison) and §8.

## 15. 2026-11-26: existing demand from land use (Calculate Existing Demand)

User's question: how best to load existing demand without a load per connection point slowing ICM. Answer given:
ICM / SWMM take inflow only at nodes, so per-connection loading gives the same pipe flows as summing per node
(lossless); run time is driven by the hydraulic network (links, short conduits, timestep), not foul loads.
Recommended ICM form (next step, not built): one ICM subcatchment per loaded node (population + wastewater profile;
trade flow + trade profile for non-residential; base flow for infiltration), per-capita flow and diurnal shape in the
Waste Water event (calibration in one place). Unconfirmed: whether ODIC accepts subcatchments without a boundary
polygon (test 3 rows; else Voronoi cells per node). If ICM run time ever is a problem, the lever is network
simplification (drop reticulation, push loads to the first retained node), optional only (infill makes DN150 the
constraint).

User's method and data (2026-11-26): **DPLH-071 Local Planning Scheme Zones and Reserves** (field *Zone*; block-sized
polygons) gives the area and the use; residential zones take the code from **DPLH-070 Local Planning Scheme R-Codes**
(field "R code number"; suburb-sized polygons); non-residential zones -> closest DS 50 class, **no match -> flag and
R15**; zero flow for roads, parks, conservation reserves, farm. Flow monitoring calibration: **disregarded for now**
(user).

Built (Python only, no Rust change -> `wwnet-arcgis-update` + Update WWNet, then right-click toolbox > Refresh):
- `qgis_plugin/wwnet/core/demand.py` (pure): `Rules` (zone text -> class, whole-word first-match rules from
  `templates/import/dplh_land_use.toml`), `parse_rcode` (number / R20 / dual R20/40 -> base / aliases), `Rates`
  (DS 50 Tables 4.1-4.3 from the ruleset; R15 minimum, next higher row, highest row above the table, as
  `wwnet-rules::residential_rate`), `Rings` (even-odd polygon with banded edges: area, scanline, contains, OGC
  polygons), `sample_points` (~50 per polygon, 2-20 m), `SewerIndex` (ring search), `calculate`.
  Allocation: each sample -> nearest model sewer within 150 m -> that sewer's US node (same rule as connection
  points); beyond -> not loaded (PARTLY_LOADED / NOT_LOADED). Per node + class one `wwn_subcatchments` row
  `LU-<node>-<R-code row | COM | IND | HOT | CBA>`, net_area_ha = allocated area, no geometry, status existing, notes
  with zones and LPS -> DS 50 codes. residential_or_commercial (centre, mixed use): residential where an R-code
  exists, else commercial. Blank zone -> zero (ZONE_BLANK; `options.blank_zone_class`). Rural residential /
  smallholdings -> zero (assumed unsewered; my call, tell the user). Region from the mean sewer northing
  (26 deg S = N 7,124,316 on the CM). Perth CBA not auto-mapped (comment in the TOML).
- `arcgis_toolbox/wwnet_arcgis/demand_import.py`: polygon rings from Pro shapes (None-separated holes), zones read
  with a spatial filter on the sewer extent + max distance (or the selection), R-codes on the zones' extent;
  `write_result` in one transaction: replace LU-* rows, delete `wwn_node_loads` source=connection_points (default;
  manual loads kept), rewrite `wwn_land_use` (Python-created MULTIPOLYGON feature table "Land use loading", NOT in
  `templates/schema.sql`, so no Rust rebuild), replace `wwn_issues` source **`demand`** rows directly (the engine's
  `store_issues` only accepts check / design / import / simulate).
- Tool **Calculate Existing Demand** (Data; 24 tools): zones layer + Zone field, R-Codes layer + R-code field
  (defaults picked from the field names), use selected, preview, max distance 150 m, dry / wet, region, replace
  connection point loads, zone rules TOML. Symbology `style_land_use` (zone_class colours).
- Verified here: the engine's simulated inflow rises by exactly the Python ADWF total (test); design / simulate /
  upgrade / check run on a model with the new rows and table. Benchmark: 6,750 zone polygons, 3,375 sewers, 12
  R-code polygons of 4,000 vertices: 2.5 s (50 samples; 200 gave the same total at 9 s).
- Tests: shared core 179 + 1 skipped (43 new in `qgis_plugin/tests/test_demand.py`), toolbox 309 (11 new in
  `arcgis_toolbox/tests/test_demand.py`; fake arcpy now returns polygons with None-separated holes and OID@).

### First real run (EdenHill, preview, 2026-11-26) and the follow-up

User's preview: 1,047 zones (2,681 ha: residential 1,710, commercial 456, industrial 128, zero 376), 1,326
R-Codes polygons (so block-sized, not suburb-sized as first described), 53 s in Pro. Loaded 1,485 ha on 7,710
nodes (8,264 rows), ADWF 205 L/s (residential 172, commercial 32, industrial 0.8). Zone names are the DPLH
standardised ones ('Medium and high density residential', 'Residential development', 'General commercial',
'Light industry', 'General industry', 'Business', 'Public purposes', 'Private clubs and institutions'); unmatched
'Special purpose' and 'Service station' -> now commercial. 428 ha residential "no R-code" and 113 ha below R15
(counted on the read area then). No connection point loads in that model (the comparison line did not print).
353 NOT_LOADED warnings, nearly all polygons in the rectangular read area beyond the network.
Fixed: NOT_LOADED only when a model sewer is within 2 x the maximum distance (warning with the distance), else
OUTSIDE_MODEL (counted, no issue); R-code notes on the loaded area only and split into outside every R-Codes
polygon vs blank code; R-Codes field values listed; per-class zone name breakdown (area loaded); "no connection
point loads" line. Shared core 187 + 1 skipped, toolbox 309.

### Second run and split coding (2026-11-26)

Second preview (EdenHillv8): commercial = 'Special use' 250.7 ha loaded (~27 L/s of 32.6), 'Public purposes' 23,
'Education' 6.9, 'General commercial' 6.7 ...; residential 'Residential' 615, 'Residential development' 342,
'Medium and high density residential' 225 ha; 340 ha residential outside every R-Codes polygon (~= the 'Residential
development' area: structure plan codes not in DPLH-070 -> R15). R-Codes values: R20 219, R20/40 150, R40 143,
R20/35 126, R20/50 115 ... (field `rcode_no`). 36 near misses (64 ha), 311 outside (643 ha). 28.7 s.
User (2026-11-26): "Add a field for the split zoning, to select higher or lower density" -> tool parameter
*Split-coded R-codes* (lower = as built, default; higher = as permitted; min / max of the two codes, so order does
not matter; `Options.split_code`); "Commercial should have flow as per ds50" -> read as: keep Special use etc. as
commercial at the DS 50 Table 4.3 rate (no change). User: "R30 for residential development zone" -> zone rules
take an optional third value, a default R-code used only where DPLH-070 has no code (`Rules.classify` returns
(class, rule, default)); bundled rule ["residential development", "residential", "R30"] ahead of "residential".
Summary line "the zone's default code where the R-Codes layer has none: 'Residential development' R30 x ha".

### Third run, written (EdenHillv8, 2026-11-26)

R30 default applied to 339.97 ha ('Residential development'); only 0.19 ha residential left outside every R-Codes
polygon. DS 50 rows loaded: R15 12.5, R20 551.8, R25 231.1, R30 371.5, R40 15.3 ha. ADWF 242.48 L/s (residential
209.06, commercial 32.62, industrial 0.79), lower split code. Written: 8,260 LU-* rows on 7,710 nodes, 1,047
polygons in the Land use loading layer.

ICM subcatchments without a boundary (ICM help, checked 2026-11-26): imported subcatchments with no boundary get a
dummy circle of `total_area` centred on the subcatchment; `contributing_area` must be entered (0 allowed) and
governs runoff; `x` / `y` = centroid. Foul flow = population x connectivity x per capita flow of the wastewater
profile + additional foul flow; trade flow x trade profile; base flow constant. So the ICM export can be a point
table per loaded node (no Voronoi needed). Exact ODIC mapping for a point layer still to confirm in ICM.

### Export To ICM: foul sub-catchments (2026-11-26)

User: "Go ahead, same trade profiles for commercial and industrial". `icm_export.subcatchments()`: ONE ICM
sub-catchment per loaded node, aggregating every load the engine simulates: LU-* and other wwn_subcatchments
(residential -> population = net area x DS 50 persons/ha, scaled to the wastewater profile's per capita flow when a
row's L/p/d differs, i.e. wet ground; commercial / industrial -> trade_flow L/s, trade profile 1 for both; manual
adwf_lps -> additional_foul_flow), wwn_node_loads (adwf_lps or connections x adwf_per_connection_lpd ->
additional_foul_flow). Fields (ICM names): subcatchment_id = node_id, system_type foul, total_area (ha; 0.01 for
load-only nodes), contributing_area 0, connectivity 100, population, wastewater_profile 1, trade_flow, trade_profile
1, additional_foul_flow, x / y (= node, output SR) + wwn_adwf_lps, wwn_residential/commercial/industrial_ha,
wwn_rcodes, notes. Not exported (warned): outlet not a node, no net area (polygon only), extra_flow_lps (design
only). Broadacre: gross x 0.75. ICM_Subcatchments point fc (geometry = node point) + CSV; stale table / CSV removed
when there are no loads. Notes: ODIC order nodes > conduits > pumps > sub-catchments; Waste Water / Trade Waste event
set-up; flows L/s. Test: after Calculate Existing Demand on the demo, the exported wwn_adwf_lps total = the engine's
simulated inflow (rel 1e-4). Toolbox 313, shared core 189 + 1 skipped.

User after the first ICM import: "Nothing in the export about the subcatchments but infoworks is saying they drain
to nodes with different subcatchments" -> ICM engineering validation check "Node is a different system type than
the subcatchment": sub-catchments were system_type foul, nodes / conduits had none (ICM default). Fixed:
`icm_export.SYSTEM_TYPE = "foul"` on every node, conduit (incl. rising mains), pump and sub-catchment (new
`system_type` field in NODE / CONDUIT / PUMP_FIELDS; the user's saved ODIC configs must map it). "Nothing in the
export about the subcatchments": unclear (messages? notes?) — asked for the export messages.

Export with run 117 (EdenHillv8, 2026-11-26 14:26): 8,701 nodes, 8,650 sewers, 19 rising mains, 12 pumps, 7,710
sub-catchments; population 100,347 at 180 L/p/d, trade flow 33.42 L/s, ADWF 242.48 L/s (= Calculate Existing Demand).
123 s (GP create / fields / delete 4-14 s per table). Still open from the messages: 3,973 nodes without ground level
(no DEM given this time); 7 stations with no pump data (6 WW_<numeric id> stations from pump stations without a
SAP_ID: the register cannot match them; S2004007) and S0030035 without a rising main: their wet wells have no outlet,
so with loads on they will fill / flood in ICM; 9 hanging rising main discharges; 7 dangling sewer ends.

ICM import (user, 2026-11-26): sub-catchments imported; ICM totals population 100,347 and trade flow 33.42 L/s =
the export. Additional foul flow 0 is expected (no node loads / manual ADWF in EdenHillv8). Next: Waste Water event
(profile 1, 180 l/hd/d + diurnal) and Trade Waste event (profile 1) in ICM, one step at a time.

### Where we stopped (next session starts here) — end of the 2026-11-26 session

State: everything from this session (`ccr-f18eb556-rbdbtr`) is merged into `main`. User's PC: runs the latest build
(CI run 117 `wwnet-arcgis-update`, Update WWNet; Python only since 38f46e2). Calculate Existing Demand written for
EdenHillv8 (242.48 L/s); Export To ICM with 7,710 foul sub-catchments imported into ICM 2024.5 by ODIC; ICM totals
confirmed (population 100,347, trade flow 33.42 L/s). Tools: 24.

1. **In progress: ICM Waste Water event.** Step 1 was given (Explorer: right-click the model group > New InfoWorks >
   Waste Water; name it, open it) — menu names from memory, the Autodesk help look-up was interrupted. Ask what the
   editor shows (columns; a tab for calibration / diurnal factors), then: profile 1, per capita flow 180 l/hd/d,
   diurnal multipliers (the user's real ICM profile "1 Dunkley Ave Applecross PS4" or the WWNet example). Then the
   Trade Waste event (profile 1, flow scaling factor 1.0, diurnal multipliers), then a dry weather run with both
   events; compare the mean foul inflow with 242 L/s. One step at a time.
2. Before reading capacity results: 8 stations have no pump in the export (6 `WW_<numeric id>` stations whose GIS
   pump station has no SAP_ID, so the register cannot match them; S2004007 without data; S0030035 without a rising
   main): their wet wells fill / flood with loads on. 9 rising main discharges hang (Outfalls; connect by hand). 7
   dangling sewer ends. 3,973 nodes without ground level (use the DEM parameter of Export To ICM).
3. Open questions: rural residential / smallholdings as zero (assumed unsewered); R-AC code equivalents
   (`[rcode_aliases]`, empty); DS 50 rates (180 L/p/d, R-code occupancy) used as existing flow — the user chose
   DS 50, they read high in up-coded areas; option to export all flow as additional foul flow (offered, not wanted
   so far); QGIS plugin has no front end for Calculate Existing Demand (core is shared); export GP calls slow (123 s;
   delete / create / AddFields 4-14 s per table — recreating the whole gdb offered earlier, not done).
4. Section 14's open items (Self Test after install, ICM pump run comparison) and §8.

## 16. 2026-11-26 (second session): gravity conduits "not connected to outfall" in ICM

Branch `claude/icm-conduit-outfall-errors-hi5c7m` (from `main` 3bcb290). User: "Import to ICM needs work, a lot of the
conduits still show errors of not being connected to outfall. How do I give you data to inform a fix" then **"I just
want to focus on gravity sewer conduit connectivity. Disregard the pressure side for now."**

Data channel: the repository is **public**, so the user must NOT upload export files (asset ids + coordinates) to
GitHub; the user pastes tool messages / ICM validation text into the chat. Asked (not yet answered): the exact ICM
message text + count + ~5 ids; whether the ICM network was empty before the ODIC import (stale objects from earlier
imports with different tee / junction ids stay unconnected).

Built (Python only -> `wwnet-arcgis-update` + Update WWNet, then right-click toolbox > Refresh: new parameter):
- `icm_export.connectivity()` (pure): reverse BFS from every Outfall over the exported links (conduits, rising mains,
  pumps; us -> ds); unconnected nodes end at a **dead end** (links in, none out, not an Outfall: dangling Break,
  wet well without pump, downstream link to a node not in the export) or a **closed loop** (sink strongly connected
  component, `_sink_loops`, iterative Kosaraju). Per place: sewers upstream, cause, nearest other sewer not attached
  (`_SewerGrid`, 50 m). Isolated nodes and links to missing nodes listed. Outfalls with sewers draining to each;
  **possible false outfalls** = terminal outfall with an unattached sewer within 5 m (missed join / reversed sewer:
  silently splits the network, no ICM error).
- New fields `wwn_outfall` (outfall reached, fewest links) and `wwn_connectivity` (ok / "no outfall: dead end at X" /
  "closed loop at X" / isolated / not imported) on ICM_Nodes and ICM_Conduits; `<stem>_ICM_connectivity.csv`
  (kind, node_id, cause, sewers, nearest_sewer, nearest_m, x, y); message block after the warnings (first line a
  warning when sewers are unconnected; top 30 places). Assumption: ICM follows link direction like this (unconfirmed).
- Export To ICM option **Gravity sewers only**: no rising mains / pumps / delivery nodes (stale ICM_Pumps and pump
  CSVs removed), wet wells with sewers in and none out -> Outfall ("wet well (gravity only export)"), nodes with no
  gravity sewer left out (WW_ / D_ points, isolated nodes).
- `_gpkg_points` (point + line WKB) replaces the line-only parser. Tests: toolbox 327 (13 new,
  `tests/test_icm_connectivity.py`), shared core 189 + 1 skipped, pyflakes clean. Synthetic 9,000-node network 0.05 s.

### Where we stopped (next session starts here)

1. User installs this branch's `wwnet-arcgis-update` artifact (Update WWNet, Refresh toolbox), runs Export To ICM on
   EdenHillv8 with *Gravity sewers only* ticked, and pastes the "Connectivity ..." message block (+ the false outfall
   line). Compare the unconnected sewer count with ICM's validation count after an ODIC import into an EMPTY network.
2. Then fix by cause: dangling ends near a main -> Join Dangling Sewer Ends / importer tee rules; closed loops ->
   flow direction in the importer; false outfalls -> missed joins; anything ICM reports that the trace does not ->
   ICM checks something else (get the exact message).
3. Pressure side parked by the user (section 15 item 2 still open).

### First gravity-only export (EdenHillv8, 2026-11-27) and IS sewers removed

Export (gravity only): 8,661 nodes, 8,650 sewers; **7,793 reach an outfall, 857 do not**: E3026 812 (dangling IS end,
nearest sewer 13.3 m), E1128 25, J30 7, E212 5, E534 2, E3749 1, E4713 1 (dangling ends), J114 4 (self-loop: a conduit
from J114 to J114; J114 has no invert). Outfalls 15: 13 wet wells + AG0110 (2 sewers), aa0133 (1). No false
outfalls. 7 `WW_` stations unmatched (no sewer end within the 1 m wet well distance of the PS point). Export took
360 s, nearly all ArcGIS GP (CreateFeatureclass 163 s for ICM_Nodes); the trace itself ~1 s.
User's screenshot of E3026: the free end of a DN150 IS stub off the DN300 main in Arthur Street ("seems to just be
a connection node"). So the IS stub there is oriented main -> E3026 AND the main past that point does not carry the
flow away (812 sewers can only leave through the stub): cause not confirmed (likely the orientation of the main's
segments around the IS junctions).
User: **"Can we just remove all IS sewers from the import. Causing more problems and not sure what value they add."**
Done: `ImportOptions.import_is_sewers` (library / template default true, unchanged for tests and the QGIS plugin);
the importer drops sewers whose MAINTYPE maps to "is" (E / IS) before any joining (count `is_sewers_skipped`,
summary line). ArcGIS Import Sewer Data and Import Network Data: parameter **Import IS (inspection shaft) sewers**
(`import_is`, Options / Sewer options), **default off**. Tests: shared core 191 + 1 skipped, toolbox 328.

Next (one step at a time): user installs the update, re-runs Import Network Data for EdenHill to a NEW GeoPackage
(IS box unticked, same pressure / register / join settings), re-runs Calculate Existing Demand (loads were on the old
nodes), Export To ICM gravity only, pastes the Connectivity block. If the Arthur Street main is still stuck, ask for
the three conduits' us / ds node and inverts there. Then J114 (self-loop) if it survives without IS sewers.

