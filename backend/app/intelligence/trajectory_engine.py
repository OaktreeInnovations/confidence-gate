"""Tracks release confidence score trajectory over time for a project."""

from __future__ import annotations

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)


def compute_trajectory(db: Database, project_id: str, limit: int = 10) -> dict:
    _default = {"trend": "stable", "scores": [], "labels": [], "slope": None}

    try:
        docs = list(
            db.release_validations.find(
                {"project_id": project_id, "status": "completed"},
                sort=[("created_at", -1)],
                limit=limit,
            )
        )

        # Reverse so scores run oldest→newest for regression x-axis
        docs = list(reversed(docs))

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
            return {"trend": "stable", "scores": scores, "labels": labels, "slope": None}

        # 8.7 Linear regression slope over all available scores.
        # Thresholds: slope > +1.5 pts/release → improving; slope < -1.5 → degrading.
        n = len(scores)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(scores) / n
        numerator = sum((xs[i] - mean_x) * (scores[i] - mean_y) for i in range(n))
        denominator = sum((xs[i] - mean_x) ** 2 for i in range(n))
        slope = round(numerator / denominator, 3) if denominator != 0 else 0.0

        if slope > 1.5:
            trend = "improving"
        elif slope < -1.5:
            trend = "degrading"
        else:
            trend = "stable"

        return {"trend": trend, "scores": scores, "labels": labels, "slope": slope}

    except Exception as e:
        logger.warning("trajectory_engine.error", error=str(e)[:200])
        return _default
