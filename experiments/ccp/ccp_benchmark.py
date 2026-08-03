#!/usr/bin/env python3
"""Multi-dataset CCP benchmark with general-purpose compressor baselines.

Runs the CCP engine over a set of datasets — synthetic controls plus any real
model weight files present — and puts the result next to gzip, bzip2 and LZMA on
the same bytes. Each CCP run happens in a child process so its peak resident set
size can be read from the kernel rather than estimated.

    python3 ccp_benchmark.py --size 64MB
    python3 ccp_benchmark.py --size 64MB --real-dir data --out-dir reports

The output is a text report and a machine-readable JSON file. Nothing about the
grouping rule or the cost formula appears in either.
"""

from __future__ import annotations

import argparse
import bz2
import json
import lzma
import os
import shutil
import subprocess
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

import ccp_datasets
from ccp_full_experiment import format_bytes, parse_size

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "ccp_full_experiment.py")

STREAM_CHUNK = 4 * 1024 * 1024

# LZMA and bzip2 are orders of magnitude slower than the rest. Above this size
# they run on a prefix of the file and the result is labelled as such, so a
# multi-hundred-megabyte dataset does not stall the whole benchmark.
SLOW_CODEC_CAP = 128 * 1024 * 1024

DEFAULT_REGION_SIZES = "4KB,16KB,64KB,256KB,1MB,4MB"


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------


@dataclass
class CodecResult:
    name: str
    compressed_bytes: int
    measured_on_bytes: int
    seconds: float
    prefix_only: bool

    @property
    def ratio_percent(self) -> float:
        if not self.measured_on_bytes:
            return 0.0
        return (1.0 - self.compressed_bytes / self.measured_on_bytes) * 100.0


def _stream_compress(
    path: str,
    make_compressor: Callable[[], object],
    limit: int | None,
) -> tuple[int, int, float]:
    """Compress a file (or its first `limit` bytes) and return sizes and time."""
    compressor = make_compressor()
    total_in = 0
    total_out = 0
    started = time.monotonic()
    with open(path, "rb") as f:
        while True:
            budget = STREAM_CHUNK if limit is None else min(STREAM_CHUNK, limit - total_in)
            if budget <= 0:
                break
            chunk = f.read(budget)
            if not chunk:
                break
            total_in += len(chunk)
            total_out += len(compressor.compress(chunk))  # type: ignore[attr-defined]
    total_out += len(compressor.flush())  # type: ignore[attr-defined]
    return total_in, total_out, time.monotonic() - started


def run_codecs(path: str, size: int, skip_slow: bool = False) -> list[CodecResult]:
    results: list[CodecResult] = []

    def add(name: str, factory: Callable[[], object], cap: int | None) -> None:
        limit = None if cap is None or size <= cap else cap
        measured_in, out_bytes, seconds = _stream_compress(path, factory, limit)
        results.append(
            CodecResult(
                name=name,
                compressed_bytes=out_bytes,
                measured_on_bytes=measured_in,
                seconds=seconds,
                prefix_only=limit is not None,
            )
        )

    add("gzip-6", lambda: zlib.compressobj(6, zlib.DEFLATED, 31), None)
    add("gzip-9", lambda: zlib.compressobj(9, zlib.DEFLATED, 31), None)
    if not skip_slow:
        add("bzip2-9", lambda: bz2.BZ2Compressor(9), SLOW_CODEC_CAP)
        add("lzma-6", lambda: lzma.LZMACompressor(preset=6), SLOW_CODEC_CAP)
    return results


def gzip_size(path: str) -> int:
    """Size of the file after gzip -6, used for the CCP-then-gzip combination."""
    _, out_bytes, _ = _stream_compress(path, lambda: zlib.compressobj(6, zlib.DEFLATED, 31), None)
    return out_bytes


# ---------------------------------------------------------------------------
# CCP via a child process, so peak RSS is measurable
# ---------------------------------------------------------------------------


