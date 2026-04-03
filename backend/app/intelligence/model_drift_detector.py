"""Detects when the scoring model's predictions stop correlating with production outcomes.

Runs nightly per project. For any project with ≥10 labeled outcomes in the last 90 days,
computes the Pearson correlation between confidence_score and outcome (1=passed, 0=failed).

A correlation below 0.3 signals that the score is no longer predictive — i.e., the model
is drifting. This is surfaced as a MODEL_DRIFT alert in project_alerts (severity: critical).

The alert is cleared when correlation recovers above 0.4.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)

_WINDOW_DAYS = 90
_MIN_SAMPLES = 10
_DRIFT_THRESHOLD = 0.3   # correlation below this = drifting
_RECOVER_THRESHOLD = 0.4  # correlation above this = recovered
_ALERT_TYPE = "MODEL_DRIFT"


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def detect_model_drift(db: Database, org_id: str) -> None:
    """Check all projects in org for model drift and upsert project_alerts accordingly."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)

    # Fetch completed validations with outcomes in the window
    pipeline = [
        {
            "$match": {
                "org_id": ObjectId(org_id),
                "status": "completed",
                "outcome": {"$in": ["production_passed", "production_failed", "rolled_back"]},
                "completed_at": {"$gte": cutoff},
                "confidence_score": {"$ne": None},
            }
        },
        {
            "$group": {
                "_id": "$project_id",
                "scores": {"$push": "$confidence_score"},
                "outcomes": {"$push": "$outcome"},
                "count": {"$sum": 1},
            }
        },
    ]

    try:
        project_data = list(db.release_validations.aggregate(pipeline))
    except Exception as e:
        logger.warning("model_drift.query_failed", error=str(e)[:200])
        return

    now = datetime.now(timezone.utc)

    for pd in project_data:
        project_id = pd["_id"]
        scores: list[float] = [float(s) for s in pd["scores"] if s is not None]
        raw_outcomes: list[str] = pd["outcomes"]
        count = pd["count"]

        if count < _MIN_SAMPLES:
            continue

        # Map outcome to numeric: passed=1, failed/rolled_back=0
        outcome_numeric = [
            1.0 if o == "production_passed" else 0.0
            for o in raw_outcomes
        ]

        corr = _pearson(scores, outcome_numeric)
        if corr is None:
            continue

        existing_alert = db.project_alerts.find_one({
            "project_id": project_id,
            "alert_type": _ALERT_TYPE,
        })

        if corr < _DRIFT_THRESHOLD:
            alert = {
                "org_id": ObjectId(org_id),
                "project_id": project_id,
                "alert_type": _ALERT_TYPE,
                "severity": "critical",
                "title": "Model Drift Detected",
                "detail": (
                    f"Confidence score no longer predicts outcomes "
                    f"(Pearson r={corr:.2f}, n={count}). "
                    "Scoring weights may need recalibration."
                ),
                "correlation": round(corr, 3),
                "sample_size": count,
                "updated_at": now,
            }
            db.project_alerts.update_one(
                {"project_id": project_id, "alert_type": _ALERT_TYPE},
                {"$set": alert},
                upsert=True,
            )
            logger.warning(
                "model_drift.alert_fired",
                project_id=str(project_id),
                correlation=round(corr, 3),
                sample_size=count,
            )

        elif corr >= _RECOVER_THRESHOLD and existing_alert:
            # Model has recovered — clear the alert
            db.project_alerts.delete_one({
                "project_id": project_id,
                "alert_type": _ALERT_TYPE,
            })
            logger.info(
                "model_drift.alert_cleared",
                project_id=str(project_id),
                correlation=round(corr, 3),
            )
