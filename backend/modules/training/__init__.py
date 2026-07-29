"""Training module - athlete profile, goals, personal bests, computed zones.

Owns the `training` Postgres schema. This Phase-1 slice covers the athlete-owned
profile tables only; activities, wellness and provider ingest follow in weeks 3-4
(roadmap 1.6-1.10).

Public surface is the service and its DTOs. `models` and `repository` are private
and the import-linter contract fails the build on a direct import of either.
"""

from backend.modules.training.schemas import (
    AthleteProfileOut,
    AthleteProfileUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
    PersonalBestCreate,
    PersonalBestOut,
    ZoneSetOut,
)
from backend.modules.training.service import TrainingService

__all__ = [
    "AthleteProfileOut",
    "AthleteProfileUpdate",
    "GoalCreate",
    "GoalOut",
    "GoalUpdate",
    "PersonalBestCreate",
    "PersonalBestOut",
    "TrainingService",
    "ZoneSetOut",
]
