# WWNet

Wastewater network modelling for gravity sewers, pump stations and rising mains. The Rust core implements Water Corporation DS 50 design rules, and a QGIS plugin provides the front end, including a Claude-powered design assistant.

| Path | What |
|---|---|
| `crates/wwnet-core` | Entities, GeoPackage I/O, topology (petgraph), validation |
| `crates/wwnet-hydraulics` | Colebrook-White / Manning part-full circular pipe hydraulics |
| `crates/wwnet-rules` | DS 50 rule set loader and clause-referenced conduit checks |
| `crates/wwnet-design` | GSDF loading (Tables 4.1–4.3), accumulation, design run |
| `crates/wwnet-cli` | `wwnet new / check / trace / design` |
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

## Loading (schema v2)

- `wwn_node_loads`: dry weather loads per node (`adwf_lps`, or `connections` x `adwf_per_connection_lpd` / 86400).
- `wwn_subcatchments.adwf_lps` / `profile_id`: manual ADWF per catchment; when set it replaces the land-use / R-code loading.
- `wwn_meta`: `adwf_per_connection_lpd` (630), `peaking_factor` (1.5), `default_profile`.
- `wwnet design`: GSDF contribution of any ADWF = ADWF x `peaking_factor`; conduits report accumulated `adwf_lps` (CLI column ADWF).
- `wwnet profile import <gpkg> <id> <csv> [--normalise] [--no-default]` (Python: `wwnet_native.import_profile`) imports a diurnal profile
  (time as hh:mm or minutes, factor; header optional). The mean is checked (warning when |mean - 1| > 0.02), never silently changed.
