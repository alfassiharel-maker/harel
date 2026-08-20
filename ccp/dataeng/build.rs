// Compiles the C++ bit execution engine and links it into this crate.
//
// Done with a direct compiler invocation rather than the `cc` crate to keep the
// build dependency-free (see Cargo.toml). The trade-off is that this handles only
// the platforms CCP is built on today; it fails loudly rather than silently
// producing a library without the engine.

use std::path::PathBuf;
use std::process::Command;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let bitexec = manifest.parent().unwrap().join("bitexec");
    let src = bitexec.join("src/bitexec.cpp");
    let include = bitexec.join("include");
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let obj = out.join("bitexec.o");
    let lib = out.join("libccp_bitexec.a");

    // Rebuild when the engine or its header changes; without these the Rust side
    // would keep linking a stale object after a C++ edit.
    println!("cargo:rerun-if-changed={}", src.display());
    println!("cargo:rerun-if-changed={}", include.join("ccp_bitexec.h").display());

    let cxx = std::env::var("CXX").unwrap_or_else(|_| "c++".to_string());

    let status = Command::new(&cxx)
        .args(["-std=c++17", "-O3", "-fPIC", "-Wall", "-Wextra", "-c"])
        .arg(&src)
        .arg("-I")
        .arg(&include)
        .arg("-o")
        .arg(&obj)
        .status()
        .unwrap_or_else(|e| panic!("failed to run C++ compiler {cxx:?}: {e}"));
    assert!(status.success(), "compiling {} failed", src.display());

    let status = Command::new("ar")
        .arg("crs")
        .arg(&lib)
        .arg(&obj)
        .status()
        .expect("failed to run ar");
    assert!(status.success(), "archiving the bit engine failed");

    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=ccp_bitexec");
    // The engine is C++; without its runtime the link fails on std::memcpy's
    // libstdc++ dependencies.
    println!("cargo:rustc-link-lib=dylib=stdc++");
}
