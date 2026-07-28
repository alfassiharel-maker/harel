"""Training load: per-session scoring and the load-over-time model.

Every load score is expressed on one scale — **100 = one hour at threshold** —
regardless of which input produced it. Without that normalisation a cyclist's
power-based number and a swimmer's pace-based number cannot be summed into a
single weekly load, and every downstream metric (CTL, ACWR, monotony) inherits
the inconsistency.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .stats import ewma, mean, rolling_sum, stdev
from .types import ActivitySummary, AthleteProfile, LoadPoint, LoadSource, Sex, Sport, TrainingLoadResult

__all__ = [
    "trimp",
    "normalized_power",
    "intensity_factor",
    "power_tss",
    "hr_tss",
    "pace_tss",
    "swim_tss",
    "session_rpe_load",
    "training_load",
    "daily_load_series",
    "fitness_fatigue",
    "AcwrResult",
    "acwr",
    "monotony_strain",
    "weekly_ramp_pct",
    "CTL_TIME_CONSTANT_DAYS",
    "ATL_TIME_CONSTANT_DAYS",
]

CTL_TIME_CONSTANT_DAYS = 42.0
ATL_TIME_CONSTANT_DAYS = 7.0

# One hour at RPE 7 is treated as one threshold hour: 60 x 7 = 420 Foster AU
# maps to a load of 100.
RPE_LOAD_CALIBRATION = 100.0 / 420.0

# Confidence attached to each source, used by the AI layer to decide how hard
# to lean on a number in its explanation.
_SOURCE_CONFIDENCE = {
    LoadSource.POWER: 0.95,
    LoadSource.PACE: 0.80,
    LoadSource.HEART_RATE: 0.75,
    LoadSource.RPE: 0.45,
    LoadSource.NONE: 0.0,
}

# Per-sport precedence. Running prefers HR over pace because we have no
# grade-adjusted pace: on hilly terrain raw pace badly under-reads effort.
# Swimming prefers pace over HR because in-water HR from a wrist optical sensor
# is unreliable.
_PRECEDENCE: dict[Sport, tuple[LoadSource, ...]] = {
    Sport.BIKE: (LoadSource.POWER, LoadSource.HEART_RATE, LoadSource.RPE),
    Sport.RUN: (LoadSource.POWER, LoadSource.HEART_RATE, LoadSource.PACE, LoadSource.RPE),
    Sport.SWIM: (LoadSource.PACE, LoadSource.HEART_RATE, LoadSource.RPE),
    Sport.STRENGTH: (LoadSource.HEART_RATE, LoadSource.RPE),
    Sport.OTHER: (LoadSource.HEART_RATE, LoadSource.RPE),
}


def trimp(
    duration_min: float,
    avg_hr: float,
    hr_rest: float,
    hr_max: float,
    sex: Sex = Sex.UNSPECIFIED,
) -> float:
    """Banister TRIMP with sex-specific exponential weighting.

    For Sex.UNSPECIFIED we average the male and female forms rather than
    silently defaulting to male, which would misreport load for roughly half
    the user base by up to ~10% at high intensity.
    """
    if duration_min <= 0:
        return 0.0
    if hr_max <= hr_rest:
        raise ValueError("hr_max must exceed hr_rest")
    hr_reserve = max(0.0, min(1.0, (avg_hr - hr_rest) / (hr_max - hr_rest)))
    male = 0.64 * math.exp(1.92 * hr_reserve)
    female = 0.86 * math.exp(1.67 * hr_reserve)
    if sex is Sex.MALE:
        factor = male
    elif sex is Sex.FEMALE:
        factor = female
    else:
        factor = (male + female) / 2.0
    return duration_min * hr_reserve * factor


def normalized_power(power_samples: Sequence[float], sample_interval_s: float = 1.0) -> float | None:
    """Coggan normalised power: 30-second rolling average, then the fourth root
    of the mean of fourth powers.

    Returns None for streams shorter than 30 seconds of data, where the rolling
    window is not defined.
    """
    if not power_samples or sample_interval_s <= 0:
        return None
    window = max(1, int(round(30.0 / sample_interval_s)))
    if len(power_samples) < window:
        return None
    rolled: list[float] = []
    running = sum(power_samples[:window])
    rolled.append(running / window)
    for index in range(window, len(power_samples)):
        running += power_samples[index] - power_samples[index - window]
        rolled.append(running / window)
    fourth = mean([value**4 for value in rolled])
    return fourth**0.25


def intensity_factor(normalized: float, threshold: float) -> float:
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    return normalized / threshold


def power_tss(duration_s: float, normalized: float, ftp_watts: float) -> float:
    """Coggan TSS. Reduces to 100 x hours x IF^2."""
    if duration_s <= 0:
        return 0.0
    factor = intensity_factor(normalized, ftp_watts)
    return 100.0 * (duration_s / 3600.0) * factor**2


def hr_tss(
    duration_min: float,
    avg_hr: float,
    profile: AthleteProfile,
) -> float | None:
    """HR-based load, self-calibrated so one hour at the athlete's own LTHR
    scores 100.

    Calibrating per athlete rather than against a population constant means the
    number stays comparable to a power-based TSS for the same person, which is
    what makes a triathlete's combined weekly load meaningful.
    """
    hr_max = profile.effective_hr_max
    lthr = profile.effective_lthr
    if not hr_max or not lthr:
        return None
    hr_rest = profile.effective_hr_rest
    if hr_max <= hr_rest:
        return None
    reference = trimp(60.0, lthr, hr_rest, hr_max, profile.sex)
    if reference <= 0:
        return None
    session = trimp(duration_min, avg_hr, hr_rest, hr_max, profile.sex)
    return 100.0 * session / reference


def pace_tss(duration_s: float, pace_s_per_km: float, threshold_pace_s_per_km: float) -> float:
    """Run load from pace (TrainingPeaks rTSS convention, IF^2).

    Uses raw pace, not grade-adjusted pace, because Garmin's activity summary
    does not expose a normalised graded pace. On rolling terrain this
    under-reports; `training_load` therefore ranks it below heart rate for runs.
    """
    if duration_s <= 0 or pace_s_per_km <= 0:
        return 0.0
    factor = threshold_pace_s_per_km / pace_s_per_km
    return 100.0 * (duration_s / 3600.0) * factor**2


def swim_tss(duration_s: float, pace_s_per_100m: float, css_s_per_100m: float) -> float:
    """Swim load from critical swim speed (TrainingPeaks sTSS convention, IF^3).

    The cubic exponent reflects that drag rises roughly with the square of
    velocity, so the metabolic cost of going faster in water climbs more
    steeply than on land.
    """
    if duration_s <= 0 or pace_s_per_100m <= 0:
        return 0.0
    factor = css_s_per_100m / pace_s_per_100m
    return 100.0 * (duration_s / 3600.0) * factor**3


def session_rpe_load(duration_min: float, rpe: float) -> float:
    """Foster session-RPE, rescaled onto the threshold-hour scale."""
    if duration_min <= 0:
        return 0.0
    if not 1 <= rpe <= 10:
        raise ValueError("rpe must be within 1..10")
    return duration_min * rpe * RPE_LOAD_CALIBRATION


def training_load(activity: ActivitySummary, profile: AthleteProfile) -> TrainingLoadResult:
    """Best available load score for one session, plus which input produced it.

    Walks the sport's precedence list and returns the first source with enough
    data. Never raises on missing data — an activity with nothing usable scores
    0 with `LoadSource.NONE`, which the UI surfaces as "needs a perceived
    effort" rather than as a zero-effort session.
    """
    detail: dict[str, float] = {}
    for source in _PRECEDENCE.get(activity.sport, _PRECEDENCE[Sport.OTHER]):
        score = _score_for_source(activity, profile, source, detail)
        if score is not None:
            return TrainingLoadResult(
                score=round(score, 2),
                source=source,
                confidence=_SOURCE_CONFIDENCE[source],
                detail=detail,
            )
    return TrainingLoadResult(score=0.0, source=LoadSource.NONE, confidence=0.0, detail=detail)


def _score_for_source(
    activity: ActivitySummary,
    profile: AthleteProfile,
    source: LoadSource,
    detail: dict[str, float],
) -> float | None:
    if source is LoadSource.POWER:
        power = activity.normalized_power or activity.avg_power
        # Threshold power is sport-specific. A cyclist's FTP must never be used
        # to scale running power, so each sport reads its own anchor and falls
        # through to heart rate when that anchor is missing.
        if activity.sport is Sport.BIKE:
            threshold = profile.ftp_watts
        elif activity.sport is Sport.RUN:
            threshold = profile.run_threshold_power_w
        else:
            threshold = None
        if power and threshold:
            detail["intensity_factor"] = round(intensity_factor(power, threshold), 3)
            detail["power_used_w"] = round(power, 1)
            return power_tss(activity.active_time_s, power, threshold)
        return None

    if source is LoadSource.HEART_RATE:
        if activity.avg_hr:
            score = hr_tss(activity.duration_min, activity.avg_hr, profile)
            if score is not None:
                detail["avg_hr"] = round(activity.avg_hr, 1)
            return score
        return None

    if source is LoadSource.PACE:
        if activity.sport is Sport.SWIM:
            pace = activity.pace_s_per_100m
            if pace and profile.css_s_per_100m:
                detail["pace_s_per_100m"] = round(pace, 1)
                return swim_tss(activity.active_time_s, pace, profile.css_s_per_100m)
            return None
        pace = activity.pace_s_per_km
        if pace and profile.threshold_pace_s_per_km:
            detail["pace_s_per_km"] = round(pace, 1)
            return pace_tss(activity.active_time_s, pace, profile.threshold_pace_s_per_km)
        return None

    if source is LoadSource.RPE:
        if activity.rpe:
            detail["rpe"] = float(activity.rpe)
            return session_rpe_load(activity.duration_min, activity.rpe)
        return None

    return None


def daily_load_series(
    activities: Sequence[ActivitySummary],
    profile: AthleteProfile,
) -> dict[date, float]:
    """Total load per calendar day. Multiple sessions in a day sum, which is
    what makes a triathlete's doubles show up as the stress they are."""
    totals: dict[date, float] = {}
    for activity in activities:
        result = training_load(activity, profile)
        totals[activity.start_date] = totals.get(activity.start_date, 0.0) + result.score
    return totals


