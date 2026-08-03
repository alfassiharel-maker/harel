#!/usr/bin/env python3
"""CCP Full Experiment — end-to-end Copy-Change-Paste evaluation.

Runs the whole chain on a binary file:

    load -> split into regions -> group regions sharing a common base ->
    decide ORIGINAL vs CCP per region -> serialise a real container ->
    decode -> verify SHA-256 -> report

The size reported for CCP is the *measured* byte length of the serialised
container, not a formula. The analytic cost model is computed alongside it and
printed as a cross-check; if the two disagree the encoder and the model have
drifted apart and the run is suspect.

Standard library only. Runs on any binary file.

    python3 ccp_full_experiment.py MODEL.gguf
    python3 ccp_full_experiment.py --random 64MB
    python3 ccp_full_experiment.py MODEL.gguf --sizes 64KB,1MB --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import re
import struct
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import BinaryIO, Iterator, Sequence

MAGIC = b"CCP1"
VERSION = 1

TYPE_ORIGINAL = 0
TYPE_CCP = 1

HEADER_STRUCT = struct.Struct("<4sBIQIIB32s")
TABLE_STRUCT = struct.Struct("<BI")
COUNT_STRUCT = struct.Struct("<I")

DEFAULT_REGION_SIZES: tuple[int, ...] = (
    4 * 1024,
    16 * 1024,
    64 * 1024,
    256 * 1024,
    1024 * 1024,
    4 * 1024 * 1024,
)

# Regions are grouped by a banded sketch over sampled byte positions. Each
# region contributes BAND_COUNT keys, each key being the bytes found at
# BAND_WIDTH sampled positions; two regions become candidates when any key
# matches. For a region of n bytes differing in k of them, one band survives
# untouched with probability (1 - k/n) ** BAND_WIDTH, so the chance of being
# missed entirely falls off as the band count grows. The bias is deliberate:
# regions that differ in few bytes — the ones that can actually pay for
# themselves — are found almost always, while near-misses that would be
# rejected on cost anyway are sometimes skipped, which only saves work.
#
# Slice hashing was tried first and discarded: it only finds regions whose
# changes are clustered in a few slices, and misses the scattered single-byte
# changes that dominate real near-duplicate data.
BAND_COUNT = 16
BAND_WIDTH = 8
SAMPLE_COUNT = BAND_COUNT * BAND_WIDTH
FULL_DIGEST_BYTES = 16

# A single band key can attract a huge bucket on degenerate data. Only this many
# members of a bucket are considered as candidates for one region, which keeps
# grouping close to linear.
CANDIDATES_PER_REGION = 64

# Medoid search over a cluster is quadratic in cluster size. Past this bound the
# lowest-index member is used instead, so encode time stays predictable on files
# with one very large near-duplicate group.
EXHAUSTIVE_BASE_MAX = 32

# Each cluster is capped so a single pathological group cannot make encoding
# quadratic over the whole file; overflow members are re-clustered.
MAX_CLUSTER_SIZE = 4096

_NONZERO = re.compile(rb"[^\x00]")

HASH_READ_CHUNK = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def parse_size(text: str) -> int:
    text = text.strip().upper().replace("IB", "B")
    for suffix, mul in (
        ("KB", 1024),
        ("MB", 1024**2),
        ("GB", 1024**3),
        ("B", 1),
    ):
        if text.endswith(suffix):
            value = float(text[: -len(suffix)])
            break
    else:
        value, mul = float(text), 1
    size = int(value * mul)
    if size <= 0:
        raise ValueError(f"size must be positive: {text!r}")
    return size


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_READ_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def position_width(region_size: int) -> int:
    """Bytes needed to address any offset inside a region."""
    return max(1, (max(region_size - 1, 1).bit_length() + 7) // 8)


def xor_bytes(a: memoryview, b: memoryview) -> bytes:
    """Bytewise XOR of two equal-length buffers."""
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    n = len(a)
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")


def diff_count(a: memoryview, b: memoryview) -> int:
    """Number of byte positions where a and b differ."""
    x = xor_bytes(a, b)
    return len(x) - x.count(0)


def delta_entries(base: memoryview, target: memoryview) -> list[tuple[int, int]]:
    """XOR delta as (position, xor_value) pairs, ascending by position.

    Reconstruction is target[i] == base[i] ^ xor_value.
    """
    x = xor_bytes(base, target)
    return [(m.start(), x[m.start()]) for m in _NONZERO.finditer(x)]


def apply_delta(base: memoryview, delta: Sequence[tuple[int, int]]) -> bytes:
    out = bytearray(base)
    for pos, xor_val in delta:
        out[pos] ^= xor_val
    return bytes(out)


# ---------------------------------------------------------------------------
# cost model
# ---------------------------------------------------------------------------


def model_delta_bits(k: int, region_size: int) -> int:
    """Analytic delta cost: a count header plus k (position, value) pairs.

    Mirrors C_delta = k * (log2(n) + 1) from the specification, with log2(n)
    rounded up to whole bytes because that is what the container actually
    writes, and the value field being a whole byte rather than a single bit
    because a byte-granular delta must carry the changed byte.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    if k == 0:
        return COUNT_STRUCT.size * 8
    return COUNT_STRUCT.size * 8 + k * (position_width(region_size) * 8 + 8)


