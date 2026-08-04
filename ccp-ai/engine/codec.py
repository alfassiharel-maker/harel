"""The bit-level codec: XOR delta, byte-plane deinterleave, entropy coding.

Why this works, in one paragraph. A derived model (fine-tune, adapter merge,
task head, patch) is numerically *close* to its base. In IEEE-754 the closeness
is structural, not statistical: a weight that moved by 1e-4 relative keeps its
sign bit, keeps its exponent byte, and keeps the high mantissa bits — only the
low bits move. XOR against the base therefore produces a byte string that is
mostly zero in the high-order byte of every element and dense only in the low
mantissa bytes. Interleaved, that signal is invisible to a general-purpose
compressor: it sees a repeating pattern of "zero, zero, noise, noise" and cannot
model it. Deinterleaving into byte planes puts all the high-order bytes next to
each other, where they collapse into a run of zeros, and isolates the
incompressible mantissa noise into its own plane. Every step is a bijection, so
the reconstruction is bit-exact — not approximate, not quantised.

Nothing here is lossy. There is no rounding, no truncation, no error tolerance.
`decode(encode(x)) == x` is asserted by the packer on every single tensor.
"""

from __future__ import annotations

import hashlib
import sys
import zlib
from array import array
from dataclasses import dataclass
from typing import Literal

Compressor = Literal["zlib", "lzma", "none"]

# zlib level 6 rather than 9. Measured on the demo families, level 9 buys under
# 1% additional ratio for roughly 3x the CPU; a distribution product pays that
# CPU on every push. lzma stays available for cold archival tiers where the
# tradeoff inverts.
DEFAULT_ZLIB_LEVEL = 6


