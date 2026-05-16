"""Celery task for computing the cross-test failure intelligence graph.

Triggered after profile aggregation completes. Computes failure clusters,
feature risk scores, and release confidence for the org.

Feature-flagged: only runs when failure_graph_enabled=True.
"""

from datetime import datetime, timezone

import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.config import Settings
from app.worker.celery_app import celery_app

settings = Settings()
logger = structlog.get_logger(__name__)


@celery_app.task(name="cg.compute_failure_graph", soft_time_limit=120, time_limit=150)
def compute_failure_graph_task(org_id: str) -> dict:
    """Compute failure intelligence graph for an org.

    Non-fatal: failures are logged but never block execution.
    """
    if not settings.failure_graph_enabled:
        return {"status": "skipped", "reason": "failure_graph_enabled=False"}

    try:
        db = _get_sync_db()

        from app.intelligence.failure_graph import compute_failure_graph
        graph = compute_failure_graph(
            db, org_id,
            window_hours=settings.failure_graph_window_hours,
        )

        return {
            "status": "ok",
            "org_id": org_id,
            "clusters": len(graph.get("clusters", [])),
            "features": len(graph.get("feature_risks", [])),
            "confidence": graph.get("release_confidence", {}).get("score", 0),
        }
    except Exception as e:
        logger.warning(
            "compute_failure_graph.failed",
            org_id=org_id,
            error=str(e)[:200],
        )
        return {"status": "error", "message": str(e)[:200]}
