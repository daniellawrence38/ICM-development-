# WWNet QGIS plugin (Phase 0/1)

Thin PyQGIS front end for the WWNet wastewater design tool (Water Corporation WA, DS 50 gravity
sewers). Targets QGIS 3.34 and 3.40 LTR.

## Features
- **New model / Open model**: creates a GeoPackage from `schema.sql` (sqlite3) or loads an existing
  one; all `wwn_*` layers go into a `WWNet` layer group with programmatic styling (nodes by
  `node_type`, conduits by DN labelled e.g. `DN225 1:300`), value-map widgets for the schema enums
  and default `status` / `data_source`.
- **Check model**: runs the check through the `wwnet_native` module (no `.exe` needed), lists issues in a
  dock; click a row to select and zoom to the feature. The `wwnet` CLI is used only as a fallback if the
  module is missing; with neither, the dock shows stored `wwn_issues` and the wheel install hint.
- **AI assistant** (optional): Claude-powered chat dock that knows the DS 50 ruleset and the schema.

## Install
Build the zip (copies `templates/schema.sql` and `rulesets/*.toml` into the package):

    cd qgis_plugin && make package        # or: python package.py     -> dist/wwnet-0.1.0.zip

- **Zip**: QGIS > Plugins > Manage and Install Plugins > Install from ZIP.
- **Symlink (development, Windows)**, from an elevated or developer-mode prompt:

      mklink /D "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\wwnet" "C:\path\to\repo\qgis_plugin\wwnet"

  (Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`.) In a symlinked dev tree the
  plugin falls back to `templates/` and `rulesets/` in the repository, so packaging is not needed.
  Enable "WWNet" in the plugin manager; the `Plugins > WWNet` menu and the WWNet toolbar appear.

The **required** Rust extension is the `wwnet_native` wheel (the `wwnet.exe` CLI is not needed). Install it into
QGIS's Python, e.g. from the OSGeo4W Shell: `pip install wwnet_native-<ver>-cp39-abi3-win_amd64.whl`
(download it from the CI artifact `wwnet_native-windows-abi3`, or build with `maturin build --release` in
`crates/wwnet-py`). It should be importable as `wwnet_native`. A top-level module called
`wwnet` clashes with this plugin's folder name, so the plugin probes `wwnet_native`,
`wwnet._native`, `wwnet.wwnet`, then `wwnet`, and only accepts a module exposing
`part_full_flow_lps` and `depth_ratio_for_flow`. Without it, hydraulics use pure-Python fallbacks but model check/design/trace are unavailable.

## Enabling the AI assistant
1. Toolbar > **AI assistant**, then **Settings...** (or Plugins > WWNet > AI settings).
2. If the `anthropic` package is missing, click **Install ...**. It runs
   `python -m pip install --user anthropic` using QGIS's own interpreter (on Windows the
   interpreter is found under `sys.exec_prefix`, because `sys.executable` is `qgis.exe`).
3. Paste your Anthropic API key. It is stored encrypted in the QGIS authentication database
   (you will be asked for / to set the QGIS master password). If no master password is available the key
   falls back to unencrypted QgsSettings and you are warned. `ANTHROPIC_API_KEY` is also honoured.
4. Choose a model (`claude-opus-5-5` default, `claude-sonnet-5-5`, `claude-haiku-5-5`) and effort
   (low / medium / high), then **Test connection**.

The assistant can read the model (`list_layers`, `get_selected_features`, `query_features`,
`get_feature`), run hydraulics / DS 50 checks (`pipe_capacity`, `check_conduit`, `residential_gsdf`,
`lookup_ds50`) and **propose** edits. `propose_edits` never writes by itself: a dialog lists each
change and nothing is applied unless you click *Apply edits*.

## Data privacy
When the assistant uses its tools, **attribute values of the features it reads** (IDs, levels, DN,
grades, notes, ...) and your chat messages are sent to Anthropic's API, together with the DS 50
ruleset and schema summary as context. Geometry is not sent. Do not use the assistant on models
containing data you are not permitted to disclose. API keys are never logged.

## Development
    make test       # pytest, no QGIS needed (engine, Colebrook fallback, schema, parsers)
    make compile    # py_compile everything

`wwnet/core/*`, `assistant/engine.py`, `client.py`, `prompt.py`, `tools.py`, `install.py` contain no
qgis imports and are unit tested. `dock.py`, `qt_bridge.py`, `qgis_tools.py`, `secrets.py`,
`settings_dialog.py`, `layers.py`, `check_dock.py`, `plugin.py` need QGIS.
