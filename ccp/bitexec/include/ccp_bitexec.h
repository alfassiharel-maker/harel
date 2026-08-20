/* CCP bit execution engine — C ABI.
 *
 * This layer receives buffers and returns results. It performs no I/O, knows
 * nothing about storage or the CCP format, and makes no policy decision — those
 * belong to the Rust data engine and the Julia strategy engine respectively.
 * Keeping it free of dependencies is what makes it testable in isolation and
 * replaceable by a faster implementation later.
 *
 * Every function requires that the caller has already validated its pointers and
 * lengths: a length of 0 is legal and does nothing, but a null pointer with a
 * non-zero length is undefined. The Rust side upholds this at the FFI boundary.
 */
#ifndef CCP_BITEXEC_H
#define CCP_BITEXEC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Feature bits reported by ccp_bitexec_features(), so a caller (and a benchmark)
 * can record which execution path actually ran rather than which one was
 * compiled in. */
#define CCP_FEATURE_SCALAR 0x1u
#define CCP_FEATURE_AVX2   0x2u

/* One pass over a block, producing every measurement the strategy engine needs.
 * Computing these together rather than in three passes matters: the block is
 * sized to stay in cache, so a single traversal reads it once. */
typedef struct CcpBlockStats {
    uint64_t changed_bits;  /* popcount(a XOR b) */
    uint64_t changed_bytes; /* number of byte positions where a and b differ */
    uint64_t length;        /* bytes compared */
} CcpBlockStats;

/* out = a XOR b. `out` may alias `a` or `b`. */
void ccp_xor(const uint8_t *a, const uint8_t *b, uint8_t *out, size_t n);

/* dst ^= src. This is the reconstruction primitive: applying a raw delta to a
 * base block is the same operation that produced the delta (XOR is self-inverse),
 * which is the property the whole design rests on. */
void ccp_xor_inplace(uint8_t *dst, const uint8_t *src, size_t n);

/* Number of set bits in the buffer. */
uint64_t ccp_popcount(const uint8_t *p, size_t n);

/* popcount(a XOR b) without materialising the XOR — the cheapest possible
 * "how different are these two blocks". */
uint64_t ccp_xor_popcount(const uint8_t *a, const uint8_t *b, size_t n);

/* Count of differing byte positions. Distinct from changed_bits because the
 * sparse encoding costs per differing *byte*, not per differing bit. */
uint64_t ccp_xor_nonzero_bytes(const uint8_t *a, const uint8_t *b, size_t n);

/* All block measurements in a single pass. */
void ccp_block_measure(const uint8_t *a, const uint8_t *b, size_t n, CcpBlockStats *out);

/* 1 if the buffers are equal, 0 otherwise. Not constant-time; this is a storage
 * engine, not a cryptographic comparison. */
int ccp_equal(const uint8_t *a, const uint8_t *b, size_t n);

/* Bitwise helpers used by masking and higher-level representation work. */
void ccp_and(const uint8_t *a, const uint8_t *b, uint8_t *out, size_t n);
void ccp_or(const uint8_t *a, const uint8_t *b, uint8_t *out, size_t n);
void ccp_not(const uint8_t *a, uint8_t *out, size_t n);

/* Single-bit access, bit 0 being the least significant bit of byte 0. */
int  ccp_bit_test(const uint8_t *p, uint64_t bit);
void ccp_bit_set(uint8_t *p, uint64_t bit);
void ccp_bit_clear(uint8_t *p, uint64_t bit);

const char *ccp_bitexec_version(void);

/* Which execution paths this build will actually dispatch to at runtime. */
uint32_t ccp_bitexec_features(void);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CCP_BITEXEC_H */
