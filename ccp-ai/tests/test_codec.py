"""Codec tests: every transform is a bijection, and corruption is detected.

Run with no dependencies installed:
    python3 -m unittest discover -s ccp-ai/tests -t ccp-ai
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import codec


def f32(values) -> bytes:
    a = array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


class XorTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        a = bytes(range(256))
        b = bytes((x * 7 + 3) % 256 for x in range(256))
        self.assertEqual(codec.xor_bytes(codec.xor_bytes(a, b), a), b)

    def test_empty(self) -> None:
        self.assertEqual(codec.xor_bytes(b"", b""), b"")

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(codec.CodecError):
            codec.xor_bytes(b"abc", b"ab")


class PlaneTests(unittest.TestCase):
    def test_split_is_reversible(self) -> None:
        buf = bytes(range(64))
        for itemsize in (1, 2, 4, 8):
            planes = codec.split_planes(buf, itemsize)
            self.assertEqual(codec.join_planes(planes, itemsize, len(buf)), buf)

    def test_plane_layout_is_stride_k(self) -> None:
        # Explicit rather than round-trip: the delta transform's whole benefit
        # depends on plane 3 of an F32 buffer being the sign+exponent byte.
        buf = bytes([0, 1, 2, 3, 10, 11, 12, 13])
        planes = codec.split_planes(buf, 4)
        self.assertEqual(planes[3], bytes([3, 13]))
        self.assertEqual(planes[0], bytes([0, 10]))

    def test_ragged_length_falls_back_to_one_plane(self) -> None:
        buf = b"12345"  # not a multiple of 4
        planes = codec.split_planes(buf, 4)
        self.assertEqual(len(planes), 1)
        self.assertEqual(codec.join_planes(planes, 4, len(buf)), buf)


class IntDeltaTests(unittest.TestCase):
    def test_roundtrip_float32(self) -> None:
        base = f32([0.1, -0.2, 3.5, -0.0, 0.0, 1e-8, 1e8])
        variant = f32([0.1001, -0.2002, 3.4999, -0.0, 1e-30, 1.1e-8, 1.00001e8])
        residual = codec.int_delta(base, variant, 4)
        self.assertIsNotNone(residual)
        self.assertEqual(codec.int_undelta(base, residual, 4), variant)

    def test_roundtrip_survives_sign_flip(self) -> None:
        # A weight crossing zero is the case where the integer view is least
        # well behaved. It must still reconstruct exactly.
        base = f32([1e-9, -1e-9, 0.0])
        variant = f32([-1e-9, 1e-9, -0.0])
        residual = codec.int_delta(base, variant, 4)
        self.assertEqual(codec.int_undelta(base, residual, 4), variant)

    def test_roundtrip_at_bit_extremes(self) -> None:
        # All-zero and all-ones words: wraparound in the subtraction must be
        # symmetric with the addition on the way back.
        base = struct.pack("<4I", 0, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF)
        variant = struct.pack("<4I", 0xFFFFFFFF, 0, 0x7FFFFFFF, 0x80000000)
        residual = codec.int_delta(base, variant, 4)
        self.assertEqual(codec.int_undelta(base, residual, 4), variant)

    def test_roundtrip_float16_and_int64(self) -> None:
        base = struct.pack("<4H", 0x3C00, 0x0001, 0xFFFF, 0x8000)
        variant = struct.pack("<4H", 0x3C01, 0x0000, 0xFFFE, 0x8001)
        residual = codec.int_delta(base, variant, 2)
        self.assertEqual(codec.int_undelta(base, residual, 2), variant)

        base8 = struct.pack("<2Q", 0, 2**64 - 1)
        variant8 = struct.pack("<2Q", 5, 2**64 - 7)
        residual8 = codec.int_delta(base8, variant8, 8)
        self.assertEqual(codec.int_undelta(base8, residual8, 8), variant8)

    def test_unsupported_width_returns_none(self) -> None:
        self.assertIsNone(codec.int_delta(b"ab", b"cd", 1))
        self.assertIsNone(codec.int_delta(b"abc", b"abcd", 4))

    def test_small_step_lands_in_low_bytes(self) -> None:
        # The load-bearing property of the whole product: a small relative move
        # produces a residual whose high bytes are zero. If this stops being
        # true the compression ratio is gone, so it is asserted rather than
        # assumed.
        base = f32([0.5] * 1024)
        variant = f32([0.5 * (1 + 1e-6)] * 1024)
        residual = codec.int_delta(base, variant, 4)
        planes = codec.split_planes(residual, 4)
        self.assertEqual(planes[3], bytes(1024))  # sign+exponent plane: all zero
        self.assertEqual(planes[2], bytes(1024))  # high mantissa plane: all zero
        self.assertNotEqual(planes[0], bytes(1024))


class BlockTests(unittest.TestCase):
    def test_delta_block_roundtrip(self) -> None:
        base = f32([i * 0.001 for i in range(4096)])
        variant = f32([i * 0.001 * (1 + 1e-5) for i in range(4096)])
        block = codec.encode_delta(base, variant, 4)
        rebuilt = codec.decode_block(
            block.payload,
            block.plane_sizes,
            block.raw_bytes,
            block.itemsize,
            block.transform,
            block.compressor,
            base=base,
        )
        self.assertEqual(rebuilt, variant)
        self.assertLess(block.stored_bytes, len(variant))

    def test_literal_block_roundtrip(self) -> None:
        data = f32([i * 0.5 for i in range(1000)])
        block = codec.encode_literal(data, 4)
        rebuilt = codec.decode_block(
            block.payload, block.plane_sizes, block.raw_bytes, block.itemsize, block.transform, block.compressor
        )
        self.assertEqual(rebuilt, data)

    def test_search_picks_the_smaller_transform(self) -> None:
        # Varied weights taking varied small steps — the realistic case. On a
        # constant tensor both transforms produce a constant residual and tie,
        # which proves nothing.
        base = f32([0.25 + (i % 601) * 3e-4 for i in range(8192)])
        variant = f32([(0.25 + (i % 601) * 3e-4) * (1 + (i % 17) * 2e-7) for i in range(8192)])
        searched = codec.encode_delta(base, variant, 4, search=True)
        xor_only = codec.encode_delta(base, variant, 4, search=False)
        self.assertEqual(searched.transform, "intdelta+planes")
        self.assertLessEqual(searched.stored_bytes, xor_only.stored_bytes)

    def test_lzma_compressor_roundtrips(self) -> None:
        data = f32([1.0] * 512)
        block = codec.encode_literal(data, 4, compressor="lzma")
        rebuilt = codec.decode_block(
            block.payload, block.plane_sizes, block.raw_bytes, block.itemsize, block.transform, "lzma"
        )
        self.assertEqual(rebuilt, data)

    def test_corrupt_payload_raises_rather_than_returning_data(self) -> None:
        data = f32([1.5] * 256)
        block = codec.encode_literal(data, 4)
        corrupted = bytearray(block.payload)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with self.assertRaises(codec.CodecError):
            codec.decode_block(
                bytes(corrupted),
                block.plane_sizes,
                block.raw_bytes,
                block.itemsize,
                block.transform,
                block.compressor,
            )

    def test_plane_sizes_that_do_not_fit_raise(self) -> None:
        with self.assertRaises(codec.CodecError):
            codec.decode_block(b"short", [999], 4, 4, "planes", "zlib")

    def test_delta_without_base_raises(self) -> None:
        base = f32([1.0] * 64)
        variant = f32([1.0001] * 64)
        block = codec.encode_delta(base, variant, 4)
        with self.assertRaises(codec.CodecError):
            codec.decode_block(
                block.payload, block.plane_sizes, block.raw_bytes, block.itemsize, block.transform, block.compressor
            )

    def test_unknown_transform_raises(self) -> None:
        with self.assertRaises(codec.CodecError):
            codec.decode_block(b"", [], 0, 4, "rot13+planes", "none")

    def test_empty_tensor_has_no_ratio(self) -> None:
        # A ratio of 0.0 for an empty tensor would be a fabricated measurement.
        block = codec.encode_literal(b"", 4)
        self.assertIsNone(block.ratio)


class PlaneReportTests(unittest.TestCase):
    def test_report_orders_by_plane_and_labels_roles(self) -> None:
        # Values must vary: a constant tensor makes every plane equally
        # compressible and the assertion below would pass for the wrong reason.
        base = f32([0.75 + (i % 977) * 1e-4 for i in range(2048)])
        variant = f32([(0.75 + (i % 977) * 1e-4) * (1 + (i % 31) * 1e-7) for i in range(2048)])
        report = codec.plane_report(base, variant, 4)
        self.assertEqual([row["plane"] for row in report], [0, 1, 2, 3])
        self.assertEqual(report[3]["role"], "sign + exponent")
        self.assertEqual(report[3]["zero_fraction"], 1.0)
        self.assertLess(report[3]["stored_bytes"], report[0]["stored_bytes"])


if __name__ == "__main__":
    unittest.main()
