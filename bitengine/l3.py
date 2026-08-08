"""BitEngine L3 — the container and the representation buffer.

L1 encodes a block. L2 decides which base a block is encoded against. Neither
persists anything, and neither can answer "show me the state at block 9,000"
without replaying everything before it. L3 is the tier that makes the
representation addressable.

Two pieces:

**The container.** A `.bite` file: a fixed header, the payloads, and a block
index. The index is what separates this from a stream — it turns "decode block
i" from a linear replay into a seek. It is written last, because the payload
lengths are not known until the payloads exist, and the header records where it
went so a reader finds it in one seek.

**The buffer.** `Reader` keeps a bounded LRU of reconstructed blocks. Repeatedly
asking for nearby blocks — which is what scrubbing a timeline or panning a diff
actually does — then costs nothing after the first walk.

The honest limit, which is the point of the keyframe machinery in L2:

    PAIRED     block i needs reference block i.            depth 1
    ANCHOR     block i needs block 0.                      depth 2
    PRECEDING  block i needs block i-stride, recursively.  depth i/stride

Without keyframes a PRECEDING container is a linked list, and random access to
its tail means decoding its whole head. `experiments/ccp/FINDINGS.md` §3 flags
exactly this and declines to claim random access as a result. With
`--keyframe-interval N` the walk is bounded by N, the cost is visible in the
savings figure, and `Reader.chain_length()` reports it so the claim can be
checked rather than believed.

Everything a container holds is untrusted on read. Every offset, length and
count is validated against the file size before it is used, and a container that
fails validation raises rather than returning partial data.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

import l1
import l2

__all__ = [
    "ENGINE_VERSION",
    "MAGIC",
    "VERSION",
    "MAX_CHAIN_WALK",
    "ContainerError",
    "Manifest",
    "write_container",
    "Reader",
    "read_container",
]

# Its own literal — see the note in l2.py. Distinct from `VERSION` below, which
# is the on-disk container format: a `.bite` file written last year must still
# open, so that number moves only when the byte layout does.
ENGINE_VERSION = "1.1"


class EngineMismatch(RuntimeError):
    """`l1`, `l2` and `l3` on this machine are not one consistent set."""


def check_engine_modules() -> None:
    """Refuse to import against a mixed set of engine modules.

    Copying one of these files onto a machine and leaving the others is the most
    likely way this project breaks in the field, and it stays invisible until
    something well below the call site raises about a keyword argument it never
    named. L3 is where the check goes because it is the only tier that imports
    the other two, so every entry point — the CLI and the dashboard alike —
    inherits it without either having to remember to ask.

    The worst outcome this prevents is not the exception. It is a mixed set
    encoding a container that no other build can decode.
    """
    found = {module.__name__: getattr(module, "ENGINE_VERSION", None) for module in (l1, l2)}
    found["l3"] = ENGINE_VERSION
    stale = {name: value for name, value in found.items() if value != ENGINE_VERSION}
    if stale:
        detail = ", ".join(
            f"{name}.py is {value or 'from before version markers existed'}"
            for name, value in sorted(stale.items())
        )
        raise EngineMismatch(
            f"the engine modules do not match: {detail}, but this build is {ENGINE_VERSION} "
            f"throughout. l1.py, l2.py and l3.py are one unit and must be copied together — "
            f"or run `python3 bundle.py`, which packages them in one step."
        )


check_engine_modules()

MAGIC = b"BITE"
VERSION = 1

# magic, version, flags, block_bytes, total_bytes, index_offset, block_count,
# strategy, stride, codec_mask, keyframe_interval, sha256, goal name
_HEADER = struct.Struct("<4sBBIQQIBIBI32s32s")
_LENGTH = struct.Struct("<I")

_STRATEGY_IDS: dict[str, int] = {l2.PAIRED: 0, l2.PRECEDING: 1, l2.ANCHOR: 2}
_STRATEGY_NAMES: dict[int, str] = {v: k for k, v in _STRATEGY_IDS.items()}

# How far back `Reader.block()` will walk a base chain before refusing. A
# container built without keyframes can require an unbounded walk, and silently
# spending minutes on what looks like a random access is worse than saying so.
MAX_CHAIN_WALK = 4096

# Reconstructed blocks held by a Reader. At the 64KB default this is 8MB, which
# is what makes repeated nearby access free without the buffer becoming the
# memory bound the streaming tiers were built to avoid.
DEFAULT_CACHE_BLOCKS = 128


class ContainerError(ValueError):
    """A container is malformed, truncated, or not a BitEngine container."""


@dataclass(frozen=True)
class Manifest:
    """Everything needed to decode a container, and nothing that isn't."""

    goal: l2.Goal
    total_bytes: int
    block_count: int
    sha256: bytes
    index_offset: int
    container_bytes: int

    # Set when the container was just written, `None` when it was read back. A
    # container records what the blocks cost, not how many bytes differed, so a
    # reader genuinely does not know these — and saying `None` is the honest
    # answer rather than reconstructing a plausible zero.
    block_savings: l1.Savings | None = None
    codec_counts: dict[str, int] | None = None

    @property
    def saving_pct(self) -> float | None:
        """What the container saved against the original, or None if empty.

        Deliberately not an `l1.Savings`: a manifest does not record how many
        bytes changed, and constructing one with `changed_bytes=0` would report
        a driver that was never measured as if it had been.
        """
        if self.total_bytes == 0:
            return None
        return (self.total_bytes - self.container_bytes) / self.total_bytes * 100.0


