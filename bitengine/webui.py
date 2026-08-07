"""Engine glue for the BitEngine dashboard.

Everything the UI does, with no UI framework imported. `app.py` is a thin
Streamlit layer over this module, which means the behaviour a user sees is
covered by `tests/test_webui.py` and does not depend on Streamlit being
installed to be verified.

Nothing here is a mock. `pack` runs the real L2 controller and L3 container,
times them, decodes the result back and checks it against the input's SHA-256
before returning. A pack whose round trip fails reports `verified=False` and the
UI is expected to show that rather than a saving.

The zstd comparison is included deliberately. `reports/head_to_head_zstd.txt`
records that `zstd --patch-from` beats this engine on every measured axis, and a
dashboard that displayed only our own saving would be presenting a number the
project's own benchmarks contradict. When zstandard is installed the panel shows
both.
"""

from __future__ import annotations

import hashlib
import io
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import l1
import l2
import l3

try:
    import zstandard
except ImportError:  # pragma: no cover - depends on the deployment
    zstandard = None  # type: ignore[assignment]

__all__ = [
    "MAX_UPLOAD_BYTES",
    "PROBE_SAMPLE_BYTES",
    "BlockRow",
    "PackOutcome",
    "UnpackOutcome",
    "InspectOutcome",
    "Baseline",
    "goal_choices",
    "build_goal",
    "choose_goal",
    "pack",
    "unpack",
    "inspect_container",
    "zstd_patch_baseline",
    "format_bytes",
    "format_pct",
]

# A browser upload is held in memory twice over (the upload buffer and the
# decoded copy), so this caps what a single request can cost. It is a guard
# rather than an engine limit: the CLI streams and has no such ceiling.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# What `probe` is allowed to read. Bounded so the sweep cost does not scale with
# file size — the same discipline the CLI uses.
PROBE_SAMPLE_BYTES = 8 * 1024 * 1024


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def format_pct(value: float | None) -> str:
    """`None` renders as `n/a`, never as `0.00%`.

    An unmeasured saving and a measured zero are different facts, and a
    dashboard that renders them identically is lying by rounding.
    """
    return "n/a" if value is None else f"{value:.2f}%"


@dataclass(frozen=True)
class BlockRow:
    index: int
    codec: str
    payload_bytes: int
    original_bytes: int

    @property
    def saving_pct(self) -> float | None:
        if self.original_bytes == 0:
            return None
        return (self.original_bytes - self.payload_bytes) / self.original_bytes * 100.0


@dataclass(frozen=True)
class Baseline:
    """A rival measured on the same inputs, so the comparison is like for like."""

    name: str
    encoded_bytes: int
    saving_pct: float
    encode_mbs: float
    decode_mbs: float
    verified: bool
    note: str = ""


@dataclass
class PackOutcome:
    container: bytes
    manifest: l3.Manifest
    goal: l2.Goal
    encode_mbs: float
    decode_mbs: float
    verified: bool
    blocks: list[BlockRow] = field(default_factory=list)
    probe_reports: list[l2.GoalReport] = field(default_factory=list)
    baselines: list[Baseline] = field(default_factory=list)

    @property
    def original_bytes(self) -> int:
        return self.manifest.total_bytes

    @property
    def container_bytes(self) -> int:
        return len(self.container)

    @property
    def saved_bytes(self) -> int:
        return self.original_bytes - self.container_bytes

    @property
    def saving_pct(self) -> float | None:
        return self.manifest.saving_pct

    @property
    def change_pct(self) -> float | None:
        savings = self.manifest.block_savings
        return savings.change_pct if savings else None

    @property
    def codec_counts(self) -> dict[str, int]:
        return dict(self.manifest.codec_counts or {})

    def filename(self, source_name: str) -> str:
        stem = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
        return f"{stem or 'output'}.bite"


@dataclass
class UnpackOutcome:
    data: bytes
    manifest: l3.Manifest
    verified: bool
    decode_mbs: float

    @property
    def restored_bytes(self) -> int:
        return len(self.data)


@dataclass
class InspectOutcome:
    manifest: l3.Manifest
    codec_counts: dict[str, int]
    blocks: list[BlockRow]
    deepest_chain: int

    @property
    def random_access_is_bounded(self) -> bool:
        return self.manifest.goal.keyframe_interval > 0 or self.deepest_chain <= 2


