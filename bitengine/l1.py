"""BitEngine L1 — the block-level bitwise delta engine.

L1 is the innermost tier: it transforms exactly one fixed-size block against one
base block and reports what that cost. It holds no files, no policy and no
state. L2 (goal → algorithm matching) and L3 (the representation buffer) are
built on top of it and are not in this module.

The single design decision in here is that L1 is *not* one delta format. It is a
family of codecs costed in closed form from three statistics of the XOR
residual, with the cheapest one chosen per block.

That choice is a direct response to a measured failure. `experiments/ccp`
implements a single sparse (position, value) delta, and `experiments/ccp/
FINDINGS.md` §2 records where it breaks:

    "a delta pays a fixed ~3 bytes for every changed byte, so cost grows
    linearly with the number of changes and crosses break-even at roughly a
    third of the region [...] At 10% divergence LZMA still returns 48.64%
    while CCP collapses to 1.22%."

A sparse delta stops paying at k/n = 1/3 because each entry carries a position.
A bitmap delta carries position implicitly — one bit per block byte — so it
stops paying at k/n = 1 - 1/8, i.e. 87.5%. Selecting between them per block
moves the cliff from 33% to 87.5% of the block changed and costs one extra byte
per block to record which codec was used. `MEASURED` in the module docstring of
tests/test_l1.py pins the numbers.

Codecs, all XOR-based so that reconstruction is target = base ^ residual:

    IDENTICAL  no payload                          k == 0
    SPARSE     count, positions, values            k small
    RUNS       count, (gap, length)*, values       changes clustered
    BITMAP     1 bit per byte, then values         k large and scattered
    RAW        the target block verbatim           no exploitable structure

RAW is always available, so `encode_block` never exceeds the block size by more
than the one-byte codec tag. That bound is what makes the engine safe to run
over arbitrary data: the negative control cannot blow up.

Guarantees this module is written to hold, since they are the project's stated
constraints:

*   No unbounded loop. Every loop is either a regex scan over a block whose
    length is checked against `MAX_BLOCK_BYTES` on entry, or a `range()` over a
    count that was validated against the block length before it was used.
*   No allocation proportional to attacker-controlled input. Decoders validate
    every count and offset against the block length *before* allocating.
*   O(n) per block, single pass for statistics, single pass to encode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ENGINE_VERSION",
    "MAX_BLOCK_BYTES",
    "CODEC_IDENTICAL",
    "CODEC_SPARSE",
    "CODEC_RUNS",
    "CODEC_BITMAP",
    "CODEC_RAW",
    "CODEC_NAMES",
    "CorruptBlock",
    "ResidualStats",
    "BlockPlan",
    "Savings",
    "SavingsAccumulator",
    "position_width",
    "resolve_allowed",
    "xor_bytes",
    "residual_stats",
    "codec_costs",
    "plan_block",
    "encode_block",
    "decode_block",
    "savings",
]


# Bumped when the shape of anything crossing a module boundary changes — a Goal
# field, a container field, a function signature another tier calls. `l1`, `l2`
# and `l3` are one unit and are only ever correct at the same value; copying one
# of them onto an older machine and leaving the others is the failure this
# catches. See `check_engine_modules` in webui.py.
ENGINE_VERSION = "1.1"

# 4 MiB. Chosen as a hard ceiling rather than an operating point: the streaming
# tiers work at 4-64 KiB, and this exists so that a malformed length can never
# make L1 allocate an unbounded buffer. Raising it is a security decision.
MAX_BLOCK_BYTES = 4 * 1024 * 1024

CODEC_IDENTICAL = 0
CODEC_SPARSE = 1
CODEC_RUNS = 2
CODEC_BITMAP = 3
CODEC_RAW = 4

CODEC_NAMES: dict[int, str] = {
    CODEC_IDENTICAL: "identical",
    CODEC_SPARSE: "sparse",
    CODEC_RUNS: "runs",
    CODEC_BITMAP: "bitmap",
    CODEC_RAW: "raw",
}

# RAW reconstructs without consulting a base. On an exact cost tie that
# independence is free value — the block stops being coupled to a base, which is
# what lets L3 serve it without a chain walk — so ties resolve to RAW.
_BASE_FREE = {CODEC_RAW}

# One nonzero byte, and one maximal run of nonzero bytes. Both scans run in C
# over a bounded block; the Python-level iteration count is the number of
# matches, never the block length.
_NONZERO = re.compile(rb"[^\x00]")
_NONZERO_RUN = re.compile(rb"[^\x00]+")

# 0 stays 0, every other byte becomes 1. Turns the residual into a per-byte
# changed-flag array in one C-level pass.
_ONE_IF_NONZERO = bytes([0] + [1] * 255)

# Set bit offsets of every possible bitmap byte, most significant bit first, so
# the scatter path can place a byte's worth of changes without constructing a
# match object per change.
_SET_BITS: tuple[tuple[int, ...], ...] = tuple(
    tuple(bit for bit in range(8) if (byte >> (7 - bit)) & 1) for byte in range(256)
)

# Relative cost of advancing one run against advancing one bitmap byte during
# bitmap decode, from the measurement quoted in `_decode_bitmap`. It selects
# between two strategies that produce identical output, so an inaccurate value
# costs throughput and can never change what is decoded.
_RUN_WALK_COST = 7



class CorruptBlock(ValueError):
    """An encoded block is malformed, truncated or internally inconsistent.

    Every decode path raises this rather than returning partial output. A block
    that cannot be reconstructed exactly is not a result.
    """


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def position_width(block_size: int) -> int:
    """Bytes needed to hold any count, offset or length within a block.

    Sized against `block_size` inclusive, not `block_size - 1`, because run
    lengths and entry counts can equal the block length while offsets cannot.
    """
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    return max(1, (block_size.bit_length() + 7) // 8)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """Bytewise XOR of two equal-length buffers.

    Routed through one big-integer XOR rather than a per-byte loop: CPython
    performs it as a single C-level operation over the whole buffer, which is
    the reason the engine can stay in pure Python and still stream.
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return b""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")