def _peak_rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def run_ccp(
    path: str,
    region_sizes: str,
    workdir: str,
    keep_containers: str | None,
) -> tuple[dict, int, float]:
    """Run the engine on `path`. Returns (results json, peak RSS KB, wall time)."""
    json_path = os.path.join(workdir, "ccp_result.json")
    report_path = os.path.join(workdir, "ccp_result.txt")
    cmd = [
        sys.executable,
        ENGINE,
        path,
        "--sizes",
        region_sizes,
        "--json",
        json_path,
        "--output",
        report_path,
        "--quiet",
    ]
    if keep_containers:
        cmd += ["--keep-containers", keep_containers]

    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peak = 0
    while proc.poll() is None:
        peak = max(peak, _peak_rss_kb(proc.pid))
        time.sleep(0.05)
    peak = max(peak, _peak_rss_kb(proc.pid))
    stdout, stderr = proc.communicate()
    elapsed = time.monotonic() - started

    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"engine failed on {path} (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace')[:2000]}"
        )
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if proc.returncode == 2:
        payload["engine_reported_roundtrip_failure"] = True
    return payload, peak, elapsed


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------


@dataclass
class Dataset:
    name: str
    path: str
    description: str
    category: str
    synthetic: bool


@dataclass
class DatasetResult:
    name: str
    description: str
    category: str
    original_bytes: int
    original_sha256: str
    ccp_bytes: int
    ccp_saving_percent: float
    ccp_best_region_size: int
    ccp_regions_total: int
    ccp_regions_ccp: int
    ccp_regions_original: int
    ccp_bases_used: int
    ccp_total_changes: int
    ccp_delta_payload_bytes: int
    ccp_index_bytes: int
    reconstruction: str
    encode_seconds: float
    decode_seconds: float
    peak_rss_kb: int
    ccp_then_gzip_bytes: int | None
    codecs: list[CodecResult] = field(default_factory=list)
    per_region_size: list[dict] = field(default_factory=list)

    @property
    def best_codec(self) -> CodecResult | None:
        if not self.codecs:
            return None
        full = [c for c in self.codecs if not c.prefix_only]
        return min(full, key=lambda c: c.compressed_bytes) if full else None


def discover_real(real_dir: str) -> list[Dataset]:
    if not real_dir or not os.path.isdir(real_dir):
        return []
    out: list[Dataset] = []
    for entry in sorted(os.listdir(real_dir)):
        path = os.path.join(real_dir, entry)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            continue
        lower = entry.lower()
        if lower.endswith((".safetensors", ".bin", ".gguf", ".pt", ".pth", ".ckpt", ".onnx")):
            category = "real model weights"
        else:
            category = "real binary"
        out.append(
            Dataset(
                name=entry,
                path=path,
                description=f"real file, {format_bytes(os.path.getsize(path))}",
                category=category,
                synthetic=False,
            )
        )
    return out


