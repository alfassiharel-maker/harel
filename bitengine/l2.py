"""BitEngine L2 — the controller that matches a user's goal to an algorithm.

L1 answers one question: given *this* base and *this* block, what is the
cheapest representation? It has no opinion about where the base came from. That
question — which base, what block size, which codecs are permitted — is the
whole of L2, and it is where the saving is actually won or lost.

`experiments/ccp/FINDINGS.md` §5 is the evidence for that. Sweeping region size
on the same data moved `exact_repeats` from 6.02% to 98.44%, and the optimum was
in a different place for different data. Choosing wrongly here costs more than
every codec decision in L1 combined.

Three axes, and a goal is a point in that space:

**Base strategy — where the block being compared against comes from.**

    PAIRED      the block at the same offset in a second stream.
                Two checkpoints of one model, two builds, two exports.
    PRECEDING   the block `stride` blocks earlier in the same stream.
                stride=1 is an adjacent-block delta; stride=blocks-per-frame is
                a video frame delta, which is the mode the brief describes: the
                XOR of two frames is exactly the per-channel colour difference,
                and unchanged pixels fall out as zero bytes at no cost.
    ANCHOR      always block 0. A fixed reference every block is a variation on.

**Block size.** Smaller blocks localise a change so fewer bytes are dragged into
its region; larger blocks amortise the per-block overhead. There is no value
that is right for all data, which is why `probe()` measures instead of assuming.

**Codec policy.** L1 will pick the smallest representation; sometimes that is
not what the user wants. Bitmap-coded blocks decode at 22 MB/s on maximally
scattered changes against 152 MB/s for the clustered equivalent, so a goal that
has to decode in real time can bar the bitmap codec and accept a smaller saving.
L1 reports the cost of every codec including barred ones, so that trade stays
measurable rather than becoming invisible.

`probe()` is the part that earns the name "controller". It does not guess which
goal suits a file; it runs a bounded sample of the real data through each
candidate and ranks them by what they actually saved.

Bounds, all enforced rather than documented:

*   Streams are read in fixed-size blocks and never held whole. Peak memory is
    the ring buffer, and `Goal` refuses a configuration whose window exceeds
    `MAX_WINDOW_BYTES`.
*   Every loop is bounded by `MAX_BLOCKS` or by a `range()` over a validated
    count. A stream longer than the cap raises rather than running on.
*   `probe()` consumes a caller-supplied prefix, so its cost does not scale with
    file size.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import BinaryIO

import l1

__all__ = [
    "ENGINE_VERSION",
    "MAX_BLOCKS",
    "MAX_WINDOW_BYTES",
    "PAIRED",
    "PRECEDING",
    "ANCHOR",
    "POLICY_SMALLEST",
    "POLICY_FAST_DECODE",
    "DEFAULT_BLOCK_SIZES",
    "MIN_PROBE_BLOCKS",
    "Goal",
    "GOALS",
    "goal_named",
    "EncodedBlock",
    "StreamResult",
    "GoalReport",
    "iter_blocks",
    "encode_stream",
    "decode_stream",
    "probe",
]


# Its own literal, not `l1.ENGINE_VERSION`. The point of the marker is to detect
# a stale copy of *this* file, and re-exporting L1's value would make a stale
# L2 report whatever version happened to sit next to it.
ENGINE_VERSION = "1.1"

# A backstop, not an operating limit: 16M blocks is 1TB at 64KB. It exists so a
# malformed or endless source cannot make the driver loop forever.
MAX_BLOCKS = 16_000_000

# Ceiling on the ring buffer a PRECEDING goal may ask for. A video goal at 4K
# needs one frame in hand; anything asking for far more than that is a
# configuration mistake and is refused at construction rather than at OOM.
MAX_WINDOW_BYTES = 256 * 1024 * 1024

PAIRED = "paired"
PRECEDING = "preceding"
ANCHOR = "anchor"

_STRATEGIES = frozenset({PAIRED, PRECEDING, ANCHOR})

# Every codec L1 offers. The smallest representation always wins.
POLICY_SMALLEST: frozenset[int] = frozenset(l1.CODEC_NAMES)

# Bars the bitmap codec, whose decode is sequential in the number of changed
# positions. Blocks that would have been bitmap-coded fall back to runs or raw,
# which cost more space and decode at a predictable rate.
POLICY_FAST_DECODE: frozenset[int] = frozenset(
    {l1.CODEC_IDENTICAL, l1.CODEC_SPARSE, l1.CODEC_RUNS, l1.CODEC_RAW}
)

# Powers of two from 4KB to 1MB. `probe()` sweeps these because block size is
# the single most consequential parameter in L2 and its optimum is neither
# predictable nor monotonic. Measured on a synthetic frame stream with a 16KB
# repeat period:
#
#     4KB   -0.02%    a quarter of a frame; every block straddles a change
#     16KB  82.04%    exactly one frame
#     64KB  45.94%    four frames; three frames' worth of change accumulate
#
# The default in a `Goal` is a starting point, not a recommendation. A sweep of
# an 82-point range is what stops a user accepting 45.94% because the goal's
# default happened to be 64KB.
DEFAULT_BLOCK_SIZES: tuple[int, ...] = (4096, 8192, 16384, 32768, 65536, 262144, 1048576)

# Below this a sweep result says nothing: a single block has no predecessor to
# delta against, so every strategy degenerates to storing it verbatim.
MIN_PROBE_BLOCKS = 2


@dataclass(frozen=True)
class Goal:
    """A user-selected optimisation target, resolved to concrete parameters.

    Frozen and validated on construction, so an invalid combination cannot reach
    the streaming driver — where the cost of discovering it is a half-written
    output rather than an exception.
    """

    name: str
    summary: str
    block_bytes: int
    base: str
    codecs: frozenset[int] = POLICY_SMALLEST
    stride_blocks: int = 1
    keyframe_interval: int = 0

    def __post_init__(self) -> None:
        if self.base not in _STRATEGIES:
            raise ValueError(f"unknown base strategy {self.base!r}, expected one of {sorted(_STRATEGIES)}")
        if not 1 <= self.block_bytes <= l1.MAX_BLOCK_BYTES:
            raise ValueError(f"block_bytes must be in 1..{l1.MAX_BLOCK_BYTES}, got {self.block_bytes}")
        if self.stride_blocks < 1:
            raise ValueError("stride_blocks must be at least 1")
        if self.keyframe_interval < 0:
            raise ValueError("keyframe_interval must be non-negative")
        if self.base != PRECEDING and self.stride_blocks != 1:
            raise ValueError("stride_blocks only applies to the PRECEDING strategy")
        if self.window_bytes > MAX_WINDOW_BYTES:
            raise ValueError(
                f"goal {self.name!r} needs a {self.window_bytes}-byte window, "
                f"over the {MAX_WINDOW_BYTES} limit"
            )
        # Normalise through L1 so a policy cannot bar the codecs that bound the
        # encoder, whatever the caller passed.
        resolved = l1.resolve_allowed(self.codecs)
        assert resolved is not None
        object.__setattr__(self, "codecs", resolved)

    @property
    def window_bytes(self) -> int:
        """Peak bytes of source held at once, excluding the block being encoded."""
        if self.base == PRECEDING:
            return self.block_bytes * self.stride_blocks
        if self.base == ANCHOR:
            return self.block_bytes
        return 0

    @property
    def needs_reference(self) -> bool:
        return self.base == PAIRED

    def is_keyframe(self, index: int) -> bool:
        """Whether block `index` is stored without reference to any other block.

        Keyframes are what make random access possible. A PRECEDING chain is
        otherwise a linked list: reaching block 10,000 means reconstructing all
        10,000. Forcing every Nth block to stand alone bounds that walk to N at
        the cost of storing those blocks verbatim — the same trade, and the same
        reason, as an I-frame in a video codec.

        `experiments/ccp/FINDINGS.md` §3 notes that per-region random access "was
        not built or measured, and should not be counted as a result". This is
        the mechanism that makes it real, and its cost shows up honestly in the
        savings figure rather than being hidden.
        """
        return self.keyframe_interval > 0 and index % self.keyframe_interval == 0

    def replace(self, **changes: object) -> Goal:
        """A copy with fields overridden, for sweeping and for CLI overrides."""
        fields: dict[str, object] = {
            "name": self.name,
            "summary": self.summary,
            "block_bytes": self.block_bytes,
            "base": self.base,
            "codecs": self.codecs,
            "stride_blocks": self.stride_blocks,
            "keyframe_interval": self.keyframe_interval,
        }
        unknown = set(changes) - set(fields)
        if unknown:
            raise ValueError(f"unknown goal fields: {sorted(unknown)}")
        fields.update(changes)
        return Goal(**fields)  # type: ignore[arg-type]

    def with_block_bytes(self, block_bytes: int) -> Goal:
        """A copy at a different block size, for sweeping."""
        return self.replace(block_bytes=block_bytes)


# The built-in goals. Each is a claim about the shape of a user's data, and the
# block sizes are starting points for `probe()` rather than tuned constants.
GOALS: dict[str, Goal] = {
    goal.name: goal
    for goal in (
        Goal(
            name="checkpoint-pair",
            summary="Two versions of one file: model checkpoints, builds, exports.",
            block_bytes=4096,
            base=PAIRED,
        ),
        Goal(
            name="video-frame-delta",
            summary="Successive frames of one stream; XOR is the per-channel colour delta.",
            block_bytes=65536,
            base=PRECEDING,
            stride_blocks=1,
        ),
        Goal(
            name="video-realtime",
            summary="Frame delta that must decode at a predictable rate; bars the bitmap codec.",
            block_bytes=65536,
            base=PRECEDING,
            stride_blocks=1,
            codecs=POLICY_FAST_DECODE,
        ),
        Goal(
            name="log-append",
            summary="Streams that repeat a nearby structure: logs, telemetry, records.",
            block_bytes=4096,
            base=PRECEDING,
            stride_blocks=1,
        ),
        Goal(
            name="anchor-dedup",
            summary="Many blocks that are variations on one fixed reference block.",
            block_bytes=65536,
            base=ANCHOR,
        ),
    )
}


def goal_named(name: str) -> Goal:
    if name not in GOALS:
        raise KeyError(f"unknown goal {name!r}, expected one of {sorted(GOALS)}")
    return GOALS[name]


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodedBlock:
    index: int
    payload: bytes
    plan: l1.BlockPlan


@dataclass
class StreamResult:
    """Totals for one encoded stream, with the drivers behind them."""

    goal: Goal
    blocks: int = 0
    savings: l1.Savings | None = None
    by_codec: dict[int, int] = field(default_factory=dict)

    def codec_names(self) -> dict[str, int]:
        return {l1.CODEC_NAMES[c]: n for c, n in sorted(self.by_codec.items())}


def iter_blocks(source: BinaryIO | Iterable[bytes], block_bytes: int) -> Iterator[bytes]:
    """Yield fixed-size blocks, the last possibly short, never holding the whole stream.

    Accepts a binary file object or any iterable of byte chunks, so a caller can
    feed a socket or a generator without materialising it. Chunk boundaries in
    the input are irrelevant: they are re-cut to `block_bytes`.
    """
    if not 1 <= block_bytes <= l1.MAX_BLOCK_BYTES:
        raise ValueError(f"block_bytes must be in 1..{l1.MAX_BLOCK_BYTES}")

    emitted = 0
    if hasattr(source, "read"):
        reader = source.read  # type: ignore[union-attr]
        for _ in range(MAX_BLOCKS):
            parts = bytearray()
            # A short read is not end of stream on a pipe or socket, so a block
            # is only short once the reader has actually returned nothing. Each
            # pass adds at least one byte or breaks, so block_bytes passes is a
            # hard ceiling.
            for _ in range(block_bytes):
                if len(parts) >= block_bytes:
                    break
                chunk = reader(block_bytes - len(parts))
                if not chunk:
                    break
                parts += chunk
            if not parts:
                return
            emitted += 1
            yield bytes(parts)
            if len(parts) < block_bytes:
                return
        raise ValueError(f"stream exceeds MAX_BLOCKS ({MAX_BLOCKS})")

    pending = bytearray()
    for chunk in source:  # type: ignore[union-attr]
        pending += chunk
        while len(pending) >= block_bytes:
            if emitted >= MAX_BLOCKS:
                raise ValueError(f"stream exceeds MAX_BLOCKS ({MAX_BLOCKS})")
            yield bytes(pending[:block_bytes])
            del pending[:block_bytes]
            emitted += 1
    if pending:
        yield bytes(pending)


class _BaseResolver:
    """Supplies the base for each block, holding only what the strategy needs.

    A zero-filled base is used wherever the strategy has nothing yet — the first
    block, or a PAIRED reference that ran out. That is not a special case bolted
    on: XOR against zero makes the residual equal to the target, so L1 sees a
    block with no exploitable structure and picks RAW on its own. An all-zero
    region, meanwhile, encodes to a single byte, which is the correct answer and
    would have been missed by a hand-written "no base yet" branch.
    """

    __slots__ = ("_goal", "_window", "_anchor", "_reference")

    def __init__(self, goal: Goal, reference: Iterator[bytes] | None) -> None:
        self._goal = goal
        # maxlen makes the memory bound structural: the deque physically cannot
        # grow past the stride, so a leak here is not expressible.
        self._window: deque[bytes] = deque(maxlen=goal.stride_blocks)
        self._anchor: bytes | None = None
        self._reference = reference

    def base_for(self, index: int, length: int) -> bytes:
        goal = self._goal
        candidate: bytes | None = None

        if goal.is_keyframe(index):
            # A keyframe still consumes its reference block, or a PAIRED stream
            # would slip out of alignment for every block after the first one.
            if goal.base == PAIRED and self._reference is not None:
                next(self._reference, None)
            return bytes(length)

        if goal.base == PAIRED:
            if self._reference is not None:
                candidate = next(self._reference, None)
        elif goal.base == ANCHOR:
            candidate = self._anchor
        elif len(self._window) == goal.stride_blocks:
            candidate = self._window[0]

        if candidate is None:
            return bytes(length)
        # The final block of a stream may be short, and a PAIRED reference may
        # be a different length from its source. A base is only meaningful for
        # the bytes the block actually has, so it is trimmed or zero-extended to
        # match rather than the block being forced to RAW on a length mismatch.
        if len(candidate) < length:
            return candidate + bytes(length - len(candidate))
        return candidate[:length]

    def observe(self, block: bytes) -> None:
        """Record a *reconstructed* block, so encode and decode stay in step."""
        if self._goal.base == ANCHOR:
            if self._anchor is None:
                self._anchor = block
        elif self._goal.base == PRECEDING:
            self._window.append(block)


def encode_stream(
    source: BinaryIO | Iterable[bytes],
    goal: Goal,
    reference: BinaryIO | Iterable[bytes] | None = None,
) -> Iterator[EncodedBlock]:
    """Encode a stream under a goal, yielding one block at a time.

    A generator by design: the caller decides what to do with each block, and
    nothing accumulates here, so a 100GB input costs the same memory as a 1MB
    one.
    """
    if goal.needs_reference and reference is None:
        raise ValueError(f"goal {goal.name!r} compares against a second stream, but none was given")

    reference_blocks = iter_blocks(reference, goal.block_bytes) if reference is not None else None
    resolver = _BaseResolver(goal, reference_blocks)

    for index, block in enumerate(iter_blocks(source, goal.block_bytes)):
        base = resolver.base_for(index, len(block))
        plan = l1.plan_block(base, block, goal.codecs)
        payload = l1.encode_block(base, block, goal.codecs)
        resolver.observe(block)
        yield EncodedBlock(index=index, payload=payload, plan=plan)


def decode_stream(
    blocks: Iterable[EncodedBlock] | Iterable[bytes],
    goal: Goal,
    reference: BinaryIO | Iterable[bytes] | None = None,
    total_bytes: int | None = None,
) -> Iterator[bytes]:
    """Reconstruct a stream encoded under the same goal.

    Accepts either the `EncodedBlock` objects `encode_stream` produced or bare
    payloads, since a container will have stripped the metadata by then. The
    goal is the only thing both sides must agree on, and every base is derived
    from already-reconstructed output, so decoding needs no index.

    `total_bytes` is how the length of a short final block is known. Every codec
    except RAW reconstructs at the length of its base, so without the original
    length a trailing partial block would silently decode to a full one. Pass it
    whenever the stream may not be a whole number of blocks — a container
    records it for exactly this reason.
    """
    if goal.needs_reference and reference is None:
        raise ValueError(f"goal {goal.name!r} compares against a second stream, but none was given")

    reference_blocks = iter_blocks(reference, goal.block_bytes) if reference is not None else None
    resolver = _BaseResolver(goal, reference_blocks)

    for index, item in enumerate(blocks):
        payload = item.payload if isinstance(item, EncodedBlock) else item
        if not payload:
            raise l1.CorruptBlock(f"block {index} is empty")

        if total_bytes is not None:
            remaining = total_bytes - index * goal.block_bytes
            if remaining <= 0:
                raise l1.CorruptBlock(f"block {index} is past the declared {total_bytes} bytes")
            length = min(goal.block_bytes, remaining)
        elif payload[0] == l1.CODEC_RAW:
            length = len(payload) - 1
        else:
            length = goal.block_bytes

        base = resolver.base_for(index, length)
        block = l1.decode_block(base, payload)
        resolver.observe(block)
        yield block


def summarise(encoded: Iterable[EncodedBlock], goal: Goal) -> StreamResult:
    """Roll a stream of encoded blocks up into one savings figure.

    Consumes the iterator, so callers that also need the payloads should tee or
    collect them first. Kept separate from `encode_stream` precisely so that
    accounting never forces the payloads to be retained.
    """
    accumulator = l1.SavingsAccumulator()
    count = 0
    for block in encoded:
        accumulator.add(block.plan)
        count += 1
    return StreamResult(
        goal=goal,
        blocks=count,
        savings=accumulator.result() if count else None,
        by_codec=accumulator.by_codec,
    )


# ---------------------------------------------------------------------------
# the matcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalReport:
    """What one candidate goal achieved on a sample, and what it cost.

    `saving_pct` is `None`, not `0.0`, when the sample was too small to fill a
    single block under this goal — the goal was not measured, and reporting a
    zero would be indistinguishable from a goal that was measured and failed.
    """

    goal: Goal
    blocks: int
    sample_bytes: int
    saving_pct: float | None
    by_codec: dict[str, int]

    @property
    def measured(self) -> bool:
        return self.saving_pct is not None


def probe(
    sample: bytes,
    candidates: Sequence[Goal] | None = None,
    reference_sample: bytes | None = None,
    block_sizes: Sequence[int] | None = None,
) -> list[GoalReport]:
    """Rank goals by what they actually save on a sample of the real data.

    This is the decision L2 exists to make, and it is made by measurement rather
    than by inspecting the file type. `FINDINGS.md` §5 is the reason: the best
    block size moved a result from 6.02% to 98.44% on the same bytes, and was in
    a different place for different data. A lookup table keyed on file extension
    would have been wrong on both.

    Encoding is planned but never emitted — `plan_block` returns the exact size
    each block would occupy, so a sweep costs one statistics pass per block and
    produces no output at all.

    The caller supplies the sample, so the cost of probing is theirs to bound. A
    few megabytes from the head of a file is enough to separate the candidates.
    """
    pool = list(candidates) if candidates is not None else list(GOALS.values())
    reports: list[GoalReport] = []

    for goal in pool:
        if goal.needs_reference and reference_sample is None:
            continue
        sizes = block_sizes if block_sizes is not None else DEFAULT_BLOCK_SIZES
        for size in sorted(set(sizes)):
            if size * MIN_PROBE_BLOCKS > len(sample):
                continue
            variant = goal.with_block_bytes(size)
            encoded = encode_stream(
                [sample],
                variant,
                [reference_sample] if reference_sample is not None else None,
            )
            result = summarise(encoded, variant)
            reports.append(
                GoalReport(
                    goal=variant,
                    blocks=result.blocks,
                    sample_bytes=len(sample),
                    saving_pct=result.savings.saving_pct if result.savings else None,
                    by_codec=result.codec_names(),
                )
            )

    # Unmeasured goals sort last rather than being dropped, so a caller can see
    # that a candidate was considered and why it produced nothing.
    reports.sort(key=lambda r: (r.saving_pct is None, -(r.saving_pct or 0.0), r.goal.block_bytes))
    return reports
