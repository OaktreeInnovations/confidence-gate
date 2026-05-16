"""Nightly task: detect model drift per org."""

import structlog

from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="cg.detect_model_drift")
def detect_model_drift_task() -> None:
    """Compute per-project model drift for all orgs and upsert project_alerts."""
    try:
        from app.clients import sync_mongo
        from app.intelligence.model_drift_detector import detect_model_drift

        db = sync_mongo.db
        orgs = list(db.orgs.find({}, {"_id": 1}))
        logger.info("detect_model_drift.start", org_count=len(orgs))

        for org in orgs:
            org_id = str(org["_id"])
            try:
                detect_model_drift(db, org_id)
            except Exception as e:
                logger.warning("detect_model_drift.org_failed", org_id=org_id, error=str(e)[:200])

        logger.info("detect_model_drift.done", org_count=len(orgs))
    except Exception as e:
        logger.error("detect_model_drift.task_failed", error=str(e)[:400])
        raise
