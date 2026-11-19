# Reference survey for WWNet (Rust + QGIS wastewater modelling)

Survey date: 2026-11-19. Method: `git clone --depth 1` of each repo into the session scratchpad, then reading files. No code from these repos was run. Licences were read from the repo LICENSE files or, for crates, from the crates.io API.

Licence rule: GPL code must not be copied into the MIT/Apache project. Ideas, field names and schema shape can be used; code cannot.

---

## 1. USEPA/Stormwater-Management-Model (SWMM 5.2 engine)

- URL: https://github.com/USEPA/Stormwater-Management-Model
- Licence: public domain (README.md line 17: "released in the Public Domain"). No LICENSE file.
- Version seen: CMake `project(swmm-solver VERSION 5.2.4)`. Source headers say Build 5.2.0.

**Layout**
- `src/solver/` holds the engine: about 80 `.c`/`.h` files, including `link.c`, `forcmain.c`, `dynwave.c`, `dwflow.c`, `project.c`, `swmm5.c`, `input.c`, `keywords.c`, `output.c`, `controls.c`, `lid.c`, `mathexpr.c`, `hash.c`, `mempool.c`.
- Public header: `src/solver/include/swmm5.h`.
- `src/outfile/` is a separate library for reading `.out` binary files (`swmm_output.c`). The solver CMake does not link it.
- `src/run/main.c` builds the `runswmm` CLI.
- `bindings/swmm5.def` is the Windows DLL export list.
- CMake: `src/solver/CMakeLists.txt` globs `*.c *.h` into a shared library `swmm5`. It links `m` and OpenMP (`find_package(OpenMP COMPONENTS C REQUIRED)`). OpenMP is used in `dynwave.c` (lines 387, 609, `#pragma omp parallel num_threads(NumThreads)`) and `project.c`, which guards `#include <omp.h>` with `#if defined(_OPENMP)` and has a single-thread fallback.
- Windows-specific bits (`__declspec`, `__stdcall`) are behind `#ifdef`s in `swmm5.h` (`DLLEXPORT` is empty on non-Windows).

**Public API (`swmm5.h`, implemented in `swmm5.c`)**
- Lifecycle: `swmm_run(f1,f2,f3)`, `swmm_open(inp,rpt,out)`, `swmm_start(saveFlag)`, `swmm_step(&elapsed)`, `swmm_stride(n,&elapsed)`, `swmm_end()`, `swmm_report()`, `swmm_close()`.
- Diagnostics: `swmm_getMassBalErr`, `swmm_getVersion`, `swmm_getError`, `swmm_getWarnings`.
- Object access: `swmm_getCount(type)`, `swmm_getName`, `swmm_getIndex(type,name)`, `swmm_getValue(prop,index)`, `swmm_setValue(prop,index,val)`, `swmm_getSavedValue(prop,index,period)`, `swmm_writeLine`, `swmm_decodeDate`.
- Enums: `swmm_Object` (GAGE, SUBCATCH, NODE, LINK, SYSTEM), `swmm_NodeType` (JUNCTION, OUTFALL, STORAGE, DIVIDER), `swmm_LinkType` (CONDUIT, PUMP, ORIFICE, WEIR, OUTLET), and property codes for node/link/subcatch/system.
- Limits: `swmm_setValue` only accepts a few properties (rainfall gage, subcatch rpt flag, node lateral flow, node head, link setting, route/report step, no-report). You cannot change pump curves, wet-well geometry or pump on/off levels through the API. Those must be written in the .inp file.
- Global state: the engine uses process-wide globals (`globals.h`, 29 extern-style declarations). Two runs cannot share one process concurrently. Parallel scenarios need separate processes (or a fork/exec model), not threads.

