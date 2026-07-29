"""Deterministic analytics engine for the AI Sports Coach platform.

Dependency-free by design (Python standard library only). This package is the
product's core intellectual property and the single source of truth for every
number the app shows or the AI layer reasons over.

Layering rule, enforced by review: **nothing in this package may import the API,
the ORM, an HTTP client, or the AI layer.** Data comes in as plain dataclasses
and results go out as plain dataclasses. That keeps the engine unit-testable
without a database, reusable from the batch worker and the ML training scripts,
and portable if a hot path ever needs reimplementing.

Public surface:

    training_load.training_load(activity, profile) -> TrainingLoadResult
    training_load.fitness_fatigue(loads, start, end) -> [LoadPoint]   # CTL/ATL/TSB
    training_load.acwr(loads, ref_day)              -> AcwrResult
    recovery.readiness(profile, wellness, loads, ref_day) -> ReadinessResult
    efficiency.session_efficiency(activity, profile, baseline) -> [EfficiencyResult]
    injury_risk.assess(profile, loads, wellness, ref_day) -> RiskResult
    performance.equivalent_times(distance, time, ...)  -> [PredictionResult]
    performance.progression_forecast(history, horizon) -> PredictionResult
    plan.generate_plan(request, profile)              -> TrainingPlan
    plan.adapt_today(session, readiness, risk)        -> AdaptedSession

See docs/04-analytics-algorithms.md for the formulas, their sources, and the
evidence caveats attached to each.
"""

from __future__ import annotations

from . import efficiency, injury_risk, performance, plan, recovery, stats, training_load, zones
from .types import (
    ActivitySummary,
    Adaptation,
    AdaptedSession,
    AthleteProfile,
    DailyWellness,
    Driver,
    EfficiencyResult,
    Goal,
    HalfSplit,
    Level,
    LoadPoint,
    LoadSource,
    PredictionResult,
    ReadinessBand,
    ReadinessResult,
    RiskBand,
    RiskResult,
    SessionPlan,
    Sex,
    Sport,
    TrainingLoadResult,
    TrainingPhase,
    TrainingPlan,
    WeekPlan,
)

__version__ = "0.1.0"

__all__ = [
    "ActivitySummary",
    # types
    "Adaptation",
    "AdaptedSession",
    "AthleteProfile",
    "DailyWellness",
    "Driver",
    "EfficiencyResult",
    "Goal",
    "HalfSplit",
    "Level",
    "LoadPoint",
    "LoadSource",
    "PredictionResult",
    "ReadinessBand",
    "ReadinessResult",
    "RiskBand",
    "RiskResult",
    "SessionPlan",
    "Sex",
    "Sport",
    "TrainingLoadResult",
    "TrainingPhase",
    "TrainingPlan",
    "WeekPlan",
    "__version__",
    "efficiency",
    "injury_risk",
    "performance",
    "plan",
    "recovery",
    # modules
    "stats",
    "training_load",
    "zones",
]
