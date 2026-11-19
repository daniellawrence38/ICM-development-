# WWNet ArcGIS Pro toolbox

Python toolbox (`WWNet.pyt`) for **ArcGIS Pro 3.3+** that fronts the WWNet wastewater design tool
(Water Corporation WA, DS 50 gravity sewers). The design engine stays in the `wwnet` command-line tool;
the toolbox creates and maps models, runs checks / design / traces, and hosts the optional
Claude-powered assistant. Models are the same EPSG:7850 GeoPackages used by the QGIS plugin.

## Tools

| Tool | What it does |
|---|---|
| New Model | Creates a GeoPackage from `schema.sql` (sqlite3) and adds the layers to the active map in a `WWNet` group layer |
| Add Model To Map | Adds the `wwn_*` layers (`<file>.gpkg\main.<table>`) of an existing model and applies symbology: nodes by `node_type`, conduits by DN with an Arcade label `DN225 1:300`. Symbology failures only warn |
| Check Model | `wwnet_native.check` (same output as `wwnet check --json`); messages via AddMessage/AddWarning/AddError; optional output issues table |
| Run Design | `wwnet_native.design` (same output as `wwnet design --json`); prints the pass/warn/fail summary, a per-conduit table (DN, grade, GSDF / pumped / total / capacity L/s, d/D, velocity, result, worst message) and failing checks with DS 50 clauses. By default adds a temporary in-memory layer `Design results — <model>` (polylines coloured pass/warn/fail, labelled with flow/capacity and d/D) to the active map and selects failed conduits on it and on the model's conduit layer. The layer lives in `memory\` and is not saved: re-run Run Design after restarting Pro. "Add results table to map" (off by default) also adds the standalone `wwn_results_conduits` table |
| Trace | Upstream / downstream from a node (value list filled from the Nodes layer); selects traced nodes and conduits |
| Pipe Capacity | Colebrook-White calculator (DN, grade, ks, flow) + DS 50 checks; uses `wwnet_native` if importable, else the shared Python fallback |
| Set Assistant API Key | Stores the Anthropic key (`GPEncryptedString`) with Windows DPAPI; default model / effort; optional fallback CLI path |
| Verify Setup | Prints where `wwnet_native` / `anthropic` / dependencies are loaded from and whether they import; optional live API test (tiny request, key never shown) |
| Ask WWNet Assistant | DS 50 assistant with tools `list_layers`, `get_selected_features`, `query_features`, `get_feature`, `pipe_capacity`, `check_conduit`, `residential_gsdf`, `lookup_ds50`, `propose_edits` |

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
