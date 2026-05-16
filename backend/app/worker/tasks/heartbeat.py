"""Beat scheduler health heartbeat.

Fires every 5 minutes via Celery Beat and writes a Redis key with a 10-minute TTL.
The /ready endpoint checks this key to confirm the Beat scheduler is alive.
If Beat dies, the key expires and /ready returns 'beat: degraded'.
"""

import structlog

from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)

_HEARTBEAT_KEY = "cg:beat_heartbeat"
_TTL_SECONDS = 600  # 10 minutes — 2× the beat interval


@celery_app.task(name="cg.beat_heartbeat")
def beat_heartbeat() -> None:
    """Write a heartbeat key to Redis to signal Beat is alive."""
    try:
        import redis as sync_redis
        from app.config import Settings
        settings = Settings()
        r = sync_redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
        r.set(_HEARTBEAT_KEY, "1", ex=_TTL_SECONDS)
        r.close()
        logger.info("beat_heartbeat.written", ttl=_TTL_SECONDS)
    except Exception as e:
        logger.warning("beat_heartbeat.error", error=str(e)[:200])
