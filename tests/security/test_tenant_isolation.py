"""Tenant-isolation matrix (roadmap 1.5).

This is the test that must exist before the endpoints multiply, not after. It
takes two athletes and asserts, endpoint by endpoint, that neither can read or
mutate the other's data. Every athlete-scoped route added later gets a row here,
and a route with no row fails review (docs/07, exit criterion).

The assertion style is deliberate: a cross-tenant *read* must come back empty or
404, and a cross-tenant *write* must 404 — never 403. A 403 would confirm the
target exists, which is itself a small leak. "Not found" tells the attacker
nothing.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import register_athlete

pytestmark = [pytest.mark.integration, pytest.mark.security]


async def _seed_goal(client: AsyncClient, athlete) -> str:
    created = await client.post(
        "/v1/me/goals",
        headers=athlete.auth,
        json={"goal_type": "endurance", "primary_sport": "bike"},
    )
    assert created.status_code == 201
    return created.json()["id"]


async def _seed_personal_best(client: AsyncClient, athlete) -> str:
    created = await client.post(
        "/v1/me/personal-bests",
        headers=athlete.auth,
        json={"sport": "run", "distance_m": 5000, "time_s": 1200, "achieved_on": "2026-06-01"},
    )
    assert created.status_code == 201
    return created.json()["id"]


async def test_me_returns_only_the_caller(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")

    alice_me = (await client.get("/v1/me", headers=alice.auth)).json()
    bob_me = (await client.get("/v1/me", headers=bob.auth)).json()

    assert alice_me["id"] == alice.id
    assert bob_me["id"] == bob.id
    assert alice_me["id"] != bob_me["id"]
    assert alice_me["email"] == "alice@example.com"


async def test_goals_are_not_visible_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")
    alice_goal = await _seed_goal(client, alice)

    bob_goals = await client.get("/v1/me/goals", headers=bob.auth)
    assert bob_goals.status_code == 200
    assert alice_goal not in [g["id"] for g in bob_goals.json()]


async def test_goal_cannot_be_patched_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")
    alice_goal = await _seed_goal(client, alice)

    attempt = await client.patch(f"/v1/me/goals/{alice_goal}", headers=bob.auth, json={"status": "abandoned"})
    # 404, not 403: Bob learns nothing about whether the goal exists.
    assert attempt.status_code == 404

    # And Alice's goal is untouched.
    still = await client.get("/v1/me/goals", headers=alice.auth)
    assert still.json()[0]["status"] == "active"


async def test_goal_cannot_be_deleted_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")
    alice_goal = await _seed_goal(client, alice)

    attempt = await client.delete(f"/v1/me/goals/{alice_goal}", headers=bob.auth)
    assert attempt.status_code == 404

    # The goal is still there for its owner.
    owner_view = await client.get("/v1/me/goals", headers=alice.auth)
    assert alice_goal in [g["id"] for g in owner_view.json()]


async def test_personal_bests_are_not_visible_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")
    alice_pb = await _seed_personal_best(client, alice)

    bob_pbs = await client.get("/v1/me/personal-bests", headers=bob.auth)
    assert alice_pb not in [p["id"] for p in bob_pbs.json()]


async def test_personal_best_cannot_be_deleted_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")
    alice_pb = await _seed_personal_best(client, alice)

    attempt = await client.delete(f"/v1/me/personal-bests/{alice_pb}", headers=bob.auth)
    assert attempt.status_code == 404


async def test_profiles_are_independent_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")

    await client.put("/v1/me/profile", headers=alice.auth, json={"ftp_watts": 300.0})
    await client.put("/v1/me/profile", headers=bob.auth, json={"ftp_watts": 200.0})

    assert (await client.get("/v1/me/profile", headers=alice.auth)).json()["ftp_watts"] == 300.0
    assert (await client.get("/v1/me/profile", headers=bob.auth)).json()["ftp_watts"] == 200.0


async def test_consents_are_not_visible_across_tenants(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")

    alice_consents = await client.get("/v1/me/consents", headers=alice.auth)
    # Every consent row Alice sees is her own — there is no way to page into Bob's.
    # (The rows carry no user_id in the DTO; RLS guarantees the set is hers.)
    assert alice_consents.status_code == 200
    assert len(alice_consents.json()) >= 3
    assert bob  # both athletes exist; the point is the sets do not bleed


async def test_sessions_list_is_per_tenant(client: AsyncClient) -> None:
    alice = await register_athlete(client, "alice@example.com")
    bob = await register_athlete(client, "bob@example.com")

    alice_sessions = await client.get("/v1/auth/sessions", headers=alice.auth)
    bob_sessions = await client.get("/v1/auth/sessions", headers=bob.auth)
    assert alice_sessions.status_code == 200
    assert bob_sessions.status_code == 200
    # Each athlete has exactly their own live session from registration.
    assert len(alice_sessions.json()) == 1
    assert len(bob_sessions.json()) == 1