**Force mains and pumps**
- `forcmain.c`: `forcemain_getEquivN`, `forcemain_getRoughFactor`, `forcemain_getFricSlope`, `forcemain_getReynolds`, `forcemain_getFricFactor`. Used from `link.c` (line 1032, 1092, 1125) and `dwflow.c` (line 212: `xsect->type == FORCE_MAIN && isFull` adds friction slope in the dynamic wave update).
- Force main equation (Hazen-Williams or Darcy-Weisbach) is a global option `FORCE_MAIN_EQN` (`keywords.c`).
- Pumps: `enums.h` lines 419-424 define `TYPE1_PUMP` to `TYPE5_PUMP` and `IDEAL_PUMP`. Curves are `PUMP1_CURVE` to `PUMP5_CURVE` (enums.h 445-449).
- `link.c`: `pump_readParams`, `pump_validate`, `pump_initState`, `pump_getInflow`. `Pump[k].pumpCurve` is set at line 354, and used at lines 1487 and 1562.
- `objects.h` line 734 onward: the `TPump` struct (type, pumpCurve, xMin, xMax, ...).

**What a Rust `build.rs` (`cc` crate) needs**
- Compile every `src/solver/*.c` with include path `src/solver/include` and `src/solver`. Link `m`.
- OpenMP is optional. Either compile without `-fopenmp` (then `_OPENMP` is undefined and the fallback is used) or add `-fopenmp` and link `gomp`.
- Do not compile `src/run/main.c` (it defines `main`). Do not compile `src/outfile` unless output reading is needed.
- Generate bindings from `src/solver/include/swmm5.h` with `bindgen`. The header is small and C-only, apart from `extern "C"` guards.
- Build a static library (`cc::Build::compile("swmm5")`) rather than a shared one.

**Risks**
- Global state, so no thread-safe concurrent runs. Use process isolation for scenarios.
- Limited setters: pumps, wet well geometry and controls are .inp-only. The Rust side must generate valid .inp files.
- C file and error handling uses `setjmp`/`longjmp`-style exception handling (`swmm_open` has `__except` for MSVC). Check this before wrapping calls in Rust panics.
- Tests (`tests/`) need Boost, so they are not useful as-is.

---

## 2. USEPA/EPANET2.2 and OpenWaterAnalytics/EPANET (EPANET 2.2+ engine)

- URLs: https://github.com/USEPA/EPANET2.2 (engine in `SRC_engines/`) and https://github.com/OpenWaterAnalytics/EPANET (the upstream engine, `src/`, `include/`, `CMakeLists.txt`).
- Licence: MIT. Confirmed in `EPANET2.2/SRC_engines/LICENSE` (MIT, 2017) and `EPANET/LICENSE` (MIT, 2019).
- The OWA repo is the newer working copy (it has `epanet2_enums.h` and `EN_PumpType` values). Use OWA for the build.

**Layout**
- `src/`: `epanet.c` (API), `epanet2.c` (legacy API), `input1.c`, `input2.c`, `input3.c` (INP parsing), `hydraul.c`, `hydsolver.c`, `smatrix.c`, `genmmd.c` (sparse solver), `quality.c`, `qualroute.c`, `rules.c`, `report.c`, `output.c`, `project.c`, `hash.c`, `mempool.c`, `leakage.c`, `validate.c`.
- `include/epanet2_2.h` (current API), `include/epanet2_enums.h`, `include/epanet2.h` (legacy).
- Opaque handle: `typedef struct Project *EN_Project;` (`EPANET/src/types.h` line 907, `struct Project`). Each project is an instance, so several projects can exist in one process. This is a better FFI model than SWMM's globals.