def ccp_pays_off(k: int, region_size: int) -> bool:
    """Whether storing a region as a delta beats storing it verbatim."""
    return model_delta_bits(k, region_size) < region_size * 8


def break_even_changes(region_size: int) -> int:
    """Largest k for which CCP still beats verbatim storage of a region."""
    per_entry_bits = position_width(region_size) * 8 + 8
    budget_bits = region_size * 8 - COUNT_STRUCT.size * 8 - 1
    return max(0, budget_bits // per_entry_bits)


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


def sample_positions(region_size: int) -> tuple[int, ...]:
    """Byte offsets sampled from a region, spread evenly across it."""
    if region_size <= 0:
        return ()
    count = min(SAMPLE_COUNT, region_size)
    step = region_size / count
    seen: list[int] = []
    used: set[int] = set()
    for i in range(count):
        pos = min(region_size - 1, int(i * step))
        if pos not in used:
            used.add(pos)
            seen.append(pos)
    return tuple(seen)


def region_signature(
    region: memoryview, positions: Sequence[int] | None = None
) -> tuple[bytes, ...]:
    """Band keys for a region, plus its full digest so exact copies always meet.

    Each band key is the raw bytes at a handful of sampled positions, tagged with
    the band index so keys from different bands cannot collide with each other.
    """
    n = len(region)
    if n == 0:
        return ()
    if positions is None:
        positions = sample_positions(n)

    keys: list[bytes] = [
        b"\xff" + hashlib.blake2b(region, digest_size=FULL_DIGEST_BYTES).digest()
    ]
    for band in range(BAND_COUNT):
        chunk = positions[band * BAND_WIDTH : (band + 1) * BAND_WIDTH]
        if not chunk:
            break
        keys.append(bytes([band]) + bytes(region[p] for p in chunk))
    return tuple(keys)


def cluster_regions(signatures: Sequence[tuple[bytes, ...]]) -> list[list[int]]:
    """Group region indices that share at least one band key.

    A single greedy pass: each still-unassigned region claims the unassigned
    regions that collide with it most often. Regions that collide with nobody
    become singletons and are stored verbatim. Grouping is deliberately loose —
    every candidate is re-checked against the real cost later, so a false
    positive costs encode time and nothing else.
    """
    inverted: dict[bytes, list[int]] = defaultdict(list)
    for idx, sig in enumerate(signatures):
        for key in sig:
            inverted[key].append(idx)

    clusters: list[list[int]] = []
    assigned = bytearray(len(signatures))

    for i, sig in enumerate(signatures):
        if assigned[i]:
            continue
        overlap: dict[int, int] = defaultdict(int)
        for key in sig:
            bucket = inverted[key]
            if len(bucket) > CANDIDATES_PER_REGION:
                bucket = bucket[:CANDIDATES_PER_REGION]
            for j in bucket:
                if j != i and not assigned[j]:
                    overlap[j] += 1

        assigned[i] = 1
        cluster = [i]
        # Strongest collisions first: those are the likeliest to survive the
        # cost check, and the cluster is capped.
        for j, _ in sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0])):
            if assigned[j]:
                continue
            cluster.append(j)
            assigned[j] = 1
            if len(cluster) >= MAX_CLUSTER_SIZE:
                break
        clusters.append(cluster)

    return clusters


