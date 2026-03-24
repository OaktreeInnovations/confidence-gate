"""Calibrates predicted failure probability from historical production outcomes."""

from __future__ import annotations

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]
_DEFAULT_PROBS = [0.65, 0.45, 0.30, 0.18, 0.08, 0.03]


def _bucket_index(score: int) -> int:
    for i, (lo, hi) in enumerate(_BUCKETS):
        if lo <= score < hi:
            return i
    return len(_BUCKETS) - 1


def compute_failure_probability(score: int, db: Database) -> dict:
    try:
        outcomes = list(db.production_outcomes.find({}))
    except Exception as e:
        logger.warning("outcome_calibration.query_failed", error=str(e)[:200])
        outcomes = []

    score = max(0, min(100, score))
    idx = _bucket_index(score)

    if len(outcomes) < 5:
        real_data_fraction = len(outcomes) / 5.0
        return {
            "predicted_failure_probability": _DEFAULT_PROBS[idx],
            "calibration_confidence": 0.1 + real_data_fraction * 0.5,
            "calibration_source": "default",
        }

    # Group outcomes by bucket
    bucket_totals = [0] * len(_BUCKETS)
    bucket_failures = [0] * len(_BUCKETS)

    for outcome in outcomes:
        s = outcome.get("confidence_score", 0)
        incident = outcome.get("incident_occurred", False)
        bi = _bucket_index(int(s))
        bucket_totals[bi] += 1
        if incident:
            bucket_failures[bi] += 1

    bucket_count = bucket_totals[idx]
    if bucket_count == 0:
        # Fall back to default for empty bucket
        real_data_fraction = len(outcomes) / 50.0
        return {
            "predicted_failure_probability": _DEFAULT_PROBS[idx],
            "calibration_confidence": 0.1 + real_data_fraction * 0.5,
            "calibration_source": "default_empty_bucket",
        }

    failure_prob = bucket_failures[idx] / bucket_count
    calibration_confidence = min(1.0, len(outcomes) / 50.0)

    return {
        "predicted_failure_probability": round(failure_prob, 4),
        "calibration_confidence": round(calibration_confidence, 4),
        "calibration_source": "historical",
    }