def _dense_days(loads: Mapping[date, float], start: date, end: date) -> tuple[list[date], list[float]]:
    """Expand a sparse day->load map into a gap-free series.

    Rest days must be present as explicit zeros: skipping them would let a
    two-week layoff decay CTL as if it were two days.
    """
    if end < start:
        raise ValueError("end must not precede start")
    days: list[date] = []
    values: list[float] = []
    current = start
    while current <= end:
        days.append(current)
        values.append(float(loads.get(current, 0.0)))
        current += timedelta(days=1)
    return days, values


def fitness_fatigue(
    loads: Mapping[date, float],
    start: date,
    end: date,
    *,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
) -> list[LoadPoint]:
    """Banister impulse-response fitness (CTL/42d) and fatigue (ATL/7d).

    Form (TSB) is CTL - ATL, available on each point. Seed values let a
    recompute resume from stored state instead of re-reading an athlete's
    entire history.
    """
    days, values = _dense_days(loads, start, end)
    ctl = ewma(values, CTL_TIME_CONSTANT_DAYS, seed=seed_ctl)
    atl = ewma(values, ATL_TIME_CONSTANT_DAYS, seed=seed_atl)
    return [
        LoadPoint(day=day, load=load, ctl=round(c, 2), atl=round(a, 2))
        for day, load, c, a in zip(days, values, ctl, atl)
    ]


