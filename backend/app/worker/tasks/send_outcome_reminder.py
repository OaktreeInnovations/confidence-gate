"""Nightly task: nudge teams to record production outcomes.

7 days after a Ship (recommendation=deploy) decision with no outcome recorded,
fire the project's configured webhook with event_type="outcome_reminder".
This is the lowest-friction path to populating the calibration feedback loop.

Marks `outcome_reminder_sent_at` on the validation to avoid re-sending.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import structlog

from app.worker.celery_app import celery_app
from app.clients import sync_mongo

logger = structlog.get_logger(__name__)

_REMINDER_AFTER_DAYS = 7


@celery_app.task(name="cg.send_outcome_reminder")
def send_outcome_reminder() -> dict:
    db = sync_mongo.get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=_REMINDER_AFTER_DAYS)

    # Find Ship decisions older than 7 days with no outcome and no reminder sent
    candidates = list(db.release_validations.find({
        "recommendation": "deploy",
        "outcome": None,
        "outcome_reminder_sent_at": None,
        "completed_at": {"$lt": cutoff},
        "status": "completed",
    }, {
        "_id": 1,
        "org_id": 1,
        "project_id": 1,
        "project_name": 1,
        "title": 1,
        "confidence_score": 1,
        "completed_at": 1,
    }))

    if not candidates:
        logger.info("send_outcome_reminder.no_candidates")
        return {"sent": 0}

    sent = 0
    errors = 0

    for val in candidates:
        val_id = val["_id"]
        project_id = val.get("project_id")

        # Look up webhook URL on the project
        project = db.projects.find_one(
            {"_id": project_id},
            {"webhook_url": 1},
        ) if project_id else None
        webhook_url = project.get("webhook_url") if project else None

        if webhook_url:
            payload = {
                "event_type": "outcome_reminder",
                "validation_id": str(val_id),
                "project_name": val.get("project_name", ""),
                "title": val.get("title", ""),
                "confidence_score": val.get("confidence_score"),
                "completed_at": val["completed_at"].isoformat() if val.get("completed_at") else None,
                "message": (
                    "This release shipped 7+ days ago. Record the outcome to improve "
                    "future confidence score accuracy."
                ),
            }
            try:
                import requests  # type: ignore
                requests.post(webhook_url, json=payload, timeout=10)
                sent += 1
            except Exception as e:
                logger.warning(
                    "send_outcome_reminder.webhook_failed",
                    val_id=str(val_id),
                    error=str(e)[:200],
                )
                errors += 1
        else:
            # No webhook — still mark sent so we don't keep querying this record
            sent += 1

        # Mark reminder sent regardless of whether a webhook was configured
        db.release_validations.update_one(
            {"_id": val_id},
            {"$set": {"outcome_reminder_sent_at": datetime.now(timezone.utc)}},
        )

    logger.info("send_outcome_reminder.done", sent=sent, errors=errors)
    return {"sent": sent, "errors": errors}
