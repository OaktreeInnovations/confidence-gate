"""9B.3 Nightly task — detect test cases whose flake rate is degrading.

A test is flagged when:
  1. Its current flake_rate > 0.3 (30%)
  2. The flake_rate has been increasing across the last 3 profile snapshots

Upserts a project_alert of type FLAKY_TEST_DEGRADING per test case.
Clears the alert when the test recovers below 0.2 flake rate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from app.clients.sync_mongo import get_db as _get_sync_db
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)

_FLAKE_HIGH_THRESHOLD = 0.30   # must exceed to trigger alert
_FLAKE_RECOVERY_THRESHOLD = 0.20  # must drop below to clear alert
_MIN_HISTORY_SNAPSHOTS = 3      # need at least 3 historical entries in profile


@celery_app.task(name="cg.detect_flaky_degradation", ignore_result=True)
def detect_flaky_degradation() -> None:
    """Scan all execution profiles and upsert/clear FLAKY_TEST_DEGRADING alerts."""
    db = _get_sync_db()
    now = datetime.now(timezone.utc)

    alerted = 0
    cleared = 0

    try:
        # Pull all profiles with flake data
        profiles = list(db.execution_profiles.find(
            {},
            {
                "_id": 0,
                "test_case_id": 1,
                "org_id": 1,
                "pass_rate": 1,
                "flake_report": 1,
                "flake_history": 1,   # list of {flake_score, recorded_at}
            },
        ))

        # Group by org_id to resolve project_id per test case
        tc_ids = [p["test_case_id"] for p in profiles if p.get("test_case_id")]
        if not tc_ids:
            return

        # Build tc_id → project_id + org_id lookup
        tc_docs = list(db.test_cases.find(
            {"_id": {"$in": tc_ids}},
            {"_id": 1, "project_id": 1, "org_id": 1, "title": 1},
        ))
        tc_map = {str(t["_id"]): t for t in tc_docs}

        for profile in profiles:
            tc_id = str(profile.get("test_case_id", ""))
            tc = tc_map.get(tc_id)
            if not tc:
                continue

            org_id = tc.get("org_id") or profile.get("org_id")
            project_id = tc.get("project_id")
            if not org_id or not project_id:
                continue

            flake_report = profile.get("flake_report") or {}
            current_flake = float(flake_report.get("overall_flake_score", 0.0))

            # Check history for increasing trend
            history = profile.get("flake_history") or []
            is_degrading = False
            if len(history) >= _MIN_HISTORY_SNAPSHOTS and current_flake > _FLAKE_HIGH_THRESHOLD:
                recent = sorted(history, key=lambda h: h.get("recorded_at", ""))[-_MIN_HISTORY_SNAPSHOTS:]
                scores = [float(h.get("flake_score", 0)) for h in recent]
                # Degrading: each snapshot is higher than the previous
                is_degrading = all(scores[i] < scores[i + 1] for i in range(len(scores) - 1))

            alert_query = {
                "project_id": project_id,
                "alert_type": "FLAKY_TEST_DEGRADING",
                "test_case_id": tc["_id"],
            }

            if is_degrading:
                db.project_alerts.update_one(
                    alert_query,
                    {"$set": {
                        "org_id": org_id,
                        "project_id": project_id,
                        "test_case_id": tc["_id"],
                        "alert_type": "FLAKY_TEST_DEGRADING",
                        "severity": "warning",
                        "title": f"Flaky test degrading: {tc.get('title', tc_id)}",
                        "detail": (
                            f"Flake rate {current_flake:.0%} and rising over last "
                            f"{_MIN_HISTORY_SNAPSHOTS} profile snapshots."
                        ),
                        "current_flake_rate": current_flake,
                        "updated_at": now,
                    }, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                )
                alerted += 1

            elif current_flake < _FLAKE_RECOVERY_THRESHOLD:
                # Test has recovered — remove alert if it exists
                result = db.project_alerts.delete_one(alert_query)
                if result.deleted_count:
                    cleared += 1

    except Exception as e:
        logger.warning("detect_flaky_degradation.error", error=str(e)[:200])
        return

    logger.info(
        "detect_flaky_degradation.done",
        alerted=alerted,
        cleared=cleared,
    )
