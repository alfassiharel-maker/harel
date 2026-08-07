"""Tests for the BitEngine command line interface.

    cd bitengine && python3 -m unittest discover -s tests -t .

The CLI is the only part a user touches, so what is asserted here is the
contract they rely on: exit codes that mean something, a `pack` that refuses to
report a saving it cannot reverse, and an undefined percentage that prints as
`n/a` rather than as `0.00%`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli  # noqa: E402
import l2  # noqa: E402
import l3  # noqa: E402


def keystream(n: int, tag: bytes = b"cli") -> bytes:
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


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Formatting(unittest.TestCase):
    def test_parse_size(self) -> None:
        self.assertEqual(cli.parse_size("4096"), 4096)
        self.assertEqual(cli.parse_size("4KB"), 4096)
        self.assertEqual(cli.parse_size("1MB"), 1024**2)
        self.assertEqual(cli.parse_size("2gb"), 2 * 1024**3)

    def test_undefined_percentage_is_not_zero(self) -> None:
        # A saving that was never measured must not read like a measured zero.
        self.assertEqual(cli.format_pct(None), "n/a")
        self.assertEqual(cli.format_pct(0.0), "0.00%")

    def test_negative_saving_is_shown(self) -> None:
        self.assertEqual(cli.format_pct(-0.02), "-0.02%")


class Fixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.source = os.path.join(self.dir, "source.bin")
        self.container = os.path.join(self.dir, "out.bite")
        self.restored = os.path.join(self.dir, "restored.bin")
        self.data = frames(24, 4096, 300)
        with open(self.source, "wb") as handle:
            handle.write(self.data)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, payload: bytes) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path


class Goals(unittest.TestCase):
    def test_lists_every_goal(self) -> None:
        code, out, _ = run("goals")
        self.assertEqual(code, cli.EXIT_OK)
        for name in l2.GOALS:
            self.assertIn(name, out)

    def test_marks_goals_needing_a_reference(self) -> None:
        _, out, _ = run("goals")
        self.assertIn("needs --reference", out)


class Probe(Fixtures):
    def test_ranks_and_names_a_winner(self) -> None:
        code, out, _ = run("probe", self.source)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Best:", out)
        self.assertIn("video-frame-delta", out)

    def test_says_paired_goals_were_skipped(self) -> None:
        _, out, _ = run("probe", self.source)
        self.assertIn("No --reference given", out)
        self.assertNotIn("checkpoint-pair", out)

    def test_paired_goal_considered_with_reference(self) -> None:
        reference = self.write("ref.bin", self.data)
        _, out, _ = run("probe", self.source, "--reference", reference)
        self.assertIn("checkpoint-pair", out)

    def test_empty_file(self) -> None:
        code, out, _ = run("probe", self.write("empty.bin", b""))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("empty", out)

    def test_file_too_small_to_measure(self) -> None:
        code, out, _ = run("probe", self.write("tiny.bin", b"hello"))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("smaller than two blocks", out)


class Pack(Fixtures):
    def test_auto_round_trip(self) -> None:
        code, out, _ = run("pack", self.source, self.container)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("PASS (sha256)", out)
        self.assertIn("Probe (sample):", out)

        code, _, _ = run("unpack", self.container, self.restored)
        self.assertEqual(code, cli.EXIT_OK)
        with open(self.restored, "rb") as handle:
            self.assertEqual(handle.read(), self.data)

    def test_explicit_goal_skips_probing(self) -> None:
        code, out, _ = run("pack", self.source, self.container, "--goal", "video-frame-delta")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("Probe (sample):", out)
        self.assertIn("video-frame-delta", out)

    def test_block_size_override(self) -> None:
        code, out, _ = run(
            "pack", self.source, self.container, "--goal", "video-frame-delta", "--block-size", "4KB"
        )
        self.assertEqual(code, cli.EXIT_OK)
        with l3.read_container(self.container) as reader:
            self.assertEqual(reader.goal.block_bytes, 4096)

    def test_keyframe_interval_bounds_random_access(self) -> None:
        run("pack", self.source, self.container, "--goal", "video-frame-delta",
            "--block-size", "4KB", "--keyframe-interval", "4")
        with l3.read_container(self.container) as reader:
            self.assertEqual(reader.goal.keyframe_interval, 4)
            self.assertLessEqual(reader.chain_length(reader.manifest.block_count - 1), 4)
            self.assertTrue(reader.verify())

    def test_fast_decode_bars_bitmap(self) -> None:
        run("pack", self.source, self.container, "--goal", "video-frame-delta",
            "--block-size", "4KB", "--fast-decode")
        with l3.read_container(self.container) as reader:
            self.assertNotIn("bitmap", reader.codec_histogram())
            self.assertTrue(reader.verify())

    def test_paired_goal_without_reference_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            run("pack", self.source, self.container, "--goal", "checkpoint-pair")

    def test_paired_round_trip(self) -> None:
        reference = self.write("ref.bin", self.data)
        target = bytearray(self.data)
        target[5000:5400] = keystream(400, b"edit")
        source = self.write("target.bin", bytes(target))

        code, out, _ = run("pack", source, self.container, "--goal", "checkpoint-pair",
                           "--reference", reference)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("PASS (sha256)", out)

        code, _, _ = run("unpack", self.container, self.restored, "--reference", reference)
        self.assertEqual(code, cli.EXIT_OK)
        with open(self.restored, "rb") as handle:
            self.assertEqual(handle.read(), bytes(target))

    def test_empty_source_refused(self) -> None:
        with self.assertRaises(SystemExit):
            run("pack", self.write("empty.bin", b""), self.container)

    def test_unknown_goal(self) -> None:
        code, _, err = run("pack", self.source, self.container, "--goal", "teleport")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("teleport", err)

    def test_missing_source(self) -> None:
        code, _, err = run("pack", os.path.join(self.dir, "nope.bin"), self.container)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("not found", err)

    def test_reports_a_real_saving(self) -> None:
        _, out, _ = run("pack", self.source, self.container, "--goal", "video-frame-delta",
                        "--block-size", "4KB")
        self.assertIn("saving", out)
        self.assertGreater(os.path.getsize(self.source), os.path.getsize(self.container))


class Unpack(Fixtures):
    def setUp(self) -> None:
        super().setUp()
        run("pack", self.source, self.container, "--goal", "video-frame-delta", "--block-size", "4KB")

    def test_verifies_by_default(self) -> None:
        code, out, _ = run("unpack", self.container, self.restored)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("PASS (sha256)", out)

    def test_no_verify_says_so(self) -> None:
        code, out, _ = run("unpack", self.container, self.restored, "--no-verify")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("skipped", out)

    def test_corrupt_container_fails_verification(self) -> None:
        with open(self.container, "r+b") as handle:
            handle.seek(l3._HEADER.size + 50)
            original = handle.read(1)
            handle.seek(l3._HEADER.size + 50)
            handle.write(bytes([original[0] ^ 0xFF]))

        code, out, err = run("unpack", self.container, self.restored)
        self.assertEqual(code, cli.EXIT_VERIFY_FAILED)
        self.assertIn("FAIL", out + err)

    def test_not_a_container(self) -> None:
        junk = self.write("junk.bin", b"this is not a container, it is prose" * 20)
        code, _, err = run("unpack", junk, self.restored)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("not a BitEngine container", err)


class Inspect(Fixtures):
    def setUp(self) -> None:
        super().setUp()
        run("pack", self.source, self.container, "--goal", "video-frame-delta", "--block-size", "4KB")

    def test_reports_the_manifest(self) -> None:
        code, out, _ = run("inspect", self.container)
        self.assertEqual(code, cli.EXIT_OK)
        for field in ("goal", "base", "block size", "blocks", "saving", "sha256", "codecs"):
            self.assertIn(field, out)

    def test_warns_about_unbounded_random_access(self) -> None:
        code, out, _ = run("inspect", self.container)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("random access", out)
        self.assertIn("--keyframe-interval", out)

    def test_no_warning_when_keyframes_bound_it(self) -> None:
        bounded = os.path.join(self.dir, "kf.bite")
        run("pack", self.source, bounded, "--goal", "video-frame-delta",
            "--block-size", "4KB", "--keyframe-interval", "4")
        _, out, _ = run("inspect", bounded)
        self.assertNotIn("repack with", out)


if __name__ == "__main__":
    unittest.main()
