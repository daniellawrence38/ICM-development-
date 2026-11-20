//! Entity structs mirroring `templates/schema.sql`.
use crate::error::{Error, Result};
use crate::geom::{LineString, MultiPolygon, Point};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

macro_rules! str_enum {
    ($(#[$m:meta])* $name:ident = $def:ident { $($var:ident => $s:literal),+ $(,)? }) => {
        $(#[$m])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        pub enum $name { $( #[serde(rename = $s)] $var ),+ }
        impl $name {
            pub const ALL: &'static [$name] = &[ $( $name::$var ),+ ];
            pub fn as_str(&self) -> &'static str {
                match self { $( $name::$var => $s ),+ }
            }
        }
        impl Default for $name { fn default() -> Self { $name::$def } }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str(self.as_str()) }
        }
        impl FromStr for $name {
            type Err = Error;
            fn from_str(s: &str) -> Result<Self> {
                match s.trim().to_ascii_lowercase().as_str() {
                    $( $s => Ok($name::$var), )+
                    _ => Err(Error::UnknownEnum { kind: stringify!($name), value: s.to_string() }),
                }
            }
        }
    };
}

str_enum!(NodeType = Ac {
    Ac => "ac", Ms => "ms", DropAc => "drop_ac", Junction => "junction",
    WetWell => "wet_well", Outfall => "outfall", IoEnd => "io_end", IsEnd => "is_end",
});
str_enum!(Status = Proposed { Existing => "existing", Proposed => "proposed", Abandoned => "abandoned" });
str_enum!(DataSource = Design {
    AsCon => "as_con", Gis => "gis", Survey => "survey", Design => "design", Assumed => "assumed",
});
str_enum!(Material = Pvc {
    Pvc => "pvc", Vc => "vc", Rcpl => "rcpl", Di => "di", Pe => "pe", Grp => "grp",
    AcPipe => "ac_pipe", Other => "other",
});
str_enum!(Location = RoadReserve {
    RoadReserve => "road_reserve", TrafficableRow => "trafficable_row", Lot => "lot",
    Pos => "pos", Paw => "paw", Other => "other",
});
str_enum!(SewerType = Reticulation {
    Reticulation => "reticulation", Io => "io", Is => "is", Main => "main",
});
str_enum!(LandUse = Residential {
    Residential => "residential", Commercial => "commercial", HotelMotel => "hotel_motel",
    PerthCba => "perth_cba", Industrial => "industrial",
});
str_enum!(Ground = Dry { Dry => "dry", Wet => "wet" });
str_enum!(Region = South { South => "south", North => "north" });

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub node_id: String,
    pub geom: Option<Point>,
    #[serde(default)]
    pub node_type: NodeType,
    pub surface_level: Option<f64>,
    pub invert_level: Option<f64>,
    pub chamber_diameter_mm: Option<i64>,
    pub flood_level: Option<f64>,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub data_source: DataSource,
    pub asset_id: Option<String>,
    pub install_year: Option<i64>,
    pub notes: Option<String>,
}

