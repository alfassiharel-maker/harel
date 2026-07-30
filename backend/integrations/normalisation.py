"""Shared, pure normalisation helpers used by every adapter.

Each provider reports the same physical quantity in its own unit — Garmin gives
speed in m/s and Polar gives pace as a duration string; one reports duration
including pauses and another excluding them. Every conversion lives here rather
than in each adapter, because two adapters implementing "metres per second" twice
will eventually disagree, and the symptom is a load score that is quietly wrong for
one provider's athletes only.

Everything in this module is a pure function: no clock, no network, no database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math

from backend.integrations.types import NormalisedSport

__all__ = [
    "checksum",
    "coerce_positive",
    "local_date_of",
    "map_sport",
    "pace_to_speed",
    "safe_ratio",
    "speed_from_distance_duration",
    "speed_to_pace",
]

# Provider sport vocabularies → ours. Deliberately explicit rather than a fuzzy
# match on substrings: `INDOOR_CYCLING` and `CYCLING` must both map to `bike`, but
# an unrecognised future value must map to `other` and be visible, not silently
# guessed into the wrong sport. A wrong sport picks the wrong load precedence and
# the wrong zone model.
_SPORT_MAP: dict[str, NormalisedSport] = {
    # running
    "running": "run",
    "run": "run",
    "trail_running": "run",
    "treadmill_running": "run",
    "indoor_running": "run",
    "track_running": "run",
    "street_running": "run",
    "virtual_run": "run",
    "ultra_run": "run",
    "walking": "run",
    "hiking": "run",
    # cycling
    "cycling": "bike",
    "bike": "bike",
    "road_biking": "bike",
    "mountain_biking": "bike",
    "gravel_cycling": "bike",
    "indoor_cycling": "bike",
    "virtual_ride": "bike",
    "cyclocross": "bike",
    "track_cycling": "bike",
    "bmx": "bike",
    "e_bike_fitness": "bike",
    # swimming
    "swimming": "swim",
    "swim": "swim",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "open_water": "swim",
    # strength
    "strength_training": "strength",
    "strength": "strength",
    "weight_training": "strength",
    "indoor_cardio": "strength",
    "hiit": "strength",
    "pilates": "strength",
    "yoga": "strength",
    "crossfit": "strength",
    # multisport containers — a triathlon arrives as a parent with child sessions
    "multi_sport": "multisport",
    "multisport": "multisport",
    "triathlon": "multisport",
    "duathlon": "multisport",
    "swimrun": "multisport",
}


def map_sport(provider_sport: str | None) -> NormalisedSport:
    """Map a provider's sport string to ours, defaulting to `other`.

    `other` is a real answer, not a failure: the analytics engine handles it by
    falling back to RPE-based load, which is honest. Guessing `run` because the
    string contained an "r" would not be.
    """
    if not provider_sport:
        return "other"
    return _SPORT_MAP.get(provider_sport.strip().lower(), "other")


def local_date_of(start_time: dt.datetime, offset_s: int | None) -> dt.date:
    """The athlete's calendar day for an activity.

    A 23:30 run belongs to that day for the athlete even though it is already
    tomorrow in UTC, and every daily aggregate — load, readiness, streaks — keys on
    this. Getting it wrong shifts a session into the wrong day and corrupts both
    days' totals.

    Falls back to the UTC date when the provider reports no offset, which is
    wrong by at most one day and is the best available answer.
    """
    if start_time.tzinfo is None:
        raise ValueError("start_time must be timezone-aware")
    if offset_s is None:
        return start_time.astimezone(dt.UTC).date()
    return (start_time.astimezone(dt.UTC) + dt.timedelta(seconds=offset_s)).date()


def coerce_positive(value: float | int | None, *, upper: float | None = None) -> float | None:
    """Keep a strictly-positive finite value, else `None`.

    Providers emit `0`, `-1` and `null` interchangeably for "no sensor". Mapping all
    three to `None` is what preserves the project-wide rule that missing data is
    `None` and never zero — a `0` average power reaching the engine would be treated
    as a real measurement and would drag the athlete's efficiency baseline down.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    if upper is not None and number > upper:
        # Out-of-range is a sensor fault or a unit mismatch, not a measurement.
        return None
    return number


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning `None` rather than raising or fabricating on bad input."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def speed_from_distance_duration(distance_m: float | None, duration_s: float | None) -> float | None:
    """Derive average speed when the provider reports distance and time but not speed."""
    return safe_ratio(distance_m, duration_s)


def pace_to_speed(pace_s_per_km: float | None) -> float | None:
    """Seconds per kilometre → metres per second."""
    if pace_s_per_km is None or pace_s_per_km <= 0:
        return None
    return 1000.0 / pace_s_per_km


def speed_to_pace(speed_m_s: float | None) -> float | None:
    """Metres per second → seconds per kilometre."""
    if speed_m_s is None or speed_m_s <= 0:
        return None
    return 1000.0 / speed_m_s


def checksum(payload: bytes) -> str:
    """SHA-256 hex digest, for verifying an object-storage stream round-trips.

    Stored alongside the storage key so a truncated or corrupted upload is
    detectable at read time instead of surfacing as an inexplicable analytics result.
    """
    return hashlib.sha256(payload).hexdigest()
