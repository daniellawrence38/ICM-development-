//! GeoPackage I/O: schema creation, geometry blobs, model load/save.
use crate::error::{Error, Result};
use crate::geom::{bbox, LineString, MultiPolygon, Point};
use crate::model::*;
use crate::validate::Issue;
use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;

pub const SCHEMA_SQL: &str = include_str!("../../../templates/schema.sql");
pub const SRS_ID: i32 = 7850;

// ---------------------------------------------------------------- geometry blobs

/// Decoded geometry (Z/M dropped).
#[derive(Debug, Clone, PartialEq)]
pub enum Geometry {
    Point(Point),
    LineString(LineString),
    MultiPolygon(MultiPolygon),
}

fn put_header(out: &mut Vec<u8>, env: Option<(f64, f64, f64, f64)>) {
    out.extend_from_slice(b"GP");
    out.push(0);
    let env_type: u8 = if env.is_some() { 1 } else { 0 };
    out.push(1 | (env_type << 1)); // little endian, envelope type
    out.extend_from_slice(&SRS_ID.to_le_bytes());
    if let Some((minx, miny, maxx, maxy)) = env {
        for v in [minx, maxx, miny, maxy] {
            out.extend_from_slice(&v.to_le_bytes());
        }
    }
}

fn put_pt(out: &mut Vec<u8>, p: &Point) {
    out.extend_from_slice(&p.x.to_le_bytes());
    out.extend_from_slice(&p.y.to_le_bytes());
}

fn put_wkb_head(out: &mut Vec<u8>, ty: u32) {
    out.push(1);
    out.extend_from_slice(&ty.to_le_bytes());
}

fn put_count(out: &mut Vec<u8>, n: usize) {
    out.extend_from_slice(&(n as u32).to_le_bytes());
}

pub fn encode_point(p: &Point) -> Vec<u8> {
    let mut o = Vec::with_capacity(29);
    put_header(&mut o, None);
    put_wkb_head(&mut o, 1);
    put_pt(&mut o, p);
    o
}

pub fn encode_linestring(l: &LineString) -> Vec<u8> {
    let mut o = Vec::new();
    put_header(&mut o, bbox(&l.0));
    put_wkb_head(&mut o, 2);
    put_count(&mut o, l.0.len());
    l.0.iter().for_each(|p| put_pt(&mut o, p));
    o
}

pub fn encode_multipolygon(m: &MultiPolygon) -> Vec<u8> {
    let mut o = Vec::new();
    put_header(&mut o, bbox(m.0.iter().flatten().flatten()));
    put_wkb_head(&mut o, 6);
    put_count(&mut o, m.0.len());
    for poly in &m.0 {
        put_wkb_head(&mut o, 3);
        put_count(&mut o, poly.len());
        for ring in poly {
            put_count(&mut o, ring.len());
            ring.iter().for_each(|p| put_pt(&mut o, p));
        }
    }
    o
}

struct Rd<'a> {
    b: &'a [u8],
    pos: usize,
    le: bool,
}

impl<'a> Rd<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        let end = self.pos.checked_add(n).filter(|e| *e <= self.b.len());
        match end {
            Some(e) => {
                let s = &self.b[self.pos..e];
                self.pos = e;
                Ok(s)
            }
            None => Err(Error::Geometry("blob truncated".into())),
        }
    }
    fn u32(&mut self) -> Result<u32> {
        let s: [u8; 4] = self.take(4)?.try_into().unwrap();
        Ok(if self.le {
            u32::from_le_bytes(s)
        } else {
            u32::from_be_bytes(s)
        })
    }
    fn f64(&mut self) -> Result<f64> {
        let s: [u8; 8] = self.take(8)?.try_into().unwrap();
        Ok(if self.le {
            f64::from_le_bytes(s)
        } else {
            f64::from_be_bytes(s)
        })
    }
    fn order(&mut self) -> Result<()> {
        self.le = match self.take(1)?[0] {
            0 => false,
            1 => true,
            b => return Err(Error::Geometry(format!("bad WKB byte order {b}"))),
        };
        Ok(())
    }
}

