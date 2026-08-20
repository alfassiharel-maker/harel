// Native tests for the bit execution engine.
//
// The engine is the layer with two execution paths (scalar and AVX2), so the
// tests deliberately use lengths that straddle every boundary: under a word,
// exactly a word, under a vector, exactly a vector, and a vector plus a
// remainder. A SIMD tail bug produces silent data corruption, which is the worst
// failure mode this project has.

#include "ccp_bitexec.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

int g_failures = 0;

void check(bool ok, const char *what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++g_failures;
    }
}

// Deterministic pseudo-random fill. A fixed generator rather than rand() so a
// failure is reproducible from the test name alone.
std::uint64_t g_state = 0x243F6A8885A308D3ull;
std::uint8_t next_byte() {
    g_state ^= g_state << 13;
    g_state ^= g_state >> 7;
    g_state ^= g_state << 17;
    return static_cast<std::uint8_t>(g_state >> 24);
}

// Reference implementations, written the obvious slow way. The engine must agree
// with these for every length; that agreement is the whole point of the suite.
std::uint64_t ref_xor_popcount(const std::vector<std::uint8_t> &a, const std::vector<std::uint8_t> &b) {
    std::uint64_t bits = 0;
    for (std::size_t i = 0; i < a.size(); ++i)
        for (int k = 0; k < 8; ++k)
            if (((a[i] ^ b[i]) >> k) & 1) ++bits;
    return bits;
}

std::uint64_t ref_nonzero_bytes(const std::vector<std::uint8_t> &a, const std::vector<std::uint8_t> &b) {
    std::uint64_t n = 0;
    for (std::size_t i = 0; i < a.size(); ++i)
        if (a[i] != b[i]) ++n;
    return n;
}

const std::size_t kLengths[] = {0, 1, 2, 7, 8, 9, 15, 16, 31, 32, 33, 63, 64, 65,
                                127, 128, 129, 255, 256, 257, 4095, 4096, 4097, 65537};

void test_xor_roundtrip_and_measurements() {
    for (std::size_t n : kLengths) {
        std::vector<std::uint8_t> a(n), b(n), d(n), back(n);
        for (std::size_t i = 0; i < n; ++i) {
            a[i] = next_byte();
            b[i] = next_byte();
        }
        ccp_xor(a.data(), b.data(), d.data(), n);

        // The core invariant: B = A XOR D. Checked byte by byte, at every length.
        std::memcpy(back.data(), a.data(), n);
        ccp_xor_inplace(back.data(), d.data(), n);
        check(std::memcmp(back.data(), b.data(), n) == 0, "B == A XOR D");

        // XOR is self-inverse in the other direction too.
        std::vector<std::uint8_t> back_a(n);
        std::memcpy(back_a.data(), b.data(), n);
        ccp_xor_inplace(back_a.data(), d.data(), n);
        check(std::memcmp(back_a.data(), a.data(), n) == 0, "A == B XOR D");

        check(ccp_xor_popcount(a.data(), b.data(), n) == ref_xor_popcount(a, b), "xor_popcount matches reference");
        check(ccp_popcount(d.data(), n) == ref_xor_popcount(a, b), "popcount(D) == changed bits");
        check(ccp_xor_nonzero_bytes(a.data(), b.data(), n) == ref_nonzero_bytes(a, b), "nonzero bytes matches reference");

        CcpBlockStats st;
        ccp_block_measure(a.data(), b.data(), n, &st);
        check(st.changed_bits == ref_xor_popcount(a, b), "block_measure bits");
        check(st.changed_bytes == ref_nonzero_bytes(a, b), "block_measure bytes");
        check(st.length == n, "block_measure length");
    }
}

void test_identical_buffers() {
    for (std::size_t n : kLengths) {
        std::vector<std::uint8_t> a(n);
        for (std::size_t i = 0; i < n; ++i) a[i] = next_byte();
        std::vector<std::uint8_t> b = a;
        check(ccp_xor_popcount(a.data(), b.data(), n) == 0, "identical buffers: zero changed bits");
        check(ccp_xor_nonzero_bytes(a.data(), b.data(), n) == 0, "identical buffers: zero changed bytes");
        check(ccp_equal(a.data(), b.data(), n) == 1, "identical buffers compare equal");
    }
}

