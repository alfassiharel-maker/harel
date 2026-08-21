"""The codec implementations and the declaration-drift check.

Byte counts here are computed by hand from the encodings described in
`implementations.py`. That is the point: a measured ratio is only evidence if the
measurement itself is pinned down.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from backend.squeeze import compile_text
from backend.squeeze.implementations import (
    decode_delta_varint,
    decode_int8_quantise,
    encode_delta_varint,
    encode_int8_quantise,
    quantisation_fidelity,
)
from backend.squeeze.verify import Sample, default_samples, measure, verify_program


class TestDeltaVarint(unittest.TestCase):
    def test_hand_computed_length(self) -> None:
        # [0, 1, 2] -> varint(count=3) is 1 byte; deltas 0, 1, 1 zigzag to
        # 0, 2, 2, one byte each. Four bytes total, against 24 raw (3 x int64).
        encoded = encode_delta_varint([0, 1, 2])
        self.assertEqual(len(encoded), 4)
        self.assertEqual(encoded, bytes([3, 0, 2, 2]))

    def test_zigzag_keeps_small_negatives_short(self) -> None:
        # Without zigzag, each -1 delta would be a ten-byte varint. Here:
        # varint(count=4) 1 byte + zigzag(100)=200 two bytes + three one-byte
        # deltas = 6 bytes, against 32 raw.
        encoded = encode_delta_varint([100, 99, 100, 99])
        self.assertEqual(len(encoded), 6)
        self.assertEqual(decode_delta_varint(encoded), [100, 99, 100, 99])

    def test_roundtrip_on_a_realistic_series(self) -> None:
        series = default_samples()["timeseries"].ints
        assert series is not None
        self.assertEqual(decode_delta_varint(encode_delta_varint(series)), list(series))

    def test_empty_series(self) -> None:
        self.assertEqual(decode_delta_varint(encode_delta_varint([])), [])

    def test_truncated_stream_raises(self) -> None:
        with self.assertRaises(ValueError):
            decode_delta_varint(bytes([5, 2]))


class TestInt8Quantisation(unittest.TestCase):
    def test_hand_computed_length(self) -> None:
        # 4-byte minimum + 4-byte scale + 4-byte count + one byte per weight.
        self.assertEqual(len(encode_int8_quantise([0.0] * 8)), 12 + 8)

    def test_endpoints_survive_to_float32_precision(self) -> None:
        # The affine mapping puts the minimum at level 0 and the maximum at
        # level 255, so both endpoints are recovered — up to the float32 the
        # header stores the scale in, which is ~6e-8 here and is the only loss
        # at the endpoints.
        weights = [0.0, 1.0]
        restored = decode_int8_quantise(encode_int8_quantise(weights))
        self.assertEqual(restored[0], 0.0)
        self.assertAlmostEqual(restored[1], 1.0, places=6)
        fidelity = quantisation_fidelity(weights, restored)
        assert fidelity is not None
        self.assertGreater(fidelity, Fraction(999_999, 1_000_000))

    def test_fidelity_is_undefined_for_a_constant_tensor(self) -> None:
        # No range means no meaningful fidelity. Returning 1.0 here would claim
        # a perfect result from no evidence.
        self.assertIsNone(quantisation_fidelity([0.5, 0.5], [0.5, 0.5]))
        self.assertIsNone(quantisation_fidelity([], []))

    def test_fidelity_of_the_default_tensor_sample(self) -> None:
        weights = default_samples()["tensor"].floats
        assert weights is not None
        restored = decode_int8_quantise(encode_int8_quantise(weights))
        fidelity = quantisation_fidelity(weights, restored)
        assert fidelity is not None
        # A 256-level grid over a range of 2.0 has a worst-case error of one
        # half-step (0.0039), so fidelity cannot fall below 1 - 0.002.
        self.assertGreater(fidelity, Fraction(998, 1000))


class TestMeasurement(unittest.TestCase):
    def test_int_series_ratio(self) -> None:
        measurement = measure("delta_varint", Sample.of_ints([0, 1, 2]))
        assert measurement is not None
        self.assertEqual((measurement.input_bytes, measurement.output_bytes), (24, 4))
        self.assertEqual(measurement.ratio, 6)
        self.assertTrue(measurement.roundtrip_ok)

    def test_incompatible_sample_returns_none(self) -> None:
        self.assertIsNone(measure("delta_varint", Sample.of_blob(b"abc")))
        self.assertIsNone(measure("no_such_codec", Sample.of_blob(b"abc")))

    def test_empty_sample_returns_none(self) -> None:
        self.assertIsNone(measure("zlib", Sample.of_blob(b"")))

    def test_blob_codecs_roundtrip(self) -> None:
        payload = default_samples()["document"].blob
        assert payload is not None
        for implementation in ("raw", "zlib", "lzma", "bz2"):
            with self.subTest(implementation=implementation):
                measurement = measure(implementation, Sample.of_blob(payload))
                assert measurement is not None
                self.assertTrue(measurement.roundtrip_ok)


class TestVerifyProgram(unittest.TestCase):
    SOURCE = """version 1
codec honest { kind lossless; ratio 2; applies_to timeseries; impl delta_varint }
codec optimistic { kind lossless; ratio 400; applies_to timeseries; impl delta_varint }
"""

    def test_drift_is_only_flagged_when_a_declaration_is_optimistic(self) -> None:
        program, _ = compile_text(self.SOURCE)
        assert program is not None
        drifts = {d.codec: d for d in verify_program(program, default_samples())}
        self.assertTrue(drifts["honest"].within_tolerance)
        self.assertFalse(drifts["optimistic"].within_tolerance)
        self.assertIn("source claims", drifts["optimistic"].note or "")

    def test_a_codec_with_no_sample_is_unverified_not_verified(self) -> None:
        program, _ = compile_text(
            "version 1\ncodec c { kind lossless; ratio 2; applies_to tensor; impl int8_quantise }\n"
        )
        assert program is not None
        drift = verify_program(program, {})[0]
        self.assertIsNone(drift.within_tolerance)
        self.assertIn("no sample", drift.note or "")

    def test_a_codec_without_an_implementation_is_skipped(self) -> None:
        program, _ = compile_text("version 1\ncodec c { kind lossless; ratio 2; applies_to blob }\n")
        assert program is not None
        self.assertEqual(verify_program(program, default_samples()), [])

    def test_the_committed_policy_declares_no_optimistic_ratio(self) -> None:
        from pathlib import Path

        program, _ = compile_text(Path("policies/footprint.sqz").read_text(encoding="utf-8"))
        assert program is not None
        for drift in verify_program(program, default_samples()):
            with self.subTest(codec=drift.codec):
                self.assertTrue(drift.within_tolerance, drift.note)

    def test_samples_are_deterministic(self) -> None:
        first = default_samples()["timeseries"]
        second = default_samples()["timeseries"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
