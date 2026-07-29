"""Injury-risk index.

**Honest framing, enforced in the type system.** `RiskResult` carries
`is_clinically_validated=False` and a `model_version`, and the API contract
requires both to be rendered to the user. This is a transparent
literature-informed heuristic that flags known risk *patterns* — it is not a
fitted, validated, or clinical prediction, and no fitted model is possible until
we have labelled injury outcomes from real users (see docs/07-roadmap-backlog.md,
Phase 3).

Structure is deliberately a two-stage seam: `extract_features` produces a plain
feature dict, `predict` consumes it. A scikit-learn model trained on collected
labels drops into `predict` without touching feature extraction, so the features
we log from day one are exactly the features the future model trains on.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from .stats import clamp, mean, z_score
from .training_load import acwr, fitness_fatigue, monotony_strain, weekly_ramp_pct
from .types import (
    DEFAULT_SLEEP_NEED_MIN,
    AthleteProfile,
    DailyWellness,
    Driver,
    RiskBand,
    RiskResult,
)

__all__ = ["COEFFICIENTS", "INTERCEPT", "MODEL_VERSION", "assess", "extract_features", "predict"]

MODEL_VERSION = "heuristic-v0"

# Log-odds coefficients. Signs and relative magnitudes follow the consensus
# direction of effect in the sports-medicine literature; the magnitudes are
# priors, not fitted estimates.
#
# `prior_injury` is the largest single term because a previous injury in the
# last 12 months is the most consistently replicated predictor of the next one.
# `low_chronic_load` is second: low chronic load means tissue that has not been
# prepared for the acute work being asked of it.
COEFFICIENTS: dict[str, float] = {
    "acwr_excess": 0.45,
    "ramp_excess": 0.30,
    "monotony_excess": 0.25,
    "sleep_debt_h": 0.35,
    "hrv_suppression": 0.30,
    "resting_hr_elevation": 0.20,
    "prior_injury": 0.85,
    "low_chronic_load": 0.55,
    "training_age": -0.40,
    "age_over_40": 0.15,
}

# Base rate: ~7% chance of a training-related injury in a 4-week window for an
# athlete with none of the risk patterns present. Compounds to roughly 60% per
# year, consistent with reported running-injury incidence.
INTERCEPT = -2.60

# Every feature is clamped to this many risk units so that one extreme input
# cannot saturate the whole model.
_FEATURE_CAP = 3.0

_TARGET_SLEEP_H = DEFAULT_SLEEP_NEED_MIN / 60.0
_HRV_SUPPRESSION_Z = -1.0
_HRV_LOOKBACK_DAYS = 14


def extract_features(
    profile: AthleteProfile,
    loads: Mapping[date, float],
    wellness_history: Sequence[DailyWellness],
    ref_day: date,
) -> dict[str, float]:
    """Engineer the risk feature vector for `ref_day`.

    Every feature is expressed in "risk units" — 0 means the pattern is absent,
    1 means one meaningful step of exposure. Absent inputs contribute 0, which
    is the correct neutral here (we cannot observe a risk pattern we have no
    data for), and `assess` reports the resulting coverage separately.
    """
    features = {name: 0.0 for name in COEFFICIENTS}

    ratio_result = acwr(loads, ref_day)
    if ratio_result.is_reliable and ratio_result.ratio is not None:
        features["acwr_excess"] = clamp(max(0.0, ratio_result.ratio - 1.30) / 0.20, 0.0, _FEATURE_CAP)

    ramp = weekly_ramp_pct(loads, ref_day)
    if ramp is not None:
        features["ramp_excess"] = clamp(max(0.0, ramp - 10.0) / 10.0, 0.0, _FEATURE_CAP)

    monotony, _ = monotony_strain(loads, ref_day)
    if monotony is not None:
        features["monotony_excess"] = clamp(max(0.0, monotony - 1.80) / 0.50, 0.0, _FEATURE_CAP)

    if loads:
        start = min(loads)
        if start <= ref_day:
            points = fitness_fatigue(loads, start, ref_day)
            if points:
                ctl = points[-1].ctl
                # A CTL of 30 is treated as the floor of "prepared"; below it,
                # risk rises as chronic load falls.
                features["low_chronic_load"] = clamp(max(0.0, 30.0 - ctl) / 30.0, 0.0, _FEATURE_CAP)

    features["sleep_debt_h"] = clamp(_sleep_debt_per_night(wellness_history, ref_day), 0.0, _FEATURE_CAP)
    features["hrv_suppression"] = clamp(_hrv_suppression(wellness_history, ref_day), 0.0, _FEATURE_CAP)
    features["resting_hr_elevation"] = clamp(
        _resting_hr_elevation(wellness_history, ref_day), 0.0, _FEATURE_CAP
    )

    features["prior_injury"] = 1.0 if profile.injuries_last_12m >= 1 else 0.0
    if profile.training_age_years is not None:
        features["training_age"] = clamp(min(profile.training_age_years, 5.0) / 5.0, 0.0, 1.0)
    if profile.age:
        features["age_over_40"] = clamp(max(0, profile.age - 40) / 10.0, 0.0, _FEATURE_CAP)

    return features


def _sleep_debt_per_night(history: Sequence[DailyWellness], ref_day: date) -> float:
    """Average hours per night below target over the trailing week."""
    window = [
        entry.sleep_total_min / 60.0
        for entry in history
        if entry.sleep_total_min and ref_day - timedelta(days=6) <= entry.day <= ref_day
    ]
    if not window:
        return 0.0
    return max(0.0, _TARGET_SLEEP_H - mean(window))


def _hrv_suppression(history: Sequence[DailyWellness], ref_day: date) -> float:
    """Suppressed HRV days in the last fortnight, in units of 7 days.

    Each day is scored against the 28-day baseline that preceded *it*, so a
    genuine downward shift in baseline is not counted forever as suppression.
    """
    lookback_start = ref_day - timedelta(days=_HRV_LOOKBACK_DAYS - 1)
    by_day = {entry.day: entry for entry in history if entry.ln_hrv is not None}
    suppressed = 0
    for offset in range(_HRV_LOOKBACK_DAYS):
        day = lookback_start + timedelta(days=offset)
        entry = by_day.get(day)
        if entry is None or entry.ln_hrv is None:
            continue
        baseline = [
            other.ln_hrv
            for other in by_day.values()
            if other.ln_hrv is not None and day - timedelta(days=28) <= other.day < day
        ]
        if len(baseline) < 7:
            continue
        z = z_score(entry.ln_hrv, baseline)
        if z is not None and z < _HRV_SUPPRESSION_Z:
            suppressed += 1
    return suppressed / 7.0


def _resting_hr_elevation(history: Sequence[DailyWellness], ref_day: date) -> float:
    """How far the trailing week's resting HR sits above the 28-day baseline,
    in standard deviations (0 when at or below baseline)."""
    recent = [
        entry.resting_hr
        for entry in history
        if entry.resting_hr and ref_day - timedelta(days=6) <= entry.day <= ref_day
    ]
    baseline = [
        entry.resting_hr
        for entry in history
        if entry.resting_hr and ref_day - timedelta(days=34) <= entry.day < ref_day - timedelta(days=6)
    ]
    if len(recent) < 3 or len(baseline) < 7:
        return 0.0
    z = z_score(mean(recent), baseline)
    if z is None:
        return 0.0
    return max(0.0, z)


def predict(features: Mapping[str, float]) -> float:
    """Logistic probability from the feature vector. Pure function, no I/O —
    this is the seam a trained model replaces."""
    logit = INTERCEPT + sum(COEFFICIENTS[name] * features.get(name, 0.0) for name in COEFFICIENTS)
    return 1.0 / (1.0 + math.exp(-logit))


def _band(probability: float) -> RiskBand:
    if probability < 0.10:
        return RiskBand.LOW
    if probability < 0.20:
        return RiskBand.MODERATE
    if probability < 0.35:
        return RiskBand.HIGH
    return RiskBand.VERY_HIGH


def assess(
    profile: AthleteProfile,
    loads: Mapping[date, float],
    wellness_history: Sequence[DailyWellness],
    ref_day: date,
) -> RiskResult:
    """Risk index for `ref_day` with per-feature attribution.

    Driver `contribution` values are **log-odds**, not probability points, and
    together with `INTERCEPT` they sum to the logit. Log-odds are additive where
    probability contributions are not, so this is the only decomposition that is
    arithmetically honest.
    """
    features = extract_features(profile, loads, wellness_history, ref_day)
    probability = predict(features)

    drivers = [
        Driver(
            name=name,
            score=round(features[name], 3),
            weight=COEFFICIENTS[name],
            contribution=round(COEFFICIENTS[name] * features[name], 4),
            value=round(features[name], 3),
            baseline=0.0,
        )
        for name in COEFFICIENTS
        if abs(COEFFICIENTS[name] * features[name]) > 1e-9
    ]
    drivers.sort(key=lambda d: abs(d.contribution), reverse=True)

    return RiskResult(
        probability=round(probability, 4),
        band=_band(probability),
        drivers=tuple(drivers),
        model_version=MODEL_VERSION,
        is_clinically_validated=False,
    )
