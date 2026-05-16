"""Celery task — deliver a webhook payload with native Celery retry.

Retry schedule (from task first attempt):
  Attempt 1: immediate
  Attempt 2: 30s delay (countdown=30)
  Attempt 3: 120s delay (countdown=120)
  Attempt 4: 480s delay (countdown=480)

On final failure, updates the validation document with
webhook_delivery_status="failed". On success, sets "delivered".

Tracks webhook_last_attempt_at on each attempt so operators can
see when the last delivery was tried.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import structlog
from bson import ObjectId

from app.clients.sync_mongo import get_db as _get_sync_db
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)

_RETRY_DELAYS = [30, 120, 480]  # seconds between retries


@celery_app.task(
    name="cg.send_webhook",
    bind=True,
    max_retries=3,
    soft_time_limit=30,
    time_limit=40,
)
def send_webhook_task(self, validation_id: str, url: str, payload: dict) -> dict:
    """POST payload to webhook URL. Retries with exponential backoff on failure."""
    db = _get_sync_db()
    now = datetime.now(timezone.utc)

    # Record attempt timestamp
    try:
        db.release_validations.update_one(
            {"_id": ObjectId(validation_id)},
            {"$set": {
                "webhook_delivery_status": "pending",
                "webhook_last_attempt_at": now,
            }},
        )
    except Exception:
        pass

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ConfidenceGate/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            logger.info("send_webhook.delivered", url=url, validation_id=validation_id)
            db.release_validations.update_one(
                {"_id": ObjectId(validation_id)},
                {"$set": {
                    "webhook_delivery_status": "delivered",
                    "webhook_last_attempt_at": datetime.now(timezone.utc),
                }},
            )
            return {"status": "delivered"}

    except Exception as exc:
        attempt = self.request.retries
        logger.warning(
            "send_webhook.failed",
            url=url,
            validation_id=validation_id,
            attempt=attempt + 1,
            error=str(exc)[:200],
        )

        if attempt < len(_RETRY_DELAYS):
            raise self.retry(exc=exc, countdown=_RETRY_DELAYS[attempt])

        # Final failure — mark as failed
        try:
            db.release_validations.update_one(
                {"_id": ObjectId(validation_id)},
                {"$set": {
                    "webhook_delivery_status": "failed",
                    "webhook_last_attempt_at": datetime.now(timezone.utc),
                }},
            )
        except Exception:
            pass

        logger.error(
            "send_webhook.exhausted",
            url=url,
            validation_id=validation_id,
            error=str(exc)[:200],
        )
        return {"status": "failed", "error": str(exc)[:200]}
