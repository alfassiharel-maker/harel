"""Command line entry point. Parses arguments, calls the pipeline, prints paths.

No logic lives here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scout.llm import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AnthropicModel,
    ConfigurationError,
    LanguageModel,
    MockModel,
    ModelCallError,
)
from scout.models import ValidationError
from scout.pipeline import DEFAULT_PROBLEM_COUNT, run
from scout.report import Report

DEFAULT_OUT_DIR = Path(__file__).parent / "runs"

EXIT_OK = 0
EXIT_FAILED = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Analyse a technical or business domain and report ranked "
        "problems and opportunities.",
    )
    parser.add_argument("domain", help='the domain to analyse, e.g. "AI infrastructure"')
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_PROBLEM_COUNT,
        help=f"candidate problems to ask for (default {DEFAULT_PROBLEM_COUNT})",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"default {DEFAULT_MODEL}")
    parser.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        choices=("low", "medium", "high", "xhigh", "max"),
        help=f"reasoning depth (default {DEFAULT_EFFORT})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"ceiling per call, covering thinking and output (default {DEFAULT_MAX_TOKENS})",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="replay a recorded payload instead of calling a provider. Runs "
        "offline and needs no API key.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"directory for the run's .md and .json (default {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--print", action="store_true", help="also write the report to stdout"
    )
    return parser


def load_model(args: argparse.Namespace) -> LanguageModel:
    if args.fixture is None:
        return AnthropicModel(model=args.model, effort=args.effort)
    payload = json.loads(args.fixture.read_text(encoding="utf-8"))
    return MockModel(
        [payload["analysis"], {"problems": payload["problems"]}],
        model=f"fixture:{args.fixture.name}",
    )


def summarise(report: Report) -> str:
    lines = [f"{len(report.ranked)} ranked, {len(report.insufficient)} without a score"]
    for position, item in enumerate(report.ranked, start=1):
        lines.append(f"  {position}. {item.score.value:5.1f}  {item.problem.title}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        model = load_model(args)
        result = run(
            domain=args.domain,
            model=model,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            count=args.count,
            max_tokens=args.max_tokens,
            out_dir=args.out,
        )
    except ConfigurationError as exc:
        print(f"scout: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ModelCallError as exc:
        print(f"scout: the model call failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ValidationError as exc:
        # A malformed domain map cannot be salvaged; a malformed candidate would
        # have been rejected individually and never reach here.
        print(f"scout: the domain analysis was unusable: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (OSError, json.JSONDecodeError) as exc:
        print(f"scout: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.print:
        from scout.report import render_markdown

        print(render_markdown(result.report))

    print(summarise(result.report), file=sys.stderr)
    print(result.markdown_path)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