/// Returns (base type 1..7, has_z, has_m); handles ISO (1000s) and EWKB flag styles.
fn wkb_type(raw: u32) -> (u32, bool, bool) {
    let mut z = raw & 0x8000_0000 != 0;
    let mut m = raw & 0x4000_0000 != 0;
    let code = raw & 0x0FFF_FFFF;
    let base = code % 1000;
    match code / 1000 {
        1 => z = true,
        2 => m = true,
        3 => {
            z = true;
            m = true
        }
        _ => {}
    }
    (base, z, m)
}

fn rd_pt(r: &mut Rd, z: bool, m: bool) -> Result<Point> {
    let x = r.f64()?;
    let y = r.f64()?;
    if z {
        r.f64()?;
    }
    if m {
        r.f64()?;
    }
    Ok(Point { x, y })
}

fn rd_pts(r: &mut Rd, z: bool, m: bool) -> Result<Vec<Point>> {
    let n = r.u32()? as usize;
    if n > r.b.len() {
        return Err(Error::Geometry("implausible point count".into()));
    }
    (0..n).map(|_| rd_pt(r, z, m)).collect()
}

fn rd_polygon(r: &mut Rd, z: bool, m: bool) -> Result<Vec<Vec<Point>>> {
    let n = r.u32()? as usize;
    if n > r.b.len() {
        return Err(Error::Geometry("implausible ring count".into()));
    }
    (0..n).map(|_| rd_pts(r, z, m)).collect()
}

fn rd_geometry(r: &mut Rd) -> Result<Geometry> {
    r.order()?;
    let raw = r.u32()?;
    let (base, z, m) = wkb_type(raw);
    if raw & 0x2000_0000 != 0 {
        r.u32()?; // EWKB srid
    }
    match base {
        1 => Ok(Geometry::Point(rd_pt(r, z, m)?)),
        2 => Ok(Geometry::LineString(LineString(rd_pts(r, z, m)?))),
        3 => Ok(Geometry::MultiPolygon(MultiPolygon(vec![rd_polygon(
            r, z, m,
        )?]))),
        6 => {
            let n = r.u32()? as usize;
            if n > r.b.len() {
                return Err(Error::Geometry("implausible polygon count".into()));
            }
            let mut polys = Vec::with_capacity(n);
            for _ in 0..n {
                r.order()?;
                let raw = r.u32()?;
                let (b, z, m) = wkb_type(raw);
                if b != 3 {
                    return Err(Error::Geometry(format!("expected Polygon, got type {b}")));
                }
                polys.push(rd_polygon(r, z, m)?);
            }
            Ok(Geometry::MultiPolygon(MultiPolygon(polys)))
        }
        t => Err(Error::Geometry(format!("unsupported WKB type {t}"))),
    }
}

/// Decode a GeoPackage geometry blob. `None` for an empty geometry.
pub fn decode(blob: &[u8]) -> Result<Option<Geometry>> {
    if blob.len() < 8 || &blob[0..2] != b"GP" {
        return Err(Error::Geometry("missing GP magic".into()));
    }
    if blob[2] != 0 {
        return Err(Error::Geometry(format!(
            "unsupported GP version {}",
            blob[2]
        )));
    }
    let flags = blob[3];
    let env_len = match (flags >> 1) & 7 {
        0 => 0,
        1 => 32,
        2 | 3 => 48,
        4 => 64,
        e => return Err(Error::Geometry(format!("bad envelope type {e}"))),
    };
    if flags & 0x10 != 0 {
        return Ok(None);
    }
    let start = 8 + env_len;
    if blob.len() <= start {
        return Err(Error::Geometry("blob truncated".into()));
    }
    let mut r = Rd {
        b: blob,
        pos: start,
        le: true,
    };
    rd_geometry(&mut r).map(Some)
}

pub fn decode_point(blob: &[u8]) -> Result<Option<Point>> {
    match decode(blob)? {
        None => Ok(None),
        Some(Geometry::Point(p)) => Ok(Some(p)),
        Some(g) => Err(Error::Geometry(format!("expected Point, got {g:?}"))),
    }
}

