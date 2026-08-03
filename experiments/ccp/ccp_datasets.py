#!/usr/bin/env python3
"""Deterministic synthetic datasets for the CCP experiment.

Every generator is a pure function of (size, seed) built on a BLAKE2b hash
chain, so a benchmark run is reproducible byte-for-byte on any machine. No
`random` module is used anywhere: a physiology-style flaky result would make the
whole experiment worthless.

The set spans the space deliberately, including cases where CCP must lose:

    random_control      incompressible; the negative control
    zeros               maximally redundant; the positive control
    exact_repeats       one block repeated verbatim
    near_duplicate_*    a base block plus a few mutated bytes per region
    drifting_duplicate  near-duplicates whose mutation count grows with offset
    structured_records  log-like records; "ordinary data" comparison
    fp32_weights        simulated dense fp32 weights
    int8_weights        simulated int8 quantized weights
    sparse_weights      pruned weights: mostly zeros, occasional values
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Callable, Iterator

BLOCK_DIGEST = 64


def hash_stream(size: int, seed: int = 1) -> Iterator[bytes]:
    """Deterministic pseudo-random byte blocks."""
    counter = 0
    produced = 0
    seed_bytes = seed.to_bytes(8, "little")
    while produced < size:
        block = hashlib.blake2b(
            counter.to_bytes(8, "little") + seed_bytes, digest_size=BLOCK_DIGEST
        ).digest()
        take = min(len(block), size - produced)
        yield block[:take]
        produced += take
        counter += 1


def hash_bytes(size: int, seed: int = 1) -> bytes:
    return b"".join(hash_stream(size, seed))


# ---------------------------------------------------------------------------
# generators
# ---------------------------------------------------------------------------


def gen_random_control(path: str, size: int, seed: int = 1) -> None:
    with open(path, "wb") as f:
        for block in hash_stream(size, seed):
            f.write(block)


def gen_zeros(path: str, size: int, seed: int = 1) -> None:
    chunk = bytes(1 << 20)
    with open(path, "wb") as f:
        written = 0
        while written < size:
            take = min(len(chunk), size - written)
            f.write(chunk[:take])
            written += take


def gen_exact_repeats(path: str, size: int, seed: int = 1) -> None:
    block = hash_bytes(64 * 1024, seed)
    with open(path, "wb") as f:
        written = 0
        while written < size:
            take = min(len(block), size - written)
            f.write(block[:take])
            written += take


def _near_duplicate(
    path: str,
    size: int,
    seed: int,
    region_size: int,
    changes: Callable[[int], int],
) -> None:
    """A base region repeated with a controlled number of mutated bytes.

    `changes(region_index)` gives how many bytes to flip in that region, which
    is what decides whether a region lands on the paying or the losing side of
    the break-even point.
    """
    base = hash_bytes(region_size, seed)
    with open(path, "wb") as f:
        written = 0
        index = 0
        while written < size:
            k = max(0, changes(index))
            if k == 0:
                region = base
            else:
                mutated = bytearray(base)
                noise = hash_bytes(k * 4, seed + index + 1)
                for j in range(k):
                    pos = (
                        int.from_bytes(noise[j * 4 : j * 4 + 3], "little") % region_size
                    )
                    delta = noise[j * 4 + 3] or 1
                    mutated[pos] ^= delta
                region = bytes(mutated)
            take = min(len(region), size - written)
            f.write(region[:take])
            written += take
            index += 1


def gen_near_duplicate_4k(path: str, size: int, seed: int = 1) -> None:
    # ~1% of a 4KB region mutated: comfortably inside the break-even budget.
    _near_duplicate(path, size, seed, 4 * 1024, lambda i: 40)


def gen_near_duplicate_64k(path: str, size: int, seed: int = 1) -> None:
    _near_duplicate(path, size, seed, 64 * 1024, lambda i: 600)


def gen_drifting_duplicate(path: str, size: int, seed: int = 1) -> None:
    """Mutation count climbs with position, crossing the break-even point.

    Regions early in the file should encode as deltas and regions late in the
    file should fall back to verbatim, so a single file exercises both sides of
    the decision rule.
    """
    region = 64 * 1024
    _near_duplicate(path, size, seed, region, lambda i: min(region, 64 * (i + 1)))


def gen_structured_records(path: str, size: int, seed: int = 1) -> None:
    """Fixed-layout binary records, the shape ordinary application data takes."""
    with open(path, "wb") as f:
        written = 0
        index = 0
        while written < size:
            noise = hash_bytes(8, seed + (index >> 8))
            record = struct.pack(
                "<IIQHH8s",
                index,
                0xDEADBEEF,
                1_700_000_000_000 + index * 37,
                index % 512,
                0,
                noise,
            )
            take = min(len(record), size - written)
            f.write(record[:take])
            written += take
            index += 1


def gen_fp32_weights(path: str, size: int, seed: int = 1) -> None:
    """Dense fp32 values on a bell-shaped distribution, as weights tend to be.

    Box-Muller over the hash stream keeps this dependency-free and repeatable.
    The scale (0.02) is the order of magnitude of initialised transformer
    weights, which is what makes the exponent byte nearly constant across the
    tensor.
    """
    count = size // 4
    with open(path, "wb") as f:
        buffer = bytearray()
        stream = hash_stream(count * 8 + 64, seed)
        pool = bytearray()
        produced = 0
        while produced < count:
            while len(pool) < 8:
                try:
                    pool += next(stream)
                except StopIteration:
                    pool += hash_bytes(4096, seed + produced)
            u1 = (int.from_bytes(pool[0:4], "little") + 1) / (2**32 + 1)
            u2 = int.from_bytes(pool[4:8], "little") / (2**32)
            del pool[:8]
            value = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
            buffer += struct.pack("<f", value * 0.02)
            produced += 1
            if len(buffer) >= 1 << 20:
                f.write(buffer)
                buffer.clear()
        f.write(buffer)
        remainder = size - count * 4
        if remainder:
            f.write(bytes(remainder))


def gen_int8_weights(path: str, size: int, seed: int = 1) -> None:
    """Int8 quantized weights: a narrow band of values around zero."""
    with open(path, "wb") as f:
        written = 0
        counter = 0
        while written < size:
            block = hash_bytes(1 << 16, seed + counter)
            # Fold each byte into a narrow signed band, which is what a
            # symmetric per-tensor quantizer produces.
            folded = bytes(((b % 97) - 48) & 0xFF for b in block)
            take = min(len(folded), size - written)
            f.write(folded[:take])
            written += take
            counter += 1


def gen_sparse_weights(path: str, size: int, seed: int = 1) -> None:
    """A pruned tensor: ~5% non-zero, the rest exact zeros."""
    with open(path, "wb") as f:
        written = 0
        counter = 0
        while written < size:
            block = hash_bytes(1 << 16, seed + counter)
            out = bytearray(len(block))
            for i in range(0, len(block) - 1, 2):
                if block[i] < 13:
                    out[i] = block[i + 1] or 1
            take = min(len(out), size - written)
            f.write(bytes(out[:take]))
            written += take
            counter += 1


GENERATORS: dict[str, Callable[[str, int, int], None]] = {
    "random_control": gen_random_control,
    "zeros": gen_zeros,
    "exact_repeats": gen_exact_repeats,
    "near_duplicate_4k": gen_near_duplicate_4k,
    "near_duplicate_64k": gen_near_duplicate_64k,
    "drifting_duplicate": gen_drifting_duplicate,
    "structured_records": gen_structured_records,
    "fp32_weights": gen_fp32_weights,
    "int8_weights": gen_int8_weights,
    "sparse_weights": gen_sparse_weights,
}

DESCRIPTIONS: dict[str, str] = {
    "random_control": "incompressible hash stream (negative control)",
    "zeros": "all zero bytes (positive control)",
    "exact_repeats": "one 64KB block repeated verbatim",
    "near_duplicate_4k": "4KB base block, ~1% of bytes mutated per region",
    "near_duplicate_64k": "64KB base block, ~1% of bytes mutated per region",
    "drifting_duplicate": "near-duplicates whose mutation rate crosses break-even",
    "structured_records": "fixed-layout binary records (ordinary data)",
    "fp32_weights": "simulated dense fp32 weights, bell-shaped",
    "int8_weights": "simulated int8 quantized weights",
    "sparse_weights": "pruned weights: ~5% non-zero",
}


def build(name: str, path: str, size: int, seed: int = 1) -> None:
    if name not in GENERATORS:
        raise KeyError(f"unknown dataset {name!r}; known: {sorted(GENERATORS)}")
    GENERATORS[name](path, size, seed)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a CCP test dataset")
    parser.add_argument("name", choices=sorted(GENERATORS))
    parser.add_argument("path")
    parser.add_argument("size", help="e.g. 64MB")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    from ccp_full_experiment import format_bytes, parse_size

    size = parse_size(args.size)
    build(args.name, args.path, size, args.seed)
    print(f"wrote {args.path}: {format_bytes(size)} ({DESCRIPTIONS[args.name]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
