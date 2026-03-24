"""Distributed execution rate limiting and concurrency guard.

Uses Redis atomic operations with TTL-based crash safety.
All counters auto-expire so a dead worker never permanently blocks slots.

Redis key schema:
    qualora:exec:org:{org_id}       — concurrent running count per org (TTL = lock_ttl)
    qualora:exec:global             — global concurrent running count (TTL = lock_ttl)
    qualora:exec:queue:{org_id}     — queued (not yet running) count per org (TTL = lock_ttl)
    qualora:exec:slot:{test_run_id} — per-run marker so decrement is idempotent (TTL = lock_ttl)

Each running test run holds a "slot" key. When the run completes
(or crashes and the TTL expires), the slot key disappears and the
counter is decremented.

Lua scripts ensure check+increment is atomic — no TOCTOU races.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from redis import Redis as SyncRedis

logger = structlog.get_logger(__name__)

# --- Exceptions ---


class ExecutionLimitExceeded(Exception):
    """Raised when per-org or global concurrency limit is reached."""

    def __init__(self, org_id: str, current: int, limit: int):
        self.org_id = org_id
        self.current = current
        self.limit = limit
        super().__init__(
            f"Org {org_id} has {current}/{limit} concurrent executions"
        )


class QueueDepthExceeded(Exception):
    """Raised when the org's queue depth limit is reached."""

    def __init__(self, org_id: str, current: int, limit: int):
        self.org_id = org_id
        self.current = current
        self.limit = limit
        super().__init__(
            f"Org {org_id} has {current}/{limit} queued executions"
        )


# --- Lua scripts for atomic operations ---

# Atomically increment if below limit. Returns (new_count, allowed).
# KEYS[1] = counter key, ARGV[1] = max_limit, ARGV[2] = ttl_seconds
_LUA_ACQUIRE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if current >= limit then
    return {current, 0}
end
local new = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return {new, 1}
"""

# Atomically decrement, floor at 0.
# KEYS[1] = counter key
_LUA_RELEASE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
    return redis.call('DECR', KEYS[1])
end
return 0
"""


def _key_org(org_id: str) -> str:
    return f"qualora:exec:org:{org_id}"


def _key_global() -> str:
    return "qualora:exec:global"


def _key_queue(org_id: str) -> str:
    return f"qualora:exec:queue:{org_id}"


def _key_slot(test_run_id: str) -> str:
    return f"qualora:exec:slot:{test_run_id}"


