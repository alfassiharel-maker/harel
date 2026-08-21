"""Real, standard-library codec implementations behind the `impl` names.

The language would be paper without these. A `.sqz` source declares
`ratio 8.0`; `impl delta_varint` names an implementation here, and
`squeeze verify` runs it over a sample to check that the declaration is still
true. Declared-but-unmeasured ratios are how a footprint plan quietly becomes
fiction after a schema change.

Standard library only, like `backend/algorithms/`: this package must stay
runnable on a bare Python 3.11 so the check is available in the fastest CI job
and on a laptop with nothing installed.

Every codec here is lossless except `int8_quantise`, which reports the fidelity
it actually achieved on the sample rather than a nominal figure.
"""

from __future__ import annotations

import bz2
import enum
import lzma
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "BYTES_PER_FLOAT32",
    "BYTES_PER_INT64",
    "IMPLEMENTATIONS",
    "Implementation",
    "SampleKind",
    "decode_delta_varint",
    "decode_int8_quantise",
    "encode_delta_varint",
    "encode_int8_quantise",
]

#: How the platform stores an uncompressed sample today. These are the baselines
#: a measured ratio is measured against: `BIGINT` for a sensor timestamp/value
#: column, `float32` for a model weight.
BYTES_PER_INT64 = 8
BYTES_PER_FLOAT32 = 4


class SampleKind(str, enum.Enum):
    """Which sample shape an implementation consumes."""

    BLOB = "blob"
    INT_SERIES = "int_series"
    FLOAT_SERIES = "float_series"


@dataclass(frozen=True)
class Implementation:
    """A codec implementation and the shape of sample it accepts."""

    name: str
    sample_kind: SampleKind
    lossless: bool


# ---------------------------------------------------------------------------
# Byte-oriented codecs
# ---------------------------------------------------------------------------
def encode_zlib(payload: bytes) -> bytes:
    # Level 6 is zlib's default and what the platform would realistically run;
    # measuring level 9 would flatter the plan relative to production.
    return zlib.compress(payload, 6)


def decode_zlib(payload: bytes) -> bytes:
    return zlib.decompress(payload)


def encode_lzma(payload: bytes) -> bytes:
    # Preset 6 with no CRC: the container's checksum is redundant when object
    # storage already verifies integrity, and it is ~1% of a small object.
    return lzma.compress(payload, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "preset": 6}])


def decode_lzma(payload: bytes) -> bytes:
    return lzma.decompress(payload, format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "preset": 6}])


def encode_bz2(payload: bytes) -> bytes:
    return bz2.compress(payload, 9)


def decode_bz2(payload: bytes) -> bytes:
    return bz2.decompress(payload)


# ---------------------------------------------------------------------------
# Integer timeseries: delta + zigzag varint
# ---------------------------------------------------------------------------
def _zigzag(value: int) -> int:
    """Map signed to unsigned so small negative deltas stay one byte.

    Standard zigzag (as in Protocol Buffers): -1 -> 1, 1 -> 2, -2 -> 3. Without
    it, a two's-complement -1 would encode as ten varint bytes, and heart-rate
    or power series that oscillate by one unit would encode *larger* than raw.
    """
    return (value << 1) ^ (value >> 63)


def _un_zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _put_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def encode_delta_varint(samples: Sequence[int]) -> bytes:
    """Delta-of-delta free, first-order delta + zigzag varint.

    Sensor series are near-monotonic (timestamps) or slowly varying (heart rate,
    power, cadence), so first-order deltas are small integers and a varint spends
    one byte on them instead of eight. This is the Gorilla paper's idea
    (Pelkonen et al., VLDB 2015, §4.1) reduced to the part that pays for itself
    without bit-level packing — byte alignment costs a little ratio and buys a
    decoder that is obviously correct.
    """
    out = bytearray()
    _put_varint(out, len(samples))
    previous = 0
    for sample in samples:
        _put_varint(out, _zigzag(sample - previous))
        previous = sample
    return bytes(out)


def decode_delta_varint(payload: bytes) -> list[int]:
    index = 0

    def read() -> int:
        nonlocal index
        shift = 0
        result = 0
        while True:
            if index >= len(payload):
                raise ValueError("truncated varint stream")
            byte = payload[index]
            index += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7

    count = read()
    values: list[int] = []
    previous = 0
    for _ in range(count):
        previous += _un_zigzag(read())
        values.append(previous)
    return values


# ---------------------------------------------------------------------------
# Tensors: int8 affine quantisation
# ---------------------------------------------------------------------------
def encode_int8_quantise(weights: Sequence[float]) -> bytes:
    """Affine per-tensor quantisation to int8, header carrying min and scale.

    Layout: `<f4 minimum><f4 scale><i4 count><count bytes>`. Per-tensor rather
    than per-channel because the language's unit of declaration is a class of
    data, not a layer; per-channel would compress marginally better and could
    not be verified from a flat sample.

    Lossy: `verify` reports the fidelity actually achieved, which is what a
    `require fidelity >= x` constraint is checked against.
    """
    if not weights:
        return struct.pack("<ffi", 0.0, 0.0, 0)
    low = min(weights)
    high = max(weights)
    span = high - low
    scale = span / 255.0 if span > 0 else 0.0
    out = bytearray(struct.pack("<ffi", low, scale, len(weights)))
    for weight in weights:
        level = 0 if scale == 0.0 else round((weight - low) / scale)
        out.append(max(0, min(255, level)))
    return bytes(out)


def decode_int8_quantise(payload: bytes) -> list[float]:
    low, scale, count = struct.unpack_from("<ffi", payload, 0)
    body = payload[struct.calcsize("<ffi") :]
    if len(body) < count:
        raise ValueError("truncated quantised tensor")
    return [low + scale * body[i] for i in range(count)]


def quantisation_fidelity(original: Sequence[float], restored: Sequence[float]) -> Fraction | None:
    """Fidelity as 1 - (mean absolute error / value range).

    Normalising by the range rather than by each value keeps the metric finite
    for weights near zero, where a relative error is meaningless. Returns `None`
    for an empty sample or a constant one — there is no meaningful fidelity to
    report, and returning 1.0 would claim a perfect result from no evidence.
    """
    if not original or len(original) != len(restored):
        return None
    span = max(original) - min(original)
    if span <= 0:
        return None
    total_error = sum(abs(a - b) for a, b in zip(original, restored, strict=True))
    mean_error = Fraction(total_error) / len(original)
    return Fraction(1) - mean_error / Fraction(span)


IMPLEMENTATIONS: dict[str, Implementation] = {
    "raw": Implementation("raw", SampleKind.BLOB, lossless=True),
    "zlib": Implementation("zlib", SampleKind.BLOB, lossless=True),
    "lzma": Implementation("lzma", SampleKind.BLOB, lossless=True),
    "bz2": Implementation("bz2", SampleKind.BLOB, lossless=True),
    "delta_varint": Implementation("delta_varint", SampleKind.INT_SERIES, lossless=True),
    "int8_quantise": Implementation("int8_quantise", SampleKind.FLOAT_SERIES, lossless=False),
}
