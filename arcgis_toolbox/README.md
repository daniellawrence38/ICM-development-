# WWNet ArcGIS Pro toolbox

Python toolbox (`WWNet.pyt`) for **ArcGIS Pro 3.3+** that fronts the WWNet wastewater network engine
(`wwnet_native`, Rust, with the EPA SWMM 5.2 dynamic-wave engine built in). Models are EPSG:7850 GeoPackages
(schema v3). There is no AI assistant in the ArcGIS toolbox (removed 2026-11-24).

Every tool's *WWNet model* parameter is filled with the model shown in the active map; browse to another `.gpkg`
to override. Results are stored in the model and shown as layers that stay after Pro restarts: **Design results**,
**Simulation results (conduits / nodes)**, **Simulation comparison** and **Issues** (one point per finding of the
last Check / Design / Import / Simulation, column `source`).

## Tools

| Tool | Category | What it does |
|---|---|---|
| New Model | Model | Creates an empty model GeoPackage and adds its layers to the map (WWNet group) |
| Add Model To Map | Model | Adds a model's layers (and result / issue layers that hold data); upgrades older models to schema v3 |
| Model Settings | Model | Shows / sets ADWF per connection (L/day), peaking factor (GSDF = ADWF x factor) and the default diurnal profile; lists the profiles |
| Join Dangling Sewer Ends | Model | Joins dangling downstream ends to the nearest sewer (splits it at a new tee); preview first |
| Import Network Data | Data | **All imports in one run**: sewers, then optional pressure data, then join dangling ends (default 5 m), then optional pump station register; model added to the map once at the end (see below) |
| Import Sewer Data | Data | Water Corporation gravity sewer layers -> new model with connection point loads (see below). IS stubs (IS sewers with a free end) are left out unless *Import IS (inspection shaft) sewers* (Options) is ticked; IS-coded pipes connected at both ends are kept; same option in Import Network Data; MAINTYPE O / OS emergency overflow pipes are kept as sewers and marked "overflow pipe" in their notes |
| Import Pressure Data | Data | Pump stations + pressure mains into an existing model (see below); an inlet whose only outgoing sewers are overflow pipes is still matched as the wet well |
| Set Pump Station Data | Data | Wet well, levels (optional wet well floor level), pumps and pump curve from the data sheet |
| Import Pump Station Data | Data | Wet well floor / levels / area, emergency storage and design pump rate from the pump station register workbook (see below); preview first |
| Calculate Existing Demand | Data | Existing dry weather flow from land use: DPLH-071 zones + DPLH-070 R-Codes -> DS 50 loading per node (sub-catchments `LU-<node>-<class>`) and the Land use loading review layer (see below); preview first |
| Import Diurnal Profile | Data | Profile from CSV / TXT or text pasted from ICM (Time / Factor); mean checked, not changed |
| Assign Profile | Data | Sets the profile of the node loads of the selected nodes, selected node loads / sub-catchments, or all |
| Export To ICM | Data | Nodes, gravity sewers, rising mains, pumps and foul sub-catchments (one per loaded node) with InfoWorks ICM field names (file gdb + CSV + mapping notes); connectivity report: which sewers have no downstream path to an outfall and where they stop (dead end / closed loop, nearest other sewer), fields `wwn_outfall` / `wwn_connectivity` on nodes and conduits, `<stem>_ICM_connectivity.csv`; option *Gravity sewers only* (wet wells as outfalls, no rising mains / pumps, overflow pipes left out); *Connectivity report only* (writes just the connectivity CSV and messages, in seconds) |
| Check Model | Analysis | Topology / data checks, plus missing pump station data; results in the Issues layer |
| Run Design | Analysis | Steady DS 50 design; Design results layer; selects failed conduits |
| Run Simulation | Analysis | SWMM dry weather run with a run label; roughness `ks` (Colebrook, as the design) by default or Manning n; series stored for the 5 conduits nearest the outlet and the conduits selected in the map; pump station tables / flags and a temporary pump station layer |
| Trace | Analysis | Upstream / downstream from a node or conduit id (blank = the one selected in the map); selects the path; reports upstream connections, ADWF and design GSDF |
| Show Simulation Run | Results | Shows a stored run (the latest 10 are kept) in the Simulation results layers |
| Compare Simulation Runs | Results | Run B minus run A per conduit: worse / better / newly surcharged / relieved, Simulation comparison layer |
| Plot Hydrograph | Results | Flow against time for stored series: peaks in the messages, optional CSV, chart on the series table (best effort) |
| Pipe Capacity | Calculators | Colebrook-White capacity of any DN; with a flow, d/D and (DN150-DN600) the DS 50 checks |
| Verify Setup | Setup | Where `wwnet_native` is loaded from and whether it has every function the toolbox needs |
| Self Test | Setup | Runs every tool on the bundled demo model in a scratch folder and writes one report file to paste back |
| Update WWNet | Setup | Applies a Python-only update zip (`wwnet-arcgis-update`) in place; never writes engine binaries; removes obsolete files |

