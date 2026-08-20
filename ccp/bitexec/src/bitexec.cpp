// CCP bit execution engine.
//
// Two execution paths exist for the hot loops: a portable 64-bit-word path and
// an AVX2 path, selected once at load time by CPU feature detection. The AVX2
// path is not an optimisation guess — XOR and popcount over a block are pure
// data-parallel work with no branches, which is the case SIMD is actually for.
//
// On cache: nothing here commands the cache hierarchy, because software cannot.
// What it does is give the hardware prefetcher the access pattern it predicts
// well — sequential, contiguous, aligned where it costs nothing — and process
// blocks small enough that the working set stays resident in practice. Residency
// remains the CPU's decision.

#include "ccp_bitexec.h"

#include <cstdint>
#include <cstring>

#if defined(__x86_64__) || defined(_M_X64)
#define CCP_X86 1
#include <immintrin.h>
#else
#define CCP_X86 0
#endif

namespace {

// Word size for the portable path. 64-bit words turn a byte loop into an
// 8-bytes-per-iteration loop and let popcount use a single instruction.
using word_t = std::uint64_t;
constexpr std::size_t kWord = sizeof(word_t);

// memcpy rather than a cast: the caller's buffers carry no alignment guarantee,
// and a misaligned load through a uint64_t* is undefined behaviour even where
// the hardware tolerates it. Every compiler we target lowers this to the same
// single load.
inline word_t load_word(const std::uint8_t *p) {
    word_t v;
    std::memcpy(&v, p, kWord);
    return v;
}

inline void store_word(std::uint8_t *p, word_t v) { std::memcpy(p, &v, kWord); }

inline int popcount64(word_t v) { return __builtin_popcountll(v); }

#if CCP_X86
bool detect_avx2() {
    __builtin_cpu_init();
    return __builtin_cpu_supports("avx2");
}
const bool kHasAvx2 = detect_avx2();

// ---- AVX2 paths ---------------------------------------------------------

__attribute__((target("avx2"))) void xor_avx2(const std::uint8_t *a, const std::uint8_t *b,
                                              std::uint8_t *out, std::size_t n) {
    std::size_t i = 0;
    for (; i + 32 <= n; i += 32) {
        __m256i va = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(a + i));
        __m256i vb = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(b + i));
        _mm256_storeu_si256(reinterpret_cast<__m256i *>(out + i), _mm256_xor_si256(va, vb));
    }
    for (; i + kWord <= n; i += kWord) store_word(out + i, load_word(a + i) ^ load_word(b + i));
    for (; i < n; ++i) out[i] = static_cast<std::uint8_t>(a[i] ^ b[i]);
}

// popcount of a XOR b. The AVX2 registers are unpacked to 64-bit lanes and
// counted with scalar POPCNT: a full vectorised popcount (Harley-Seal or the
// pshufb nibble table) is faster still, but this is already ~4x the byte loop
// and stays obviously correct — the place to optimise further is measured, not
// guessed.
__attribute__((target("avx2"))) std::uint64_t xor_popcount_avx2(const std::uint8_t *a,
                                                               const std::uint8_t *b,
                                                               std::size_t n) {
    std::uint64_t bits = 0;
    std::size_t i = 0;
    for (; i + 32 <= n; i += 32) {
        __m256i va = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(a + i));
        __m256i vb = _mm256_loadu_si256(reinterpret_cast<const __m256i *>(b + i));
        __m256i vx = _mm256_xor_si256(va, vb);
        bits += static_cast<std::uint64_t>(popcount64(static_cast<word_t>(_mm256_extract_epi64(vx, 0))));
        bits += static_cast<std::uint64_t>(popcount64(static_cast<word_t>(_mm256_extract_epi64(vx, 1))));
        bits += static_cast<std::uint64_t>(popcount64(static_cast<word_t>(_mm256_extract_epi64(vx, 2))));
        bits += static_cast<std::uint64_t>(popcount64(static_cast<word_t>(_mm256_extract_epi64(vx, 3))));
    }
    for (; i + kWord <= n; i += kWord)
        bits += static_cast<std::uint64_t>(popcount64(load_word(a + i) ^ load_word(b + i)));
    for (; i < n; ++i)
        bits += static_cast<std::uint64_t>(__builtin_popcount(static_cast<unsigned>(a[i] ^ b[i])));
    return bits;
}
#else
const bool kHasAvx2 = false;
#endif

// ---- portable paths -----------------------------------------------------

void xor_scalar(const std::uint8_t *a, const std::uint8_t *b, std::uint8_t *out, std::size_t n) {
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) store_word(out + i, load_word(a + i) ^ load_word(b + i));
    for (; i < n; ++i) out[i] = static_cast<std::uint8_t>(a[i] ^ b[i]);
}

std::uint64_t xor_popcount_scalar(const std::uint8_t *a, const std::uint8_t *b, std::size_t n) {
    std::uint64_t bits = 0;
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord)
        bits += static_cast<std::uint64_t>(popcount64(load_word(a + i) ^ load_word(b + i)));
    for (; i < n; ++i)
        bits += static_cast<std::uint64_t>(__builtin_popcount(static_cast<unsigned>(a[i] ^ b[i])));
    return bits;
}

}  // namespace