**Public API (`epanet2_2.h`)**
- Lifecycle: `EN_createproject`, `EN_deleteproject`, `EN_runproject`, `EN_open`, `EN_close`, `EN_saveinpfile`.
- Hydraulics: `EN_openH`, `EN_initH`, `EN_runH`, `EN_nextH`, `EN_closeH`, `EN_solveH`.
- Quality: `EN_openQ`, `EN_initQ`, `EN_runQ`, `EN_stepQ`, `EN_closeQ`, `EN_solveQ`.
- Object access: `EN_addnode`, `EN_addlink`, `EN_getnodevalue`, `EN_setnodevalue`, `EN_getlinkvalue`, `EN_setlinkvalue`, `EN_getnodeid`, `EN_getlinkid`, `EN_getcoord`, `EN_setcoord`, `EN_getcurve`, `EN_setcurve`, `EN_getpumptype` and the pump head-curve getter/setter (header lines 1423-1449), controls (`EN_addcontrol`, `EN_getcontrol`), rules (`EN_addrule`, `EN_getrule`, premise/action setters).
- Pump properties (`epanet2_enums.h`): `EN_PUMP_STATE`, `EN_PUMP_EFFIC`, `EN_PUMP_POWER`, `EN_PUMP_HCURVE`, `EN_PUMP_ECURVE`, `EN_PUMP_ECOST`, `EN_PUMP_EPAT`. Pump curve types in `epanet2_enums.h` include `EN_PUMP_CURVE` (head v. flow) and `EN_POWER_FUNC` (line 424); the header also documents a constant-power option.
- Report callback: `EN_setreportcallback` (header line ~581).

**Fit for WWNet**
- EPANET is a water distribution (pressurised) engine: no wet-well geometry, no gravity-sewer surcharge, no free-surface flow. It fits manifolded rising-main networks, as PLAN.md section 2 already proposes. It does not fit gravity sewers or wet-well dynamics. The `EN_setcurve` and pump curve accessors cover pump duty-point work.

**Risks**
- EPANET's wet-well and tank model is a level-based tank, not a sewer wet-well with area-vs-level tables and pump cycling. Use SWMM for wet-well dynamics.
- Boost-based tests in the OWA repo are not needed for the engine.

---

## 3. pyswmm/pyswmm and pyswmm/swmm-toolkit (Python wrappers)

- pyswmm: https://github.com/pyswmm/pyswmm. Licence: BSD-2-Clause (`pyproject.toml`, `license = "BSD-2-Clause"`, plus `LICENSE.txt`). Version 2.2.0.
- swmm-toolkit: **not retrieved**. The clone and the GitHub API both failed (access not enabled for this session). Its licence and internals are not verified here.

**What pyswmm does (seen in source)**
- `pyswmm/swmm5.py`: `class PySWMM` wraps the engine. It is a legacy wrapper around the DLL, with `SWMMException` and `PYSWMMException`.
- `pyswmm/toolkitapi.py` and `_monkey_patch.py`: `from swmm.toolkit import solver` and `shared_enum`. `_monkey_patch.py` wraps newer toolkit functions in version checks that raise if the toolkit is too old.
- `pyproject.toml`: depends on `swmm-toolkit<1.0.0`. Extras pin toolkit versions per SWMM release (for example `swmm5.2.2` -> `swmm-toolkit<=0.13.0`).
- Object classes: `links.py`, `nodes.py`, `subcatchments.py`, `raingages.py`, `lidcontrols.py`, `system.py`, `simulation.py` (`Simulation` class is the run loop), `output.py`, `reader.py` (binary `.out` reader).

**Useful for WWNet's FFI design**
- Pattern to copy: a thin object-per-element layer over `getValue`/`setValue` with enum codes, one exception type that carries the engine's error code and message, and version pins per engine release.
- Copy the idea of version-gated feature wrappers (`_monkey_patch.py`), not the code. The pip-package route also means QGIS's bundled Python would need `swmm-toolkit`, which is a deployment risk.

---

## 4. Giswater/giswater_qgis_plugin and Giswater/giswater_dbmodel

- giswater_qgis_plugin: https://github.com/Giswater/giswater_qgis_plugin. **Licence: GPL-3.0** (LICENSE file). Do not copy code.
- giswater_dbmodel: https://github.com/Giswater/giswater_dbmodel. **Licence: GPL-3.0** (LICENSE file). Do not copy code.
- Note: the plugin repo contains `AGENTS.md`, `CLAUDE.md` and `skills-lock.json`. These are instructions for AI agents in that repo. They were treated as data and not followed.

