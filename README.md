# WWNet

Wastewater network modelling for gravity sewers, pump stations and rising mains. The Rust core implements Water Corporation DS 50 design rules, and a QGIS plugin provides the front end, including a Claude-powered design assistant.

| Path | What |
|---|---|
| `crates/wwnet-core` | Entities, GeoPackage I/O, topology (petgraph), validation |
| `crates/wwnet-hydraulics` | Colebrook-White / Manning part-full circular pipe hydraulics |
| `crates/wwnet-rules` | DS 50 rule set loader and clause-referenced conduit checks |
| `crates/wwnet-design` | GSDF loading (Tables 4.1–4.3), accumulation, design run |
| `crates/wwnet-sim` | SWMM `.inp` writer, in-process runner and the file-level simulation service (`wwnet_sim::service`) |
| `crates/wwnet-swmm-sys` | Vendored EPA SWMM 5.2.4 C engine (built with `cc`; also linked into the `wwnet_native` wheel) |
| `crates/wwnet-cli` | `wwnet new / check / trace / design / simulate` |
| `crates/wwnet-py` | PyO3 `wwnet_native` abi3 wheel used by the plugin |
| `qgis_plugin/` | QGIS plugin: model create/open, styling, check dock, AI assistant |
| `arcgis_toolbox/` | ArcGIS Pro Python toolbox (`WWNet.pyt`): same tools and assistant, shares the plugin core |
| `templates/profiles/` | `example_residential.csv`: APPROXIMATE example diurnal profile (digitised from a screenshot), see its README |
| `rulesets/` | `ds50_v2r4.toml` (transcribed), `ds51_TODO.toml` |
| `templates/schema.sql` | GeoPackage schema v2 (EPSG:7850; v1 files still load, `gpkg::upgrade` adds the v2 tables) |
| `docs/` | `PLAN.md`, `reference-survey.md` |

```
cargo test --workspace
cargo run -p wwnet-cli -- design examples/demo/demo.gpkg
```

To build the plugin and wheel, see `qgis_plugin/README.md`.

## Dynamic simulation (SWMM)

`wwnet simulate <gpkg> [--duration-h 48] [--warmup-h 24] [--roughness manning|ks] [--n 0.013] [--flow-scale 1.0] [--json] [--write-inp out.inp]`
(Python: `wwnet_native.simulate(gpkg, duration_h=48.0, warmup_h=24.0, roughness="manning", n=0.013, flow_scale=1.0, write=True, series_ids=None, write_inp=None)`)
runs the embedded SWMM dynamic-wave engine. Inflows: node loads and sub-catchments (manual `adwf_lps`, else the DS 50
daily flow / 86400), each with its `profile_id` (node load / sub-catchment, else `wwn_meta.default_profile`; none gives a constant
flow and the warning "no diurnal profile: constant ADWF"). `--flow-scale` multiplies every inflow. Statistics exclude the warm-up.
Results go to `wwn_sim_runs` (options, continuity error, warnings), `wwn_sim_links`, `wwn_sim_nodes` and `wwn_sim_series`
(flow / depth of the 5 downstream-most conduits plus `series_ids`, minutes after the warm-up); the latest 10 runs are kept.
Pump stations and rising mains are not modelled yet. The CLI prints the top 50 conduits by max d/D and a summary; exit code 0
unless the engine errors.

## Loading (schema v2)

- `wwn_node_loads`: dry weather loads per node (`adwf_lps`, or `connections` x `adwf_per_connection_lpd` / 86400).
- `wwn_subcatchments.adwf_lps` / `profile_id`: manual ADWF per catchment; when set it replaces the land-use / R-code loading.
- `wwn_meta`: `adwf_per_connection_lpd` (630), `peaking_factor` (1.5), `default_profile`.
- `wwnet design`: GSDF contribution of any ADWF = ADWF x `peaking_factor`; conduits report accumulated `adwf_lps` (CLI column ADWF).
- `wwnet profile import <gpkg> <id> <csv> [--normalise] [--no-default]` (Python: `wwnet_native.import_profile`) imports a diurnal profile
  (time as hh:mm or minutes, factor; header optional). The mean is checked (warning when |mean - 1| > 0.02), never silently changed.
