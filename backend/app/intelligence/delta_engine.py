"""Computes score and quality deltas between the current release and the previous one."""

from __future__ import annotations

from pymongo.database import Database
import structlog

logger = structlog.get_logger(__name__)


def compute_delta(
    db: Database,
    project_id: str,
    current_batch_summary: dict,
    current_score: int,
    current_run_confidences: list[float],
    current_flake_rates: list[float],
) -> dict:
    _no_prev = {
        "score_delta": None,
        "pass_rate_delta": None,
        "reasons": [],
        "has_previous": False,
    }

    try:
        prev_doc = db.release_validations.find_one(
            {
                "project_id": project_id,
                "status": "completed",
            },
            sort=[("created_at", -1)],
        )

        if not prev_doc:
            return _no_prev

        prev_report = prev_doc.get("report", {})
        if not prev_report:
            return _no_prev

        reasons: list[str] = []

        # Score delta
        prev_score = prev_report.get("confidence_score")
        score_delta: int | None = None
        if prev_score is not None:
            score_delta = current_score - int(prev_score)
            if score_delta > 0:
                reasons.append(f"Confidence score improved by {score_delta} points")
            elif score_delta < 0:
                reasons.append(f"Confidence score dropped by {abs(score_delta)} points")

        # Pass rate delta
        prev_batch = prev_report.get("batch_summary", {})
        prev_pass_rate = prev_batch.get("pass_rate")
        current_pass_rate = current_batch_summary.get("pass_rate", 0.0)
        pass_rate_delta: float | None = None

        if prev_pass_rate is not None:
            pass_rate_delta = round(current_pass_rate - float(prev_pass_rate), 4)
            pct_change = round(pass_rate_delta * 100, 1)
            if abs(pct_change) >= 5:
                if pct_change > 0:
                    reasons.append(f"Pass rate improved by {pct_change}%")
                else:
                    reasons.append(f"Pass rate dropped by {abs(pct_change)}%")

        # Avg confidence delta
        prev_per_run = prev_report.get("per_run_results", [])
        prev_confidences = [
            r.get("confidence", 0.0) for r in prev_per_run if r.get("confidence") is not None
        ]
        if prev_confidences and current_run_confidences:
            prev_avg_conf = sum(prev_confidences) / len(prev_confidences)
            current_avg_conf = sum(current_run_confidences) / len(current_run_confidences)
            avg_conf_delta = round(current_avg_conf - prev_avg_conf, 4)
            if abs(avg_conf_delta) >= 0.05:
                if avg_conf_delta > 0:
                    reasons.append(f"Average confidence improved by {avg_conf_delta:.2f}")
                else:
                    reasons.append(f"Average confidence declined by {abs(avg_conf_delta):.2f}")

        # Flake delta
        prev_flaky = prev_report.get("flaky_tests", [])
        prev_flake_rates = [ft.get("flake_rate", 0.0) for ft in prev_flaky]
        if prev_flake_rates and current_flake_rates:
            prev_avg_flake = sum(prev_flake_rates) / len(prev_flake_rates)
            current_avg_flake = sum(current_flake_rates) / len(current_flake_rates)
            flake_delta = round(current_avg_flake - prev_avg_flake, 4)
            if abs(flake_delta) >= 0.05:
                if flake_delta > 0:
                    reasons.append(f"Average flake rate worsened by {flake_delta:.2f}")
                else:
                    reasons.append(f"Average flake rate improved by {abs(flake_delta):.2f}")

        return {
            "score_delta": score_delta,
            "pass_rate_delta": pass_rate_delta,
            "reasons": reasons,
            "has_previous": True,
        }

    except Exception as e:
        logger.warning("delta_engine.error", error=str(e)[:200])
        return _no_prev