## Import Network Data

One tool for the whole import. It takes the *Import Sewer Data* inputs and options, the optional *Import Pressure Data*
pump station / pressure main layers and options (blank = skipped; give both layers or neither), an optional pump station
register workbook + TOML (blank = skipped), *Join dangling sewer ends* (default on) with *Join distance (m)* (default 5.0)
and *Add model to map*. As in Import Sewer Data, an existing output needs *Overwrite the outputs of geoprocessing
operations* (Environments). Pressure options are prefixed `ps_` / `pf_` in the parameter names; the sewer and pressure
tools are unchanged and can still be re-run alone.

Steps, each preceded by a header line in the messages (`== 1/4 Sewers ==` ...):

1. **Sewers**: Import Sewer Data without adding layers to the map.
2. **Pressure** (if layers given): Import Pressure Data. It runs *before* the join: the inlet sewer's dead end at each pump
   station point becomes the wet well first.
3. **Join dangling ends** (if ticked): Join Dangling Sewer Ends, applied. It only considers dangling junction / is_end /
   io_end nodes, so wet wells are never joined, and it only joins to gravity sewers (rising mains are a separate table).
4. **Pump station register** (if given): Import Pump Station Data (needs the wet wells from step 2).

The model is then added to the map once (no sqlite write while Pro holds the layers) and a summary line gives nodes,
sewers, tees, joins made, pump stations, rising mains and stations with pump data, then "Next: Export To ICM".
If the sewer step fails, nothing else runs. A pressure failure (an `ERROR:` warning) skips the register step; a join failure
does not stop the register step. Model check errors from the sewer import are findings in the Issues layer, not a failure.

## Import Pressure Data

Adds the Water Corporation (WA) layers *Sewer Pump Station* (points) and *Sewer Pressure Main* (polylines, segmented) to an
**existing** WWNet GeoPackage (run Import Sewer Data first). Logic: `wwnet_arcgis/pressure_import.py`; field defaults in the
`[pressure]` section of `templates/import/watercorp_sewer.toml` (materials reuse `[codes.material]`).

**Parameters**: model `.gpkg`, pump stations, pressure mains, *Use selected features only* (on; a layer with nothing
selected is read by spatial filter on the model extent, never in full), search distance pump station -> main end / gravity
terminal node (30 m), snap tolerance between main segments (1 m), join distance main end -> gravity node (1 m: the end must meet the node),
*Include not-in-use* (off; only STATUS `A` is imported, `N` too when ticked, never `R` / others), *Add / refresh layers on map*,
then one **Field** parameter per role (ps id / fallback id / names / status; main id / status / diameter / internal diameter /
material / length / name).

