"""The command line, exercised through `main()` so exit codes are covered."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.squeeze.cli import main

POLICY = "policies/footprint.sqz"

BROKEN = """version 1
class orphan : timeseries {
  record 24 bytes
  retain nowhere 7 days
}
"""

# Deliberately over budget: 1 record per day for 30 days at 1 MiB, capped at
# 1 KiB. The plan must compile, and `--strict` must fail on it.
OVER_BUDGET = """version 1
codec none_useful { kind lossless; ratio 1; applies_to blob; impl raw }
tier warm { medium disk; codecs none_useful }
class blobs : blob {
  record  1 MiB
  growth  1 records per fleet per day
  retain  warm 30 days
}
policy p { objective minimise bytes; limit total 1 KiB; covers blobs }
"""


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestCli(unittest.TestCase):
    def test_check_the_committed_policy(self) -> None:
        code, out, _ = run("check", POLICY)
        self.assertEqual(code, 0)
        self.assertIn("ok", out)

    def test_plan_renders_a_report(self) -> None:
        code, out, _ = run("plan", POLICY)
        self.assertEqual(code, 0)
        self.assertIn("drivers", out)
        self.assertIn("limits", out)

    def test_plan_json_is_parseable(self) -> None:
        code, out, _ = run("plan", POLICY, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["policy"], "footprint_v1")
        self.assertTrue(payload[0]["is_provable"])

    def test_strict_plan_passes_on_the_committed_policy(self) -> None:
        self.assertEqual(run("plan", POLICY, "--strict")[0], 0)

    def test_verify_strict_passes_on_the_committed_policy(self) -> None:
        self.assertEqual(run("verify", POLICY, "--strict")[0], 0)

    def test_unknown_policy_name_is_a_usage_error(self) -> None:
        code, _, err = run("plan", POLICY, "--policy", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no policy named", err)

    def test_missing_file_is_a_usage_error(self) -> None:
        code, _, err = run("check", "policies/does-not-exist.sqz")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_errors_are_printed_with_location_and_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.sqz"
            path.write_text(BROKEN, encoding="utf-8")
            code, _, err = run("check", str(path))
        self.assertEqual(code, 1)
        self.assertIn("SQZ0351", err)
        self.assertIn("broken.sqz:", err)

    def test_strict_fails_on_an_exceeded_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "over.sqz"
            path.write_text(OVER_BUDGET, encoding="utf-8")
            lenient, out, _ = run("plan", str(path))
            strict, _, err = run("plan", str(path), "--strict")
        self.assertEqual(lenient, 0)
        self.assertIn("EXCEEDED", out)
        self.assertEqual(strict, 1)
        self.assertIn("not proven", err)


if __name__ == "__main__":
    unittest.main()
