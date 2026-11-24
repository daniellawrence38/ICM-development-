# WWNet — session handover (2026-11-24; base 2026-11-23)

Branch: `ccr-54bf38a8-b92qd9` (all work pushed; last code commit `e25f28d`). 2026-11-24: fast-forwarded from `ccr-472a9a6a-he69zu`, then pressure side in Export To ICM (§11). **Start here: §11 'Where we stopped'.** Earlier: `ccr-472a9a6a-he69zu`, `ccr-974cdaeb-c682le`. NOT merged: `ccr-7bd083b1-j0qlhq` (schema v3 / toolbox UX, diverged from `e107482`).
Plan: `docs/PLAN.md`. OSS survey: `docs/reference-survey.md`. Function overview deck: `docs/WWNet_functions.pptx`.

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
- Last build installed by the user: **`60e8cc8`** (Self Test in Pro all PASS/WARN). `e25f28d` (headloss type) is pushed but NOT yet installed.

## 4. Architecture

| Path | Role |
|---|---|
| `crates/wwnet-core` | Model structs, GeoPackage I/O (schema v2), topology, validation |
| `crates/wwnet-hydraulics` | Colebrook-White / Manning part-full |
| `crates/wwnet-rules`, `rulesets/` | DS 50 clause-referenced checks |
| `crates/wwnet-design` | Steady design (ADWF, GSDF = ADWF × 1.5, checks) |
| `crates/wwnet-swmm-sys` | Vendored EPA SWMM 5.2.4 C + hand-written FFI |
| `crates/wwnet-sim` | .inp writer (`inp.rs`), **pump stations / rising mains (`pumps.rs`)**, runner, results → `wwn_sim_*` tables |
| `crates/wwnet-cli` | `wwnet` CLI (dev/CI only) |
| `crates/wwnet-py` | PyO3 abi3 module `wwnet_native` |
| `qgis_plugin/wwnet/core/` | Shared pure Python (importer, schema, **repair.py**) — packaged for ArcGIS as `wwnet_shared` |
| `arcgis_toolbox/WWNet.pyt` + `wwnet_arcgis/` | ArcGIS Pro toolbox (18 tools, §5) |
| `templates/schema.sql` | GeoPackage schema (incl. `wwn_sim_pumps`, `wwn_sim_pump_stations`, `wwn_sim_rising_mains`) |
| `templates/import/watercorp_sewer.toml` | Field roles + code maps (gravity and `[pressure]`) |
| `templates/demo/demo.gpkg` (from `examples/demo`) | Shipped demo model used by Self Test |

Key ArcGIS modules: `tools_impl.py` (tool logic), `gpkg.py` (`split_datasource` handles Pro's `Instance=<file>.gpkg,Dataset=main.%<table>`), `maplayers.py`, `sewer_import.py`, `pressure_import.py`, `pumpdata.py`, `icm_export.py`, `gpenv.py`, `updater.py`, `selftest.py`, `symbology.py`.

## 5. Toolbox tools and status

| Tool | Category | Status |
|---|---|---|
| New Model | Model | verified |
| Add Model To Map | Model | verified |
| Check Model | Model | verified (now also lists missing pump data) |
| **Join Dangling Sewer Ends** | Model | new: splits the main at a missed tee, inserts a tee node, invert = US invert − grade × distance; preview default. Used by the user to fix E4/E46 |
| Import Sewer Data | Data | verified (61 sewers); new Advanced param *Tee join distance* (0.5 m) + "possible missed tee" report |
| **Import Pressure Data** | Data | new, **not yet run on real data** |
| **Set Pump Station Data** | Data | new, not yet run in Pro (manual entry: wet well dia/area, levels AHD or depth, duty/assist/standby, rate or pasted curve) |
| Import Diurnal Profile | Data | fake-tested; Self Test PASS in Pro |
| **Export To ICM** | Data | **verified: gravity model loads into ICM 2024.5 and runs.** Terminal chambers → Outfall; dangling ends stay Break (warned); DEM fill of missing ground levels; GDA2020 or GDA94 output. **2026-11-24: rising mains + pumps now exported (§11), not yet run in Pro / ICM** |
| Run Design | Analysis | verified |
| Run Simulation | Analysis | verified (gravity); **pump station results new, not yet seen in Pro** |
| Trace | Analysis | verified (after dataSource fix) |
| Pipe Capacity | Analysis | verified (Self Test) |
| **Self Test** | Assistant | verified in Pro |
| **Update WWNet** | Assistant | verified in Pro |
| Verify Setup | Assistant | verified |
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