def select_base(regions: "RegionView", cluster: Sequence[int]) -> int:
    """Cluster member with the smallest total distance to the others."""
    if len(cluster) == 1:
        return cluster[0]
    if len(cluster) > EXHAUSTIVE_BASE_MAX:
        return cluster[0]

    best_idx = cluster[0]
    best_score: int | None = None
    for candidate in cluster:
        base = regions[candidate]
        score = 0
        for other in cluster:
            if other != candidate:
                score += diff_count(base, regions[other])
        if best_score is None or score < best_score:
            best_score = score
            best_idx = candidate
    return best_idx


# ---------------------------------------------------------------------------
# region access over an mmap
# ---------------------------------------------------------------------------


class RegionView:
    """Zero-copy indexed access to the equal-sized regions of a buffer."""

    def __init__(self, view: memoryview, region_size: int) -> None:
        self._view = view
        self.region_size = region_size
        self.count = len(view) // region_size
        self.tail_offset = self.count * region_size
        self.tail_length = len(view) - self.tail_offset

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> memoryview:
        if not 0 <= index < self.count:
            raise IndexError(index)
        start = index * self.region_size
        return self._view[start : start + self.region_size]

    def tail(self) -> memoryview:
        return self._view[self.tail_offset :]


# ---------------------------------------------------------------------------
# encoding
# ---------------------------------------------------------------------------


@dataclass
class RegionPlan:
    kind: int
    base_index: int = 0
    change_count: int = 0


@dataclass
class EncodeStats:
    region_size: int
    regions_total: int
    regions_ccp: int
    regions_original: int
    bases_used: int
    clusters_found: int
    largest_cluster: int
    total_changes: int
    tail_bytes: int
    container_bytes: int
    model_bytes: int
    header_bytes: int
    table_bytes: int
    base_payload_bytes: int
    delta_payload_bytes: int
    original_payload_bytes: int
    encode_seconds: float


def _plan_regions(regions: RegionView) -> tuple[list[RegionPlan], dict[str, int]]:
    positions = sample_positions(regions.region_size)
    signatures = [region_signature(regions[i], positions) for i in range(len(regions))]
    clusters = cluster_regions(signatures)

    plans: list[RegionPlan | None] = [None] * len(regions)
    bases: set[int] = set()
    total_changes = 0

    for cluster in clusters:
        if len(cluster) == 1:
            plans[cluster[0]] = RegionPlan(kind=TYPE_ORIGINAL)
            continue

        base_index = select_base(regions, cluster)
        base = regions[base_index]
        plans[base_index] = RegionPlan(kind=TYPE_ORIGINAL)

        cluster_has_delta = False
        for other in cluster:
            if other == base_index:
                continue
            k = diff_count(base, regions[other])
            if ccp_pays_off(k, regions.region_size):
                plans[other] = RegionPlan(
                    kind=TYPE_CCP, base_index=base_index, change_count=k
                )
                total_changes += k
                cluster_has_delta = True
            else:
                plans[other] = RegionPlan(kind=TYPE_ORIGINAL)
        if cluster_has_delta:
            bases.add(base_index)

    for i, plan in enumerate(plans):
        if plan is None:
            raise RuntimeError(f"region {i} was never planned")

    stats = {
        "bases_used": len(bases),
        "clusters_found": len(clusters),
        "largest_cluster": max((len(c) for c in clusters), default=0),
        "total_changes": total_changes,
    }
    return [p for p in plans if p is not None], stats


