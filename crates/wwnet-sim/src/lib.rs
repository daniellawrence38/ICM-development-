//! Dynamic-wave hydraulic simulation of WWNet gravity networks using the embedded
//! EPA SWMM 5.2.4 engine.
//!
//! Pipeline: [`SimInput`] -> [`inp::build_inp`] (SWMM input text) -> [`simulate`] (runs the
//! engine in-process, post-processes the time series after the warm-up period) ->
//! [`SimResult`].
pub mod inp;
pub mod run;
pub mod types;

pub use run::{simulate, simulate_gpkg_free};
pub use types::*;
