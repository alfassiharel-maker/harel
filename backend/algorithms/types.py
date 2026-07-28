"""Domain types for the analytics engine.

Pure data containers: no I/O, no framework imports, no third-party dependencies.
The engine is importable by the API layer, the batch worker, the ML training
scripts and the test suite alike.

See docs/04-analytics-algorithms.md for the reasoning behind each field.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

# Defaults used when an athlete has not supplied (or we cannot infer) a value.
DEFAULT_HR_REST = 60
DEFAULT_SLEEP_NEED_MIN = 8 * 60
LTHR_FRACTION_OF_HR_MAX = 0.90


class Sport(str, Enum):
    RUN = "run"
    BIKE = "bike"
    SWIM = "swim"
    STRENGTH = "strength"
    OTHER = "other"


class Sex(str, Enum):
    MALE = "male"
    FEMALE = "female"
    UNSPECIFIED = "unspecified"


class LoadSource(str, Enum):
    """Which input produced a training-load score.

    Ordered by trust: power is a direct mechanical measurement, RPE is a
    self-report. Every score carries its source so the UI and the AI layer can
    say how much to lean on it.
    """

    POWER = "power"
    HEART_RATE = "heart_rate"
    PACE = "pace"
    RPE = "rpe"
    NONE = "none"


class ReadinessBand(str, Enum):
    COMPROMISED = "compromised"
    LIMITED = "limited"
    MODERATE = "moderate"
    GOOD = "good"
    PRIME = "prime"


class RiskBand(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"


class TrainingPhase(str, Enum):
    BASE = "base"
    BUILD = "build"
    PEAK = "peak"
    TAPER = "taper"
    RACE = "race"
    RECOVERY = "recovery"


class Adaptation(str, Enum):
    AS_PLANNED = "as_planned"
    REDUCE_INTENSITY = "reduce_intensity"
    REDUCE_VOLUME = "reduce_volume"
    EASY_ONLY = "easy_only"
    REST = "rest"


class Goal(str, Enum):
    RACE_TIME = "race_time"
    ENDURANCE = "endurance"
    STRENGTH = "strength"
    WEIGHT_LOSS = "weight_loss"
    GENERAL_FITNESS = "general_fitness"


class Level(str, Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


@dataclass(frozen=True)
class Driver:
    """One explained contributor to a composite score.

    `contribution` is in final-score points, signed: negative means this input
    pushed the score down. The AI layer sends drivers, never raw streams.
    """

    name: str
    score: float
    weight: float
    contribution: float
    value: float | None = None
    baseline: float | None = None

    @property
    def direction(self) -> str:
        if self.contribution > 0.5:
            return "positive"
        if self.contribution < -0.5:
            return "negative"
        return "neutral"


@dataclass(frozen=True)
class AthleteProfile:
    """Physiological profile. Every threshold is optional — the engine degrades
    to a lower-trust load source rather than refusing to produce a number."""

    sex: Sex = Sex.UNSPECIFIED
    age: int | None = None
    weight_kg: float | None = None
    height_cm: float | None = None
    hr_max: int | None = None
    hr_rest: int | None = None
    lthr: int | None = None
    # Cycling FTP. Kept strictly separate from running threshold power: they are
    # different physiological quantities in the same unit, and treating one as
    # the other silently corrupts every load score for the affected sport.
    ftp_watts: float | None = None
    run_threshold_power_w: float | None = None
    threshold_pace_s_per_km: float | None = None
    css_s_per_100m: float | None = None
    training_age_years: float | None = None
    injuries_last_12m: int = 0
    level: Level = Level.INTERMEDIATE

    @property
    def effective_hr_max(self) -> int | None:
        """Measured HRmax, else the Tanaka age estimate (208 - 0.7 x age).

        Tanaka is used rather than 220-age: it is materially more accurate for
        masters athletes, which is a large share of the target market.
        """
        if self.hr_max:
            return self.hr_max
        if self.age:
            return int(round(208 - 0.7 * self.age))
        return None

    @property
    def effective_hr_rest(self) -> int:
        return self.hr_rest or DEFAULT_HR_REST

    @property
    def effective_lthr(self) -> int | None:
        if self.lthr:
            return self.lthr
        hr_max = self.effective_hr_max
        if hr_max:
            return int(round(hr_max * LTHR_FRACTION_OF_HR_MAX))
        return None


@dataclass(frozen=True)
class HalfSplit:
    """Aggregates for one half of an activity, used for aerobic decoupling."""

    avg_hr: float | None = None
    avg_power: float | None = None
    avg_speed_m_s: float | None = None


@dataclass(frozen=True)
class ActivitySummary:
    """One completed session, normalised away from any provider's schema."""

    sport: Sport
    start_date: date
    duration_s: int
    activity_id: str | None = None
    moving_time_s: int | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power: float | None = None
    normalized_power: float | None = None
    avg_cadence: float | None = None
    elevation_gain_m: float | None = None
    calories: float | None = None
    rpe: float | None = None
    total_strokes: int | None = None
    pool_length_m: float | None = None
    first_half: HalfSplit | None = None
    second_half: HalfSplit | None = None

    def __post_init__(self) -> None:
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self.rpe is not None and not 1 <= self.rpe <= 10:
            raise ValueError("rpe must be within 1..10")

    @property
    def duration_min(self) -> float:
        return self.duration_s / 60.0

    @property
    def active_time_s(self) -> int:
        return self.moving_time_s or self.duration_s

    @property
    def speed_m_s(self) -> float | None:
        if not self.distance_m:
            return None
        return self.distance_m / self.active_time_s

    @property
    def pace_s_per_km(self) -> float | None:
        speed = self.speed_m_s
        if not speed:
            return None
        return 1000.0 / speed

    @property
    def pace_s_per_100m(self) -> float | None:
        speed = self.speed_m_s
        if not speed:
            return None
        return 100.0 / speed

    @property
    def distance_per_stroke_m(self) -> float | None:
        if not self.distance_m or not self.total_strokes:
            return None
        return self.distance_m / self.total_strokes