impl Node {
    pub fn new(id: &str, x: f64, y: f64) -> Self {
        Node {
            node_id: id.into(),
            geom: Some(Point::new(x, y)),
            node_type: NodeType::default(),
            surface_level: None,
            invert_level: None,
            chamber_diameter_mm: None,
            flood_level: None,
            status: Status::default(),
            data_source: DataSource::default(),
            asset_id: None,
            install_year: None,
            notes: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Conduit {
    pub conduit_id: String,
    #[serde(default)]
    pub geom: Option<LineString>,
    pub us_node_id: String,
    pub ds_node_id: String,
    pub dn: i64,
    pub internal_diameter_mm: Option<f64>,
    #[serde(default)]
    pub material: Material,
    pub pipe_class: Option<String>,
    pub us_invert: Option<f64>,
    pub ds_invert: Option<f64>,
    pub length_m: Option<f64>,
    pub ks_mm: Option<f64>,
    #[serde(default)]
    pub location: Location,
    #[serde(default)]
    pub sewer_type: SewerType,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub data_source: DataSource,
    pub install_year: Option<i64>,
    pub condition_grade: Option<i64>,
    pub asset_id: Option<String>,
    pub notes: Option<String>,
}

impl Conduit {
    pub fn new(id: &str, us: &str, ds: &str, dn: i64) -> Self {
        Conduit {
            conduit_id: id.into(),
            geom: None,
            us_node_id: us.into(),
            ds_node_id: ds.into(),
            dn,
            internal_diameter_mm: None,
            material: Material::default(),
            pipe_class: None,
            us_invert: None,
            ds_invert: None,
            length_m: None,
            ks_mm: None,
            location: Location::default(),
            sewer_type: SewerType::default(),
            status: Status::default(),
            data_source: DataSource::default(),
            install_year: None,
            condition_grade: None,
            asset_id: None,
            notes: None,
        }
    }
    /// Stored length, or the geometry length when NULL.
    pub fn effective_length(&self) -> Option<f64> {
        self.length_m
            .or_else(|| self.geom.as_ref().map(|g| g.length()))
    }
    /// Internal diameter in mm (falls back to DN, the DS 50 basis).
    pub fn effective_id_mm(&self) -> f64 {
        self.internal_diameter_mm.unwrap_or(self.dn as f64)
    }
    /// Grade as 1 in N (positive when falling downstream).
    pub fn grade_1_in(&self) -> Option<f64> {
        let (u, d, l) = (self.us_invert?, self.ds_invert?, self.effective_length()?);
        let drop = u - d;
        if drop > 0.0 && l > 0.0 {
            Some(l / drop)
        } else {
            None
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PumpStation {
    pub ps_id: String,
    pub geom: Option<Point>,
    pub wet_well_node_id: Option<String>,
    pub discharge_node_id: Option<String>,
    pub rising_main_id: Option<String>,
    pub pump_rate_lps: Option<f64>,
    pub duty_config: Option<String>,
    pub wet_well_area_m2: Option<f64>,
    pub start_level: Option<f64>,
    pub stop_level: Option<f64>,
    pub overflow_level: Option<f64>,
    pub emergency_storage_m3: Option<f64>,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub data_source: DataSource,
    pub notes: Option<String>,
}

impl PumpStation {
    pub fn new(id: &str) -> Self {
        PumpStation {
            ps_id: id.into(),
            geom: None,
            wet_well_node_id: None,
            discharge_node_id: None,
            rising_main_id: None,
            pump_rate_lps: None,
            duty_config: None,
            wet_well_area_m2: None,
            start_level: None,
            stop_level: None,
            overflow_level: None,
            emergency_storage_m3: None,
            status: Status::default(),
            data_source: DataSource::default(),
            notes: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pump {
    pub pump_id: String,
    pub ps_id: String,
    #[serde(default = "default_role")]
    pub role: String,
    pub curve_json: Option<String>,
    #[serde(default = "default_speed")]
    pub speed_pct: f64,
    pub notes: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RisingMain {
    pub rm_id: String,
    pub geom: Option<LineString>,
    pub ps_id: Option<String>,
    pub discharge_node_id: Option<String>,
    pub dn: Option<i64>,
    pub internal_diameter_mm: Option<f64>,
    pub material: Option<String>,
    #[serde(default = "default_roughness_type")]
    pub roughness_type: String,
    pub roughness: Option<f64>,
    pub length_m: Option<f64>,
    pub profile_json: Option<String>,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub data_source: DataSource,
    pub notes: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Subcatchment {
    pub subcatchment_id: String,
    pub geom: Option<MultiPolygon>,
    pub outlet_node_id: String,
    pub net_area_ha: Option<f64>,
    pub gross_area_ha: Option<f64>,
    #[serde(default)]
    pub broadacre: bool,
    #[serde(default)]
    pub land_use: LandUse,
    #[serde(default = "default_rcode")]
    pub rcode: String,
    #[serde(default)]
    pub ground: Ground,
    #[serde(default)]
    pub region: Region,
    #[serde(default)]
    pub extra_flow_lps: f64,
    #[serde(default)]
    pub status: Status,
    pub notes: Option<String>,
}

impl Subcatchment {
    pub fn new(id: &str, outlet: &str) -> Self {
        Subcatchment {
            subcatchment_id: id.into(),
            geom: None,
            outlet_node_id: outlet.into(),
            net_area_ha: None,
            gross_area_ha: None,
            broadacre: false,
            land_use: LandUse::default(),
            rcode: "R20".into(),
            ground: Ground::default(),
            region: Region::default(),
            extra_flow_lps: 0.0,
            status: Status::default(),
            notes: None,
        }
    }
}

fn default_role() -> String {
    "duty".into()
}
fn default_speed() -> f64 {
    100.0
}
fn default_roughness_type() -> String {
    "ks".into()
}
fn default_rcode() -> String {
    "R20".into()
}

/// The whole in-memory model.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Model {
    #[serde(default)]
    pub nodes: Vec<Node>,
    #[serde(default)]
    pub conduits: Vec<Conduit>,
    #[serde(default)]
    pub pump_stations: Vec<PumpStation>,
    #[serde(default)]
    pub pumps: Vec<Pump>,
    #[serde(default)]
    pub rising_mains: Vec<RisingMain>,
    #[serde(default)]
    pub subcatchments: Vec<Subcatchment>,
}
