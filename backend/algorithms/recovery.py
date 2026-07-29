"""Recovery / readiness scoring.

Produces a 0-100 score **with its drivers**, not a bare number. The drivers are
what the AI layer consumes: "your readiness is 48, driven mainly by HRV 14%
below your baseline and 5.5h of sleep" is actionable, where "48" is not.

Design rules:
  * Every comparison is against the athlete's own rolling baseline, never a
    population norm. HRV in particular varies several-fold between individuals.
  * A day-to-day move smaller than the athlete's smallest worthwhile change is
    reported as neutral, not as a signal.
  * Missing inputs renormalise the weights and lower `data_quality`. We never
    substitute a zero for an unknown, which would read as "terrible" instead of
    "unmeasured".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from .stats import clamp, mean, smallest_worthwhile_change, z_score
from .training_load import acwr, fitness_fatigue, monotony_strain
from .types import (
    DEFAULT_SLEEP_NEED_MIN,
    AthleteProfile,
    DailyWellness,
    Driver,
    ReadinessBand,
    ReadinessResult,
)

__all__ = ["BASELINE_WINDOW_DAYS", "COMPONENT_WEIGHTS", "readiness"]

BASELINE_WINDOW_DAYS = 28
MIN_BASELINE_POINTS = 7

COMPONENT_WEIGHTS: dict[str, float] = {
    "hrv": 0.30,
    "resting_hr": 0.15,
    "sleep": 0.25,
    "training_load": 0.20,
    "subjective": 0.10,
}

# Target share of total sleep spent in deep + REM. Below this, total hours can
# look fine while the sleep did little for recovery.
DEEP_REM_TARGET_FRACTION = 0.45


def _score_from_z(z: float, *, higher_is_better: bool) -> float:
    """Map a standard score onto 0-100, saturating at +/- 2 SD.

    Two SD is where we stop distinguishing degrees of bad: an athlete 3 SD below
    their HRV baseline and one 5 SD below both need the same advice (do not
    train hard), so widening the scale further adds noise, not information.
    """
    signed = z if higher_is_better else -z
    return 50.0 + 50.0 * clamp(signed / 2.0, -1.0, 1.0)


def _window(
    history: Sequence[DailyWellness], ref_day: date
) -> tuple[DailyWellness | None, list[DailyWellness]]:
    """Split history into (today's reading, baseline window before today)."""
    today: DailyWellness | None = None
    baseline: list[DailyWellness] = []
    earliest = ref_day - timedelta(days=BASELINE_WINDOW_DAYS)
    for entry in history:
        if entry.day == ref_day:
            today = entry
        elif earliest <= entry.day < ref_day:
            baseline.append(entry)
    baseline.sort(key=lambda e: e.day)
    return today, baseline


def _hrv_component(
    today: DailyWellness, baseline: Sequence[DailyWellness]
) -> tuple[float, float, float] | None:
    """Returns (score, today_rmssd, baseline_mean_rmssd)."""
    if today.ln_hrv is None or today.hrv_rmssd_ms is None:
        # ln_hrv is derived from hrv_rmssd_ms, so the second clause is always
        # implied by the first — stated explicitly so the type checker (and a
        # reader) can see hrv_rmssd_ms is non-None for the rest of the function.
        return None
    raw = [e.hrv_rmssd_ms for e in baseline if e.hrv_rmssd_ms]
    logs = [e.ln_hrv for e in baseline if e.ln_hrv is not None]
    if len(logs) < MIN_BASELINE_POINTS:
        return None

    baseline_mean_raw = mean(raw)
    deviation_pct = 100.0 * (today.hrv_rmssd_ms - baseline_mean_raw) / baseline_mean_raw
    swc_pct = smallest_worthwhile_change(raw)
    if abs(deviation_pct) < swc_pct:
        # Inside the athlete's own noise band — deliberately neutral.
        return 50.0, today.hrv_rmssd_ms, baseline_mean_raw

    z = z_score(today.ln_hrv, logs)
    if z is None:
        return None
    return _score_from_z(z, higher_is_better=True), today.hrv_rmssd_ms, baseline_mean_raw


def _resting_hr_component(
    today: DailyWellness, baseline: Sequence[DailyWellness]
) -> tuple[float, float, float] | None:
    if today.resting_hr is None:
        return None
    values = [e.resting_hr for e in baseline if e.resting_hr]
    if len(values) < MIN_BASELINE_POINTS:
        return None
    z = z_score(today.resting_hr, values)
    if z is None:
        return None
    return _score_from_z(z, higher_is_better=False), today.resting_hr, mean(values)


def _sleep_component(today: DailyWellness) -> tuple[float, float] | None:
    """Duration, efficiency and architecture, averaged over whatever is present."""
    parts: list[float] = []
    if today.sleep_total_min:
        parts.append(clamp(100.0 * today.sleep_total_min / DEFAULT_SLEEP_NEED_MIN, 0.0, 100.0))
    if today.sleep_efficiency_pct is not None:
        parts.append(clamp(today.sleep_efficiency_pct, 0.0, 100.0))
    if today.sleep_total_min and (today.sleep_deep_min is not None or today.sleep_rem_min is not None):
        restorative = (today.sleep_deep_min or 0.0) + (today.sleep_rem_min or 0.0)
        fraction = restorative / today.sleep_total_min
        parts.append(clamp(100.0 * fraction / DEEP_REM_TARGET_FRACTION, 0.0, 100.0))
    if not parts:
        return None
    return mean(parts), (today.sleep_total_min or 0.0)


def _load_component(
    loads: Mapping[date, float],
    ref_day: date,
) -> tuple[float, float] | None:
    """Freshness from form, ramp safety from ACWR, and a monotony penalty."""
    if not loads:
        return None
    start = min(loads)
    if start > ref_day:
        return None
    points = fitness_fatigue(loads, start, ref_day)
    if not points:
        return None
    tsb = points[-1].tsb

    parts: list[float] = []
    # TSB +5 or better is fully fresh; -30 is deeply buried.
    parts.append(clamp(100.0 * (tsb + 30.0) / 35.0, 0.0, 100.0))

    ratio_result = acwr(loads, ref_day)
    if ratio_result.is_reliable and ratio_result.ratio is not None:
        ratio = ratio_result.ratio
        if ratio <= 1.30:
            # Undertraining is not a readiness problem; it is a progress problem.
            parts.append(100.0)
        else:
            parts.append(clamp(100.0 - (ratio - 1.30) * 200.0, 0.0, 100.0))

    monotony, _ = monotony_strain(loads, ref_day)
    if monotony is not None:
        # Monotony above 2.0 is the classic overreaching pattern.
        parts.append(clamp(100.0 - max(0.0, monotony - 1.5) * 60.0, 0.0, 100.0))

    return mean(parts), round(tsb, 1)


def _subjective_component(today: DailyWellness) -> tuple[float, float] | None:
    hooper = today.hooper_index
    if hooper is None:
        return None
    # Hooper is 1 (best) to 5 (worst).
    return clamp(100.0 * (5.0 - hooper) / 4.0, 0.0, 100.0), hooper


def _band(score: float) -> ReadinessBand:
    if score < 25:
        return ReadinessBand.COMPROMISED
    if score < 50:
        return ReadinessBand.LIMITED
    if score < 70:
        return ReadinessBand.MODERATE
    if score < 85:
        return ReadinessBand.GOOD
    return ReadinessBand.PRIME


def readiness(
    profile: AthleteProfile,
    wellness_history: Sequence[DailyWellness],
    loads: Mapping[date, float],
    ref_day: date,
) -> ReadinessResult:
    """Composite readiness for `ref_day`.

    With no usable inputs at all the result is a neutral 50 with
    `data_quality == 0.0`. Callers must check `data_quality` before presenting
    the score as advice — the API refuses to render a recommendation below 0.35.
    """
    today, baseline = _window(wellness_history, ref_day)

    components: dict[str, tuple[float, float | None, float | None]] = {}

    if today is not None:
        hrv = _hrv_component(today, baseline)
        if hrv:
            score, value, base = hrv
            components["hrv"] = (score, value, round(base, 1))

        rhr = _resting_hr_component(today, baseline)
        if rhr:
            score, value, base = rhr
            components["resting_hr"] = (score, value, round(base, 1))

        sleep = _sleep_component(today)
        if sleep:
            score, minutes = sleep
            components["sleep"] = (score, round(minutes / 60.0, 2), round(DEFAULT_SLEEP_NEED_MIN / 60.0, 2))

        subjective = _subjective_component(today)
        if subjective:
            score, hooper = subjective
            components["subjective"] = (score, round(hooper, 2), 1.0)

    load_component = _load_component(loads, ref_day)
    if load_component:
        score, tsb = load_component
        components["training_load"] = (score, tsb, 0.0)

    if not components:
        return ReadinessResult(score=50.0, band=ReadinessBand.MODERATE, drivers=(), data_quality=0.0)

    available_weight = sum(COMPONENT_WEIGHTS[name] for name in components)
    drivers: list[Driver] = []
    total = 0.0
    # Distinct names from the `score, value, base` unpacks above: those are all
    # floats, whereas a component's value/baseline here are float | None, and
    # reusing the names would ask the type checker to give one variable two types.
    for name, (comp_score, comp_value, comp_base) in components.items():
        normalised_weight = COMPONENT_WEIGHTS[name] / available_weight
        total += normalised_weight * comp_score
        drivers.append(
            Driver(
                name=name,
                score=round(comp_score, 1),
                weight=round(normalised_weight, 3),
                # Signed points relative to neutral; these sum exactly to score - 50.
                contribution=round(normalised_weight * (comp_score - 50.0), 2),
                value=comp_value,
                baseline=comp_base,
            )
        )

    drivers.sort(key=lambda d: abs(d.contribution), reverse=True)
    return ReadinessResult(
        score=round(total, 1),
        band=_band(total),
        drivers=tuple(drivers),
        data_quality=round(available_weight, 3),
    )
