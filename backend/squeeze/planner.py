"""The planner: `Program` -> `Plan`.

Selection is deterministic and explainable, not clever. For each (class, tier)
residency the planner filters the tier's codecs down to those that are *provably*
usable, then picks the best one under the policy objective, with a total order
for ties so the same source always compiles to the same plan.

A codec is filtered out — and the reason is kept and printed — when:

  * it does not apply to the class's data kind;
  * it has no measured ratio (an unmeasured codec cannot be planned with);
  * the class is `sensitivity clinical` and the codec is lossy. Health inputs
    feed numbers an athlete makes decisions on; storing an approximation of a
    measured HRV value is not a trade the planner is allowed to make;
  * a `require fidelity >= x` constraint exceeds the codec's fidelity;
  * the tier states a latency budget and the codec's CPU cost per record either
    exceeds it or is unmeasured. Unmeasured fails closed: an unprovable latency
    claim is treated as violated, not as satisfied.

Volume arithmetic, in one place so it can be checked by hand:

    records_in_window = growth.amount * (residency / growth.period)
    raw_bytes         = record_size * records_in_window * population_factor
    stored_bytes      = ceil(raw_bytes / ratio)

`residency` is the *difference* between consecutive retention windows, because
windows are cumulative: `retain hot 30 days, warm 12 months` means the hot tier
holds days 0-30 and the warm tier holds day 30 to month 12. Residency is also
clipped to the policy horizon — a 5-year cold window cannot hold more than 12
months of data 12 months after launch, and a plan that claimed otherwise would
over-provision the largest, cheapest tier by years.
"""

from __future__ import annotations

from fractions import Fraction

from .diagnostics import Diagnostic, DiagnosticBag, Severity
from .model import Codec, CodecKind, DataClass, Objective, Policy, Program, Sensitivity, Tier
from .plan import Allocation, Driver, LimitCheck, Plan, RejectedCodec
from .units import BYTES_PER_GIB, SECONDS_PER_DAY, Dimension, Quantity

__all__ = ["plan_policy", "plan_program"]

#: Growth scopes that scale with the athlete population. `fleet` does not: a
#: fleet-scoped class (model checkpoints, aggregate indexes) has one copy for
#: the whole system.
_PER_USER_SCOPES = frozenset({"user", "account"})


def plan_program(program: Program, diagnostics: DiagnosticBag | None = None) -> list[Plan]:
    """Compile every policy in the program, in declaration order."""
    return [plan_policy(program, policy, diagnostics) for policy in program.policies.values()]