pub fn decode_linestring(blob: &[u8]) -> Result<Option<LineString>> {
    match decode(blob)? {
        None => Ok(None),
        Some(Geometry::LineString(p)) => Ok(Some(p)),
        Some(_) => Err(Error::Geometry("expected LineString".into())),
    }
}

pub fn decode_multipolygon(blob: &[u8]) -> Result<Option<MultiPolygon>> {
    match decode(blob)? {
        None => Ok(None),
        Some(Geometry::MultiPolygon(p)) => Ok(Some(p)),
        Some(_) => Err(Error::Geometry("expected MultiPolygon".into())),
    }
}

// ---------------------------------------------------------------- file creation

/// Create a new empty model GeoPackage. Fails if the file already has content.
pub fn create(path: impl AsRef<Path>) -> Result<()> {
    let path = path.as_ref();
    if let Ok(md) = std::fs::metadata(path) {
        if md.len() > 0 {
            return Err(Error::Other(format!(
                "{} already exists and is not empty",
                path.display()
            )));
        }
    }
    let conn = Connection::open(path)?;
    conn.execute_batch(SCHEMA_SQL)?;
    Ok(())
}

pub fn open(path: impl AsRef<Path>) -> Result<Connection> {
    Ok(Connection::open(path)?)
}

// ---------------------------------------------------------------- insert helpers

fn pt_blob(g: &Option<Point>) -> Option<Vec<u8>> {
    g.as_ref().map(encode_point)
}
fn ls_blob(g: &Option<LineString>) -> Option<Vec<u8>> {
    g.as_ref().map(encode_linestring)
}

pub fn insert_node(c: &Connection, n: &Node) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_nodes (geom,node_id,node_type,surface_level,invert_level,chamber_diameter_mm,flood_level,status,data_source,asset_id,install_year,notes)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        params![pt_blob(&n.geom), n.node_id, n.node_type.as_str(), n.surface_level, n.invert_level,
            n.chamber_diameter_mm, n.flood_level, n.status.as_str(), n.data_source.as_str(),
            n.asset_id, n.install_year, n.notes],
    )?;
    Ok(())
}

pub fn insert_conduit(c: &Connection, x: &Conduit) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_conduits (geom,conduit_id,us_node_id,ds_node_id,dn,internal_diameter_mm,material,pipe_class,us_invert,ds_invert,length_m,ks_mm,location,sewer_type,status,data_source,install_year,condition_grade,asset_id,notes)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![ls_blob(&x.geom), x.conduit_id, x.us_node_id, x.ds_node_id, x.dn,
            x.internal_diameter_mm, x.material.as_str(), x.pipe_class, x.us_invert, x.ds_invert,
            x.length_m, x.ks_mm, x.location.as_str(), x.sewer_type.as_str(), x.status.as_str(),
            x.data_source.as_str(), x.install_year, x.condition_grade, x.asset_id, x.notes],
    )?;
    Ok(())
}

pub fn insert_pump_station(c: &Connection, p: &PumpStation) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_pump_stations (geom,ps_id,wet_well_node_id,discharge_node_id,rising_main_id,pump_rate_lps,duty_config,wet_well_area_m2,start_level,stop_level,overflow_level,emergency_storage_m3,status,data_source,notes)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![pt_blob(&p.geom), p.ps_id, p.wet_well_node_id, p.discharge_node_id, p.rising_main_id,
            p.pump_rate_lps, p.duty_config, p.wet_well_area_m2, p.start_level, p.stop_level,
            p.overflow_level, p.emergency_storage_m3, p.status.as_str(), p.data_source.as_str(), p.notes],
    )?;
    Ok(())
}

pub fn insert_pump(c: &Connection, p: &Pump) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_pumps (pump_id,ps_id,role,curve_json,speed_pct,notes) VALUES (?,?,?,?,?,?)",
        params![p.pump_id, p.ps_id, p.role, p.curve_json, p.speed_pct, p.notes],
    )?;
    Ok(())
}

