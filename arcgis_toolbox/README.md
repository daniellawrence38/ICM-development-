# WWNet ArcGIS Pro toolbox

Python toolbox (`WWNet.pyt`) for **ArcGIS Pro 3.3+** that fronts the WWNet wastewater design tool
(Water Corporation WA, DS 50 gravity sewers). The design engine stays in the `wwnet` command-line tool;
the toolbox creates and maps models, runs checks / design / traces, and hosts the optional
Claude-powered assistant. Models are the same EPSG:7850 GeoPackages used by the QGIS plugin.

## Tools

| Tool | What it does |
|---|---|
| Import Diurnal Profile | (category Data) Imports a diurnal profile (CSV / TXT file or text pasted from ICM: `Time<TAB>Factor`; hh:mm or minutes; any interval) into a model, optionally as the default profile; warns if the mean is not 1 (tick *Normalise* to divide by the mean). Example: `templates/profiles/example_residential.csv` (APPROXIMATE, digitised from a screenshot) |
| Import Sewer Data | (category Data) Converts Water Corporation gravity-sewer layers into a new WWNet GeoPackage, checks it and adds it to the map; see [Import Sewer Data](#import-sewer-data) |
| New Model | Creates a GeoPackage from `schema.sql` (sqlite3, schema v2) and adds the layers to the active map in a `WWNet` group layer |
| Add Model To Map | Adds the `wwn_*` layers (`<file>.gpkg\main.<table>`) of an existing model and applies symbology: nodes by `node_type`, conduits by DN with an Arcade label `DN225 1:300`. Symbology failures only warn |
| Check Model | `wwnet_native.check` (same output as `wwnet check --json`); messages via AddMessage/AddWarning/AddError; optional output issues table |
| Run Design | `wwnet_native.design` (same output as `wwnet design --json`); prints the pass/warn/fail summary, a per-conduit table (DN, grade, GSDF / pumped / total / governing capacity (Cap = lesser of Table 4.4 and hydraulic) / T4.4 / Hyd L/s, d/D, velocity, result, worst message) and failing/warning checks with DS 50 clauses (max 5 per rule; info such as "cover not checked" is summarised in one line). By default adds a temporary in-memory layer `Design results — <model>` (polylines coloured pass/warn/fail, labelled with flow/capacity and d/D) to the active map and selects failed conduits on it and on the model's conduit layer. The layer lives in `memory\` and is not saved: re-run Run Design after restarting Pro. "Add results table to map" (off by default) also adds the standalone `wwn_results_conduits` table |
| Trace | Upstream / downstream from a node (value list filled from the Nodes layer); selects traced nodes and conduits |
| Run Simulation | `wwnet_native.simulate` (SWMM dynamic wave; needs a wheel built with the simulation service, else the `wwnet simulate` CLI fallback). Parameters: duration h (48), warm-up h (24), roughness manning/ks, Manning n (0.013), flow scale (1.0), show results layer, optional `.inp` export. Messages: summary (inflow ADWF, peak outlet flow, continuity error, surcharged / flooded counts), top-20 conduits by max d/D, warnings. Adds the temporary layer `Simulation results — <model>` (conduits green < 0.5, yellow 0.5-0.75, orange 0.75-1.0 d/D, red surcharged; labelled `<flow> L/s d/D <x>`; fields `max_flow_lps`, `max_d_over_d`, `max_velocity_mps`, `surcharged_min`) and, only if a node floods, `Simulation nodes — <model>` (red points). Full results are stored in the model's `wwn_sim_*` tables |
| Pipe Capacity | Colebrook-White calculator (DN, grade, ks, flow) + DS 50 checks; uses `wwnet_native` if importable, else the shared Python fallback |
| Set Assistant API Key | Stores the Anthropic key (`GPEncryptedString`) with Windows DPAPI; default model / effort; optional fallback CLI path |
| Verify Setup | Prints where `wwnet_native` / `anthropic` / dependencies are loaded from and whether they import; optional live API test (tiny request, key never shown) |
| Ask WWNet Assistant | DS 50 assistant with tools `list_layers`, `get_selected_features`, `query_features`, `get_feature`, `pipe_capacity`, `check_conduit`, `residential_gsdf`, `lookup_ds50`, `propose_edits` |

## Import Sewer Data

Connection point loads: with a connection point layer, each connection point within the sewers' extent + the search distance
(default 10 m, spatial filter, never a full scan) is assigned to the nearest imported sewer (point-to-line distance within the
search distance) and counted as one connection (630 L/d) at that sewer's UPSTREAM node, aggregated into one `wwn_node_loads` row
per node (`CP-<node_id>`). The messages report `Loads: N connection point(s) assigned to M node(s); K unassigned; ADWF total X L/s`.
Tick off *Create loads from connection points* to skip. Run Design reports ADWF / GSDF totals; the results layer has `adwf_lps`.
Add Model To Map upgrades a v1 model to schema v2 (new tables and the subcatchment `adwf_lps` / `profile_id` fields).

Converts the Water Corporation (WA) layers *Sewer access chambers* (points), *Sewer connection points* (points,
optional) and *Gravity sewers* (polylines) into a new WWNet GeoPackage. Needs a `wwnet_native` that has
`write_model` (rebuild the wheel / use the current bundle). Logic: `qgis_plugin/wwnet/core/importer.py` (pure
Python, shared with QGIS); defaults and code maps: `templates/import/watercorp_sewer.toml`.

**Parameters**: the three layers, output `.gpkg` (an existing file needs *Overwrite the outputs of geoprocessing
operations*), snap tolerance (0.5 m), grade source (`levels` | `field`), *Use selected features only* (off; a layer
with no selection is read in full), *Add to map* (on), then one **Field** parameter per role. Picking a layer fills
its fields with the Water Corporation names that exist in it; change any of them.

| Role | Layer | Default field | Used for |
|---|---|---|---|
| ac_id / ac_surface_level | access chambers | `PACID` / `TOPLEV` | `node_id`, `surface_level` (lid, m AHD) |
| chamber_type | access chambers | `CHAMBER_TY` | `M` -> `ac`, `J` -> `ms`, `I` -> skipped; optional (missing field: all `ac`) |
| cp_id | connection points | `ID` | node id when referenced by a sewer |
| pipe_id | sewers | `ID` | `conduit_id`, `asset_id` |
| us_node / ds_node | sewers | `US_MANHOLE_ID` / `DS_MANHOLE_ID` | end nodes (id match first) |
| us_invert / ds_invert | sewers | `UPSTREAM` / `DOWNSTREAM` | invert levels, m AHD |
| flow_grade | sewers | `FLOW_GRADE` | "1 in x", cross-check (and fallback with grade source `field`) |
| length | sewers | `LENGTH` | `length_m` (else geometry length) |
| diameter | sewers | `HEIGHT` (mm) | `dn` |
| status / sewer_type / material | sewers | `STATUS` / `MAINTYPE` / `MATERIAL` | coded values |
| purpose / planset / install_date | sewers | `PURPOSE` / `PLANSET` / `DATEINST` | notes; `install_year` |

**Rules and assumptions**
- Coded values use the text before the first `-` (`A- actual` -> `A`); maps are in the TOML. Unknown codes fall back
  (status `existing`, type `reticulation`, material `other`, chamber `ac`) with a warning and are listed with counts.
  `MAINTYPE` `E` (meaning unconfirmed) is *assumed* reticulation: one "Assumed MAINTYPE codes" summary line, no
  per-sewer warning.
- Chambers: `CHAMBER_TY` `M` = standard manhole -> `ac`, `J` = maintenance shaft -> `ms`, `I` = inspection shaft ->
  **skipped silently** (they sit at sewer ends and have no `PACID`; only counted: "skipped N inspection shaft(s)").
  A non-`I` chamber without an id warns (`ac_no_id`).
- **Connectivity is by geometry.** Gravity sewers are segmented between chambers (split at junctions and
  connections) and every segment of a run carries the run's `US_MANHOLE_ID` / `DS_MANHOLE_ID`, so the ids are only
  used for direction and validation. A segment end within the snap tolerance (0.5 m) of a chamber connects to it,
  else of a connection point; remaining coincident ends share a junction node `J<n>` ("segment junction"); a lone end
  that lies within `tee_tolerance_m` (template `[options]`, default 0.5 m) of another sewer's mid-line is a
  **T-junction** (junction fitting; mains are not split in the GIS): the main is split there into conduits
  `<id>` (upstream piece, keeps the id), `<id>~2`, ... (downstream), all with `asset_id` = original id and note
  "split at tee for <branch>"; lengths are split by chainage and inverts interpolated linearly along the oriented
  main; a junction node `T<n>` ("tee (mid-line junction)", invert = interpolated main invert) is created and the
  branch end attaches to it (warning `tee_joined`; `tee_adverse` when the branch ds invert is >0.01 m below the
  main's invert). Points within the snap tolerance of the main's own ends are endpoint cases, not tees. Any other
  lone end becomes a dangling end node `E<n>`. No per-sewer placeholders are created.
- Direction: for each (US id, DS id) run the chain of segments is oriented from the end at the US chamber (or the
  DS chamber) through the shared junctions; leftovers take the direction of already oriented neighbours, else the
  digitised direction is kept (`direction_unknown`). Remaining unknowns are resolved in this order, to a fixed
  point (each counted as `Direction: ... N by tee rule, N by dead-end rule, N by IS/IO rule` and noted on the conduit):
  (0) tee rule (first): a branch end joined at a tee is its downstream end, so an IS sewer with both ends free is
  oriented into the main and its other end becomes the `is_end` upstream; split pieces share their sewer's direction;
  (a) dead-end rule: exactly one end attached to the network (chamber, connection point, or a junction shared with
  another segment) and the other dangling -> the dangling end is upstream; (b) IS/IO sewers (`MAINTYPE` `E`/`IS`/`IO`):
  the end not at a chamber is upstream (covers a blank US id); (c) neighbour propagation again through junctions.
  Only segments really flipped are reversed (`direction_reversed`). A segment end at a chamber that is neither its US nor DS id warns `id_mismatch`.
- Each segment keeps its own `UPSTREAM`/`DOWNSTREAM` levels as `us_invert`/`ds_invert`. Node `invert_level` = lowest
  `us_invert` of the outgoing segments, else the lowest `ds_invert` of the incoming ones (noted on the node).
  Falls into a node (incoming invert above the node invert, e.g. falls through manholes) are normal and not
  reported; the model check only flags an incoming invert below the node (warning), a fall of more than 5 cm into a
  maintenance shaft (info `FALL_THROUGH_MS`) and an outgoing invert off the node invert (info).
- Connection points become nodes only when a sewer end touches them (or names them by id and is within 2 m):
  `io_end` if an `io` sewer touches them, else `junction`. **The connection point layer is never read in full**:
  only points whose id appears as a sewer US/DS id not matched by a chamber (`ID IN (...)`, 900 ids per query) and
  points intersecting the sewer extent + 2 m (`SearchCursor` spatial filter; where-clause only if the installed
  arcpy rejects it) are read. A selection in the layer is honoured with *Use selected features only*. With sewers
  selected and no chambers selected, chambers are read the same way (extent + 2 m).
- **Context chambers**: with *Use selected features only* the chamber selection never starves connectivity. After
  the sewers are read, chambers whose `PACID` equals a US/DS manhole id used by the read sewers but not yet read
  (`PACID IN (...)`, 900 per query, quoted/numeric per field type) and chambers within the sewer extent + 2 m
  (spatial filter, same fallback as connection points) are read too, with the chamber-type rule applied. The chamber
  layer is never scanned in full when sewers are selected. Message: "Access chambers: N selected + M
  referenced/nearby read (not the whole layer)". They are reported as "context chamber(s)"; context chambers no
  sewer end touches are dropped (`Ignored N context chamber(s)`).
- Dangling ends summary: "dangling ends: X IS/IO upstream ends (expected), Y dead-end upstream starts, Z other
  (listed)". "Other" (a dangling *downstream* end) is listed as warning `dangling_end` with conduit id and
  coordinates (max 20).
- Grade from levels is compared with `FLOW_GRADE`; a difference over 10 % warns with both values. Grade source
  `field` derives a missing invert from the other invert, length and `FLOW_GRADE` (data_source `assumed`); it never
  overrides levels that are present. The summary adds one `Grade check` line: the median of levels grade /
  `FLOW_GRADE` (1.0 = agree) using the `LENGTH` field, the geometry length and, if the sewer layer has it, the
  optional `Shape_Length` field (template key `shape_length`, not a tool parameter), plus median `LENGTH` /
  geometry length, to identify why the two grades differ. The levels grade is always the one used.
- Diameter: a standard DN (100-1200) is used as is; any other value maps to the nearest standard DN and the raw
  value is kept in `internal_diameter_mm`. **If no diameter field is mapped every sewer gets DN 150 (assumed) and a
  loud warning is printed: capacity results are meaningless until real diameters are supplied.**
- Location is left at the schema default (`road_reserve`); `PURPOSE`/`PLANSET` go to notes.
- Coordinates are read with `spatial_reference` EPSG:7850. If a source is GDA94 (28348-28358, 4283 or a GDA 1994
  name) `arcpy.env.geographicTransformations` is set to a **direct** GDA94 -> GDA2020 transformation chosen from
  `arcpy.ListTransformations`: the name must contain `GDA_1994_To_GDA2020` and neither `AGD` nor `+` (composites via
  AGD66 are wrong); preference NTv2 conformal-and-distortion, then NTv2 conformal, then any other direct one. None
  listed (or a failure to set it) only warns. Curved segments are read as their vertices.
- Messages: counts, then a warning summary by code, then up to 40 example warnings (max 5 per code). Model-check
  findings are never `AddError` (Pro would mark a finished tool "Failed"): error-severity ones are warnings prefixed
  `ERROR:`; `AddError` is only for real failures (exceptions). The same applies to Check Model.
- After writing, `wwnet_native.check` runs; counts and the first 50 issues are printed, then the layers are added.

## Install

### Recommended: the no-install bundle (no pip, no environment clone, no `.exe`)
1. Download the CI artifact **`wwnet-arcgis-bundle-win-py311`** (GitHub Actions > CI run > Artifacts) and extract the
   zip to a **local, non-OneDrive** folder you can write to, e.g. `C:\Tools\WWNet-ArcGIS` (OneDrive sync can lock
   `.pyd` files and Pro caches modules).
2. In ArcGIS Pro: Catalog pane > Toolboxes > right-click > **Add Toolbox** > select `WWNet.pyt`.
   After updating files, right-click the toolbox > Refresh (or restart Pro).
3. Run **Verify Setup**. It lists the Python version and where `wwnet_native`, `anthropic`, `pydantic`, `httpx2`,
   `jiter`, `typing_extensions`, `anyio`, `truststore`, `certifi` are loaded from, and whether `import anthropic` /
   `import wwnet_native` work. Tick *Live API test* (after step 4) for one tiny request with your stored key.
4. Run **Set Assistant API Key**. The key is encrypted with DPAPI (current Windows user) in
   `%APPDATA%\WWNet\anthropic_key.bin`; model / effort / CLI path go in `%APPDATA%\WWNet\settings.json`.
   Lookup order when running: `ANTHROPIC_API_KEY` environment variable, then the DPAPI file. The key is never printed.

How it works: the zip carries `vendor/` (anthropic, pydantic, pydantic_core, httpx2, httpcore2, jiter, truststore,
typing_extensions, `wwnet_native` ... built for Python 3.11 win_amd64, the version in ArcGIS Pro 3.3). The toolbox puts
`vendor/` at the front of `sys.path` when it loads (`wwnet_arcgis/vendor.py`); nothing is installed into Pro's Python.
`anyio`, `sniffio`, `idna` and `certifi` are deliberately **not** shipped: Pro's own copies are used, so Pro's
Jupyter and other tools are unaffected. `anthropic` needs `typing_extensions >= 4.14` whereas Pro ships 4.9 (and has
usually imported it already); the toolbox then swaps in the vendored copy for the running session (backward
compatible; Verify Setup prints a note when it does). Python in Pro other than 3.11 needs a rebuilt bundle.
Build one yourself: `pip install --target vendor --only-binary=:all: anthropic <wwnet_native wheel>`, delete the
`anyio*`, `sniffio*`, `idna*`, `certifi*` folders, then `python package.py --vendor-dir vendor` (package.py also
skips those four if still present).

Behind a TLS-inspecting proxy the assistant trusts the Windows certificate store (httpx2 uses `truststore` on
Windows; for older SDKs the toolbox calls `truststore.inject_into_ssl()` only during an assistant run). If
`SSL_CERT_FILE` / `SSL_CERT_DIR` is set, httpx2 uses that instead.

### Alternative: clone Pro's environment and pip install (if you may)
1. Extract `wwnet-arcgis-<ver>.zip` (build it with `python package.py`, or use the repo checkout directly) and add
   `WWNet.pyt` as above.
2. `arcgispro-py3` is read-only: Pro > Settings > **Package Manager** > Environment Manager > *Clone Default*;
   activate the clone and restart Pro. In *Python Command Prompt* run
   `pip install wwnet_native-<ver>-cp39-abi3-win_amd64.whl` (CI artifact `wwnet_native-windows-abi3`, or build from
   `crates/wwnet-py` with `maturin build --release`) and, for the assistant, `pip install anthropic`.
3. The `wwnet` CLI is only an optional fallback (path in *Set Assistant API Key* > `PATH` >
   `<toolbox>\bin\wwnet.exe`) when the module is missing; it is not needed.
4. Run **Set Assistant API Key** and **Verify Setup**.

### Development (no packaging)
Keep the toolbox inside the repository: `wwnet_arcgis/shared.py` then imports the shared modules straight from
`../qgis_plugin/wwnet` (aliased as `wwnet_shared`). The packaged zip carries a copy under `wwnet_shared/`
(`core/*` and `assistant/{engine,client,prompt,tools,install}.py`; QGIS-dependent files are never copied, and a
test enforces that nothing shipped imports `qgis` / Qt).

## Corporate IT caveats
- The default environment (`arcgispro-py3`) is read-only for most users and cloning may be blocked by the proxy:
  use the no-install bundle (above), which needs neither a clone nor pip.
- The assistant needs outbound **HTTPS (443) to `api.anthropic.com`**; behind a TLS-inspecting proxy the corporate
  root CA must be trusted by the cloned environment. Check, design, trace and capacity need no network.
- No `.exe` is run: only the `wwnet_native` Python extension (`.pyd`) is loaded by Pro's own Python, so
  application-control rules that block unsigned executables do not apply to the toolbox.
- `%APPDATA%\WWNet` is per user (roaming); DPAPI blobs only decrypt for the same Windows user, so a copied key file
  is useless on another account.

## Data privacy
When the assistant uses its tools, **attribute values of the features it reads** (IDs, levels, DN, grades, notes ...)
and your question are sent to Anthropic's API, together with the DS 50 ruleset and schema summary. Geometry is not
sent. Do not use the assistant on models containing data you may not disclose. Conversation history stored locally
in `%APPDATA%\WWNet\history\<hash>.json` (one file per project + map) keeps only your questions and the
assistant's final answers; tick **Reset conversation** to delete it.

## The assistant and edits
`propose_edits` never writes by itself. With **Apply proposed edits** unticked (default) the run prints the proposals
as a table (layer, id, field, current, proposed) and a note to re-run with the box ticked. With it ticked, the
proposals made *in that run* are validated against the schema (enums, types, no id/geometry edits, results/issues
tables read-only) and written with `arcpy.da.Editor` + `UpdateCursor`; each change is reported (`old -> new`).
Nothing else is ever written. Because each run is a fresh model call, a re-run may propose slightly different edits:
review the messages afterwards.

## Editing GeoPackages in Pro
- Pro reads GeoPackage as a simple feature class (`file.gpkg\main.wwn_conduits`). Editing is supported; `fid` is the
  ObjectID and tables need the `fid` primary key from `schema.sql`.
- Do not have an open Pro **Edit** session with unsaved edits on the layer (or a join on it) when applying
  assistant edits: the cursor will fail with a lock error. Save/discard first.
- `wwnet_native` opens the file itself: save edits in Pro before Check/Design. Remove joins before editing.
- `wwn_results_conduits` and `wwn_issues` are rewritten by Design; treat them as read-only.
- Layers are added by catalog path (`main.<table>`); if a table is missing from `gpkg_contents` it is skipped.

## Feature comparison with the QGIS plugin

| Feature | QGIS plugin | ArcGIS toolbox |
|---|---|---|
| New / open model, grouped layers | yes (menu + toolbar) | yes (tools) |
| Styling | nodes by type, conduits by DN + label, value-map widgets, defaults | nodes by type, conduits by DN + Arcade label; no form widgets/defaults |
| Check model | dock with click-to-zoom list | geoprocessing messages + optional table (no dock) |
| Design run | `wwnet design` result in layer | summary, results table/join, select failed |
| Trace | `wwnet_native.trace` only | upstream/downstream tool with selection |
| Pipe capacity | assistant tool only | dedicated calculator tool |
| Assistant UI | chat dock, streaming, cancel | tool dialog, streamed to messages, no cancel mid-run |
| Assistant tools / engine / prompt | shared | **same shared modules** |
| Edit approval | per-edit dialog, selects features | tick-box up front; proposals table; applied only in that run |
| Key storage | QGIS auth DB / QgsSettings | Windows DPAPI file / env var |
| `anthropic` install | one-click pip | none: shipped in the bundle's `vendor/` (or manual pip in a cloned env) |
| Conversation | live in dock | persisted per map, resettable |
| Platforms | Windows/Linux/macOS | Windows (ArcGIS Pro) |

## Development

    pip install pytest anthropic
    # build + install the native wheel (native-dependent tests skip without it)
    cd crates/wwnet-py && maturin build --release && pip install --force-reinstall ../../target/wheels/wwnet_native-*.whl
    cargo build -p wwnet-cli          # only for the CLI-fallback/parity tests (or set WWNET_EXE)
    pytest arcgis_toolbox/tests -q    # no ArcGIS needed: tests use a fake `arcpy` over sqlite3
    python arcgis_toolbox/package.py  # dist/wwnet-arcgis-<ver>.zip (add --vendor-dir <folder> for the no-install bundle)

Not verifiable outside ArcGIS Pro (see the final notes in the change description): real `arcpy` behaviour for
layer/group creation, symbology, `Editor` on GeoPackage, joins and selections, parameter UI, and the real DPAPI calls.
