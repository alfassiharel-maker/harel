"""Performance prediction.

Three independent families, so a prediction can be cross-checked rather than
taken on faith:
  * **Riegel** — empirical distance scaling, with the exponent fitted to the
    athlete's own PBs where we have three or more.
  * **Daniels VDOT** — physiological, maps any race result to equivalent times
    at other distances.
  * **Critical speed / critical power** — two-parameter hyperbolic model, the
    basis for FTP and for pacing.

Plus a trend forecast that answers "what is the chance I break my PB next
month?" as an actual probability with an interval, rather than as encouragement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

from .stats import LinearFit, linear_regression, probability_below
from .types import PredictionResult

__all__ = [
    "DEFAULT_RIEGEL_EXPONENT",
    "STANDARD_DISTANCES_M",
    "critical_power",
    "critical_speed",
    "equivalent_times",
    "fit_riegel_exponent",
    "ftp_from_20min_test",
    "predict_time_from_vdot",
    "probability_of_beating",
    "progression_forecast",
    "riegel_predict",
    "triathlon_prediction",
    "vdot_from_race",
]

DEFAULT_RIEGEL_EXPONENT = 1.06

STANDARD_DISTANCES_M: dict[str, float] = {
    "1500m": 1500.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "half_marathon": 21097.5,
    "marathon": 42195.0,
}


def riegel_predict(
    known_time_s: float,
    known_distance_m: float,
    target_distance_m: float,
    exponent: float = DEFAULT_RIEGEL_EXPONENT,
) -> float:
    """Riegel: T2 = T1 x (D2 / D1) ^ exponent."""
    if known_time_s <= 0 or known_distance_m <= 0 or target_distance_m <= 0:
        raise ValueError("times and distances must be positive")
    # float() coerces the `** exponent` result, which the type checker widens to
    # Any, back to the declared float. No numerical effect.
    return float(known_time_s * (target_distance_m / known_distance_m) ** exponent)


def fit_riegel_exponent(personal_bests: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Fit the athlete's own fatigue exponent from (distance_m, time_s) PBs.

    log(T) = log(k) + exponent x log(D), so the exponent is the slope of a
    log-log fit. Returns (exponent, r_squared). Falls back to the population
    default with r^2 = 0 when there are fewer than three PBs, and clamps to
    1.00-1.20 — a fit outside that range means the inputs are inconsistent
    (a stale marathon time against a fresh 5k, say), not that the athlete has
    exotic physiology.
    """
    usable = [(d, t) for d, t in personal_bests if d > 0 and t > 0]
    if len(usable) < 3:
        return DEFAULT_RIEGEL_EXPONENT, 0.0
    xs = [math.log(d) for d, _ in usable]
    ys = [math.log(t) for _, t in usable]
    fit = linear_regression(xs, ys)
    exponent = min(1.20, max(1.00, fit.slope))
    return round(exponent, 4), round(fit.r_squared, 4)


def _velocity_to_vo2(v_m_per_min: float) -> float:
    """Daniels/Gilbert oxygen cost of running at a given velocity (ml/kg/min)."""
    return -4.60 + 0.182258 * v_m_per_min + 0.000104 * v_m_per_min**2


def _fraction_of_vo2max(duration_min: float) -> float:
    """Fraction of VO2max sustainable for a given race duration."""
    return (
        0.8 + 0.1894393 * math.exp(-0.012778 * duration_min) + 0.2989558 * math.exp(-0.1932605 * duration_min)
    )


def vdot_from_race(distance_m: float, time_s: float) -> float:
    """VDOT (Daniels' effective VO2max) from a race result."""
    if distance_m <= 0 or time_s <= 0:
        raise ValueError("distance and time must be positive")
    duration_min = time_s / 60.0
    velocity = distance_m / duration_min
    return _velocity_to_vo2(velocity) / _fraction_of_vo2max(duration_min)


def predict_time_from_vdot(vdot: float, distance_m: float) -> float:
    """Invert the VDOT model for a target distance, by bisection.

    Required VDOT falls monotonically as the finishing time rises, so bisection
    is guaranteed to converge; there is no closed form because the
    %VO2max term is a sum of exponentials in time.
    """
    if vdot <= 0 or distance_m <= 0:
        raise ValueError("vdot and distance must be positive")

    def required_vdot(time_s: float) -> float:
        return vdot_from_race(distance_m, time_s)

    low, high = 30.0, 12 * 3600.0
    # Widen if the target sits outside the bracket rather than returning a bound.
    if required_vdot(high) > vdot:
        return high
    if required_vdot(low) < vdot:
        return low
    for _ in range(200):
        mid = (low + high) / 2.0
        if required_vdot(mid) > vdot:
            low = mid
        else:
            high = mid
        if high - low < 0.01:
            break
    return round((low + high) / 2.0, 2)


