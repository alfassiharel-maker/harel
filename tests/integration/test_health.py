"""Liveness and readiness probes (roadmap 1.1, docs/03 §12)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_healthz_is_dependency_free(client: AsyncClient) -> None:
    # Liveness must not touch Postgres or Redis: a dependency blip must not make
    # the orchestrator kill an otherwise-healthy process.
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_dependency_health(client: AsyncClient) -> None:
    # With Postgres and Redis up (the suite's precondition), readiness is 200 and
    # names each check so an operator can see what was probed.
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"postgres": True, "redis": True}