**Rules**
- `ps_id` = `SAP_ID`, else the integer `ID`. A pump station is imported even when no main is found (warning).
- Segments are chained by an endpoint graph (ends within the snap tolerance are one point). A rising main starts at a
  segment end within the search distance of a pump station (dead ends preferred; an end belongs to its nearest station) and
  continues through degree-2 joins to a dead end or a junction (3 or more segments: the chain stops and warns). It is oriented
  from the pump station end; `rm_id` = `RM_<ps_id>` (`_2`, `_3` for more mains), `dn` = most common valid DIAMETER (warns if
  they differ), `internal_diameter_mm` = INDIAM1, `length_m` = sum of LENGTH (geometry length where LENGTH is 0 / unknown),
  `roughness_type` `ks` with NULL roughness (engine default).
- The mains' UPSTREAM / DOWNSTREAM levels are **not used** (unreliable): `profile_json` stays NULL.
- Discharge: the nearest existing node within the join distance (1 m) of the main's far end; otherwise nothing is guessed:
  a new `junction` node `D_<rm_id>` is left hanging at the end (notes "rising main discharge, not connected: connect
  manually", one warning per main naming the nearest node). Export To ICM makes it an Outfall with reason "rising main
  discharge, not connected (connect manually)" until it is connected.
- Wet well: the gravity sewer end AT the pump station point: the nearest gravity terminal node (no outgoing conduit)
  within the wet well distance (1 m) of the station point becomes `wet_well` (`wet_well_node_id`). Nothing further away
  is guessed: otherwise a `wet_well` node `WW_<ps_id>` is created at the station point, left unconnected (notes "no
  gravity sewer ends at the pump station point: connect the inlet sewer manually"; one warning naming the nearest
  dead-end gravity node and its distance). The search distance (30 m) only finds the rising main start.
- Sentinels (-99.9 levels, -9996 / -9998, zero diameters / lengths) are treated as NULL. Re-running replaces the
  rows and tool-created nodes of the same `ps_id`s; hydraulic values and `wwn_pumps` rows already entered are kept.

**Pump hydraulics are not in the GIS.** After the import fill in, per pump station, in the `wwn_pump_stations` attribute table
`wet_well_area_m2`, `start_level`, `stop_level` and `pump_rate_lps` (and optionally `overflow_level`, `emergency_storage_m3`),
or add `wwn_pumps` rows (`pump_id`, `ps_id`, `role` duty / assist / standby, `curve_json` `[[q_lps, head_m], ...]`). The tool
lists the stations still missing them. The layers `wwn_pump_stations`, `wwn_rising_mains` and the `wwn_pumps` table are added
to the map (Add Model To Map also shows `wwn_pumps`).

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

**Data rules (bulk-import fixes)**: levels <= -99 (`-99.9`, `-9996`, `-9998`) in `UPSTREAM` / `DOWNSTREAM` / `TOPLEV` are
missing, never levels. Missing inverts are estimated once connectivity and direction are known (other end -/+ length /
`FLOW_GRADE`, else from the adjoining sewers' inverts; real values are never changed) and each one is flagged
`INVERT_ESTIMATED`. Only `STATUS` A sewers are imported (N / D skipped, counted in the summary). Chamber types E and P are
skipped (sewers connect by geometry); a `PACID` or manhole reference that is blank or all zeros means no id. A branch end is
not teed into a main whose end node its other end already sits at (`SUSPECT_TEE`, as is a directed cycle through a tee).
`TEE_JOINED` / `END_SNAPPED` are info rows (Issues layer, not warnings); `GRADE_MISMATCH` is listed only when the fall differs
by more than 0.05 m, the rest are counted in one line. Codes: `templates/import/watercorp_sewer.toml`.

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
  main's invert). Points within the snap tolerance of the main's own ends are endpoint cases, not tees. Any other The tolerance is the *Tee join distance (m)* parameter (Advanced, last). Lone ends within 5 m of another sewer's mid-line but beyond it are listed in the report as `possible missed tee` (fix with Join Dangling Sewer Ends).
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

## Join Dangling Sewer Ends

The importer joins a lone sewer end to another sewer's mid-line only within its *Tee join distance* (0.5 m). A sewer end a
little further away stays a dangling node (`junction` / `is_end` / `io_end` that receives a sewer and has no outgoing one),
which InfoWorks ICM and the SWMM engine treat wrongly. Import Sewer Data now lists these near misses in its report
(`possible missed tee: E4 1.3 m from P123 ...`) and takes an *Advanced* parameter **Tee join distance (m)** (last
parameter; default 0.5). This tool repairs an existing model without re-importing. Logic: `wwnet_shared/core/repair.py`
(sqlite3 only, shared with QGIS); the tool body is `tools_impl.join_dangling_ends`.

**Parameters**: model `.gpkg`, *Maximum join distance (m)* (5), *Preview only* (on: first run prints the table, nothing is
changed). Close attribute tables and remove the model's layers from the map before applying (a locked file gives a clear
message); afterwards re-add the layers and re-run Check Model / Export To ICM.

**Rules**
- Dangling node: >= 1 incoming non-abandoned sewer, none outgoing, `node_type` junction / is_end / io_end. Real terminal
  chambers (ac, ms, drop_ac, outfall, wet_well) are never touched.
- Main: nearest other sewer (not touching the node) by perpendicular distance to its polyline, within the maximum
  distance. Beyond it the node is listed as not joined with the nearest sewer and its distance.
- Projection within 0.5 m of an end node of the main: the branch is re-pointed to that node (no split).
- Otherwise the main is split at the projected point (chainage measured from its upstream node, whichever way it is
  digitised). Tee node `T_<node>` (junction, status / data source of the main): invert = us invert - fall x chainage / length,
  surface level interpolated when both ends have one. The upstream piece keeps the sewer id, the new downstream piece is
  `<id>~t` (geometry cut, lengths split in proportion, all other attributes copied, notes `split at tee for <branch>`).
- Branch sewers are re-pointed to the tee and their end vertex moved onto it (`length_m` grows by the gap when set). The
  branch invert is kept; one arriving below the main invert is warned (`check levels`). The dangling node is deleted once
  nothing refers to it (loads / pump station links are moved to the tee).
- One transaction; rolled back on any error. A GeoPackage with R-tree spatial index tables is refused.

## Install

### The no-install bundle (no pip, no environment clone, no `.exe`)
1. Close ArcGIS Pro. Download the CI artifact **`wwnet-arcgis-bundle-win-py311`** from the latest green run.
2. **Delete the old `WWNet-ArcGIS` folder** (so no file of an earlier version is left behind), then extract the new
   one to a local, non-OneDrive folder.
3. In ArcGIS Pro: Catalog pane > Toolboxes > Add Toolbox > `WWNet.pyt` (or right-click the toolbox > Refresh).
4. Run **Verify Setup**, then **Self Test** (tick the map steps) and send the report.

What is in it: the toolbox Python files, `wwnet_shared/` (the shared engine-facing Python), templates, rule sets,
the demo model, and `vendor/wwnet_native/` with the one compiled file, `wwnet_native.pyd` (Windows version
information: product WWNet, description "WWNet wastewater network engine (Python extension module)"). Nothing is
installed into Pro's Python; the toolbox puts `vendor/` first on `sys.path` when a tool runs.

### Quick updates (Python only)
CI artifact **`wwnet-arcgis-update`** + the **Update WWNet** tool: no extract, no Unblock, no restart (modules are
reloaded on every tool run). It writes only Python and data files, never binaries: a change to the engine always
needs the full bundle above. If `WWNet.pyt` changed: right-click the toolbox > Refresh.

### Development (no packaging)
Keep the toolbox inside the repository: `wwnet_arcgis/shared.py` then imports `../qgis_plugin/wwnet/core` as
`wwnet_shared`. The packaged zip carries a copy of `core/*` only (a test enforces that nothing shipped imports
`qgis` / Qt or contains assistant / credential code).

## Antivirus / corporate IT
- The only compiled file is `vendor/wwnet_native/wwnet_native.pyd` (unsigned, rebuilt by CI for every engine
  change, so each build is new to reputation-based scanners). If a scanner flags it, report it to IT security as a
  false positive with the file's SHA-256 so it can be allowlisted; code signing would be the permanent fix.
- The toolbox stores no credentials, makes no network connections, runs no `.exe` or subprocess, and never
  renames or replaces binaries at start-up.

## Pump station register

**Import Pump Station Data** fills the pump data the GIS does not hold from the Water Corporation pump station register
workbook (`.xlsx` / `.xlsm`), for every model pump station whose `ps_id` equals a SAP functional location (or a
sub-location such as `S0037310-PMP01`). Logic: `wwnet_arcgis/pump_register.py` (workbook reader `xlsx.py`, stdlib only).

**Columns used** (defaults, all in `templates/import/watercorp_pump_register.toml`; leave the *Import template*
parameter blank to use the bundled copy, or point it at an edited one): functional location in column C of every sheet.
*Level Settings*: ground level E, overflow F, high level alarm L, start P, stop Q, base (wet well floor) T (depths below
top of slab, m). *Civil*: wet well storage R (m3 between the alarm and the overflow level), emergency storage AB (m3).
*Motor and Pump Attributes*: design flow AF, design head AG. Units (`m` / `mm`, `L/s` / `m3/h` / `ML/d`), sheet names,
duty / standby counts and the sub-location separators are in the TOML too; a bad value is reported with its TOML key.

**What it writes** (through the same code as Set Pump Station Data): the wet well floor and ground level (the wet well
node's invert and surface level), stop / start / overflow levels (AHD = ground - depth), wet well area (storage divided by
alarm-to-overflow depth), emergency storage, and one duty pump at the design flow with the standby count. An existing pump
curve is kept (only the duty rate is updated). Messages are prose lines; errors start with `ERROR:` and never fail the tool.

- **Preview only (write nothing)** validates everything and reports; untick it to import.
- **Re-run after Import Sewer Data (overwrite)**: that rebuilds the model and loses the pump data and the wet well
  node's invert. Import Pressure Data re-creates its `WW_<ps_id>` wet well nodes, so re-import the register after it too.
- Several pump rows per station: the first row sets the duty rate; further rows only add standby pumps and rows with
  different test data are flagged as not used (future enhancement: per-pump rates / curves).
- Then run **Export To ICM**: the pumps go to `ICM_Pumps` (fixed-rate pump at the design flow).
- A small example workbook laid out like the real register ships as `templates/demo/pump_register_demo.xlsx`
  (`examples/demo/` in the repo; made by `tests/make_pump_register_demo.py`); Self Test imports it.

**Set Pump Station Data** has an optional *Wet well floor level (m AHD)*. The wet well node's invert is the incoming
sewer invert, so real stop levels below it need the floor: when given, levels are validated against it and the node's
invert is set to it. Blank leaves the node as it is.

## Calculate Existing Demand

Existing dry weather flow from land use and area, loaded per node (never per connection: ICM and SWMM only take inflow
at nodes, so per-node loading gives the same pipe flows with no extra model objects).

1. Each **Local Planning Scheme zone polygon (DPLH-071)** is classified from its *Zone* text by the rules in
   `templates/import/dplh_land_use.toml` (first rule whose words occur in the zone wins): residential,
   residential_or_commercial (centres, mixed use: residential where an R-code exists, else commercial), commercial,
   hotel_motel, perth_cba, industrial, or **zero** (roads, rail, open space, conservation, waterways, rural / farm,
   rural residential, utilities, unzoned, blank zone). A zone matching no rule is flagged `ZONE_UNMATCHED` and
   loaded as residential **R15**; the messages list those zone names with their area.
2. Residential polygons take the code of the **R-Codes polygon (DPLH-070)** they fall in, point by point (a block
   across two codes is split): split codes (R20/40) use the **lower** code (as built, default) or the **higher** code
   (as permitted; tool parameter); no code -> the zone rule's default code if it has one (bundled:
   'Residential development' -> **R30**, structure plan areas whose codes are not in DPLH-070), else R15; below
   R15 -> R15 (DS 50 4.2.1(c));
   codes between table rows -> the next higher row; R-AC and other text codes -> R15 unless mapped in
   `[rcode_aliases]`.