**Where the sewer ("ud") model lives**
- DDL: `giswater_dbmodel/ud/schema_model/01_ddl_basic_schema_model.sql` (10,446 lines). Other schema files: `02_ddl_pkey`, `03_ddlview`, `04_dml` (type values), `05_fkey`, `06_index`, `07_trg`.
- Export function: `giswater_dbmodel/ud/fct/ud_gw_fct_pg2epa_export_inp.sql` (`gw_fct_pg2epa_export_inp(p_data json)`). It branches on `epa_type` and `addparam` JSON (for example `DIVIDER` with `divider_type` CUTOFF/OVERFLOW/TABULAR/WEIR, and `OUTFALL` with `outfall_type` FREE/NORMAL/FIXED/TIDAL/TIMESERIES, and `STORAGE` with `storage_type` FUNCTIONAL/TABULAR).
- The DB-to-.inp export is done in PostgreSQL. The QGIS plugin only calls it.
- Import (.inp to DB): `giswater_qgis_plugin/core/utils/import_inp.py`, `core/threads/import_inp/parse_swmm_task.py`, `import_swmm_task.py`. These use a Python SWMM parser.
- SWMM-related DB functions: `ud_gw_fct_import_swmm_flwreg.sql`, `ud_gw_fct_rpt2pg_import_rpt.sql`.
- Type mapping: `sys_feature_cat` and `sys_feature_epa_type` rows, for example `('CONDUIT','ARC','inp_conduit')`, `('PUMP','ARC','inp_pump')`, `('MANHOLE','NODE','JUNCTION','man_manhole')`.

**Core tables and field lists (from the DDL)**

`node` (main table, abbreviated): `node_id, code, top_elev, ymax, elev, node_type, nodecat_id, epa_type, sector_id, state, state_type, annotation, observ, comment, dma_id, function_type, category_type, fluid_type, location_type, workcat_id, workcat_id_end, buildercat_id, builtdate, enddate, ownercat_id, muni_id, postcode, streetaxis_id, postnumber, descript, link, verified, the_geom, expl_id, num_value, feature_type, tstamp, lastupdate, lastupdate_user, insert_user, matcat_id, district_id, asset_id, drainzone_id, parent_id, expl_id2`

`arc`: `arc_id, code, node_1, node_2, y1, y2, elev1, elev2, custom_y1, custom_y2, custom_elev1, custom_elev2, sys_elev1, sys_elev2, arc_type, arccat_id, matcat_id, epa_type, sector_id, state, state_type, sys_slope, inverted_slope, custom_length, dma_id, workcat_id, builtdate, enddate, ownercat_id, descript, the_geom, expl_id, feature_type, nodetype_1, node_sys_top_elev_1, node_sys_elev_1, nodetype_2, node_sys_top_elev_2, node_sys_elev_2, pavcat_id, drainzone_id, asset_id, parent_id`

`connec`: `connec_id, code, top_elev, y1, y2, connec_type, connecat_id, sector_id, customer_code, demand, state, state_type, connec_depth, connec_length, arc_id, pjoint_type, pjoint_id, matcat_id, expl_id, asset_id, drainzone_id, ...`

`gully`: `gully_id, code, top_elev, ymax, sandbox, matcat_id, gully_type, gratecat_id, units, groove, siphon, connec_arccat_id, connec_length, connec_depth, arc_id, _pol_id_, sector_id, state, ..., epa_type, groove_height, groove_length, units_placement, connec_matcat_id, connec_y2, gratecat2_id, drainzone_id, expl_id2`

`cat_arc` (catalogue): `id, matcat_id, shape, geom1..geom8, geom_r, descript, link, brand, model, svg, z1, z2, width, area, estimated_depth, bulk, cost_unit, cost, m2bottom_cost, m3protec_cost, active, label, tsect_id, curve_id, arc_type, acoeff, connect_cost`

`cat_node` (catalogue): `id, matcat_id, shape, geom1..geom3, descript, link, brand, model, svg, estimated_y, cost_unit, cost, active, label, node_type, acoeff`

`cat_connec` (catalogue): `id, matcat_id, shape, geom1..geom4, geom_r, descript, link, brand, model, svg, active, label, connec_type`

`cat_mat_arc`: `id, descript, n, link, active` (roughness `n` per material)

