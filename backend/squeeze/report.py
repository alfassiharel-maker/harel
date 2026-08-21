"""Rendering a `Plan`: one text form for review, one dict form for machines.

Both forms are part of the contract. The text form is what goes into a pull
request, so it is column-aligned and states unknowns as `unknown` rather than
omitting the row — an absent line reads as "nothing to see", which is exactly
the wrong impression for a class whose footprint nobody has measured. The dict
form is JSON-serialisable with `json.dumps` and stable in key order, so two
plans can be diffed.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .diagnostics import Diagnostic
from .plan import Plan
from .units import BYTES_PER_GIB
from .verify import Drift

__all__ = ["format_bytes", "plan_to_dict", "render_drifts", "render_plan"]

_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1024**4),
    ("GiB", 1024**3),
    ("MiB", 1024**2),
    ("KiB", 1024),
)


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    for name, scale in _UNITS:
        if abs(value) >= scale:
            return f"{value / scale:.4g} {name}"
    return f"{value} B"


def _ratio(value: Fraction | None) -> str:
    return "—" if value is None else f"{float(value):.3g}x"


def _percent(value: Fraction | None) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _days(value: Fraction | None) -> str:
    return "unknown" if value is None else f"{float(value):.4g} d"


def render_plan(plan: Plan, *, source_name: str = "<policy>") -> str:
    lines: list[str] = []
    lines.append(f"policy {plan.policy}")
    lines.append("")

    header = f"  {'class':<22}{'tier':<8}{'codec':<15}{'residency':>11}{'raw':>12}{'stored':>12}{'ratio':>8}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for allocation in plan.allocations:
        lines.append(
            f"  {allocation.data_class:<22}{allocation.tier:<8}"
            f"{(allocation.codec or 'none'):<15}"
            f"{_days(allocation.residency_days):>11}"
            f"{format_bytes(allocation.raw_bytes):>12}"
            f"{format_bytes(allocation.stored_bytes):>12}"
            f"{_ratio(allocation.ratio):>8}"
        )
        if allocation.unknown_reason is not None:
            lines.append(f"      unknown: {allocation.unknown_reason}")
        for rejected in allocation.rejected:
            # A codec that simply does not handle this data kind was never a
            # candidate, and printing every one of those buries the rejections
            # that a reviewer needs to see — a fidelity floor or a latency
            # budget that ruled out the codec the author expected. Those are
            # always shown; kind mismatches only when nothing was chosen.
            if allocation.codec is not None and rejected.reason.startswith("does not apply"):
                continue
            lines.append(f"      rejected {rejected.codec}: {rejected.reason}")

    lines.append("")
    lines.append(
        f"  totals      raw {format_bytes(plan.total_raw_bytes)} -> "
        f"stored {format_bytes(plan.total_stored_bytes)} "
        f"({_ratio(plan.ratio)}), saved {format_bytes(plan.saved_bytes)}"
    )
    lines.append(f"  per athlete {format_bytes(plan.per_user_bytes)}")
    if plan.monthly_cost_minor is None:
        lines.append("  monthly     unknown (a tier is unpriced)")
    else:
        lines.append(
            f"  monthly     {float(plan.monthly_cost_minor):.0f} minor {plan.currency} at the stored size"
        )
    lines.append(
        f"  coverage    {_percent(plan.coverage)} of allocations sized"
        + ("" if plan.coverage == 1 else "  <- the totals above are partial")
    )

    if plan.limits:
        lines.append("")
        lines.append("  limits")
        for check in plan.limits:
            verdict = {True: "ok", False: "EXCEEDED", None: "unprovable"}[check.satisfied]
            lines.append(
                f"    {check.name:<12}{format_bytes(check.limit_bytes):>12} "
                f"actual {format_bytes(check.actual_bytes):>12}   {verdict}"
            )

    if plan.drivers:
        lines.append("")
        lines.append("  drivers (bytes saved; these sum to the total saving)")
        for index, driver in enumerate(plan.drivers, start=1):
            lines.append(
                f"    {index:>2}. {driver.name:<28}{format_bytes(driver.saved_bytes):>12}"
                f"{_percent(driver.share):>8}   {driver.detail}"
            )

    if plan.diagnostics:
        lines.append("")
        lines.append("  notes")
        for diagnostic in plan.diagnostics:
            lines.append("    " + diagnostic.render(source_name).replace("\n", "\n    "))

    return "\n".join(lines) + "\n"


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """JSON-ready. Byte counts stay integers; ratios become floats only here,
    at the boundary, so no arithmetic in this package is ever done on a float."""
    return {
        "policy": plan.policy,
        "currency": plan.currency,
        "coverage": float(plan.coverage),
        "total_raw_bytes": plan.total_raw_bytes,
        "total_stored_bytes": plan.total_stored_bytes,
        "saved_bytes": plan.saved_bytes,
        "ratio": None if plan.ratio is None else float(plan.ratio),
        "per_user_bytes": plan.per_user_bytes,
        "monthly_cost_minor": (None if plan.monthly_cost_minor is None else float(plan.monthly_cost_minor)),
        "monthly_cost_per_gib_basis": BYTES_PER_GIB,
        "is_provable": plan.is_provable,
        "allocations": [
            {
                "class": a.data_class,
                "kind": a.kind,
                "tier": a.tier,
                "medium": a.medium,
                "codec": a.codec,
                "residency_days": None if a.residency_days is None else float(a.residency_days),
                "raw_bytes": a.raw_bytes,
                "stored_bytes": a.stored_bytes,
                "ratio": None if a.ratio is None else float(a.ratio),
                "fidelity": None if a.fidelity is None else float(a.fidelity),
                "monthly_cost_minor": (None if a.monthly_cost_minor is None else float(a.monthly_cost_minor)),
                "unknown_reason": a.unknown_reason,
                "rejected": [{"codec": r.codec, "reason": r.reason} for r in a.rejected],
            }
            for a in plan.allocations
        ],
        "limits": [
            {
                "name": c.name,
                "limit_bytes": c.limit_bytes,
                "actual_bytes": c.actual_bytes,
                "satisfied": c.satisfied,
            }
            for c in plan.limits
        ],
        "drivers": [
            {
                "name": d.name,
                "saved_bytes": d.saved_bytes,
                "share": None if d.share is None else float(d.share),
                "detail": d.detail,
            }
            for d in plan.drivers
        ],
        "diagnostics": [_diagnostic_to_dict(d) for d in plan.diagnostics],
    }


def _diagnostic_to_dict(diagnostic: Diagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "severity": diagnostic.severity.value,
        "message": diagnostic.message,
        "line": diagnostic.line,
        "column": diagnostic.column,
        "hint": diagnostic.hint,
    }


def render_drifts(drifts: list[Drift]) -> str:
    lines = [
        f"  {'codec':<16}{'impl':<15}{'declared':>10}{'measured':>10}{'error':>9}{'fidelity':>10}  verdict"
    ]
    lines.append("  " + "-" * (len(lines[0]) - 2))
    for drift in drifts:
        verdict = {True: "ok", False: "DRIFT", None: "unverified"}[drift.within_tolerance]
        lines.append(
            f"  {drift.codec:<16}{drift.implementation:<15}"
            f"{_ratio(drift.declared_ratio):>10}"
            f"{_ratio(drift.measured.ratio) if drift.measured.output_bytes else '—':>10}"
            f"{_percent(drift.relative_error):>9}"
            f"{('—' if drift.measured.fidelity is None else f'{float(drift.measured.fidelity):.4f}'):>10}"
            f"  {verdict}"
        )
        if drift.note:
            lines.append(f"      {drift.note}")
    return "\n".join(lines) + "\n"