def plan_policy(program: Program, policy: Policy, diagnostics: DiagnosticBag | None = None) -> Plan:
    bag = DiagnosticBag() if diagnostics is None else diagnostics
    local = DiagnosticBag()

    required_fidelity = _required_fidelity(policy)
    class_names = policy.classes or tuple(program.classes)
    allocations: list[Allocation] = []

    for class_name in class_names:
        data_class = program.classes.get(class_name)
        if data_class is None:
            continue
        previous_window = Fraction(0)
        for retention in data_class.retain:
            tier = program.tiers.get(retention.tier)
            if tier is None:
                continue
            residency = _residency_seconds(
                window=retention.window.seconds(), previous=previous_window, horizon=policy.horizon
            )
            previous_window = retention.window.seconds()
            allocations.append(
                _allocate(
                    program=program,
                    policy=policy,
                    data_class=data_class,
                    tier=tier,
                    residency_seconds=residency,
                    required_fidelity=required_fidelity,
                    diagnostics=local,
                )
            )

    sized = [a for a in allocations if a.unknown_reason is None]
    coverage = Fraction(len(sized), len(allocations)) if allocations else Fraction(0)

    total_raw = sum(a.raw_bytes or 0 for a in sized) if sized else None
    total_stored = sum(a.stored_bytes or 0 for a in sized) if sized else None
    saved = None if total_raw is None or total_stored is None else total_raw - total_stored

    drivers = _drivers(sized, saved)
    if any(r.subject == "explanation" and r.operator is None for r in policy.requirements) and not drivers:
        # `require explanation` is a promise the plan makes to its reader. If the
        # plan cannot name what drove its number, it has not kept it.
        local.warn(
            "SQZ0403",
            f"policy {policy.name!r} requires an explanation, but no allocation could be sized, "
            "so the plan has no drivers to show",
            policy.line,
            1,
        )
    per_user = _per_user_bytes(program, policy, sized, class_names)
    monthly_cost = _monthly_cost(sized)
    limits = _limit_checks(policy, total_stored, per_user, complete=coverage == 1)

    bag.extend(local)
    return Plan(
        policy=policy.name,
        currency=program.currency,
        allocations=tuple(allocations),
        total_raw_bytes=total_raw,
        total_stored_bytes=total_stored,
        saved_bytes=saved,
        drivers=drivers,
        per_user_bytes=per_user,
        monthly_cost_minor=monthly_cost,
        limits=limits,
        coverage=coverage,
        diagnostics=tuple(local.items),
    )


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def _allocate(
    *,
    program: Program,
    policy: Policy,
    data_class: DataClass,
    tier: Tier,
    residency_seconds: Fraction,
    required_fidelity: Fraction | None,
    diagnostics: DiagnosticBag,
) -> Allocation:
    candidates: list[Codec] = []
    rejected: list[RejectedCodec] = []

    for name in tier.codecs:
        codec = program.codecs.get(name)
        if codec is None:
            continue
        reason = _rejection_reason(
            codec=codec,
            data_class=data_class,
            tier=tier,
            required_fidelity=required_fidelity,
        )
        if reason is None:
            candidates.append(codec)
        else:
            rejected.append(RejectedCodec(codec=name, reason=reason))

    chosen = _choose(candidates, policy.objective)
    if chosen is None and rejected:
        diagnostics.warn(
            "SQZ0401",
            f"class {data_class.name!r} on tier {tier.name!r} will be stored uncompressed: "
            + "; ".join(f"{r.codec} ({r.reason})" for r in rejected),
            data_class.line,
            1,
        )

    raw = _raw_bytes(data_class, policy, residency_seconds)
    if raw is None:
        missing = "record size" if data_class.record_size is None else "growth rate"
        diagnostics.unknown(
            "SQZ0402",
            f"footprint of class {data_class.name!r} on tier {tier.name!r} is unknown: no {missing} declared",
            data_class.line,
            1,
            hint="measure it; the plan will not substitute an estimate",
        )
        return Allocation(
            data_class=data_class.name,
            kind=data_class.kind,
            tier=tier.name,
            medium=tier.medium,
            codec=None if chosen is None else chosen.name,
            residency_days=residency_seconds / SECONDS_PER_DAY,
            raw_bytes=None,
            stored_bytes=None,
            ratio=None if chosen is None else chosen.ratio,
            fidelity=None if chosen is None else chosen.fidelity,
            monthly_cost_minor=None,
            rejected=tuple(rejected),
            unknown_reason=f"no {missing} declared",
        )

    ratio = chosen.ratio if chosen is not None else None
    stored = raw if ratio is None else _ceil_div(raw, ratio)
    cost = (
        None
        if tier.unit_cost_per_gib_month is None
        else Fraction(stored, BYTES_PER_GIB) * tier.unit_cost_per_gib_month
    )
    return Allocation(
        data_class=data_class.name,
        kind=data_class.kind,
        tier=tier.name,
        medium=tier.medium,
        codec=None if chosen is None else chosen.name,
        residency_days=residency_seconds / SECONDS_PER_DAY,
        raw_bytes=raw,
        stored_bytes=stored,
        ratio=ratio,
        fidelity=None if chosen is None else chosen.fidelity,
        monthly_cost_minor=cost,
        rejected=tuple(rejected),
        unknown_reason=None,
    )


def _rejection_reason(
    *,
    codec: Codec,
    data_class: DataClass,
    tier: Tier,
    required_fidelity: Fraction | None,
) -> str | None:
    if data_class.kind not in codec.applies_to:
        return f"does not apply to {data_class.kind} data"
    if codec.ratio is None:
        return "no measured ratio"
    if data_class.sensitivity is Sensitivity.CLINICAL and codec.kind is CodecKind.LOSSY:
        return "lossy codec on a clinical class"
    if required_fidelity is not None and codec.fidelity is not None and codec.fidelity < required_fidelity:
        return f"fidelity {float(codec.fidelity):.4g} below the required {float(required_fidelity):.4g}"
    if tier.latency is not None:
        if codec.cpu_per_mib is None:
            # Fail closed: an unmeasured CPU cost cannot be shown to fit the
            # budget, and a plan that assumed it fits would be discovered wrong
            # in production rather than in review.
            return f"CPU cost unmeasured, so the {tier.latency} budget is unprovable"
        if data_class.record_size is None:
            return "record size unmeasured, so the latency budget is unprovable"
        per_record = codec.cpu_per_mib.as_fraction() * Fraction(
            data_class.record_size.bytes_exact(), 1024 * 1024
        )
        if per_record > tier.latency.seconds():
            return (
                f"{_ms(per_record)} ms per record exceeds the tier budget of {_ms(tier.latency.seconds())} ms"
            )
    return None


def _choose(candidates: list[Codec], objective: Objective) -> Codec | None:
    """Pick one codec under a total order, so plans are reproducible.

    Bytes and cost share a key: on a single tier the monthly bill is a fixed
    price per stored byte, so the cheapest plan is the smallest one. Latency
    inverts the primary key and keeps ratio as the tie-break.
    """
    if not candidates:
        return None
    if objective is Objective.LATENCY:
        return min(
            candidates,
            key=lambda c: (
                c.cpu_per_mib.as_fraction() if c.cpu_per_mib is not None else Fraction(10**9),
                -(c.ratio or Fraction(1)),
                c.name,
            ),
        )
    return min(
        candidates,
        key=lambda c: (
            -(c.ratio or Fraction(1)),
            c.cpu_per_mib.as_fraction() if c.cpu_per_mib is not None else Fraction(10**9),
            c.name,
        ),
    )


