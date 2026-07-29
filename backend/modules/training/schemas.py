"""Training DTOs.

Numeric bounds mirror the `CHECK` constraints in `database/migrations/0003`
exactly. That duplication is intentional: without it a typo (`hr_max: 1800`)
reaches the database and comes back as an `IntegrityError`, which the API can
only honestly render as a 500. With it the athlete gets a 422 naming the field.

The database keeps the constraints regardless — the schema is the last line of
defence, not the first, and a future worker or SQL script writing the same table
does not go through Pydantic.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

__all__ = [
    "THRESHOLD_SOURCE_VALUES",
    "AthleteProfileOut",
    "AthleteProfileUpdate",
    "GoalCreate",
    "GoalOut",
    "GoalUpdate",
    "PersonalBestCreate",
    "PersonalBestOut",
    "ZoneOut",
    "ZoneSetOut",
]

# How a threshold came to be known. `tested` means a protocol was performed,
# `estimated` means we derived it from something else — the difference decides
# whether the UI may present a zone as fact.
THRESHOLD_SOURCE_VALUES = ("tested", "estimated", "self_reported", "provider")

Sex = Literal["male", "female", "unspecified"]
Level = Literal["beginner", "intermediate", "advanced"]
GoalType = Literal["race_time", "endurance", "strength", "weight_loss", "general_fitness"]
GoalSport = Literal["run", "bike", "swim", "strength", "triathlon", "other"]
GoalStatus = Literal["active", "achieved", "abandoned", "expired"]
PbSport = Literal["run", "bike", "swim", "other"]
PbSource = Literal["race", "activity", "estimated", "self_reported"]

HeightCm = Annotated[float, Field(ge=80, le=260)]
WeightKg = Annotated[float, Field(ge=25, le=350)]
TrainingAgeYears = Annotated[float, Field(ge=0, le=80)]
HrMax = Annotated[int, Field(ge=100, le=240)]
HrRest = Annotated[int, Field(ge=25, le=120)]
Lthr = Annotated[int, Field(ge=80, le=230)]
FtpWatts = Annotated[float, Field(ge=30, le=800)]
RunThresholdPowerW = Annotated[float, Field(ge=50, le=800)]
ThresholdPace = Annotated[float, Field(ge=120, le=900)]
CssPer100m = Annotated[float, Field(ge=40, le=300)]
Vo2Max = Annotated[float, Field(ge=15, le=100)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AthleteProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sex: Sex
    birth_date: dt.date | None
    height_cm: float | None
    weight_kg: float | None
    level: Level
    training_age_years: float | None
    injuries_last_12m: int
    hr_max: int | None
    hr_rest: int | None
    lthr: int | None
    ftp_watts: float | None
    run_threshold_power_w: float | None
    threshold_pace_s_per_km: float | None
    css_s_per_100m: float | None
    vo2max: float | None
    threshold_sources: dict[str, str]
    thresholds_updated_at: dt.datetime | None
    # Null until the athlete saves for the first time: the profile row is created
    # lazily, so "never updated" is a real state, not zero.
    updated_at: dt.datetime | None


class AthleteProfileUpdate(_Strict):
    """Full replacement (`PUT`), so every field is optional and `None` means clear.

    `PUT` rather than `PATCH` is why `model_fields_set` is consulted in the
    service: with a replacement, "field absent" and "field explicitly null" have
    to be distinguishable, or an athlete editing their weight would silently wipe
    their FTP.
    """

    sex: Sex | None = None
    birth_date: dt.date | None = None
    height_cm: HeightCm | None = None
    weight_kg: WeightKg | None = None
    level: Level | None = None
    training_age_years: TrainingAgeYears | None = None
    injuries_last_12m: Annotated[int, Field(ge=0, le=50)] | None = None
    hr_max: HrMax | None = None
    hr_rest: HrRest | None = None
    lthr: Lthr | None = None
    ftp_watts: FtpWatts | None = None
    run_threshold_power_w: RunThresholdPowerW | None = None
    threshold_pace_s_per_km: ThresholdPace | None = None
    css_s_per_100m: CssPer100m | None = None
    vo2max: Vo2Max | None = None
    threshold_sources: dict[str, str] | None = None

    @field_validator("birth_date")
    @classmethod
    def _plausible_birth_date(cls, value: dt.date | None) -> dt.date | None:
        if value is None:
            return value
        today = dt.datetime.now(dt.UTC).date()
        age = (today - value).days / 365.25
        # Under 13 is a COPPA/GDPR-minors problem the product does not handle, and
        # over 110 is a typo. Both would otherwise flow into the Tanaka HRmax
        # estimate and produce zones from nonsense.
        if not 13 <= age <= 110:
            raise ValueError("birth_date must give an age between 13 and 110")
        return value

    @field_validator("threshold_sources")
    @classmethod
    def _known_sources(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        bad = sorted({v for v in value.values() if v not in THRESHOLD_SOURCE_VALUES})
        if bad:
            raise ValueError(f"unknown threshold source values: {', '.join(bad)}")
        return value

    @model_validator(mode="after")
    def _hr_ordering(self) -> AthleteProfileUpdate:
        # Mirrors the table's CHECK (hr_max > hr_rest). Only enforced when both
        # arrive together; a partial write that would break the invariant is
        # caught by the service, which sees the merged row.
        if self.hr_max is not None and self.hr_rest is not None and self.hr_max <= self.hr_rest:
            raise ValueError("hr_max must be greater than hr_rest")
        return self


class GoalCreate(_Strict):
    goal_type: GoalType
    primary_sport: GoalSport
    target_distance_m: Annotated[float, Field(gt=0, le=1_000_000)] | None = None
    target_value: Annotated[float, Field(gt=0)] | None = None
    target_unit: str | None = Field(default=None, max_length=32)
    race_date: dt.date | None = None
    priority: Annotated[int, Field(ge=1, le=3)] = 1

    @model_validator(mode="after")
    def _race_time_needs_a_target(self) -> GoalCreate:
        # A race-time goal with no target is not a goal, and the plan generator
        # would have nothing to periodise towards.
        if self.goal_type == "race_time" and (self.target_value is None or self.race_date is None):
            raise ValueError("a race_time goal needs both target_value and race_date")
        return self


class GoalUpdate(_Strict):
    target_distance_m: Annotated[float, Field(gt=0, le=1_000_000)] | None = None
    target_value: Annotated[float, Field(gt=0)] | None = None
    target_unit: str | None = Field(default=None, max_length=32)
    race_date: dt.date | None = None
    priority: Annotated[int, Field(ge=1, le=3)] | None = None
    status: GoalStatus | None = None


class GoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_type: GoalType
    primary_sport: GoalSport
    target_distance_m: float | None
    target_value: float | None
    target_unit: str | None
    race_date: dt.date | None
    priority: int
    status: GoalStatus
    created_at: dt.datetime
    updated_at: dt.datetime


class PersonalBestCreate(_Strict):
    sport: PbSport
    distance_m: Annotated[float, Field(gt=0, le=1_000_000)]
    time_s: Annotated[float, Field(gt=0, le=1_000_000)]
    achieved_on: dt.date
    source: PbSource = "self_reported"

    @model_validator(mode="after")
    def _not_in_the_future(self) -> PersonalBestCreate:
        if self.achieved_on > dt.datetime.now(dt.UTC).date():
            raise ValueError("achieved_on cannot be in the future")
        return self

    @model_validator(mode="after")
    def _physically_possible(self) -> PersonalBestCreate:
        # Sanity gate, not a record book: a 3 m/s swim or a 15 m/s run is a data
        # entry error, and a fabricated best would bias the fitted Riegel
        # exponent for every subsequent prediction.
        ceiling_m_s = {"run": 13.0, "bike": 30.0, "swim": 3.0, "other": 30.0}[self.sport]
        if self.distance_m / self.time_s > ceiling_m_s:
            raise ValueError(f"implausible speed for {self.sport}")
        return self


class PersonalBestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sport: PbSport
    distance_m: float
    time_s: float
    achieved_on: dt.date
    source: PbSource
    created_at: dt.datetime


class ZoneOut(BaseModel):
    index: int
    name: str
    low: float
    # The top band is open-ended. `None` on the wire rather than a made-up
    # ceiling, and never `Infinity` — that is not valid JSON (RFC 8259 §6) and
    # several clients parse it as `null` or throw.
    high: float | None
    anchor: str

    @field_serializer("high")
    def _serialise_high(self, value: float | None) -> float | None:
        if value is None or math.isinf(value):
            return None
        return value


class ZoneSetOut(BaseModel):
    """Zones plus the anchor they were derived from, and how trusted it is.

    `anchor_source` is not decoration. An athlete shown zones built on an
    age-estimated HRmax will otherwise train to a number that is a guess, and has
    no way of knowing (docs/03 §3).
    """

    sport: Literal["run", "bike", "swim", "strength", "other"]
    kind: Literal["heart_rate", "power", "pace"]
    anchor: str
    anchor_value: float
    anchor_source: str
    zones: list[ZoneOut]
