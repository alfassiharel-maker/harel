//! Safe bindings to the C++ bit execution engine.
//!
//! Every `unsafe` block in the data engine lives in this module. The rule the
//! bindings uphold is the one the C header states: pointers are valid for the
//! declared length. Each wrapper takes slices, derives the length from the
//! shorter one where two are involved, and passes a length no larger than either
//! buffer — so the engine cannot be handed an out-of-bounds range even if a
//! caller's arithmetic is wrong.

use std::os::raw::{c_char, c_int};

#[repr(C)]
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BlockStats {
    pub changed_bits: u64,
    pub changed_bytes: u64,
    pub length: u64,
}

extern "C" {
    fn ccp_xor(a: *const u8, b: *const u8, out: *mut u8, n: usize);
    fn ccp_xor_inplace(dst: *mut u8, src: *const u8, n: usize);
    fn ccp_popcount(p: *const u8, n: usize) -> u64;
    fn ccp_xor_popcount(a: *const u8, b: *const u8, n: usize) -> u64;
    fn ccp_xor_nonzero_bytes(a: *const u8, b: *const u8, n: usize) -> u64;
    fn ccp_block_measure(a: *const u8, b: *const u8, n: usize, out: *mut BlockStats);
    fn ccp_equal(a: *const u8, b: *const u8, n: usize) -> c_int;
    fn ccp_bitexec_version() -> *const c_char;
    fn ccp_bitexec_features() -> u32;
}

/// `out = a XOR b`, over `min` of the three lengths.
pub fn xor(a: &[u8], b: &[u8], out: &mut [u8]) -> usize {
    let n = a.len().min(b.len()).min(out.len());
    if n > 0 {
        unsafe { ccp_xor(a.as_ptr(), b.as_ptr(), out.as_mut_ptr(), n) };
    }
    n
}

/// `dst ^= src`. The reconstruction primitive — applying a raw delta to a base
/// block in place, with no scratch buffer.
pub fn xor_inplace(dst: &mut [u8], src: &[u8]) -> usize {
    let n = dst.len().min(src.len());
    if n > 0 {
        unsafe { ccp_xor_inplace(dst.as_mut_ptr(), src.as_ptr(), n) };
    }
    n
}

pub fn popcount(buf: &[u8]) -> u64 {
    if buf.is_empty() {
        return 0;
    }
    unsafe { ccp_popcount(buf.as_ptr(), buf.len()) }
}

pub fn xor_popcount(a: &[u8], b: &[u8]) -> u64 {
    let n = a.len().min(b.len());
    if n == 0 {
        return 0;
    }
    unsafe { ccp_xor_popcount(a.as_ptr(), b.as_ptr(), n) }
}

pub fn xor_nonzero_bytes(a: &[u8], b: &[u8]) -> u64 {
    let n = a.len().min(b.len());
    if n == 0 {
        return 0;
    }
    unsafe { ccp_xor_nonzero_bytes(a.as_ptr(), b.as_ptr(), n) }
}

/// All measurements for one block in a single pass over it.
pub fn measure(a: &[u8], b: &[u8]) -> BlockStats {
    let n = a.len().min(b.len());
    let mut stats = BlockStats::default();
    if n > 0 {
        unsafe { ccp_block_measure(a.as_ptr(), b.as_ptr(), n, &mut stats) };
    }
    stats
}

pub fn equal(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    if a.is_empty() {
        return true;
    }
    unsafe { ccp_equal(a.as_ptr(), b.as_ptr(), a.len()) == 1 }
}

pub fn version() -> String {
    // Safety: the engine returns a pointer to a static string literal.
    unsafe { std::ffi::CStr::from_ptr(ccp_bitexec_version()) }
        .to_string_lossy()
        .into_owned()
}

pub fn features() -> u32 {
    unsafe { ccp_bitexec_features() }
}

/// Human-readable list of the execution paths this build dispatches to. Recorded
/// in benchmark output so a throughput number is attributable to a code path.
pub fn feature_names() -> Vec<&'static str> {
    let f = features();
    let mut v = Vec::new();
    if f & 0x1 != 0 {
        v.push("scalar");
    }
    if f & 0x2 != 0 {
        v.push("avx2");
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    // The engine has its own native suite; these assert the *binding* is wired to
    // the real implementation rather than to a stub, which is the failure mode a
    // Rust-side test can catch and a C++-side test cannot.
    #[test]
    fn xor_roundtrip_through_ffi() {
        let a: Vec<u8> = (0..1000u32).map(|i| (i * 7 % 251) as u8).collect();
        let b: Vec<u8> = (0..1000u32).map(|i| (i * 13 % 241) as u8).collect();
        let mut d = vec![0u8; a.len()];
        xor(&a, &b, &mut d);
        let mut back = a.clone();
        xor_inplace(&mut back, &d);
        assert_eq!(back, b, "B == A XOR D across the FFI boundary");
    }

    #[test]
    fn measurements_agree_with_naive_rust() {
        let a: Vec<u8> = (0..4097u32).map(|i| (i % 256) as u8).collect();
        let mut b = a.clone();
        b[0] ^= 0x01;
        b[4096] ^= 0xFF;
        let stats = measure(&a, &b);
        assert_eq!(stats.changed_bytes, 2);
        assert_eq!(stats.changed_bits, 1 + 8);
        assert_eq!(stats.length, 4097);
        assert_eq!(xor_popcount(&a, &b), 9);
        assert_eq!(xor_nonzero_bytes(&a, &b), 2);
    }

    #[test]
    fn engine_is_actually_linked() {
        assert!(version().starts_with("ccp-bitexec"));
        assert!(features() & 0x1 != 0, "scalar path must always be present");
    }
}
