"""Statistical primitives for the analytics engine.

Deliberately dependency-free. NumPy/Pandas enter one layer up, in the batch
recompute path (see docs/08-tech-decisions.md, ADR-004), so that the
per-athlete deterministic core stays trivially testable and portable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "LinearFit",
    "clamp",
    "coefficient_of_variation",
    "ewma",
    "linear_regression",
    "mad",
    "mean",
    "median",
    "normal_cdf",
    "percentile",
    "probability_below",
    "robust_z_score",
    "rolling_mean",
    "rolling_sum",
    "smallest_worthwhile_change",
    "stdev",
    "z_score",
]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean() of empty sequence")
    return sum(values) / len(values)


def stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (n-1). Returns 0.0 for fewer than 2 points."""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    variance = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("median() of empty sequence")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation, scaled to be a consistent estimator of sigma
    for normal data (x1.4826)."""
    if not values:
        raise ValueError("mad() of empty sequence")
    med = median(values)
    return 1.4826 * median([abs(v - med) for v in values])


def coefficient_of_variation(values: Sequence[float]) -> float:
    """Within-athlete CV as a percentage. Undefined for a zero mean."""
    mu = mean(values)
    if mu == 0:
        return 0.0
    return 100.0 * stdev(values) / abs(mu)


def smallest_worthwhile_change(values: Sequence[float]) -> float:
    """SWC = 0.5 x within-athlete CV (Plews et al.).

    Below this, a day-to-day HRV move is noise and must not be surfaced as a
    signal. Returned as a percentage, on the same scale as the CV.
    """
    return 0.5 * coefficient_of_variation(values)


def z_score(value: float, baseline: Sequence[float], *, min_sd: float = 1e-9) -> float | None:
    """Standard score of `value` against a baseline window.

    Returns None when the baseline cannot support a comparison — fewer than 2
    points, or no dispersion at all. Callers must treat None as "unknown", not
    as zero: a flat baseline is missing information, not a neutral reading.
    """
    if len(baseline) < 2:
        return None
    sd = stdev(baseline)
    if sd <= min_sd:
        return None
    return (value - mean(baseline)) / sd


def robust_z_score(value: float, baseline: Sequence[float]) -> float | None:
    """Median/MAD standard score. Preferred where a single freak night of sleep
    or a botched HRV reading would otherwise drag the mean."""
    if len(baseline) < 2:
        return None
    dispersion = mad(baseline)
    if dispersion <= 1e-9:
        return None
    return (value - median(baseline)) / dispersion


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile, `pct` in 0..100."""
    if not values:
        raise ValueError("percentile() of empty sequence")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be within 0..100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def ewma(values: Sequence[float], time_constant_days: float, *, seed: float | None = None) -> list[float]:
    """Exponentially weighted moving average with an impulse-response decay.

    alpha = 1 - exp(-1 / time_constant). This is the Banister formulation used
    for CTL/ATL, not the 2/(N+1) convention from finance — the two disagree by
    enough to shift a form estimate by several points.
    """
    if time_constant_days <= 0:
        raise ValueError("time_constant_days must be positive")
    alpha = 1.0 - math.exp(-1.0 / time_constant_days)
    out: list[float] = []
    current = 0.0 if seed is None else seed
    for value in values:
        current = current + alpha * (value - current)
        out.append(current)
    return out


def rolling_sum(values: Sequence[float], window: int) -> list[float]:
    """Trailing sum over `window` points, inclusive of the current point.

    Short leading windows sum what is available rather than emitting None, so
    an athlete's first fortnight still produces usable output.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        out.append(float(sum(values[start : index + 1])))
    return out


def rolling_mean(values: Sequence[float], window: int) -> list[float]:
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        chunk = values[start : index + 1]
        out.append(sum(chunk) / len(chunk))
    return out


class LinearFit:
    """Ordinary least squares fit of y on x, plus what is needed to put an
    interval around a forecast."""

    __slots__ = ("_sxx", "_x_mean", "intercept", "n", "r_squared", "residual_sd", "slope")

    def __init__(
        self,
        slope: float,
        intercept: float,
        r_squared: float,
        residual_sd: float,
        n: int,
        x_mean: float,
        sxx: float,
    ) -> None:
        self.slope = slope
        self.intercept = intercept
        self.r_squared = r_squared
        self.residual_sd = residual_sd
        self.n = n
        self._x_mean = x_mean
        self._sxx = sxx

    def predict(self, x: float) -> float:
        return self.intercept + self.slope * x

    def prediction_sd(self, x: float) -> float:
        """Standard deviation of a single future observation at `x`.

        Includes both residual scatter and the uncertainty in the fitted line,
        so extrapolating far beyond the observed range widens honestly.
        """
        if self.n < 3 or self._sxx <= 0:
            return self.residual_sd
        leverage = 1.0 + 1.0 / self.n + ((x - self._x_mean) ** 2) / self._sxx
        return self.residual_sd * math.sqrt(leverage)


def linear_regression(
    xs: Sequence[float],
    ys: Sequence[float],
    weights: Sequence[float] | None = None,
) -> LinearFit:
    """Least squares fit, optionally weighted (use recency weights to let a
    recent block of training count for more than last winter's)."""
    if len(xs) != len(ys):
        raise ValueError("xs and ys must be the same length")
    if len(xs) < 2:
        raise ValueError("need at least 2 points to fit a line")
    if weights is None:
        weights = [1.0] * len(xs)
    if len(weights) != len(xs):
        raise ValueError("weights must match xs")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive value")

    x_mean = sum(w * x for w, x in zip(weights, xs, strict=False)) / total_weight
    y_mean = sum(w * y for w, y in zip(weights, ys, strict=False)) / total_weight
    sxx = sum(w * (x - x_mean) ** 2 for w, x in zip(weights, xs, strict=False))
    sxy = sum(w * (x - x_mean) * (y - y_mean) for w, x, y in zip(weights, xs, ys, strict=False))
    syy = sum(w * (y - y_mean) ** 2 for w, y in zip(weights, ys, strict=False))

    slope = 0.0 if sxx <= 0 else sxy / sxx
    intercept = y_mean - slope * x_mean
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=False)]
    weighted_sse = sum(w * r * r for w, r in zip(weights, residuals, strict=False))
    dof = len(xs) - 2
    residual_sd = math.sqrt(weighted_sse / dof) if dof > 0 else 0.0
    r_squared = 0.0 if syy <= 0 else clamp(1.0 - weighted_sse / syy, 0.0, 1.0)
    return LinearFit(slope, intercept, r_squared, residual_sd, len(xs), x_mean, sxx)


def normal_cdf(value: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        return 1.0 if value >= mu else 0.0
    return 0.5 * (1.0 + math.erf((value - mu) / (sigma * math.sqrt(2.0))))


def probability_below(threshold: float, mu: float, sigma: float) -> float:
    """P(X < threshold) for X ~ N(mu, sigma). Used for "will I break my PB?",
    where a lower time is the good outcome."""
    return normal_cdf(threshold, mu, sigma)
