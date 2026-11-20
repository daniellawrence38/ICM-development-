//! Compile the vendored SWMM 5.2.4 solver into a static library.
//! OpenMP is deliberately not enabled (the pragmas are ignored without it), which keeps
//! MSVC and gcc builds identical and results deterministic.
use std::path::PathBuf;

fn main() {
    let dir = PathBuf::from("swmm");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .expect("swmm dir")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "c"))
        .collect();
    files.sort();
    let mut b = cc::Build::new();
    b.files(&files)
        .include(&dir)
        .include(dir.join("include"))
        .warnings(false);
    // Static library: no __declspec(dllexport)/__stdcall on the API.
    b.define("SWMM_STATIC", None);
    if b.get_compiler().is_like_msvc() {
        b.define("_CRT_SECURE_NO_WARNINGS", None);
        b.flag("/fp:precise");
    } else {
        b.flag_if_supported("-fno-strict-aliasing");
        b.flag_if_supported("-w");
        println!("cargo:rustc-link-lib=m");
    }
    b.compile("swmm5");
    println!("cargo:rerun-if-changed=swmm");
    println!("cargo:rerun-if-changed=build.rs");
}
