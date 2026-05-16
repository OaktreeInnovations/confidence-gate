"""Nightly Celery task — sync production outcomes from release_validations into
the production_outcomes collection that outcome_calibration.py reads.

The outcome endpoint (POST /outcome) writes `outcome` onto the release_validation
document. This task bridges that to the calibration collection so the calibration
model learns from real data.

Mapping:
  release_validations.outcome = "production_passed"  → incident_occurred = False
  release_validations.outcome = "production_failed"  → incident_occurred = True
  release_validations.outcome = "rolled_back"        → incident_occurred = True
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)

_INCIDENT_OUTCOMES = {"production_failed", "rolled_back", "p0_outage", "p1_degradation"}


@celery_app.task(
    name="cg.sync_outcomes_to_calibration",
    soft_time_limit=120,
    time_limit=150,
)
def sync_outcomes_to_calibration_task() -> dict:
    """Copy completed release validation outcomes into production_outcomes."""
    db = _get_sync_db()

    # Find all validations with an outcome and a frozen score
    cursor = db.release_validations.find(
        {
            "outcome": {"$ne": None},
            "final_score_at_decision": {"$ne": None},
        },
        {
            "_id": 1,
            "org_id": 1,
            "final_score_at_decision": 1,
            "outcome": 1,
            "outcome_recorded_at": 1,
        },
    )

    synced = 0
    skipped = 0

    for doc in cursor:
        validation_id = str(doc["_id"])
        score = doc["final_score_at_decision"]
        outcome = doc["outcome"]
        incident_occurred = outcome in _INCIDENT_OUTCOMES

        existing = db.production_outcomes.find_one({"validation_id": validation_id})
        if existing:
            skipped += 1
            continue

        db.production_outcomes.insert_one({
            "validation_id": validation_id,
            "org_id": doc.get("org_id"),
            "confidence_score": score,
            "incident_occurred": incident_occurred,
            "outcome": outcome,
            "synced_at": datetime.now(timezone.utc),
            "outcome_recorded_at": doc.get("outcome_recorded_at"),
        })
        synced += 1

    logger.info(
        "sync_outcomes_to_calibration.done",
        synced=synced,
        skipped=skipped,
    )
    return {"status": "ok", "synced": synced, "skipped": skipped}
