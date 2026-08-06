"""Orchestration: analyse → generate → validate → score → render → save.

This module holds no rules of its own. It calls the model twice, hands both
replies to the validator, hands what survives to the report builder, and writes
the result to disk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from scout.llm import LanguageModel, cost_micros
from scout.models import Problem, RejectedProblem, parse_analysis, parse_problems
from scout.prompts import (
    ANALYSIS_SCHEMA,
    ANALYSIS_SYSTEM,
    PROBLEMS_SCHEMA,
    PROBLEMS_SYSTEM,
    analysis_prompt,
    problems_prompt,
)
from scout.report import Report, RunMeta, build, render_markdown, to_run_record

DEFAULT_PROBLEM_COUNT = 8
DEFAULT_MAX_TOKENS = 32_000


@dataclass(frozen=True, slots=True)
class RunResult:
    report: Report
    accepted: tuple[Problem, ...]
    rejected: tuple[RejectedProblem, ...]
    json_path: Path | None
    markdown_path: Path | None


def slug(domain: str) -> str:
    """A filename-safe stem. Keeps letters of any script, drops the rest."""
    cleaned = re.sub(r"[^\w\s-]", "", domain, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned.lower() or "run"


def run(
    *,
    domain: str,
    model: LanguageModel,
    generated_at: str,
    count: int = DEFAULT_PROBLEM_COUNT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    out_dir: Path | None = None,
) -> RunResult:
    """Analyse one domain and produce a report.

    ``generated_at`` is passed in rather than read from the clock here, so a
    caller can reproduce a run byte for byte.
    """
    analysis_reply = model.complete_json(
        system=ANALYSIS_SYSTEM,
        prompt=analysis_prompt(domain),
        schema=ANALYSIS_SCHEMA,
        max_tokens=max_tokens,
    )
    analysis = parse_analysis(analysis_reply.payload)

    problems_reply = model.complete_json(
        system=PROBLEMS_SYSTEM,
        prompt=problems_prompt(
            json.dumps(analysis_reply.payload, ensure_ascii=False, indent=2), count
        ),
        schema=PROBLEMS_SCHEMA,
        max_tokens=max_tokens,
    )
    accepted, rejected = parse_problems(problems_reply.payload)

    input_tokens = _total(analysis_reply.input_tokens, problems_reply.input_tokens)
    output_tokens = _total(analysis_reply.output_tokens, problems_reply.output_tokens)
    meta = RunMeta(
        provider=model.provider,
        model=model.model,
        generated_at=generated_at,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd_micros=cost_micros(input_tokens, output_tokens),
    )

    report = build(analysis, accepted, rejected, meta)

    json_path: Path | None = None
    markdown_path: Path | None = None
    if out_dir is not None:
        json_path, markdown_path = save(report, out_dir, generated_at)

    return RunResult(
        report=report,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        json_path=json_path,
        markdown_path=markdown_path,
    )


def save(report: Report, out_dir: Path, generated_at: str) -> tuple[Path, Path]:
    """Write the Markdown and JSON twins. Returns both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", generated_at)[:14]
    stem = f"{slug(report.domain)}-{stamp}"

    markdown_path = out_dir / f"{stem}.md"
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    json_path = out_dir / f"{stem}.json"
    json_path.write_text(
        json.dumps(to_run_record(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return json_path, markdown_path


def _total(first: int | None, second: int | None) -> int | None:
    """Sum two usage counts, or ``None`` if either is unreported.

    A partial total would understate cost while looking authoritative.
    """
    if first is None or second is None:
        return None
    return first + second