@dataclass(frozen=True)
class AcwrResult:
    ratio: float | None
    acute_daily: float
    chronic_daily: float
    method: str
    days_of_history: int

    @property
    def is_reliable(self) -> bool:
        """An ACWR computed on less than a full chronic window is not
        meaningful — early in an athlete's history the denominator is still
        filling up and the ratio reads artificially high."""
        return self.days_of_history >= 28 and self.ratio is not None

    @property
    def zone(self) -> str:
        if self.ratio is None:
            return "unknown"
        if self.ratio < 0.80:
            return "undertraining"
        if self.ratio <= 1.30:
            return "sweet_spot"
        if self.ratio <= 1.50:
            return "elevated"
        return "danger"


def acwr(
    loads: Mapping[date, float],
    ref_day: date,
    *,
    acute_days: int = 7,
    chronic_days: int = 28,
    method: str = "rolling",
) -> AcwrResult:
    """Acute:chronic workload ratio as a ratio of *daily averages*.

    Note on evidence: ACWR is contested in the sports-science literature
    (Impellizzeri and colleagues have shown the coupled rolling form is
    mathematically prone to spurious association). We compute it because it is
    a useful and widely understood monotonicity check on ramp rate, and we
    surface it as one driver among several rather than as a verdict. It is never
    the sole input to the injury-risk model.
    """
    if chronic_days <= acute_days:
        raise ValueError("chronic_days must exceed acute_days")
    start = ref_day - timedelta(days=chronic_days - 1)
    _, values = _dense_days(loads, start, ref_day)
    observed_days = len([d for d in loads if start <= d <= ref_day])

    if method == "ewma":
        # Both averages are seeded with the window's mean daily load rather than
        # zero. Seeded at zero, the 28-day average is still only ~63% converged
        # after 28 samples while the 7-day one is ~98% converged, which inflates
        # the ratio to ~1.55 even for a perfectly constant load.
        seed = mean(values) if values else 0.0
        acute_daily = ewma(values, float(acute_days), seed=seed)[-1]
        chronic_daily = ewma(values, float(chronic_days), seed=seed)[-1]
    elif method == "rolling":
        acute_daily = rolling_sum(values, acute_days)[-1] / acute_days
        chronic_daily = rolling_sum(values, chronic_days)[-1] / chronic_days
    else:
        raise ValueError("method must be 'rolling' or 'ewma'")

    ratio = None if chronic_daily <= 0 else acute_daily / chronic_daily
    return AcwrResult(
        ratio=None if ratio is None else round(ratio, 3),
        acute_daily=round(acute_daily, 2),
        chronic_daily=round(chronic_daily, 2),
        method=method,
        days_of_history=observed_days,
    )