class ExecutionLimiter:
    """Distributed concurrency limiter using Redis.

    Two phases:
        1. enqueue() — called from FastAPI when a run is submitted.
           Checks queue depth. Increments queue counter.
        2. acquire() — called from Celery task before execution starts.
           Decrements queue counter. Checks + increments concurrency counter.
        3. release() — called from Celery task when execution finishes.
           Decrements concurrency counter. Idempotent via slot key.
    """

    def __init__(
        self,
        max_concurrent_per_org: int = 3,
        max_queue_depth_per_org: int = 20,
        global_max_concurrent: int = 10,
        lock_ttl_seconds: int = 900,
    ):
        self.max_concurrent_per_org = max_concurrent_per_org
        self.max_queue_depth_per_org = max_queue_depth_per_org
        self.global_max_concurrent = global_max_concurrent
        self.lock_ttl_seconds = lock_ttl_seconds

    # --- Async API (FastAPI side) ---

    async def enqueue(self, redis: aioredis.Redis, org_id: str) -> int:
        """Check queue depth and increment queue counter.

        Returns the new queue depth.
        Raises QueueDepthExceeded if limit is reached.
        """
        queue_key = _key_queue(org_id)
        result = await redis.eval(
            _LUA_ACQUIRE, 1, queue_key,
            str(self.max_queue_depth_per_org),
            str(self.lock_ttl_seconds),
        )
        count, allowed = int(result[0]), int(result[1])
        if not allowed:
            logger.warning(
                "execution_limit.queue_depth_exceeded",
                org_id=org_id, current=count,
                limit=self.max_queue_depth_per_org,
            )
            raise QueueDepthExceeded(org_id, count, self.max_queue_depth_per_org)

        logger.debug("execution_limit.enqueued", org_id=org_id, queue_depth=count)
        return count

    async def dequeue_async(self, redis: aioredis.Redis, org_id: str) -> None:
        """Decrement queue counter (async version for error paths in FastAPI)."""
        await redis.eval(_LUA_RELEASE, 1, _key_queue(org_id))

    # --- Sync API (Celery worker side) ---

    def acquire(
        self, redis: SyncRedis, org_id: str, test_run_id: str
    ) -> None:
        """Acquire an execution slot. Called at task start.

        Decrements the queue counter, then checks concurrency limits.
        Sets a per-run slot key for idempotent release.
        Raises ExecutionLimitExceeded if at capacity.
        """
        # Dequeue (we're starting now)
        redis.eval(_LUA_RELEASE, 1, _key_queue(org_id))

        # Check per-org concurrency
        org_key = _key_org(org_id)
        result = redis.eval(
            _LUA_ACQUIRE, 1, org_key,
            str(self.max_concurrent_per_org),
            str(self.lock_ttl_seconds),
        )
        org_count, org_allowed = int(result[0]), int(result[1])
        if not org_allowed:
            logger.warning(
                "execution_limit.org_concurrency_exceeded",
                org_id=org_id, current=org_count,
                limit=self.max_concurrent_per_org,
            )
            raise ExecutionLimitExceeded(
                org_id, org_count, self.max_concurrent_per_org
            )

        # Check global concurrency
        global_key = _key_global()
        result = redis.eval(
            _LUA_ACQUIRE, 1, global_key,
            str(self.global_max_concurrent),
            str(self.lock_ttl_seconds),
        )
        global_count, global_allowed = int(result[0]), int(result[1])
        if not global_allowed:
            # Roll back per-org increment
            redis.eval(_LUA_RELEASE, 1, org_key)
            logger.warning(
                "execution_limit.global_concurrency_exceeded",
                current=global_count,
                limit=self.global_max_concurrent,
            )
            raise ExecutionLimitExceeded(
                org_id, global_count, self.global_max_concurrent
            )

        # Mark this run's slot for idempotent release
        slot_key = _key_slot(test_run_id)
        redis.set(slot_key, "1", ex=self.lock_ttl_seconds)

        logger.info(
            "execution_limit.acquired",
            org_id=org_id, test_run_id=test_run_id,
            org_concurrent=org_count, global_concurrent=global_count,
        )

    def release(
        self, redis: SyncRedis, org_id: str, test_run_id: str
    ) -> None:
        """Release an execution slot. Idempotent — safe to call multiple times.

        Only decrements if the slot key still exists (prevents double-release).
        """
        slot_key = _key_slot(test_run_id)
        deleted = redis.delete(slot_key)
        if not deleted:
            # Already released (or TTL expired) — nothing to do
            return

        redis.eval(_LUA_RELEASE, 1, _key_org(org_id))
        redis.eval(_LUA_RELEASE, 1, _key_global())

        logger.info(
            "execution_limit.released",
            org_id=org_id, test_run_id=test_run_id,
        )

    # --- Observability ---

    async def get_status(
        self, redis: aioredis.Redis, org_id: str
    ) -> dict:
        """Return current counters for an org (for dashboards / health checks)."""
        org_concurrent = int(await redis.get(_key_org(org_id)) or 0)
        org_queued = int(await redis.get(_key_queue(org_id)) or 0)
        global_concurrent = int(await redis.get(_key_global()) or 0)
        return {
            "org_concurrent": org_concurrent,
            "org_queued": org_queued,
            "global_concurrent": global_concurrent,
            "org_concurrent_limit": self.max_concurrent_per_org,
            "org_queue_limit": self.max_queue_depth_per_org,
            "global_concurrent_limit": self.global_max_concurrent,
        }
