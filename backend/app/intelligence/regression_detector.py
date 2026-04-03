"""Detects sustained regression patterns across the last N release validations.

Runs as a post-completion step in compute_release_report. Stores alerts in the
`project_alerts` collection. The dashboard reads from this collection to surface
org-level regression signals.

Signals detected:
  1. CONSECUTIVE_DECLINE  — scores have fallen for 3+ consecutive releases
  2. HIGH_VOLATILITY      — score standard deviation across last 6 releases > 15 points
  3. SUSTAINED_LOW_GRADE  — last 4 releases all graded D or F (score < 60)

Alerts are upserted keyed on (project_id, alert_type) so the same signal does
not create duplicate documents — it updates the existing alert's metadata.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_WINDOW = 6          # how many recent validations to inspect
_DECLINE_MIN = 3     # consecutive declines needed to fire alert
_VOLATILITY_STD = 15 # score std-dev threshold for HIGH_VOLATILITY
_LOW_SCORE_THRESH = 60
_LOW_GRADE_RUN = 4   # how many consecutive low-score releases trigger SUSTAINED_LOW_GRADE


def detect_regressions(db: Database, project_id: str, org_id: str) -> list[dict]:
    """Analyse the last _WINDOW completed validations and upsert any regression alerts.

    Returns the list of alert dicts that were upserted (may be empty).
    """
    docs = list(db.release_validations.find(
        {
            "project_id": db.release_validations.database.get_collection("release_validations"),
            "org_id": org_id,
            "status": "completed",
            "confidence_score": {"$ne": None},
        },
        {"_id": 1, "confidence_score": 1, "confidence_grade": 1, "completed_at": 1},
    ).sort("completed_at", -1).limit(_WINDOW))

    # Re-query with correct project_id filter (pymongo needs ObjectId)
    from bson import ObjectId
    try:
        project_oid = ObjectId(project_id)
        org_oid = ObjectId(org_id) if len(org_id) == 24 else org_id
    except Exception:
        return []

    docs = list(db.release_validations.find(
        {
            "project_id": project_oid,
            "org_id": org_oid,
            "status": "completed",
            "confidence_score": {"$ne": None},
        },
        {"_id": 1, "confidence_score": 1, "confidence_grade": 1, "completed_at": 1},
    ).sort("completed_at", -1).limit(_WINDOW))

    if len(docs) < _DECLINE_MIN:
        return []  # not enough history

    scores = [int(d["confidence_score"]) for d in docs]  # newest first
    alerts_fired: list[dict] = []

    now = datetime.now(timezone.utc)

    # ── Signal 1: Consecutive decline ────────────────────────────────────────
    consecutive_declines = 0
    for i in range(len(scores) - 1):
        if scores[i] < scores[i + 1]:   # newer < older → declining
            consecutive_declines += 1
        else:
            break

    if consecutive_declines >= _DECLINE_MIN:
        alert = {
            "project_id": project_oid,
            "org_id": org_oid,
            "alert_type": "CONSECUTIVE_DECLINE",
            "severity": "warning",
            "title": f"Score declining for {consecutive_declines} consecutive releases",
            "detail": (
                f"Last {consecutive_declines + 1} releases: "
                + " → ".join(str(s) for s in reversed(scores[:consecutive_declines + 1]))
            ),
            "consecutive_count": consecutive_declines,
            "scores": scores[:consecutive_declines + 1],
            "updated_at": now,
        }
        db.project_alerts.update_one(
            {"project_id": project_oid, "alert_type": "CONSECUTIVE_DECLINE"},
            {"$set": alert, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        alerts_fired.append(alert)
        logger.info(
            "regression_detector.consecutive_decline",
            project_id=project_id,
            consecutive=consecutive_declines,
            scores=scores[:consecutive_declines + 1],
        )
    else:
        # Clear stale alert if pattern no longer holds
        db.project_alerts.delete_one(
            {"project_id": project_oid, "alert_type": "CONSECUTIVE_DECLINE"}
        )

    # ── Signal 2: High volatility ─────────────────────────────────────────────
    if len(scores) >= 4:
        std = statistics.stdev(scores)
        if std >= _VOLATILITY_STD:
            alert = {
                "project_id": project_oid,
                "org_id": org_oid,
                "alert_type": "HIGH_VOLATILITY",
                "severity": "warning",
                "title": f"Score volatility is high (±{std:.0f} pts)",
                "detail": (
                    f"Scores over last {len(scores)} releases vary by ±{std:.0f} points — "
                    "inconsistent test execution quality."
                ),
                "std_dev": round(std, 1),
                "scores": scores,
                "updated_at": now,
            }
            db.project_alerts.update_one(
                {"project_id": project_oid, "alert_type": "HIGH_VOLATILITY"},
                {"$set": alert, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            alerts_fired.append(alert)
        else:
            db.project_alerts.delete_one(
                {"project_id": project_oid, "alert_type": "HIGH_VOLATILITY"}
            )

    # ── Signal 3: Sustained low grade ─────────────────────────────────────────
    recent = scores[:_LOW_GRADE_RUN]
    if len(recent) >= _LOW_GRADE_RUN and all(s < _LOW_SCORE_THRESH for s in recent):
        alert = {
            "project_id": project_oid,
            "org_id": org_oid,
            "alert_type": "SUSTAINED_LOW_GRADE",
            "severity": "critical",
            "title": f"Score below {_LOW_SCORE_THRESH} for {_LOW_GRADE_RUN} consecutive releases",
            "detail": (
                f"Last {_LOW_GRADE_RUN} releases all scored below {_LOW_SCORE_THRESH}: "
                + ", ".join(str(s) for s in recent)
            ),
            "scores": recent,
            "updated_at": now,
        }
        db.project_alerts.update_one(
            {"project_id": project_oid, "alert_type": "SUSTAINED_LOW_GRADE"},
            {"$set": alert, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        alerts_fired.append(alert)
    else:
        db.project_alerts.delete_one(
            {"project_id": project_oid, "alert_type": "SUSTAINED_LOW_GRADE"}
        )

    return alerts_fired