def monotony_strain(loads: Mapping[date, float], ref_day: date, window_days: int = 7) -> tuple[float | None, float | None]:
    """Foster training monotony (mean/SD of daily load) and strain
    (weekly load x monotony).

    High monotony means every day looks the same — the classic pattern behind
    non-functional overreaching even at a moderate total volume.
    """
    start = ref_day - timedelta(days=window_days - 1)
    _, values = _dense_days(loads, start, ref_day)
    spread = stdev(values)
    if spread <= 0:
        return None, None
    monotony = mean(values) / spread
    strain = sum(values) * monotony
    return round(monotony, 3), round(strain, 1)


def weekly_ramp_pct(loads: Mapping[date, float], ref_day: date) -> float | None:
    """Percentage change in weekly load versus the preceding week.

    Returns None when the previous week is empty, where a percentage change is
    undefined (coming back from zero is not an infinite ramp, it is a restart).
    """
    this_start = ref_day - timedelta(days=6)
    prev_start = ref_day - timedelta(days=13)
    _, this_week = _dense_days(loads, this_start, ref_day)
    _, prev_week = _dense_days(loads, prev_start, this_start - timedelta(days=1))
    previous = sum(prev_week)
    if previous <= 0:
        return None
    return round(100.0 * (sum(this_week) - previous) / previous, 1)
