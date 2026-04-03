"""3.2 Visual Regression Detection.

After each test run, computes a perceptual hash (dHash via Pillow) for each step
screenshot and compares it against the stored baseline for that test_case+step.
Visual changes are flagged without failing the test.

Hash algorithm: dHash (difference hash / gradient hash), 8×9 → 64-bit.
dHash detects edges and gradients, making it significantly more discriminating than
aHash for UI regression detection (fewer false positives from brightness shifts).
Hamming distance threshold: 10/64 bits (15.6%) — tolerates minor anti-aliasing,
subpixel rendering, and compression artefacts.

Storage: `visual_baselines` collection
  { test_case_id, step_number, org_id, hash, test_run_id, captured_at }
  Unique index on (test_case_id, step_number, org_id).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_HASH_WIDTH = 9   # 9 columns → 8 horizontal differences per row
_HASH_HEIGHT = 8  # 8 rows → 64 bits total
_HAMMING_THRESHOLD = 10  # max bits different before flagging change (15.6%)


def _dhash(image_bytes: bytes) -> str | None:
    """Compute a 64-bit dHash (gradient/difference hash). Returns 64-char binary string or None.

    Resize to 9×8, convert to greyscale, compare each pixel to its right neighbour.
    Result is robust to brightness shifts, contrast changes, and colour-profile swaps —
    all common false-positive sources with aHash.
    """
    try:
        from PIL import Image
        img = (
            Image.open(io.BytesIO(image_bytes))
            .convert("L")
            .resize((_HASH_WIDTH, _HASH_HEIGHT), Image.LANCZOS)
        )
        pixels = list(img.getdata())
        bits: list[str] = []
        for row in range(_HASH_HEIGHT):
            for col in range(_HASH_WIDTH - 1):
                left = pixels[row * _HASH_WIDTH + col]
                right = pixels[row * _HASH_WIDTH + col + 1]
                bits.append("1" if left > right else "0")
        return "".join(bits)
    except Exception as e:
        logger.warning("visual_regression.hash_failed", error=str(e)[:200])
        return None


def _hamming(h1: str, h2: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def _download_screenshot(s3_client, bucket: str, key: str) -> bytes | None:
    """Download a screenshot from MinIO/S3. Returns raw bytes or None."""
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except Exception as e:
        logger.warning("visual_regression.download_failed", key=key, error=str(e)[:200])
        return None


def _evidence_key_from_url(evidence_url: str) -> str | None:
    """Extract the S3 object key from a presigned or path URL."""
    if not evidence_url:
        return None
    # Presigned URL: contains ?X-Amz-Algorithm= — extract path segment
    path = evidence_url.split("?")[0]
    # Strip scheme + host if present: http://host/bucket/key → key
    parts = path.lstrip("/").split("/", 1)
    if len(parts) == 2:
        return parts[1]  # everything after bucket name
    return path.lstrip("/") or None


def process_run_visual_regression(
    db: Database,
    s3_client,
    bucket: str,
    test_run_id: str,
    test_case_id: str,
    org_id: str,
    step_results: list[dict],
) -> list[dict]:
    """Compute visual regression for all steps in a run.

    Returns list of visual change records:
      { step_number, action, change_detected, hamming_distance, baseline_exists }
    Also upserts hashes into visual_baselines for future comparison.
    """
    now = datetime.now(timezone.utc)
    visual_changes: list[dict] = []

    for step in step_results:
        step_number = step.get("step_number", 0)
        evidence_url = step.get("evidence_url", "")
        action = step.get("action", "")

        if step.get("status") not in ("passed", "failed"):
            continue
        if not evidence_url:
            continue

        key = _evidence_key_from_url(evidence_url)
        if not key:
            continue

        image_bytes = _download_screenshot(s3_client, bucket, key)
        if not image_bytes:
            continue

        new_hash = _dhash(image_bytes)
        if not new_hash:
            continue

        # Load baseline
        baseline = db.visual_baselines.find_one(
            {"test_case_id": test_case_id, "step_number": step_number, "org_id": org_id},
        )

        change_detected = False
        hamming_distance: int | None = None
        baseline_exists = baseline is not None

        if baseline and baseline.get("hash"):
            hamming_distance = _hamming(new_hash, baseline["hash"])
            change_detected = hamming_distance > _HAMMING_THRESHOLD

        # Upsert baseline — only update if step passed (don't set failed screenshot as baseline)
        if step.get("status") == "passed":
            db.visual_baselines.update_one(
                {"test_case_id": test_case_id, "step_number": step_number, "org_id": org_id},
                {"$set": {
                    "hash": new_hash,
                    "test_run_id": test_run_id,
                    "captured_at": now,
                    "evidence_url": evidence_url,
                }},
                upsert=True,
            )

        if baseline_exists:
            visual_changes.append({
                "step_number": step_number,
                "action": action[:100],
                "change_detected": change_detected,
                "hamming_distance": hamming_distance,
                "baseline_exists": baseline_exists,
            })

    changed_count = sum(1 for c in visual_changes if c["change_detected"])
    if changed_count > 0:
        logger.info(
            "visual_regression.changes_detected",
            test_run_id=test_run_id,
            test_case_id=test_case_id,
            changed_steps=changed_count,
        )

    return visual_changes
