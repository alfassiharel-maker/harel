"""`python -m backend.squeeze` — check, plan and verify a `.sqz` source.

Exit codes are the interface a CI job uses:

    0  clean
    1  the source has errors, or `--strict` found an unproven plan
    2  usage error (missing file, bad arguments)

`--strict` is what a pipeline should use. It fails on an exceeded limit *and* on
an unprovable one: a plan whose coverage is below 100% has not shown that it
fits, and "we could not tell" must not pass a gate that exists to catch growth.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .checker import compile_source
from .diagnostics import DiagnosticBag
from .model import Program
from .planner import plan_policy
from .report import plan_to_dict, render_drifts, render_plan
from .verify import default_samples, verify_program

__all__ = ["main"]

_OK = 0
_FAILED = 1
_USAGE = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="squeeze",
        description="Compile a footprint policy (.sqz) into a reviewable plan.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser("check", help="parse and validate only")
    check.add_argument("source", type=Path)

    plan = subcommands.add_parser("plan", help="compile to a footprint plan")
    plan.add_argument("source", type=Path)
    plan.add_argument("--policy", help="plan only this policy (default: all)")
    plan.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    plan.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if a limit is exceeded or cannot be proven",
    )

    verify = subcommands.add_parser("verify", help="measure declared codec ratios against samples")
    verify.add_argument("source", type=Path)
    verify.add_argument("--strict", action="store_true", help="exit non-zero on drift or an unverified codec")

    arguments = parser.parse_args(argv)
    source_path: Path = arguments.source
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as error:
        _err(f"cannot read {source_path}: {error}\n")
        return _USAGE

    diagnostics = DiagnosticBag()
    program = compile_source(text, diagnostics)
    for diagnostic in diagnostics.items:
        _err(diagnostic.render(str(source_path)) + "\n")
    if program is None:
        _err(f"{len(diagnostics.errors)} error(s); no plan produced\n")
        return _FAILED

    if arguments.command == "check":
        _out(f"{source_path}: ok — {_summary(program)}\n")
        return _OK

    if arguments.command == "verify":
        return _verify(program, strict=arguments.strict)

    return _plan(
        program,
        source_name=str(source_path),
        only=arguments.policy,
        as_json=arguments.json,
        strict=arguments.strict,
    )


def _plan(program: Program, *, source_name: str, only: str | None, as_json: bool, strict: bool) -> int:
    names = [only] if only else list(program.policies)
    if only and only not in program.policies:
        _err(f"no policy named {only!r}; declared: {', '.join(program.policies) or 'none'}\n")
        return _USAGE
    if not names:
        _err("the source declares no policy, so there is nothing to plan\n")
        return _FAILED

    failures = 0
    payload: list[dict[str, object]] = []
    for name in names:
        plan = plan_policy(program, program.policies[name])
        if as_json:
            payload.append(plan_to_dict(plan))
        else:
            _out(render_plan(plan, source_name=source_name))
            _out("\n")
        # A limit that is exceeded and a limit that cannot be proven both fail
        # the gate: "we could not tell" is not a pass for a budget check.
        if strict and (any(check.satisfied is not True for check in plan.limits) or plan.coverage != 1):
            failures += 1
    if as_json:
        _out(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    if failures:
        _err(f"{failures} polic(y/ies) not proven within their limits\n")
        return _FAILED
    return _OK


def _verify(program: Program, *, strict: bool) -> int:
    drifts = verify_program(program, default_samples())
    if not drifts:
        _out("no codec names an implementation, so there is nothing to verify\n")
        return _FAILED if strict else _OK
    _out(render_drifts(drifts))
    if strict and any(drift.within_tolerance is not True for drift in drifts):
        _err("declared ratios drifted from measurement, or could not be verified\n")
        return _FAILED
    return _OK


def _summary(program: Program) -> str:
    return (
        f"{len(program.codecs)} codec(s), {len(program.tiers)} tier(s), "
        f"{len(program.classes)} class(es), {len(program.policies)} polic(y/ies)"
    )


def _out(text: str) -> None:
    sys.stdout.write(text)


def _err(text: str) -> None:
    sys.stderr.write(text)
