"""Redis-backed session store for Quick Capture recording sessions.

Each session is keyed as  capture:session:{session_id}  and stores:
  - raw_events: list of browser events received so far
  - steps:      transformed StepIntent JSON list (populated on stop)
  - metadata:   project_id, user_id, started_at, base_url, title

TTL: 2 hours — sessions not saved within 2 hours are auto-expired.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)

_SESSION_TTL_SECONDS = 7200  # 2 hours
_KEY_PREFIX = "capture:session:"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


async def create_session(
    redis: aioredis.Redis,
    project_id: str,
    user_id: str,
    base_url: str = "",
    title: str = "",
) -> str:
    """Create a new capture session and return its UUID."""
    session_id = str(uuid.uuid4())
    data = {
        "session_id": session_id,
        "project_id": project_id,
        "user_id": user_id,
        "base_url": base_url,
        "title": title or "Captured Test",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stopped_at": "",
        "raw_events": "[]",
        "steps": "[]",
        "event_count": 0,
    }
    await redis.hset(_key(session_id), mapping=data)
    await redis.expire(_key(session_id), _SESSION_TTL_SECONDS)
    logger.info("capture.session_created", session_id=session_id, project_id=project_id)
    return session_id


async def append_events(
    redis: aioredis.Redis,
    session_id: str,
    new_events: list[dict],
) -> int:
    """Append events to an existing session. Returns updated event count.

    Raises KeyError if session does not exist.
    """
    key = _key(session_id)
    raw = await redis.hget(key, "raw_events")
    if raw is None:
        raise KeyError(f"Session {session_id!r} not found")

    existing: list[dict] = json.loads(raw)
    existing.extend(new_events)
    count = len(existing)

    await redis.hset(key, mapping={
        "raw_events": json.dumps(existing),
        "event_count": count,
    })
    await redis.expire(key, _SESSION_TTL_SECONDS)
    return count


async def get_session(redis: aioredis.Redis, session_id: str) -> dict | None:
    """Return full session dict, or None if not found."""
    key = _key(session_id)
    data = await redis.hgetall(key)
    if not data:
        return None
    # Deserialise nested JSON fields
    data["raw_events"] = json.loads(data.get("raw_events", "[]"))
    data["steps"] = json.loads(data.get("steps", "[]"))
    data["event_count"] = int(data.get("event_count", 0))
    data["stopped_at"] = data.get("stopped_at") or None
    return data


async def store_steps(
    redis: aioredis.Redis,
    session_id: str,
    steps_json: list[dict],
    stopped_at: str,
) -> None:
    """Store transformed steps and mark session as stopped."""
    key = _key(session_id)
    await redis.hset(key, mapping={
        "steps": json.dumps(steps_json),
        "stopped_at": stopped_at,
    })
    await redis.expire(key, _SESSION_TTL_SECONDS)


async def delete_session(redis: aioredis.Redis, session_id: str) -> None:
    """Remove a session from Redis (called after successful save to DB)."""
    await redis.delete(_key(session_id))
    logger.info("capture.session_deleted", session_id=session_id)
