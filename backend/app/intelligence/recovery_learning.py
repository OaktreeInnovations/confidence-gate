"""Cross-run recovery strategy learning.

Tracks which recovery actions succeed for which failure types across test runs.
Persisted in MongoDB ``recovery_stats`` collection, one document per
(test_case_id, org_id, failure_type, action_type).

Used by ``plan_recovery()`` to reorder recovery actions so the most effective
strategy runs first, and skip strategies with historically low success rates.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

# Skip threshold: if a recovery action's success rate falls below this
# across enough observations, it can be deprioritised.
_SKIP_THRESHOLD = 0.20
_MIN_OBSERVATIONS = 5


def record_recovery_outcome(
    db: Database,
    test_case_id: str,
    org_id: str,
    failure_type: str,
    action_type: str,
    success: bool,
) -> None:
    """Record the outcome of a recovery action for cross-run learning.

    Upserts into ``recovery_stats`` collection, incrementing the relevant
    counter (success_count or failure_count).
    """
    now = datetime.now(timezone.utc)
    query = {
        "test_case_id": test_case_id,
        "org_id": org_id,
        "failure_type": failure_type,
        "action_type": action_type,
    }

    inc_field = "success_count" if success else "failure_count"

    db.recovery_stats.update_one(
        query,
        {
            "$inc": {inc_field: 1},
            "$set": {"last_updated": now},
            "$setOnInsert": {
                "test_case_id": test_case_id,
                "org_id": org_id,
                "failure_type": failure_type,
                "action_type": action_type,
                "created_at": now,
            },
        },
        upsert=True,
    )


def get_recovery_rankings(
    db: Database,
    test_case_id: str,
    org_id: str,
    failure_type: str,
) -> list[dict]:
    """Query historical recovery effectiveness for a failure type.

    Returns action types ranked by success rate (descending), with
    a ``skip`` flag if below the skip threshold with enough observations.

    Returns:
        List of dicts: ``[{"action_type": str, "success_rate": float,
        "total": int, "skip": bool}]``
    """
    cursor = db.recovery_stats.find({
        "test_case_id": test_case_id,
        "org_id": org_id,
        "failure_type": failure_type,
    })

    rankings: list[dict] = []
    for doc in cursor:
        s = doc.get("success_count", 0)
        f = doc.get("failure_count", 0)
        total = s + f
        rate = s / total if total > 0 else 0.0
        rankings.append({
            "action_type": doc["action_type"],
            "success_rate": round(rate, 3),
            "total": total,
            "skip": rate < _SKIP_THRESHOLD and total >= _MIN_OBSERVATIONS,
        })

    rankings.sort(key=lambda x: (-x["success_rate"], -x["total"]))
    return rankings


def get_org_recovery_summary(
    db: Database,
    org_id: str,
) -> dict[str, list[dict]]:
    """Get full recovery stats summary for an org, grouped by failure_type.

    Used for dashboards and debugging.
    """
    cursor = db.recovery_stats.find({"org_id": org_id})

    by_failure: dict[str, list[dict]] = {}
    for doc in cursor:
        ft = doc.get("failure_type", "unknown")
        s = doc.get("success_count", 0)
        f = doc.get("failure_count", 0)
        total = s + f
        if ft not in by_failure:
            by_failure[ft] = []
        by_failure[ft].append({
            "action_type": doc["action_type"],
            "success_rate": round(s / total, 3) if total > 0 else 0.0,
            "total": total,
            "test_case_id": doc.get("test_case_id", ""),
        })

    for entries in by_failure.values():
        entries.sort(key=lambda x: -x["success_rate"])

    return by_failure
