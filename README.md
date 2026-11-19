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
| `rulesets/` | `ds50_v2r4.toml` (transcribed), `ds51_TODO.toml` |
| `templates/schema.sql` | GeoPackage schema v1 (EPSG:7850) |
| `docs/` | `PLAN.md`, `reference-survey.md` |

```
cargo test --workspace
cargo run -p wwnet-cli -- design examples/demo/demo.gpkg
```

To build the plugin and wheel, see `qgis_plugin/README.md`.
