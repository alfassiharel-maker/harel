"""Tests for the BitEngine L1 block engine.

Runs with no dependencies installed:

    cd bitengine && python3 -m unittest discover -s tests -t .

MEASURED — the numbers this suite pins, for a 4096-byte block:

    sparse-only delta beats verbatim while k <= 1364    33.3% of the block
    L1 codec set beats verbatim while k <= 3583         87.5% of the block

The first row is the scheme implemented in `experiments/ccp`, and 33.3% is the
cliff `experiments/ccp/FINDINGS.md` §2 attributes CCP's collapse to. The second
row is what selecting a bitmap codec per block buys. Both are asserted below
against the cost model rather than quoted.

No fixture uses a random number generator. Where incompressible bytes are
needed they come from a SHA-256 keystream, which is deterministic and identical
on every platform and Python build.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l1  # noqa: E402

BLOCK = 4096


def incompressible(n: int, tag: bytes = b"bitengine") -> bytes:
    """Deterministic high-entropy bytes: SHA-256 run as a counter-mode keystream."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def with_changes(base: bytes, positions: list[int], value: int = 0xFF) -> bytes:
    """`base` with `value` XORed into each listed position."""
    out = bytearray(base)
    for pos in positions:
        out[pos] ^= value
    return bytes(out)


def scattered(count: int, block: int = BLOCK, stride: int = 7) -> list[int]:
    """`count` distinct positions spread across a block, deterministically.

    A fixed stride coprime with the block size visits distinct positions, which
    is what the sparse and bitmap codecs must be exercised against — a
    contiguous span would be handled by the run codec instead.
    """
    if count > block:
        raise ValueError("cannot change more positions than the block has bytes")
    return sorted((i * stride) % block for i in range(count)) if count else []


class PositionWidth(unittest.TestCase):
    def test_widths(self) -> None:
        self.assertEqual(l1.position_width(1), 1)
        self.assertEqual(l1.position_width(255), 1)
        self.assertEqual(l1.position_width(256), 2)
        self.assertEqual(l1.position_width(4096), 2)
        self.assertEqual(l1.position_width(65536), 3)

    def test_rejects_non_positive(self) -> None:
        with self.assertRaises(ValueError):
            l1.position_width(0)


class Xor(unittest.TestCase):
    def test_known_value(self) -> None:
        self.assertEqual(l1.xor_bytes(b"\xf0\x0f", b"\xff\xff"), b"\x0f\xf0")

    def test_self_inverse(self) -> None:
        a = incompressible(512, b"a")
        b = incompressible(512, b"b")
        self.assertEqual(l1.xor_bytes(a, l1.xor_bytes(a, b)), b)

    def test_length_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            l1.xor_bytes(b"abc", b"ab")


class Stats(unittest.TestCase):
    def test_anchor_no_change(self) -> None:
        stats = l1.residual_stats(bytes(BLOCK))
        self.assertEqual(stats.changed_bytes, 0)
        self.assertEqual(stats.changed_bits, 0)
        self.assertEqual(stats.runs, 0)

    def test_bits_and_bytes_are_distinct(self) -> None:
        # One byte differing by one bit is one changed byte and one changed bit.
        # Both codecs cost it the same, and the stats must not pretend otherwise.
        residual = bytearray(BLOCK)
        residual[10] = 0b0000_0001
        one_bit = l1.residual_stats(bytes(residual))
        residual[10] = 0b1111_1111
        eight_bits = l1.residual_stats(bytes(residual))

        self.assertEqual(one_bit.changed_bytes, 1)
        self.assertEqual(one_bit.changed_bits, 1)
        self.assertEqual(eight_bits.changed_bytes, 1)
        self.assertEqual(eight_bits.changed_bits, 8)
        self.assertEqual(
            l1.codec_costs(one_bit)[l1.CODEC_SPARSE],
            l1.codec_costs(eight_bits)[l1.CODEC_SPARSE],
        )

    def test_runs_counted(self) -> None:
        residual = bytearray(BLOCK)
        residual[10:20] = b"\xff" * 10
        residual[100:105] = b"\xff" * 5
        stats = l1.residual_stats(bytes(residual))
        self.assertEqual(stats.runs, 2)
        self.assertEqual(stats.changed_bytes, 15)

    def test_rejects_empty_and_oversized(self) -> None:
        with self.assertRaises(ValueError):
            l1.residual_stats(b"")
        with self.assertRaises(ValueError):
            l1.residual_stats(bytes(l1.MAX_BLOCK_BYTES + 1))


