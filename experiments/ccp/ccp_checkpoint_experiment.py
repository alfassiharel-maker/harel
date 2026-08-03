#!/usr/bin/env python3
"""Does CCP pay off across two versions of the same model?

Storing one checkpoint is the hard case for a base-plus-delta scheme, because
nothing inside a single set of weights repeats. Storing *two related* checkpoints
is the case the idea was built for: a fine-tune, a resumed run, a merged adapter.
This script measures that directly.

A base file is paired with derived variants that stand in for different kinds of
update, and each pair is measured as a single file:

    identical        the same weights twice — the ceiling for any delta scheme
    sparse_*         a fraction of weights replaced outright, as a targeted or
                     adapter-style update touches only some tensors
    dense_low_byte   every weight nudged by one unit in the last place
    dense_two_bytes  every weight nudged slightly harder

The distinction between the sparse and dense rows is the whole finding: both can
be a "small change" by any weight-space measure while being worlds apart in how
many *bytes* move, and a byte-level delta only sees bytes.

Perturbations are byte-exact and dependency-free. Little-endian fp32 puts the
sign and exponent in the high byte and the least significant mantissa bits in
the low byte, so rewriting byte 0 of every value changes every weight by a
relative amount near 2**-23 while leaving its magnitude alone.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Sequence

import lzma

import ccp_datasets
from ccp_full_experiment import (
    format_bytes,
    parse_size,
    run_experiment,
)

FLOAT_WIDTH = 4


# ---------------------------------------------------------------------------
# variants
# ---------------------------------------------------------------------------


def perturb_dense(data: bytearray, byte_lanes: Sequence[int], seed: int) -> int:
    """Rewrite the given byte of every 4-byte value. Returns bytes changed."""
    changed = 0
    count = len(data) // FLOAT_WIDTH
    noise = ccp_datasets.hash_bytes(count + 64, seed)
    for lane in byte_lanes:
        for i in range(count):
            offset = i * FLOAT_WIDTH + lane
            value = noise[i] or 1
            if data[offset] != (data[offset] ^ value):
                changed += 1
            data[offset] ^= value
    return changed


def perturb_sparse(data: bytearray, fraction: float, seed: int) -> int:
    """Replace `fraction` of the 4-byte values outright."""
    count = len(data) // FLOAT_WIDTH
    target = int(count * fraction)
    if target <= 0:
        return 0
    stride = max(1, count // target)
    replaced = 0
    noise = ccp_datasets.hash_bytes(target * FLOAT_WIDTH + 64, seed)
    cursor = 0
    for i in range(0, count, stride):
        offset = i * FLOAT_WIDTH
        data[offset : offset + FLOAT_WIDTH] = noise[cursor : cursor + FLOAT_WIDTH]
        cursor += FLOAT_WIDTH
        replaced += 1
        if cursor + FLOAT_WIDTH > len(noise):
            break
    return replaced * FLOAT_WIDTH


@dataclass
class Variant:
    name: str
    description: str
    apply: Callable[[bytearray, int], int]


VARIANTS: list[Variant] = [
    Variant(
        "identical",
        "the same checkpoint stored twice",
        lambda data, seed: 0,
    ),
    Variant(
        "sparse_0.1pct",
        "0.1% of weights replaced outright",
        lambda data, seed: perturb_sparse(data, 0.001, seed),
    ),
    Variant(
        "sparse_1pct",
        "1% of weights replaced outright",
        lambda data, seed: perturb_sparse(data, 0.01, seed),
    ),
    Variant(
        "sparse_10pct",
        "10% of weights replaced outright",
        lambda data, seed: perturb_sparse(data, 0.10, seed),
    ),
    Variant(
        "dense_low_byte",
        "every weight nudged by ~1 ulp",
        lambda data, seed: perturb_dense(data, (0,), seed),
    ),
    Variant(
        "dense_two_bytes",
        "every weight nudged, two mantissa bytes",
        lambda data, seed: perturb_dense(data, (0, 1), seed),
    ),
]


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def _stream_size(path: str, compressor: object) -> int:
    total = 0
    with open(path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            total += len(compressor.compress(chunk))  # type: ignore[attr-defined]
    return total + len(compressor.flush())  # type: ignore[attr-defined]


def gzip_size(path: str) -> int:
    return _stream_size(path, zlib.compressobj(6, zlib.DEFLATED, 31))


# The baselines are chosen by match-window size, because that is the only thing
# that decides whether a compressor can see the second copy of a checkpoint at
# all. gzip's window is 32KB regardless of level; LZMA's dictionary is a tunable
# and is what makes it a fair opponent here rather than a straw man.
BASELINES: list[tuple[str, str, Callable[[], object]]] = [
    (
        "gzip-6",
        "32KB window",
        lambda: zlib.compressobj(6, zlib.DEFLATED, 31),
    ),
    (
        "lzma-6",
        "8MB dictionary",
        lambda: lzma.LZMACompressor(
            format=lzma.FORMAT_XZ,
            filters=[{"id": lzma.FILTER_LZMA2, "preset": 6}],
        ),
    ),
    (
        "lzma-long",
        "512MB dictionary",
        lambda: lzma.LZMACompressor(
            format=lzma.FORMAT_XZ,
            filters=[
                {
                    "id": lzma.FILTER_LZMA2,
                    "preset": 6,
                    "dict_size": 512 * 1024 * 1024,
                }
            ],
        ),
    ),
]


@dataclass
class PairResult:
    variant: str
    description: str
    pair_bytes: int
    bytes_changed: int
    ccp_bytes: int
    ccp_saving_percent: float
    ccp_region_size: int
    regions_ccp: int
    regions_total: int
    reconstruction: str
    encode_seconds: float
    decode_seconds: float
    baselines: dict[str, int] = field(default_factory=dict)
    baseline_seconds: dict[str, float] = field(default_factory=dict)

    def baseline_saving(self, name: str) -> float | None:
        value = self.baselines.get(name)
        if value is None or not self.pair_bytes:
            return None
        return (1.0 - value / self.pair_bytes) * 100.0


def run_pair(
    base: bytes,
    variant: Variant,
    region_sizes: Sequence[int],
    workdir: str,
    seed: int,
) -> PairResult:
    mutated = bytearray(base)
    changed = variant.apply(mutated, seed)

    pair_path = os.path.join(workdir, f"pair_{variant.name}.bin")
    with open(pair_path, "wb") as f:
        f.write(base)
        f.write(mutated)
    pair_bytes = os.path.getsize(pair_path)

    experiment = run_experiment(
        pair_path,
        f"{variant.name} pair",
        region_sizes,
        workdir,
        verbose=False,
    )
    best = experiment.best

    baselines: dict[str, int] = {}
    baseline_seconds: dict[str, float] = {}
    for name, _window, factory in BASELINES:
        started = time.monotonic()
        baselines[name] = _stream_size(pair_path, factory())
        baseline_seconds[name] = time.monotonic() - started
    os.unlink(pair_path)

    if best is None:
        raise RuntimeError(f"variant {variant.name}: no verified round-trip")

    return PairResult(
        variant=variant.name,
        description=variant.description,
        pair_bytes=pair_bytes,
        bytes_changed=changed,
        ccp_bytes=best.ccp_bytes,
        ccp_saving_percent=best.saving_percent,
        ccp_region_size=best.region_size,
        regions_ccp=best.regions_ccp,
        regions_total=best.regions_total,
        reconstruction=best.reconstruction,
        encode_seconds=best.encode_seconds,
        decode_seconds=best.decode_seconds,
        baselines=baselines,
        baseline_seconds=baseline_seconds,
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def render(
    source_label: str,
    slice_bytes: int,
    results: list[PairResult],
    elapsed: float,
) -> str:
    rule = "=" * 96
    thin = "-" * 96
    lines = [rule, "CCP ACROSS TWO MODEL VERSIONS", rule, ""]
    lines.append(f"Base weights      {source_label}")
    lines.append(f"Slice measured    {format_bytes(slice_bytes)} per checkpoint")
    lines.append(f"Pair size         {format_bytes(slice_bytes * 2)}")
    lines.append(f"Wall time         {elapsed:.1f}s")
    lines.append("")
    lines.append("Each row stores two checkpoints as one file. A scheme that stored")
    lines.append("one copy and a free delta would reach 50%; that is the ceiling.")
    lines.append("")

    head = (
        f"{'Variant':<18} {'Bytes changed':>15} {'CCP':>8} "
        f"{'Region':>9} {'Delta regions':>15} {'Verify':>7}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        share = 100.0 * r.bytes_changed / (r.pair_bytes // 2) if r.pair_bytes else 0.0
        lines.append(
            f"{r.variant:<18} "
            f"{r.bytes_changed:>11} {share:>3.0f}% "
            f"{r.ccp_saving_percent:>7.2f}% "
            f"{format_bytes(r.ccp_region_size):>9} "
            f"{r.regions_ccp:>7}/{r.regions_total:<7} "
            f"{r.reconstruction:>7}"
        )
    lines.append("")

    lines.append(thin)
    lines.append("AGAINST GENERAL-PURPOSE COMPRESSION")
    lines.append(thin)
    lines.append("")
    lines.append("A compressor can only exploit the second copy if its match window")
    lines.append("reaches back that far. The window is stated for each baseline.")
    lines.append("")
    head = f"{'Variant':<18} {'CCP':>9}"
    for name, window, _factory in BASELINES:
        head += f" {name + ' (' + window + ')':>26}"
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        row = f"{r.variant:<18} {r.ccp_saving_percent:>8.2f}%"
        for name, _window, _factory in BASELINES:
            saving = r.baseline_saving(name)
            row += f" {'-':>26}" if saving is None else f" {saving:>25.2f}%"
        lines.append(row)
    lines.append("")

    lines.append(thin)
    lines.append("TIMING")
    lines.append(thin)
    lines.append("")
    head = f"{'Variant':<18} {'Encode':>10} {'Decode':>10}"
    for name, _window, _factory in BASELINES:
        head += f" {name:>12}"
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        row = f"{r.variant:<18} {r.encode_seconds:>9.2f}s {r.decode_seconds:>9.2f}s"
        for name, _window, _factory in BASELINES:
            seconds = r.baseline_seconds.get(name)
            row += f" {'-':>12}" if seconds is None else f" {seconds:>11.2f}s"
        lines.append(row)
    lines.append("")

    lines.append(thin)
    lines.append("READING")
    lines.append(thin)
    lines.append("")
    sparse = [r for r in results if r.variant.startswith("sparse")]
    dense = [r for r in results if r.variant.startswith("dense")]
    identical = next((r for r in results if r.variant == "identical"), None)
    if identical:
        lines.append(
            f"  Two identical checkpoints:      {identical.ccp_saving_percent:.2f}% "
            f"(ceiling is 50%)"
        )
    for r in sparse:
        lines.append(
            f"  {r.description:<30} {r.ccp_saving_percent:>7.2f}%  "
            f"({r.bytes_changed} bytes moved)"
        )
    for r in dense:
        lines.append(
            f"  {r.description:<30} {r.ccp_saving_percent:>7.2f}%  "
            f"({r.bytes_changed} bytes moved)"
        )
    lines.append("")
    long_window = BASELINES[-1][0]
    if identical:
        rival = identical.baseline_saving(long_window)
        if rival is not None:
            lines.append("")
            lines.append(
                f"  On identical checkpoints a {BASELINES[-1][1]} compressor reaches "
                f"{rival:.2f}%,"
            )
            lines.append(
                f"  against CCP's {identical.ccp_saving_percent:.2f}%. Cross-copy"
            )
            lines.append(
                "  redundancy is not exclusive to this method — what differs is the"
            )
            lines.append("  cost of getting at it. See the timing table.")
    lines.append("")
    if sparse and dense:
        best_sparse = max(sparse, key=lambda r: r.ccp_saving_percent)
        worst_dense = min(dense, key=lambda r: r.ccp_saving_percent)
        lines.append(
            f"  A sparse update keeps {best_sparse.ccp_saving_percent:.2f}% while a"
        )
        lines.append(
            f"  dense one-unit-in-the-last-place update keeps "
            f"{worst_dense.ccp_saving_percent:.2f}%, even though the dense"
        )
        lines.append(
            "  update moves every weight by a smaller amount than the sparse one."
        )
        lines.append("  What matters to the method is how many bytes move, not how far.")
    lines.append("")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure CCP across two versions of one model"
    )
    parser.add_argument("model", help="a model weight file to use as the base")
    parser.add_argument(
        "--slice",
        default="128MB",
        help="how much of the file to use per checkpoint (default 128MB)",
    )
    parser.add_argument(
        "--offset",
        default="0",
        help="skip this many bytes of the file first, to land past a header",
    )
    parser.add_argument(
        "--sizes",
        default="4KB,64KB,1MB",
        help="region sizes to sweep (default 4KB,64KB,1MB)",
    )
    parser.add_argument("--output", default="ccp_checkpoint_report.txt")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.model):
        print(f"error: not a file: {args.model}", file=sys.stderr)
        return 1

    slice_bytes = parse_size(args.slice)
    offset = parse_size(args.offset) if args.offset != "0" else 0
    region_sizes = [parse_size(s) for s in args.sizes.split(",")]

    with open(args.model, "rb") as f:
        f.seek(offset)
        base = f.read(slice_bytes)
    if len(base) < FLOAT_WIDTH:
        print("error: slice is too small to interpret as weights", file=sys.stderr)
        return 1
    base = base[: len(base) - (len(base) % FLOAT_WIDTH)]

    label = (
        f"{os.path.abspath(args.model)} "
        f"(offset {offset}, sha256 {hashlib.sha256(base).hexdigest()[:16]}...)"
    )
    print(f"Base: {label}")
    print(f"Slice: {format_bytes(len(base))} per checkpoint\n")

    started = time.monotonic()
    results: list[PairResult] = []
    with tempfile.TemporaryDirectory(prefix="ccp_ckpt_") as workdir:
        for variant in VARIANTS:
            print(f"  {variant.name} ({variant.description}) ...", flush=True)
            result = run_pair(base, variant, region_sizes, workdir, args.seed)
            results.append(result)
            summary = "  ".join(
                f"{name} {result.baseline_saving(name):.2f}%"
                for name, _window, _factory in BASELINES
                if result.baseline_saving(name) is not None
            )
            print(
                f"    CCP {result.ccp_saving_percent:.2f}%  {summary}  "
                f"verify={result.reconstruction}",
                flush=True,
            )
    elapsed = time.monotonic() - started

    report = render(label, len(base), results, elapsed)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport: {args.output}")

    if any(r.reconstruction != "PASS" for r in results):
        print("FATAL: a round-trip failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