def _pack_uint(value: int, width: int) -> bytes:
    return value.to_bytes(width, "big")


def _read_uint(buf: bytes, offset: int, width: int) -> int:
    if offset + width > len(buf):
        raise CorruptBlock("truncated integer field")
    return int.from_bytes(buf[offset : offset + width], "big")


def _check_block_size(n: int) -> None:
    if n == 0:
        raise ValueError("block must not be empty")
    if n > MAX_BLOCK_BYTES:
        raise ValueError(f"block of {n} bytes exceeds MAX_BLOCK_BYTES ({MAX_BLOCK_BYTES})")


# ---------------------------------------------------------------------------
# statistics — one pass, everything the cost model needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResidualStats:
    """What one XOR residual contains, in the terms the cost model uses.

    `changed_bits` is the true bit-level difference — the popcount of the
    residual. It is reported because it is the quantity the product talks about,
    but it is deliberately *not* an input to any cost formula: every codec here
    is byte-addressed, so a block with one changed bit costs the same as a block
    with eight changed bits in the same byte. Keeping the two apart is what
    stops the savings figure from being quietly optimistic.
    """

    block_bytes: int
    changed_bytes: int
    changed_bits: int
    runs: int


def residual_stats(residual: bytes) -> ResidualStats:
    """Summarise a residual in O(n) with no Python-level loop at all.

    Every quantity here is one big-integer operation over the whole block.
    Counting runs by iterating matches was measured at 13% of encode time on a
    densely scattered block; the shift-and-mask below replaces that loop with
    two `bit_count()` calls and does not grow with the number of runs.
    """
    n = len(residual)
    _check_block_size(n)

    flags = residual.translate(_ONE_IF_NONZERO)
    present = int.from_bytes(flags, "big")
    if present == 0:
        return ResidualStats(block_bytes=n, changed_bytes=0, changed_bits=0, runs=0)

    # flags holds 0 or 1 per byte, so its popcount is the number of changed
    # bytes, while the residual's own popcount is the true bit-level difference.
    changed_bytes = present.bit_count()
    changed_bits = int.from_bytes(residual, "big").bit_count()

    # A run starts at every position that is set and whose predecessor is not.
    # Shifting the flag array one byte right gives the predecessor of each
    # position, and `present & ~previous` — written without `~` because Python
    # integers are unbounded and would sign-extend — isolates the run starts.
    previous = present >> 8
    runs = (present ^ (present & previous)).bit_count()

    return ResidualStats(
        block_bytes=n,
        changed_bytes=changed_bytes,
        changed_bits=changed_bits,
        runs=runs,
    )


# ---------------------------------------------------------------------------
# cost model — closed form, so no codec is ever trial-encoded
# ---------------------------------------------------------------------------