pub fn insert_rising_main(c: &Connection, r: &RisingMain) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_rising_mains (geom,rm_id,ps_id,discharge_node_id,dn,internal_diameter_mm,material,roughness_type,roughness,length_m,profile_json,status,data_source,notes)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![ls_blob(&r.geom), r.rm_id, r.ps_id, r.discharge_node_id, r.dn, r.internal_diameter_mm,
            r.material, r.roughness_type, r.roughness, r.length_m, r.profile_json, r.status.as_str(),
            r.data_source.as_str(), r.notes],
    )?;
    Ok(())
}

pub fn insert_subcatchment(c: &Connection, s: &Subcatchment) -> Result<()> {
    c.execute(
        "INSERT INTO wwn_subcatchments (geom,subcatchment_id,outlet_node_id,net_area_ha,gross_area_ha,broadacre,land_use,rcode,ground,region,extra_flow_lps,status,notes)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![s.geom.as_ref().map(encode_multipolygon), s.subcatchment_id, s.outlet_node_id,
            s.net_area_ha, s.gross_area_ha, s.broadacre as i64, s.land_use.as_str(), s.rcode,
            s.ground.as_str(), s.region.as_str(), s.extra_flow_lps, s.status.as_str(), s.notes],
    )?;
    Ok(())
}

/// Recompute `gpkg_contents` extents for all feature tables from stored geometries.
pub fn update_extents(c: &Connection) -> Result<()> {
    for table in [
        "wwn_nodes",
        "wwn_conduits",
        "wwn_pump_stations",
        "wwn_rising_mains",
        "wwn_subcatchments",
    ] {
        let mut stmt = c.prepare(&format!("SELECT geom FROM {table} WHERE geom IS NOT NULL"))?;
        let blobs: Vec<Vec<u8>> = stmt
            .query_map([], |r| r.get(0))?
            .collect::<std::result::Result<_, _>>()?;
        let mut pts: Vec<Point> = Vec::new();
        for b in &blobs {
            match decode(b)? {
                Some(Geometry::Point(p)) => pts.push(p),
                Some(Geometry::LineString(l)) => pts.extend(l.0),
                Some(Geometry::MultiPolygon(m)) => pts.extend(m.0.into_iter().flatten().flatten()),
                None => {}
            }
        }
        let (a, b, cc, d) = match bbox(&pts) {
            Some((a, b, cc, d)) => (Some(a), Some(b), Some(cc), Some(d)),
            None => (None, None, None, None),
        };
        c.execute(
            "UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
            params![a, b, cc, d, table],
        )?;
    }
    Ok(())
}

// ---------------------------------------------------------------- results

/// One row of `wwn_results_conduits`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ConduitResult {
    pub conduit_id: String,
    pub ruleset: Option<String>,
    pub gsdf_lps: Option<f64>,
    pub capacity_lps: Option<f64>,
    pub d_over_d: Option<f64>,
    pub velocity_mps: Option<f64>,
    pub grade_1in: Option<f64>,
    /// pass | warn | fail
    pub result: String,
    /// JSON text `[{rule, clause, value, limit, result}]`
    pub messages: Option<String>,
}

// ---------------------------------------------------------------- Model I/O

fn en<T: std::str::FromStr<Err = Error> + Default>(s: Option<String>) -> Result<T> {
    match s {
        None => Ok(T::default()),
        Some(s) => s.parse(),
    }
}

