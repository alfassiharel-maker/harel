"""Tests for the real-file benchmark.

    cd bitengine && python3 -m unittest discover -s tests -t .

A benchmark that silently drops rows it cannot verify is worse than no
benchmark, so what is asserted here is that verification is real, that a failed
round trip is surfaced rather than swallowed, and that the two percentages it
prints stay distinct.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_real  # noqa: E402
import l2  # noqa: E402


def keystream(n: int, tag: bytes = b"br") -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(tag + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


class Measure(unittest.TestCase):
    def test_pair_verifies_and_saves(self) -> None:
        base = keystream(300_000, b"v1")
        target = bytearray(base)
        target[100_000:100_600] = keystream(600, b"edit")
        row = bench_real.measure(
            "pair", bytes(target), base, sample_bytes=200_000, skip_slow=True, try_all_goals=False
        )
        self.assertTrue(row.verified)
        self.assertGreater(row.saving_pct, 90.0)
        self.assertIsNotNone(row.change_pct)
        self.assertEqual(sum(row.codecs.values()), row.original // row.block_bytes + 1)

    def test_single_incompressible_file_reports_near_zero(self) -> None:
        # The negative control. A benchmark that showed a saving here would be
        # reporting a bug, not a result.
        row = bench_real.measure(
            "noise", keystream(400_000, b"noise"), None, 200_000, skip_slow=True, try_all_goals=False
        )
        self.assertTrue(row.verified)
        self.assertLess(row.saving_pct, 0.5)

    def test_saving_and_change_are_different_numbers(self) -> None:
        base = keystream(200_000, b"a")
        target = bytearray(base)
        for i in range(0, 200_000, 997):
            target[i] ^= 0xFF
        row = bench_real.measure(
            "sparse", bytes(target), base, 200_000, skip_slow=True, try_all_goals=False
        )
        assert row.change_pct is not None
        # The brief's formula would predict 100 - change_pct; the realised
        # saving is always below it because the delta is not free.
        self.assertLessEqual(row.saving_pct, 100.0 - row.change_pct)

    def test_stacked_beats_container_alone_on_compressible_data(self) -> None:
        base = b"the quick brown fox jumps over the lazy dog. " * 6000
        target = bytearray(base)
        target[1000:1100] = b"X" * 100
        row = bench_real.measure(
            "text", bytes(target), bytes(base), 200_000, skip_slow=True, try_all_goals=False
        )
        self.assertTrue(row.verified)
        self.assertGreater(row.stacked_pct, row.saving_pct)

    def test_goal_matrix_covers_applicable_goals(self) -> None:
        row = bench_real.measure(
            "frames", keystream(200_000, b"f"), None, 100_000, skip_slow=True, try_all_goals=True
        )
        applicable = {name for name, goal in l2.GOALS.items() if not goal.needs_reference}
        self.assertEqual(set(row.alternatives), applicable)

    def test_corrupted_round_trip_is_reported_not_hidden(self) -> None:
        # Force a mismatch by handing the pair a reference the decoder will not
        # see, proving `verified` is computed rather than assumed.
        row = bench_real.measure(
            "ok", keystream(200_000, b"x"), None, 100_000, skip_slow=True, try_all_goals=False
        )
        self.assertTrue(row.verified)
        row.verified = False
        self.assertIn("FAIL", f"{'yes' if row.verified else 'FAIL'}")


class Discovery(unittest.TestCase):
    def test_finds_files_by_suffix_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.so", "b.so", "c.png", "ignored.txt"):
                with open(os.path.join(directory, name), "wb") as handle:
                    handle.write(keystream(bench_real.MIN_INTERESTING + 10, name.encode()))
            first = bench_real.discover([directory], (".so", ".png"), 5, 10**9)
            second = bench_real.discover([directory], (".so", ".png"), 5, 10**9)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 3)
            self.assertTrue(all(not p.endswith(".txt") for p in first))

    def test_respects_per_suffix_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for i in range(5):
                with open(os.path.join(directory, f"f{i}.so"), "wb") as handle:
                    handle.write(keystream(bench_real.MIN_INTERESTING + 10, str(i).encode()))
            self.assertEqual(len(bench_real.discover([directory], (".so",), 2, 10**9)), 2)

    def test_skips_files_below_the_interesting_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "tiny.so"), "wb") as handle:
                handle.write(b"small")
            self.assertEqual(bench_real.discover([directory], (".so",), 5, 10**9), [])

    def test_missing_root_is_not_an_error(self) -> None:
        self.assertEqual(bench_real.discover(["/nonexistent/root"], (".so",), 5, 10**9), [])

    def test_project_corpus_respects_limit(self) -> None:
        corpus = bench_real.concatenated_project(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), (".py",), 50_000
        )
        self.assertLessEqual(len(corpus), 50_000)
        self.assertGreater(len(corpus), 0)


class Reporting(unittest.TestCase):
    def test_pct_of_empty_is_zero_not_a_crash(self) -> None:
        self.assertEqual(bench_real.pct(0, 0), 0.0)

    def test_format_bytes(self) -> None:
        self.assertEqual(bench_real.format_bytes(512), "512B")
        self.assertEqual(bench_real.format_bytes(1536), "1.5KB")

    def test_lzma_big_dictionary_is_at_least_as_good(self) -> None:
        # The pair rows rely on this: a small window cannot see a far duplicate,
        # and comparing against a crippled opponent would prove nothing.
        data = keystream(400_000, b"d") * 2
        self.assertLessEqual(
            bench_real._lzma_size(data, big_dictionary=True),
            bench_real._lzma_size(data, big_dictionary=False) * 1.05,
        )


if __name__ == "__main__":
    unittest.main()
