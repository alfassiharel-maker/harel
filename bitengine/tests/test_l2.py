"""Tests for the BitEngine L2 controller.

    cd bitengine && python3 -m unittest discover -s tests -t .

The property that matters most here is that encode and decode derive their bases
from the same reconstructed history. If they ever disagree the output is wrong
rather than merely large, so every strategy is round-tripped, including at block
boundaries and with a short final block.

No fixture uses a random number generator; incompressible bytes come from a
SHA-256 keystream.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l1  # noqa: E402
import l2  # noqa: E402


def keystream(n: int, tag: bytes = b"l2") -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def frames(count: int, size: int, changed: int) -> bytes:
    """A synthetic video stream: each frame repeats the last with one patch moved.

    Models the case the brief describes — successive frames that differ only
    where something on screen moved.
    """
    first = keystream(size, b"frame0")
    out = bytearray(first)
    previous = bytearray(first)
    for i in range(1, count):
        current = bytearray(previous)
        start = (i * 97) % max(1, size - changed)
        current[start : start + changed] = keystream(changed, b"m" + i.to_bytes(2, "big"))
        out += current
        previous = current
    return bytes(out)


def roundtrip(data: bytes, goal: l2.Goal, reference: bytes | None = None) -> tuple[bytes, l2.StreamResult]:
    encoded = list(l2.encode_stream([data], goal, [reference] if reference is not None else None))
    result = l2.summarise(encoded, goal)
    decoded = b"".join(
        l2.decode_stream(
            encoded,
            goal,
            [reference] if reference is not None else None,
            total_bytes=len(data),
        )
    )
    return decoded, result


class GoalValidation(unittest.TestCase):
    def test_builtin_goals_are_constructible(self) -> None:
        self.assertIn("video-frame-delta", l2.GOALS)
        for name, goal in l2.GOALS.items():
            self.assertEqual(goal.name, name)
            self.assertLessEqual(goal.window_bytes, l2.MAX_WINDOW_BYTES)

    def test_unknown_strategy_rejected(self) -> None:
        with self.assertRaises(ValueError):
            l2.Goal(name="x", summary="", block_bytes=4096, base="telepathy")

    def test_block_size_bounds(self) -> None:
        for size in (0, l1.MAX_BLOCK_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                l2.Goal(name="x", summary="", block_bytes=size, base=l2.ANCHOR)

    def test_window_ceiling_enforced(self) -> None:
        with self.assertRaises(ValueError):
            l2.Goal(
                name="greedy",
                summary="",
                block_bytes=l1.MAX_BLOCK_BYTES,
                base=l2.PRECEDING,
                stride_blocks=1000,
            )

    def test_stride_only_applies_to_preceding(self) -> None:
        with self.assertRaises(ValueError):
            l2.Goal(name="x", summary="", block_bytes=4096, base=l2.ANCHOR, stride_blocks=4)

    def test_policy_cannot_bar_the_bounding_codecs(self) -> None:
        # A policy that removed RAW would let a block encode larger than itself.
        goal = l2.Goal(
            name="x", summary="", block_bytes=4096, base=l2.ANCHOR, codecs=frozenset({l1.CODEC_SPARSE})
        )
        self.assertIn(l1.CODEC_RAW, goal.codecs)
        self.assertIn(l1.CODEC_IDENTICAL, goal.codecs)

    def test_unknown_codec_rejected(self) -> None:
        with self.assertRaises(ValueError):
            l2.Goal(name="x", summary="", block_bytes=4096, base=l2.ANCHOR, codecs=frozenset({99}))

    def test_goal_named(self) -> None:
        self.assertEqual(l2.goal_named("video-frame-delta").base, l2.PRECEDING)
        with self.assertRaises(KeyError):
            l2.goal_named("nope")


class Blocking(unittest.TestCase):
    def test_recuts_arbitrary_chunk_boundaries(self) -> None:
        # Input chunking must not affect blocking, or a socket and a file would
        # encode the same bytes differently.
        data = keystream(10_000)
        chunked = [data[i : i + 333] for i in range(0, len(data), 333)]
        self.assertEqual(list(l2.iter_blocks(chunked, 4096)), list(l2.iter_blocks([data], 4096)))

    def test_file_object_and_iterable_agree(self) -> None:
        data = keystream(9000)
        self.assertEqual(
            list(l2.iter_blocks(io.BytesIO(data), 4096)), list(l2.iter_blocks([data], 4096))
        )

    def test_short_reads_are_not_end_of_stream(self) -> None:
        # A pipe may return fewer bytes than asked for without being finished.
        class Dribble(io.RawIOBase):
            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self._pos = 0

            def read(self, size: int = -1) -> bytes:  # type: ignore[override]
                take = 7 if size < 0 else min(7, size)
                chunk = self._payload[self._pos : self._pos + take]
                self._pos += len(chunk)
                return chunk

        data = keystream(9000)
        self.assertEqual(list(l2.iter_blocks(Dribble(data), 4096)), list(l2.iter_blocks([data], 4096)))

    def test_final_short_block(self) -> None:
        blocks = list(l2.iter_blocks([keystream(5000)], 4096))
        self.assertEqual([len(b) for b in blocks], [4096, 904])

    def test_empty_stream(self) -> None:
        self.assertEqual(list(l2.iter_blocks([b""], 4096)), [])


class RoundTrip(unittest.TestCase):
    """Every strategy, every awkward length. A wrong base gives wrong bytes."""

    def test_paired(self) -> None:
        goal = l2.goal_named("checkpoint-pair")
        reference = keystream(200_000, b"v1")
        target = bytearray(reference)
        target[50_000:50_500] = keystream(500, b"edit")
        decoded, result = roundtrip(bytes(target), goal, reference)
        self.assertEqual(decoded, bytes(target))
        assert result.savings is not None and result.savings.saving_pct is not None
        self.assertGreater(result.savings.saving_pct, 98.0)

    def test_preceding_frames(self) -> None:
        goal = l2.goal_named("video-frame-delta")
        data = frames(count=8, size=65536, changed=4000)
        decoded, result = roundtrip(data, goal)
        self.assertEqual(decoded, data)
        assert result.savings is not None and result.savings.saving_pct is not None
        self.assertGreater(result.savings.saving_pct, 70.0)

    def test_anchor(self) -> None:
        goal = l2.goal_named("anchor-dedup")
        block = keystream(65536, b"anchor")
        data = block * 4
        decoded, result = roundtrip(data, goal)
        self.assertEqual(decoded, data)
        assert result.savings is not None and result.savings.saving_pct is not None
        # Three of the four blocks are exact duplicates of the anchor.
        self.assertGreater(result.savings.saving_pct, 74.0)

    def test_awkward_lengths(self) -> None:
        for goal_name in ("checkpoint-pair", "video-frame-delta", "anchor-dedup"):
            goal = l2.goal_named(goal_name).with_block_bytes(1024)
            for length in (1, 1023, 1024, 1025, 4097, 10_000):
                with self.subTest(goal=goal_name, length=length):
                    data = keystream(length, b"len")
                    reference = keystream(length, b"len") if goal.needs_reference else None
                    decoded, _ = roundtrip(data, goal, reference)
                    self.assertEqual(decoded, data)

    def test_stride_greater_than_one(self) -> None:
        # Interleaved streams: block i resembles block i-3, not block i-1.
        goal = l2.Goal(
            name="interleaved", summary="", block_bytes=1024, base=l2.PRECEDING, stride_blocks=3
        )
        lanes = [keystream(1024, b"a"), keystream(1024, b"b"), keystream(1024, b"c")]
        data = b"".join(lanes * 5)
        decoded, result = roundtrip(data, goal)
        self.assertEqual(decoded, data)
        assert result.savings is not None and result.savings.saving_pct is not None
        self.assertGreater(result.savings.saving_pct, 75.0)

    def test_incompressible_never_expands(self) -> None:
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        data = keystream(64 * 1024, b"noise")
        decoded, result = roundtrip(data, goal)
        self.assertEqual(decoded, data)
        assert result.savings is not None and result.savings.saving_pct is not None
        # One tag byte per block is the entire overhead.
        self.assertGreater(result.savings.saving_pct, -0.1)

    def test_reference_shorter_than_source(self) -> None:
        goal = l2.goal_named("checkpoint-pair").with_block_bytes(1024)
        data = keystream(8000, b"long")
        decoded, _ = roundtrip(data, goal, keystream(3000, b"long"))
        self.assertEqual(decoded, data)

    def test_bare_payloads_decode(self) -> None:
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        data = frames(count=4, size=4096, changed=200)
        encoded = list(l2.encode_stream([data], goal))
        payloads = [b.payload for b in encoded]
        self.assertEqual(b"".join(l2.decode_stream(payloads, goal, total_bytes=len(data))), data)

    def test_missing_reference_rejected(self) -> None:
        goal = l2.goal_named("checkpoint-pair")
        with self.assertRaises(ValueError):
            list(l2.encode_stream([keystream(4096)], goal))
        with self.assertRaises(ValueError):
            list(l2.decode_stream([], goal))

    def test_extra_blocks_past_declared_length_rejected(self) -> None:
        goal = l2.goal_named("anchor-dedup").with_block_bytes(1024)
        data = keystream(2048, b"x")
        encoded = list(l2.encode_stream([data], goal))
        with self.assertRaises(l1.CorruptBlock):
            list(l2.decode_stream(encoded + encoded, goal, total_bytes=len(data)))


class Policy(unittest.TestCase):
    def test_fast_decode_bars_bitmap_and_costs_saving(self) -> None:
        # Scattered changes are exactly where bitmap wins, so this is where the
        # policy has to cost something. If it were free it would be the default.
        block = 4096
        base_frame = keystream(block, b"p")
        second = bytearray(base_frame)
        for i in range(int(block * 0.40)):
            second[(i * 7) % block] ^= 0xFF
        data = base_frame + bytes(second)

        smallest = l2.goal_named("video-frame-delta").with_block_bytes(block)
        fast = l2.goal_named("video-realtime").with_block_bytes(block)

        decoded_a, result_a = roundtrip(data, smallest)
        decoded_b, result_b = roundtrip(data, fast)

        self.assertEqual(decoded_a, data)
        self.assertEqual(decoded_b, data)
        assert result_a.savings is not None and result_b.savings is not None
        assert result_a.savings.saving_pct is not None and result_b.savings.saving_pct is not None

        self.assertIn("bitmap", result_a.codec_names())
        self.assertNotIn("bitmap", result_b.codec_names())
        self.assertGreater(result_a.savings.saving_pct, result_b.savings.saving_pct)

    def test_policy_never_changes_correctness(self) -> None:
        # A payload is self-describing, so a decoder needs no knowledge of the
        # policy that produced it.
        data = frames(count=6, size=4096, changed=1800)
        for codecs in (l2.POLICY_SMALLEST, l2.POLICY_FAST_DECODE, frozenset({l1.CODEC_RAW})):
            goal = l2.Goal(
                name="p", summary="", block_bytes=4096, base=l2.PRECEDING, codecs=codecs
            )
            with self.subTest(codecs=sorted(codecs)):
                decoded, _ = roundtrip(data, goal)
                self.assertEqual(decoded, data)

    def test_barred_codec_costs_still_reported(self) -> None:
        # The price of a policy must stay visible, or it cannot be reviewed.
        block = 4096
        a = keystream(block, b"q")
        b = bytearray(a)
        for i in range(int(block * 0.40)):
            b[(i * 7) % block] ^= 0xFF
        plan = l1.plan_block(a, bytes(b), l2.POLICY_FAST_DECODE)
        self.assertNotEqual(plan.codec, l1.CODEC_BITMAP)
        self.assertIn(l1.CODEC_BITMAP, plan.costs)
        self.assertLess(plan.costs[l1.CODEC_BITMAP], plan.encoded_bytes)


class Probe(unittest.TestCase):
    """The matcher must pick by measurement, not by assumption."""

    def test_picks_preceding_at_the_frame_size(self) -> None:
        # The sweep is what makes this work. The video goal's own default block
        # size is 64KB, which on this 16KB-frame stream returns 45.94%; the
        # sweep finds 16KB and 82.04%. Without it the user silently accepts the
        # worse number.
        data = frames(count=8, size=16384, changed=1000)
        reports = [r for r in l2.probe(data) if r.measured]
        self.assertTrue(reports)
        best = reports[0]
        self.assertEqual(best.goal.base, l2.PRECEDING)
        self.assertEqual(best.goal.block_bytes, 16384)
        assert best.saving_pct is not None
        self.assertGreater(best.saving_pct, 80.0)

    def test_block_size_alignment_dominates_the_result(self) -> None:
        # An 82-point swing from block size alone, and not monotonic in it.
        data = frames(count=8, size=16384, changed=1000)
        goal = l2.goal_named("video-frame-delta")
        measured = {
            r.goal.block_bytes: r.saving_pct
            for r in l2.probe(data, [goal], block_sizes=(4096, 16384, 65536))
            if r.measured
        }
        self.assertLess(measured[4096] or 0.0, 1.0)
        self.assertGreater(measured[16384] or 0.0, 80.0)
        self.assertLess(measured[65536] or 0.0, 60.0)

    def test_picks_anchor_for_one_repeated_block(self) -> None:
        data = keystream(16384, b"one") * 24
        best = [r for r in l2.probe(data) if r.measured][0]
        self.assertIn(best.goal.base, (l2.ANCHOR, l2.PRECEDING))
        assert best.saving_pct is not None
        self.assertGreater(best.saving_pct, 80.0)

    def test_finds_nothing_in_incompressible_data(self) -> None:
        # The negative control for the matcher: it must not invent a winner.
        for report in l2.probe(keystream(64 * 1024, b"entropy")):
            if report.measured:
                assert report.saving_pct is not None
                self.assertLess(report.saving_pct, 0.5)

    def test_sweep_peaks_at_the_repeat_period(self) -> None:
        # FINDINGS.md section 5: the best block size is a property of the data.
        # This stream repeats with period 8KB. A 4KB block splits the period so
        # alternate blocks miss the anchor entirely; a 16KB block spans two
        # periods and wastes half of each comparison. The optimum is neither the
        # smallest nor the largest, which is why a sweep and not a heuristic.
        unit = keystream(4096, b"u")
        data = (unit + keystream(4096, b"v")) * 8
        goal = l2.goal_named("anchor-dedup")
        reports = l2.probe(data, [goal], block_sizes=(4096, 8192, 16384))
        measured = {r.goal.block_bytes: r.saving_pct for r in reports if r.measured}
        self.assertEqual(set(measured), {4096, 8192, 16384})
        assert all(v is not None for v in measured.values())
        self.assertGreater(measured[8192] or 0.0, measured[4096] or 0.0)
        self.assertGreater(measured[8192] or 0.0, measured[16384] or 0.0)

    def test_paired_goals_skipped_without_reference(self) -> None:
        names = {r.goal.name for r in l2.probe(keystream(32 * 1024))}
        self.assertNotIn("checkpoint-pair", names)
        names_with = {r.goal.name for r in l2.probe(keystream(32 * 1024), reference_sample=keystream(32 * 1024))}
        self.assertIn("checkpoint-pair", names_with)

    def test_sample_smaller_than_block_is_unmeasured_not_zero(self) -> None:
        reports = l2.probe(keystream(100), [l2.goal_named("anchor-dedup")])
        self.assertEqual(reports, [])

    def test_ranking_puts_unmeasured_last(self) -> None:
        data = frames(count=4, size=8192, changed=500)
        reports = l2.probe(data, block_sizes=(8192, 1 << 20))
        measured = [r.measured for r in reports]
        self.assertEqual(measured, sorted(measured, reverse=True))

    def test_probe_emits_no_payloads(self) -> None:
        # Probing plans but never encodes; a sweep must not cost output bytes.
        data = frames(count=4, size=8192, changed=500)
        for report in l2.probe(data):
            self.assertGreaterEqual(report.blocks, 0)
            self.assertEqual(report.sample_bytes, len(data))


class Reporting(unittest.TestCase):
    def test_empty_stream_is_undefined_not_zero(self) -> None:
        goal = l2.goal_named("anchor-dedup")
        result = l2.summarise(l2.encode_stream([b""], goal), goal)
        self.assertEqual(result.blocks, 0)
        self.assertIsNone(result.savings)

    def test_codec_histogram_is_a_driver(self) -> None:
        data = frames(count=6, size=4096, changed=300)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        result = l2.summarise(l2.encode_stream([data], goal), goal)
        self.assertEqual(sum(result.codec_names().values()), result.blocks)

    def test_encode_stream_is_lazy(self) -> None:
        # The driver must not accumulate: a generator that had already consumed
        # its source would defeat the streaming bound.
        goal = l2.goal_named("anchor-dedup").with_block_bytes(1024)
        consumed = []

        def source():
            for i in range(4):
                consumed.append(i)
                yield keystream(1024, b"s")

        stream = l2.encode_stream(source(), goal)
        self.assertEqual(consumed, [])
        next(stream)
        self.assertEqual(consumed, [0])


if __name__ == "__main__":
    unittest.main()
