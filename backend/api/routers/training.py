"""Athlete profile, goals, personal bests and zones (docs/03 §3).

These live under `/v1/me` alongside the identity routes because to the athlete
they are one thing — "my account" — even though they are owned by a different
module. FastAPI composes the two routers under the same prefix; the paths do not
overlap.

The profile is a mutable resource, so it carries an `ETag` and honours
`If-Match`. The tag is set on every profile response so a client always has a
fresh validator to send back on the next write. The comparison itself happens
inside the service's transaction, not here — checking it in the transport layer
would reintroduce the lost-update race the header exists to close.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status

from backend.api.deps import CurrentPrincipal, get_training_service
from backend.core.etag import etag_of
from backend.modules.training import (
    AthleteProfileOut,
    AthleteProfileUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
    PersonalBestCreate,
    PersonalBestOut,
    TrainingService,
    ZoneSetOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/me", tags=["training"])

TrainingDep = Annotated[TrainingService, Depends(get_training_service)]


# -- Profile ------------------------------------------------------------------


@router.get("/profile")
async def get_profile(
    principal: CurrentPrincipal, service: TrainingDep, response: Response
) -> AthleteProfileOut:
    profile = await service.get_profile(principal)
    response.headers["ETag"] = etag_of(profile)
    return profile


@router.put("/profile")
async def replace_profile(
    payload: AthleteProfileUpdate,
    principal: CurrentPrincipal,
    service: TrainingDep,
    response: Response,
    # Optional rather than mandatory: the profile is created on first write, and
    # there is nothing to match on that first request. When the client does send a
    # tag it is enforced (412 on mismatch), which is what protects a threshold edit
    # from a concurrent overwrite by another device.
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AthleteProfileOut:
    profile = await service.replace_profile(principal, payload, if_match=if_match)
    response.headers["ETag"] = etag_of(profile)
    return profile


@router.get("/zones")
async def get_zones(
    principal: CurrentPrincipal,
    service: TrainingDep,
    sport: Annotated[Literal["run", "bike", "swim", "strength", "other"], Query()] = "run",
) -> ZoneSetOut:
    return await service.get_zones(principal, sport)


# -- Goals --------------------------------------------------------------------


@router.get("/goals")
async def list_goals(
    principal: CurrentPrincipal,
    service: TrainingDep,
    goal_status: Annotated[
        Literal["active", "achieved", "abandoned", "expired"] | None, Query(alias="status")
    ] = None,
) -> list[GoalOut]:
    return await service.list_goals(principal, status=goal_status)


@router.post("/goals", status_code=status.HTTP_201_CREATED)
async def create_goal(payload: GoalCreate, principal: CurrentPrincipal, service: TrainingDep) -> GoalOut:
    return await service.create_goal(principal, payload)


@router.patch("/goals/{goal_id}")
async def update_goal(
    goal_id: uuid.UUID, payload: GoalUpdate, principal: CurrentPrincipal, service: TrainingDep
) -> GoalOut:
    return await service.update_goal(principal, goal_id, payload)


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(goal_id: uuid.UUID, principal: CurrentPrincipal, service: TrainingDep) -> Response:
    await service.delete_goal(principal, goal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- Personal bests -----------------------------------------------------------


@router.get("/personal-bests")
async def list_personal_bests(
    principal: CurrentPrincipal,
    service: TrainingDep,
    sport: Annotated[Literal["run", "bike", "swim", "other"] | None, Query()] = None,
) -> list[PersonalBestOut]:
    return await service.list_personal_bests(principal, sport=sport)


@router.post("/personal-bests", status_code=status.HTTP_201_CREATED)
async def add_personal_best(
    payload: PersonalBestCreate, principal: CurrentPrincipal, service: TrainingDep
) -> PersonalBestOut:
    return await service.add_personal_best(principal, payload)


@router.delete("/personal-bests/{best_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_best(
    best_id: uuid.UUID, principal: CurrentPrincipal, service: TrainingDep
) -> Response:
    await service.delete_personal_best(principal, best_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
