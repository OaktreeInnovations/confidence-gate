"""Lightweight synchronous Redis-backed cache for the intelligence pipeline.

Used in the Celery worker context where async Redis is not available.
All operations are non-fatal — a cache miss simply falls through to MongoDB.
"""

from __future__ import annotations

import json
from functools import lru_cache

import structlog

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def _client():
    """Return a module-level sync Redis client (lazy init, cached)."""
    try:
        import redis as _redis
        from app.config import Settings
        settings = Settings()
        return _redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=1,
        )
    except Exception as e:
        logger.warning("cache.init_failed", error=str(e)[:200])
        return None


def get(key: str) -> dict | list | None:
    """Return cached value or None on miss/error."""
    c = _client()
    if c is None:
        return None
    try:
        raw = c.get(key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.debug("cache.get_error", key=key, error=str(e)[:100])
    return None


def set(key: str, value: dict | list, ttl: int) -> None:
    """Write value to cache with TTL seconds. Non-fatal on error."""
    c = _client()
    if c is None:
        return
    try:
        c.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as e:
        logger.debug("cache.set_error", key=key, error=str(e)[:100])


def delete(key: str) -> None:
    """Invalidate a cache key. Non-fatal on error."""
    c = _client()
    if c is None:
        return
    try:
        c.delete(key)
    except Exception as e:
        logger.debug("cache.delete_error", key=key, error=str(e)[:100])
