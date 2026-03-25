"""Final Score Engine — orchestrates all intelligence layers into a single output.

Pipeline:
  1. Base score from V1/V2 deterministic scorer
  2. Instability penalty (retries, healing, behavior failures)
  3. Coverage penalty (shallow test depth)
  4. Risk adjustment (high-risk step outcomes)
  5. AI adjustment: ±5 (no PRD) or -20 to +5 (PRD provided — gap analysis)
  6. Outcome calibration (predicted failure probability)
  7. Meta-confidence (score trustworthiness)
  8. Anomaly detection
  9. Change delta vs previous release
  10. Confidence trajectory
  11. Time decay (freshness)

All layers are non-fatal — failures return safe defaults.
"""

from __future__ import annotations

from bson import ObjectId
from pymongo.database import Database
import structlog

from app.intelligence.release_scorer import compute_release_report as _base_score
from app.intelligence.instability_engine import compute_instability
from app.intelligence.coverage_engine import compute_coverage
from app.intelligence.risk_engine import compute_risk_adjustment
from app.intelligence.outcome_calibration import compute_failure_probability
from app.intelligence.score_confidence_engine import compute_score_confidence
from app.intelligence.anomaly_engine import detect_anomalies
from app.intelligence.delta_engine import compute_delta
from app.intelligence.trajectory_engine import compute_trajectory
from app.intelligence.decay_engine import apply_time_decay

logger = structlog.get_logger(__name__)


