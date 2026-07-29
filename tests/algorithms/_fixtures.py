"""Deterministic fixtures for analytics tests.

No randomness anywhere: a failing analytics test must be reproducible from the
test name alone, and a flaky physiology test is worse than no test.
"""

from __future__ import annotations

from datetime import date, timedelta

from backend.algorithms.types import (
    ActivitySummary,
    AthleteProfile,
    DailyWellness,
    Driver,
    ReadinessBand,
    ReadinessResult,
    Sex,
    Sport,
)

REF_DAY = date(2026, 6, 15)


def profile(**overrides) -> AthleteProfile:
    """A fully-instrumented intermediate triathlete."""
    defaults = {
        "sex": Sex.MALE,
        "age": 35,
        "weight_kg": 72.0,
        "height_cm": 178.0,
        "hr_max": 190,
        "hr_rest": 50,
        "lthr": 170,
        "ftp_watts": 250.0,
        "threshold_pace_s_per_km": 240.0,
        "css_s_per_100m": 95.0,
        "training_age_years": 6.0,
        "injuries_last_12m": 0,
    }
    defaults.update(overrides)
    return AthleteProfile(**defaults)


def steady_wellness(
    days: int = 40,
    end: date = REF_DAY,
    *,
    hrv_base: float = 60.0,
    resting_hr: float = 50.0,
    sleep_min: float = 480.0,
) -> list[DailyWellness]:
    """A stable baseline with small deterministic day-to-day variation.

    The variation matters: a perfectly flat series has zero dispersion, and the
    engine correctly refuses to compute a z-score against it.
    """
    out: list[DailyWellness] = []
    for offset in range(days, 0, -1):
        day = end - timedelta(days=offset)
        wobble = (offset % 5) - 2  # -2..+2
        out.append(
            DailyWellness(
                day=day,
                hrv_rmssd_ms=hrv_base + wobble,
                resting_hr=resting_hr + (wobble * 0.5),
                sleep_total_min=sleep_min + wobble * 5,
                sleep_deep_min=(sleep_min + wobble * 5) * 0.22,
                sleep_rem_min=(sleep_min + wobble * 5) * 0.24,
                sleep_efficiency_pct=92.0,
                soreness=2,
                mood=2,
                stress=2,
                fatigue=2,
            )
        )
    return out


def constant_loads(daily: float = 50.0, days: int = 60, end: date = REF_DAY) -> dict[date, float]:
    return {end - timedelta(days=offset): daily for offset in range(days)}


def spiking_loads(end: date = REF_DAY) -> dict[date, float]:
    """Eight quiet weeks then a sharp overload week — the pattern the risk model
    is meant to catch."""
    loads: dict[date, float] = {}
    for offset in range(56, 7, -1):
        loads[end - timedelta(days=offset)] = 30.0
    for offset in range(7, -1, -1):
        loads[end - timedelta(days=offset)] = 130.0
    return loads


def run_activity(**overrides) -> ActivitySummary:
    defaults = {
        "sport": Sport.RUN,
        "start_date": REF_DAY,
        "duration_s": 3600,
        "moving_time_s": 3600,
        "distance_m": 14000.0,
        "avg_hr": 155.0,
    }
    defaults.update(overrides)
    return ActivitySummary(**defaults)


def bike_activity(**overrides) -> ActivitySummary:
    defaults = {
        "sport": Sport.BIKE,
        "start_date": REF_DAY,
        "duration_s": 3600,
        "moving_time_s": 3600,
        "distance_m": 32000.0,
        "avg_power": 220.0,
        "normalized_power": 235.0,
        "avg_hr": 145.0,
    }
    defaults.update(overrides)
    return ActivitySummary(**defaults)


def swim_activity(**overrides) -> ActivitySummary:
    defaults = {
        "sport": Sport.SWIM,
        "start_date": REF_DAY,
        "duration_s": 1800,
        "moving_time_s": 1800,
        "distance_m": 1800.0,
        "total_strokes": 1200,
        "pool_length_m": 25.0,
        "avg_hr": 140.0,
    }
    defaults.update(overrides)
    return ActivitySummary(**defaults)


def readiness_result(score: float, *, data_quality: float = 1.0) -> ReadinessResult:
    """Hand-built readiness for testing the adaptation gates in isolation."""
    band = (
        ReadinessBand.COMPROMISED
        if score < 25
        else ReadinessBand.LIMITED
        if score < 50
        else ReadinessBand.MODERATE
        if score < 70
        else ReadinessBand.GOOD
        if score < 85
        else ReadinessBand.PRIME
    )
    return ReadinessResult(
        score=score,
        band=band,
        drivers=(Driver(name="hrv", score=score, weight=1.0, contribution=score - 50.0),),
        data_quality=data_quality,
    )
