"""Athlete profile, zones, goals and personal bests (roadmap 1.4)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import register_athlete

pytestmark = pytest.mark.integration


# --- Profile -----------------------------------------------------------------


async def test_new_profile_reads_as_all_unknown(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile1@example.com")
    response = await client.get("/v1/me/profile", headers=athlete.auth)
    assert response.status_code == 200
    body = response.json()
    # Never-saved thresholds are null (unknown), never 0.
    assert body["ftp_watts"] is None
    assert body["hr_max"] is None
    assert body["thresholds_updated_at"] is None
    assert response.headers["etag"]


async def test_profile_put_then_get_roundtrips(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile2@example.com")
    put = await client.put(
        "/v1/me/profile",
        headers=athlete.auth,
        json={"ftp_watts": 250.0, "hr_max": 190, "hr_rest": 48, "weight_kg": 72.5},
    )
    assert put.status_code == 200, put.text
    assert put.json()["ftp_watts"] == 250.0
    assert put.json()["thresholds_updated_at"] is not None

    get = await client.get("/v1/me/profile", headers=athlete.auth)
    assert get.json()["hr_max"] == 190
    assert get.json()["weight_kg"] == 72.5


async def test_partial_put_does_not_clear_untouched_thresholds(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile3@example.com")
    await client.put("/v1/me/profile", headers=athlete.auth, json={"ftp_watts": 250.0})
    # A later edit that only mentions weight must not wipe the FTP.
    await client.put("/v1/me/profile", headers=athlete.auth, json={"weight_kg": 70.0})
    body = (await client.get("/v1/me/profile", headers=athlete.auth)).json()
    assert body["ftp_watts"] == 250.0
    assert body["weight_kg"] == 70.0


async def test_stale_if_match_is_rejected(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile4@example.com")
    first = await client.get("/v1/me/profile", headers=athlete.auth)
    stale_etag = first.headers["etag"]

    # Someone else writes, moving the tag on.
    await client.put("/v1/me/profile", headers=athlete.auth, json={"ftp_watts": 300.0})

    conflict = await client.put(
        "/v1/me/profile",
        headers={**athlete.auth, "If-Match": stale_etag},
        json={"ftp_watts": 310.0},
    )
    assert conflict.status_code == 412
    assert conflict.json()["code"] == "precondition_failed"


async def test_fresh_if_match_is_accepted(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile5@example.com")
    got = await client.get("/v1/me/profile", headers=athlete.auth)
    etag = got.headers["etag"]
    ok = await client.put(
        "/v1/me/profile",
        headers={**athlete.auth, "If-Match": etag},
        json={"ftp_watts": 275.0},
    )
    assert ok.status_code == 200


async def test_hr_max_must_exceed_hr_rest(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile6@example.com")
    bad = await client.put("/v1/me/profile", headers=athlete.auth, json={"hr_max": 150, "hr_rest": 160})
    assert bad.status_code == 422


async def test_out_of_range_threshold_is_a_422_not_a_500(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "profile7@example.com")
    # 1800 bpm is a typo; it must be caught before the database CHECK turns it
    # into an IntegrityError the API could only honestly report as a 500.
    bad = await client.put("/v1/me/profile", headers=athlete.auth, json={"hr_max": 1800})
    assert bad.status_code == 422


# --- Zones -------------------------------------------------------------------


async def test_zones_from_ftp_are_power_zones(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "zones1@example.com")
    await client.put(
        "/v1/me/profile",
        headers=athlete.auth,
        json={"ftp_watts": 250.0, "threshold_sources": {"ftp_watts": "tested"}},
    )
    response = await client.get("/v1/me/zones?sport=bike", headers=athlete.auth)
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "power"
    assert body["anchor"] == "ftp"
    assert body["anchor_source"] == "tested"
    assert len(body["zones"]) == 7
    # The open-ended top band is null on the wire, never Infinity.
    assert body["zones"][-1]["high"] is None


async def test_zones_without_any_anchor_refuse_rather_than_guess(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "zones2@example.com")
    response = await client.get("/v1/me/zones?sport=run", headers=athlete.auth)
    assert response.status_code == 422
    assert response.json()["code"] == "insufficient_data"


async def test_zones_from_estimated_hrmax_say_so(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "zones3@example.com")
    # Birth date only: HRmax is a Tanaka estimate, and the athlete must be told.
    await client.put("/v1/me/profile", headers=athlete.auth, json={"birth_date": "1990-01-01"})
    response = await client.get("/v1/me/zones?sport=run", headers=athlete.auth)
    assert response.status_code == 200
    assert response.json()["anchor"] == "hr_max"
    assert response.json()["anchor_source"] == "estimated"


# --- Goals -------------------------------------------------------------------


async def test_goal_lifecycle(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "goals1@example.com")
    created = await client.post(
        "/v1/me/goals",
        headers=athlete.auth,
        json={
            "goal_type": "race_time",
            "primary_sport": "run",
            "target_distance_m": 10000,
            "target_value": 2400,
            "race_date": "2026-11-01",
            "priority": 1,
        },
    )
    assert created.status_code == 201, created.text
    goal_id = created.json()["id"]

    listed = await client.get("/v1/me/goals", headers=athlete.auth)
    assert [g["id"] for g in listed.json()] == [goal_id]

    patched = await client.patch(f"/v1/me/goals/{goal_id}", headers=athlete.auth, json={"status": "achieved"})
    assert patched.json()["status"] == "achieved"

    deleted = await client.delete(f"/v1/me/goals/{goal_id}", headers=athlete.auth)
    assert deleted.status_code == 204


async def test_race_time_goal_needs_target_and_date(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "goals2@example.com")
    bad = await client.post(
        "/v1/me/goals",
        headers=athlete.auth,
        json={"goal_type": "race_time", "primary_sport": "run"},
    )
    assert bad.status_code == 422


# --- Personal bests ----------------------------------------------------------


async def test_personal_best_keeps_the_faster_time(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "pb1@example.com")
    base = {"sport": "run", "distance_m": 5000, "achieved_on": "2026-06-01", "source": "race"}

    await client.post("/v1/me/personal-bests", headers=athlete.auth, json={**base, "time_s": 1200})
    # A slower resubmission for the same day/distance must not worsen the record.
    slower = await client.post("/v1/me/personal-bests", headers=athlete.auth, json={**base, "time_s": 1300})
    assert slower.status_code == 201
    assert slower.json()["time_s"] == 1200.0


async def test_implausible_personal_best_is_rejected(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "pb2@example.com")
    # 1000 m in 10 s is 100 m/s — a data-entry error that would poison the fitted
    # Riegel exponent for every future prediction.
    bad = await client.post(
        "/v1/me/personal-bests",
        headers=athlete.auth,
        json={"sport": "run", "distance_m": 1000, "time_s": 10, "achieved_on": "2026-06-01"},
    )
    assert bad.status_code == 422
