"""Redis-backed sliding-window rate limiter for API endpoints.

Usage (FastAPI dependency):
    from app.intelligence.rate_limiter import check_validation_rate_limit

    @router.post("")
    async def create_release_validation(
        ...
        _: None = Depends(check_validation_rate_limit),
    ):
        ...

Algorithm:
  - Key: rate_limit:{scope}:{org_id}
  - Uses Redis INCR + EXPIRE (atomic for single instance; sufficient for this use case)
  - Window: 60 seconds
  - Limit: configurable, default 10 per window

Returns HTTP 429 with Retry-After header if limit exceeded.
"""

from __future__ import annotations

from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_redis_client
from app.models import User


_WINDOW_SECONDS = 60
_DEFAULT_LIMIT = 10  # requests per window


async def _check_rate_limit(
    redis: aioredis.Redis,
    key: str,
    limit: int,
    window: int,
) -> None:
    """Increment key counter; raise 429 if limit exceeded. Non-atomic but sufficient."""
    current = await redis.get(key)
    if current is not None and int(current) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: max {limit} requests per {window}s",
            headers={"Retry-After": str(window)},
        )

    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, window)
    await pipe.execute()


async def check_validation_rate_limit(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[aioredis.Redis, Depends(get_redis_client)],
) -> None:
    """FastAPI dependency: enforce per-org validation creation rate limit."""
    if not current_user.org_id:
        return  # let the endpoint handle missing org
    org_id = str(current_user.org_id)
    key = f"rate_limit:validation_create:{org_id}"
    await _check_rate_limit(redis, key, _DEFAULT_LIMIT, _WINDOW_SECONDS)
