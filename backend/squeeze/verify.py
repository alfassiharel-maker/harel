"""Verification: does the declared ratio still hold?

A footprint plan is only as good as the ratios its codecs claim. Schemas change,
payloads get a new field, a series that used to be near-monotonic starts
oscillating — and the `.sqz` source keeps claiming 8x because nobody re-measured.
`verify` closes that loop: it runs the named implementation over a sample,
measures the real ratio, round-trips the data to prove the codec is honest about
being lossless, and reports drift against the declaration.

Samples are deterministic by construction. There is no randomness anywhere in
this module: a footprint check that produced a different number on each run
would be treated as noise and ignored, which is the same reason the physiology
fixtures are fixed (CLAUDE.md: "No randomness in fixtures").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from .implementations import (
    BYTES_PER_FLOAT32,
    BYTES_PER_INT64,
    IMPLEMENTATIONS,
    SampleKind,
    decode_bz2,
    decode_delta_varint,
    decode_int8_quantise,
    decode_lzma,
    decode_zlib,
    encode_bz2,
    encode_delta_varint,
    encode_int8_quantise,
    encode_lzma,
    encode_zlib,
    quantisation_fidelity,
)
from .model import Program

__all__ = [
    "DEFAULT_TOLERANCE",
    "Drift",
    "Measurement",
    "Sample",
    "default_samples",
    "measure",
    "verify_program",
]

#: A declared ratio may sit this far above the measured one before it is drift.
#: 10% is tight enough to catch a schema change and loose enough to survive a
#: Python patch release changing a zlib default.
DEFAULT_TOLERANCE = Fraction(1, 10)


@dataclass(frozen=True)
class Sample:
    """A tagged sample. The tag decides which implementations can consume it."""

    kind: SampleKind
    blob: bytes | None = None
    ints: tuple[int, ...] | None = None
    floats: tuple[float, ...] | None = None

    @classmethod
    def of_blob(cls, payload: bytes) -> Sample:
        return cls(kind=SampleKind.BLOB, blob=payload)

    @classmethod
    def of_ints(cls, values: Sequence[int]) -> Sample:
        return cls(kind=SampleKind.INT_SERIES, ints=tuple(values))

    @classmethod
    def of_floats(cls, values: Sequence[float]) -> Sample:
        return cls(kind=SampleKind.FLOAT_SERIES, floats=tuple(values))


@dataclass(frozen=True)
class Measurement:
    implementation: str
    input_bytes: int
    output_bytes: int
    ratio: Fraction
    #: `None` for a lossless codec (fidelity is 1 by definition) or when it
    #: cannot be computed from the sample.
    fidelity: Fraction | None
    #: False when decode(encode(x)) != x for a codec that claims lossless.
    roundtrip_ok: bool


@dataclass(frozen=True)
class Drift:
    codec: str
    implementation: str
    declared_ratio: Fraction | None
    measured: Measurement
    #: `(declared - measured) / measured`. Positive means the source overstates
    #: the codec. `None` when nothing was declared to compare against.
    relative_error: Fraction | None
    #: `None` when there is no declaration to check — unverified, not verified.
    within_tolerance: bool | None
    note: str | None


def measure(implementation: str, sample: Sample) -> Measurement | None:
    """Run one implementation over one sample. `None` if they are incompatible."""
    spec = IMPLEMENTATIONS.get(implementation)
    if spec is None or spec.sample_kind is not sample.kind:
        return None

    if sample.kind is SampleKind.BLOB:
        payload = sample.blob or b""
        if not payload:
            return None
        encoded, ok = _blob_roundtrip(implementation, payload)
        if encoded is None:
            return None
        return Measurement(
            implementation=implementation,
            input_bytes=len(payload),
            output_bytes=len(encoded),
            ratio=Fraction(len(payload), max(1, len(encoded))),
            fidelity=None,
            roundtrip_ok=ok,
        )

    if sample.kind is SampleKind.INT_SERIES:
        values = sample.ints or ()
        if not values:
            return None
        encoded_ints = encode_delta_varint(values)
        raw_size = len(values) * BYTES_PER_INT64
        return Measurement(
            implementation=implementation,
            input_bytes=raw_size,
            output_bytes=len(encoded_ints),
            ratio=Fraction(raw_size, max(1, len(encoded_ints))),
            fidelity=None,
            roundtrip_ok=decode_delta_varint(encoded_ints) == list(values),
        )

    floats = sample.floats or ()
    if not floats:
        return None
    encoded_floats = encode_int8_quantise(floats)
    restored = decode_int8_quantise(encoded_floats)
    raw_size = len(floats) * BYTES_PER_FLOAT32
    return Measurement(
        implementation=implementation,
        input_bytes=raw_size,
        output_bytes=len(encoded_floats),
        ratio=Fraction(raw_size, max(1, len(encoded_floats))),
        fidelity=quantisation_fidelity(floats, restored),
        # A lossy codec is not asked to round-trip exactly; what it must do is
        # return the same number of values, which a truncated payload would not.
        roundtrip_ok=len(restored) == len(floats),
    )


def _blob_roundtrip(implementation: str, payload: bytes) -> tuple[bytes | None, bool]:
    if implementation == "raw":
        return payload, True
    if implementation == "zlib":
        encoded = encode_zlib(payload)
        return encoded, decode_zlib(encoded) == payload
    if implementation == "lzma":
        encoded = encode_lzma(payload)
        return encoded, decode_lzma(encoded) == payload
    if implementation == "bz2":
        encoded = encode_bz2(payload)
        return encoded, decode_bz2(encoded) == payload
    return None, False


def verify_program(
    program: Program,
    samples: Mapping[str, Sample],
    tolerance: Fraction = DEFAULT_TOLERANCE,
) -> list[Drift]:
    """Check every codec that names an implementation against a sample.

    `samples` is keyed by data kind (`timeseries`, `blob`, `tensor`, …) so one
    sample set serves every codec that applies to that kind. A codec whose kinds
    have no sample is reported with `within_tolerance=None`: unverified is a
    distinct outcome from verified-and-fine, and the CLI prints it as such.
    """
    drifts: list[Drift] = []
    for codec in program.codecs.values():
        if codec.implementation is None:
            continue
        sample = next((samples[kind] for kind in sorted(codec.applies_to) if kind in samples), None)
        if sample is None:
            drifts.append(
                Drift(
                    codec=codec.name,
                    implementation=codec.implementation,
                    declared_ratio=codec.ratio,
                    measured=Measurement(codec.implementation, 0, 0, Fraction(0), None, False),
                    relative_error=None,
                    within_tolerance=None,
                    note=f"no sample for kinds {', '.join(sorted(codec.applies_to))}",
                )
            )
            continue
        measurement = measure(codec.implementation, sample)
        if measurement is None:
            drifts.append(
                Drift(
                    codec=codec.name,
                    implementation=codec.implementation,
                    declared_ratio=codec.ratio,
                    measured=Measurement(codec.implementation, 0, 0, Fraction(0), None, False),
                    relative_error=None,
                    within_tolerance=None,
                    note=f"implementation {codec.implementation!r} cannot consume a "
                    f"{sample.kind.value} sample",
                )
            )
            continue

        relative: Fraction | None = None
        within: bool | None = None
        note: str | None = None
        if codec.ratio is not None and measurement.ratio > 0:
            relative = (codec.ratio - measurement.ratio) / measurement.ratio
            within = relative <= tolerance
            if not within:
                note = (
                    f"source claims {float(codec.ratio):.3g}x, sample measures "
                    f"{float(measurement.ratio):.3g}x"
                )
        if not measurement.roundtrip_ok:
            note = "; ".join(filter(None, [note, "round-trip failed"]))
            within = False
        drifts.append(
            Drift(
                codec=codec.name,
                implementation=codec.implementation,
                declared_ratio=codec.ratio,
                measured=measurement,
                relative_error=relative,
                within_tolerance=within,
                note=note,
            )
        )
    return drifts


def default_samples() -> dict[str, Sample]:
    """Deterministic stand-ins shaped like the platform's real payloads.

    These exist so `squeeze verify` says something useful with no arguments.
    They are *representative*, not authoritative: a real check points at real
    exported rows. The shapes are taken from what the training module actually
    stores — a one-second heart-rate stream, an activity JSON document, and a
    small tensor of twin weights.
    """
    # A 20-minute heart-rate stream: warm-up ramp, steady tempo, cool-down. A
    # closed-form recurrence, not a random walk, so the ratio is reproducible.
    heart_rate: list[int] = []
    value = 96
    for second in range(1200):
        if second < 300:
            value += 1 if second % 6 == 0 else 0
        elif second < 900:
            value += 1 if second % 40 == 0 else (-1 if second % 37 == 0 else 0)
        else:
            value -= 1 if second % 9 == 0 else 0
        heart_rate.append(value)

    document = (
        b'{"activity_id":"018f4c2a-0000-7000-8000-000000000000",'
        b'"sport":"run","duration_s":3600,"distance_m":12000,'
        b'"laps":[{"n":1,"s":300,"m":1000},{"n":2,"s":298,"m":1000},'
        b'{"n":3,"s":301,"m":1000}]}'
    ) * 12

    # Weights spread symmetrically about zero, the shape int8 quantisation is
    # designed for. 512 values keeps the 12-byte header from dominating.
    weights = [((index % 101) - 50) / 50.0 for index in range(512)]

    return {
        "timeseries": Sample.of_ints(heart_rate),
        "relational": Sample.of_ints(heart_rate),
        "document": Sample.of_blob(document),
        "blob": Sample.of_blob(document),
        "context": Sample.of_blob(document),
        "tensor": Sample.of_floats(weights),
    }