# ---------------------------------------------------------------------------
# goals
# ---------------------------------------------------------------------------


def goal_choices(has_reference: bool) -> list[l2.Goal]:
    """Goals that can actually run given what the user supplied.

    A paired goal without a second file is not a choice the UI should offer —
    it would fail at encode time, after the upload, which is the worst moment to
    discover it.
    """
    return [goal for goal in l2.GOALS.values() if goal.needs_reference == has_reference]


def build_goal(
    base: l2.Goal,
    block_bytes: int | None = None,
    keyframe_interval: int | None = None,
    fast_decode: bool = False,
) -> l2.Goal:
    changes: dict[str, object] = {}
    if block_bytes:
        changes["block_bytes"] = block_bytes
    if keyframe_interval is not None:
        changes["keyframe_interval"] = keyframe_interval
    if fast_decode:
        changes["codecs"] = l2.POLICY_FAST_DECODE
    return base.replace(**changes) if changes else base


def choose_goal(
    target: bytes, reference: bytes | None, sample_bytes: int = PROBE_SAMPLE_BYTES
) -> tuple[l2.Goal | None, list[l2.GoalReport]]:
    """Rank goals on a bounded sample of the real bytes and return the winner.

    Returns `(None, reports)` when nothing could be measured — a file smaller
    than two blocks at every candidate size. The caller must handle that rather
    than receiving a default that was never measured.
    """
    candidates = goal_choices(reference is not None)
    reports = l2.probe(
        target[:sample_bytes],
        candidates,
        reference[:sample_bytes] if reference is not None else None,
    )
    measured = [report for report in reports if report.measured]
    return (measured[0].goal if measured else None), reports


# ---------------------------------------------------------------------------
# pack / unpack / inspect
# ---------------------------------------------------------------------------


def _check_size(data: bytes, what: str) -> None:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"{what} is {format_bytes(len(data))}, over the "
            f"{format_bytes(MAX_UPLOAD_BYTES)} browser upload limit. Use the CLI, which streams."
        )


def pack(
    target: bytes,
    reference: bytes | None,
    goal: l2.Goal,
    with_baselines: bool = True,
) -> PackOutcome:
    """Encode, time it, then decode it back and verify before reporting anything.

    The verification is not optional and not a separate button. A saving that
    cannot be reversed is not a saving, so the round trip is part of producing
    the number.
    """
    _check_size(target, "target file")
    if reference is not None:
        _check_size(reference, "reference file")
    if not target:
        raise ValueError("target file is empty; there is nothing to pack")
    if goal.needs_reference and reference is None:
        raise ValueError(f"goal {goal.name!r} compares two files; upload a reference as well")

    buffer = io.BytesIO()
    started = time.perf_counter()
    manifest = l3.write_container(
        buffer, io.BytesIO(target), goal, io.BytesIO(reference) if reference is not None else None
    )
    encode_seconds = time.perf_counter() - started
    container = buffer.getvalue()

    started = time.perf_counter()
    with l3.Reader(
        io.BytesIO(container), io.BytesIO(reference) if reference is not None else None
    ) as reader:
        restored = b"".join(reader.blocks())
        table = reader.block_table()
    decode_seconds = time.perf_counter() - started

    verified = restored == target and hashlib.sha256(restored).digest() == manifest.sha256
    megabytes = len(target) / 1e6

    blocks = [
        BlockRow(index=index, codec=l1.CODEC_NAMES[codec], payload_bytes=payload, original_bytes=original)
        for index, codec, payload, original in table
    ]

    baselines: list[Baseline] = []
    if with_baselines and reference is not None:
        rival = zstd_patch_baseline(target, reference)
        if rival is not None:
            baselines.append(rival)

    return PackOutcome(
        container=container,
        manifest=manifest,
        goal=goal,
        encode_mbs=megabytes / encode_seconds if encode_seconds else 0.0,
        decode_mbs=megabytes / decode_seconds if decode_seconds else 0.0,
        verified=verified,
        blocks=blocks,
        baselines=baselines,
    )


