"""Training service — profile, goals, personal bests, computed zones.

The Phase-1 slice of the training module (roadmap 1.4). It is the module that
owns the athlete's physiological anchors, which makes it the module every derived
metric depends on: a wrong FTP here is a wrong training load for every cycling
session the athlete has ever recorded.

Two rules show up repeatedly below and are worth stating once:

* **Missing is `None`, never `0`.** A cleared threshold is unknown, and the
  analytics engine is built to say "I cannot compute this" rather than to produce
  a plausible number from a default.
* **Concurrency is checked inside the transaction.** `If-Match` is compared
  against the row read in the same transaction as the write. Comparing in the
  transport layer and then calling the service would be a time-of-check /
  time-of-use race, which is exactly the lost update the header exists to stop.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from backend.algorithms import types as algo
from backend.algorithms.zones import default_zones_for_sport, power_zones
from backend.core.context import Principal
from backend.core.errors import InsufficientData, NotFound, PreconditionFailed, ValidationFailed
from backend.core.etag import etag_matches, etag_of
from backend.core.ids import uuid7
from backend.database.session import Database
from backend.modules.training.models import AthleteGoal, AthleteProfile, PersonalBest
from backend.modules.training.repository import TrainingRepository
from backend.modules.training.schemas import (
    AthleteProfileOut,
    AthleteProfileUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
    PersonalBestCreate,
    PersonalBestOut,
    ZoneOut,
    ZoneSetOut,
)

__all__ = ["TrainingService"]

# Columns that describe a physiological anchor. Writing any of them stamps
# `thresholds_updated_at`, which the UI uses to nag about a stale FTP — an FTP
# from eight months ago is not the athlete's FTP.
_THRESHOLD_FIELDS = frozenset(
    {
        "hr_max",
        "hr_rest",
        "lthr",
        "ftp_watts",
        "run_threshold_power_w",
        "threshold_pace_s_per_km",
        "css_s_per_100m",
        "vo2max",
    }
)

ZoneSport = Literal["run", "bike", "swim", "strength", "other"]


class TrainingService:
    def __init__(self, *, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------ profile

    async def get_profile(self, principal: Principal) -> AthleteProfileOut:
        """The athlete's profile, or an all-unknown default if none exists yet.

        A freshly registered athlete has no profile row — the row is created on
        first write. Returning 404 would make the client handle "you have not
        filled this in" as an error condition, so instead the unset state is
        represented honestly: every threshold `None`, and `updated_at` `None` to
        say "never saved". No row is written, because a `GET` must not.
        """
        async with self._db.session(principal) as session:
            row = await TrainingRepository(session).get_profile(principal.user_id)
            return self._to_profile_out(row)

    async def replace_profile(
        self,
        principal: Principal,
        update: AthleteProfileUpdate,
        *,
        if_match: str | None,
    ) -> AthleteProfileOut:
        """Write the profile fields the client actually sent.

        Absent fields are left alone; fields explicitly set to `null` are cleared.
        `model_fields_set` is what distinguishes the two, and without it an athlete
        who edits their weight in one screen would wipe the thresholds another
        screen had set.
        """
        supplied = update.model_fields_set
        if not supplied:
            raise ValidationFailed("No profile fields supplied.")

        values: dict[str, object] = {name: getattr(update, name) for name in sorted(supplied)}

        async with self._db.session(principal) as session:
            repo = TrainingRepository(session)
            current = await repo.get_profile(principal.user_id)

            if if_match is not None and not etag_matches(if_match, etag_of(self._to_profile_out(current))):
                raise PreconditionFailed(
                    "This profile was changed by another device. Reload and try again.",
                    meta={"reason": "etag_mismatch"},
                )

            self._check_merged_invariants(current, values)

            if any(field in values for field in _THRESHOLD_FIELDS):
                values["thresholds_updated_at"] = dt.datetime.now(dt.UTC)

            row = await repo.upsert_profile(principal.user_id, values)
            return self._to_profile_out(row)

    @staticmethod
    def _check_merged_invariants(current: AthleteProfile | None, values: dict[str, object]) -> None:
        """Validate the row as it will be *after* the merge.

        The DTO can only see the fields in one request. `hr_max` alone is valid;
        `hr_max = 150` against a stored `hr_rest = 160` is not, and only the merged
        view can tell. The database `CHECK` would also reject it, but as an
        `IntegrityError` that the API can honestly report only as a 500.
        """

        def merged(field: str) -> object:
            if field in values:
                return values[field]
            return getattr(current, field) if current is not None else None

        hr_max, hr_rest = merged("hr_max"), merged("hr_rest")
        if isinstance(hr_max, int) and isinstance(hr_rest, int) and hr_max <= hr_rest:
            raise ValidationFailed("hr_max must be greater than hr_rest.")

    # -------------------------------------------------------------------- zones

    async def get_zones(self, principal: Principal, sport: ZoneSport) -> ZoneSetOut:
        """Computed training zones, with the anchor they came from.

        Refuses rather than guessing. An athlete with no HRmax, no age and no
        threshold has no zones — showing them bands derived from a silent default
        would have them training to a number nobody measured.
        """
        profile = await self.get_profile(principal)
        engine_profile = self._to_engine_profile(profile)
        sources = profile.threshold_sources

        if sport == "bike" and profile.ftp_watts:
            zones = power_zones(profile.ftp_watts)
            return self._zone_set(
                sport=sport,
                kind="power",
                anchor="ftp",
                anchor_value=profile.ftp_watts,
                anchor_source=sources.get("ftp_watts", "self_reported"),
                zones=zones,
            )

        zones = default_zones_for_sport(engine_profile, self._engine_sport(sport))
        if not zones:
            raise InsufficientData(
                "Zones need at least one of: measured HRmax, lactate threshold HR, "
                "or a birth date to estimate HRmax from.",
                meta={"missing": ["hr_max", "lthr", "birth_date"]},
            )

        anchor = zones[0].anchor
        anchor_value, anchor_source = self._anchor_details(anchor, profile, engine_profile)
        kind: Literal["heart_rate", "power", "pace"] = "pace" if anchor == "threshold_pace" else "heart_rate"
        return self._zone_set(
            sport=sport,
            kind=kind,
            anchor=anchor,
            anchor_value=anchor_value,
            anchor_source=anchor_source,
            zones=zones,
        )

    @staticmethod
    def _anchor_details(
        anchor: str, profile: AthleteProfileOut, engine_profile: algo.AthleteProfile
    ) -> tuple[float, str]:
        sources = profile.threshold_sources
        if anchor == "lthr":
            return float(engine_profile.lthr or 0), sources.get("lthr", "self_reported")
        if anchor == "threshold_pace":
            return (
                float(profile.threshold_pace_s_per_km or 0),
                sources.get("threshold_pace_s_per_km", "self_reported"),
            )
        # `hr_max` bands. When the profile carries no measured HRmax the engine has
        # fallen back to the Tanaka age estimate, and the athlete must be told.
        effective = engine_profile.effective_hr_max or 0
        source = "estimated" if profile.hr_max is None else sources.get("hr_max", "self_reported")
        return float(effective), source

    @staticmethod
    def _zone_set(
        *,
        sport: ZoneSport,
        kind: Literal["heart_rate", "power", "pace"],
        anchor: str,
        anchor_value: float,
        anchor_source: str,
        zones: tuple[object, ...],
    ) -> ZoneSetOut:
        return ZoneSetOut(
            sport=sport,
            kind=kind,
            anchor=anchor,
            anchor_value=round(anchor_value, 1),
            anchor_source=anchor_source,
            zones=[
                ZoneOut(
                    index=zone.index,  # type: ignore[attr-defined]
                    name=zone.name,  # type: ignore[attr-defined]
                    low=round(zone.low, 1),  # type: ignore[attr-defined]
                    high=zone.high,  # type: ignore[attr-defined]
                    anchor=zone.anchor,  # type: ignore[attr-defined]
                )
                for zone in zones
            ],
        )

    @staticmethod
    def _engine_sport(sport: ZoneSport) -> algo.Sport:
        return {
            "run": algo.Sport.RUN,
            "bike": algo.Sport.BIKE,
            "swim": algo.Sport.SWIM,
            "strength": algo.Sport.STRENGTH,
            "other": algo.Sport.OTHER,
        }[sport]

    # -------------------------------------------------------------------- goals

    async def create_goal(self, principal: Principal, payload: GoalCreate) -> GoalOut:
        async with self._db.session(principal) as session:
            goal = await TrainingRepository(session).insert_goal(
                AthleteGoal(
                    id=uuid7(),
                    user_id=principal.user_id,
                    goal_type=payload.goal_type,
                    primary_sport=payload.primary_sport,
                    target_distance_m=payload.target_distance_m,
                    target_value=payload.target_value,
                    target_unit=payload.target_unit,
                    race_date=payload.race_date,
                    priority=payload.priority,
                    status="active",
                )
            )
            await session.refresh(goal)
            return GoalOut.model_validate(goal)

    async def list_goals(self, principal: Principal, *, status: str | None = None) -> list[GoalOut]:
        async with self._db.session(principal) as session:
            rows = await TrainingRepository(session).list_goals(principal.user_id, status=status)
            return [GoalOut.model_validate(row) for row in rows]

    async def update_goal(self, principal: Principal, goal_id: uuid.UUID, payload: GoalUpdate) -> GoalOut:
        values = {name: getattr(payload, name) for name in sorted(payload.model_fields_set)}
        if not values:
            raise ValidationFailed("No fields to update.")
        async with self._db.session(principal) as session:
            repo = TrainingRepository(session)
            if not await repo.update_goal(principal.user_id, goal_id, values):
                # Either the goal does not exist or it belongs to someone else.
                # The same 404 for both: distinguishing them confirms the existence
                # of another athlete's row.
                raise NotFound("Goal not found.")
            goal = await repo.get_goal(principal.user_id, goal_id)
            if goal is None:
                raise NotFound("Goal not found.")
            return GoalOut.model_validate(goal)

    async def delete_goal(self, principal: Principal, goal_id: uuid.UUID) -> None:
        async with self._db.session(principal) as session:
            if not await TrainingRepository(session).delete_goal(principal.user_id, goal_id):
                raise NotFound("Goal not found.")

    # ----------------------------------------------------------- personal bests

    async def add_personal_best(self, principal: Principal, payload: PersonalBestCreate) -> PersonalBestOut:
        async with self._db.session(principal) as session:
            row = await TrainingRepository(session).upsert_personal_best(
                PersonalBest(
                    id=uuid7(),
                    user_id=principal.user_id,
                    sport=payload.sport,
                    distance_m=payload.distance_m,
                    time_s=payload.time_s,
                    achieved_on=payload.achieved_on,
                    source=payload.source,
                    activity_id=None,
                )
            )
            await session.refresh(row)
            return PersonalBestOut.model_validate(row)

    async def list_personal_bests(
        self, principal: Principal, *, sport: str | None = None
    ) -> list[PersonalBestOut]:
        async with self._db.session(principal) as session:
            rows = await TrainingRepository(session).list_personal_bests(principal.user_id, sport=sport)
            return [PersonalBestOut.model_validate(row) for row in rows]

    async def delete_personal_best(self, principal: Principal, best_id: uuid.UUID) -> None:
        async with self._db.session(principal) as session:
            if not await TrainingRepository(session).delete_personal_best(principal.user_id, best_id):
                raise NotFound("Personal best not found.")

    # -------------------------------------------------------------- serialising

    @staticmethod
    def _to_profile_out(row: AthleteProfile | None) -> AthleteProfileOut:
        if row is None:
            # The never-saved state. Defaults match the column defaults in
            # migration 0003 so this representation is what a first write produces.
            return AthleteProfileOut(
                sex="unspecified",
                birth_date=None,
                height_cm=None,
                weight_kg=None,
                level="intermediate",
                training_age_years=None,
                injuries_last_12m=0,
                hr_max=None,
                hr_rest=None,
                lthr=None,
                ftp_watts=None,
                run_threshold_power_w=None,
                threshold_pace_s_per_km=None,
                css_s_per_100m=None,
                vo2max=None,
                threshold_sources={},
                thresholds_updated_at=None,
                updated_at=None,
            )
        return AthleteProfileOut.model_validate(row)

    @staticmethod
    def _to_engine_profile(profile: AthleteProfileOut) -> algo.AthleteProfile:
        """Map the stored profile onto the analytics engine's own type.

        The engine takes `age`, not a birth date: it is a pure-function library
        with no clock, so the caller owns "now". Deriving age here also keeps the
        date of birth out of every downstream layer.
        """
        age: int | None = None
        if profile.birth_date is not None:
            today = dt.datetime.now(dt.UTC).date()
            age = int((today - profile.birth_date).days // 365)

        return algo.AthleteProfile(
            sex=algo.Sex(profile.sex),
            age=age,
            weight_kg=profile.weight_kg,
            height_cm=profile.height_cm,
            hr_max=profile.hr_max,
            hr_rest=profile.hr_rest,
            lthr=profile.lthr,
            ftp_watts=profile.ftp_watts,
            run_threshold_power_w=profile.run_threshold_power_w,
            threshold_pace_s_per_km=profile.threshold_pace_s_per_km,
            css_s_per_100m=profile.css_s_per_100m,
            training_age_years=profile.training_age_years,
            injuries_last_12m=profile.injuries_last_12m,
            level=algo.Level(profile.level),
        )
