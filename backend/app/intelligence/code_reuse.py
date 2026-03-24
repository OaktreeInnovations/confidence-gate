"""Code Reuse Guard + Stability Scoring.

Maintains a per-step code cache that stores the last successful Playwright code
for each step of a test case. Steps that demonstrate stability (consecutive
first-try successes, low timing variance, no selector fallbacks) can bypass
AI code generation entirely, saving cost and eliminating non-determinism.

Storage: `step_code_cache` collection in MongoDB.
One document per (test_case_id, step_number, org_id).

Regeneration decision:
    1. Cache exists AND stability_score >= threshold → REUSE
    2. Reused code fails on first attempt → REGENERATE (DOM drift)
    3. No cache or unstable → normal AI generation

Stability score (0.0 - 1.0) computed from:
    - consecutive_first_try_successes (weight: 0.5)
    - timing_consistency (1 - coefficient_of_variation, weight: 0.2)
    - selector_stability (no fallbacks triggered, weight: 0.2)
    - no_retry_needed (weight: 0.1)
"""

from __future__ import annotations

import hashlib
import statistics
from datetime import datetime, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

# Minimum consecutive first-try successes before code is eligible for reuse
_MIN_CONSECUTIVE_SUCCESSES = 1

# Default stability threshold for code reuse
_DEFAULT_REUSE_THRESHOLD = 0.5

# Maximum number of timing samples to keep for variance calculation
_MAX_TIMING_SAMPLES = 20


