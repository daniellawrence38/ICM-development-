//! Typed DS50 (Water Corporation, Ver 2 Rev 4) rule set and design checks.
//!
//! The rule set is data (`rulesets/ds50_v2r4.toml`); this crate deserialises it,
//! provides lookups for loading rates and Table 4.4/4.5/4.8 values, and runs
//! [`check_conduit`] returning clause-referenced [`Check`] results.
//! Units: L/s, L/day, m, ha; grades are "1 in x" denominators.

mod checks;
mod ruleset;

pub use checks::*;
pub use ruleset::*;
