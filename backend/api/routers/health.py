"""Liveness and readiness probes (docs/03 §12).

The two are deliberately different. `/healthz` answers "is this process alive"
with no I/O, so a dependency outage does not cause the orchestrator to kill and
reschedule healthy pods — which would turn a database blip into an outage.
`/readyz` answers "should this pod receive traffic" and does check Postgres and
Redis, so a pod with a broken dependency is pulled from the load balancer without
being restarted.
"""

from __future__ import annotations

from typing import Annotated

import redis.asyncio as redis
from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from backend.api.deps import AppServices, get_services

__all__ = ["router"]

router = APIRouter(tags=["meta"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(services: Annotated[AppServices, Depends(get_services)]) -> JSONResponse:
    db_ok = await services.database.healthcheck()
    redis_ok = await _redis_ok(services.settings.redis_url)

    checks = {"postgres": db_ok, "redis": redis_ok}
    ready = all(checks.values())
    # 503 when not ready so the orchestrator holds traffic. The body names which
    # dependency failed, for the operator — it exposes nothing an attacker gains
    # from, and a probe that only says "not ready" wastes an on-call's night.
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )


async def _redis_ok(url: str) -> bool:
    client = redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        # `aclose()` is the non-deprecated close in redis-py 5.x; the pinned
        # types-redis stubs still only know the older `close`, hence the ignore.
        await client.aclose()  # type: ignore[attr-defined]
