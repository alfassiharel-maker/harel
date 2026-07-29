"""Training ORM models for the Phase-1 subset of `database/migrations/0003`.

Only the athlete-owned profile tables are mapped here. Activities, wellness,
provider connections and streams arrive with the ingest work in Phase 1 weeks 3-4
(roadmap 1.6-1.10); mapping them now would be unused code that still has to be
kept in step with the schema.

`asdecimal=False` on every NUMERIC column is load-bearing rather than cosmetic:
the analytics engine in `backend/algorithms` is float-only by design (ADR-003, no
dependencies), and a `Decimal` arriving from the driver would raise `TypeError`
the first time it met a float - or worse, silently change rounding behaviour in a
physiology formula.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Date, ForeignKey, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, created_at_column, updated_at_column, uuid_pk

__all__ = [
    "GOAL_SPORTS",
    "GOAL_STATUSES",
    "GOAL_TYPES",
    "LEVELS",
    "PB_SOURCES",
    "PB_SPORTS",
    "SEXES",
    "AthleteGoal",
    "AthleteProfile",
    "PersonalBest",
]

SCHEMA = "training"

SEXES = ("male", "female", "unspecified")
LEVELS = ("beginner", "intermediate", "advanced")
GOAL_TYPES = ("race_time", "endurance", "strength", "weight_loss", "general_fitness")
GOAL_SPORTS = ("run", "bike", "swim", "strength", "triathlon", "other")
GOAL_STATUSES = ("active", "achieved", "abandoned", "expired")
PB_SPORTS = ("run", "bike", "swim", "other")
PB_SOURCES = ("race", "activity", "estimated", "self_reported")


class AthleteProfile(Base):
    """1:1 with `identity.users`. The user id *is* the primary key.

    Every threshold is nullable and stays nullable. A missing FTP is missing
    information, not zero: the engine falls back to a lower-trust load source and
    says so, which is the behaviour the whole analytics layer is built on.
    """

    __tablename__ = "athlete_profiles"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), primary_key=True
    )
    sex: Mapped[str] = mapped_column(Text, nullable=False, default="unspecified")
    # Date of birth, not age, so age is always current. Never sent to the model
    # layer as a date - only as a band (docs/05).
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 1, asdecimal=False))
    weight_kg: Mapped[float | None] = mapped_column(Numeric(5, 2, asdecimal=False))
    level: Mapped[str] = mapped_column(Text, nullable=False, default="intermediate")
    training_age_years: Mapped[float | None] = mapped_column(Numeric(4, 1, asdecimal=False))
    injuries_last_12m: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    hr_max: Mapped[int | None] = mapped_column(SmallInteger)
    hr_rest: Mapped[int | None] = mapped_column(SmallInteger)
    lthr: Mapped[int | None] = mapped_column(SmallInteger)
    # Cycling FTP and running threshold power are different physiological
    # quantities that happen to share a unit. Separate columns, never conflated.
    ftp_watts: Mapped[float | None] = mapped_column(Numeric(6, 1, asdecimal=False))
    run_threshold_power_w: Mapped[float | None] = mapped_column(Numeric(6, 1, asdecimal=False))
    threshold_pace_s_per_km: Mapped[float | None] = mapped_column(Numeric(6, 1, asdecimal=False))
    css_s_per_100m: Mapped[float | None] = mapped_column(Numeric(6, 1, asdecimal=False))
    vo2max: Mapped[float | None] = mapped_column(Numeric(4, 1, asdecimal=False))

    # `{"ftp_watts": "tested", "hr_max": "estimated", ...}`. The UI must be able
    # to show an athlete that a zone came from a guess rather than a test.
    threshold_sources: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    thresholds_updated_at: Mapped[dt.datetime | None] = mapped_column()
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = updated_at_column()


class AthleteGoal(Base):
    __tablename__ = "athlete_goals"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False
    )
    goal_type: Mapped[str] = mapped_column(Text, nullable=False)
    primary_sport: Mapped[str] = mapped_column(Text, nullable=False)
    target_distance_m: Mapped[float | None] = mapped_column(Numeric(10, 1, asdecimal=False))
    target_value: Mapped[float | None] = mapped_column(Numeric(12, 3, asdecimal=False))
    target_unit: Mapped[str | None] = mapped_column(Text)
    race_date: Mapped[dt.date | None] = mapped_column(Date)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = updated_at_column()


class PersonalBest(Base):
    """Feeds the per-athlete Riegel exponent, so `source` matters.

    A race best is trustworthy; a best segment lifted out of a training run is
    not, and the prediction models weight them differently.
    """

    __tablename__ = "personal_bests"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("identity.users.id", ondelete="CASCADE"), nullable=False
    )
    sport: Mapped[str] = mapped_column(Text, nullable=False)
    distance_m: Mapped[float] = mapped_column(Numeric(10, 1, asdecimal=False), nullable=False)
    time_s: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=False)
    achieved_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="activity")
    activity_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[dt.datetime] = created_at_column()
