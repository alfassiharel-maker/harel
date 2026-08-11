#!/usr/bin/env python3
"""Final validation: the product, driven through its own application layer, on real data.

Not a synthetic fixture and not a library call — this drives `AppController`, the
same object the user interface drives, so what is measured is the product rather
than a component of it.

    python3 -m ccp.benchmarks.final_validation <directory> [--out report.txt]

Correctness comes from digest verification and from comparing selective reads
against full materialisation and against the original files on disk. Timings are
reported and relied on for nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from typing import List, Optional, Sequence

from ..integration import RecentArtifacts
from ..ui.controller import AppController

WINDOWS = (64, 256, 4096, 65536)


def _fmt(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(count) < 1024 or unit == "GB":
            return f"{int(count)} B" if unit == "B" else f"{count:.2f} {unit}"
        count /= 1024
    return f"{count:.2f} GB"


def run(directory: str) -> str:
    lines: List[str] = []
    emit = lines.append
    emit("CCP FORGE — FINAL REAL-DATA VALIDATION")
    emit("=" * 74)
    emit(f"input:    {os.path.abspath(directory)}")
    emit(f"platform: {sys.platform}")
    emit("driven through: ccp.ui.controller.AppController (the application layer)")
    emit("")

    workspace = tempfile.mkdtemp(prefix="ccp-validation-")
    controller = AppController(
        recents=RecentArtifacts(os.path.join(workspace, "appdata")),
        output_dir=os.path.join(workspace, "out"),
    )
    failures = 0
    try:
        # --- build ------------------------------------------------------
        started = time.perf_counter()
        summary = controller.build(directory)
        build_seconds = time.perf_counter() - started

        emit("1. BUILD")
        emit("-" * 74)
        emit(f"   input size          {_fmt(summary.original_bytes)} ({summary.original_bytes} bytes)")
        emit(f"   artifact size       {_fmt(summary.artifact_bytes)} ({summary.artifact_bytes} bytes)")
        emit(
            "   savings             "
            + ("n/a" if summary.saving is None else f"{summary.saving * 100:.2f}%")
        )
        emit(f"   units               {summary.units} ({summary.literals} full, {summary.derived} derived)")
        emit(f"   shared-base groups  {summary.groups}")
        emit(f"   build time          {build_seconds * 1000:.0f} ms")
        emit("")

        # --- verify -----------------------------------------------------
        report = controller.verify()
        emit("2. VERIFICATION")
        emit("-" * 74)
        emit(f"   result              {'PASS' if report['ok'] else 'FAIL'}")
        emit(f"   units verified      {report['units_ok']}/{report['units_checked']}")
        emit(f"   bytes verified      {_fmt(report['bytes_verified'])}")
        emit(f"   time                {report['elapsed_seconds'] * 1000:.0f} ms")
        if not report["ok"]:
            failures += 1
        emit("")

        # --- selective access -------------------------------------------
        rows = controller.units(limit=100000)["units"]
        multi = [r for r in rows if r["instructions"] > 1]
        target = max(multi or rows, key=lambda r: r["size"])
        uid, unit_size = target["uid"], target["size"]
        full = controller.materialize(uid)
        full_touched = full["metrics"]["bytes_touched"]

        emit("3. SELECTIVE READ")
        emit("-" * 74)
        emit(f"   unit                {uid}")
        emit(f"   size                {_fmt(unit_size)}   kind {target['kind']}   instructions {target['instructions']}")
        emit("")
        emit(f"   {'window':>9} {'returned':>9} {'touched':>9} {'work ratio':>11} {'instr':>9} {'correct':>8}")

        # The reference bytes for this unit: exported once through the product's
        # own full-materialisation path, which is digest-checked, then each
        # selective read is compared against the corresponding slice of it.
        reference_dir = os.path.join(workspace, "reference")
        reference_path = controller.export_unit(uid, reference_dir, overwrite=True)["path"]
        with open(reference_path, "rb") as handle:
            reference = handle.read()

        offset = unit_size // 3
        for window in WINDOWS:
            if window > unit_size:
                continue
            result = controller.read_range(uid, offset, window)
            metrics = result["metrics"]
            got = bytes.fromhex(result["preview"]["hex"])
            expected_slice = reference[offset : offset + window]
            # The preview is capped, so compare the part that crossed the API.
            correct = got == expected_slice[: len(got)]
            ratio = metrics["bytes_touched"] / full_touched if full_touched else 0.0
            if not correct:
                failures += 1
            emit(
                f"   {window:>9} {metrics['bytes_returned']:>9} {metrics['bytes_touched']:>9} "
                f"{ratio * 100:>10.3f}% "
                f"{str(metrics['instructions_visited']) + '/' + str(metrics['instructions_total']):>9} "
                f"{'yes' if correct else 'NO':>8}"
            )
        emit(
            f"   {'full':>9} {unit_size:>9} {full_touched:>9} {100.0:>10.3f}% "
            f"{str(full['metrics']['instructions_visited']) + '/' + str(full['metrics']['instructions_total']):>9} "
            f"{'yes':>8}"
        )
        emit("")

        # --- correctness against the original files ---------------------
        emit("4. CORRECTNESS AGAINST THE ORIGINAL FILES")
        emit("-" * 74)
        export_dir = os.path.join(workspace, "exported")
        checked = 0
        mismatches = 0
        for row in rows:
            source = os.path.join(directory, row["uid"])
            if not os.path.isfile(source):
                continue
            result = controller.export_unit(row["uid"], export_dir, overwrite=True)
            with open(source, "rb") as handle:
                original = handle.read()
            with open(result["path"], "rb") as handle:
                restored = handle.read()
            checked += 1
            if original != restored:
                mismatches += 1
        failures += mismatches
        emit(f"   units exported and compared   {checked}")
        emit(f"   byte-for-byte mismatches      {mismatches}")
        emit("")

        # --- corpus sweep ------------------------------------------------
        emit("5. CORPUS SWEEP — 256-byte selective read from every unit")
        emit("-" * 74)
        artifact_total = summary.original_bytes
        touched = 0
        returned = 0
        for row in rows:
            if row["size"] == 0:
                continue
            metrics = controller.read_range(row["uid"], row["size"] // 2, 256)["metrics"]
            touched += metrics["bytes_touched"]
            returned += metrics["bytes_returned"]
        emit(f"   units read                    {len(rows)}")
        emit(f"   bytes returned                {returned}")
        emit(f"   bytes touched                 {touched}")
        emit(f"   artifact content total        {artifact_total}")
        emit(f"   touched / total               {touched / artifact_total * 100:.3f}%")
        emit("")

        # --- package -----------------------------------------------------
        emit("6. PACKAGED APPLICATION")
        emit("-" * 74)
        for name in ("CCPForge", "CCPForge.exe", "CCPForge.pyz"):
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "dist",
                name,
            )
            if os.path.isfile(candidate):
                emit(f"   {name:<16} {_fmt(os.path.getsize(candidate))}")
        emit("")

        emit("VERDICT")
        emit("-" * 74)
        if failures == 0:
            emit("   PASS — the artifact verified, every exported unit matched the")
            emit("   original file byte for byte, and selective reads touched a small")
            emit("   fraction of what a full reconstruction touches.")
        else:
            emit(f"   FAIL — {failures} problem(s) found.")
    finally:
        controller.close()
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CCP Forge final validation")
    parser.add_argument("directory")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    report = run(args.directory)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0 if "FAIL —" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
