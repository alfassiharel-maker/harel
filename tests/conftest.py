"""Shared fixtures for the integration and security suites.

These tests exercise the real application against a real Postgres — the point of
the security suite is that row-level security genuinely applies, and that can
only be shown against a database that enforces it. They therefore need the
`docker compose` Postgres up and the migrations applied (`make db-bootstrap`).

The app connects as `app_rw`, deliberately: `app_rw` is not the table owner and
holds neither SUPERUSER nor BYPASSRLS, so the RLS policies actually bite. A suite
that connected as the owner would pass while proving nothing.

Cleanup between tests runs on a *separate* superuser connection. It has to:
truncating athlete data is not something `app_rw` is allowed to do, and that it
is not allowed to is part of what these tests assert elsewhere.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.config import Settings
from backend.core.security import PasswordService, TokenService
from backend.database.session import Database

# --- Connection parameters (local/CI only) -----------------------------------
DB_HOST = os.environ.get("PGHOST", "localhost")
DB_PORT = int(os.environ.get("PGPORT", "5432"))
DB_NAME = os.environ.get("DBNAME", "aisportscoach")
APP_ROLE_DSN = f"postgresql+asyncpg://app_rw:localdev@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SUPERUSER_DSN = f"postgresql://postgres:localdev@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Tables truncated between tests. `CASCADE` from identity.users reaches every
# athlete-owned table through the ON DELETE CASCADE foreign keys; audit_events is
# listed explicitly because its FK to users is ON DELETE SET NULL, so it would
# otherwise survive and leak rows across tests.
_TRUNCATE = "TRUNCATE identity.users, identity.audit_events RESTART IDENTITY CASCADE"


@pytest.fixture(scope="session")
def settings() -> Settings:
    # Built explicitly rather than from the environment so the suite is
    # self-contained and cannot be steered by a stray shell variable. `ci`
    # permits the ephemeral JWT keypair, which is exactly right for a throwaway
    # test process.
    return Settings(
        app_env="ci",
        app_debug=False,
        log_format="console",
        database_url=APP_ROLE_DSN,
        # Small pool: the suite is not concurrent, and a large pool just slows
        # engine disposal between tests.
        database_pool_size=5,
        database_max_overflow=0,
        access_token_ttl_seconds=600,
        max_failed_logins=5,
        lockout_minutes=15,
    )


@pytest_asyncio.fixture
async def _clean_db() -> AsyncIterator[None]:
    """Truncate athlete data before each test, as the superuser."""
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        await conn.execute(_TRUNCATE)
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def app(settings: Settings, _clean_db: None) -> AsyncIterator[object]:
    # Imported here, after settings exist, so importing the test module never
    # triggers application startup as a side effect.
    from backend.api.main import create_app

    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app: object) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db(settings: Settings, _clean_db: None) -> AsyncIterator[Database]:
    """A `Database` for tests that talk to the RLS layer directly (security suite)."""
    database = Database(settings)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture(scope="session")
def passwords() -> PasswordService:
    return PasswordService()


@pytest.fixture(scope="session")
def tokens(settings: Settings) -> TokenService:
    return TokenService(settings)


# --- Helpers ------------------------------------------------------------------


def register_payload(email: str, **overrides: object) -> dict[str, object]:
    """A valid registration body. Overrides let a test bend one field at a time."""
    body: dict[str, object] = {
        "email": email,
        "password": "correct-horse-battery-staple",
        "display_name": "Test Athlete",
        "accept_terms": True,
        "accept_privacy_policy": True,
        "consent_health_data": True,
        "consent_marketing": False,
    }
    body.update(overrides)
    return body


class Athlete:
    """A registered athlete plus the tokens to act as them."""

    def __init__(self, user: dict, tokens: dict) -> None:
        self.user = user
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens["refresh_token"]
        self.id = user["id"]

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def register_athlete(client: AsyncClient, email: str, **overrides: object) -> Athlete:
    response = await client.post("/v1/auth/register", json=register_payload(email, **overrides))
    assert response.status_code == 201, response.text
    body = response.json()
    return Athlete(body["user"], body["tokens"])