def build_synthetic(workdir: str, size: int, seed: int, names: Sequence[str]) -> list[Dataset]:
    out: list[Dataset] = []
    for name in names:
        path = os.path.join(workdir, f"{name}.bin")
        print(f"  generating {name} ({format_bytes(size)}) ...", flush=True)
        ccp_datasets.build(name, path, size, seed)
        out.append(
            Dataset(
                name=name,
                path=path,
                description=ccp_datasets.DESCRIPTIONS[name],
                category="synthetic control",
                synthetic=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def evaluate(
    dataset: Dataset,
    region_sizes: str,
    workdir: str,
    skip_slow: bool,
    measure_ccp_then_gzip: bool,
) -> DatasetResult:
    size = os.path.getsize(dataset.path)
    run_dir = os.path.join(workdir, "run")
    os.makedirs(run_dir, exist_ok=True)
    keep_dir = os.path.join(run_dir, "containers") if measure_ccp_then_gzip else None
    if keep_dir and os.path.isdir(keep_dir):
        shutil.rmtree(keep_dir)

    payload, peak_rss, _ = run_ccp(dataset.path, region_sizes, run_dir, keep_dir)
    results = payload.get("results", [])
    passing = [r for r in results if r["reconstruction"] == "PASS"]
    best = min(passing, key=lambda r: r["ccp_bytes"]) if passing else None

    ccp_then_gzip: int | None = None
    if keep_dir and best and os.path.isdir(keep_dir):
        suffix = f".r{best['region_size']}.ccp"
        for entry in os.listdir(keep_dir):
            if entry.endswith(suffix):
                ccp_then_gzip = gzip_size(os.path.join(keep_dir, entry))
                break
        shutil.rmtree(keep_dir, ignore_errors=True)

    codecs = run_codecs(dataset.path, size, skip_slow=skip_slow)

    if best is None:
        return DatasetResult(
            name=dataset.name,
            description=dataset.description,
            category=dataset.category,
            original_bytes=size,
            original_sha256=payload.get("original_sha256", ""),
            ccp_bytes=size,
            ccp_saving_percent=0.0,
            ccp_best_region_size=0,
            ccp_regions_total=0,
            ccp_regions_ccp=0,
            ccp_regions_original=0,
            ccp_bases_used=0,
            ccp_total_changes=0,
            ccp_delta_payload_bytes=0,
            ccp_index_bytes=0,
            reconstruction="FAIL",
            encode_seconds=0.0,
            decode_seconds=0.0,
            peak_rss_kb=peak_rss,
            ccp_then_gzip_bytes=None,
            codecs=codecs,
            per_region_size=results,
        )

    return DatasetResult(
        name=dataset.name,
        description=dataset.description,
        category=dataset.category,
        original_bytes=size,
        original_sha256=payload.get("original_sha256", ""),
        ccp_bytes=best["ccp_bytes"],
        ccp_saving_percent=best["saving_percent"],
        ccp_best_region_size=best["region_size"],
        ccp_regions_total=best["regions_total"],
        ccp_regions_ccp=best["regions_ccp"],
        ccp_regions_original=best["regions_original"],
        ccp_bases_used=best["bases_used"],
        ccp_total_changes=best["total_changes"],
        ccp_delta_payload_bytes=best["delta_payload_bytes"],
        ccp_index_bytes=best["table_bytes"] + best["header_bytes"],
        reconstruction="PASS",
        encode_seconds=best["encode_seconds"],
        decode_seconds=best["decode_seconds"],
        peak_rss_kb=peak_rss,
        ccp_then_gzip_bytes=ccp_then_gzip,
        codecs=codecs,
        per_region_size=results,
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def render(results: list[DatasetResult], elapsed: float) -> str:
    rule = "=" * 100
    thin = "-" * 100
    lines = [rule, "CCP BENCHMARK — DATASET COMPARISON", rule, ""]
    lines.append(f"Datasets evaluated: {len(results)}")
    lines.append(f"Total wall time:    {elapsed:.1f}s")
    lines.append("")

    lines.append(thin)
    lines.append("HEADLINE TABLE")
    lines.append(thin)
    lines.append("")
    head = (
        f"{'Dataset':<34} {'Original':>12} {'CCP size':>13} {'Saving':>9} "
        f"{'Region':>8} {'Verify':>7}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        region = format_bytes(r.ccp_best_region_size) if r.ccp_best_region_size else "-"
        lines.append(
            f"{r.name[:34]:<34} "
            f"{format_bytes(r.original_bytes):>12} "
            f"{format_bytes(r.ccp_bytes):>13} "
            f"{r.ccp_saving_percent:>8.2f}% "
            f"{region:>8} "
            f"{r.reconstruction:>7}"
        )
    lines.append("")

    lines.append(thin)
    lines.append("CCP VERSUS GENERAL-PURPOSE COMPRESSION")
    lines.append(thin)
    lines.append("")
    lines.append("Percentages are size reduction against the original. 'p' marks a")
    lines.append(f"value measured on the first {format_bytes(SLOW_CODEC_CAP)} only.")
    lines.append("")
    codec_names: list[str] = []
    for r in results:
        for c in r.codecs:
            if c.name not in codec_names:
                codec_names.append(c.name)
    head = f"{'Dataset':<34} {'CCP':>9} {'CCP+gzip':>10}"
    for name in codec_names:
        head += f" {name:>10}"
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        row = f"{r.name[:34]:<34} {r.ccp_saving_percent:>8.2f}%"
        if r.ccp_then_gzip_bytes is not None and r.original_bytes:
            combo = (1.0 - r.ccp_then_gzip_bytes / r.original_bytes) * 100.0
            row += f" {combo:>9.2f}%"
        else:
            row += f" {'-':>10}"
        by_name = {c.name: c for c in r.codecs}
        for name in codec_names:
            c = by_name.get(name)
            if c is None:
                row += f" {'-':>10}"
            else:
                mark = "p" if c.prefix_only else " "
                row += f" {c.ratio_percent:>8.2f}%{mark}"
        lines.append(row)
    lines.append("")

    lines.append(thin)
    lines.append("STRUCTURAL FINDINGS")
    lines.append(thin)
    lines.append("")
    head = (
        f"{'Dataset':<34} {'Regions':>9} {'Delta':>9} {'Verbatim':>9} "
        f"{'Bases':>7} {'Changed bytes':>15} {'Index':>11}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        lines.append(
            f"{r.name[:34]:<34} "
            f"{r.ccp_regions_total:>9} "
            f"{r.ccp_regions_ccp:>9} "
            f"{r.ccp_regions_original:>9} "
            f"{r.ccp_bases_used:>7} "
            f"{r.ccp_total_changes:>15} "
            f"{format_bytes(r.ccp_index_bytes):>11}"
        )
    lines.append("")

    lines.append(thin)
    lines.append("PERFORMANCE")
    lines.append(thin)
    lines.append("")
    head = (
        f"{'Dataset':<34} {'Encode':>10} {'Decode':>10} "
        f"{'Enc MB/s':>10} {'Dec MB/s':>10} {'Peak RSS':>11}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        mb = r.original_bytes / (1024 * 1024)
        enc = mb / r.encode_seconds if r.encode_seconds else 0.0
        dec = mb / r.decode_seconds if r.decode_seconds else 0.0
        lines.append(
            f"{r.name[:34]:<34} "
            f"{r.encode_seconds:>9.2f}s "
            f"{r.decode_seconds:>9.2f}s "
            f"{enc:>10.1f} "
            f"{dec:>10.1f} "
            f"{format_bytes(r.peak_rss_kb * 1024):>11}"
        )
    lines.append("")

    lines.append(thin)
    lines.append("REGION SIZE SENSITIVITY (saving % by region size)")
    lines.append(thin)
    lines.append("")
    all_sizes: list[int] = []
    for r in results:
        for entry in r.per_region_size:
            if entry["region_size"] not in all_sizes:
                all_sizes.append(entry["region_size"])
    all_sizes.sort()
    head = f"{'Dataset':<34}"
    for s in all_sizes:
        head += f" {format_bytes(s):>10}"
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        by_size = {e["region_size"]: e for e in r.per_region_size}
        row = f"{r.name[:34]:<34}"
        for s in all_sizes:
            e = by_size.get(s)
            row += f" {'-':>10}" if e is None else f" {e['saving_percent']:>9.2f}%"
        lines.append(row)
    lines.append("")

    passed = [r for r in results if r.reconstruction == "PASS"]
    wins = [r for r in passed if r.ccp_saving_percent >= 1.0]
    losses = [r for r in passed if r.ccp_saving_percent <= 0.0]

    lines.append(thin)
    lines.append("WHERE CCP WORKS")
    lines.append(thin)
    lines.append("")
    if wins:
        for r in sorted(wins, key=lambda r: -r.ccp_saving_percent):
            lines.append(
                f"  {r.name[:44]:<44} {r.ccp_saving_percent:>7.2f}%  ({r.description})"
            )
    else:
        lines.append("  no dataset reached a 1% saving")
    lines.append("")
    lines.append(thin)
    lines.append("WHERE CCP DOES NOT WORK")
    lines.append(thin)
    lines.append("")
    if losses:
        for r in sorted(losses, key=lambda r: r.ccp_saving_percent):
            lines.append(
                f"  {r.name[:44]:<44} {r.ccp_saving_percent:>7.2f}%  ({r.description})"
            )
    else:
        lines.append("  every dataset produced a non-negative saving")
    lines.append("")

    lines.append(thin)
    lines.append("VERIFICATION")
    lines.append(thin)
    lines.append("")
    lines.append(f"  Round-trips verified by SHA-256: {len(passed)}/{len(results)}")
    failures = [r for r in results if r.reconstruction != "PASS"]
    for r in failures:
        lines.append(f"  FAILED: {r.name}")
    lines.append("")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CCP multi-dataset benchmark")
    parser.add_argument(
        "--size", default="64MB", help="size of each synthetic dataset (default 64MB)"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--real-dir",
        default="data",
        help="directory of real files to include (default: data)",
    )
    parser.add_argument(
        "--out-dir", default="reports", help="where to write the report (default: reports)"
    )
    parser.add_argument(
        "--sizes",
        default=DEFAULT_REGION_SIZES,
        help=f"region sizes to sweep (default: {DEFAULT_REGION_SIZES})",
    )
    parser.add_argument(
        "--datasets",
        default=None,
        help="comma-separated synthetic dataset names (default: all)",
    )
    parser.add_argument(
        "--no-synthetic", action="store_true", help="skip the synthetic controls"
    )
    parser.add_argument(
        "--skip-slow-codecs",
        action="store_true",
        help="only run the gzip baselines",
    )
    parser.add_argument(
        "--no-ccp-gzip",
        action="store_true",
        help="skip measuring CCP output re-compressed with gzip",
    )
    parser.add_argument("--workdir", default=None, help="scratch directory")
    args = parser.parse_args(argv)

    size = parse_size(args.size)
    os.makedirs(args.out_dir, exist_ok=True)
    workdir = args.workdir or os.path.join(args.out_dir, "_work")
    os.makedirs(workdir, exist_ok=True)

    datasets: list[Dataset] = []
    if not args.no_synthetic:
        names = (
            [n.strip() for n in args.datasets.split(",")]
            if args.datasets
            else sorted(ccp_datasets.GENERATORS)
        )
        print("Building synthetic datasets:")
        datasets += build_synthetic(workdir, size, args.seed, names)
    datasets += discover_real(args.real_dir)

    if not datasets:
        print("error: no datasets to evaluate", file=sys.stderr)
        return 1

    started = time.monotonic()
    results: list[DatasetResult] = []
    for i, dataset in enumerate(datasets, 1):
        print(
            f"\n[{i}/{len(datasets)}] {dataset.name} "
            f"({format_bytes(os.path.getsize(dataset.path))}) ...",
            flush=True,
        )
        result = evaluate(
            dataset,
            args.sizes,
            workdir,
            skip_slow=args.skip_slow_codecs,
            measure_ccp_then_gzip=not args.no_ccp_gzip,
        )
        results.append(result)
        print(
            f"    CCP {result.ccp_saving_percent:.2f}%  "
            f"verify={result.reconstruction}  "
            f"peak RSS={format_bytes(result.peak_rss_kb * 1024)}",
            flush=True,
        )
        if dataset.synthetic:
            os.unlink(dataset.path)

    elapsed = time.monotonic() - started
    report = render(results, elapsed)
    report_path = os.path.join(args.out_dir, "ccp_benchmark_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    json_path = os.path.join(args.out_dir, "ccp_benchmark_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "synthetic_dataset_bytes": size,
                "seed": args.seed,
                "region_sizes": args.sizes,
                "elapsed_seconds": elapsed,
                "results": [asdict(r) for r in results],
            },
            f,
            indent=2,
        )

    print(f"\nReport: {report_path}")
    print(f"JSON:   {json_path}")
    shutil.rmtree(os.path.join(workdir, "run"), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