void test_single_bit_change() {
    // The sparsest possible non-trivial delta, and the case the sparse encoding
    // exists for: one flipped bit must report exactly one changed bit and one
    // changed byte regardless of where it lands.
    const std::size_t n = 4096;
    for (std::uint64_t bit : {0ull, 1ull, 7ull, 8ull, 63ull, 64ull, 255ull, 256ull, 32767ull}) {
        std::vector<std::uint8_t> a(n, 0), b(n, 0);
        ccp_bit_set(b.data(), bit);
        check(ccp_bit_test(b.data(), bit) == 1, "bit_set then bit_test");
        check(ccp_xor_popcount(a.data(), b.data(), n) == 1, "one flipped bit -> one changed bit");
        check(ccp_xor_nonzero_bytes(a.data(), b.data(), n) == 1, "one flipped bit -> one changed byte");
        ccp_bit_clear(b.data(), bit);
        check(ccp_bit_test(b.data(), bit) == 0, "bit_clear");
        check(ccp_equal(a.data(), b.data(), n) == 1, "cleared bit restores equality");
    }
}

void test_all_bits_differ() {
    // The opposite extreme, and the case that must choose FULL: every bit differs.
    for (std::size_t n : kLengths) {
        std::vector<std::uint8_t> a(n, 0x00), b(n, 0xFF);
        check(ccp_xor_popcount(a.data(), b.data(), n) == static_cast<std::uint64_t>(n) * 8, "all bits differ");
        check(ccp_xor_nonzero_bytes(a.data(), b.data(), n) == n, "all bytes differ");
    }
}

void test_bitwise_helpers() {
    const std::size_t n = 1000;
    std::vector<std::uint8_t> a(n), b(n), out(n);
    for (std::size_t i = 0; i < n; ++i) {
        a[i] = next_byte();
        b[i] = next_byte();
    }
    ccp_and(a.data(), b.data(), out.data(), n);
    for (std::size_t i = 0; i < n; ++i) check(out[i] == (a[i] & b[i]), "and");
    ccp_or(a.data(), b.data(), out.data(), n);
    for (std::size_t i = 0; i < n; ++i) check(out[i] == (a[i] | b[i]), "or");
    ccp_not(a.data(), out.data(), n);
    for (std::size_t i = 0; i < n; ++i) check(out[i] == static_cast<std::uint8_t>(~a[i]), "not");
}

void test_aliasing() {
    // Reconstruction relies on out == dst aliasing an input; if the SIMD path got
    // this wrong it would corrupt every delta application.
    const std::size_t n = 8192;
    std::vector<std::uint8_t> a(n), b(n);
    for (std::size_t i = 0; i < n; ++i) {
        a[i] = next_byte();
        b[i] = next_byte();
    }
    std::vector<std::uint8_t> expected(n);
    for (std::size_t i = 0; i < n; ++i) expected[i] = static_cast<std::uint8_t>(a[i] ^ b[i]);
    std::vector<std::uint8_t> aliased = a;
    ccp_xor(aliased.data(), b.data(), aliased.data(), n);
    check(std::memcmp(aliased.data(), expected.data(), n) == 0, "out aliasing a");
}

}  // namespace

int main() {
    std::printf("%s, features=0x%x\n", ccp_bitexec_version(), ccp_bitexec_features());
    test_xor_roundtrip_and_measurements();
    test_identical_buffers();
    test_single_bit_change();
    test_all_bits_differ();
    test_bitwise_helpers();
    test_aliasing();
    if (g_failures == 0) {
        std::printf("bitexec: all tests passed\n");
        return 0;
    }
    std::fprintf(stderr, "bitexec: %d failures\n", g_failures);
    return 1;
}
