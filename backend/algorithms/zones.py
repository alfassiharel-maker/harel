"""Training zones and intensity distribution.

Heart-rate zones are anchored on lactate threshold HR where we have it, and
fall back to %HRmax otherwise. Anchoring on LTHR matters: two athletes with the
same HRmax can have thresholds 15 bpm apart, so a %HRmax-only model puts one of
them in the wrong zone for most of a session.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .types import AthleteProfile, Sport

__all__ = [
    "Zone",
    "hr_zones",
    "power_zones",
    "pace_zones",
    "zone_for_value",
    "zone_seconds",
    "IntensityDistribution",
    "intensity_distribution",
]


@dataclass(frozen=True)
class Zone:
    index: int
    name: str
    low: float
    high: float
    anchor: str

    def contains(self, value: float) -> bool:
        return self.low <= value < self.high


# Friel 5-zone run/bike model, as fractions of LTHR. Z5 is left open-ended:
# splitting 5a/5b/5c is only meaningful with track-test data we do not have.
_LTHR_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Z1 Recovery", 0.00, 0.85),
    ("Z2 Aerobic", 0.85, 0.90),
    ("Z3 Tempo", 0.90, 0.95),
    ("Z4 Threshold", 0.95, 1.00),
    ("Z5 VO2max", 1.00, math.inf),
)

# Fallback bands as fractions of HRmax.
_HR_MAX_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Z1 Recovery", 0.50, 0.60),
    ("Z2 Aerobic", 0.60, 0.70),
    ("Z3 Tempo", 0.70, 0.80),
    ("Z4 Threshold", 0.80, 0.90),
    ("Z5 VO2max", 0.90, math.inf),
)

# Coggan 7-zone power model, as fractions of FTP.
_FTP_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Z1 Active Recovery", 0.00, 0.56),
    ("Z2 Endurance", 0.56, 0.76),
    ("Z3 Tempo", 0.76, 0.91),
    ("Z4 Threshold", 0.91, 1.06),
    ("Z5 VO2max", 1.06, 1.21),
    ("Z6 Anaerobic", 1.21, 1.51),
    ("Z7 Neuromuscular", 1.51, math.inf),
)

# Pace bands as fractions of threshold *speed*. Converted to seconds-per-km
# inside pace_zones(), where the ordering inverts.
_PACE_SPEED_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Z1 Recovery", 0.00, 0.78),
    ("Z2 Aerobic", 0.78, 0.87),
    ("Z3 Tempo", 0.87, 0.95),
    ("Z4 Threshold", 0.95, 1.02),
    ("Z5 VO2max", 1.02, math.inf),
)


def hr_zones(profile: AthleteProfile) -> tuple[Zone, ...]:
    """Five HR zones in bpm. Empty when neither LTHR nor HRmax can be derived."""
    if profile.lthr:
        anchor_value, bands, anchor = profile.lthr, _LTHR_BANDS, "lthr"
    else:
        hr_max = profile.effective_hr_max
        if not hr_max:
            return ()
        # An LTHR estimated from HRmax adds no information over %HRmax bands,
        # so use the %HRmax model directly rather than pretending to a threshold.
        anchor_value, bands, anchor = hr_max, _HR_MAX_BANDS, "hr_max"
    return tuple(
        Zone(index=i + 1, name=name, low=anchor_value * low, high=anchor_value * high, anchor=anchor)
        for i, (name, low, high) in enumerate(bands)
    )


def power_zones(ftp_watts: float) -> tuple[Zone, ...]:
    if ftp_watts <= 0:
        raise ValueError("ftp_watts must be positive")
    return tuple(
        Zone(index=i + 1, name=name, low=ftp_watts * low, high=ftp_watts * high, anchor="ftp")
        for i, (name, low, high) in enumerate(_FTP_BANDS)
    )


def pace_zones(threshold_pace_s_per_km: float) -> tuple[Zone, ...]:
    """Five pace zones in seconds per km.

    `low`/`high` stay ordered low-to-high numerically, which means Z1 holds the
    *slowest* paces (largest seconds-per-km). Callers comparing a pace to a zone
    should use `zone_for_value`, which handles the inversion.
    """
    if threshold_pace_s_per_km <= 0:
        raise ValueError("threshold_pace_s_per_km must be positive")
    zones: list[Zone] = []
    for i, (name, speed_low, speed_high) in enumerate(_PACE_SPEED_BANDS):
        # Faster speed fraction -> smaller seconds per km, hence the swap.
        pace_high = math.inf if speed_low == 0 else threshold_pace_s_per_km / speed_low
        pace_low = 0.0 if speed_high == math.inf else threshold_pace_s_per_km / speed_high
        zones.append(Zone(index=i + 1, name=name, low=pace_low, high=pace_high, anchor="threshold_pace"))
    return tuple(zones)


def zone_for_value(value: float, zones: Sequence[Zone]) -> Zone | None:
    for zone in zones:
        if zone.contains(value):
            return zone
    if zones and value >= zones[-1].low:
        return zones[-1]
    return None


def zone_seconds(
    samples: Sequence[float],
    zones: Sequence[Zone],
    sample_interval_s: float = 1.0,
) -> dict[int, float]:
    """Seconds accumulated in each zone from a sample stream."""
    if sample_interval_s <= 0:
        raise ValueError("sample_interval_s must be positive")
    totals: dict[int, float] = {zone.index: 0.0 for zone in zones}
    for value in samples:
        zone = zone_for_value(value, zones)
        if zone is not None:
            totals[zone.index] += sample_interval_s
    return totals


@dataclass(frozen=True)
class IntensityDistribution:
    """Three-band split used to check polarisation.

    Z1-Z2 is low, Z3 moderate, Z4+ high. The 80/20 target most endurance
    literature refers to is low vs (moderate + high).
    """

    low_pct: float
    moderate_pct: float
    high_pct: float
    total_seconds: float

    @property
    def is_polarised(self) -> bool:
        """Low >= 75% of time and more high than moderate — the shape a
        polarised block is meant to have."""
        return self.low_pct >= 75.0 and self.high_pct >= self.moderate_pct

    @property
    def easy_share_pct(self) -> float:
        return self.low_pct


def intensity_distribution(seconds_by_zone: dict[int, float]) -> IntensityDistribution:
    total = sum(seconds_by_zone.values())
    if total <= 0:
        return IntensityDistribution(0.0, 0.0, 0.0, 0.0)
    low = sum(v for k, v in seconds_by_zone.items() if k <= 2)
    moderate = seconds_by_zone.get(3, 0.0)
    high = sum(v for k, v in seconds_by_zone.items() if k >= 4)
    return IntensityDistribution(
        low_pct=100.0 * low / total,
        moderate_pct=100.0 * moderate / total,
        high_pct=100.0 * high / total,
        total_seconds=total,
    )


def default_zones_for_sport(profile: AthleteProfile, sport: Sport) -> tuple[Zone, ...]:
    """The zone set the UI should show for a sport, given what we know."""
    if sport is Sport.BIKE and profile.ftp_watts:
        return power_zones(profile.ftp_watts)
    if sport is Sport.RUN and profile.threshold_pace_s_per_km and not profile.effective_lthr:
        return pace_zones(profile.threshold_pace_s_per_km)
    return hr_zones(profile)