impl Model {
    /// Write the whole model (inserts only; use on an empty GeoPackage) and update extents.
    pub fn write(&self, path: impl AsRef<Path>) -> Result<()> {
        let mut conn = open(path)?;
        let tx = conn.transaction()?;
        self.nodes.iter().try_for_each(|x| insert_node(&tx, x))?;
        self.conduits
            .iter()
            .try_for_each(|x| insert_conduit(&tx, x))?;
        self.pump_stations
            .iter()
            .try_for_each(|x| insert_pump_station(&tx, x))?;
        self.pumps.iter().try_for_each(|x| insert_pump(&tx, x))?;
        self.rising_mains
            .iter()
            .try_for_each(|x| insert_rising_main(&tx, x))?;
        self.subcatchments
            .iter()
            .try_for_each(|x| insert_subcatchment(&tx, x))?;
        update_extents(&tx)?;
        tx.commit()?;
        Ok(())
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Model> {
        let c = Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )?;
        let mut m = Model::default();

        let mut st = c.prepare("SELECT geom,node_id,node_type,surface_level,invert_level,chamber_diameter_mm,flood_level,status,data_source,asset_id,install_year,notes FROM wwn_nodes ORDER BY fid")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.nodes.push(Node {
                geom: r
                    .get::<_, Option<Vec<u8>>>(0)?
                    .map(|b| decode_point(&b))
                    .transpose()?
                    .flatten(),
                node_id: r.get(1)?,
                node_type: en(r.get(2)?)?,
                surface_level: r.get(3)?,
                invert_level: r.get(4)?,
                chamber_diameter_mm: r.get(5)?,
                flood_level: r.get(6)?,
                status: en(r.get(7)?)?,
                data_source: en(r.get(8)?)?,
                asset_id: r.get(9)?,
                install_year: r.get(10)?,
                notes: r.get(11)?,
            });
        }

