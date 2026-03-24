"""Time-aware score decay based on recency of last test run."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger(__name__)

_LAMBDA = 0.005  # score halves after ~138 hours (~6 days)


def apply_time_decay(score: int, last_run_timestamp: datetime | None) -> dict:
    if last_run_timestamp is None:
        return {
            "decayed_score": score,
            "freshness": "HIGH",
            "hours_since_run": 0.0,
        }

    try:
        now = datetime.now(timezone.utc)
        ts = last_run_timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        hours_since_run = max(0.0, (now - ts).total_seconds() / 3600.0)

        if hours_since_run < 24.0:
            return {
                "decayed_score": score,
                "freshness": "HIGH",
                "hours_since_run": round(hours_since_run, 2),
            }
        elif hours_since_run <= 72.0:
            freshness = "MEDIUM"
        else:
            freshness = "LOW"

        decayed = score * math.exp(-_LAMBDA * hours_since_run)
        decayed_score = max(0, min(100, int(decayed)))

        return {
            "decayed_score": decayed_score,
            "freshness": freshness,
            "hours_since_run": round(hours_since_run, 2),
        }

    except Exception as e:
        logger.warning("decay_engine.error", error=str(e)[:200])
        return {
            "decayed_score": score,
            "freshness": "HIGH",
            "hours_since_run": 0.0,
        }
