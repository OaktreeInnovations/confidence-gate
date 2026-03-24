"""Read-only API for cross-test failure intelligence graph.

Exposes pre-computed failure clusters, feature risk profiles,
and release confidence scores. No writes — all data is computed
by the background Celery task.
"""

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
import structlog

from app.api.auth.dependencies import get_current_user
from app.dependencies import get_db, get_settings
from app.models import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


def _require_org(user: User) -> ObjectId:
    if not user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User must belong to an organization",
        )
    return user.org_id


@router.get("/failure-graph")
async def get_failure_graph(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Get the latest failure intelligence graph for the user's org."""
    settings = get_settings(request)
    if not settings.failure_graph_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failure graph feature is not enabled",
        )

    org_id = _require_org(user)
    doc = await db.failure_graph.find_one({"org_id": str(org_id)})

    if not doc:
        return {
            "status": "no_data",
            "message": "No failure graph computed yet. Run some tests first.",
            "clusters": [],
            "feature_risks": [],
            "release_confidence": {"score": 0, "grade": "F", "components": {}, "blockers": []},
        }

    return {
        "status": "ok",
        "computed_at": doc.get("computed_at"),
        "window_hours": doc.get("window_hours"),
        "total_test_cases": doc.get("total_test_cases", 0),
        "total_runs_in_window": doc.get("total_runs_in_window", 0),
        "total_failure_instances": doc.get("total_failure_instances", 0),
        "clusters": doc.get("clusters", []),
        "feature_risks": doc.get("feature_risks", []),
        "release_confidence": doc.get("release_confidence", {}),
    }


@router.get("/release-confidence")
async def get_release_confidence(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Get just the release confidence score (lightweight endpoint)."""
    settings = get_settings(request)
    if not settings.failure_graph_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failure graph feature is not enabled",
        )

    org_id = _require_org(user)
    doc = await db.failure_graph.find_one(
        {"org_id": str(org_id)},
        {"release_confidence": 1, "computed_at": 1},
    )

    if not doc:
        return {"score": 0, "grade": "F", "computed_at": None}

    rc = doc.get("release_confidence", {})
    return {
        "score": rc.get("score", 0),
        "grade": rc.get("grade", "F"),
        "components": rc.get("components", {}),
        "blockers": rc.get("blockers", []),
        "computed_at": doc.get("computed_at"),
    }
