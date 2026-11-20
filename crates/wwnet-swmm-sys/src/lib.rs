//! Raw bindings to the vendored EPA SWMM 5.2.4 engine (public domain).
//!
//! SWMM keeps all project state in process-global variables, so only one simulation may
//! be active at a time. Callers must hold the guard returned by [`lock`] for the whole
//! `swmm_open` .. `swmm_close` sequence.
#![allow(non_upper_case_globals)]

use std::os::raw::{c_char, c_double, c_float, c_int};
use std::sync::{Mutex, MutexGuard};

static SWMM_LOCK: Mutex<()> = Mutex::new(());

/// Acquire the process-wide SWMM lock (poison-tolerant).
pub fn lock() -> MutexGuard<'static, ()> {
    SWMM_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

// swmm_Object
pub const SWMM_NODE: c_int = 2;
pub const SWMM_LINK: c_int = 3;

// swmm_NodeProperty
pub const SWMM_NODE_ELEV: c_int = 301;
pub const SWMM_NODE_MAXDEPTH: c_int = 302;
pub const SWMM_NODE_DEPTH: c_int = 303;
pub const SWMM_NODE_HEAD: c_int = 304;
pub const SWMM_NODE_VOLUME: c_int = 305;
pub const SWMM_NODE_LATFLOW: c_int = 306;
pub const SWMM_NODE_INFLOW: c_int = 307;
pub const SWMM_NODE_OVERFLOW: c_int = 308;

// swmm_LinkProperty
pub const SWMM_LINK_LENGTH: c_int = 403;
pub const SWMM_LINK_SLOPE: c_int = 404;
pub const SWMM_LINK_FULLDEPTH: c_int = 405;
pub const SWMM_LINK_FULLFLOW: c_int = 406;
pub const SWMM_LINK_FLOW: c_int = 410;
pub const SWMM_LINK_DEPTH: c_int = 411;
pub const SWMM_LINK_VELOCITY: c_int = 412;
pub const SWMM_LINK_TOPWIDTH: c_int = 413;

// swmm_SystemProperty
pub const SWMM_ELAPSEDTIME: c_int = 2;
pub const SWMM_ROUTESTEP: c_int = 3;

// Exported with `extern "system"` so the (Windows-only) `__stdcall` in swmm5.h matches on
// 32-bit targets; on every other target this is the C ABI.
extern "system" {
    pub fn swmm_run(f1: *const c_char, f2: *const c_char, f3: *const c_char) -> c_int;
    pub fn swmm_open(f1: *const c_char, f2: *const c_char, f3: *const c_char) -> c_int;
    pub fn swmm_start(save_flag: c_int) -> c_int;
    /// `elapsed_time` is returned in decimal days; 0 means the run has finished.
    pub fn swmm_step(elapsed_time: *mut c_double) -> c_int;
    pub fn swmm_end() -> c_int;
    pub fn swmm_report() -> c_int;
    pub fn swmm_close() -> c_int;
    pub fn swmm_getMassBalErr(
        runoff_err: *mut c_float,
        flow_err: *mut c_float,
        qual_err: *mut c_float,
    ) -> c_int;
    pub fn swmm_getVersion() -> c_int;
    pub fn swmm_getError(err_msg: *mut c_char, msg_len: c_int) -> c_int;
    pub fn swmm_getWarnings() -> c_int;
    pub fn swmm_getCount(obj_type: c_int) -> c_int;
    pub fn swmm_getName(obj_type: c_int, index: c_int, name: *mut c_char, size: c_int);
    pub fn swmm_getIndex(obj_type: c_int, name: *const c_char) -> c_int;
    pub fn swmm_getValue(property: c_int, index: c_int) -> c_double;
}

/// Engine version as an integer, e.g. `52004` for 5.2.4.
pub fn version() -> i32 {
    let _g = lock();
    unsafe { swmm_getVersion() }
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_is_5_2_4() {
        assert_eq!(super::version(), 52004);
    }
}
