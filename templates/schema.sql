-- WWNet GeoPackage schema v3 (v2 adds node loads, diurnal profiles and loading settings; v3 makes issues and results
-- map layers: wwn_issues gets source + point geometry, wwn_results_conduits and the wwn_sim_*_results / wwn_sim_compare
-- tables are feature tables, wwn_sim_runs gets label + summary_json). Older files are upgraded by `gpkg::upgrade`.
-- Single source of truth for wwnet-core (Rust) and both front ends.
-- CRS: EPSG:7850 (GDA2020 / MGA zone 50). Levels in m AHD, diameters in mm, flows in L/s.
-- Enumerations (TEXT, lower_snake):
--   node_type:   ac | ms | drop_ac | junction | wet_well | outfall | io_end | is_end
--   status:      existing | proposed | abandoned
--   data_source: as_con | gis | survey | design | assumed
--   material:    pvc | vc | rcpl | di | pe | grp | ac_pipe | other
--   location:    road_reserve | trafficable_row | lot | pos | paw | other
--   sewer_type:  reticulation | io | is | main
--   land_use:    residential | commercial | hotel_motel | perth_cba | industrial
--   ground:      dry | wet          region: south | north (of 26 deg S)

PRAGMA application_id = 1196444487;  -- 'GPKG'
PRAGMA user_version = 10300;

CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
  srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
  organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
CREATE TABLE IF NOT EXISTS gpkg_contents (
  table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
  description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
  CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
  table_name TEXT NOT NULL, column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL,
  srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
  CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
  CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
  CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));

INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES
 ('Undefined cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined cartesian coordinate reference system'),
 ('Undefined geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined geographic coordinate reference system'),
 ('WGS 84 geodetic', 4326, 'EPSG', 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', 'longitude/latitude WGS84'),
 ('GDA2020 / MGA zone 50', 7850, 'EPSG', 7850, 'PROJCS["GDA2020 / MGA zone 50",GEOGCS["GDA2020",DATUM["Geocentric_Datum_of_Australia_2020",SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",117],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],PARAMETER["false_northing",10000000],UNIT["metre",1]]', 'GDA2020 / MGA zone 50');

CREATE TABLE IF NOT EXISTS wwn_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO wwn_meta VALUES ('schema_version','3'), ('ruleset','ds50_v2r4'), ('crs','EPSG:7850'),
 ('adwf_per_connection_lpd','630'), ('peaking_factor','1.5');  -- default_profile: absent = none

CREATE TABLE IF NOT EXISTS wwn_nodes (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT,
  node_id TEXT NOT NULL UNIQUE, node_type TEXT NOT NULL DEFAULT 'ac',
  surface_level REAL, invert_level REAL, chamber_diameter_mm INTEGER, flood_level REAL,
  status TEXT NOT NULL DEFAULT 'proposed', data_source TEXT NOT NULL DEFAULT 'design',
  asset_id TEXT, install_year INTEGER, notes TEXT);

CREATE TABLE IF NOT EXISTS wwn_conduits (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
  conduit_id TEXT NOT NULL UNIQUE, us_node_id TEXT NOT NULL, ds_node_id TEXT NOT NULL,
  dn INTEGER NOT NULL, internal_diameter_mm REAL,          -- NULL => use DN (DS 50 basis)
  material TEXT DEFAULT 'pvc', pipe_class TEXT,
  us_invert REAL, ds_invert REAL, length_m REAL,           -- NULL length => geometry length
  ks_mm REAL,                                              -- NULL => ruleset value (1.0)
  location TEXT DEFAULT 'road_reserve', sewer_type TEXT DEFAULT 'reticulation',
  status TEXT NOT NULL DEFAULT 'proposed', data_source TEXT NOT NULL DEFAULT 'design',
  install_year INTEGER, condition_grade INTEGER, asset_id TEXT, notes TEXT);

CREATE TABLE IF NOT EXISTS wwn_pump_stations (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT,
  ps_id TEXT NOT NULL UNIQUE, wet_well_node_id TEXT, discharge_node_id TEXT, rising_main_id TEXT,
  pump_rate_lps REAL, duty_config TEXT, wet_well_area_m2 REAL,
  start_level REAL, stop_level REAL, overflow_level REAL, emergency_storage_m3 REAL,
  status TEXT NOT NULL DEFAULT 'proposed', data_source TEXT NOT NULL DEFAULT 'design', notes TEXT);

CREATE TABLE IF NOT EXISTS wwn_pumps (
  fid INTEGER PRIMARY KEY AUTOINCREMENT,
  pump_id TEXT NOT NULL UNIQUE, ps_id TEXT NOT NULL,
  role TEXT DEFAULT 'duty',                 -- duty | assist | standby
  curve_json TEXT,                          -- [[q_lps, head_m], ...]
  speed_pct REAL DEFAULT 100, notes TEXT);

CREATE TABLE IF NOT EXISTS wwn_rising_mains (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
  rm_id TEXT NOT NULL UNIQUE, ps_id TEXT, discharge_node_id TEXT,
  dn INTEGER, internal_diameter_mm REAL, material TEXT,
  roughness_type TEXT DEFAULT 'ks', roughness REAL,        -- ks (mm) or hw_c
  length_m REAL, profile_json TEXT,                        -- [[chainage_m, level_m], ...]
  status TEXT NOT NULL DEFAULT 'proposed', data_source TEXT NOT NULL DEFAULT 'design', notes TEXT);

CREATE TABLE IF NOT EXISTS wwn_subcatchments (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom MULTIPOLYGON,
  subcatchment_id TEXT NOT NULL UNIQUE, outlet_node_id TEXT NOT NULL,
  net_area_ha REAL, gross_area_ha REAL, broadacre INTEGER DEFAULT 0,
  land_use TEXT DEFAULT 'residential', rcode TEXT DEFAULT 'R20',
  ground TEXT DEFAULT 'dry', region TEXT DEFAULT 'south',
  extra_flow_lps REAL DEFAULT 0,             -- approved trade waste / special flows
  adwf_lps REAL,                             -- v2: manual ADWF (L/s); replaces land-use/R-code loading when set
  profile_id TEXT,                           -- v2: diurnal profile (NULL = default profile)
  status TEXT NOT NULL DEFAULT 'proposed', notes TEXT);

-- Written by `wwnet design`; read-only for users. A feature table (conduit geometry copied in) so the latest design
-- shows as a persistent, styled layer. Rewritten by every design run.
CREATE TABLE IF NOT EXISTS wwn_results_conduits (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
  conduit_id TEXT NOT NULL UNIQUE, run_at TEXT, ruleset TEXT, dn INTEGER, grade_1in REAL,
  adwf_lps REAL, gsdf_lps REAL, pumped_lps REAL, total_lps REAL,
  capacity_lps REAL,                        -- governing: lesser of table44_lps and hydraulic_capacity_lps
  table44_lps REAL, hydraulic_capacity_lps REAL, d_over_d REAL, velocity_mps REAL,
  result TEXT,                              -- pass | warn | fail | info
  worst_message TEXT,
  messages TEXT);                           -- JSON {checks: [{rule, clause, value, limit, result, message}]}
-- Issues of the latest check / design / import / simulate (column source); each run replaces its own rows. Point =
-- the node, the conduit midpoint, the pump station or the sub-catchment centre (NULL for model-level issues).
CREATE TABLE IF NOT EXISTS wwn_issues (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, severity TEXT, code TEXT,
  object_type TEXT, object_id TEXT, clause TEXT, message TEXT,
  source TEXT,                              -- check | design | import | simulate
  geom POINT);

-- Schema v2: dry weather loads and diurnal profiles.
CREATE TABLE IF NOT EXISTS wwn_node_loads (
  load_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
  connections INTEGER, adwf_lps REAL,        -- ADWF = adwf_lps, else connections x adwf_per_connection_lpd / 86400
  profile_id TEXT, source TEXT,              -- source: connection_points | manual | ...
  notes TEXT);
CREATE TABLE IF NOT EXISTS wwn_profiles (
  profile_id TEXT NOT NULL, minute_of_day INTEGER NOT NULL, factor REAL NOT NULL,
  PRIMARY KEY (profile_id, minute_of_day));
CREATE TABLE IF NOT EXISTS wwn_profile_info (
  profile_id TEXT PRIMARY KEY, description TEXT, interval_min INTEGER, mean_factor REAL);

-- Written by `wwnet simulate` (SWMM dynamic engine); read-only for users. Statistics exclude the warm-up.
CREATE TABLE IF NOT EXISTS wwn_sim_runs (
  run_id TEXT PRIMARY KEY,                  -- UTC timestamp of the run
  options_json TEXT, continuity_error_pct REAL, warnings_json TEXT,
  label TEXT,                               -- v3: user label, e.g. 'existing x1.0'
  summary_json TEXT);                       -- v3: run summary (inflow, peak outlet flow, surcharged / flooded counts)
CREATE TABLE IF NOT EXISTS wwn_sim_links (
  run_id TEXT NOT NULL, conduit_id TEXT NOT NULL,
  max_flow_lps REAL, max_depth_m REAL, max_d_over_d REAL, max_velocity_mps REAL,
  time_of_max_flow TEXT,                    -- time of day (HH:MM) of the peak
  surcharged_min REAL,
  PRIMARY KEY (run_id, conduit_id));
CREATE TABLE IF NOT EXISTS wwn_sim_nodes (
  run_id TEXT NOT NULL, node_id TEXT NOT NULL,
  max_hgl_m REAL, max_depth_m REAL, flood_volume_m3 REAL, surcharged_min REAL,
  PRIMARY KEY (run_id, node_id));
CREATE TABLE IF NOT EXISTS wwn_sim_series (
  run_id TEXT NOT NULL, object_type TEXT NOT NULL,   -- conduit | node (node flow = flooding)
  object_id TEXT NOT NULL, minute REAL NOT NULL,     -- minutes after the end of the warm-up
  flow_lps REAL, depth_m REAL,
  PRIMARY KEY (run_id, object_type, object_id, minute));
CREATE TABLE IF NOT EXISTS wwn_sim_pumps (
  run_id TEXT NOT NULL, pump_id TEXT NOT NULL, ps_id TEXT, role TEXT,
  max_flow_lps REAL, starts INTEGER, starts_per_hour REAL, run_time_pct REAL,
  PRIMARY KEY (run_id, pump_id));
CREATE TABLE IF NOT EXISTS wwn_sim_pump_stations (
  run_id TEXT NOT NULL, ps_id TEXT NOT NULL, wet_well_node_id TEXT,
  max_level_m REAL, max_depth_m REAL, overflow_volume_m3 REAL,
  time_of_max_level TEXT,                 -- time of day (HH:MM)
  PRIMARY KEY (run_id, ps_id));
CREATE TABLE IF NOT EXISTS wwn_sim_rising_mains (
  run_id TEXT NOT NULL, rm_id TEXT NOT NULL, ps_id TEXT, length_m REAL,
  diameter_mm REAL, max_flow_lps REAL, max_velocity_mps REAL,
  PRIMARY KEY (run_id, rm_id));

-- v3: map layers of one stored simulation run (the latest, or the one chosen with Show Simulation Run), and of a
-- run comparison. Rewritten by `wwnet simulate` / show_sim_run / compare_sim_runs.
CREATE TABLE IF NOT EXISTS wwn_sim_conduit_results (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
  run_id TEXT, label TEXT, conduit_id TEXT NOT NULL UNIQUE, dn INTEGER,
  max_flow_lps REAL, max_depth_m REAL, max_d_over_d REAL, max_velocity_mps REAL,
  time_of_max_flow TEXT, surcharged_min REAL,
  d_class TEXT);                            -- low (<0.5) | medium (<0.75) | high (<1.0) | surcharged
CREATE TABLE IF NOT EXISTS wwn_sim_node_results (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom POINT,
  run_id TEXT, label TEXT, node_id TEXT NOT NULL UNIQUE,
  max_hgl_m REAL, max_depth_m REAL, freeboard_m REAL,  -- freeboard = surface level - max HGL
  flood_volume_m3 REAL, surcharged_min REAL);
CREATE TABLE IF NOT EXISTS wwn_sim_compare (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, geom LINESTRING,
  conduit_id TEXT NOT NULL UNIQUE, run_a TEXT, run_b TEXT,
  max_flow_a_lps REAL, max_flow_b_lps REAL, d_flow_lps REAL, d_flow_pct REAL,
  max_d_over_d_a REAL, max_d_over_d_b REAL, d_d_over_d REAL,
  surcharged_a INTEGER, surcharged_b INTEGER,
  change TEXT);                             -- worse | better | same | new_surcharge | relieved

INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES
 ('wwn_nodes','features','Nodes',7850), ('wwn_conduits','features','Gravity sewers',7850),
 ('wwn_pump_stations','features','Pump stations',7850), ('wwn_rising_mains','features','Rising mains',7850),
 ('wwn_subcatchments','features','Sub-catchments',7850),
 ('wwn_pumps','attributes','Pumps',NULL), ('wwn_meta','attributes','Model metadata',NULL),
 ('wwn_results_conduits','features','Design results',7850), ('wwn_issues','features','Issues',7850),
 ('wwn_node_loads','attributes','Node loads',NULL), ('wwn_profiles','attributes','Profile values',NULL),
 ('wwn_profile_info','attributes','Profiles',NULL),
 ('wwn_sim_runs','attributes','Simulation runs',NULL), ('wwn_sim_links','attributes','Simulation link results',NULL),
 ('wwn_sim_nodes','attributes','Simulation node results',NULL), ('wwn_sim_series','attributes','Simulation time series',NULL),
 ('wwn_sim_pumps','attributes','Simulation pump results',NULL), ('wwn_sim_pump_stations','attributes','Simulation pump station results',NULL),
 ('wwn_sim_rising_mains','attributes','Simulation rising main results',NULL),
 ('wwn_sim_conduit_results','features','Simulation results (conduits)',7850),
 ('wwn_sim_node_results','features','Simulation results (nodes)',7850),
 ('wwn_sim_compare','features','Simulation comparison',7850);
INSERT OR IGNORE INTO gpkg_geometry_columns VALUES
 ('wwn_nodes','geom','POINT',7850,0,0), ('wwn_conduits','geom','LINESTRING',7850,0,0),
 ('wwn_pump_stations','geom','POINT',7850,0,0), ('wwn_rising_mains','geom','LINESTRING',7850,0,0),
 ('wwn_subcatchments','geom','MULTIPOLYGON',7850,0,0),
 ('wwn_results_conduits','geom','LINESTRING',7850,0,0), ('wwn_issues','geom','POINT',7850,0,0),
 ('wwn_sim_conduit_results','geom','LINESTRING',7850,0,0), ('wwn_sim_node_results','geom','POINT',7850,0,0),
 ('wwn_sim_compare','geom','LINESTRING',7850,0,0);