class HandComputedCost(unittest.TestCase):
    """One cost worked out by hand, so the formula cannot drift unnoticed."""

    def test_three_scattered_changes_in_4kb(self) -> None:
        # n = 4096 so w = position_width(4096) = 2.
        #   sparse = 1 tag + 2 count + 3*2 positions + 3 values      = 12
        #   runs   = 1 tag + 2 count + 3*(2+2) gaps/lengths + 3      = 18
        #   bitmap = 1 tag + 4096/8 bitmap + 3 values                = 516
        #   raw    = 1 tag + 4096                                    = 4097
        base = incompressible(BLOCK)
        target = with_changes(base, [5, 1000, 4000])

        plan = l1.plan_block(base, target)
        self.assertEqual(plan.costs[l1.CODEC_SPARSE], 12)
        self.assertEqual(plan.costs[l1.CODEC_RUNS], 18)
        self.assertEqual(plan.costs[l1.CODEC_BITMAP], 516)
        self.assertEqual(plan.costs[l1.CODEC_RAW], 4097)
        self.assertEqual(plan.codec, l1.CODEC_SPARSE)
        self.assertEqual(plan.encoded_bytes, 12)
        self.assertEqual(len(l1.encode_block(base, target)), 12)


class BreakEvenBoundary(unittest.TestCase):
    """The claim this engine exists to make, asserted rather than quoted."""

    @staticmethod
    def _largest_k_beating_raw(codec: int) -> int:
        raw = 1 + BLOCK
        best = -1
        # Bounded by the block size: k cannot exceed the bytes in the block.
        for k in range(BLOCK + 1):
            stats = l1.ResidualStats(
                block_bytes=BLOCK, changed_bytes=k, changed_bits=k * 8, runs=k
            )
            if l1.codec_costs(stats)[codec] < raw:
                best = k
        return best

    def test_sparse_alone_cliffs_at_one_third(self) -> None:
        # This is the experiments/ccp scheme, and the cliff FINDINGS.md §2 names.
        k = self._largest_k_beating_raw(l1.CODEC_SPARSE)
        self.assertEqual(k, 1364)
        self.assertAlmostEqual(k / BLOCK * 100, 33.30, places=1)

    def test_bitmap_extends_the_cliff_to_seven_eighths(self) -> None:
        k = self._largest_k_beating_raw(l1.CODEC_BITMAP)
        self.assertEqual(k, 3583)
        self.assertAlmostEqual(k / BLOCK * 100, 87.48, places=1)

    def test_engine_still_saves_where_sparse_alone_would_not(self) -> None:
        # 40% of the block changed: past the sparse cliff, inside the bitmap's.
        k = int(BLOCK * 0.40)
        base = incompressible(BLOCK)
        target = with_changes(base, scattered(k))
        plan = l1.plan_block(base, target)

        self.assertEqual(plan.stats.changed_bytes, k)
        self.assertGreater(plan.costs[l1.CODEC_SPARSE], plan.costs[l1.CODEC_RAW])
        self.assertEqual(plan.codec, l1.CODEC_BITMAP)

        result = l1.savings(BLOCK, plan.encoded_bytes, plan.stats.changed_bytes, plan.stats.changed_bits)
        assert result.saving_pct is not None
        self.assertGreater(result.saving_pct, 47.0)

    def test_beyond_the_cliff_falls_back_to_raw(self) -> None:
        base = incompressible(BLOCK)
        target = incompressible(BLOCK, b"different")
        plan = l1.plan_block(base, target)
        self.assertEqual(plan.codec, l1.CODEC_RAW)
        self.assertEqual(plan.encoded_bytes, BLOCK + 1)


