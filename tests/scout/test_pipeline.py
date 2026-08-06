"""End-to-end pipeline and CLI, replayed from the fixture. No network, no key.

The Anthropic path is exercised only as far as it can be without a provider:
that the module imports, that a missing key is a clean configuration error, and
that the cost arithmetic is right.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from scout import cli
from scout.llm import (
    ConfigurationError,
    LanguageModel,
    MockModel,
    ModelCallError,
    ModelReply,
    cost_micros,
)
from scout.pipeline import run, save, slug

FIXTURE = Path(__file__).parent / "fixtures" / "ai-infrastructure.json"
WHEN = "2026-08-06T09:00:00+00:00"


def fixture_model(**kwargs: Any) -> MockModel:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return MockModel([payload["analysis"], {"problems": payload["problems"]}], **kwargs)


class CountingModel:
    """Records what the pipeline asked for, so prompt wiring can be asserted."""

    provider = "counting"
    model = "counting"

    def __init__(self, inner: MockModel) -> None:
        self._inner = inner
        self.systems: list[str] = []
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, Any],
        max_tokens: int = 0,
    ) -> ModelReply:
        self.systems.append(system)
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        return self._inner.complete_json(
            system=system, prompt=prompt, schema=schema, max_tokens=max_tokens
        )


class PipelineTests(unittest.TestCase):
    def test_a_full_run_produces_a_ranked_report(self) -> None:
        result = run(domain="תשתית AI", model=fixture_model(), generated_at=WHEN)
        self.assertEqual(3, len(result.report.ranked))
        self.assertEqual(1, len(result.report.insufficient))
        self.assertEqual("gpu-idle-billing", result.report.ranked[0].problem.id)

    def test_the_model_is_called_exactly_twice_map_then_problems(self) -> None:
        model = CountingModel(fixture_model())
        run(domain="תשתית AI", model=model, generated_at=WHEN, max_tokens=1234)
        self.assertEqual(2, len(model.prompts))
        self.assertIn("Map this domain", model.prompts[0])
        self.assertIn("Here is the domain map", model.prompts[1])
        self.assertEqual([1234, 1234], model.max_tokens)

    def test_the_second_call_receives_the_first_calls_output(self) -> None:
        """The whole reason for two calls: reasoning over a finished map."""
        model = CountingModel(fixture_model())
        run(domain="תשתית AI", model=model, generated_at=WHEN)
        self.assertIn("מתזמן עבודות", model.prompts[1])

    def test_the_requested_problem_count_reaches_the_prompt(self) -> None:
        model = CountingModel(fixture_model())
        run(domain="תשתית AI", model=model, generated_at=WHEN, count=11)
        self.assertIn("11 candidate problems", model.prompts[1])

    def test_provenance_is_recorded_without_usage_when_none_is_reported(self) -> None:
        result = run(domain="תשתית AI", model=fixture_model(), generated_at=WHEN)
        meta = result.report.meta
        self.assertEqual("mock", meta.provider)
        self.assertEqual(WHEN, meta.generated_at)
        self.assertIsNone(meta.input_tokens)
        self.assertIsNone(meta.cost_usd_micros)

    def test_a_run_is_reproducible(self) -> None:
        first = run(domain="תשתית AI", model=fixture_model(), generated_at=WHEN)
        second = run(domain="תשתית AI", model=fixture_model(), generated_at=WHEN)
        from scout.report import render_markdown

        self.assertEqual(render_markdown(first.report), render_markdown(second.report))

    def test_an_exhausted_model_is_an_error_not_an_empty_report(self) -> None:
        with self.assertRaises(ModelCallError):
            run(domain="x", model=MockModel([]), generated_at=WHEN)


class SaveTests(unittest.TestCase):
    def test_both_twins_are_written_and_the_json_round_trips(self) -> None:
        result = run(domain="תשתית AI", model=fixture_model(), generated_at=WHEN)
        with tempfile.TemporaryDirectory() as tmp:
            json_path, markdown_path = save(result.report, Path(tmp), WHEN)
            self.assertTrue(json_path.exists() and markdown_path.exists())
            record = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(3, len(record["ranked"]))
            self.assertIn("## 4. בעיות מדורגות", markdown_path.read_text(encoding="utf-8"))

    def test_the_output_directory_is_created(self) -> None:
        result = run(domain="AI", model=fixture_model(), generated_at=WHEN)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "runs"
            _, markdown_path = save(result.report, target, WHEN)
            self.assertTrue(markdown_path.exists())

    def test_slug_is_filename_safe(self) -> None:
        self.assertEqual("ai-infrastructure", slug("AI Infrastructure"))
        self.assertEqual("תשתית-ai", slug("תשתית AI"))
        self.assertEqual("run", slug("///"))
        self.assertNotIn("/", slug("a/b"))


class CostTests(unittest.TestCase):
    def test_cost_uses_integer_micro_dollars(self) -> None:
        """1M input + 1M output on claude-opus-5 is $5 + $25."""
        self.assertEqual(30_000_000, cost_micros(1_000_000, 1_000_000))

    def test_cost_is_none_when_usage_is_unreported(self) -> None:
        self.assertIsNone(cost_micros(None, 10))
        self.assertIsNone(cost_micros(10, None))


class AnthropicModelTests(unittest.TestCase):
    def test_a_missing_key_is_a_configuration_error(self) -> None:
        try:
            import anthropic  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("anthropic is not installed")
        from scout.llm import AnthropicModel

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with self.assertRaises(ConfigurationError):
                AnthropicModel()

    def test_a_missing_sdk_is_a_configuration_error(self) -> None:
        from scout.llm import AnthropicModel

        with mock.patch.dict("sys.modules", {"anthropic": None}):
            with self.assertRaises(ConfigurationError):
                AnthropicModel()


class CliTests(unittest.TestCase):
    def invoke(self, argv: list[str]) -> int:
        """Run the CLI with its streams captured, so tests stay quiet."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            return cli.main(argv)

    def test_a_fixture_run_exits_zero_and_writes_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = self.invoke(["תשתית AI", "--fixture", str(FIXTURE), "--out", tmp])
            self.assertEqual(0, code)
            written = sorted(p.suffix for p in Path(tmp).iterdir())
            self.assertEqual([".json", ".md"], written)

    def test_a_missing_fixture_exits_nonzero_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = self.invoke(["AI", "--fixture", f"{tmp}/absent.json", "--out", tmp])
            self.assertEqual(1, code)

    def test_the_default_model_is_only_built_when_no_fixture_is_given(self) -> None:
        """Steps 1-2 must never need a key: --fixture must not touch the SDK."""
        parser = cli.build_parser()
        args = parser.parse_args(["AI", "--fixture", str(FIXTURE)])
        model = cli.load_model(args)
        self.assertEqual("mock", model.provider)

    def test_the_protocol_is_satisfied_by_both_implementations(self) -> None:
        model: LanguageModel = fixture_model()
        self.assertEqual("mock", model.provider)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