def code_hash(code: str) -> str:
    """Compute a short hash of generated code for comparison."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def action_hash(action_text: str) -> str:
    """SHA256 of step action text for cache versioning."""
    return hashlib.sha256(action_text.strip().lower().encode("utf-8")).hexdigest()[:16]


def get_cached_code(
    db: Database,
    test_case_id: str,
    org_id: str,
    step_number: int,
    reuse_threshold: float = _DEFAULT_REUSE_THRESHOLD,
    code_type: str = "python",
    action_text: str = "",
) -> dict | None:
    """Check if reusable code exists for a step.

    Returns the cache document if:
        - Cache exists for this step with matching code_type
        - stability_score >= reuse_threshold
        - consecutive_first_try_successes >= _MIN_CONSECUTIVE_SUCCESSES

    Args:
        code_type: "python" for legacy AI-generated code,
                   "intent" for structured JSON intents.
        action_text: Step action text. When provided, cache is only matched
                     if action_hash matches (auto-invalidates on step edit).

    Returns None if code should be regenerated.
    """
    query: dict = {
        "test_case_id": test_case_id,
        "step_number": step_number,
        "org_id": org_id,
    }
    # Match docs by code_type; legacy docs without code_type are treated as "python"
    if code_type == "python":
        query["$or"] = [{"code_type": "python"}, {"code_type": {"$exists": False}}]
    else:
        query["code_type"] = code_type
    # Filter by action hash when provided — invalidates cache on step text changes
    if action_text:
        query["action_hash"] = action_hash(action_text)
    doc = db.step_code_cache.find_one(query)

    if not doc:
        return None

    score = doc.get("stability_score", 0.0)
    consecutive = doc.get("consecutive_first_try_successes", 0)
    code = doc.get("last_successful_code", "")

    if not code:
        return None

    if consecutive < _MIN_CONSECUTIVE_SUCCESSES:
        return None

    if score < reuse_threshold:
        return None

    logger.info(
        "code_reuse.cache_hit",
        test_case_id=test_case_id,
        step=step_number,
        stability_score=round(score, 3),
        consecutive_successes=consecutive,
    )

    return doc


def update_code_cache(
    db: Database,
    test_case_id: str,
    org_id: str,
    step_number: int,
    action: str,
    code: str,
    status: str,
    first_try_success: bool,
    execution_ms: int,
    used_selector_fallback: bool,
    needed_retry: bool,
    code_type: str = "python",
) -> None:
    """Update the step code cache after a step execution.

    Called by the aggregation pipeline after each test run.

    Args:
        db: sync pymongo Database
        test_case_id: test case ObjectId string
        org_id: organization ID
        step_number: step number within test case
        action: step action description
        code: the Playwright code (or intent JSON) that was executed
        status: step result status ("passed", "failed", "error")
        first_try_success: True if step passed on first attempt
        execution_ms: total execution time in ms
        used_selector_fallback: True if selector fallback was triggered
        needed_retry: True if step needed more than 1 attempt
        code_type: "python" for legacy code, "intent" for structured JSON
    """
    query = {
        "test_case_id": test_case_id,
        "step_number": step_number,
        "org_id": org_id,
        "code_type": code_type,
    }
    now = datetime.now(timezone.utc)

    existing = db.step_code_cache.find_one(query)

    if status == "passed" and code:
        # Success path: update cache
        if existing:
            consecutive = existing.get("consecutive_first_try_successes", 0)
            timing_samples = existing.get("timing_samples", [])
            total_runs = existing.get("total_runs", 0)
            total_first_try = existing.get("total_first_try_successes", 0)
            selector_fallback_count = existing.get("selector_fallback_count", 0)
            retry_count = existing.get("retry_count", 0)
        else:
            consecutive = 0
            timing_samples = []
            total_runs = 0
            total_first_try = 0
            selector_fallback_count = 0
            retry_count = 0

        total_runs += 1

        if first_try_success:
            consecutive += 1
            total_first_try += 1
        else:
            consecutive = 0  # Reset streak on any non-first-try

        if used_selector_fallback:
            selector_fallback_count += 1
        if needed_retry:
            retry_count += 1

        # Keep bounded timing samples
        timing_samples.append(execution_ms)
        if len(timing_samples) > _MAX_TIMING_SAMPLES:
            timing_samples = timing_samples[-_MAX_TIMING_SAMPLES:]

        # Compute stability score
        score = _compute_stability_score(
            consecutive_first_try=consecutive,
            total_runs=total_runs,
            total_first_try=total_first_try,
            timing_samples=timing_samples,
            selector_fallback_count=selector_fallback_count,
            retry_count=retry_count,
        )

        update_doc = {
            "$set": {
                "last_successful_code": code,
                "code_hash": code_hash(code),
                "action": action,
                "action_hash": action_hash(action),
                "consecutive_first_try_successes": consecutive,
                "total_runs": total_runs,
                "total_first_try_successes": total_first_try,
                "selector_fallback_count": selector_fallback_count,
                "retry_count": retry_count,
                "timing_samples": timing_samples,
                "stability_score": round(score, 4),
                "consecutive_failures": 0,
                "last_success_at": now,
                "last_updated": now,
                "last_execution_ms": execution_ms,
            },
            "$setOnInsert": {
                "test_case_id": test_case_id,
                "step_number": step_number,
                "org_id": org_id,
                "code_type": code_type,
                "created_at": now,
            },
        }

        db.step_code_cache.update_one(query, update_doc, upsert=True)

    elif existing:
        # Failure path — use grace period for stable caches.
        # A cache with high stability (proven over many runs) shouldn't be
        # wiped by a single failure (often a vision false-negative).
        # Only fully invalidate after 2+ consecutive failures.
        prev_score = existing.get("stability_score", 0.0)
        prev_consecutive = existing.get("consecutive_first_try_successes", 0)
        consecutive_failures = existing.get("consecutive_failures", 0) + 1

        if prev_score >= 0.7 and prev_consecutive >= 2 and consecutive_failures <= 1:
            # Grace: keep the code but degrade score, don't wipe
            degraded_score = round(prev_score * 0.6, 4)
            db.step_code_cache.update_one(
                query,
                {
                    "$set": {
                        "consecutive_first_try_successes": 0,
                        "consecutive_failures": consecutive_failures,
                        "total_runs": existing.get("total_runs", 0) + 1,
                        "last_failure_status": status,
                        "last_updated": now,
                        "stability_score": degraded_score,
                    },
                },
            )
            logger.info(
                "code_reuse.cache_degraded",
                test_case_id=test_case_id,
                step=step_number,
                failure_status=status,
                degraded_score=degraded_score,
                consecutive_failures=consecutive_failures,
            )
        else:
            # Hard invalidate: unstable cache or repeated failures
            db.step_code_cache.update_one(
                query,
                {
                    "$set": {
                        "consecutive_first_try_successes": 0,
                        "consecutive_failures": consecutive_failures,
                        "total_runs": existing.get("total_runs", 0) + 1,
                        "last_failure_status": status,
                        "last_updated": now,
                        "stability_score": 0.0,
                    },
                },
            )
            logger.info(
                "code_reuse.cache_invalidated",
                test_case_id=test_case_id,
                step=step_number,
                failure_status=status,
                consecutive_failures=consecutive_failures,
            )


def _compute_stability_score(
    consecutive_first_try: int,
    total_runs: int,
    total_first_try: int,
    timing_samples: list[int],
    selector_fallback_count: int,
    retry_count: int,
) -> float:
    """Compute stability score (0.0 - 1.0) from execution history.

    Components:
        - consecutive_success_score (50%): consecutive first-try successes / 5
        - timing_consistency (20%): 1 - coefficient_of_variation (capped)
        - selector_stability (20%): 1 - (fallback_count / total_runs)
        - retry_stability (10%): 1 - (retry_count / total_runs)
    """
    if total_runs == 0:
        return 0.0

    # Consecutive success: 0-5 mapped to 0-1
    consecutive_score = min(consecutive_first_try / 5.0, 1.0)

    # Timing consistency: low variance = high score
    timing_score = 1.0
    if len(timing_samples) >= 3:
        mean_ms = statistics.mean(timing_samples)
        if mean_ms > 0:
            stdev_ms = statistics.stdev(timing_samples)
            cv = stdev_ms / mean_ms  # coefficient of variation
            timing_score = max(1.0 - cv, 0.0)

    # Selector stability: no fallbacks = 1.0
    selector_score = 1.0 - min(selector_fallback_count / max(total_runs, 1), 1.0)

    # Retry stability: no retries = 1.0
    retry_score = 1.0 - min(retry_count / max(total_runs, 1), 1.0)

    score = (
        0.50 * consecutive_score
        + 0.20 * timing_score
        + 0.20 * selector_score
        + 0.10 * retry_score
    )

    return min(max(score, 0.0), 1.0)


def get_all_step_cache(
    db: Database,
    test_case_id: str,
    org_id: str,
    code_type: str = "python",
) -> dict[int, dict]:
    """Load code cache for all steps of a test case.

    Returns dict keyed by step_number with cache documents.
    Only includes entries that have code stored and matching code_type.
    """
    query: dict = {
        "test_case_id": test_case_id,
        "org_id": org_id,
        "last_successful_code": {"$ne": ""},
    }
    # Filter by code_type; also match docs without code_type (legacy python)
    if code_type == "python":
        query["$or"] = [{"code_type": "python"}, {"code_type": {"$exists": False}}]
    else:
        query["code_type"] = code_type
    cursor = db.step_code_cache.find(query)

    result = {}
    for doc in cursor:
        sn = doc.get("step_number", 0)
        if sn > 0:
            result[sn] = doc

    return result
