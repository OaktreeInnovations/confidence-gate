"""7C.1 Nightly cross-org signal weight optimization task."""

import structlog

from app.worker.celery_app import celery_app
from app.clients import sync_mongo

logger = structlog.get_logger(__name__)


@celery_app.task(name="cg.optimize_global_weights", ignore_result=True)
def optimize_global_weights() -> None:
    """Fit cross-org logistic regression and store learned signal weights."""
    from app.intelligence.global_weight_optimizer import run_global_optimization

    db = sync_mongo.db
    result = run_global_optimization(db)
    if result:
        logger.info(
            "optimize_global_weights.complete",
            sample_count=result["sample_count"],
        )
    else:
        logger.info("optimize_global_weights.skipped_insufficient_data")