def compute_final_score(
    db: Database,
    org_id: str,
    batch_id: str,
    project_id: str,
    openai_api_key: str = "",
    context: dict | None = None,
) -> dict:
    """Compute the full release intelligence report.

    Returns a dict that can be stored directly in release_validations.report.
    """
    context = context or {}

    # ── 1. Base deterministic score (V1/V2) ──────────────────────────────────
    report = _base_score(db, org_id, batch_id, project_id)
    base_score = report.confidence_score
    report_dict = report.to_dict()

    # Collect shared inputs for downstream engines
    org_oid = ObjectId(org_id)
    runs = list(db.test_runs.find({"batch_id": batch_id, "org_id": org_oid}))
    run_ids = [str(r["_id"]) for r in runs]
    telemetry_docs = {
        doc["test_run_id"]: doc
        for doc in db.execution_telemetry.find({"test_run_id": {"$in": run_ids}})
    }
    tc_ids = list({str(r.get("test_case_id", "")) for r in runs})
    profiles = {
        doc["test_case_id"]: doc
        for doc in db.execution_profiles.find({
            "test_case_id": {"$in": tc_ids},
            "org_id": org_id,
        })
    }

    # ── 2. Instability index ──────────────────────────────────────────────────
    instability = compute_instability(runs, telemetry_docs)
    instability_penalty = instability["instability_penalty"]

    # ── 3. Coverage ───────────────────────────────────────────────────────────
    coverage = compute_coverage(telemetry_docs)
    coverage_penalty = coverage["coverage_penalty"]

    # ── 4. Risk adjustment ────────────────────────────────────────────────────
    risk = compute_risk_adjustment(runs, telemetry_docs, profiles)
    risk_adjustment = risk["risk_adjustment"]

    # ── 5. Adjusted score (before AI) ─────────────────────────────────────────
    adjusted = base_score - instability_penalty - coverage_penalty + risk_adjustment
    adjusted = max(0, min(100, adjusted))

    # ── 6. Anomaly detection ──────────────────────────────────────────────────
    anomalies = []
    try:
        anomalies = detect_anomalies(runs, telemetry_docs, profiles)
    except Exception as e:
        logger.warning("final_score.anomaly_error", error=str(e)[:200])

    # ── 7. Delta vs previous release ─────────────────────────────────────────
    delta = {"score_delta": None, "pass_rate_delta": None, "reasons": [], "has_previous": False}
    try:
        run_confidences = [
            pr.get("confidence", 0.5 if pr.get("status") == "passed" else 0.0)
            for pr in report_dict.get("per_run_results", [])
        ]
        flake_rates = [p.get("flake_rate", 0.0) for p in profiles.values()]
        delta = compute_delta(
            db, project_id,
            report_dict.get("batch_summary", {}),
            int(adjusted),
            run_confidences,
            flake_rates,
        )
    except Exception as e:
        logger.warning("final_score.delta_error", error=str(e)[:200])

    # ── 8. Trajectory ─────────────────────────────────────────────────────────
    trajectory = {"trend": report.trend, "scores": [], "labels": []}
    try:
        trajectory = compute_trajectory(db, project_id)
    except Exception as e:
        logger.warning("final_score.trajectory_error", error=str(e)[:200])

    # ── 9. Score meta-confidence ──────────────────────────────────────────────
    score_confidence_result = {"score_confidence": 0.5, "data_quality": "MEDIUM"}
    try:
        score_confidence_result = compute_score_confidence(runs, telemetry_docs, profiles)
    except Exception as e:
        logger.warning("final_score.score_confidence_error", error=str(e)[:200])

    # ── 10. AI risk analyst (bounded ±5) ──────────────────────────────────────
    ai_result = {"ai_adjustment": 0, "ai_confidence": 0.0, "ai_insights": [], "risk_explanations": []}
    if openai_api_key:
        try:
            from app.intelligence.ai_risk_analyst import analyze_with_ai
            enriched = {
                **report_dict,
                "instability_index": instability["instability_index"],
                "coverage_score": coverage["coverage_score"],
                "anomalies": anomalies,
                "delta": delta,
                "trend": trajectory["trend"],
            }
            ai_result = analyze_with_ai(enriched, context, openai_api_key)
        except Exception as e:
            logger.warning("final_score.ai_error", error=str(e)[:200])

    ai_adjustment = ai_result.get("ai_adjustment", 0)
    # ai_adjustment can be -20 to +5 when PRD is provided; clamp final to 0-100
    final_score = max(0, min(100, int(adjusted) + int(ai_adjustment)))

    # Update grade and decision with final score
    from app.intelligence.release_scorer import _score_to_grade, _score_to_recommendation
    final_grade = _score_to_grade(final_score)
    final_decision, final_reasons = _score_to_recommendation(final_score, report.blockers)

    # ── 11. Outcome calibration ───────────────────────────────────────────────
    calibration = {"predicted_failure_probability": 0.15, "calibration_confidence": 0.1, "calibration_source": "default"}
    try:
        calibration = compute_failure_probability(final_score, db)
    except Exception as e:
        logger.warning("final_score.calibration_error", error=str(e)[:200])

    # ── 12. Time decay ────────────────────────────────────────────────────────
    last_run_ts = None
    if runs:
        last_run_ts = max((r.get("created_at") for r in runs if r.get("created_at")), default=None)
    decay = apply_time_decay(final_score, last_run_ts)

    # ── Assemble final output ─────────────────────────────────────────────────
    score_version = "v3" if ai_adjustment != 0 else report.score_version

    output = {
        # Core
        "confidence_score": final_score,
        "confidence_grade": final_grade,
        "recommendation": final_decision,
        "recommendation_reasons": final_reasons,

        # Calibration
        "predicted_failure_probability": calibration["predicted_failure_probability"],
        "calibration_confidence": calibration["calibration_confidence"],

        # Meta-confidence
        "score_confidence": score_confidence_result["score_confidence"],
        "data_quality": score_confidence_result["data_quality"],

        # Coverage
        "coverage_score": coverage["coverage_score"],
        "coverage_penalty": round(coverage_penalty, 1),

        # Instability
        "instability_index": instability["instability_index"],
        "instability_penalty": round(instability_penalty, 1),
        "instability_components": instability["components"],

        # Risk
        "risk_adjustment": round(risk_adjustment, 1),
        "high_risk_steps": risk.get("high_risk_steps", []),

        # Score breakdown
        "base_score": base_score,
        "adjusted_score": int(adjusted),
        "ai_adjustment": ai_adjustment,
        "ai_confidence": ai_result.get("ai_confidence", 0.0),
        "score_version": score_version,

        # Healing
        "healing_penalty": report_dict.get("healing_penalty", 0.0),

        # Time decay
        "freshness": decay["freshness"],
        "decayed_score": decay["decayed_score"],
        "hours_since_run": round(decay["hours_since_run"], 1),

        # Historical
        "historical_confidence": report.historical_confidence,
        "trend": trajectory["trend"],
        "risk_delta": report.risk_delta,
        "trajectory": {"scores": trajectory["scores"], "labels": trajectory["labels"]},

        # Anomalies
        "anomalies": anomalies,

        # Delta
        "delta": delta,

        # AI
        "ai_insights": ai_result.get("ai_insights", []),
        "risk_explanations": ai_result.get("risk_explanations", []),

        # Existing signal data
        "signals": report_dict.get("signals", []),
        "root_causes": report_dict.get("root_causes", []),
        "blockers": report_dict.get("blockers", []),
        "batch_summary": report_dict.get("batch_summary", {}),
        "per_run_results": report_dict.get("per_run_results", []),
        "feature_risks": report_dict.get("feature_risks", []),
        "flaky_tests": report_dict.get("flaky_tests", []),
        "timing_anomalies": report_dict.get("timing_anomalies", []),
    }

    logger.info(
        "final_score.computed",
        org_id=org_id,
        batch_id=batch_id,
        base_score=base_score,
        instability_penalty=round(instability_penalty, 1),
        coverage_penalty=round(coverage_penalty, 1),
        risk_adjustment=round(risk_adjustment, 1),
        ai_adjustment=ai_adjustment,
        final_score=final_score,
        grade=final_grade,
        decision=final_decision,
        score_version=score_version,
        data_quality=score_confidence_result["data_quality"],
        coverage_score=coverage["coverage_score"],
        instability_index=instability["instability_index"],
        anomalies=len(anomalies),
        freshness=decay["freshness"],
        trend=trajectory["trend"],
    )

    return output
