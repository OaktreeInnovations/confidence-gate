"""Periodic task to delete expired evidence from MinIO and MongoDB."""

from datetime import datetime, timezone

import boto3
from celery.exceptions import SoftTimeLimitExceeded
import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.config import Settings
from app.worker.celery_app import celery_app

settings = Settings()
logger = structlog.get_logger(__name__)

_BATCH_SIZE = 100


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"http{'s' if settings.minio_use_ssl else ''}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
        region_name="us-east-1",
    )


@celery_app.task(name="cg.prune_expired_evidence", soft_time_limit=300, time_limit=360)
def prune_expired_evidence() -> dict:
    """Delete expired evidence objects from MinIO and their metadata from MongoDB.

    Runs hourly via Celery Beat. Processes in batches to avoid long-running ops.
    """
    if not settings.evidence_lifecycle_enabled:
        return {"status": "skipped", "reason": "evidence_lifecycle_enabled=False"}

    db = _get_sync_db()
    s3 = _get_s3_client()
    bucket = settings.minio_default_bucket
    now = datetime.now(timezone.utc)

    deleted = 0
    errors = 0

    try:
        while True:
            expired_docs = list(
                db.evidence_metadata.find(
                    {"expires_at": {"$lte": now}},
                ).limit(_BATCH_SIZE)
            )

            if not expired_docs:
                break

            for doc in expired_docs:
                s3_key = doc.get("s3_key", "")
                doc_id = doc["_id"]

                try:
                    if s3_key:
                        s3.delete_object(Bucket=bucket, Key=s3_key)
                    db.evidence_metadata.delete_one({"_id": doc_id})
                    deleted += 1
                except Exception as e:
                    errors += 1
                    logger.warning(
                        "prune.delete_failed",
                        s3_key=s3_key,
                        error=str(e)[:200],
                    )

    except SoftTimeLimitExceeded:
        logger.warning("prune.time_limit", deleted=deleted, errors=errors)

    logger.info("prune.completed", deleted=deleted, errors=errors)
    return {"status": "completed", "deleted": deleted, "errors": errors}
