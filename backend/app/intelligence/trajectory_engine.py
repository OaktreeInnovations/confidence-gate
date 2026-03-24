"""Tracks release confidence score trajectory over time for a project."""

from __future__ import annotations

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)


def compute_trajectory(db: Database, project_id: str, limit: int = 10) -> dict:
    _default = {"trend": "stable", "scores": [], "labels": []}

    try:
        docs = list(
            db.release_validations.find(
                {"project_id": project_id, "status": "completed"},
                sort=[("created_at", 1)],
                limit=limit,
            )
        )

        scores: list[int] = []
        labels: list[str] = []

        for doc in docs:
            report = doc.get("report", {})
            score = report.get("confidence_score")
            created_at = doc.get("created_at")
            if score is None:
                continue
            scores.append(int(score))
            label = created_at.isoformat() if created_at else ""
            labels.append(label)

        if len(scores) < 3:
            return {"trend": "stable", "scores": scores, "labels": labels}

        last_3 = scores[-3:]
        if last_3[0] < last_3[1] < last_3[2]:
            trend = "improving"
        elif last_3[0] > last_3[1] > last_3[2]:
            trend = "degrading"
        else:
            trend = "stable"

        return {"trend": trend, "scores": scores, "labels": labels}

    except Exception as e:
        logger.warning("trajectory_engine.error", error=str(e)[:200])
        return _default
