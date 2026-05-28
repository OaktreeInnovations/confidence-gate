"""2.5 Validation SLA — marks release validations that have exceeded their time budget."""

from datetime import datetime, timedelta, timezone

import structlog

from app.worker.celery_app import celery_app
from app.clients import sync_mongo
from app.config import Settings

logger = structlog.get_logger(__name__)
settings = Settings()


@celery_app.task(name="cg.check_validation_sla", ignore_result=True)
def check_validation_sla() -> None:
    """Mark active validations that have exceeded the SLA as sla_breached."""
    if settings.validation_sla_minutes <= 0:
        return

    db = sync_mongo.get_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.validation_sla_minutes)

    # Find validations that are still active and started before the cutoff
    active = list(db.release_validations.find(
        {
            "status": {"$in": ["pending", "running", "computing"]},
            "sla_breached": {"$ne": True},
            "created_at": {"$lt": cutoff},
        },
        {"_id": 1},
    ))

    if not active:
        return

    ids = [d["_id"] for d in active]
    result = db.release_validations.update_many(
        {"_id": {"$in": ids}},
        {"$set": {"sla_breached": True, "sla_breached_at": now, "updated_at": now}},
    )
    logger.info(
        "validation_sla.breached",
        count=result.modified_count,
        sla_minutes=settings.validation_sla_minutes,
    )
