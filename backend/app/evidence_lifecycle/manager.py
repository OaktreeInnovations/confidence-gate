"""Evidence lifecycle manager.

Tracks evidence metadata in MongoDB for future retention enforcement.
Provides compression hooks for the upload pipeline.

This is the foundation layer — it records what was uploaded and when
it should expire. Actual deletion is handled by a future scheduled
job that queries for expired evidence and removes it from MinIO +
metadata collection.

MongoDB collection: evidence_metadata

Document schema:
    {
        _id: ObjectId,
        org_id: str,
        test_run_id: str,
        step_number: int,
        evidence_type: str,          # "screenshot" | "console" | "dom" | "network" | "log"
        s3_key: str,                 # full MinIO object key
        s3_bucket: str,
        content_type: str,           # "image/png", "application/json", etc.
        size_bytes: int,             # original size
        compressed_size_bytes: int,  # size after compression (or same as size_bytes)
        compression: str,            # "gzip" | "jpeg" | "none"
        created_at: datetime,
        expires_at: datetime,        # when this evidence becomes eligible for deletion
    }

Indexes:
    (org_id, test_run_id)            — lookup by run
    (expires_at, 1)                  — retention sweep query
    (org_id, created_at, -1)         — admin dashboard
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pymongo.database import Database

logger = structlog.get_logger(__name__)


class EvidenceManager:
    """Tracks evidence metadata and applies compression policy.

    Designed for synchronous use in Celery workers.
    """

    def __init__(
        self,
        db: Database,
        retention_days: int = 90,
        compression_enabled: bool = False,
    ):
        self._db = db
        self._collection = db.evidence_metadata
        self._retention_days = retention_days
        self._compression_enabled = compression_enabled

    def record_upload(
        self,
        org_id: str,
        test_run_id: str,
        step_number: int,
        evidence_type: str,
        s3_key: str,
        s3_bucket: str,
        content_type: str,
        size_bytes: int,
        compressed_size_bytes: int | None = None,
        compression: str = "none",
    ) -> None:
        """Record evidence metadata after a successful upload.

        Non-fatal: failures are logged but never block test execution.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self._retention_days)

        doc = {
            "org_id": org_id,
            "test_run_id": test_run_id,
            "step_number": step_number,
            "evidence_type": evidence_type,
            "s3_key": s3_key,
            "s3_bucket": s3_bucket,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "compressed_size_bytes": compressed_size_bytes or size_bytes,
            "compression": compression,
            "created_at": now,
            "expires_at": expires_at,
        }

        try:
            self._collection.insert_one(doc)
            logger.debug(
                "evidence.metadata_recorded",
                org_id=org_id, test_run_id=test_run_id,
                step=step_number, type=evidence_type,
                size=size_bytes, expires_at=expires_at.isoformat(),
            )
        except Exception as e:
            logger.warning(
                "evidence.metadata_record_failed",
                error=str(e)[:200],
                org_id=org_id, test_run_id=test_run_id,
            )

    def prepare_upload(
        self,
        data: bytes,
        content_type: str,
        evidence_type: str,
    ) -> tuple[bytes, str, str, int, int]:
        """Optionally compress data before upload.

        Returns:
            (data, content_type, compression_name, original_size, compressed_size)
        """
        original_size = len(data)

        if not self._compression_enabled:
            return data, content_type, "none", original_size, original_size

        if evidence_type == "screenshot" and content_type == "image/png":
            from .compression import compress_screenshot
            compressed, new_ct, was_compressed = compress_screenshot(data)
            compression = "jpeg" if was_compressed else "none"
            return compressed, new_ct, compression, original_size, len(compressed)

        if content_type in ("application/json", "text/plain", "text/html"):
            from .compression import compress_json
            compressed, encoding, was_compressed = compress_json(data)
            compression = "gzip" if was_compressed else "none"
            return compressed, content_type, compression, original_size, len(compressed)

        return data, content_type, "none", original_size, original_size


def ensure_evidence_indexes(db: Database) -> None:
    """Create indexes for the evidence_metadata collection.

    Called once during app startup alongside other index creation.
    """
    coll = db.evidence_metadata
    coll.create_index([("org_id", 1), ("test_run_id", 1)])
    coll.create_index([("expires_at", 1)])
    coll.create_index([("org_id", 1), ("created_at", -1)])
    logger.info("evidence.indexes_ensured")