        let mut st = c.prepare("SELECT geom,conduit_id,us_node_id,ds_node_id,dn,internal_diameter_mm,material,pipe_class,us_invert,ds_invert,length_m,ks_mm,location,sewer_type,status,data_source,install_year,condition_grade,asset_id,notes FROM wwn_conduits ORDER BY fid")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.conduits.push(Conduit {
                geom: r
                    .get::<_, Option<Vec<u8>>>(0)?
                    .map(|b| decode_linestring(&b))
                    .transpose()?
                    .flatten(),
                conduit_id: r.get(1)?,
                us_node_id: r.get(2)?,
                ds_node_id: r.get(3)?,
                dn: r.get(4)?,
                internal_diameter_mm: r.get(5)?,
                material: en(r.get(6)?)?,
                pipe_class: r.get(7)?,
                us_invert: r.get(8)?,
                ds_invert: r.get(9)?,
                length_m: r.get(10)?,
                ks_mm: r.get(11)?,
                location: en(r.get(12)?)?,
                sewer_type: en(r.get(13)?)?,
                status: en(r.get(14)?)?,
                data_source: en(r.get(15)?)?,
                install_year: r.get(16)?,
                condition_grade: r.get(17)?,
                asset_id: r.get(18)?,
                notes: r.get(19)?,
            });
        }

        let mut st = c.prepare("SELECT geom,ps_id,wet_well_node_id,discharge_node_id,rising_main_id,pump_rate_lps,duty_config,wet_well_area_m2,start_level,stop_level,overflow_level,emergency_storage_m3,status,data_source,notes FROM wwn_pump_stations ORDER BY fid")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.pump_stations.push(PumpStation {
                geom: r
                    .get::<_, Option<Vec<u8>>>(0)?
                    .map(|b| decode_point(&b))
                    .transpose()?
                    .flatten(),
                ps_id: r.get(1)?,
                wet_well_node_id: r.get(2)?,
                discharge_node_id: r.get(3)?,
                rising_main_id: r.get(4)?,
                pump_rate_lps: r.get(5)?,
                duty_config: r.get(6)?,
                wet_well_area_m2: r.get(7)?,
                start_level: r.get(8)?,
                stop_level: r.get(9)?,
                overflow_level: r.get(10)?,
                emergency_storage_m3: r.get(11)?,
                status: en(r.get(12)?)?,
                data_source: en(r.get(13)?)?,
                notes: r.get(14)?,
            });
        }

        let mut st = c.prepare(
            "SELECT pump_id,ps_id,role,curve_json,speed_pct,notes FROM wwn_pumps ORDER BY fid",
        )?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.pumps.push(Pump {
                pump_id: r.get(0)?,
                ps_id: r.get(1)?,
                role: r
                    .get::<_, Option<String>>(2)?
                    .unwrap_or_else(|| "duty".into()),
                curve_json: r.get(3)?,
                speed_pct: r.get::<_, Option<f64>>(4)?.unwrap_or(100.0),
                notes: r.get(5)?,
            });
        }

        let mut st = c.prepare("SELECT geom,rm_id,ps_id,discharge_node_id,dn,internal_diameter_mm,material,roughness_type,roughness,length_m,profile_json,status,data_source,notes FROM wwn_rising_mains ORDER BY fid")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.rising_mains.push(RisingMain {
                geom: r
                    .get::<_, Option<Vec<u8>>>(0)?
                    .map(|b| decode_linestring(&b))
                    .transpose()?
                    .flatten(),
                rm_id: r.get(1)?,
                ps_id: r.get(2)?,
                discharge_node_id: r.get(3)?,
                dn: r.get(4)?,
                internal_diameter_mm: r.get(5)?,
                material: r.get(6)?,
                roughness_type: r
                    .get::<_, Option<String>>(7)?
                    .unwrap_or_else(|| "ks".into()),
                roughness: r.get(8)?,
                length_m: r.get(9)?,
                profile_json: r.get(10)?,
                status: en(r.get(11)?)?,
                data_source: en(r.get(12)?)?,
                notes: r.get(13)?,
            });
        }

        let mut st = c.prepare("SELECT geom,subcatchment_id,outlet_node_id,net_area_ha,gross_area_ha,broadacre,land_use,rcode,ground,region,extra_flow_lps,status,notes FROM wwn_subcatchments ORDER BY fid")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            m.subcatchments.push(Subcatchment {
                geom: r
                    .get::<_, Option<Vec<u8>>>(0)?
                    .map(|b| decode_multipolygon(&b))
                    .transpose()?
                    .flatten(),
                subcatchment_id: r.get(1)?,
                outlet_node_id: r.get(2)?,
                net_area_ha: r.get(3)?,
                gross_area_ha: r.get(4)?,
                broadacre: r.get::<_, Option<i64>>(5)?.unwrap_or(0) != 0,
                land_use: en(r.get(6)?)?,
                rcode: r
                    .get::<_, Option<String>>(7)?
                    .unwrap_or_else(|| "R20".into()),
                ground: en(r.get(8)?)?,
                region: en(r.get(9)?)?,
                extra_flow_lps: r.get::<_, Option<f64>>(10)?.unwrap_or(0.0),
                status: en(r.get(11)?)?,
                notes: r.get(12)?,
            });
        }
        Ok(m)
    }

    /// Replace the contents of `wwn_results_conduits` with `results`.
    pub fn save_results(path: impl AsRef<Path>, results: &[ConduitResult]) -> Result<()> {
        let mut c = open(path)?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM wwn_results_conduits", [])?;
        for r in results {
            tx.execute(
                "INSERT INTO wwn_results_conduits (conduit_id,run_at,ruleset,gsdf_lps,capacity_lps,d_over_d,velocity_mps,grade_1in,result,messages)
                 VALUES (?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?,?,?,?,?,?,?)",
                params![r.conduit_id, r.ruleset, r.gsdf_lps, r.capacity_lps, r.d_over_d,
                    r.velocity_mps, r.grade_1in, r.result, r.messages],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Replace the contents of `wwn_issues` with `issues`.
    pub fn write_issues(path: impl AsRef<Path>, issues: &[Issue]) -> Result<()> {
        let mut c = open(path)?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM wwn_issues", [])?;
        for i in issues {
            tx.execute(
                "INSERT INTO wwn_issues (run_at,severity,code,object_type,object_id,clause,message)
                 VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?,?,?,?,?)",
                params![
                    i.severity.as_str(),
                    i.code,
                    i.object_type,
                    i.object_id,
                    i.clause,
                    i.message
                ],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Read back `wwn_issues` (mainly for tests/tools).
    pub fn read_issue_count(path: impl AsRef<Path>) -> Result<i64> {
        let c = open(path)?;
        Ok(
            c.query_row("SELECT count(*) FROM wwn_issues", [], |r| r.get(0))
                .optional()?
                .unwrap_or(0),
        )
    }
}
