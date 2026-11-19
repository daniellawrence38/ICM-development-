//! Core data model, GeoPackage I/O, network graph and validation for WWNet.
pub mod error;
pub mod geom;
pub mod gpkg;
pub mod model;
pub mod network;
pub mod validate;

pub use error::{Error, Result};
pub use geom::{LineString, MultiPolygon, Point};
pub use model::*;
pub use network::Network;
pub use validate::{validate, Issue, Severity};
