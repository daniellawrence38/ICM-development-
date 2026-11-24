//! Embed Windows version information (file Properties > Details) in `wwnet_native.pyd`.
//!
//! An unsigned DLL without any version resource is scored as more suspicious by antivirus
//! machine-learning models. This is ordinary product metadata, nothing else; on non-Windows
//! targets the script does nothing.
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let version = env!("CARGO_PKG_VERSION");
    let mut res = winresource::WindowsResource::new();
    res.set("ProductName", "WWNet")
        .set(
            "FileDescription",
            "WWNet wastewater network engine (Python extension module)",
        )
        .set("CompanyName", "WWNet developers")
        .set(
            "LegalCopyright",
            "WWNet developers. Includes EPA SWMM 5.2 (public domain).",
        )
        .set("OriginalFilename", "wwnet_native.pyd")
        .set("InternalName", "wwnet_native")
        .set("FileVersion", version)
        .set("ProductVersion", version);
    // Fail the build rather than ship a binary silently missing its version information.
    res.compile()
        .expect("embedding the Windows version resource (needs rc.exe from the Windows SDK)");
}
