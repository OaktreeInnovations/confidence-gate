"""3.4 Nightly benchmark aggregation task."""

from datetime import datetime, timezone

import structlog

from app.worker.celery_app import celery_app
from app.clients import sync_mongo

logger = structlog.get_logger(__name__)


@celery_app.task(name="cg.compute_benchmarks", ignore_result=True)
def compute_benchmarks() -> None:
    """Compute per-org benchmark stats and store in benchmark_stats collection."""
    from app.intelligence.benchmark_engine import compute_org_benchmark, compute_confidence_percentiles

    db = sync_mongo.db

    # Get all distinct org_ids from execution_profiles
    org_ids = db.execution_profiles.distinct("org_id")
    if not org_ids:
        return

    now = datetime.now(timezone.utc)
    computed = 0
    for org_id in org_ids:
        try:
            result = compute_org_benchmark(db, str(org_id))
            result["computed_at"] = now
            db.benchmark_stats.update_one(
                {"org_id": str(org_id)},
                {"$set": result},
                upsert=True,
            )
            computed += 1
        except Exception as e:
            logger.warning("compute_benchmarks.org_failed", org_id=str(org_id), error=str(e)[:200])

    # 7C.3 Compute cross-org confidence percentiles and upsert
    try:
        percentiles = compute_confidence_percentiles(db)
        for org_id_str, pct in percentiles.items():
            db.benchmark_stats.update_one(
                {"org_id": org_id_str},
                {"$set": {"confidence_percentile": pct, "percentile_computed_at": now}},
                upsert=True,
            )
        logger.info("compute_benchmarks.percentiles_done", orgs=len(percentiles))
    except Exception as e:
        logger.warning("compute_benchmarks.percentiles_failed", error=str(e)[:200])

    logger.info("compute_benchmarks.done", orgs_computed=computed)