def _codec_mask(codecs: frozenset[int]) -> int:
    mask = 0
    for codec in codecs:
        mask |= 1 << codec
    return mask


def _codecs_from_mask(mask: int) -> frozenset[int]:
    return frozenset(codec for codec in l1.CODEC_NAMES if mask & (1 << codec))


def write_container(
    destination: BinaryIO,
    source: BinaryIO | Iterable[bytes],
    goal: l2.Goal,
    reference: BinaryIO | Iterable[bytes] | None = None,
) -> Manifest:
    """Encode a stream into a container, streaming, and return its manifest.

    `destination` must be seekable: the header is written twice, once as a
    placeholder and once with the totals that are only known at the end. Nothing
    else is buffered — payload lengths accumulate, and at four bytes per block a
    1TB input at 64KB blocks costs 64MB of index, which is the one term here
    that grows with the input.
    """
    if not destination.seekable():
        raise ContainerError("destination must be seekable; the header is finalised after the payloads")

    header_at = destination.tell()
    destination.write(bytes(_HEADER.size))

    digest = hashlib.sha256()
    lengths: list[int] = []
    total_bytes = 0
    accumulator = l1.SavingsAccumulator()

    # `source` is teed through the digest as it is consumed, so the input is
    # read exactly once even though both the hash and the encoder need it.
    def hashed(stream: BinaryIO | Iterable[bytes]) -> Iterator[bytes]:
        nonlocal total_bytes
        for block in l2.iter_blocks(stream, goal.block_bytes):
            digest.update(block)
            total_bytes += len(block)
            yield block

    for encoded in l2.encode_stream(hashed(source), goal, reference):
        destination.write(encoded.payload)
        lengths.append(len(encoded.payload))
        accumulator.add(encoded.plan)

    index_offset = destination.tell()
    index = bytearray()
    for length in lengths:
        index += _LENGTH.pack(length)
    destination.write(index)
    container_bytes = destination.tell() - header_at

    destination.seek(header_at)
    destination.write(
        _HEADER.pack(
            MAGIC,
            VERSION,
            0,
            goal.block_bytes,
            total_bytes,
            index_offset,
            len(lengths),
            _STRATEGY_IDS[goal.base],
            goal.stride_blocks,
            _codec_mask(goal.codecs),
            goal.keyframe_interval,
            digest.digest(),
            goal.name.encode("utf-8")[:32].ljust(32, b"\x00"),
        )
    )
    destination.seek(header_at + container_bytes)

    return Manifest(
        goal=goal,
        total_bytes=total_bytes,
        block_count=len(lengths),
        sha256=digest.digest(),
        index_offset=index_offset,
        container_bytes=container_bytes,
        block_savings=accumulator.result() if lengths else None,
        codec_counts={l1.CODEC_NAMES[c]: n for c, n in sorted(accumulator.by_codec.items())},
    )