def codec_costs(stats: ResidualStats) -> dict[int, int]:
    """Exact encoded size in bytes for every applicable codec.

    These are not estimates. `encode_block` asserts the byte it produces matches
    the number predicted here, so a divergence surfaces as a failing assertion
    rather than as a silently wrong savings figure.

    Every cost includes the one-byte codec tag, so the values are directly
    comparable and the minimum is the real winner.
    """
    n = stats.block_bytes
    k = stats.changed_bytes
    r = stats.runs
    w = position_width(n)

    costs: dict[int, int] = {}

    if k == 0:
        costs[CODEC_IDENTICAL] = 1

    # tag + count + k positions + k values
    costs[CODEC_SPARSE] = 1 + w + k * (w + 1)

    # tag + run count + r (gap, length) pairs + k values
    costs[CODEC_RUNS] = 1 + w + r * (2 * w) + k

    # tag + one bit per block byte + k values
    costs[CODEC_BITMAP] = 1 + (n + 7) // 8 + k

    # tag + the block itself. Always available, which is what bounds the engine.
    costs[CODEC_RAW] = 1 + n

    return costs


@dataclass(frozen=True)
class BlockPlan:
    """The decision for one block, with the drivers that produced it.

    `costs` is carried deliberately: a composite choice that reports only its
    winner cannot be audited, and L2 needs the runner-up to explain why it
    picked a goal's algorithm over the default.
    """

    codec: int
    encoded_bytes: int
    stats: ResidualStats
    costs: dict[int, int]

    @property
    def codec_name(self) -> str:
        return CODEC_NAMES[self.codec]


def _choose(costs: dict[int, int], allowed: frozenset[int] | None) -> int:
    # Cost first; base-independence second; codec id last, purely so the choice
    # is total and reproducible. Determinism matters here — the same input must
    # always produce the same container, or round-trip tests cannot be trusted.
    permitted = costs if allowed is None else {c: v for c, v in costs.items() if c in allowed}
    return min(permitted, key=lambda c: (permitted[c], 0 if c in _BASE_FREE else 1, c))


def resolve_allowed(allowed: frozenset[int] | None) -> frozenset[int] | None:
    """Normalise a codec restriction, forcing in the two that must never be barred.

    RAW is what bounds the encoder — without it a block could be forced into a
    representation larger than itself. IDENTICAL costs one byte and is never not
    the right answer for an unchanged block. A policy that excluded either would
    be a bug in the policy, so L1 refuses to honour it rather than trusting the
    caller.
    """
    if allowed is None:
        return None
    resolved = frozenset(allowed) | {CODEC_IDENTICAL, CODEC_RAW}
    unknown = resolved - set(CODEC_NAMES)
    if unknown:
        raise ValueError(f"unknown codec ids: {sorted(unknown)}")
    return resolved


def plan_block(base: bytes, target: bytes, allowed: frozenset[int] | None = None) -> BlockPlan:
    """Decide how one block should be stored, without encoding it.

    This is the L1/L2 seam: L2 can cost a whole stream, or compare two candidate
    bases, without paying for encoding it will then discard. `allowed` is how a
    caller expresses a policy — restricting the codec set trades saving for a
    property L1 cannot see, such as decode speed.

    `costs` on the returned plan always reports every codec, including barred
    ones, so the cost of a policy stays visible rather than being hidden by it.
    """
    if len(base) != len(target):
        raise ValueError(f"base and target differ in length: {len(base)} vs {len(target)}")
    _check_block_size(len(target))

    stats = residual_stats(xor_bytes(base, target))
    costs = codec_costs(stats)
    codec = _choose(costs, resolve_allowed(allowed))
    return BlockPlan(codec=codec, encoded_bytes=costs[codec], stats=stats, costs=costs)


# ---------------------------------------------------------------------------
# encode
# ---------------------------------------------------------------------------


def _encode_sparse(residual: bytes, w: int) -> bytes:
    positions = [m.start() for m in _NONZERO.finditer(residual)]
    out = bytearray()
    out.append(CODEC_SPARSE)
    out += _pack_uint(len(positions), w)
    for pos in positions:
        out += _pack_uint(pos, w)
    out += residual.translate(None, b"\x00")
    return bytes(out)


