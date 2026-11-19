-- WWNet GeoPackage schema v1. Single source of truth for wwnet-core (Rust) and the QGIS plugin.
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
INSERT OR IGNORE INTO wwn_meta VALUES ('schema_version','1'), ('ruleset','ds50_v2r4'), ('crs','EPSG:7850');

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
  status TEXT NOT NULL DEFAULT 'proposed', notes TEXT);

-- Written by `wwnet design`; read-only for users.
CREATE TABLE IF NOT EXISTS wwn_results_conduits (
  conduit_id TEXT PRIMARY KEY, run_at TEXT, ruleset TEXT,
  gsdf_lps REAL, capacity_lps REAL, d_over_d REAL, velocity_mps REAL,
  grade_1in REAL, result TEXT,              -- pass | warn | fail
  messages TEXT);                           -- JSON [{rule, clause, value, limit, result}]
CREATE TABLE IF NOT EXISTS wwn_issues (
  fid INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, severity TEXT, code TEXT,
  object_type TEXT, object_id TEXT, clause TEXT, message TEXT);

INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES
 ('wwn_nodes','features','Nodes',7850), ('wwn_conduits','features','Gravity sewers',7850),
 ('wwn_pump_stations','features','Pump stations',7850), ('wwn_rising_mains','features','Rising mains',7850),
 ('wwn_subcatchments','features','Sub-catchments',7850),
 ('wwn_pumps','attributes','Pumps',NULL), ('wwn_meta','attributes','Model metadata',NULL),
 ('wwn_results_conduits','attributes','Design results',NULL), ('wwn_issues','attributes','Validation issues',NULL);
INSERT OR IGNORE INTO gpkg_geometry_columns VALUES
 ('wwn_nodes','geom','POINT',7850,0,0), ('wwn_conduits','geom','LINESTRING',7850,0,0),
 ('wwn_pump_stations','geom','POINT',7850,0,0), ('wwn_rising_mains','geom','LINESTRING',7850,0,0),
 ('wwn_subcatchments','geom','MULTIPOLYGON',7850,0,0);
