"""Measure what L1 actually does, so the README quotes a run and not a guess.

    python3 bench_l1.py                 # default 16MB, 64KB blocks
    python3 bench_l1.py --size 64MB --block 4KB

Every row round-trips and is compared byte-for-byte against the input before its
numbers are printed. A row that cannot be reconstructed is not reported, it is
an error.

Two change shapes are measured because they bound the engine's behaviour:

    clustered   one contiguous edit per block — what a real patch looks like
    scattered   changes spread as far apart as possible — the adversarial case
                for any run-based coding, constructed to be the worst input

The `sparse-only` column is what a single-codec delta of the kind in
`experiments/ccp` would have stored, computed from the same blocks. It is the
comparison the codec set exists to beat.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections.abc import Sequence

import l1

UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def parse_size(text: str) -> int:
    upper = text.strip().upper()
    for suffix, scale in sorted(UNITS.items(), key=lambda kv: -len(kv[0])):
        if upper.endswith(suffix):
            return int(float(upper[: -len(suffix)]) * scale)
    return int(upper)


def keystream(n: int, tag: bytes) -> bytes:
    """Deterministic incompressible bytes. No RNG, identical on every platform."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def build_target(base: bytes, block: int, fraction: float, clustered: bool) -> bytes:
    target = bytearray(base)
    changed = int(block * fraction)
    for offset in range(0, len(base), block):
        if clustered:
            target[offset : offset + changed] = keystream(changed, b"p" + offset.to_bytes(8, "big"))
        else:
            # Stride 7 is coprime with every power-of-two block size, so the
            # changes land as far from each other as the block allows.
            for i in range(changed):
                target[offset + (i * 7) % block] ^= 0xFF
    return bytes(target)


def measure(base: bytes, target: bytes, block: int) -> dict[str, object]:
    bases = [base[o : o + block] for o in range(0, len(base), block)]
    targets = [target[o : o + block] for o in range(0, len(target), block)]

    accumulator = l1.SavingsAccumulator()
    sparse_only = 0
    for b, t in zip(bases, targets, strict=True):
        plan = l1.plan_block(b, t)
        accumulator.add(plan)
        sparse_only += min(plan.costs[l1.CODEC_SPARSE], plan.costs[l1.CODEC_RAW])

    started = time.perf_counter()
    encoded = [l1.encode_block(b, t) for b, t in zip(bases, targets, strict=True)]
    encode_seconds = time.perf_counter() - started

    started = time.perf_counter()
    decoded = [l1.decode_block(b, e) for b, e in zip(bases, encoded, strict=True)]
    decode_seconds = time.perf_counter() - started

    if hashlib.sha256(b"".join(decoded)).digest() != hashlib.sha256(target).digest():
        raise SystemExit("FATAL: round trip did not reproduce the input")

    total = len(base)
    result = accumulator.result()
    assert result.saving_pct is not None
    dominant = max(accumulator.by_codec, key=lambda c: accumulator.by_codec[c])
    return {
        "codec": l1.CODEC_NAMES[dominant],
        "saving": result.saving_pct,
        "sparse_only": (total - sparse_only) / total * 100.0,
        "encode": total / 1e6 / encode_seconds,
        "decode": total / 1e6 / decode_seconds,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="16MB", help="total bytes to process")
    parser.add_argument("--block", default="64KB", help="block size, must be <= 4MB")
    args = parser.parse_args(argv)

    size = parse_size(args.size)
    block = parse_size(args.block)
    if block > l1.MAX_BLOCK_BYTES:
        parser.error(f"block exceeds MAX_BLOCK_BYTES ({l1.MAX_BLOCK_BYTES})")
    if size % block:
        parser.error("size must be a whole number of blocks")

    base = keystream(size, b"bitengine-base")

    print(f"BitEngine L1 — {size / 1024**2:.0f}MB in {block // 1024}KB blocks, every row verified\n")
    print(f"{'shape':>10} {'changed':>8} {'codec':>8} {'saving':>9} {'sparse-only':>12} {'encode':>11} {'decode':>11}")
    print("-" * 76)

    for clustered in (True, False):
        for fraction in (0.001, 0.01, 0.10, 0.25, 0.40, 0.70, 0.95):
            target = build_target(base, block, fraction, clustered)
            row = measure(base, target, block)
            print(
                f"{'clustered' if clustered else 'scattered':>10} "
                f"{fraction * 100:>7.1f}% {row['codec']:>8} "
                f"{row['saving']:>8.2f}% {row['sparse_only']:>11.2f}% "
                f"{row['encode']:>8.0f} MB/s {row['decode']:>8.0f} MB/s"
            )
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