class Reader:
    """Random and sequential access to a container, with a bounded block cache.

    Opens with two reads — the header and the index — regardless of container
    size. Payloads are read on demand.
    """

    __slots__ = ("_file", "_owned", "_manifest", "_offsets", "_lengths", "_cache", "_cache_blocks", "_reference")

    def __init__(
        self,
        file: BinaryIO,
        reference: BinaryIO | None = None,
        cache_blocks: int = DEFAULT_CACHE_BLOCKS,
        owned: bool = False,
    ) -> None:
        if cache_blocks < 1:
            raise ValueError("cache_blocks must be at least 1")
        self._file = file
        self._owned = owned
        self._reference = reference
        self._cache_blocks = cache_blocks
        self._cache: OrderedDict[int, bytes] = OrderedDict()

        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        if file_size < _HEADER.size:
            raise ContainerError("file is shorter than a container header")

        file.seek(0)
        fields = _HEADER.unpack(file.read(_HEADER.size))
        (
            magic,
            version,
            _flags,
            block_bytes,
            total_bytes,
            index_offset,
            block_count,
            strategy,
            stride,
            codec_mask,
            keyframe_interval,
            digest,
            raw_name,
        ) = fields

        if magic != MAGIC:
            raise ContainerError(f"not a BitEngine container (magic {magic!r})")
        if version != VERSION:
            raise ContainerError(f"unsupported container version {version}")
        if strategy not in _STRATEGY_NAMES:
            raise ContainerError(f"unknown base strategy id {strategy}")

        # Every quantity below is attacker-controlled. Validate before allocating.
        index_bytes = block_count * _LENGTH.size
        if index_offset < _HEADER.size or index_offset > file_size:
            raise ContainerError("index offset outside the file")
        if index_offset + index_bytes > file_size:
            raise ContainerError("index extends past the end of the file")
        if block_count > l2.MAX_BLOCKS:
            raise ContainerError(f"block count {block_count} over the {l2.MAX_BLOCKS} limit")

        codecs = _codecs_from_mask(codec_mask)
        if not codecs:
            raise ContainerError("container permits no codecs")

        try:
            goal = l2.Goal(
                name=raw_name.rstrip(b"\x00").decode("utf-8", "replace") or "container",
                summary="restored from a container",
                block_bytes=block_bytes,
                base=_STRATEGY_NAMES[strategy],
                codecs=codecs,
                stride_blocks=stride,
                keyframe_interval=keyframe_interval,
            )
        except ValueError as exc:
            raise ContainerError(f"container header describes an invalid goal: {exc}") from exc

        file.seek(index_offset)
        index = file.read(index_bytes)
        if len(index) != index_bytes:
            raise ContainerError("index is truncated")

        offsets: list[int] = []
        lengths: list[int] = []
        cursor = _HEADER.size
        for i in range(block_count):
            (length,) = _LENGTH.unpack_from(index, i * _LENGTH.size)
            # A payload is at most the block plus its codec tag; anything larger
            # is a corrupt index, not a large block.
            if length < 1 or length > block_bytes + 1:
                raise ContainerError(f"block {i} declares an impossible length {length}")
            if cursor + length > index_offset:
                raise ContainerError(f"block {i} overruns the payload region")
            offsets.append(cursor)
            lengths.append(length)
            cursor += length

        self._offsets = offsets
        self._lengths = lengths
        self._manifest = Manifest(
            goal=goal,
            total_bytes=total_bytes,
            block_count=block_count,
            sha256=digest,
            index_offset=index_offset,
            container_bytes=file_size,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owned:
            self._file.close()

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    @property
    def goal(self) -> l2.Goal:
        return self._manifest.goal

    # -- access ------------------------------------------------------------

    def block_length(self, index: int) -> int:
        """Length of the reconstructed block, which the final one may shorten."""
        goal = self.goal
        remaining = self._manifest.total_bytes - index * goal.block_bytes
        return max(0, min(goal.block_bytes, remaining))

    def payload(self, index: int) -> bytes:
        if not 0 <= index < self._manifest.block_count:
            raise IndexError(f"block {index} outside 0..{self._manifest.block_count - 1}")
        self._file.seek(self._offsets[index])
        data = self._file.read(self._lengths[index])
        if len(data) != self._lengths[index]:
            raise ContainerError(f"block {index} is truncated on disk")
        return data

    def chain_length(self, index: int) -> int:
        """Blocks that must be reconstructed to reach `index`, cache included.

        Reports the real cost of a random access so the "instant" claim can be
        checked. A goal with keyframes bounds this; one without does not.
        """
        goal = self.goal
        if index in self._cache:
            return 0
        if goal.base == l2.PAIRED:
            return 1
        if goal.base == l2.ANCHOR:
            return 1 if index == 0 or 0 in self._cache else 2

        depth = 0
        cursor = index
        for _ in range(MAX_CHAIN_WALK):
            depth += 1
            if goal.is_keyframe(cursor):
                return depth
            previous = cursor - goal.stride_blocks
            if previous < 0 or previous in self._cache:
                return depth
            cursor = previous
        return depth

    def _base_for(self, index: int) -> bytes:
        """The base block `index` was encoded against, reconstructing it if needed."""
        goal = self.goal
        length = self.block_length(index)

        if goal.is_keyframe(index):
            return bytes(length)

        if goal.base == l2.PAIRED:
            if self._reference is None:
                raise ContainerError("this container was built against a reference stream; supply it")
            self._reference.seek(index * goal.block_bytes)
            candidate = self._reference.read(goal.block_bytes)
        elif goal.base == l2.ANCHOR:
            candidate = self.block(0) if index > 0 else b""
        else:
            previous = index - goal.stride_blocks
            candidate = self.block(previous) if previous >= 0 else b""

        if not candidate:
            return bytes(length)
        if len(candidate) < length:
            return candidate + bytes(length - len(candidate))
        return candidate[:length]

    def block(self, index: int) -> bytes:
        """Reconstruct one block, using and populating the buffer.

        The base chain is walked iteratively rather than recursively: a
        PRECEDING container without keyframes can be tens of thousands of blocks
        deep, which recursion would turn into a stack overflow instead of a
        clear error.
        """
        if not 0 <= index < self._manifest.block_count:
            raise IndexError(f"block {index} outside 0..{self._manifest.block_count - 1}")

        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached

        goal = self.goal
        pending: list[int] = []
        cursor = index
        for _ in range(MAX_CHAIN_WALK):
            if cursor in self._cache or goal.is_keyframe(cursor) or goal.base != l2.PRECEDING:
                break
            previous = cursor - goal.stride_blocks
            if previous < 0:
                break
            pending.append(cursor)
            cursor = previous
        else:
            raise ContainerError(
                f"reaching block {index} needs a chain deeper than {MAX_CHAIN_WALK}; "
                "repack with a keyframe interval to make random access bounded"
            )

        # Deepest first, so each reconstruction finds its base already cached.
        for target in (cursor, *reversed(pending)):
            if target in self._cache:
                continue
            block = l1.decode_block(self._base_for(target), self.payload(target))
            expected = self.block_length(target)
            if len(block) != expected:
                raise ContainerError(f"block {target} decoded to {len(block)} bytes, expected {expected}")
            self._store(target, block)

        return self._cache[index]

    def _store(self, index: int, block: bytes) -> None:
        self._cache[index] = block
        self._cache.move_to_end(index)
        while len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)

    def blocks(self) -> Iterator[bytes]:
        """Every block in order. Sequential, so the chain is never walked twice."""
        for index in range(self._manifest.block_count):
            yield self.block(index)

    def verify(self) -> bool:
        """Whether the container reconstructs to the SHA-256 it recorded."""
        digest = hashlib.sha256()
        for block in self.blocks():
            digest.update(block)
        return digest.digest() == self._manifest.sha256

    def block_table(self) -> list[tuple[int, int, int, int]]:
        """Per block: index, codec id, stored payload bytes, reconstructed bytes.

        Reads one tag byte per block and decodes nothing, so a breakdown can be
        drawn for a container far too large to decode. This is what makes the
        per-block view in the UI honest — it reports what is actually on disk
        rather than a model of it.
        """
        table: list[tuple[int, int, int, int]] = []
        for index in range(self._manifest.block_count):
            self._file.seek(self._offsets[index])
            tag = self._file.read(1)
            if not tag:
                raise ContainerError(f"block {index} is truncated on disk")
            if tag[0] not in l1.CODEC_NAMES:
                raise ContainerError(f"block {index} has unknown codec id {tag[0]}")
            table.append((index, tag[0], self._lengths[index], self.block_length(index)))
        return table

    def codec_histogram(self) -> dict[str, int]:
        """Codec of every stored block, read from the payload tags alone.

        Costs one seek per block and no decoding, so `inspect` stays cheap on a
        container far too large to decode.
        """
        counts: dict[str, int] = {}
        for index in range(self._manifest.block_count):
            self._file.seek(self._offsets[index])
            tag = self._file.read(1)
            if not tag:
                raise ContainerError(f"block {index} is truncated on disk")
            name = l1.CODEC_NAMES.get(tag[0])
            if name is None:
                raise ContainerError(f"block {index} has unknown codec id {tag[0]}")
            counts[name] = counts.get(name, 0) + 1
        return counts


def read_container(
    path: str, reference_path: str | None = None, cache_blocks: int = DEFAULT_CACHE_BLOCKS
) -> Reader:
    """Open a container by path. The caller closes the Reader."""
    handle = open(path, "rb")  # noqa: SIM115 — ownership passes to the Reader
    try:
        reference = open(reference_path, "rb") if reference_path else None  # noqa: SIM115
        return Reader(handle, reference=reference, cache_blocks=cache_blocks, owned=True)
    except BaseException:
        handle.close()
        raise
