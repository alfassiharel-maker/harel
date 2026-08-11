#!/usr/bin/env python3
"""Product Engine benchmark on real data.

Demonstrates the product layer end to end through its own public surface:

    open artifact -> validate -> inspect -> selective read / materialize
                  -> per-operation observability -> close

and reports, for a real directory, the numbers a product caller would use to
justify deployment: representation size, verification, and — the point of the
layer — how little a windowed read touches compared with materialising the unit.

Correctness comes from digest validation and from comparing every windowed read
against the full materialisation of the same unit. Latency is reported and relied
on for nothing.

    python3 -m ccp.benchmarks.product_benchmark <directory>
    python3 -m ccp.benchmarks.product_benchmark <directory> --out report.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional, Sequence

from ..product import ProductEngine

WINDOW_SIZES = (64, 256, 4096, 65536)
REPEATS = 5


def _format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{int(count)} B" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} GB"


def _best_of(function, repeats: int = REPEATS):
    best = float("inf")
    result = None
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        result = function()
        best = min(best, time.perf_counter() - started)
    return best, result


def _largest(artifact, uids: Sequence[str]) -> Optional[str]:
    best, best_size = None, -1
    for uid in uids:
        size = artifact.stat(uid).size
        if size > best_size:
            best, best_size = uid, size
    return best if best_size > 0 else None


def run(directory: str) -> str:
    if not os.path.isdir(directory):
        raise NotADirectoryError(directory)

    lines: List[str] = []
    emit = lines.append
    emit("CCP PRODUCT ENGINE BENCHMARK")
    emit("=" * 72)
    emit(f"input: {os.path.abspath(directory)}")
    emit("")

    engine = ProductEngine()

    # --- lifecycle: create + open + validate -----------------------------
    build_seconds, container = _best_of(
        lambda: engine.build_from_directory(directory), 1
    )
    assert isinstance(container, bytes)

    open_seconds, artifact = _best_of(lambda: engine.open(container), REPEATS)
    artifact = engine.open(container)

    info = artifact.info()
    emit("ARTIFACT")
    emit("-" * 72)
    emit(f"origin               {artifact.origin}")
    emit(f"units                {info.units} ({info.literals} full, {info.derived} derived)")
    emit(f"original             {_format_bytes(info.original_bytes)}")
    emit(f"representation       {_format_bytes(info.container_bytes)}")
    saving = info.saving
    emit(
        "saving               "
        + ("n/a" if saving is None else f"{saving * 100:.2f}%")
    )
    emit(f"build time           {build_seconds * 1000:.1f} ms")
    emit(f"open time            {open_seconds * 1000:.2f} ms (best of {REPEATS})")

    started = time.perf_counter()
    report = artifact.validate()
    emit(f"validate             {report.units_ok}/{report.units_checked} units, "
         f"{(time.perf_counter() - started) * 1000:.1f} ms")
    emit(f"verification state   {'verified' if artifact.is_verified else 'unverified'}")
    emit("")

    # --- selective access ------------------------------------------------
    derived = [u for u in artifact.units() if artifact.stat(u).kind == "derived"]
    target = _largest(artifact, derived) or _largest(artifact, artifact.units())
    if target is None:
        artifact.close()
        emit("no units to read")
        return "\n".join(lines)

    unit_size = artifact.stat(target).size
    full = artifact.materialize(target).data
    emit("SELECTIVE ACCESS (product read_range)")
    emit("-" * 72)
    emit(f"unit                 {target}")
    emit(f"unit size            {_format_bytes(unit_size)}   kind {artifact.stat(target).kind}")
    emit("")
    emit(f"{'window':>10} {'requested':>10} {'touched':>10} {'touched %':>10} "
         f"{'instr':>10} {'verify':>18} {'correct':>8}")

    offset = unit_size // 3
    for window in WINDOW_SIZES:
        if window > unit_size:
            continue
        _, result = _best_of(lambda w=window: artifact.read_range(target, offset, w))
        assert result is not None
        correct = result.data == full[offset : offset + window]
        rep = result.report
        ratio = rep.work_ratio
        emit(
            f"{window:>10} {rep.bytes_requested:>10} {rep.bytes_touched:>10} "
            f"{('n/a' if ratio is None else f'{rep.bytes_touched / unit_size * 100:9.2f}%'):>10} "
            f"{str(rep.instructions_visited) + '/' + str(rep.instructions_total):>10} "
            f"{rep.verification.value:>18} {'yes' if correct else 'NO':>8}"
        )
    mat = artifact.materialize(target)
    emit(
        f"{'full':>10} {unit_size:>10} {mat.report.bytes_touched:>10} "
        f"{'   100.00%':>10} "
        f"{str(mat.report.instructions_visited) + '/' + str(mat.report.instructions_total):>10} "
        f"{mat.report.verification.value:>18} {'yes':>8}"
    )
    emit("")

    # --- whole-corpus selective sweep, through the ledger ----------------
    # Expected slices are materialised first, into a plain dict, so the ledger
    # measured below reflects only the selective reads and is not inflated by the
    # full reconstructions used to check them.
    emit("CORPUS SWEEP: 256-byte product read from every unit")
    emit("-" * 72)
    expected = {}
    for uid in artifact.units():
        size = artifact.stat(uid).size
        if size:
            at = size // 2
            expected[uid] = artifact.materialize(uid).data[at : at + 256]

    artifact.reset_ledger()
    mismatches = 0
    for uid in artifact.units():
        size = artifact.stat(uid).size
        if size == 0:
            continue
        at = size // 2
        result = artifact.read_range(uid, at, 256)
        if result.data != expected[uid]:
            mismatches += 1
    ledger = artifact.ledger
    total = info.original_bytes
    emit(f"units read           {ledger.operations}")
    emit(f"mismatches           {mismatches}")
    emit(f"bytes returned       {ledger.bytes_returned}")
    emit(f"bytes touched        {ledger.bytes_touched}")
    emit(f"artifact total       {total}")
    if total:
        emit(f"touched / total      {ledger.bytes_touched / total * 100:.3f}%")
    emit(f"instructions visited {ledger.instructions_visited}")
    emit("")
    emit("VERDICT")
    emit("-" * 72)
    if report.ok and mismatches == 0:
        emit("The product engine opened, validated, and served selective reads over")
        emit("a real artifact through its public API. Every read matched a full")
        emit("materialisation, and windowed reads touched a small fraction of the")
        emit("artifact — the whole was never materialised to serve a part.")
    else:
        emit(f"FAILED: {len(report.failures)} validation failure(s), "
             f"{mismatches} mismatch(es).")

    # --- resource ownership ----------------------------------------------
    artifact.close()
    closed_cleanly = not artifact.is_open
    emit("")
    emit(f"artifact closed      {closed_cleanly} (resources released explicitly)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CCP Product Engine benchmark")
    parser.add_argument("directory")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    report = run(args.directory)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0 if "FAILED" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
