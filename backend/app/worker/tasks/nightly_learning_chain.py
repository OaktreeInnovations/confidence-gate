"""Nightly learning chain — runs sync_outcomes → compute_learned_thresholds → optimize_global_weights
in order so each step has fresh data from the previous one.
"""

import structlog
from celery import chain

from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(name="cg.nightly_learning_chain", ignore_result=True)
def nightly_learning_chain() -> None:
    from app.worker.tasks.sync_outcomes_to_calibration import sync_outcomes_to_calibration
    from app.worker.tasks.compute_learned_thresholds import compute_learned_thresholds
    from app.worker.tasks.optimize_global_weights import optimize_global_weights

    logger.info("nightly_learning_chain.start")
    chain(
        sync_outcomes_to_calibration.si(),
        compute_learned_thresholds.si(),
        optimize_global_weights.si(),
    ).apply_async()
    logger.info("nightly_learning_chain.dispatched")