class CodecSelection(unittest.TestCase):
    def test_identical(self) -> None:
        base = incompressible(BLOCK)
        plan = l1.plan_block(base, base)
        self.assertEqual(plan.codec, l1.CODEC_IDENTICAL)
        self.assertEqual(plan.encoded_bytes, 1)

    def test_clustered_changes_pick_runs(self) -> None:
        base = incompressible(BLOCK)
        target = bytearray(base)
        target[1000:1600] = incompressible(600, b"patch")
        plan = l1.plan_block(base, bytes(target))
        self.assertEqual(plan.codec, l1.CODEC_RUNS)

    def test_sparse_to_bitmap_crossover(self) -> None:
        # sparse = 3 + 3k, bitmap = 513 + k. Equal at k = 255, where the tie
        # resolves to the lower codec id; bitmap wins from k = 256.
        def codec_for(k: int) -> int:
            stats = l1.ResidualStats(block_bytes=BLOCK, changed_bytes=k, changed_bits=k * 8, runs=k)
            costs = l1.codec_costs(stats)
            return min(costs, key=lambda c: (costs[c], 0 if c in l1._BASE_FREE else 1, c))

        self.assertEqual(codec_for(255), l1.CODEC_SPARSE)
        self.assertEqual(codec_for(256), l1.CODEC_BITMAP)

    def test_negative_control_never_exceeds_raw(self) -> None:
        # The engine must not expand data. One tag byte is the whole overhead.
        for tag in (b"c1", b"c2", b"c3"):
            base = incompressible(BLOCK, tag)
            target = incompressible(BLOCK, tag + b"x")
            plan = l1.plan_block(base, target)
            self.assertLessEqual(plan.encoded_bytes, BLOCK + 1)


class RoundTrip(unittest.TestCase):
    """Every codec must reconstruct byte-for-byte, and the suite must cover all."""

    def _cases(self) -> list[tuple[str, bytes, bytes]]:
        base = incompressible(BLOCK)
        clustered = bytearray(base)
        clustered[1000:1600] = incompressible(600, b"patch")
        return [
            ("identical", base, base),
            ("sparse", base, with_changes(base, [0, 5, 1000, 4095])),
            ("runs", base, bytes(clustered)),
            ("bitmap", base, with_changes(base, scattered(int(BLOCK * 0.40)))),
            ("raw", base, incompressible(BLOCK, b"unrelated")),
            ("single-byte-block", b"\x01", b"\x02"),
            ("all-zero-base", bytes(BLOCK), with_changes(bytes(BLOCK), [7, 8, 9])),
        ]

    def test_round_trip(self) -> None:
        seen: set[int] = set()
        for name, base, target in self._cases():
            with self.subTest(case=name):
                encoded = l1.encode_block(base, target)
                self.assertEqual(l1.decode_block(base, encoded), target)
                seen.add(encoded[0])
        self.assertEqual(
            seen,
            {l1.CODEC_IDENTICAL, l1.CODEC_SPARSE, l1.CODEC_RUNS, l1.CODEC_BITMAP, l1.CODEC_RAW},
            "round-trip suite no longer exercises every codec",
        )

    def test_round_trip_across_change_densities(self) -> None:
        # Sweeps the whole k range, so every codec boundary is crossed and the
        # cost model is checked against the encoder at each one.
        base = incompressible(BLOCK)
        for k in (0, 1, 2, 255, 256, 1364, 1365, 2048, 3583, 3584, BLOCK):
            with self.subTest(k=k):
                target = with_changes(base, scattered(k))
                encoded = l1.encode_block(base, target)
                self.assertEqual(l1.decode_block(base, encoded), target)
                self.assertLessEqual(len(encoded), BLOCK + 1)

    def test_unaligned_block_sizes(self) -> None:
        # Bitmap padding is only exercised when the block is not a multiple of 8.
        for n in (1, 7, 8, 9, 63, 255, 257, 1000):
            with self.subTest(n=n):
                base = incompressible(n, b"u")
                target = with_changes(base, scattered(max(1, n // 2), block=n, stride=3))
                encoded = l1.encode_block(base, target)
                self.assertEqual(l1.decode_block(base, encoded), target)

    def test_deterministic(self) -> None:
        base = incompressible(BLOCK)
        target = with_changes(base, scattered(300))
        self.assertEqual(l1.encode_block(base, target), l1.encode_block(base, target))

    def test_length_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            l1.plan_block(incompressible(64), incompressible(65))


class MalformedInput(unittest.TestCase):
    """Encoded blocks are untrusted. Every decoder rejects rather than guesses."""

    def setUp(self) -> None:
        self.base = incompressible(BLOCK)

    def test_empty(self) -> None:
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, b"")

    def test_unknown_codec(self) -> None:
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, bytes([200]) + b"junk")

    def test_identical_with_payload(self) -> None:
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, bytes([l1.CODEC_IDENTICAL]) + b"\x00")

    def test_raw_wrong_length(self) -> None:
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, bytes([l1.CODEC_RAW]) + b"short")

    def test_sparse_count_exceeds_block(self) -> None:
        # A count that would drive a huge allocation is rejected before it is used.
        payload = bytes([l1.CODEC_SPARSE]) + (0xFFFF).to_bytes(2, "big")
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)

    def test_sparse_position_outside_block(self) -> None:
        payload = (
            bytes([l1.CODEC_SPARSE])
            + (1).to_bytes(2, "big")
            + (BLOCK).to_bytes(2, "big")
            + b"\xff"
        )
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)

    def test_sparse_positions_not_ascending(self) -> None:
        payload = (
            bytes([l1.CODEC_SPARSE])
            + (2).to_bytes(2, "big")
            + (10).to_bytes(2, "big")
            + (10).to_bytes(2, "big")
            + b"\xff\xff"
        )
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)

    def test_truncated_bodies(self) -> None:
        base = self.base
        cases = {
            l1.CODEC_SPARSE: with_changes(base, [5, 1000, 4000]),
            l1.CODEC_RUNS: bytes(base[:1000] + incompressible(600, b"p") + base[1600:]),
            l1.CODEC_BITMAP: with_changes(base, scattered(int(BLOCK * 0.40))),
        }
        for codec, target in cases.items():
            encoded = l1.encode_block(base, target)
            with self.subTest(codec=l1.CODEC_NAMES[codec]):
                # Guards the fixture as well as the decoder: if a change makes
                # this block encode under a different codec, the case silently
                # stops covering the decoder it was written for.
                self.assertEqual(encoded[0], codec)
                with self.assertRaises(l1.CorruptBlock):
                    l1.decode_block(base, encoded[:-1])

    def test_trailing_values(self) -> None:
        encoded = l1.encode_block(self.base, with_changes(self.base, [1, 2, 3]))
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, encoded + b"\xff")

    def test_zero_delta_entry_rejected(self) -> None:
        # A zero XOR value is a no-op that a well-formed encoder never emits;
        # accepting it would allow two encodings of the same block.
        payload = (
            bytes([l1.CODEC_SPARSE])
            + (1).to_bytes(2, "big")
            + (10).to_bytes(2, "big")
            + b"\x00"
        )
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)

    def test_run_beyond_block(self) -> None:
        payload = (
            bytes([l1.CODEC_RUNS])
            + (1).to_bytes(2, "big")
            + (BLOCK - 1).to_bytes(2, "big")
            + (10).to_bytes(2, "big")
            + b"\xff" * 10
        )
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)

    def test_impossible_run_count(self) -> None:
        payload = bytes([l1.CODEC_RUNS]) + (BLOCK).to_bytes(2, "big")
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(self.base, payload)