@dataclass(frozen=True)
class DailyWellness:
    """Overnight and subjective inputs for one calendar day."""

    day: date
    hrv_rmssd_ms: float | None = None
    resting_hr: float | None = None
    sleep_total_min: float | None = None
    sleep_deep_min: float | None = None
    sleep_rem_min: float | None = None
    sleep_efficiency_pct: float | None = None
    body_weight_kg: float | None = None
    # Subjective wellness, Hooper-style, 1 = best and 5 = worst.
    soreness: int | None = None
    mood: int | None = None
    stress: int | None = None
    fatigue: int | None = None

    @property
    def ln_hrv(self) -> float | None:
        """ln(rMSSD). HRV is log-normally distributed; comparing raw rMSSD to a
        raw mean systematically over-weights high outliers."""
        if not self.hrv_rmssd_ms or self.hrv_rmssd_ms <= 0:
            return None
        return math.log(self.hrv_rmssd_ms)

    @property
    def hooper_index(self) -> float | None:
        parts = [p for p in (self.soreness, self.mood, self.stress, self.fatigue) if p is not None]
        if not parts:
            return None
        return sum(parts) / len(parts)


@dataclass(frozen=True)
class TrainingLoadResult:
    score: float
    source: LoadSource
    confidence: float
    detail: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadPoint:
    """Fitness / fatigue / form for one calendar day."""

    day: date
    load: float
    ctl: float
    atl: float

    @property
    def tsb(self) -> float:
        return self.ctl - self.atl


@dataclass(frozen=True)
class ReadinessResult:
    score: float
    band: ReadinessBand
    drivers: tuple[Driver, ...]
    data_quality: float


@dataclass(frozen=True)
class EfficiencyResult:
    metric: str
    value: float
    baseline: float | None = None
    delta_pct: float | None = None
    unit: str = ""

    @property
    def is_improvement(self) -> bool | None:
        """Higher is better for every efficiency metric here except SWOLF,
        which `efficiency.py` inverts before constructing the result."""
        if self.delta_pct is None:
            return None
        return self.delta_pct >= 0


@dataclass(frozen=True)
class RiskResult:
    probability: float
    band: RiskBand
    drivers: tuple[Driver, ...]
    model_version: str
    is_clinically_validated: bool = False


@dataclass(frozen=True)
class PredictionResult:
    metric: str
    value: float
    unit: str
    low: float | None = None
    high: float | None = None
    method: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class SessionPlan:
    day_offset: int
    sport: Sport
    title: str
    target_load: float
    duration_min: int
    intensity: str  # "recovery" | "easy" | "tempo" | "threshold" | "vo2max"
    is_key_session: bool = False
    notes: str = ""


@dataclass(frozen=True)
class WeekPlan:
    week_index: int
    phase: TrainingPhase
    target_load: float
    sessions: tuple[SessionPlan, ...]
    is_recovery_week: bool = False


@dataclass(frozen=True)
class TrainingPlan:
    weeks: tuple[WeekPlan, ...]
    primary_sport: Sport
    goal: Goal
    ramp_cap_pct: float

    @property
    def total_load(self) -> float:
        return sum(w.target_load for w in self.weeks)


@dataclass(frozen=True)
class AdaptedSession:
    action: Adaptation
    session: SessionPlan | None
    reason: str
