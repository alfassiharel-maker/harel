"""Authentication flows end to end (roadmap 1.3).

Every test here runs against the real app and the real database. The assertions
are about observable behaviour — status codes, whether a token still works —
never internal state, so they would keep passing if the implementation were
rewritten as long as the contract held.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import register_athlete, register_payload

pytestmark = pytest.mark.integration


async def test_register_returns_user_and_tokens(client: AsyncClient) -> None:
    response = await client.post("/v1/auth/register", json=register_payload("newathlete@example.com"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == "newathlete@example.com"
    assert body["user"]["role"] == "athlete"
    assert body["user"]["email_verified"] is False
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["tokens"]["token_type"] == "Bearer"


async def test_register_rejects_unaccepted_terms(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/register", json=register_payload("noterms@example.com", accept_terms=False)
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_register_rejects_unknown_fields(client: AsyncClient) -> None:
    # A client trying to smuggle `role: admin` through registration must be
    # rejected, not silently ignored.
    body = register_payload("sneaky@example.com")
    body["role"] = "admin"
    response = await client.post("/v1/auth/register", json=body)
    assert response.status_code == 422


async def test_duplicate_registration_conflicts_without_confirming_email(
    client: AsyncClient,
) -> None:
    await register_athlete(client, "dup@example.com")
    response = await client.post("/v1/auth/register", json=register_payload("dup@example.com"))
    assert response.status_code == 409
    # The detail must not confirm "this email is registered" in so many words.
    assert "dup@example.com" not in response.text


async def test_login_succeeds_with_correct_password(client: AsyncClient) -> None:
    await register_athlete(client, "login@example.com")
    response = await client.post(
        "/v1/auth/login",
        json={"email": "login@example.com", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tokens"]["access_token"]


async def test_wrong_password_and_unknown_account_are_indistinguishable(
    client: AsyncClient,
) -> None:
    await register_athlete(client, "real@example.com")

    wrong = await client.post(
        "/v1/auth/login", json={"email": "real@example.com", "password": "wrong-password-here"}
    )
    unknown = await client.post(
        "/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-here"}
    )

    assert wrong.status_code == 401
    assert unknown.status_code == 401
    # Same machine code for both: the endpoint is not an account-existence oracle.
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid_credentials"


async def test_repeated_failures_lock_the_account(client: AsyncClient) -> None:
    await register_athlete(client, "lockme@example.com")
    # settings.max_failed_logins is 5 in the test config.
    for _ in range(5):
        await client.post(
            "/v1/auth/login", json={"email": "lockme@example.com", "password": "nope-nope-nope"}
        )
    # Even the *correct* password is now refused with a lock, not a 401.
    locked = await client.post(
        "/v1/auth/login",
        json={"email": "lockme@example.com", "password": "correct-horse-battery-staple"},
    )
    assert locked.status_code == 423
    assert locked.json()["code"] == "account_locked"


async def test_refresh_rotates_and_reuse_revokes_the_family(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "rotate@example.com")
    original = athlete.refresh_token

    first = await client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != original

    # Replaying the original (already-rotated) token is the signature of theft:
    # the whole family is revoked.
    reuse = await client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert reuse.status_code == 401

    # And the legitimate-looking rotated token is now dead too — victim and
    # attacker are both logged out, as intended.
    after = await client.post("/v1/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_logout_revokes_the_refresh_token(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "logout@example.com")
    logout = await client.post("/v1/auth/logout", json={"refresh_token": athlete.refresh_token})
    assert logout.status_code == 204

    replay = await client.post("/v1/auth/refresh", json={"refresh_token": athlete.refresh_token})
    assert replay.status_code == 401


async def test_logout_is_idempotent(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "logout2@example.com")
    await client.post("/v1/auth/logout", json={"refresh_token": athlete.refresh_token})
    again = await client.post("/v1/auth/logout", json={"refresh_token": athlete.refresh_token})
    assert again.status_code == 204


async def test_protected_route_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/v1/me")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Bearer")
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_protected_route_rejects_a_garbage_token(client: AsyncClient) -> None:
    response = await client.get("/v1/me", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert response.status_code == 401


async def test_access_token_reaches_own_profile(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "self@example.com")
    response = await client.get("/v1/me", headers=athlete.auth)
    assert response.status_code == 200
    assert response.json()["id"] == athlete.id
