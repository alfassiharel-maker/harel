"""Consent capture and withdrawal (roadmap 1.4, docs/06 §10)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import register_athlete

pytestmark = pytest.mark.integration


async def test_registration_records_consents(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "consent1@example.com")
    response = await client.get("/v1/me/consents", headers=athlete.auth)
    assert response.status_code == 200
    by_purpose = {c["purpose"]: c["granted"] for c in response.json()}
    assert by_purpose["terms_of_service"] is True
    assert by_purpose["privacy_policy"] is True
    assert by_purpose["health_data_processing"] is True
    # marketing was declined in the default registration payload.
    assert by_purpose["marketing_email"] is False


async def test_withdrawal_is_a_new_row_not_an_overwrite(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "consent2@example.com")
    # Grant marketing, then withdraw it. The endpoint returns the *current* state.
    await client.put(
        "/v1/me/consents/marketing_email",
        headers=athlete.auth,
        json={"granted": True, "document_version": "2026-07-01"},
    )
    withdrawn = await client.put(
        "/v1/me/consents/marketing_email",
        headers=athlete.auth,
        json={"granted": False, "document_version": "2026-07-01"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["granted"] is False

    current = await client.get("/v1/me/consents", headers=athlete.auth)
    marketing = [c for c in current.json() if c["purpose"] == "marketing_email"]
    # DISTINCT ON returns exactly one current row per purpose — the latest.
    assert len(marketing) == 1
    assert marketing[0]["granted"] is False


async def test_health_consent_cannot_be_withdrawn_inline(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "consent3@example.com")
    # Withdrawing health-data consent removes the lawful basis for the analytics;
    # it must route through account deletion/export, not a silent toggle.
    response = await client.put(
        "/v1/me/consents/health_data_processing",
        headers=athlete.auth,
        json={"granted": False, "document_version": "2026-07-01"},
    )
    assert response.status_code == 422


async def test_unknown_consent_purpose_is_rejected(client: AsyncClient) -> None:
    athlete = await register_athlete(client, "consent4@example.com")
    response = await client.put(
        "/v1/me/consents/not_a_real_purpose",
        headers=athlete.auth,
        json={"granted": True, "document_version": "2026-07-01"},
    )
    assert response.status_code == 422
