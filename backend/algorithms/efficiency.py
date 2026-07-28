"""Efficiency analytics — output per unit of physiological cost.

This is the module that answers "was my heart rate efficient for that pace?".
Every metric is reported both as an absolute value and as a percentage delta
against the athlete's own rolling baseline, because the absolute number is
meaningless in isolation and the delta is the sentence the athlete wants:
"you ran 4:30/km at 145 bpm, 8% less efficient than your average".
"""

from __future__ import annotations

from collections.abc import Sequence

from .stats import mean
from .types import ActivitySummary, AthleteProfile, EfficiencyResult, HalfSplit, Sport

__all__ = [
    "running_efficiency_index",
    "cycling_efficiency_factor",
    "swolf",
    "stroke_index",
    "decoupling_pct",
    "estimated_1rm",
    "compare_to_baseline",
    "session_efficiency",
    "GOOD_DECOUPLING_THRESHOLD_PCT",
]

# Below 5% aerobic decoupling over a steady effort is the usual marker of
# adequate aerobic durability for the duration attempted.
GOOD_DECOUPLING_THRESHOLD_PCT = 5.0


def running_efficiency_index(distance_m: float, moving_time_s: float, avg_hr: float) -> float | None:
    """Efficiency index: metres per minute per beat.

    Rises as an athlete gets fitter at the same heart rate. Only comparable
    between sessions of similar intensity and terrain, which is why
    `session_efficiency` compares against an intensity-matched baseline.
    """
    if distance_m <= 0 or moving_time_s <= 0 or avg_hr <= 0:
        return None
    speed_m_per_min = distance_m / (moving_time_s / 60.0)
    return speed_m_per_min / avg_hr


def cycling_efficiency_factor(normalized_power: float, avg_hr: float) -> float | None:
    """Coggan efficiency factor: normalised power per beat."""
    if normalized_power <= 0 or avg_hr <= 0:
        return None
    return normalized_power / avg_hr


def swolf(length_time_s: float, strokes_per_length: float) -> float | None:
    """SWOLF for one pool length: seconds + strokes.

    Lower is better, which is the one inverted metric in this module.
    `session_efficiency` negates the delta so that "improvement" stays
    consistently positive across every metric the UI shows.
    """
    if length_time_s <= 0 or strokes_per_length <= 0:
        return None
    return length_time_s + strokes_per_length


def stroke_index(velocity_m_s: float, distance_per_stroke_m: float) -> float | None:
    """Stroke index: velocity x distance-per-stroke.

    Separates a swimmer who got faster by thrashing (higher rate, shorter
    stroke) from one who genuinely improved propulsion — the two look identical
    on pace alone.
    """
    if velocity_m_s <= 0 or distance_per_stroke_m <= 0:
        return None
    return velocity_m_s * distance_per_stroke_m


def decoupling_pct(first: HalfSplit, second: HalfSplit) -> float | None:
    """Aerobic decoupling between halves of a session, as a percentage.

    Positive means output-per-beat fell in the second half (cardiac drift under
    fatigue). Prefers power where present, otherwise speed.
    """

    def ratio(half: HalfSplit) -> float | None:
        if not half.avg_hr or half.avg_hr <= 0:
            return None
        output = half.avg_power if half.avg_power else half.avg_speed_m_s
        if not output or output <= 0:
            return None
        return output / half.avg_hr

    first_ratio = ratio(first)
    second_ratio = ratio(second)
    if first_ratio is None or second_ratio is None or first_ratio <= 0:
        return None
    return round(100.0 * (first_ratio - second_ratio) / first_ratio, 2)


def estimated_1rm(weight_kg: float, reps: int) -> dict[str, float]:
    """One-rep-max estimates from a submaximal set.

    Both Epley and Brzycki are returned plus their mean: they diverge by several
    percent and disagree in opposite directions at high and low rep counts, so
    presenting one alone overstates precision. Beyond 12 reps neither formula is
    trustworthy and we say so via `reliable`.
    """
    if weight_kg <= 0 or reps < 1:
        raise ValueError("weight_kg must be positive and reps at least 1")
    if reps == 1:
        return {"epley": weight_kg, "brzycki": weight_kg, "mean": weight_kg, "reliable": 1.0}
    epley = weight_kg * (1.0 + reps / 30.0)
    brzycki = weight_kg * 36.0 / (37.0 - reps) if reps < 37 else epley
    return {
        "epley": round(epley, 1),
        "brzycki": round(brzycki, 1),
        "mean": round((epley + brzycki) / 2.0, 1),
        "reliable": 1.0 if reps <= 12 else 0.0,
    }


def compare_to_baseline(
    metric: str,
    value: float,
    baseline_values: Sequence[float],
    *,
    unit: str = "",
    higher_is_better: bool = True,
) -> EfficiencyResult:
    """Wrap a raw metric with its delta against the athlete's own history.

    `delta_pct` is always signed so that positive means better, regardless of
    whether the underlying metric is one where lower wins.
    """
    if not baseline_values:
        return EfficiencyResult(metric=metric, value=round(value, 4), unit=unit)
    baseline = mean(baseline_values)
    if baseline == 0:
        return EfficiencyResult(metric=metric, value=round(value, 4), baseline=0.0, unit=unit)
    raw_delta = 100.0 * (value - baseline) / abs(baseline)
    delta = raw_delta if higher_is_better else -raw_delta
    return EfficiencyResult(
        metric=metric,
        value=round(value, 4),
        baseline=round(baseline, 4),
        delta_pct=round(delta, 2),
        unit=unit,
    )


def session_efficiency(
    activity: ActivitySummary,
    profile: AthleteProfile,
    baseline_values: Sequence[float] = (),
) -> list[EfficiencyResult]:
    """All efficiency metrics computable for one session.

    `baseline_values` are prior values of the *primary* metric for the same
    sport, ideally filtered to sessions of comparable intensity — the caller
    (the analytics service) owns that filtering because it has the query layer.
    """
    results: list[EfficiencyResult] = []

    if activity.sport is Sport.RUN and activity.distance_m and activity.avg_hr:
        index = running_efficiency_index(activity.distance_m, activity.active_time_s, activity.avg_hr)
        if index is not None:
            results.append(
                compare_to_baseline("running_efficiency_index", index, baseline_values, unit="m/min/bpm")
            )

    if activity.sport is Sport.BIKE and activity.avg_hr:
        power = activity.normalized_power or activity.avg_power
        if power:
            factor = cycling_efficiency_factor(power, activity.avg_hr)
            if factor is not None:
                results.append(
                    compare_to_baseline("cycling_efficiency_factor", factor, baseline_values, unit="W/bpm")
                )

    if activity.sport is Sport.SWIM:
        velocity = activity.speed_m_s
        dps = activity.distance_per_stroke_m
        if velocity and dps:
            index = stroke_index(velocity, dps)
            if index is not None:
                results.append(compare_to_baseline("stroke_index", index, baseline_values, unit="m^2/s"))
        if activity.pool_length_m and activity.total_strokes and activity.distance_m:
            lengths = activity.distance_m / activity.pool_length_m
            if lengths >= 1:
                score = swolf(activity.active_time_s / lengths, activity.total_strokes / lengths)
                if score is not None:
                    results.append(
                        compare_to_baseline("swolf", score, (), unit="s+strokes", higher_is_better=False)
                    )

    if activity.first_half and activity.second_half:
        drift = decoupling_pct(activity.first_half, activity.second_half)
        if drift is not None:
            results.append(EfficiencyResult(metric="aerobic_decoupling", value=drift, unit="%"))

    return results
