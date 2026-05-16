"""Nightly Celery task — recompute per-org learned decision thresholds.

Reads production_outcomes (populated by sync_outcomes_to_calibration) and
updates org_learned_thresholds via weight_learner.compute_org_thresholds.
Runs after sync_outcomes_to_calibration in the same nightly window.
"""

from __future__ import annotations

import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    name="cg.compute_learned_thresholds",
    soft_time_limit=120,
    time_limit=150,
)
def compute_learned_thresholds_task() -> dict:
    """Recompute decision thresholds from outcome data for every org."""
    from app.intelligence.weight_learner import compute_org_thresholds

    db = _get_sync_db()

    # Collect all org_ids that have outcome data
    org_ids = db.production_outcomes.distinct("org_id")
    updated = 0
    skipped = 0

    for org_id in org_ids:
        if not org_id:
            skipped += 1
            continue
        try:
            compute_org_thresholds(db, str(org_id))
            updated += 1
        except Exception as e:
            logger.warning(
                "compute_learned_thresholds.failed",
                org_id=str(org_id),
                error=str(e)[:200],
            )
            skipped += 1

    logger.info("compute_learned_thresholds.done", updated=updated, skipped=skipped)
    return {"status": "ok", "updated": updated, "skipped": skipped}
