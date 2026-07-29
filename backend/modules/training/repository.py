"""All SQL for the training module.

Every method takes a session whose RLS context is already established, and every
predicate still names `user_id` explicitly. The redundancy is deliberate: RLS is
the backstop for a mistake in this file, so this file must not lean on it
(docs/06 §3).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.training.models import AthleteGoal, AthleteProfile, PersonalBest

__all__ = ["TrainingRepository"]


class TrainingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Profile --------------------------------------------------------------

    async def get_profile(self, user_id: uuid.UUID) -> AthleteProfile | None:
        result = await self._session.execute(select(AthleteProfile).where(AthleteProfile.user_id == user_id))
        return result.scalar_one_or_none()

    async def upsert_profile(self, user_id: uuid.UUID, values: dict[str, object]) -> AthleteProfile:
        """Create the profile row or update the named columns.

        `ON CONFLICT DO UPDATE` rather than select-then-branch: the profile row is
        created lazily on first write, and two concurrent first writes (the mobile
        app and the web app during onboarding) would otherwise race into a
        primary-key violation.

        Only the keys present in `values` are written, so a caller updating weight
        cannot blank a threshold it never mentioned.
        """
        statement = (
            pg_insert(AthleteProfile)
            .values(user_id=user_id, **values)
            .on_conflict_do_update(
                index_elements=[AthleteProfile.user_id],
                set_=values,
                # Belt and braces. The RLS policy already restricts the row to the
                # caller; this makes the update a no-op rather than a cross-tenant
                # write if the policy is ever loosened.
                where=AthleteProfile.user_id == user_id,
            )
            .returning(AthleteProfile)
        )
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def touch_thresholds(self, user_id: uuid.UUID, when: dt.datetime) -> None:
        await self._session.execute(
            update(AthleteProfile).where(AthleteProfile.user_id == user_id).values(thresholds_updated_at=when)
        )

    # -- Goals ----------------------------------------------------------------

    async def insert_goal(self, goal: AthleteGoal) -> AthleteGoal:
        self._session.add(goal)
        await self._session.flush()
        return goal

    async def list_goals(self, user_id: uuid.UUID, *, status: str | None = None) -> list[AthleteGoal]:
        query = select(AthleteGoal).where(AthleteGoal.user_id == user_id)
        if status is not None:
            query = query.where(AthleteGoal.status == status)
        # Matches the (user_id, status, priority) index, and puts an A-race first.
        query = query.order_by(AthleteGoal.priority, AthleteGoal.created_at.desc())
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_goal(self, user_id: uuid.UUID, goal_id: uuid.UUID) -> AthleteGoal | None:
        result = await self._session.execute(
            select(AthleteGoal).where(AthleteGoal.id == goal_id, AthleteGoal.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_goal(self, user_id: uuid.UUID, goal_id: uuid.UUID, values: dict[str, object]) -> bool:
        if not values:
            return False
        result = await self._session.execute(
            update(AthleteGoal)
            .where(AthleteGoal.id == goal_id, AthleteGoal.user_id == user_id)
            .values(**values)
        )
        return (result.rowcount or 0) == 1

    async def delete_goal(self, user_id: uuid.UUID, goal_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(AthleteGoal).where(AthleteGoal.id == goal_id, AthleteGoal.user_id == user_id)
        )
        return (result.rowcount or 0) == 1

    # -- Personal bests -------------------------------------------------------

    async def upsert_personal_best(self, best: PersonalBest) -> PersonalBest:
        """Insert, or keep the faster time for the same day and distance.

        The table's unique key is `(user_id, sport, distance_m, achieved_on)`. A
        re-submitted best for the same day should not 409 at the athlete — but it
        must never make a personal best *slower*, because the fitted Riegel
        exponent is sensitive to the tail of the distance/time curve.
        """
        statement = (
            pg_insert(PersonalBest)
            .values(
                id=best.id,
                user_id=best.user_id,
                sport=best.sport,
                distance_m=best.distance_m,
                time_s=best.time_s,
                achieved_on=best.achieved_on,
                source=best.source,
                activity_id=best.activity_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    PersonalBest.user_id,
                    PersonalBest.sport,
                    PersonalBest.distance_m,
                    PersonalBest.achieved_on,
                ],
                set_={"time_s": best.time_s, "source": best.source},
                where=PersonalBest.time_s > best.time_s,
            )
            .returning(PersonalBest)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        # `DO UPDATE ... WHERE` skipped the row: the stored best is already at
        # least as fast. Return what is stored rather than inventing a result.
        existing = await self._session.execute(
            select(PersonalBest).where(
                PersonalBest.user_id == best.user_id,
                PersonalBest.sport == best.sport,
                PersonalBest.distance_m == best.distance_m,
                PersonalBest.achieved_on == best.achieved_on,
            )
        )
        return existing.scalar_one()

    async def list_personal_bests(
        self, user_id: uuid.UUID, *, sport: str | None = None
    ) -> list[PersonalBest]:
        query = select(PersonalBest).where(PersonalBest.user_id == user_id)
        if sport is not None:
            query = query.where(PersonalBest.sport == sport)
        result = await self._session.execute(query.order_by(PersonalBest.sport, PersonalBest.distance_m))
        return list(result.scalars().all())

    async def delete_personal_best(self, user_id: uuid.UUID, best_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(PersonalBest).where(PersonalBest.id == best_id, PersonalBest.user_id == user_id)
        )
        return (result.rowcount or 0) == 1
