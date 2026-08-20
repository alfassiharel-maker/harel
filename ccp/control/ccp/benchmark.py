"""Benchmarking.

The product's claim is quantitative, so it has to be measured against the
baselines that could actually beat it — not only against "storing full copies",
which is the easy comparison.

Three baselines are reported for a version series:

1. **Full copies** — the naive versioning system CCP is meant to improve on.
2. **Independent standard compression** — each version compressed on its own with
   the standard library's zlib. This is the baseline that most often defeats a
   naive differential scheme, because a generic compressor already exploits
   internal redundancy, and it is the honest bar to clear.
3. **Concatenated compression** — every version compressed together as one stream,
   which lets the compressor find cross-version redundancy within its window. It
   is not a practical versioning system (no random access to one version), but it
   bounds what a compressor could in principle recover.

Also reported is the sparsity measure that decides whether the whole approach can
work on a given workload: changed bits over total bits. No claim is derived here
beyond what the numbers show for the artifacts actually measured.
"""

from __future__ import annotations

import time
import zlib
from pathlib import Path
from typing import Any

from .engine import Engine

# Big enough to keep zlib fed, small enough that peak memory stays unrelated to
# artifact size — the same discipline the engine follows.
CHUNK = 1024 * 1024


def compressed_size(paths: list[Path], together: bool) -> int:
    """Compressed size of the given files, streamed.

    `together` compresses them as a single stream; otherwise each is compressed
    independently and the sizes summed.
    """
    if together:
        c = zlib.compressobj(level=6)
        total = 0
        for p in paths:
            with p.open("rb") as f:
                while chunk := f.read(CHUNK):
                    total += len(c.compress(chunk))
        return total + len(c.flush())

    total = 0
    for p in paths:
        c = zlib.compressobj(level=6)
        with p.open("rb") as f:
            while chunk := f.read(CHUNK):
                total += len(c.compress(chunk))
        total += len(c.flush())
    return total


def run(
    engine: Engine,
    repo: Path,
    artifacts: list[Path],
    names: list[str],
    block_size: int | None = None,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """Stores a version series, reconstructs it, and measures everything.

    Reconstruction is timed and verified as part of the benchmark rather than
    separately: an encode-only number would flatter a scheme whose read path is
    the expensive half.
    """
    workdir = workdir or repo.parent / "bench-work"
    workdir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    previous: str | None = None
    encode_seconds = 0.0

    for artifact, name in zip(artifacts, names):
        started = time.perf_counter()
        outcome = engine.store(repo, artifact, name, base=previous, block_size=block_size)
        elapsed = time.perf_counter() - started
        encode_seconds += elapsed

        total_bits = outcome["artifact_size"] * 8
        results.append(
            {
                "name": name,
                "artifact_size": outcome["artifact_size"],
                "stored_size": outcome["stored_size"],
                "chain_depth": outcome["chain_depth"],
                "changed_bits": outcome["changed_bits"],
                "changed_bytes": outcome["changed_bytes"],
                # The sparsity ratio: the single number that predicts whether a
                # position-aligned XOR delta can pay off on this workload.
                "changed_bit_fraction": (outcome["changed_bits"] / total_bits) if total_bits else 0.0,
                "encode_seconds": round(elapsed, 3),
                "representations": _count_kinds(outcome["blocks"]),
            }
        )
        previous = name

    # Read path: reconstruct every version, timed, and confirm each verifies.
    decode_seconds = 0.0
    for name in names:
        out = workdir / f"{name}.reconstructed"
        started = time.perf_counter()
        engine.reconstruct(repo, name, out)
        decode_seconds += time.perf_counter() - started
        out.unlink(missing_ok=True)

    logical_total = sum(r["artifact_size"] for r in results)
    ccp_total = sum(r["stored_size"] for r in results)
    independent = compressed_size(artifacts, together=False)
    concatenated = compressed_size(artifacts, together=True)

    return {
        "versions": results,
        "totals": {
            "logical_bytes": logical_total,
            "ccp_stored_bytes": ccp_total,
            "baseline_full_copies_bytes": logical_total,
            "baseline_independent_zlib_bytes": independent,
            "baseline_concatenated_zlib_bytes": concatenated,
            "encode_seconds": round(encode_seconds, 3),
            "decode_seconds": round(decode_seconds, 3),
            "encode_throughput_mib_s": _throughput(logical_total, encode_seconds),
            "decode_throughput_mib_s": _throughput(logical_total, decode_seconds),
        },
        "comparisons": {
            # Ratios below 1.0 mean CCP stored less than the baseline. Stated as
            # ratios rather than "percent saved" so the direction cannot be
            # misread, and never generalised beyond these artifacts.
            "ccp_over_full_copies": _ratio(ccp_total, logical_total),
            "ccp_over_independent_zlib": _ratio(ccp_total, independent),
            "ccp_over_concatenated_zlib": _ratio(ccp_total, concatenated),
        },
    }


def _count_kinds(blocks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["kind"]] = counts.get(b["kind"], 0) + 1
    return dict(sorted(counts.items()))


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _throughput(total_bytes: int, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return round(total_bytes / seconds / (1024 * 1024), 1)