class BitmapInternals(unittest.TestCase):
    """The bitmap codec has two scatter paths and a packing trick. Both are load-bearing."""

    def test_pack_unpack_round_trip(self) -> None:
        for n in (8, 64, 4096):
            with self.subTest(n=n):
                residual = bytes(bytearray(incompressible(n, b"f")))
                flags = residual.translate(l1._ONE_IF_NONZERO)
                self.assertEqual(l1._unpack_bitmap(l1._pack_bitmap(flags), n), flags)

    def test_both_scatter_paths_agree(self) -> None:
        # The two paths are a throughput choice, never a correctness one, so
        # each is forced over data shaped to favour the other.
        base = incompressible(BLOCK)
        # 200 runs of 8 bytes: too many runs for the run codec's (gap, length)
        # table to win, but long enough that the run walk beats the byte walk.
        banded = bytearray(base)
        for i in range(200):
            start = i * 20
            banded[start : start + 8] = incompressible(8, b"b" + i.to_bytes(2, "big"))
        cases = {
            "isolated changes": with_changes(base, scattered(int(BLOCK * 0.40))),
            "banded runs": bytes(banded),
        }
        original = l1._RUN_WALK_COST
        try:
            for name, target in cases.items():
                encoded = l1.encode_block(base, target)
                self.assertEqual(encoded[0], l1.CODEC_BITMAP, f"{name} is no longer bitmap-coded")
                outputs = []
                for forced in (0, 10**9):  # 0 forces the run walk, huge forces the byte walk
                    l1._RUN_WALK_COST = forced
                    outputs.append(l1.decode_block(base, encoded))
                with self.subTest(case=name):
                    self.assertEqual(outputs[0], target)
                    self.assertEqual(outputs[0], outputs[1])
        finally:
            l1._RUN_WALK_COST = original

    def test_bits_past_end_of_block_rejected(self) -> None:
        # n = 12 leaves 4 padding bits. Setting one must not let the scatter
        # path write past the block.
        n = 12
        payload = bytes([l1.CODEC_BITMAP]) + bytes([0x00, 0x01]) + b"\xff"
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(incompressible(n, b"t"), payload)

    def test_value_count_must_match_bitmap(self) -> None:
        base = incompressible(BLOCK)
        encoded = l1.encode_block(base, with_changes(base, scattered(int(BLOCK * 0.40))))
        self.assertEqual(encoded[0], l1.CODEC_BITMAP)
        with self.assertRaises(l1.CorruptBlock):
            l1.decode_block(base, encoded + b"\xff")


