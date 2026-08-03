#!/usr/bin/env python3
"""Per-tensor CCP analysis of a safetensors model file.

The whole-file benchmark answers "does CCP shrink this checkpoint". It cannot
answer "is there structural redundancy inside the weights, and if not, why", and
that is the question a buyer will actually ask. This script goes tensor by
tensor:

    * every tensor is encoded and decoded on its own, verified by SHA-256
    * gzip is measured on the same bytes for reference
    * for 2- and 4-byte dtypes the tensor is also measured after byte-plane
      deinterleaving, which groups the exponent bytes of adjacent values
      together — a reversible reordering that tests whether the redundancy in
      float weights is present but scattered rather than absent

The safetensors header is plain JSON, so this needs no ML framework installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
from dataclasses import dataclass, field
from typing import Sequence

from ccp_full_experiment import (
    HEADER_STRUCT,
    TABLE_STRUCT,
    TYPE_CCP,
    decode_bytes,
    encode_bytes,
    format_bytes,
    parse_size,
    read_header,
)

DTYPE_WIDTH: dict[str, int] = {
    "F64": 8,
    "F32": 4,
    "F16": 2,
    "BF16": 2,
    "I64": 8,
    "I32": 4,
    "I16": 2,
    "I8": 1,
    "U8": 1,
    "BOOL": 1,
}

CANDIDATE_REGION_SIZES: tuple[int, ...] = (
    4 * 1024,
    16 * 1024,
    64 * 1024,
    256 * 1024,
    1024 * 1024,
)


# ---------------------------------------------------------------------------
# safetensors
# ---------------------------------------------------------------------------


@dataclass
class TensorEntry:
    name: str
    dtype: str
    shape: list[int]
    start: int
    end: int

    @property
    def nbytes(self) -> int:
        return self.end - self.start


def read_safetensors_index(path: str) -> tuple[list[TensorEntry], int]:
    """Return the tensor table and the offset where tensor data begins."""
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ValueError("not a safetensors file: header length missing")
        (header_len,) = struct.unpack("<Q", raw)
        if header_len <= 0 or header_len > 512 * 1024 * 1024:
            raise ValueError(f"implausible safetensors header length {header_len}")
        header = json.loads(f.read(header_len))

    entries: list[TensorEntry] = []
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        start, end = meta["data_offsets"]
        entries.append(
            TensorEntry(
                name=name,
                dtype=meta["dtype"],
                shape=list(meta["shape"]),
                start=int(start),
                end=int(end),
            )
        )
    entries.sort(key=lambda e: e.start)
    return entries, 8 + header_len


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------


def deinterleave(data: bytes, width: int) -> bytes:
    """Group byte i of every element together. Reversible."""
    if width <= 1:
        return data
    usable = len(data) - (len(data) % width)
    view = memoryview(data)
    planes = [bytes(view[plane:usable:width]) for plane in range(width)]
    return b"".join(planes) + bytes(view[usable:])


def reinterleave(data: bytes, width: int, original_length: int) -> bytes:
    if width <= 1:
        return data
    usable = original_length - (original_length % width)
    count = usable // width
    out = bytearray(original_length)
    for plane in range(width):
        chunk = data[plane * count : (plane + 1) * count]
        out[plane:usable:width] = chunk
    out[usable:] = data[usable:]
    return bytes(out)


def gzip_bytes(data: bytes, level: int = 6) -> int:
    import zlib

    compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
    return len(compressor.compress(data)) + len(compressor.flush())


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    ccp_bytes: int
    region_size: int
    regions_ccp: int
    regions_total: int
    verified: bool


def measure_ccp(data: bytes) -> Measurement:
    """Best verified CCP encoding of `data` over the candidate region sizes."""
    best: Measurement | None = None
    for region_size in CANDIDATE_REGION_SIZES:
        if region_size > len(data):
            continue
        container = encode_bytes(data, region_size)
        verified = decode_bytes(container) == data

        view = memoryview(container)
        try:
            header = read_header(view)
            regions_ccp = 0
            for i in range(header.region_count):
                kind, _ = TABLE_STRUCT.unpack_from(
                    view, HEADER_STRUCT.size + i * TABLE_STRUCT.size
                )
                regions_ccp += 1 if kind == TYPE_CCP else 0
        finally:
            view.release()

        candidate = Measurement(
            ccp_bytes=len(container),
            region_size=region_size,
            regions_ccp=regions_ccp,
            regions_total=header.region_count,
            verified=verified,
        )
        if not verified:
            return candidate
        if best is None or candidate.ccp_bytes < best.ccp_bytes:
            best = candidate

    if best is None:
        return Measurement(len(data), 0, 0, 0, True)
    return best


@dataclass
class TensorResult:
    name: str
    dtype: str
    shape: list[int]
    nbytes: int
    ccp_bytes: int
    ccp_region_size: int
    ccp_regions_ccp: int
    ccp_regions_total: int
    gzip_bytes: int
    ccp_deinterleaved_bytes: int | None
    gzip_deinterleaved_bytes: int | None
    verified: bool
    deinterleave_verified: bool | None

    def saving(self, value: int | None) -> float | None:
        if value is None or not self.nbytes:
            return None
        return (1.0 - value / self.nbytes) * 100.0


def layer_family(name: str) -> str:
    """Collapse a tensor name to a comparable family across layers."""
    parts = []
    for token in name.split("."):
        parts.append("N" if token.isdigit() else token)
    return ".".join(parts)


def analyse(
    path: str,
    min_bytes: int,
    limit: int | None,
    test_deinterleave: bool,
) -> tuple[list[TensorResult], list[TensorEntry]]:
    entries, data_start = read_safetensors_index(path)
    selected = [e for e in entries if e.nbytes >= min_bytes]
    selected.sort(key=lambda e: -e.nbytes)
    skipped = [e for e in entries if e.nbytes < min_bytes]
    if limit is not None:
        skipped += selected[limit:]
        selected = selected[:limit]

    results: list[TensorResult] = []
    with open(path, "rb") as f:
        for i, entry in enumerate(selected, 1):
            f.seek(data_start + entry.start)
            data = f.read(entry.nbytes)
            if len(data) != entry.nbytes:
                raise ValueError(f"short read for tensor {entry.name}")

            print(
                f"  [{i}/{len(selected)}] {entry.name} "
                f"{entry.dtype}{entry.shape} {format_bytes(entry.nbytes)}",
                flush=True,
            )

            measurement = measure_ccp(data)
            gz = gzip_bytes(data)

            ccp_di: int | None = None
            gz_di: int | None = None
            di_verified: bool | None = None
            width = DTYPE_WIDTH.get(entry.dtype, 1)
            if test_deinterleave and width > 1 and entry.nbytes >= width * 2:
                transformed = deinterleave(data, width)
                di_verified = reinterleave(transformed, width, len(data)) == data
                di_measurement = measure_ccp(transformed)
                ccp_di = di_measurement.ccp_bytes
                gz_di = gzip_bytes(transformed)
                di_verified = di_verified and di_measurement.verified

            results.append(
                TensorResult(
                    name=entry.name,
                    dtype=entry.dtype,
                    shape=entry.shape,
                    nbytes=entry.nbytes,
                    ccp_bytes=measurement.ccp_bytes,
                    ccp_region_size=measurement.region_size,
                    ccp_regions_ccp=measurement.regions_ccp,
                    ccp_regions_total=measurement.regions_total,
                    gzip_bytes=gz,
                    ccp_deinterleaved_bytes=ccp_di,
                    gzip_deinterleaved_bytes=gz_di,
                    verified=measurement.verified,
                    deinterleave_verified=di_verified,
                )
            )
    return results, skipped


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def render(
    path: str,
    results: list[TensorResult],
    skipped: Sequence[TensorEntry],
    min_bytes: int,
) -> str:
    rule = "=" * 108
    thin = "-" * 108
    lines = [rule, "CCP PER-TENSOR ANALYSIS", rule, ""]
    lines.append(f"Model file      {os.path.abspath(path)}")
    lines.append(f"File size       {format_bytes(os.path.getsize(path))}")
    lines.append(f"Tensors tested  {len(results)}")
    lines.append(
        f"Tensors skipped {len(skipped)} (below {format_bytes(min_bytes)} or past --limit)"
    )
    lines.append("")

    lines.append(thin)
    lines.append("PER-TENSOR RESULTS")
    lines.append(thin)
    lines.append("")
    lines.append("CCP  = CCP saving on the tensor as stored")
    lines.append("CCP* = CCP saving after a reversible byte-plane reordering")
    lines.append("gzip*, likewise, is gzip on the reordered bytes")
    lines.append("")
    head = (
        f"{'Tensor':<46} {'dtype':>6} {'Size':>11} "
        f"{'CCP':>8} {'CCP*':>8} {'gzip':>8} {'gzip*':>8} {'Verify':>7}"
    )
    lines.append(head)
    lines.append("-" * len(head))
    for r in results:
        def pct(value: int | None) -> str:
            s = r.saving(value)
            return "-" if s is None else f"{s:.2f}%"

        verify = "PASS" if r.verified else "FAIL"
        if r.deinterleave_verified is False:
            verify = "FAIL*"
        lines.append(
            f"{r.name[:46]:<46} {r.dtype:>6} {format_bytes(r.nbytes):>11} "
            f"{pct(r.ccp_bytes):>8} {pct(r.ccp_deinterleaved_bytes):>8} "
            f"{pct(r.gzip_bytes):>8} {pct(r.gzip_deinterleaved_bytes):>8} {verify:>7}"
        )
    lines.append("")

    lines.append(thin)
    lines.append("AGGREGATE OVER TESTED TENSORS")
    lines.append(thin)
    lines.append("")
    total = sum(r.nbytes for r in results)
    if total:
        ccp_total = sum(r.ccp_bytes for r in results)
        gz_total = sum(r.gzip_bytes for r in results)
        ccp_di_total = sum(
            r.ccp_deinterleaved_bytes if r.ccp_deinterleaved_bytes is not None else r.nbytes
            for r in results
        )
        gz_di_total = sum(
            r.gzip_deinterleaved_bytes if r.gzip_deinterleaved_bytes is not None else r.nbytes
            for r in results
        )
        lines.append(f"Tensor bytes tested        {format_bytes(total)}")
        lines.append(
            f"CCP                        {format_bytes(ccp_total)}  "
            f"({(1 - ccp_total / total) * 100:.2f}%)"
        )
        lines.append(
            f"CCP, byte-plane reordered  {format_bytes(ccp_di_total)}  "
            f"({(1 - ccp_di_total / total) * 100:.2f}%)"
        )
        lines.append(
            f"gzip                       {format_bytes(gz_total)}  "
            f"({(1 - gz_total / total) * 100:.2f}%)"
        )
        lines.append(
            f"gzip, byte-plane reordered {format_bytes(gz_di_total)}  "
            f"({(1 - gz_di_total / total) * 100:.2f}%)"
        )
    lines.append("")

    lines.append(thin)
    lines.append("BY LAYER FAMILY (layer index collapsed)")
    lines.append(thin)
    lines.append("")
    families: dict[str, list[TensorResult]] = {}
    for r in results:
        families.setdefault(layer_family(r.name), []).append(r)
    head = f"{'Family':<52} {'Count':>6} {'Bytes':>12} {'CCP':>8} {'CCP*':>8} {'gzip':>8}"
    lines.append(head)
    lines.append("-" * len(head))
    for family, group in sorted(families.items(), key=lambda kv: -sum(r.nbytes for r in kv[1])):
        nbytes = sum(r.nbytes for r in group)
        ccp = sum(r.ccp_bytes for r in group)
        ccp_di = sum(
            r.ccp_deinterleaved_bytes if r.ccp_deinterleaved_bytes is not None else r.nbytes
            for r in group
        )
        gz = sum(r.gzip_bytes for r in group)
        lines.append(
            f"{family[:52]:<52} {len(group):>6} {format_bytes(nbytes):>12} "
            f"{(1 - ccp / nbytes) * 100:>7.2f}% "
            f"{(1 - ccp_di / nbytes) * 100:>7.2f}% "
            f"{(1 - gz / nbytes) * 100:>7.2f}%"
        )
    lines.append("")

    lines.append(thin)
    lines.append("TENSORS WHERE CCP FOUND STRUCTURE")
    lines.append(thin)
    lines.append("")
    winners = [r for r in results if (r.saving(r.ccp_bytes) or 0) >= 1.0]
    if winners:
        for r in sorted(winners, key=lambda r: -(r.saving(r.ccp_bytes) or 0)):
            lines.append(
                f"  {r.name[:60]:<60} {r.saving(r.ccp_bytes):>7.2f}%  "
                f"{r.ccp_regions_ccp}/{r.ccp_regions_total} regions as deltas"
            )
    else:
        lines.append("  none reached a 1% saving on the tensor as stored")
    lines.append("")

    failures = [r for r in results if not r.verified or r.deinterleave_verified is False]
    lines.append(thin)
    lines.append("VERIFICATION")
    lines.append(thin)
    lines.append("")
    lines.append(
        f"  Tensors round-tripped byte-for-byte: {len(results) - len(failures)}/{len(results)}"
    )
    for r in failures:
        lines.append(f"  FAILED: {r.name}")
    lines.append("")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-tensor CCP analysis")
    parser.add_argument("model", help="path to a .safetensors file")
    parser.add_argument(
        "--output", default="ccp_layer_report.txt", help="report path"
    )
    parser.add_argument("--json", default=None, help="also write JSON results here")
    parser.add_argument(
        "--min-size",
        default="1MB",
        help="ignore tensors smaller than this (default 1MB)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="test at most this many tensors"
    )
    parser.add_argument(
        "--no-transform",
        action="store_true",
        help="skip the byte-plane reordering comparison",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.model):
        print(f"error: not a file: {args.model}", file=sys.stderr)
        return 1

    min_bytes = parse_size(args.min_size)
    print(f"Analysing {args.model} ...")
    results, skipped = analyse(
        args.model, min_bytes, args.limit, test_deinterleave=not args.no_transform
    )

    report = render(args.model, results, skipped, min_bytes)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport: {args.output}")

    if args.json:
        from dataclasses import asdict

        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": os.path.abspath(args.model),
                    "file_bytes": os.path.getsize(args.model),
                    "tensors": [asdict(r) for r in results],
                },
                f,
                indent=2,
            )
        print(f"JSON:   {args.json}")

    if any(not r.verified or r.deinterleave_verified is False for r in results):
        print("FATAL: a tensor failed to round-trip", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