def _required_fidelity(policy: Policy) -> Fraction | None:
    best: Fraction | None = None
    for requirement in policy.requirements:
        if requirement.subject != "fidelity" or requirement.operator not in (">=", ">", "=="):
            continue
        value = requirement.value
        if isinstance(value, Fraction):
            best = value if best is None else max(best, value)
    return best


def _residency_seconds(*, window: Fraction, previous: Fraction, horizon: Quantity | None) -> Fraction:
    span = window - previous
    if horizon is None:
        return span
    remaining = horizon.seconds() - previous
    if remaining <= 0:
        return Fraction(0)
    return min(span, remaining)


def _raw_bytes(data_class: DataClass, policy: Policy, residency_seconds: Fraction) -> int | None:
    if data_class.record_size is None or data_class.growth is None:
        return None
    growth = data_class.growth
    period = growth.period.seconds()
    if period == 0:
        return None
    records = growth.amount * residency_seconds / period
    total = Fraction(data_class.record_size.bytes_exact()) * records
    if growth.scope in _PER_USER_SCOPES:
        if policy.population is None:
            # Per-user footprint is knowable, the fleet total is not. Reporting
            # the per-user figure as if it were the total would understate the
            # bill by four orders of magnitude, so refuse instead.
            return None
        total *= policy.population
    return _ceil(total)


def _per_user_bytes(
    program: Program, policy: Policy, sized: list[Allocation], class_names: tuple[str, ...]
) -> int | None:
    """Stored bytes for one athlete: the fleet total for user-scoped classes,
    divided by the population. Fleet-scoped classes are excluded — a shared
    model checkpoint is not part of any one athlete's footprint."""
    if policy.population is None or policy.population == 0:
        return None
    per_user_classes = {
        name
        for name in class_names
        if (data_class := program.classes.get(name)) is not None
        and data_class.growth is not None
        and data_class.growth.scope in _PER_USER_SCOPES
    }
    relevant = [a for a in sized if a.data_class in per_user_classes]
    if not relevant:
        return None
    total = sum(a.stored_bytes or 0 for a in relevant)
    return _ceil(Fraction(total) / policy.population)


def _monthly_cost(sized: list[Allocation]) -> Fraction | None:
    # One unpriced tier makes the total unknown. A partial bill presented as a
    # total is the kind of number that gets quoted in a board deck.
    if not sized or any(a.monthly_cost_minor is None for a in sized):
        return None
    return sum((a.monthly_cost_minor or Fraction(0) for a in sized), Fraction(0))


def _limit_checks(
    policy: Policy, total_stored: int | None, per_user: int | None, *, complete: bool
) -> tuple[LimitCheck, ...]:
    checks: list[LimitCheck] = []
    for name, limit in sorted(policy.limits.items()):
        if limit.dimension is not Dimension.BYTES:
            continue
        actual = per_user if name == "per_user" else total_stored
        satisfied = None if actual is None or not complete else actual <= limit.bytes_exact()
        checks.append(
            LimitCheck(
                name=name,
                limit_bytes=limit.bytes_exact(),
                actual_bytes=actual,
                satisfied=satisfied,
            )
        )
    return tuple(checks)


def _drivers(sized: list[Allocation], saved: int | None) -> tuple[Driver, ...]:
    """Rank allocations by bytes saved.

    The shares are computed so they sum to exactly the total: the last driver
    absorbs the rounding remainder rather than every share being rounded
    independently, which would leave the column not adding up to 100%.
    """
    if saved is None:
        return ()
    contributions = [(a, a.saved_bytes or 0) for a in sized if a.saved_bytes is not None]
    contributions.sort(key=lambda item: (-item[1], item[0].data_class, item[0].tier))
    drivers: list[Driver] = []
    for allocation, amount in contributions:
        share = Fraction(amount, saved) if saved > 0 else None
        codec = allocation.codec or "uncompressed"
        drivers.append(
            Driver(
                name=f"{allocation.data_class}@{allocation.tier}",
                saved_bytes=amount,
                share=share,
                detail=f"{codec} on {allocation.medium}",
            )
        )
    return tuple(drivers)


def fatal_diagnostics(plan: Plan) -> list[Diagnostic]:
    """Diagnostics that should fail a CI gate: a violated limit is an error."""
    fatal = [d for d in plan.diagnostics if d.severity is Severity.ERROR]
    return fatal


def _ceil(value: Fraction) -> int:
    whole = value.numerator // value.denominator
    return whole if value.denominator == 1 else whole + 1


def _ceil_div(amount: int, ratio: Fraction) -> int:
    return _ceil(Fraction(amount) / ratio)


def _ms(seconds: Fraction) -> str:
    return f"{float(seconds * 1000):.3g}"