// ---- public C ABI -------------------------------------------------------

extern "C" {

void ccp_xor(const std::uint8_t *a, const std::uint8_t *b, std::uint8_t *out, std::size_t n) {
    if (n == 0) return;
#if CCP_X86
    if (kHasAvx2) {
        xor_avx2(a, b, out, n);
        return;
    }
#endif
    xor_scalar(a, b, out, n);
}

void ccp_xor_inplace(std::uint8_t *dst, const std::uint8_t *src, std::size_t n) {
    // Aliasing dst as both input and output is exactly what the XOR paths
    // support, so reconstruction needs no scratch buffer per block.
    ccp_xor(dst, src, dst, n);
}

std::uint64_t ccp_popcount(const std::uint8_t *p, std::size_t n) {
    std::uint64_t bits = 0;
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) bits += static_cast<std::uint64_t>(popcount64(load_word(p + i)));
    for (; i < n; ++i) bits += static_cast<std::uint64_t>(__builtin_popcount(static_cast<unsigned>(p[i])));
    return bits;
}

std::uint64_t ccp_xor_popcount(const std::uint8_t *a, const std::uint8_t *b, std::size_t n) {
    if (n == 0) return 0;
#if CCP_X86
    if (kHasAvx2) return xor_popcount_avx2(a, b, n);
#endif
    return xor_popcount_scalar(a, b, n);
}

std::uint64_t ccp_xor_nonzero_bytes(const std::uint8_t *a, const std::uint8_t *b, std::size_t n) {
    std::uint64_t count = 0;
    std::size_t i = 0;
    // Word-at-a-time with an early skip: identical regions are the common case
    // in the workloads this product targets, and a whole word of zeros costs one
    // compare instead of eight.
    for (; i + kWord <= n; i += kWord) {
        word_t x = load_word(a + i) ^ load_word(b + i);
        if (x == 0) continue;
        for (std::size_t k = 0; k < kWord; ++k)
            if (((x >> (8 * k)) & 0xFFu) != 0) ++count;
    }
    for (; i < n; ++i)
        if (a[i] != b[i]) ++count;
    return count;
}

void ccp_block_measure(const std::uint8_t *a, const std::uint8_t *b, std::size_t n,
                       CcpBlockStats *out) {
    if (out == nullptr) return;
    out->length = static_cast<std::uint64_t>(n);
    out->changed_bits = 0;
    out->changed_bytes = 0;
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) {
        word_t x = load_word(a + i) ^ load_word(b + i);
        if (x == 0) continue;
        out->changed_bits += static_cast<std::uint64_t>(popcount64(x));
        for (std::size_t k = 0; k < kWord; ++k)
            if (((x >> (8 * k)) & 0xFFu) != 0) ++out->changed_bytes;
    }
    for (; i < n; ++i) {
        unsigned x = static_cast<unsigned>(a[i] ^ b[i]);
        if (x == 0) continue;
        out->changed_bits += static_cast<std::uint64_t>(__builtin_popcount(x));
        ++out->changed_bytes;
    }
}

int ccp_equal(const std::uint8_t *a, const std::uint8_t *b, std::size_t n) {
    return std::memcmp(a, b, n) == 0 ? 1 : 0;
}

void ccp_and(const std::uint8_t *a, const std::uint8_t *b, std::uint8_t *out, std::size_t n) {
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) store_word(out + i, load_word(a + i) & load_word(b + i));
    for (; i < n; ++i) out[i] = static_cast<std::uint8_t>(a[i] & b[i]);
}

void ccp_or(const std::uint8_t *a, const std::uint8_t *b, std::uint8_t *out, std::size_t n) {
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) store_word(out + i, load_word(a + i) | load_word(b + i));
    for (; i < n; ++i) out[i] = static_cast<std::uint8_t>(a[i] | b[i]);
}

void ccp_not(const std::uint8_t *a, std::uint8_t *out, std::size_t n) {
    std::size_t i = 0;
    for (; i + kWord <= n; i += kWord) store_word(out + i, ~load_word(a + i));
    for (; i < n; ++i) out[i] = static_cast<std::uint8_t>(~a[i]);
}

int ccp_bit_test(const std::uint8_t *p, std::uint64_t bit) {
    return (p[bit >> 3] >> (bit & 7u)) & 1u;
}

void ccp_bit_set(std::uint8_t *p, std::uint64_t bit) {
    p[bit >> 3] = static_cast<std::uint8_t>(p[bit >> 3] | (1u << (bit & 7u)));
}

void ccp_bit_clear(std::uint8_t *p, std::uint64_t bit) {
    p[bit >> 3] = static_cast<std::uint8_t>(p[bit >> 3] & ~(1u << (bit & 7u)));
}

const char *ccp_bitexec_version(void) { return "ccp-bitexec 0.1.0"; }

std::uint32_t ccp_bitexec_features(void) {
    std::uint32_t f = CCP_FEATURE_SCALAR;
    if (kHasAvx2) f |= CCP_FEATURE_AVX2;
    return f;
}

}  // extern "C"