def _encode_runs(residual: bytes, w: int) -> bytes:
    out = bytearray()
    out.append(CODEC_RUNS)

    spans = [m.span() for m in _NONZERO_RUN.finditer(residual)]
    out += _pack_uint(len(spans), w)

    # Gaps are relative to the end of the previous run, so every field stays
    # within `w` bytes regardless of where in the block the run sits.
    previous_end = 0
    for start, end in spans:
        out += _pack_uint(start - previous_end, w)
        out += _pack_uint(end - start, w)
        previous_end = end

    out += residual.translate(None, b"\x00")
    return bytes(out)


def _pack_bitmap(flags: bytes) -> bytes:
    """Pack 0/1 flag bytes into one bit each. `flags` length must be a multiple of 8.

    Lane t of the stride-8 decomposition contributes bit (7 - t) of every output
    byte, and the lanes occupy disjoint bits, so eight strided slices and eight
    shifts assemble the whole bitmap with no carries and no Python-level loop
    over the block. The obvious `for i in range(0, n, 8)` version was measured
    as the encode bottleneck on bitmap-coded blocks.
    """
    m = len(flags) // 8
    if m == 0:
        return b""
    packed = 0
    for lane in range(8):
        packed |= int.from_bytes(flags[lane::8], "big") << (7 - lane)
    return packed.to_bytes(m, "big")


def _unpack_bitmap(bitmap: bytes, n: int) -> bytes:
    """Inverse of `_pack_bitmap`, truncated back to `n` flag bytes."""
    m = len(bitmap)
    if m == 0:
        return b""
    packed = int.from_bytes(bitmap, "big")
    lane_mask = int.from_bytes(b"\x01" * m, "big")
    flags = bytearray(m * 8)
    for lane in range(8):
        flags[lane::8] = ((packed >> (7 - lane)) & lane_mask).to_bytes(m, "big")
    return bytes(flags[:n])


def _encode_bitmap(residual: bytes, n: int) -> bytes:
    flags = residual.translate(_ONE_IF_NONZERO)
    padding = (-n) % 8
    if padding:
        flags += b"\x00" * padding

    out = bytearray()
    out.append(CODEC_BITMAP)
    out += _pack_bitmap(flags)
    out += residual.translate(None, b"\x00")
    return bytes(out)


def encode_block(base: bytes, target: bytes, allowed: frozenset[int] | None = None) -> bytes:
    """Encode one block against one base, choosing the cheapest representation.

    The result is self-describing: `decode_block(base, encode_block(base, t))`
    returns `t` exactly, for any `base` and `t` of equal length, and regardless
    of which codecs `allowed` permitted — the policy affects size, never
    correctness, and the decoder needs no knowledge of it.
    """
    plan = plan_block(base, target, allowed)
    n = len(target)
    w = position_width(n)

    if plan.codec == CODEC_IDENTICAL:
        encoded = bytes([CODEC_IDENTICAL])
    elif plan.codec == CODEC_RAW:
        encoded = bytes([CODEC_RAW]) + target
    else:
        residual = xor_bytes(base, target)
        if plan.codec == CODEC_SPARSE:
            encoded = _encode_sparse(residual, w)
        elif plan.codec == CODEC_RUNS:
            encoded = _encode_runs(residual, w)
        else:
            encoded = _encode_bitmap(residual, n)

    # The cost model is what every savings figure is computed from, so it is
    # checked against reality on every single block rather than in a test only.
    if len(encoded) != plan.encoded_bytes:
        raise AssertionError(
            f"cost model disagrees with encoder for {plan.codec_name}: "
            f"predicted {plan.encoded_bytes}, produced {len(encoded)}"
        )
    return encoded


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------


def _decode_sparse(payload: bytes, n: int, w: int) -> bytes:
    count = _read_uint(payload, 0, w)
    # Validated before it is used to size anything: a block cannot contain more
    # changed bytes than it has bytes.
    if count > n:
        raise CorruptBlock(f"sparse count {count} exceeds block size {n}")

    values_at = w + count * w
    if len(payload) != values_at + count:
        raise CorruptBlock("sparse payload length does not match its count")

    residual = bytearray(n)
    previous = -1
    for i in range(count):
        pos = _read_uint(payload, w + i * w, w)
        if pos >= n:
            raise CorruptBlock(f"sparse position {pos} outside block of {n}")
        if pos <= previous:
            raise CorruptBlock("sparse positions are not strictly ascending")
        value = payload[values_at + i]
        if value == 0:
            raise CorruptBlock("sparse entry carries a zero delta")
        residual[pos] = value
        previous = pos

    return bytes(residual)