Shape values seen: `cat_arc_shape` includes `FORCE_MAIN` (`04_dml`, line 143), so a rising main is a catalogue shape with a shape flag, not a separate table.

Inp tables (one per SWMM section, keyed by the feature id):
- `inp_pump`: `arc_id, curve_id, status, startup, shutoff`
- `inp_flwreg_pump`: `id, node_id, to_arc, order_id, flwreg_length, curve_id, status, startup, shutoff, nodarc_id` (pump as a flow regulator between a node and an arc)
- `inp_conduit`: `arc_id, barrels, culvert, kentry, kexit, kavg, flap, q0, qmax, seepage, custom_n`
- `inp_outfall`: `node_id, outfall_type, stage, curve_id, timser_id, gate`
- `inp_storage`: `node_id, storage_type, curve_id, a1, a2, a0, fevap, sh, hc, imd, y0, ysur, apond`
- `inp_junction`: `node_id, y0, ysur, apond, outfallparam`
- `inp_divider`: `node_id, divider_type, arc_id, curve_id, qmin, ht, cd, y0, ysur, apond`
- `inp_orifice`, `inp_weir`, `inp_outlet`, `inp_curve` (`id, curve_type, descript`), `inp_curve_value`, `inp_timeseries`, `inp_controls` (`id, sector_id, text, active`), `inp_options` lives in `sys_param_user` (for example `inp_options_force_main_equation`).
- Field-level detail for `inp_*` lives in `addparam` JSON on the feature row. The exporter reads `temp_t_node.addparam::json ->> 'divider_type'` and similar.

**Ideas worth taking (fields and patterns, not code)**
- Separate **feature** tables (node, arc, connec, gully) from **catalogue** tables (cat_arc, cat_node) with `arccat_id`/`nodecat_id` foreign keys. Keep dimension geometry in `geom1..geomN` columns with a shape code.
- Use an `epa_type` column on each feature to route it to the right engine section. Keep the engine-specific parameters in one JSON column (`addparam`) validated by type, so the schema does not grow per engine.
- State fields: `state` (existing/planned/...), `state_type`, `workcat_id`/`workcat_id_end`/`builtdate`/`enddate` for asset history, `verified`, `uncertain` and `inventory` flags. These line up with PLAN.md's data-source/confidence flags.
- Expl/sector/DMA/drainage-zone columns: `expl_id`, `sector_id`, `drainzone_id` for grouping and reporting.
- Separate pump-station records (`inp_pump`, `inp_flwreg_pump`) carrying status, startup and shutoff levels, as PLAN.md's pump station section needs.

**Risks**
- GPL-3.0. Only field names, table names and concepts may be reused. Do not copy SQL, Python or function bodies into an MIT/Apache project. The DDL column lists above are facts about the schema, but any table DDL text reused verbatim would be copying. Write our own schema.
- The model is PostgreSQL/PostGIS-specific (`SCHEMA_NAME`, `gw_fct_*`), not GeoPackage. Port the ideas, not the functions.

---

## 5. Rust crates (crates.io API and `cargo search`, 2026-11-19)

| Crate | Version | Last updated | Licence | Notes |
|---|---|---|---|---|
| `epanet-sys` | 2.3.0 | 2026-03-25 | MIT | Raw FFI for EPANET 2.3. Repo: cameron-devine/epanet-sys. Use or copy its build approach. |
| `epanet` | 0.3.0 | 2026-03-25 | MIT | Safe bindings over EPANET 2.3 (repo cameron-devine/rusty-epanet). Possible fallback. |
| `epanet-rs` | 0.2.3 | 2026-07-02 | MIT OR Apache-2.0 | Pure-Rust re-implementation of the EPANET2 hydraulic solver (vitens). Not an FFI. Would need validating against EPANET. |
| `geopackage` | 0.14.4 | 2026-11-10 | MIT OR Apache-2.0 | GPKG read/write. `geopackage-core` is the no-IO codec (2,107 recent downloads). |
| `wkb` | 0.9.3 | 2026-10-14 | MIT OR Apache-2.0 | Georust WKB reader/writer, high download count (2.8M total). Good default for geometry blobs. |
| `hydra-sdk`, `hydra-engine-uds`, `hydra-interop-swmm`, `hydra-interop-epanet` | 17.0.2 | 2026-11-09 | **AGPL-3.0-only** | Do not depend on these (copyleft, network-use clause). |
| `stormsewer` | 0.10.1 | 2026-09-22 | **GPL-3.0-or-later** | Do not depend on this. Use only as a reference for Rational/Manning checks, with no code copied. |