def unpack(container: bytes, reference: bytes | None) -> UnpackOutcome:
    """Restore a container, verifying against the SHA-256 it recorded.

    `container` is untrusted: it arrived over an upload form. Every field in it
    is validated by `l3.Reader` before allocation, and a malformed container
    raises `l3.ContainerError` rather than producing partial output.
    """
    _check_size(container, "container")
    if not container:
        raise ValueError("no container uploaded")

    started = time.perf_counter()
    with l3.Reader(
        io.BytesIO(container), io.BytesIO(reference) if reference is not None else None
    ) as reader:
        data = b"".join(reader.blocks())
        manifest = reader.manifest
    decode_seconds = time.perf_counter() - started

    verified = (
        len(data) == manifest.total_bytes and hashlib.sha256(data).digest() == manifest.sha256
    )
    return UnpackOutcome(
        data=data,
        manifest=manifest,
        verified=verified,
        decode_mbs=(len(data) / 1e6 / decode_seconds) if decode_seconds else 0.0,
    )


def inspect_container(container: bytes, reference: bytes | None = None) -> InspectOutcome:
    """Read a container's manifest and block table without decoding it."""
    _check_size(container, "container")
    if not container:
        raise ValueError("no container uploaded")

    with l3.Reader(
        io.BytesIO(container), io.BytesIO(reference) if reference is not None else None
    ) as reader:
        manifest = reader.manifest
        table = reader.block_table()
        counts = reader.codec_histogram()
        deepest = reader.chain_length(manifest.block_count - 1) if manifest.block_count else 0

    return InspectOutcome(
        manifest=manifest,
        codec_counts=counts,
        blocks=[
            BlockRow(index=i, codec=l1.CODEC_NAMES[c], payload_bytes=p, original_bytes=o)
            for i, c, p, o in table
        ],
        deepest_chain=deepest,
    )


# ---------------------------------------------------------------------------
# the honest comparison
# ---------------------------------------------------------------------------


def zstd_patch_baseline(target: bytes, reference: bytes, level: int = 19) -> Baseline | None:
    """`zstd --patch-from` on the same two files — the same-information rival.

    Returns `None` when zstandard is not installed, and the UI then says so
    rather than omitting the row silently. Hiding the comparison would make the
    dashboard read as though our saving were the best available, which
    `reports/head_to_head_zstd.txt` shows it is not.
    """
    if zstandard is None:
        return None

    dictionary = zstandard.ZstdCompressionDict(reference, dict_type=zstandard.DICT_TYPE_RAWCONTENT)
    params = zstandard.ZstdCompressionParameters.from_level(level, enable_ldm=True, window_log=27)
    compressor = zstandard.ZstdCompressor(dict_data=dictionary, compression_params=params)

    started = time.perf_counter()
    packed = compressor.compress(target)
    encode_seconds = time.perf_counter() - started

    decompressor = zstandard.ZstdDecompressor(dict_data=dictionary, max_window_size=1 << 27)
    started = time.perf_counter()
    restored = decompressor.decompress(packed, max_output_size=len(target) * 2 + 1024)
    decode_seconds = time.perf_counter() - started

    megabytes = len(target) / 1e6
    return Baseline(
        name=f"zstd --patch-from -{level}",
        encoded_bytes=len(packed),
        saving_pct=(len(target) - len(packed)) / len(target) * 100.0 if target else 0.0,
        encode_mbs=megabytes / encode_seconds if encode_seconds else 0.0,
        decode_mbs=megabytes / decode_seconds if decode_seconds else 0.0,
        verified=restored == target,
        note="same information, same job — the comparison that decides the ratio question",
    )


def codec_palette() -> dict[str, str]:
    """Stable colours for the block map, so a codec keeps its colour across runs."""
    return {
        "identical": "#2f855a",
        "sparse": "#3182ce",
        "runs": "#805ad5",
        "bitmap": "#d69e2e",
        "raw": "#c53030",
    }


def block_map_segments(blocks: Sequence[BlockRow], max_segments: int = 2000) -> list[tuple[str, int]]:
    """Collapse the block list into (codec, run length) pairs for drawing.

    A container can hold millions of blocks and a browser cannot draw one
    element each, so consecutive blocks sharing a codec become one segment. If
    that is still too many, the tail is dropped and the caller is told how many
    blocks it covers rather than the picture silently misrepresenting the file.
    """
    segments: list[tuple[str, int]] = []
    for block in blocks:
        if segments and segments[-1][0] == block.codec:
            segments[-1] = (block.codec, segments[-1][1] + 1)
        else:
            segments.append((block.codec, 1))
        if len(segments) >= max_segments:
            break
    return segments