def _decode_runs(payload: bytes, n: int, w: int) -> bytes:
    count = _read_uint(payload, 0, w)
    # A block of n bytes cannot hold more than ceil(n/2) maximal nonzero runs.
    if count > (n + 1) // 2:
        raise CorruptBlock(f"run count {count} impossible for block of {n}")

    values_at = w + count * 2 * w
    if len(payload) < values_at:
        raise CorruptBlock("truncated run table")

    residual = bytearray(n)
    values = payload[values_at:]
    if b"\x00" in values:
        raise CorruptBlock("run carries a zero delta")

    available = len(values)
    cursor = 0
    consumed = 0
    for i in range(count):
        gap = _read_uint(payload, w + i * 2 * w, w)
        length = _read_uint(payload, w + i * 2 * w + w, w)
        if length == 0:
            raise CorruptBlock("zero-length run")
        start = cursor + gap
        end = start + length
        if end > n:
            raise CorruptBlock(f"run [{start}:{end}] outside block of {n}")
        nxt = consumed + length
        if nxt > available:
            raise CorruptBlock("run table demands more values than are present")
        residual[start:end] = values[consumed:nxt]
        cursor = end
        consumed = nxt

    if consumed != available:
        raise CorruptBlock("trailing values after the run table")
    return bytes(residual)


def _decode_bitmap(payload: bytes, n: int) -> bytes:
    bitmap_bytes = (n + 7) // 8
    if len(payload) < bitmap_bytes:
        raise CorruptBlock("truncated bitmap")

    bitmap = payload[:bitmap_bytes]
    values = payload[bitmap_bytes:]
    packed = int.from_bytes(bitmap, "big")

    # Bits past the end of the block must be clear, or the run scan below could
    # be steered into writing outside the block.
    tail = (-n) % 8
    if tail and packed & ((1 << tail) - 1):
        raise CorruptBlock("bitmap sets bits past the end of the block")

    # Positions occupy consecutive bits, so the whole bitmap can be characterised
    # with three big-integer operations: how many bytes changed, and how many
    # maximal runs they form.
    changed = packed.bit_count()
    runs = (packed ^ (packed & (packed >> 1))).bit_count()

    if changed != len(values):
        raise CorruptBlock(f"bitmap marks {changed} changes but carries {len(values)} values")
    # One scan for the whole value block rather than one per run. A zero delta is
    # a no-op no well-formed encoder emits, and accepting one would give a block
    # two valid encodings.
    if b"\x00" in values:
        raise CorruptBlock("bitmap carries a zero delta")

    residual = bytearray(n)

    # Two scatter strategies with opposite strengths, measured on 64KB blocks:
    # walking runs costs ~0.44us per run, walking bitmap bytes costs ~0.06us per
    # byte-or-value. Long runs favour the first (139 MB/s vs 35 MB/s on one
    # contiguous patch), isolated changes favour the second (32 MB/s vs 6 MB/s
    # on maximally scattered bytes). The ratio of those constants is the
    # threshold, and both paths produce identical output.
    if runs * _RUN_WALK_COST <= bitmap_bytes + changed:
        flags = _unpack_bitmap(bitmap, n)
        consumed = 0
        for match in _NONZERO_RUN.finditer(flags):
            start = match.start()
            end = match.end()
            nxt = consumed + end - start
            residual[start:end] = values[consumed:nxt]
            consumed = nxt
    else:
        consumed = 0
        offset = 0
        for byte in bitmap:
            if byte:
                for bit in _SET_BITS[byte]:
                    residual[offset + bit] = values[consumed]
                    consumed += 1
            offset += 8

    return bytes(residual)


def decode_block(base: bytes, encoded: bytes) -> bytes:
    """Reconstruct a block from its base and its encoded form.

    Treats `encoded` as untrusted. Every length, count and offset is checked
    against the base length before it is used, and any inconsistency raises
    `CorruptBlock` rather than producing a partially reconstructed block.
    """
    n = len(base)
    _check_block_size(n)
    if not encoded:
        raise CorruptBlock("empty block")

    codec = encoded[0]
    payload = encoded[1:]
    w = position_width(n)

    if codec == CODEC_IDENTICAL:
        if payload:
            raise CorruptBlock("identical block carries a payload")
        return base
    if codec == CODEC_RAW:
        if len(payload) != n:
            raise CorruptBlock(f"raw block is {len(payload)} bytes, expected {n}")
        return payload
    if codec == CODEC_SPARSE:
        residual = _decode_sparse(payload, n, w)
    elif codec == CODEC_RUNS:
        residual = _decode_runs(payload, n, w)
    elif codec == CODEC_BITMAP:
        residual = _decode_bitmap(payload, n)
    else:
        raise CorruptBlock(f"unknown codec id {codec}")

    return xor_bytes(base, residual)