class SavingsMath(unittest.TestCase):
    def test_undefined_on_empty_input(self) -> None:
        # The undefined case: no data means no saving to report, not 0%.
        result = l1.savings(0, 0, 0, 0)
        self.assertIsNone(result.saving_pct)
        self.assertIsNone(result.change_pct)

    def test_hand_computed(self) -> None:
        # 1000 bytes in, 12 out, 3 bytes actually changed.
        result = l1.savings(1000, 12, 3, 24)
        self.assertAlmostEqual(result.saving_pct or 0.0, 98.8)
        self.assertAlmostEqual(result.change_pct or 0.0, 0.3)
        self.assertEqual(result.overhead_bytes, 9)
        self.assertEqual(result.saved_bytes, 988)

    def test_saving_is_never_better_than_change_would_suggest(self) -> None:
        # (1 - encoded/total) can never beat (1 - change/total), because no
        # encoding of k changed bytes costs fewer than k bytes. This is the
        # invariant that stops change_pct being reported as if it were a saving.
        base = incompressible(BLOCK)
        for k in (0, 1, 100, 1000, 3000, BLOCK):
            with self.subTest(k=k):
                plan = l1.plan_block(base, with_changes(base, scattered(k)))
                result = l1.savings(
                    BLOCK, plan.encoded_bytes, plan.stats.changed_bytes, plan.stats.changed_bits
                )
                assert result.saving_pct is not None and result.change_pct is not None
                self.assertLessEqual(result.saving_pct, 100.0 - result.change_pct)

    def test_negative_saving_reported_as_is(self) -> None:
        result = l1.savings(100, 101, 100, 800)
        assert result.saving_pct is not None
        self.assertLess(result.saving_pct, 0.0)

    def test_rejects_impossible_counts(self) -> None:
        with self.assertRaises(ValueError):
            l1.savings(10, 5, 11, 0)
        with self.assertRaises(ValueError):
            l1.savings(10, 5, 10, 81)
        with self.assertRaises(ValueError):
            l1.savings(-1, 0, 0, 0)


class Accumulator(unittest.TestCase):
    def test_totals_and_drivers(self) -> None:
        base = incompressible(BLOCK)
        acc = l1.SavingsAccumulator()
        acc.add(l1.plan_block(base, base))
        # Scattered, not adjacent: three contiguous positions would be cheaper
        # as a run (10 bytes) than as sparse entries (12).
        acc.add(l1.plan_block(base, with_changes(base, [5, 1000, 4000])))
        acc.add(l1.plan_block(base, incompressible(BLOCK, b"unrelated")))

        result = acc.result()
        self.assertEqual(acc.blocks, 3)
        self.assertEqual(result.total_bytes, BLOCK * 3)
        self.assertEqual(result.encoded_bytes, 1 + 12 + (BLOCK + 1))
        self.assertEqual(
            acc.by_codec,
            {l1.CODEC_IDENTICAL: 1, l1.CODEC_SPARSE: 1, l1.CODEC_RAW: 1},
        )

    def test_empty_accumulator_is_undefined(self) -> None:
        self.assertIsNone(l1.SavingsAccumulator().result().saving_pct)


if __name__ == "__main__":
    unittest.main()
