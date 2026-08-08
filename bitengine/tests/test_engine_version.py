"""Tests for the mixed-module guard.

    cd bitengine && python3 -m unittest discover -s tests -t .

`l1`, `l2` and `l3` are one unit. Copying one of them onto a machine and leaving
the others is the most likely way this project breaks in the field, and before
this guard existed it surfaced as

    TypeError: Goal.__init__() got an unexpected keyword argument
    'keyframe_interval'

raised inside `dataclasses`, five frames below anything naming the real cause.
The guard has to fail on a mismatch and stay silent on a matched set, so both
directions are asserted here.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import l1  # noqa: E402
import l2  # noqa: E402
import l3  # noqa: E402
import webui  # noqa: E402


class Markers(unittest.TestCase):
    def test_the_three_tiers_agree(self) -> None:
        self.assertEqual(l1.ENGINE_VERSION, l3.ENGINE_VERSION)
        self.assertEqual(l2.ENGINE_VERSION, l3.ENGINE_VERSION)

    def test_the_dashboard_agrees_with_the_engine(self) -> None:
        self.assertEqual(webui.ENGINE_VERSION, l3.ENGINE_VERSION)

    def test_each_module_carries_its_own_literal(self) -> None:
        """Not `l1.ENGINE_VERSION` re-exported.

        A stale l2.py that read its version from whichever l1.py sat next to it
        would report the current version and defeat the check entirely.
        """
        for module in (l1, l2, l3):
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                f"{module.__name__}.py",
            )
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn(
                f'ENGINE_VERSION = "{module.ENGINE_VERSION}"',
                source,
                f"{module.__name__}.py must define its version as a literal",
            )


class L3Guard(unittest.TestCase):
    """L3 imports l1 and l2, so it is where a spanning check belongs."""

    def test_a_matched_set_passes(self) -> None:
        l3.check_engine_modules()  # must not raise

    def test_an_older_l2_is_refused(self) -> None:
        with mock.patch.object(l2, "ENGINE_VERSION", "1.0"):
            with self.assertRaises(l3.EngineMismatch) as caught:
                l3.check_engine_modules()
        self.assertIn("l2.py is 1.0", str(caught.exception))

    def test_a_module_with_no_marker_is_refused(self) -> None:
        # The real report came from a file predating version markers entirely.
        with mock.patch.object(l1, "ENGINE_VERSION", None):
            with self.assertRaises(l3.EngineMismatch) as caught:
                l3.check_engine_modules()
        self.assertIn("before version markers existed", str(caught.exception))

    def test_the_message_says_how_to_fix_it(self) -> None:
        with mock.patch.object(l2, "ENGINE_VERSION", "0.9"):
            with self.assertRaises(l3.EngineMismatch) as caught:
                l3.check_engine_modules()
        self.assertIn("bundle.py", str(caught.exception))


class DashboardGuard(unittest.TestCase):
    def test_a_matched_set_passes(self) -> None:
        webui.check_engine_modules()  # must not raise

    def test_a_goal_missing_a_field_is_refused(self) -> None:
        """The exact failure from the field report.

        A matching version marker is necessary and not sufficient: someone can
        drop a Goal field without bumping it, and `build_goal` sets these by
        name.
        """
        kept = tuple(f for f in dataclasses.fields(l2.Goal) if f.name != "keyframe_interval")
        with mock.patch.object(dataclasses, "fields", return_value=kept):
            with self.assertRaises(webui.EngineMismatch) as caught:
                webui.check_engine_modules()
        self.assertIn("keyframe_interval", str(caught.exception))

    def test_every_field_build_goal_sets_is_required(self) -> None:
        # If build_goal learns a new override, the guard must learn it too, or
        # the drift it exists to catch becomes invisible again.
        self.assertLessEqual(
            {"block_bytes", "keyframe_interval", "codecs"}, webui._REQUIRED_GOAL_FIELDS
        )
        self.assertLessEqual(
            webui._REQUIRED_GOAL_FIELDS, {f.name for f in dataclasses.fields(l2.Goal)}
        )

    def test_build_goal_accepts_the_overrides_the_dashboard_offers(self) -> None:
        # The end-to-end call that failed in the report, against the real Goal.
        goal = webui.build_goal(
            l2.goal_named("video-frame-delta"),
            block_bytes=8192,
            keyframe_interval=8,
            fast_decode=True,
        )
        self.assertEqual(goal.block_bytes, 8192)
        self.assertEqual(goal.keyframe_interval, 8)


if __name__ == "__main__":
    unittest.main()