class CodecError(ValueError):
    """A stored blob could not be decoded — treat as corruption, never as data."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """Byte-wise XOR of two equal-length buffers.

    Routed through int.from_bytes/to_bytes because that path is a single C-level
    bignum operation; a Python-level zip() over a 14 GB tensor is not a product.
    """
    if len(a) != len(b):
        raise CodecError(f"XOR operands differ in length: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return b""
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(n, "little")


def split_planes(buf: bytes, itemsize: int) -> list[bytes]:
    """Deinterleave `buf` into `itemsize` byte planes.

    Plane k holds byte k of every element. For F32 that means plane 3 carries
    sign+exponent, plane 0 the lowest mantissa byte. Slicing with a stride is a
    C-level copy, so this is memory-bandwidth bound rather than interpreter
    bound.
    """
    if itemsize <= 1:
        return [buf]
    if len(buf) % itemsize:
        # A tensor whose length is not a multiple of its dtype width is not
        # something to guess about: fall back to a single plane, which is still
        # exactly reversible.
        return [buf]
    return [buf[k::itemsize] for k in range(itemsize)]


def join_planes(planes: list[bytes], itemsize: int, total: int) -> bytes:
    """Inverse of split_planes."""
    if itemsize <= 1 or len(planes) == 1:
        joined = planes[0]
        if len(joined) != total:
            raise CodecError(f"plane length {len(joined)} does not match expected {total}")
        return joined
    if len(planes) != itemsize:
        raise CodecError(f"expected {itemsize} planes, got {len(planes)}")
    out = bytearray(total)
    for k, plane in enumerate(planes):
        out[k::itemsize] = plane
    return bytes(out)


# Integer view per dtype width, for the arithmetic-delta transform. Signed and
# unsigned typecodes of the same width.
_INT_CODES: dict[int, tuple[str, str]] = {2: ("H", "h"), 4: ("I", "i"), 8: ("Q", "q")}


def int_delta(base: bytes, variant: bytes, itemsize: int) -> bytes | None:
    """Element-wise difference of the two buffers' integer bit patterns, zigzagged.

    Why this beats XOR, which is the non-obvious part. XOR zeroes the bits that
    did not change, but it is blind to *magnitude*: a weight whose bit pattern
    moved from 0x1000 to 0x0FFF changed by one ULP and XORs to 0x1FFF — thirteen
    set bits of pure noise. Every carry boundary the fine-tune crosses produces
    that. Subtraction in the integer domain keeps one ULP as the number 1.

    IEEE-754 makes this legitimate: within a sign, the bit pattern of a float is
    monotone in its value, so "close in value" really does mean "close as an
    integer". A small relative step therefore lands in the low bits of the
    difference, the high bytes stay zero, and the byte planes collapse. Zigzag
    (n<<1)^(n>>k) is applied so a negative difference is a small number too,
    rather than an all-ones word.

    Returns None when the buffers are not a whole number of elements of a width
    this transform handles — the caller then falls back to XOR, which needs no
    element structure at all.
    """
    codes = _INT_CODES.get(itemsize)
    if codes is None or len(base) != len(variant) or len(base) % itemsize:
        return None
    unsigned_code = codes[0]
    bits = itemsize * 8
    mask = (1 << bits) - 1
    sign_shift = bits - 1

    a = array(unsigned_code)
    b = array(unsigned_code)
    a.frombytes(base)
    b.frombytes(variant)
    if sys.byteorder == "big":
        # Weights are little-endian on disk; the integer view must agree.
        a.byteswap()
        b.byteswap()

    # One C-driven comprehension rather than a Python loop: the arithmetic is
    # interpreted, but the iteration and the array indexing are not. On a 14 GB
    # checkpoint that is the difference between minutes and hours.
    #
    # `d` is the difference reduced into the word — Python ints are unbounded, so
    # y-x can be twice the word range and must be wrapped before it is zigzagged,
    # or the encoding is not reversible at the extremes. Zigzag is then folded
    # into the same pass: shift left one and complement if the sign bit was set.
    zig = array(
        unsigned_code,
        [
            (((d := (y - x) & mask) << 1) & mask) ^ (-(d >> sign_shift) & mask)
            for x, y in zip(a, b)
        ],
    )
    if sys.byteorder == "big":
        zig.byteswap()
    return zig.tobytes()


def int_undelta(base: bytes, residual: bytes, itemsize: int) -> bytes:
    """Inverse of int_delta."""
    codes = _INT_CODES.get(itemsize)
    if codes is None:
        raise CodecError(f"int-delta cannot be reversed for itemsize {itemsize}")
    unsigned_code, _ = codes
    bits = itemsize * 8
    mask = (1 << bits) - 1
    if len(base) != len(residual):
        raise CodecError(f"int-delta operands differ in length: {len(base)} vs {len(residual)}")

    a = array(unsigned_code)
    z = array(unsigned_code)
    a.frombytes(base)
    z.frombytes(residual)
    if sys.byteorder == "big":
        a.byteswap()
        z.byteswap()

    # Un-zigzag: v = (u >> 1) ^ -(u & 1), then add back the base pattern.
    out = array(unsigned_code, [(x + ((u >> 1) ^ (-(u & 1) & mask))) & mask for x, u in zip(a, z)])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _compress(data: bytes, compressor: Compressor, level: int) -> bytes:
    if compressor == "none":
        return data
    if compressor == "zlib":
        return zlib.compress(data, level)
    if compressor == "lzma":
        import lzma  # imported lazily: only the archival tier pays the import

        return lzma.compress(data, preset=6)
    raise CodecError(f"unknown compressor {compressor!r}")


def _decompress(data: bytes, compressor: Compressor) -> bytes:
    try:
        if compressor == "none":
            return data
        if compressor == "zlib":
            return zlib.decompress(data)
        if compressor == "lzma":
            import lzma

            return lzma.decompress(data)
    except (zlib.error, OSError, EOFError) as exc:
        raise CodecError(f"{compressor} stream is corrupt: {exc}") from exc
    raise CodecError(f"unknown compressor {compressor!r}")


@dataclass(frozen=True)
class EncodedBlock:
    """One encoded tensor (or one raw byte span).

    `plane_sizes` lets the decoder cut the concatenated payload back into planes
    without a per-plane framing header.
    """

    payload: bytes
    plane_sizes: tuple[int, ...]
    raw_bytes: int
    itemsize: int
    transform: str
    compressor: Compressor
    digest: str

    @property
    def stored_bytes(self) -> int:
        return len(self.payload)

    @property
    def ratio(self) -> float | None:
        """stored/raw. None for an empty tensor — there is no ratio to report."""
        if self.raw_bytes == 0:
            return None
        return self.stored_bytes / self.raw_bytes


def _encode_residual(
    residual: bytes,
    transform: str,
    variant: bytes,
    itemsize: int,
    compressor: Compressor,
    level: int,
) -> EncodedBlock:
    planes = split_planes(residual, itemsize)
    compressed = [_compress(p, compressor, level) for p in planes]
    return EncodedBlock(
        payload=b"".join(compressed),
        plane_sizes=tuple(len(c) for c in compressed),
        raw_bytes=len(variant),
        itemsize=itemsize,
        transform=transform,
        compressor=compressor,
        digest=sha256(variant),
    )


def encode_delta(
    base: bytes,
    variant: bytes,
    itemsize: int,
    compressor: Compressor = "zlib",
    level: int = DEFAULT_ZLIB_LEVEL,
    search: bool = True,
) -> EncodedBlock:
    """Encode `variant` as a bit-level delta against `base`.

    Two residual transforms are genuinely competitive and which one wins depends
    on the tensor, so with `search=True` both are built and the smaller is kept.
    The cost is one extra compression pass at push time — paid once by the
    publisher, saved on every download by every client. The chosen transform is
    recorded in the block, so decode never has to guess.
    """
    candidates = [
        _encode_residual(xor_bytes(base, variant), "xor+planes", variant, itemsize, compressor, level)
    ]
    if search:
        arithmetic = int_delta(base, variant, itemsize)
        if arithmetic is not None:
            candidates.append(
                _encode_residual(arithmetic, "intdelta+planes", variant, itemsize, compressor, level)
            )
    return min(candidates, key=lambda block: block.stored_bytes)


def encode_literal(
    data: bytes,
    itemsize: int,
    compressor: Compressor = "zlib",
    level: int = DEFAULT_ZLIB_LEVEL,
) -> EncodedBlock:
    """Encode a tensor that has no counterpart in the base (new head, reshape).

    Still byte-plane split: the exponent plane of a freshly initialised weight
    matrix is highly redundant even with no base to subtract, so the transform
    pays for itself here too.
    """
    planes = split_planes(data, itemsize)
    compressed = [_compress(p, compressor, level) for p in planes]
    return EncodedBlock(
        payload=b"".join(compressed),
        plane_sizes=tuple(len(c) for c in compressed),
        raw_bytes=len(data),
        itemsize=itemsize,
        transform="planes",
        compressor=compressor,
        digest=sha256(data),
    )


def decode_block(
    payload: bytes,
    plane_sizes: tuple[int, ...] | list[int],
    raw_bytes: int,
    itemsize: int,
    transform: str,
    compressor: Compressor,
    base: bytes | None = None,
) -> bytes:
    """Rebuild the exact original bytes of one block."""
    cursor = 0
    planes: list[bytes] = []
    for size in plane_sizes:
        if size < 0 or cursor + size > len(payload):
            raise CodecError("plane sizes do not fit the stored payload")
        planes.append(_decompress(payload[cursor : cursor + size], compressor))
        cursor += size
    if cursor != len(payload):
        raise CodecError(f"{len(payload) - cursor} trailing bytes in payload")

    joined = join_planes(planes, itemsize, raw_bytes)

    if transform == "planes":
        return joined
    if transform in ("xor+planes", "intdelta+planes"):
        if base is None:
            raise CodecError("a delta block cannot be decoded without its base tensor")
        if transform == "xor+planes":
            return xor_bytes(base, joined)
        return int_undelta(base, joined, itemsize)
    raise CodecError(f"unknown transform {transform!r}")


def plane_report(base: bytes, variant: bytes, itemsize: int) -> list[dict[str, object]]:
    """Per-plane compressibility of a delta — the explanation panel's data.

    This is how the demo shows *why* the ratio is what it is: the high-order
    plane is nearly free, the low mantissa plane is nearly incompressible, and
    the product's value is the gap between them.
    """
    residual = xor_bytes(base, variant)
    planes = split_planes(residual, itemsize)
    report: list[dict[str, object]] = []
    for k, plane in enumerate(planes):
        stored = len(zlib.compress(plane, DEFAULT_ZLIB_LEVEL))
        zero_fraction = plane.count(0) / len(plane) if plane else None
        report.append(
            {
                "plane": k,
                "role": _plane_role(k, itemsize),
                "raw_bytes": len(plane),
                "stored_bytes": stored,
                "zero_fraction": zero_fraction,
            }
        )
    return report


def _plane_role(k: int, itemsize: int) -> str:
    """Human label for a byte plane, little-endian IEEE-754."""
    if itemsize == 4:
        return {3: "sign + exponent", 2: "high mantissa", 1: "mid mantissa", 0: "low mantissa"}.get(k, f"byte {k}")
    if itemsize == 2:
        return {1: "sign + exponent", 0: "mantissa"}.get(k, f"byte {k}")
    return f"byte {k}"