3. DS 50 Tables 4.1 / 4.2 (persons per net ha x L/person/day) and 4.3 (L/net ha/day), dry or wet ground, south or
   north of 26 deg S (from the model location unless chosen). The polygon area is treated as net area.
4. The polygon is sampled (~50 points, 2-20 m apart); each point goes to the **nearest model sewer within the
   maximum distance (150 m)** and loads that sewer's upstream node (the connection point rule). Points further away
   are not loaded. A polygon with no model sewer within twice that distance is `OUTSIDE_MODEL` (normal at the edges
   of the rectangular area read; counted, no warning); one 150-300 m away is `NOT_LOADED` and listed as a warning
   with its distance (unsewered, or sewers missing from the model?); `PARTLY_LOADED` when > 5 % is lost.
5. Per node and class one `wwn_subcatchments` row `LU-<node>-<class>` (e.g. `LU-N123-R20`, `LU-N123-IND`), no
   geometry, status existing. Re-running replaces the `LU-*` rows. **Run Design** (GSDF) and **Run Simulation**
   (daily flow / 86400 with the profile) load them; other sub-catchments are kept (warned: double counting).
6. **Connection point loads** from Import Sewer Data (connections x 630 L/d) are replaced by default (the messages
   compare both totals); untick *Replace the connection point loads* to keep both. Manual node loads stay.