- No crate was found that wraps SWMM 5.2 in a stable, MIT/Apache form. `cargo search swmm` only returned the Hydra and stormsewer crates above, and `swmm-sys`/`swmm5` do not exist on crates.io.
- Both licences of the WWNet plan (MIT/Apache) make the EPANET crates (MIT) usable. The SWMM engine itself is public domain, so vendoring it is allowed.

---

## 6. Existing QGIS SWMM plugin

- `Jannik-Schilling/generate_swmm_inp`: https://github.com/Jannik-Schilling/generate_swmm_inp. Licence: **GPL-2.0**. Last pushed 2026-06-21. Found via `gh api search/repositories`. Not cloned or read, because only its licence and existence were checked. Treat as idea-only.
- Other hits (not cloned): `rdtcamingawan/SWMM-Tools` (GPL-3.0), `Work-RH2/stormwater-report-skill` (MIT, a report skill, not a model builder), `dickinsonre/EPANET-SWMM5-Generate` (no licence).

---

## Recommendations for WWNet

- Vendor the SWMM 5.2.4 solver (`src/solver/*.c`, `include/swmm5.h`) under `third_party/swmm/`. Build it with the `cc` crate in `build.rs` (no `src/run`, no `src/outfile`). Make OpenMP optional. Generate bindings with `bindgen` from `swmm5.h`. Keep the public-domain notice in the vendored folder.
- Run each SWMM scenario in a separate process (the engine uses globals, so threads are unsafe). Use `rayon` only to schedule the processes.
- Write .inp files from Rust for pumps, curves, wet wells and controls, because `swmm_setValue` cannot set them. Test the .inp writer with `insta` snapshots.
- For rising-main networks, use EPANET via the MIT `epanet-sys` crate or vendor the OWA EPANET 2.2 `src/`. Use `EN_Project` handles, which allow several projects per process. Do not use EPANET for gravity sewers or wet-well dynamics.
- Do not depend on the Hydra or stormsewer crates (AGPL/GPL). Use `geopackage` (MIT/Apache) for GPKG I/O and `wkb` for geometry blobs, or write GPKG tables directly with `rusqlite` as PLAN.md proposes.
- Adopt Giswater's concepts, not its code: feature tables (node, arc, connec, gully) separate from catalogue tables (cat_arc, cat_node), an `epa_type` routing column, and a JSON `addparam` for engine-specific fields. Write our own schema.
- Field set to add from Giswater's shape: `state`, `state_type`, `workcat_id`, `builtdate`, `enddate`, `verified`, `uncertain`, `expl_id`/`sector_id`/`drainzone_id`, `asset_id`, and the pump fields `status`, `startup`, `shutoff`, `curve_id`.
- Add a `force_main` shape to our cat table (as Giswater does with `FORCE_MAIN`), and a global force-main equation option (Hazen-Williams or Darcy-Weisbach).
- Mirror pyswmm's pattern: one exception type carrying the engine's error code, a thin per-element wrapper over get/set, and version pins per engine release. Avoid the pip `swmm-toolkit` route inside QGIS unless its licence and packaging are checked first (not verified here).
- Before adopting any code, check the licence of each source. Only public-domain (SWMM), MIT (EPANET, pyswmm's BSD-2 is a Python dependency, not ours) and MIT/Apache crates can be copied or vendored. Keep GPL repos (Giswater, generate_swmm_inp) as idea-only references.
- Verify swmm-toolkit (not retrieved) and the generate_swmm_inp internals (not read) before relying on them.
