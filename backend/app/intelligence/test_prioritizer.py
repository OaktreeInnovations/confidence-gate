"""9B.2 Test prioritization engine.

Scores each test case by risk using execution profile data:
    risk = recency_weight * (1 - pass_rate) * (1 + is_critical)

recency_weight decays from 1.0 to 0.5 over 30 days since last run, so tests
that haven't run recently carry less certainty but are still included.

Returns test cases sorted by risk descending.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_RECENCY_HALF_LIFE_DAYS = 30   # days until recency_weight halves
_CRITICAL_MULTIPLIER = 2.0     # is_critical=True doubles the risk score
_DEFAULT_PASS_RATE = 0.5       # assumed pass rate for unprofiled tests (neutral)


def get_prioritized_tests(
    db: Database,
    org_id: str,
    project_id: str,
    n: int = 20,
) -> list[dict]:
    """Return up to n test cases ranked by risk score (highest first).

    Each item: {test_case_id, title, risk_score, pass_rate, flake_rate,
                is_critical, last_run_at, profiled, recency_weight}
    """
    from bson import ObjectId

    try:
        proj_oid = ObjectId(project_id)
        org_oid = ObjectId(org_id)
    except Exception:
        return []

    try:
        # Fetch all active test cases for the project
        tc_docs = list(db.test_cases.find(
            {
                "project_id": proj_oid,
                "org_id": org_oid,
                "status": {"$ne": "archived"},
            },
            {
                "_id": 1,
                "title": 1,
                "priority": 1,
                "tags": 1,
            },
        ))

        if not tc_docs:
            return []

        tc_ids = [str(t["_id"]) for t in tc_docs]

        # Fetch execution profiles
        profiles = {
            str(p["test_case_id"]): p
            for p in db.execution_profiles.find(
                {"test_case_id": {"$in": tc_ids}, "org_id": org_id},
            )
        }

        # Fetch most recent run per test case for recency weighting
        pipeline = [
            {"$match": {"test_case_id": {"$in": [t["_id"] for t in tc_docs]}, "org_id": org_oid}},
            {"$sort": {"created_at": -1}},
            {"$group": {"_id": "$test_case_id", "last_run_at": {"$first": "$created_at"}}},
        ]
        last_run_map: dict[str, datetime] = {
            str(r["_id"]): r["last_run_at"]
            for r in db.test_runs.aggregate(pipeline)
        }

        now = datetime.now(timezone.utc)
        results = []

        for tc in tc_docs:
            tc_id = str(tc["_id"])
            profile = profiles.get(tc_id)
            is_critical = tc.get("priority") == "critical"

            pass_rate = float(profile.get("pass_rate", _DEFAULT_PASS_RATE)) if profile else _DEFAULT_PASS_RATE
            flake_rate = float(profile.get("flake_report", {}).get("overall_flake_score", 0.0)) if profile else 0.0
            profiled = profile is not None

            last_run_at = last_run_map.get(tc_id)
            if last_run_at:
                if last_run_at.tzinfo is None:
                    last_run_at = last_run_at.replace(tzinfo=timezone.utc)
                days_since = (now - last_run_at).total_seconds() / 86400
                # Exponential decay: weight = 1.0 at 0 days, 0.5 at 30 days
                recency_weight = max(0.1, 0.5 ** (days_since / _RECENCY_HALF_LIFE_DAYS))
            else:
                recency_weight = 0.5  # never run — moderate recency

            failure_rate = 1.0 - pass_rate
            critical_factor = _CRITICAL_MULTIPLIER if is_critical else 1.0
            risk_score = round(recency_weight * failure_rate * critical_factor, 4)

            results.append({
                "test_case_id": tc_id,
                "title": tc.get("title", ""),
                "risk_score": risk_score,
                "pass_rate": round(pass_rate, 4),
                "flake_rate": round(flake_rate, 4),
                "is_critical": is_critical,
                "last_run_at": last_run_at.isoformat() if last_run_at else None,
                "profiled": profiled,
                "recency_weight": round(recency_weight, 4),
            })

        results.sort(key=lambda x: x["risk_score"], reverse=True)
        return results[:n]

    except Exception as e:
        logger.warning("test_prioritizer.error", error=str(e)[:200])
        return []