def equivalent_times(
    distance_m: float,
    time_s: float,
    targets: Sequence[float] | None = None,
    personal_bests: Sequence[tuple[float, float]] = (),
) -> list[PredictionResult]:
    """Equivalent race times at other distances, via both VDOT and Riegel.

    Returning both is the point: where they agree the prediction is solid, and
    where they diverge by more than a few percent the athlete has a
    distance-specific strength or weakness worth naming.
    """
    if targets is None:
        targets = tuple(STANDARD_DISTANCES_M.values())
    exponent, exponent_fit = fit_riegel_exponent(personal_bests)
    vdot = vdot_from_race(distance_m, time_s)

    results: list[PredictionResult] = []
    for target in targets:
        vdot_time = predict_time_from_vdot(vdot, target)
        riegel_time = riegel_predict(time_s, distance_m, target, exponent)
        blended = (vdot_time + riegel_time) / 2.0
        spread = abs(vdot_time - riegel_time)
        results.append(
            PredictionResult(
                metric=f"time_{int(target)}m",
                value=round(blended, 1),
                unit="s",
                low=round(min(vdot_time, riegel_time), 1),
                high=round(max(vdot_time, riegel_time), 1),
                method=f"vdot={vdot:.1f}; riegel_exp={exponent:.3f} (fit r2={exponent_fit:.2f})",
                # Models agreeing within 2% is high confidence; 10% apart is low.
                confidence=round(max(0.2, 1.0 - (spread / blended) * 5.0), 2) if blended else 0.0,
            )
        )
    return results


def critical_speed(d1_m: float, t1_s: float, d2_m: float, t2_s: float) -> tuple[float, float]:
    """Two-parameter CS model from two maximal efforts.

    Returns (critical_speed_m_s, D_prime_m). Use efforts roughly 3-20 minutes
    long: shorter and the anaerobic contribution dominates, longer and the model
    systematically overestimates CS.
    """
    if t1_s == t2_s:
        raise ValueError("the two efforts must have different durations")
    cs = (d2_m - d1_m) / (t2_s - t1_s)
    d_prime = d1_m - cs * t1_s
    return round(cs, 4), round(d_prime, 1)


def critical_power(p1_w: float, t1_s: float, p2_w: float, t2_s: float) -> tuple[float, float]:
    """Two-parameter CP model: P = CP + W' / t. Returns (CP_watts, W_prime_J)."""
    if t1_s <= 0 or t2_s <= 0 or t1_s == t2_s:
        raise ValueError("durations must be positive and different")
    w_prime = (p1_w - p2_w) / (1.0 / t1_s - 1.0 / t2_s)
    cp = p1_w - w_prime / t1_s
    return round(cp, 1), round(w_prime, 0)


def ftp_from_20min_test(mean_power_20min_w: float) -> float:
    """The conventional FTP estimate: 95% of a 20-minute maximal effort."""
    if mean_power_20min_w <= 0:
        raise ValueError("power must be positive")
    return round(0.95 * mean_power_20min_w, 1)


def progression_forecast(
    history: Sequence[tuple[date, float]],
    horizon_days: int,
    *,
    lower_is_better: bool = True,
    recency_half_life_days: float = 60.0,
    metric: str = "performance",
    unit: str = "s",
) -> PredictionResult | None:
    """Extrapolate an athlete's own trend, with a 95% prediction interval.

    Observations are weighted by recency (exponential half-life), so a block of
    recent training counts for more than last winter's form. Returns None with
    fewer than three observations — two points define a line with no residual
    scatter, and an interval of zero width would be a lie.
    """
    usable = sorted((day, value) for day, value in history if value > 0)
    if len(usable) < 3:
        return None

    origin = usable[0][0]
    latest = usable[-1][0]
    xs = [float((day - origin).days) for day, _ in usable]
    ys = [value for _, value in usable]
    weights = [0.5 ** (((latest - day).days) / recency_half_life_days) for day, _ in usable]

    fit: LinearFit = linear_regression(xs, ys, weights)
    target_x = float((latest - origin).days + horizon_days)
    predicted = fit.predict(target_x)
    sd = fit.prediction_sd(target_x)
    half_width = 1.96 * sd

    # A forecast is only as trustworthy as the fit and the sample behind it.
    confidence = round(min(1.0, fit.r_squared * min(1.0, len(usable) / 6.0)), 2)
    return PredictionResult(
        metric=metric,
        value=round(predicted, 2),
        unit=unit,
        low=round(predicted - half_width, 2),
        high=round(predicted + half_width, 2),
        method=(
            f"weighted-ols n={len(usable)} r2={fit.r_squared:.2f} "
            f"slope={fit.slope:.4f}/day {'improving' if (fit.slope < 0) == lower_is_better else 'declining'}"
        ),
        confidence=confidence,
    )


def probability_of_beating(
    target_value: float, forecast: PredictionResult, *, lower_is_better: bool = True
) -> float:
    """Probability the athlete beats `target_value` at the forecast horizon.

    Derived from the forecast's own interval, so it degrades gracefully: a wide
    interval yields a probability near 0.5 rather than false precision.
    """
    if forecast.low is None or forecast.high is None:
        return 0.0
    sigma = (forecast.high - forecast.low) / (2 * 1.96)
    if sigma <= 0:
        return 1.0 if (target_value > forecast.value) == lower_is_better else 0.0
    below = probability_below(target_value, forecast.value, sigma)
    return round(below if lower_is_better else 1.0 - below, 4)


def triathlon_prediction(
    swim_time_s: float,
    bike_time_s: float,
    run_time_s: float,
    *,
    transition_allowance_s: float = 240.0,
) -> PredictionResult:
    """Segment sum plus a transition allowance.

    Transitions are a real and frequently underestimated cost — four minutes for
    two transitions is a reasonable age-group default, and is surfaced as an
    explicit parameter rather than buried.
    """
    total = swim_time_s + bike_time_s + run_time_s + transition_allowance_s
    return PredictionResult(
        metric="triathlon_total_time",
        value=round(total, 1),
        unit="s",
        method=f"segments + {transition_allowance_s:.0f}s transitions",
        confidence=0.6,
    )