# ---------------------------------------------------------------------------
# savings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Savings:
    """A savings figure together with every driver behind it.

    Two distinct percentages are reported, because conflating them is the
    easiest way to overstate this engine:

    `change_pct` is (change size / total size) * 100 — how much of the data
    actually differs. It is a property of the *data*.

    `saving_pct` is (1 - encoded / total) * 100 — what was actually saved once
    the representation has been paid for. It is a property of the *result*, and
    it is the only one of the two that a user's disk sees.

    They are never equal, because no encoding of k changed bytes costs exactly k
    bytes. `overhead_bytes` is the gap, and `saving_pct` is always the smaller
    claim. Any figure shown to a user must be `saving_pct`.
    """

    total_bytes: int
    encoded_bytes: int
    changed_bytes: int
    changed_bits: int
    overhead_bytes: int
    saving_pct: float | None
    change_pct: float | None

    @property
    def saved_bytes(self) -> int:
        return self.total_bytes - self.encoded_bytes


def savings(
    total_bytes: int,
    encoded_bytes: int,
    changed_bytes: int,
    changed_bits: int,
) -> Savings:
    """Compute a savings figure from measured byte counts.

    Returns `None` percentages when `total_bytes` is zero. There is no saving to
    report on nothing, and reporting 0.0% would be a plausible fake: a caller
    cannot tell it apart from a real measurement that saved nothing.
    """
    if total_bytes < 0 or encoded_bytes < 0 or changed_bytes < 0 or changed_bits < 0:
        raise ValueError("byte and bit counts must be non-negative")
    if changed_bytes > total_bytes:
        raise ValueError(f"changed_bytes {changed_bytes} exceeds total_bytes {total_bytes}")
    if changed_bits > total_bytes * 8:
        raise ValueError(f"changed_bits {changed_bits} exceeds total_bytes * 8")

    if total_bytes == 0:
        return Savings(
            total_bytes=0,
            encoded_bytes=encoded_bytes,
            changed_bytes=0,
            changed_bits=0,
            overhead_bytes=encoded_bytes,
            saving_pct=None,
            change_pct=None,
        )

    return Savings(
        total_bytes=total_bytes,
        encoded_bytes=encoded_bytes,
        changed_bytes=changed_bytes,
        changed_bits=changed_bits,
        overhead_bytes=encoded_bytes - changed_bytes,
        saving_pct=(total_bytes - encoded_bytes) / total_bytes * 100.0,
        change_pct=changed_bytes / total_bytes * 100.0,
    )


class SavingsAccumulator:
    """Running totals across a stream of blocks, in constant memory.

    The streaming tiers process a file in fixed-size blocks and must be able to
    report progress without retaining any of them, so this holds four integers
    and a codec tally and nothing else. It is the only stateful object in L1.
    """

    __slots__ = ("_total", "_encoded", "_changed_bytes", "_changed_bits", "_blocks", "_by_codec")

    def __init__(self) -> None:
        self._total = 0
        self._encoded = 0
        self._changed_bytes = 0
        self._changed_bits = 0
        self._blocks = 0
        self._by_codec: dict[int, int] = {}

    def add(self, plan: BlockPlan) -> None:
        self._total += plan.stats.block_bytes
        self._encoded += plan.encoded_bytes
        self._changed_bytes += plan.stats.changed_bytes
        self._changed_bits += plan.stats.changed_bits
        self._blocks += 1
        self._by_codec[plan.codec] = self._by_codec.get(plan.codec, 0) + 1

    @property
    def blocks(self) -> int:
        return self._blocks

    @property
    def by_codec(self) -> dict[int, int]:
        """Blocks stored under each codec — the drivers behind the total."""
        return dict(self._by_codec)

    def result(self) -> Savings:
        return savings(
            total_bytes=self._total,
            encoded_bytes=self._encoded,
            changed_bytes=self._changed_bytes,
            changed_bits=self._changed_bits,
        )
