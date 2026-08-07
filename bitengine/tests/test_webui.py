"""Tests for the dashboard's engine glue.

    cd bitengine && python3 -m unittest discover -s tests -t .

`app.py` is a thin Streamlit layer over `webui.py`, and all the behaviour a user
sees lives here — so these run without Streamlit installed.

The property that matters is that the page cannot show a saving for a container
that does not decode back. `pack` verifies as part of producing its numbers, and
that is asserted rather than assumed.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l1  # noqa: E402
import l2  # noqa: E402
import l3  # noqa: E402
import webui  # noqa: E402


def keystream(n: int, tag: bytes = b"web") -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def frames(count: int, size: int, changed: int) -> bytes:
    first = keystream(size, b"f0")
    out = bytearray(first)
    previous = bytearray(first)
    for i in range(1, count):
        current = bytearray(previous)
        start = (i * 97) % max(1, size - changed)
        current[start : start + changed] = keystream(changed, b"m" + i.to_bytes(2, "big"))
        out += current
        previous = current
    return bytes(out)


class Formatting(unittest.TestCase):
    def test_undefined_is_not_zero(self) -> None:
        self.assertEqual(webui.format_pct(None), "n/a")
        self.assertEqual(webui.format_pct(0.0), "0.00%")

    def test_bytes(self) -> None:
        self.assertEqual(webui.format_bytes(512), "512 B")
        self.assertEqual(webui.format_bytes(2048), "2.00 KB")


class GoalSelection(unittest.TestCase):
    def test_choices_depend_on_whether_a_reference_was_given(self) -> None:
        without = {g.name for g in webui.goal_choices(has_reference=False)}
        with_reference = {g.name for g in webui.goal_choices(has_reference=True)}
        self.assertIn("video-frame-delta", without)
        self.assertNotIn("checkpoint-pair", without)
        self.assertEqual(with_reference, {"checkpoint-pair"})
        self.assertFalse(without & with_reference)

    def test_probe_picks_the_frame_size(self) -> None:
        data = frames(10, 16384, 900)
        goal, reports = webui.choose_goal(data, None)
        self.assertIsNotNone(goal)
        assert goal is not None
        self.assertEqual(goal.base, l2.PRECEDING)
        self.assertEqual(goal.block_bytes, 16384)
        self.assertTrue(any(r.measured for r in reports))

    def test_unmeasurable_input_returns_none_not_a_default(self) -> None:
        # A goal the UI never measured must not be presented as if it had been.
        goal, reports = webui.choose_goal(b"tiny", None)
        self.assertIsNone(goal)
        self.assertFalse([r for r in reports if r.measured])

    def test_build_goal_applies_overrides(self) -> None:
        base = l2.goal_named("video-frame-delta")
        goal = webui.build_goal(base, block_bytes=8192, keyframe_interval=4, fast_decode=True)
        self.assertEqual(goal.block_bytes, 8192)
        self.assertEqual(goal.keyframe_interval, 4)
        self.assertNotIn(l1.CODEC_BITMAP, goal.codecs)

    def test_build_goal_without_changes_returns_the_same_goal(self) -> None:
        base = l2.goal_named("anchor-dedup")
        self.assertIs(webui.build_goal(base), base)


class Pack(unittest.TestCase):
    def test_verifies_and_reports_real_numbers(self) -> None:
        data = frames(16, 8192, 400)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(8192)
        outcome = webui.pack(data, None, goal)

        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.original_bytes, len(data))
        self.assertEqual(outcome.container_bytes, len(outcome.container))
        assert outcome.saving_pct is not None
        self.assertGreater(outcome.saving_pct, 80.0)
        self.assertGreater(outcome.encode_mbs, 0.0)
        self.assertEqual(len(outcome.blocks), outcome.manifest.block_count)
        self.assertEqual(sum(outcome.codec_counts.values()), outcome.manifest.block_count)

    def test_block_rows_match_the_container_on_disk(self) -> None:
        data = frames(8, 4096, 300)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        outcome = webui.pack(data, None, goal)
        payload_total = sum(b.payload_bytes for b in outcome.blocks)
        # Payloads plus the header and the four-byte-per-block index.
        self.assertEqual(payload_total + l3._HEADER.size + 4 * len(outcome.blocks), len(outcome.container))
        self.assertEqual(sum(b.original_bytes for b in outcome.blocks), len(data))

    def test_change_and_saving_are_distinct(self) -> None:
        data = frames(12, 4096, 900)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        outcome = webui.pack(data, None, goal)
        assert outcome.change_pct is not None and outcome.saving_pct is not None
        self.assertLessEqual(outcome.saving_pct, 100.0 - outcome.change_pct)

    def test_paired_pack_round_trips(self) -> None:
        reference = keystream(120_000, b"v1")
        target = bytearray(reference)
        target[40_000:40_500] = keystream(500, b"edit")
        outcome = webui.pack(bytes(target), reference, l2.goal_named("checkpoint-pair"))
        self.assertTrue(outcome.verified)
        result = webui.unpack(outcome.container, reference)
        self.assertTrue(result.verified)
        self.assertEqual(result.data, bytes(target))

    def test_paired_goal_without_reference_is_refused_before_encoding(self) -> None:
        with self.assertRaises(ValueError):
            webui.pack(keystream(50_000), None, l2.goal_named("checkpoint-pair"))

    def test_empty_target_refused(self) -> None:
        with self.assertRaises(ValueError):
            webui.pack(b"", None, l2.goal_named("anchor-dedup"))

    def test_oversized_upload_refused(self) -> None:
        original = webui.MAX_UPLOAD_BYTES
        try:
            webui.MAX_UPLOAD_BYTES = 1024
            with self.assertRaises(ValueError) as caught:
                webui.pack(keystream(4096), None, l2.goal_named("anchor-dedup"))
            self.assertIn("upload limit", str(caught.exception))
        finally:
            webui.MAX_UPLOAD_BYTES = original

    def test_incompressible_input_reports_a_negative_saving_honestly(self) -> None:
        data = keystream(64 * 1024, b"noise")
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        outcome = webui.pack(data, None, goal)
        self.assertTrue(outcome.verified)
        assert outcome.saving_pct is not None
        self.assertLess(outcome.saving_pct, 0.5)

    def test_filename_has_a_bite_suffix(self) -> None:
        outcome = webui.pack(
            frames(4, 4096, 100), None, l2.goal_named("video-frame-delta").with_block_bytes(4096)
        )
        self.assertEqual(outcome.filename("model.safetensors"), "model.bite")
        self.assertEqual(outcome.filename("noextension"), "noextension.bite")


class Unpack(unittest.TestCase):
    def setUp(self) -> None:
        self.data = frames(12, 4096, 300)
        self.goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        self.container = webui.pack(self.data, None, self.goal).container

    def test_round_trip(self) -> None:
        result = webui.unpack(self.container, None)
        self.assertTrue(result.verified)
        self.assertEqual(result.data, self.data)
        self.assertEqual(result.restored_bytes, len(self.data))

    def test_corrupt_container_reports_failure_rather_than_data(self) -> None:
        broken = bytearray(self.container)
        broken[l3._HEADER.size + 40] ^= 0xFF
        result = webui.unpack(bytes(broken), None)
        self.assertFalse(result.verified)

    def test_not_a_container(self) -> None:
        with self.assertRaises(l3.ContainerError):
            webui.unpack(b"this is not a container" * 100, None)

    def test_empty_upload(self) -> None:
        with self.assertRaises(ValueError):
            webui.unpack(b"", None)

    def test_truncated_index_is_rejected(self) -> None:
        with self.assertRaises(l3.ContainerError):
            webui.unpack(self.container[:-3], None)

    def test_hostile_block_count_is_rejected_before_allocation(self) -> None:
        fields = list(l3._HEADER.unpack(self.container[: l3._HEADER.size]))
        fields[6] = 2**31  # block_count
        hostile = l3._HEADER.pack(*fields) + self.container[l3._HEADER.size :]
        with self.assertRaises(l3.ContainerError):
            webui.unpack(hostile, None)


class Inspect(unittest.TestCase):
    def test_reports_manifest_without_decoding(self) -> None:
        data = frames(16, 4096, 300)
        goal = l2.goal_named("video-frame-delta").with_block_bytes(4096)
        container = webui.pack(data, None, goal).container

        report = webui.inspect_container(container)
        self.assertEqual(report.manifest.total_bytes, len(data))
        self.assertEqual(len(report.blocks), report.manifest.block_count)
        self.assertEqual(sum(report.codec_counts.values()), report.manifest.block_count)
        self.assertEqual(report.deepest_chain, report.manifest.block_count)
        self.assertFalse(report.random_access_is_bounded)

    def test_keyframes_bound_random_access(self) -> None:
        data = frames(16, 4096, 300)
        goal = l2.goal_named("video-frame-delta").replace(block_bytes=4096, keyframe_interval=4)
        container = webui.pack(data, None, goal).container
        report = webui.inspect_container(container)
        self.assertLessEqual(report.deepest_chain, 4)
        self.assertTrue(report.random_access_is_bounded)


class BlockMap(unittest.TestCase):
    def test_collapses_consecutive_same_codec_blocks(self) -> None:
        rows = [webui.BlockRow(i, "runs", 10, 4096) for i in range(50)]
        self.assertEqual(webui.block_map_segments(rows), [("runs", 50)])

    def test_preserves_transitions(self) -> None:
        rows = [
            webui.BlockRow(0, "raw", 4097, 4096),
            webui.BlockRow(1, "runs", 10, 4096),
            webui.BlockRow(2, "runs", 12, 4096),
            webui.BlockRow(3, "raw", 4097, 4096),
        ]
        self.assertEqual(webui.block_map_segments(rows), [("raw", 1), ("runs", 2), ("raw", 1)])

    def test_bounded_so_a_browser_can_draw_it(self) -> None:
        rows = [webui.BlockRow(i, "raw" if i % 2 else "runs", 10, 4096) for i in range(10_000)]
        self.assertLessEqual(len(webui.block_map_segments(rows, max_segments=100)), 100)

    def test_every_codec_has_a_colour(self) -> None:
        self.assertEqual(set(webui.codec_palette()), set(l1.CODEC_NAMES.values()))

    def test_empty(self) -> None:
        self.assertEqual(webui.block_map_segments([]), [])


class Baselines(unittest.TestCase):
    @unittest.skipIf(webui.zstandard is None, "zstandard not installed")
    def test_zstd_patch_baseline_is_measured_on_the_same_inputs(self) -> None:
        reference = keystream(200_000, b"v1")
        target = bytearray(reference)
        target[50_000:50_400] = keystream(400, b"edit")
        baseline = webui.zstd_patch_baseline(bytes(target), reference)
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertTrue(baseline.verified)
        self.assertGreater(baseline.saving_pct, 90.0)

    @unittest.skipIf(webui.zstandard is None, "zstandard not installed")
    def test_pack_attaches_the_baseline_for_pairs(self) -> None:
        reference = keystream(120_000, b"v1")
        target = bytearray(reference)
        target[30_000:30_400] = keystream(400, b"e")
        outcome = webui.pack(bytes(target), reference, l2.goal_named("checkpoint-pair"))
        self.assertTrue(outcome.baselines)
        self.assertIn("patch-from", outcome.baselines[0].name)

    def test_no_baseline_for_single_files(self) -> None:
        # There is no same-information rival without a reference, so none is claimed.
        outcome = webui.pack(
            frames(8, 4096, 200), None, l2.goal_named("video-frame-delta").with_block_bytes(4096)
        )
        self.assertEqual(outcome.baselines, [])


if __name__ == "__main__":
    unittest.main()


class StreamlitApp(unittest.TestCase):
    """A smoke test for app.py itself, skipped when Streamlit is absent.

    webui.py covers the behaviour, but nothing else would catch a typo in a
    Streamlit API call inside app.py — the failure would only appear in a
    browser. AppTest executes the script the way the server does.
    """

    def test_app_script_runs_without_exceptions(self) -> None:
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit not installed")

        app_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
        )
        harness = AppTest.from_file(app_path, default_timeout=120)
        harness.run()

        self.assertEqual(
            [e.value for e in harness.exception], [], "app.py raised while rendering"
        )
        self.assertEqual([t.value for t in harness.title], ["BitEngine"])
        self.assertEqual(len(harness.tabs), 3, "expected Pack, Unpack and Inspect tabs")
