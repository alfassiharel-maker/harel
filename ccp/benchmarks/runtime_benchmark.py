#!/usr/bin/env python3
"""Runtime benchmark on real data.

Measures the chain the Runtime exists to serve:

    input data -> Core -> representation -> Runtime -> read/materialize -> verified

and reports, for a real directory:

    original size, representation size, bytes touched, instructions visited,
    reconstruction correctness, latency

Correctness is established by reconstructing every unit and checking it against
the digest recorded at build time, and by comparing every selective read against
the corresponding slice of a full materialisation. **None of that depends on the
timings below.** Latency is reported because a caller needs it; it proves
nothing, and no pass/fail in this file is derived from it.

    python3 -m ccp.benchmarks.runtime_benchmark <directory>
    python3 -m ccp.benchmarks.runtime_benchmark <directory> --out report.txt
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional, Sequence, Tuple

from ..api import build, open_representation

# Window sizes swept for the selective-read measurement. The point of the sweep
# is that the advantage is a function of how much of a unit is wanted, so a
# single window size would be a cherry-pick.
WINDOW_SIZES = (64, 256, 4096, 65536)

# Timing repetitions. Best-of, because the minimum is the robust estimator for a
# lower bound on work: it is the run least disturbed by the rest of the machine.
REPEATS = 5


def _format_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{int(count)} B" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} GB"


def _best_of(function, repeats: int = REPEATS) -> Tuple[float, object]:
    best = float("inf")
    result = None
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        result = function()
        best = min(best, time.perf_counter() - started)
    return best, result


def run(directory: str) -> str:
    if not os.path.isdir(directory):
        raise NotADirectoryError(directory)

    lines: List[str] = []
    emit = lines.append
    emit("CCP RUNTIME BENCHMARK")
    emit("=" * 72)
    emit(f"input: {os.path.abspath(directory)}")
    emit("")

    # --- build ------------------------------------------------------------
    build_seconds, container = _best_of(lambda: build.from_directory(directory), 1)
    assert isinstance(container, bytes)
    load_seconds, runtime = _best_of(lambda: open_representation(container), REPEATS)
    runtime = open_representation(container)

    info = runtime.info()
    emit("REPRESENTATION")
    emit("-" * 72)
    emit(f"units                {info.units} ({info.literals} full, {info.derived} derived)")
    emit(f"original             {_format_bytes(info.original_bytes)}")
    emit(f"representation       {_format_bytes(info.container_bytes)}")
    emit(f"  payload            {_format_bytes(info.payload_bytes)}")
    emit(f"  index              {_format_bytes(info.index_bytes)}")
    saving = info.saving
    emit(
        "saving               "
        + ("n/a (no input bytes)" if saving is None else f"{saving * 100:.2f}%")
    )
    emit(f"build time           {build_seconds * 1000:.1f} ms")
    emit(f"load time            {load_seconds * 1000:.2f} ms (best of {REPEATS})")
    emit("")

    # --- correctness ------------------------------------------------------
    report = runtime.verify()
    emit("CORRECTNESS")
    emit("-" * 72)
    emit(f"units verified       {report.units_ok}/{report.units_checked}")
    emit(f"bytes verified       {_format_bytes(report.bytes_verified)}")
    emit(f"verify time          {report.elapsed_seconds * 1000:.1f} ms")
    if report.failures:
        emit("FAILURES:")
        for uid, error in report.failures[:10]:
            emit(f"  {uid}: {error}")
    emit("")

    # --- selective read ---------------------------------------------------
    derived = [u for u in runtime.units() if runtime.stat(u).kind == "derived"]
    target = _largest(runtime, derived) or _largest(runtime, runtime.units())
    if target is None:
        emit("no units to read")
        return "\n".join(lines)

    unit_size = runtime.stat(target).size
    emit("SELECTIVE READ")
    emit("-" * 72)
    emit(f"unit                 {target}")
    emit(f"unit size            {_format_bytes(unit_size)}")
    emit(f"kind                 {runtime.stat(target).kind}")
    emit(f"instructions         {runtime.stat(target).instructions}")
    emit("")
    emit(
        f"{'window':>10} {'touched':>10} {'touched %':>10} "
        f"{'instr':>12} {'latency ms':>11} {'correct':>8}"
    )

    full_bytes = runtime.materialize(target)
    offset = unit_size // 3
    for window in WINDOW_SIZES:
        if window > unit_size:
            continue
        seconds, result = _best_of(
            lambda w=window: runtime.read_range(target, offset, w), REPEATS
        )
        assert result is not None
        # Correctness of the selective path, checked against the full
        # reconstruction rather than against an expectation computed here.
        correct = result.data == full_bytes[offset : offset + window]
        ratio = result.work_ratio
        emit(
            f"{window:>10} {result.bytes_touched:>10} "
            f"{'n/a' if ratio is None else f'{ratio * 100:9.2f}%'} "
            f"{str(result.instructions_visited) + '/' + str(result.instructions_total):>12} "
            f"{seconds * 1000:>11.4f} {'yes' if correct else 'NO':>8}"
        )

    full_seconds, _ = _best_of(lambda: runtime.materialize(target), REPEATS)
    emit("")
    emit(
        f"{'full':>10} {unit_size:>10} {'   100.00%':>10} "
        f"{str(runtime.stat(target).instructions) + '/' + str(runtime.stat(target).instructions):>12} "
        f"{full_seconds * 1000:>11.4f} {'yes':>8}"
    )
    emit("")
    emit("  'full' is materialising the unit and slicing it -- the baseline a")
    emit("  selective read is measured against. Latency is reported, not relied on:")
    emit("  the correctness column comes from byte comparison, not from timing.")
    emit("")

    # --- whole-corpus sweep ----------------------------------------------
    emit("CORPUS SWEEP: 256-byte read from every unit")
    emit("-" * 72)
    runtime.reset_ledger()
    started = time.perf_counter()
    mismatches = 0
    for uid in runtime.units():
        size = runtime.stat(uid).size
        if size == 0:
            continue
        at = size // 2
        result = runtime.read_range(uid, at, 256)
        if result.data != runtime.materialize(uid, verify=False)[at : at + 256]:
            mismatches += 1
    elapsed = time.perf_counter() - started
    ledger = runtime.ledger

    # The ledger counted both the selective reads and the full materialisations
    # used to check them, so the comparison below is stated from the parts rather
    # than from the total, which would flatter the selective side.
    selective = [r for r in ledger.records if r.operation == "read_range"]
    full = [r for r in ledger.records if r.operation == "materialize"]
    selective_touched = sum(r.bytes_touched for r in selective)
    full_touched = sum(r.bytes_touched for r in full)
    emit(f"units read           {len(selective)}")
    emit(f"mismatches           {mismatches}")
    emit(f"bytes touched        {selective_touched} (selective)")
    emit(f"                     {full_touched} (full materialisation of the same units)")
    if full_touched:
        emit(f"work ratio           {selective_touched / full_touched * 100:.2f}%")
    emit(f"instructions visited {sum(r.instructions_visited for r in selective)}")
    emit(f"wall clock           {elapsed * 1000:.1f} ms (both paths, including checks)")
    emit("")
    emit("VERDICT")
    emit("-" * 72)
    if report.ok and mismatches == 0:
        emit("Every unit reconstructed and matched its recorded digest, and every")
        emit("selective read matched the corresponding slice of a full")
        emit("reconstruction. The runtime serves windows by executing only the")
        emit("instructions that cover them.")
    else:
        emit(f"FAILED: {len(report.failures)} verification failure(s), "
             f"{mismatches} read mismatch(es).")
    return "\n".join(lines)


def _largest(runtime, uids: Sequence[str]) -> Optional[str]:
    best: Optional[str] = None
    best_size = -1
    for uid in uids:
        size = runtime.stat(uid).size
        if size > best_size:
            best, best_size = uid, size
    return best if best_size > 0 else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CCP Runtime benchmark on real data")
    parser.add_argument("directory", help="directory to build a representation from")
    parser.add_argument("--out", help="also write the report here")
    args = parser.parse_args(argv)

    report = run(args.directory)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0 if "FAILED" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
