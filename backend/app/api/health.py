from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.dependencies import get_settings

router = APIRouter()

_BEAT_HEARTBEAT_KEY = "cg:beat_heartbeat"


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    mongo_ok = await request.app.state.mongo.ping()
    redis_ok = await request.app.state.redis.ping()
    s3_ok = await request.app.state.s3.ping()
    firebase_ok = await request.app.state.firebase.ping()

    checks = {
        "mongo": "ok" if mongo_ok else "fail",
        "redis": "ok" if redis_ok else "fail",
        "minio": "ok" if s3_ok else "fail",
        "firebase": "ok" if firebase_ok else "fail",
    }

    all_ok = all(v == "ok" for v in checks.values())

    # Feature flags status
    settings = get_settings(request)
    features = {
        "execution_rate_limiting": settings.execution_rate_limit_enabled,
        "evidence_lifecycle": settings.evidence_lifecycle_enabled,
        "evidence_compression": settings.evidence_compression_enabled,
        "execution_strategy": settings.execution_strategy_enabled,
        "failure_graph": settings.failure_graph_enabled,
    }

    # Beat scheduler heartbeat — written every 5 min with 10-min TTL by the heartbeat task
    try:
        beat_alive = await request.app.state.redis.client.exists(_BEAT_HEARTBEAT_KEY)
        checks["beat"] = "ok" if beat_alive else "degraded"
    except Exception:
        checks["beat"] = "unknown"

    all_ok = all(v == "ok" for v in checks.values())

    return JSONResponse(
        content={
            "status": "ok" if all_ok else "degraded",
            "checks": checks,
            "features": features,
        },
        status_code=200 if all_ok else 503,
    )