The messages list the area per class and, per class, the zone names with their area loaded (check what makes up
"commercial"), the R-code notes on the loaded area (outside every R-Codes polygon / blank code / below R15 / dual /
unrecognised) and the values found in the R-Codes layer's field.

The **Land use loading** layer (`wwn_land_use`, rewritten each run) shows every zone polygon read with its class,
LPS and DS 50 codes, area, area loaded, ADWF, the nodes it loads and its flags; issues go to the Issues layer
(source `demand`). Zones are read within the model extent + the maximum distance (or the selected polygons).

**Export To ICM** writes the loads as `ICM_Subcatchments` (+ `<model>_ICM_subcatchments.csv`): one foul
sub-catchment per loaded node (id = node id, point at the node; ICM draws a circle of `total_area` for a
sub-catchment without a boundary), `contributing_area` 0, connectivity 100. Residential -> `population` (DS 50
persons/ha x area) on wastewater profile 1; commercial and industrial -> `trade_flow` (L/s) on trade profile 1 (one
shared trade profile); manual ADWF and node loads -> `additional_foul_flow` (L/s). In ICM, create a Waste Water event
(profile 1: per capita flow 180 l/hd/d dry south, diurnal multipliers) and a Trade Waste event (profile 1: flow
scaling factor 1.0, diurnal multipliers). The total equals the ADWF Run Simulation uses (`wwn_adwf_lps` per row).
Import it after the nodes (ODIC object type Subcatchment; flows in L/s).

## Editing GeoPackages in Pro
- Pro reads GeoPackage tables as `file.gpkg\main.<table>`; `fid` is the ObjectID.
- The engine opens the file itself: save edits in Pro before Check / Design / Simulate (each tool reminds you).
- Tools that write with sqlite (Import Pressure Data, Set Pump Station Data, Import Pump Station Data, Calculate Existing Demand, Join Dangling Sewer Ends) fail while
  Pro holds a lock: close attribute tables / remove the model's layers first.
- Result and issue layers are rewritten by the tools: treat them as read-only.

## Development

    pip install pytest pyflakes
    cd crates/wwnet-py && maturin build --release && pip install --force-reinstall ../../target/wheels/wwnet_native-*.whl
    cargo build -p wwnet-cli          # only for the binding / CLI parity test (or set WWNET_EXE)
    pytest arcgis_toolbox/tests -q    # no ArcGIS needed: tests use a fake `arcpy` over sqlite3
    python arcgis_toolbox/package.py  # dist/wwnet-arcgis-<ver>.zip (add --vendor-dir <folder> for the bundle)

Not verifiable outside ArcGIS Pro: real `arcpy` behaviour for layer / group creation, symbology, selections,
charts, parameter UI.