def encode(
    source_view: memoryview,
    region_size: int,
    source_sha256: bytes,
    out: BinaryIO,
) -> EncodeStats:
    """Write a CCP container for `source_view` to `out`."""
    started = time.monotonic()
    if region_size <= 0:
        raise ValueError("region_size must be positive")

    regions = RegionView(source_view, region_size)
    plans, cluster_stats = _plan_regions(regions)

    pos_width = position_width(region_size)
    header = HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        region_size,
        len(source_view),
        regions.count,
        regions.tail_length,
        pos_width,
        source_sha256,
    )
    out.write(header)

    for plan in plans:
        out.write(TABLE_STRUCT.pack(plan.kind, plan.base_index))
    table_bytes = TABLE_STRUCT.size * len(plans)

    base_payload = 0
    delta_payload = 0
    original_payload = 0
    regions_ccp = 0
    regions_original = 0
    base_set = {p.base_index for p in plans if p.kind == TYPE_CCP}

    for index, plan in enumerate(plans):
        if plan.kind == TYPE_ORIGINAL:
            region = regions[index]
            out.write(region)
            if index in base_set:
                base_payload += len(region)
            else:
                original_payload += len(region)
            regions_original += 1
            continue

        base = regions[plan.base_index]
        delta = delta_entries(base, regions[index])
        if len(delta) != plan.change_count:
            raise RuntimeError(
                f"region {index}: delta size {len(delta)} disagrees with "
                f"planned change count {plan.change_count}"
            )
        record = bytearray(COUNT_STRUCT.pack(len(delta)))
        for pos, xor_val in delta:
            record += pos.to_bytes(pos_width, "little")
            record.append(xor_val)
        out.write(record)
        delta_payload += len(record)
        regions_ccp += 1

    if regions.tail_length:
        out.write(regions.tail())
        original_payload += regions.tail_length

    container_bytes = (
        len(header) + table_bytes + base_payload + delta_payload + original_payload
    )

    model_bits = (
        (len(header) + table_bytes) * 8
        + (base_payload + original_payload) * 8
        + sum(
            model_delta_bits(p.change_count, region_size)
            for p in plans
            if p.kind == TYPE_CCP
        )
    )

    return EncodeStats(
        region_size=region_size,
        regions_total=regions.count + (1 if regions.tail_length else 0),
        regions_ccp=regions_ccp,
        regions_original=regions_original + (1 if regions.tail_length else 0),
        bases_used=cluster_stats["bases_used"],
        clusters_found=cluster_stats["clusters_found"],
        largest_cluster=cluster_stats["largest_cluster"],
        total_changes=cluster_stats["total_changes"],
        tail_bytes=regions.tail_length,
        container_bytes=container_bytes,
        model_bytes=(model_bits + 7) // 8,
        header_bytes=len(header),
        table_bytes=table_bytes,
        base_payload_bytes=base_payload,
        delta_payload_bytes=delta_payload,
        original_payload_bytes=original_payload,
        encode_seconds=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


@dataclass
class ContainerHeader:
    region_size: int
    original_size: int
    region_count: int
    tail_length: int
    pos_width: int
    sha256: bytes


def read_header(view: memoryview) -> ContainerHeader:
    if len(view) < HEADER_STRUCT.size:
        raise ValueError("container truncated: header incomplete")
    magic, version, region_size, original_size, count, tail, pos_width, digest = (
        HEADER_STRUCT.unpack_from(view, 0)
    )
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported container version {version}")
    if region_size <= 0:
        raise ValueError("bad region size in header")
    if tail >= region_size and count > 0:
        raise ValueError("tail length must be smaller than region size")
    # pos_width comes from the container, so a hostile or corrupt file could
    # claim a width that does not match the region it is addressing.
    if pos_width != position_width(region_size):
        raise ValueError(
            f"position width {pos_width} does not match region size {region_size}"
        )
    if count * region_size + tail != original_size:
        raise ValueError("header region accounting does not match original size")
    return ContainerHeader(region_size, original_size, count, tail, pos_width, digest)


def _payload_offsets(
    view: memoryview, header: ContainerHeader, table: Sequence[tuple[int, int]]
) -> list[int]:
    """Offset of each region's payload record inside the container."""
    offset = HEADER_STRUCT.size + TABLE_STRUCT.size * header.region_count
    offsets: list[int] = []
    entry_size = header.pos_width + 1
    for kind, _base in table:
        offsets.append(offset)
        if kind == TYPE_ORIGINAL:
            offset += header.region_size
        else:
            if offset + COUNT_STRUCT.size > len(view):
                raise ValueError("container truncated: delta header past end")
            (count,) = COUNT_STRUCT.unpack_from(view, offset)
            offset += COUNT_STRUCT.size + count * entry_size
        if offset > len(view):
            raise ValueError("container truncated: payload past end")
    if offset + header.tail_length > len(view):
        raise ValueError("container truncated: tail past end")
    offsets.append(offset)
    return offsets


def _read_table(
    view: memoryview, header: ContainerHeader
) -> list[tuple[int, int]]:
    table: list[tuple[int, int]] = []
    offset = HEADER_STRUCT.size
    for index in range(header.region_count):
        kind, base = TABLE_STRUCT.unpack_from(view, offset)
        offset += TABLE_STRUCT.size
        if kind not in (TYPE_ORIGINAL, TYPE_CCP):
            raise ValueError(f"region {index}: unknown type {kind}")
        if kind == TYPE_CCP:
            if not 0 <= base < header.region_count:
                raise ValueError(f"region {index}: base index {base} out of range")
            if base == index:
                raise ValueError(f"region {index}: self-referential base")
        table.append((kind, base))
    return table


def iter_decoded_regions(view: memoryview) -> Iterator[bytes]:
    """Yield the original file content back, region by region."""
    header = read_header(view)
    table = _read_table(view, header)
    offsets = _payload_offsets(view, header, table)

    for kind, base in table:
        if kind == TYPE_CCP and table[base][0] != TYPE_ORIGINAL:
            raise ValueError("delta chains are not allowed: base is not ORIGINAL")

    entry_size = header.pos_width + 1
    for index, (kind, base) in enumerate(table):
        if kind == TYPE_ORIGINAL:
            start = offsets[index]
            yield bytes(view[start : start + header.region_size])
            continue

        base_start = offsets[base]
        base_view = view[base_start : base_start + header.region_size]
        start = offsets[index]
        (count,) = COUNT_STRUCT.unpack_from(view, start)
        cursor = start + COUNT_STRUCT.size
        out = bytearray(base_view)
        for _ in range(count):
            pos = int.from_bytes(view[cursor : cursor + header.pos_width], "little")
            if pos >= header.region_size:
                raise ValueError(f"region {index}: delta position {pos} out of range")
            out[pos] ^= view[cursor + header.pos_width]
            cursor += entry_size
        yield bytes(out)

    if header.tail_length:
        start = offsets[-1]
        yield bytes(view[start : start + header.tail_length])


def decode_to_file(container_path: str, out_path: str) -> str:
    """Decode a container to `out_path` and return the SHA-256 of the result."""
    digest = hashlib.sha256()
    with open(container_path, "rb") as src, open(out_path, "wb") as dst:
        with mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            view = memoryview(mapped)
            try:
                for chunk in iter_decoded_regions(view):
                    digest.update(chunk)
                    dst.write(chunk)
            finally:
                view.release()
    return digest.hexdigest()


def decode_bytes(container: bytes) -> bytes:
    view = memoryview(container)
    try:
        return b"".join(iter_decoded_regions(view))
    finally:
        view.release()


def encode_bytes(data: bytes, region_size: int) -> bytes:
    """Convenience round-trip helper used by the tests."""
    import io

    buffer = io.BytesIO()
    view = memoryview(data)
    try:
        encode(view, region_size, hashlib.sha256(data).digest(), buffer)
    finally:
        view.release()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# experiment driver
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    region_size: int
    original_bytes: int
    ccp_bytes: int
    model_bytes: int
    saving_percent: float
    regions_total: int
    regions_ccp: int
    regions_original: int
    bases_used: int
    clusters_found: int
    largest_cluster: int
    total_changes: int
    break_even_changes: int
    header_bytes: int
    table_bytes: int
    base_payload_bytes: int
    delta_payload_bytes: int
    original_payload_bytes: int
    encode_seconds: float
    decode_seconds: float
    reconstruction: str
    decoded_sha256: str

    @property
    def passed(self) -> bool:
        return self.reconstruction == "PASS"


def run_region_size(
    source_path: str,
    region_size: int,
    source_sha256_hex: str,
    workdir: str,
    keep_container_dir: str | None = None,
) -> RunResult:
    source_sha = bytes.fromhex(source_sha256_hex)
    container_path = os.path.join(workdir, f"ccp_{region_size}.bin")
    decoded_path = os.path.join(workdir, f"decoded_{region_size}.bin")

    with open(source_path, "rb") as src:
        with mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            view = memoryview(mapped)
            try:
                with open(container_path, "wb") as out:
                    stats = encode(view, region_size, source_sha, out)
            finally:
                view.release()

    on_disk = os.path.getsize(container_path)
    if on_disk != stats.container_bytes:
        raise RuntimeError(
            f"container accounting mismatch: wrote {on_disk} bytes, "
            f"accounted {stats.container_bytes}"
        )

    decode_started = time.monotonic()
    decoded_sha = decode_to_file(container_path, decoded_path)
    decode_seconds = time.monotonic() - decode_started

    original_bytes = os.path.getsize(source_path)
    reconstruction = "PASS" if decoded_sha == source_sha256_hex else "FAIL"

    if keep_container_dir:
        os.makedirs(keep_container_dir, exist_ok=True)
        kept = os.path.join(
            keep_container_dir,
            f"{os.path.basename(source_path)}.r{region_size}.ccp",
        )
        os.replace(container_path, kept)
    else:
        os.unlink(container_path)
    os.unlink(decoded_path)

    saving = (
        (1.0 - stats.container_bytes / original_bytes) * 100.0
        if original_bytes
        else 0.0
    )

    return RunResult(
        region_size=region_size,
        original_bytes=original_bytes,
        ccp_bytes=stats.container_bytes,
        model_bytes=stats.model_bytes,
        saving_percent=saving,
        regions_total=stats.regions_total,
        regions_ccp=stats.regions_ccp,
        regions_original=stats.regions_original,
        bases_used=stats.bases_used,
        clusters_found=stats.clusters_found,
        largest_cluster=stats.largest_cluster,
        total_changes=stats.total_changes,
        break_even_changes=break_even_changes(region_size),
        header_bytes=stats.header_bytes,
        table_bytes=stats.table_bytes,
        base_payload_bytes=stats.base_payload_bytes,
        delta_payload_bytes=stats.delta_payload_bytes,
        original_payload_bytes=stats.original_payload_bytes,
        encode_seconds=stats.encode_seconds,
        decode_seconds=decode_seconds,
        reconstruction=reconstruction,
        decoded_sha256=decoded_sha,
    )


@dataclass
class Experiment:
    input_label: str
    original_bytes: int
    original_sha256: str
    results: list[RunResult] = field(default_factory=list)
    total_seconds: float = 0.0

    @property
    def best(self) -> RunResult | None:
        passing = [r for r in self.results if r.passed]
        if not passing:
            return None
        return min(passing, key=lambda r: r.ccp_bytes)

    @property
    def verdict(self) -> str:
        best = self.best
        if best is None:
            return "INCONCLUSIVE — no region size produced a verified round-trip"
        if best.saving_percent <= 0:
            return "NO SAVING — CCP was larger than the original at every region size"
        if best.saving_percent < 1.0:
            return "MARGINAL — saving under 1%, inside metadata noise"
        return f"SAVING — {best.saving_percent:.2f}% at {format_bytes(best.region_size)} regions"


def run_experiment(
    source_path: str,
    label: str,
    region_sizes: Sequence[int],
    workdir: str,
    verbose: bool = True,
    keep_container_dir: str | None = None,
) -> Experiment:
    original_bytes = os.path.getsize(source_path)
    if verbose:
        print("Hashing input ...")
    source_sha = sha256_file(source_path)

    experiment = Experiment(
        input_label=label,
        original_bytes=original_bytes,
        original_sha256=source_sha,
    )

    if verbose:
        print(f"Input:   {label}")
        print(f"Size:    {format_bytes(original_bytes)} ({original_bytes} bytes)")
        print(f"SHA-256: {source_sha}")

    started = time.monotonic()
    for region_size in region_sizes:
        if region_size > original_bytes:
            if verbose:
                print(
                    f"Skipping region size {format_bytes(region_size)}: "
                    f"larger than the input."
                )
            continue
        if verbose:
            print(f"\nRegion size {format_bytes(region_size)} ...")
        result = run_region_size(
            source_path, region_size, source_sha, workdir, keep_container_dir
        )
        experiment.results.append(result)
        if verbose:
            print(
                f"  regions={result.regions_total} "
                f"ccp={result.regions_ccp} "
                f"original={result.regions_original} "
                f"bases={result.bases_used}"
            )
            print(
                f"  ccp_bytes={result.ccp_bytes} "
                f"saving={result.saving_percent:.2f}% "
                f"reconstruction={result.reconstruction}"
            )
            print(
                f"  encode={result.encode_seconds:.2f}s "
                f"decode={result.decode_seconds:.2f}s"
            )
    experiment.total_seconds = time.monotonic() - started
    return experiment


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------


def write_random_file(path: str, size: int, seed: int = 1) -> None:
    """Incompressible control data, reproducible across runs."""
    with open(path, "wb") as f:
        written = 0
        counter = 0
        while written < size:
            block = hashlib.blake2b(
                counter.to_bytes(8, "little") + seed.to_bytes(8, "little"),
                digest_size=64,
            ).digest()
            take = min(len(block), size - written)
            f.write(block[:take])
            written += take
            counter += 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def render_report(experiment: Experiment) -> str:
    rule = "=" * 74
    thin = "-" * 74
    lines: list[str] = [rule, "CCP FULL EXPERIMENT REPORT", rule, ""]
    lines.append("Input")
    lines.append(f"  {experiment.input_label}")
    lines.append("")
    lines.append("Original size")
    lines.append(
        f"  {format_bytes(experiment.original_bytes)}  "
        f"({experiment.original_bytes} bytes)"
    )
    lines.append("")
    lines.append("Original SHA-256")
    lines.append(f"  {experiment.original_sha256}")
    lines.append("")

    lines.append(thin)
    lines.append("RESULTS BY REGION SIZE")
    lines.append(thin)
    lines.append("")
    header = (
        f"{'Region':>9}  {'Regions':>9}  {'CCP':>8}  {'Verbatim':>9}  "
        f"{'Bases':>6}  {'CCP size':>13}  {'Saving':>8}  {'Verify':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in experiment.results:
        lines.append(
            f"{format_bytes(r.region_size):>9}  "
            f"{r.regions_total:>9}  "
            f"{r.regions_ccp:>8}  "
            f"{r.regions_original:>9}  "
            f"{r.bases_used:>6}  "
            f"{r.ccp_bytes:>13}  "
            f"{r.saving_percent:>7.2f}%  "
            f"{r.reconstruction:>7}"
        )
    if not experiment.results:
        lines.append("  (no region size was small enough for this input)")
    lines.append("")

    lines.append(thin)
    lines.append("COST BREAKDOWN BY REGION SIZE")
    lines.append(thin)
    lines.append("")
    breakdown = (
        f"{'Region':>9}  {'Header':>8}  {'Index':>12}  {'Bases':>13}  "
        f"{'Deltas':>13}  {'Verbatim':>13}  {'Changes':>12}"
    )
    lines.append(breakdown)
    lines.append("-" * len(breakdown))
    for r in experiment.results:
        lines.append(
            f"{format_bytes(r.region_size):>9}  "
            f"{r.header_bytes:>8}  "
            f"{r.table_bytes:>12}  "
            f"{r.base_payload_bytes:>13}  "
            f"{r.delta_payload_bytes:>13}  "
            f"{r.original_payload_bytes:>13}  "
            f"{r.total_changes:>12}"
        )
    lines.append("")
    lines.append("Break-even change budget per region")
    for r in experiment.results:
        pct = 100.0 * r.break_even_changes / r.region_size
        lines.append(
            f"  {format_bytes(r.region_size):>9}: "
            f"{r.break_even_changes} changed bytes ({pct:.1f}% of the region)"
        )
    lines.append("")

    lines.append(thin)
    lines.append("TIMING")
    lines.append(thin)
    lines.append("")
    timing = f"{'Region':>9}  {'Encode':>10}  {'Decode':>10}"
    lines.append(timing)
    lines.append("-" * len(timing))
    for r in experiment.results:
        lines.append(
            f"{format_bytes(r.region_size):>9}  "
            f"{r.encode_seconds:>9.2f}s  "
            f"{r.decode_seconds:>9.2f}s"
        )
    lines.append("")

    best = experiment.best
    lines.append(thin)
    lines.append("BEST VERIFIED RESULT")
    lines.append(thin)
    lines.append("")
    if best is None:
        lines.append("  none — see verdict below")
    else:
        lines.append(f"Original size        {format_bytes(best.original_bytes)}")
        lines.append(f"CCP size             {format_bytes(best.ccp_bytes)}")
        lines.append(f"Total saving         {best.saving_percent:.2f}%")
        lines.append(f"Regions analyzed     {best.regions_total}")
        lines.append(f"Regions using CCP    {best.regions_ccp}")
        lines.append(f"Regions unchanged    {best.regions_original}")
        lines.append(f"Best region size     {format_bytes(best.region_size)}")
        lines.append(f"Reconstruction       {best.reconstruction}")
        lines.append(f"Encode time          {best.encode_seconds:.2f}s")
        lines.append(f"Decode time          {best.decode_seconds:.2f}s")
        agreement = (
            "agrees"
            if abs(best.model_bytes - best.ccp_bytes) <= max(8, best.regions_total)
            else "DIVERGES"
        )
        lines.append(
            f"Analytic cost model  {best.model_bytes} bytes ({agreement} "
            f"with the measured container)"
        )
    lines.append("")
    lines.append(f"Total processing     {experiment.total_seconds:.2f}s")
    lines.append("")

    lines.append(thin)
    lines.append("VERDICT")
    lines.append(thin)
    lines.append("")
    lines.append(f"  {experiment.verdict}")
    lines.append("")
    failed = [r for r in experiment.results if not r.passed]
    if failed:
        lines.append("  Round-trip failures:")
        for r in failed:
            lines.append(f"    region size {format_bytes(r.region_size)}: FAIL")
        lines.append("")
    lines.append(rule)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CCP full experiment: encode, measure, decode, verify."
    )
    parser.add_argument("input", nargs="?", help="path to a binary input file")
    parser.add_argument(
        "--output",
        default="ccp_full_report.txt",
        help="report path (default: ccp_full_report.txt)",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="also write machine-readable results to this path",
    )
    parser.add_argument(
        "--random",
        default=None,
        metavar="SIZE",
        help="use synthetic incompressible data of SIZE instead of a file "
        "(control baseline, e.g. 64MB)",
    )
    parser.add_argument(
        "--seed", type=int, default=1, help="seed for --random (default: 1)"
    )
    parser.add_argument(
        "--sizes",
        default=None,
        metavar="LIST",
        help="comma-separated region sizes to test (e.g. 64KB,1MB); "
        "defaults to 4KB..4MB",
    )
    parser.add_argument(
        "--keep-containers",
        default=None,
        metavar="DIR",
        help="retain the encoded containers in DIR instead of deleting them "
        "(used to measure CCP followed by a general-purpose compressor)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.sizes:
        region_sizes = tuple(parse_size(s) for s in args.sizes.split(","))
    else:
        region_sizes = DEFAULT_REGION_SIZES

    with tempfile.TemporaryDirectory(prefix="ccp_") as workdir:
        if args.random is not None:
            size = parse_size(args.random)
            source_path = os.path.join(workdir, "random.bin")
            if not args.quiet:
                print(f"Generating {format_bytes(size)} of control data ...")
            write_random_file(source_path, size, seed=args.seed)
            label = f"synthetic incompressible data, {format_bytes(size)}, seed={args.seed}"
        elif args.input:
            source_path = args.input
            if not os.path.isfile(source_path):
                print(f"error: not a file: {source_path}", file=sys.stderr)
                return 1
            if os.path.getsize(source_path) == 0:
                print("error: input file is empty", file=sys.stderr)
                return 1
            label = os.path.abspath(source_path)
        else:
            print(
                "error: provide an input file or --random SIZE", file=sys.stderr
            )
            return 1

        experiment = run_experiment(
            source_path,
            label,
            region_sizes,
            workdir,
            verbose=not args.quiet,
            keep_container_dir=args.keep_containers,
        )

    report = render_report(experiment)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    if not args.quiet:
        print(f"\n{experiment.verdict}")
        print(f"Report written to {args.output}")

    if args.json:
        payload = {
            "input": experiment.input_label,
            "original_bytes": experiment.original_bytes,
            "original_sha256": experiment.original_sha256,
            "verdict": experiment.verdict,
            "total_seconds": experiment.total_seconds,
            "results": [asdict(r) for r in experiment.results],
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        if not args.quiet:
            print(f"JSON written to {args.json}")

    if any(not r.passed for r in experiment.results):
        print("FATAL: a round-trip failed; results are not trustworthy", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
