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
from app.intelligence.weight_learner import get_org_thresholds

logger = structlog.get_logger(__name__)


def _build_actionable_steps(
    gate_blocks: list[str],
    root_causes: list[dict],
    high_risk_steps: list[dict],
    per_run_results: list[dict],
    max_steps: int = 3,
) -> list[dict]:
    """Return up to max_steps prioritised fix suggestions.

    Priority order:
      1. Gate violations (most critical — block the deploy)
      2. Highest-impact root causes
      3. High-risk steps that failed
    """
    steps: list[dict] = []

    # Gate block hints: map common patterns to concrete actions
    _gate_hints = {
        "inconclusive": "Investigate the inconclusive step — add an explicit assertion or wait for the element to be stable before verifying.",
        "behavior override": "Replace heuristic-verified steps with deterministic assertions so outcomes can be confirmed programmatically.",
        "below the minimum passing threshold": "Improve test coverage or fix failing tests to push the confidence score above the block threshold.",
        "critical test": "Fix the failing critical test — it is designated to block releases regardless of the overall score.",
    }

    for block in gate_blocks[:2]:
        hint = next(
            (v for k, v in _gate_hints.items() if k in block.lower()),
            "Resolve the gate violation described above before attempting to ship.",
        )
        steps.append({"priority": "critical", "title": "Fix gate violation", "detail": block, "action": hint})
        if len(steps) >= max_steps:
            return steps

    # Root cause hints
    _rc_hints = {
        "selector": "Update fragile selectors to use stable data-testid attributes.",
        "timeout": "Increase wait times or add explicit element-ready checks for slow-loading UI.",
        "network": "Mock or stub the flaky external dependency in tests.",
        "authentication": "Ensure test credentials are valid and the auth flow is stable.",
        "flak": "Run the test in isolation to confirm whether it is environment-dependent, then add retry logic.",
    }

    for rc in sorted(root_causes, key=lambda x: -x.get("impact_score", 0))[:2]:
        if len(steps) >= max_steps:
            break
        desc = rc.get("description", "")
        hint = next(
            (v for k, v in _rc_hints.items() if k in desc.lower()),
            f"Fix the root cause affecting {len(rc.get('affected_tests', []))} test(s).",
        )
        steps.append({
            "priority": "high",
            "title": desc[:100],
            "detail": f"Affects {len(rc.get('affected_tests', []))} test(s); impact score {rc.get('impact_score', 0):.0%}",
            "action": hint,
        })

    # High-risk steps
    for step in high_risk_steps[:1]:
        if len(steps) >= max_steps:
            break
        steps.append({
            "priority": "medium",
            "title": f"High-risk step: {step.get('action', 'unknown')[:80]}",
            "detail": step.get("reason", "This step has a high failure rate across runs."),
            "action": "Add a retry or self-healing assertion for this step.",
        })

    return steps[:max_steps]


def _compute_route_coverage(db: Database, project_id: str, runs: list[dict]) -> dict:
    """Aggregate routes visited in the current batch vs. historically known routes."""
    try:
        from bson import ObjectId as _OID
        batch_routes: set[str] = set()
        for run in runs:
            for r in run.get("routes_visited", []):
                if r:
                    batch_routes.add(r)

        if not batch_routes:
            return {"batch_routes": [], "known_routes": [], "coverage_pct": None}

        # Known routes = union of routes seen in the last 50 completed runs for this project
        project_oid = _OID(project_id)
        historical = list(db.test_runs.find(
            {"project_id": project_oid, "routes_visited": {"$exists": True, "$ne": []}},
            {"routes_visited": 1},
        ).sort("created_at", -1).limit(50))

        known_routes: set[str] = set()
        for h in historical:
            for r in h.get("routes_visited", []):
                if r:
                    known_routes.add(r)

        # Include current batch in known routes for the pct calculation
        known_routes |= batch_routes

        coverage_pct = None
        if known_routes:
            coverage_pct = round(len(batch_routes) / len(known_routes) * 100, 1)

        return {
            "batch_routes": sorted(batch_routes),
            "known_routes": sorted(known_routes),
            "coverage_pct": coverage_pct,
        }
    except Exception as e:
        logger.warning("final_score.route_coverage_error", error=str(e)[:200])
        return {"batch_routes": [], "known_routes": [], "coverage_pct": None}


def compute_final_score(
    db: Database,
    org_id: str,
    batch_id: str,
    project_id: str,
    openai_api_key: str = "",
    context: dict | None = None,
    min_runs_for_decision: int = 3,
) -> dict:
    """Compute the full release intelligence report.

    Returns a dict that can be stored directly in release_validations.report.
    """
    import time as _time
    context = context or {}
    _pipeline_start = _time.perf_counter()
    _stage_timings: dict[str, float] = {}

    def _mark(stage: str, t0: float) -> float:
        elapsed = round((_time.perf_counter() - t0) * 1000, 1)
        _stage_timings[stage] = elapsed
        return _time.perf_counter()

    # ── 1. Base deterministic score (V1/V2) ──────────────────────────────────
    _t = _time.perf_counter()
    report = _base_score(db, org_id, batch_id, project_id)
    _t = _mark("base_score", _t)
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
    from app.intelligence import cache as _cache
    profiles_cache_key = f"cg:profiles:{batch_id}"
    profiles_cached = _cache.get(profiles_cache_key)
    if profiles_cached and isinstance(profiles_cached, list):
        profiles = {doc["test_case_id"]: doc for doc in profiles_cached}
    else:
        profile_docs = list(db.execution_profiles.find({
            "test_case_id": {"$in": tc_ids},
            "org_id": org_id,
        }))
        profiles = {doc["test_case_id"]: doc for doc in profile_docs}
        # Serialize for cache — ObjectId keys become strings via default=str
        _cache.set(profiles_cache_key, profile_docs, ttl=120)
    _t = _mark("db_queries", _t)

    # 8.1 Track which sub-engines fail so the report can surface degradation.
    _failed_engines: list[str] = []

    # ── 2. Instability index ──────────────────────────────────────────────────
    try:
        instability = compute_instability(runs, telemetry_docs)
    except Exception as e:
        logger.warning("final_score.instability_error", error=str(e)[:200])
        instability = {"instability_index": 0, "instability_penalty": 0.0, "components": {}, "evidence_weight": 1.0}
        _failed_engines.append("instability")
    instability_penalty = instability["instability_penalty"]
    _t = _mark("instability", _t)

    # ── 3. Coverage ───────────────────────────────────────────────────────────
    try:
        coverage = compute_coverage(telemetry_docs)
    except Exception as e:
        logger.warning("final_score.coverage_error", error=str(e)[:200])
        coverage = {"coverage_score": None, "coverage_penalty": 0.0, "url_diversity": 0, "action_diversity": 0}
        _failed_engines.append("coverage")
    coverage_penalty = coverage["coverage_penalty"]
    _t = _mark("coverage", _t)

    # ── 4. Risk adjustment ────────────────────────────────────────────────────
    try:
        risk = compute_risk_adjustment(runs, telemetry_docs, profiles)
    except Exception as e:
        logger.warning("final_score.risk_error", error=str(e)[:200])
        risk = {"risk_adjustment": 0.0, "high_risk_steps": []}
        _failed_engines.append("risk")
    risk_adjustment = risk["risk_adjustment"]
    _t = _mark("risk", _t)

    # ── 5. Adjusted score (before AI) ─────────────────────────────────────────
    adjusted = base_score - instability_penalty - coverage_penalty + risk_adjustment
    adjusted = max(0, min(100, adjusted))

    # ── 6. Anomaly detection ──────────────────────────────────────────────────
    anomalies = []
    try:
        anomalies = detect_anomalies(runs, telemetry_docs, profiles)
    except Exception as e:
        logger.warning("final_score.anomaly_error", error=str(e)[:200])
        _failed_engines.append("anomaly")
    _t = _mark("anomaly", _t)

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
        _failed_engines.append("delta")
    _t = _mark("delta", _t)

    # ── 8. Trajectory ─────────────────────────────────────────────────────────
    trajectory = {"trend": report.trend, "scores": [], "labels": []}
    try:
        trajectory = compute_trajectory(db, project_id)
    except Exception as e:
        logger.warning("final_score.trajectory_error", error=str(e)[:200])
        _failed_engines.append("trajectory")
    _t = _mark("trajectory", _t)

    # ── 9. Score meta-confidence ──────────────────────────────────────────────
    score_confidence_result = {"score_confidence": 0.5, "data_quality": "MEDIUM"}
    try:
        score_confidence_result = compute_score_confidence(runs, telemetry_docs, profiles)
    except Exception as e:
        logger.warning("final_score.score_confidence_error", error=str(e)[:200])
        _failed_engines.append("score_confidence")
    _t = _mark("score_confidence", _t)

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
    _t = _mark("ai_analyst", _t)

    ai_adjustment = ai_result.get("ai_adjustment", 0)
    # ai_adjustment can be -20 to +5 when PRD is provided; clamp final to 0-100
    final_score = max(0, min(100, int(adjusted) + int(ai_adjustment)))

    # Load per-org learned thresholds (falls back to defaults when insufficient data)
    try:
        block_threshold, caution_threshold = get_org_thresholds(db, org_id)
    except Exception as e:
        logger.warning("final_score.threshold_load_error", error=str(e)[:200])
        block_threshold, caution_threshold = 60, 85

    # Update grade and decision with final score
    from app.intelligence.release_scorer import _score_to_grade, _score_to_recommendation
    final_grade = _score_to_grade(final_score)
    final_decision, final_reasons = _score_to_recommendation(
        final_score, report.blockers,
        deploy_threshold=caution_threshold,
        caution_threshold=block_threshold,
    )

    # ── 11. Outcome calibration ───────────────────────────────────────────────
    calibration = {"predicted_failure_probability": 0.15, "calibration_confidence": 0.1, "calibration_source": "default"}
    try:
        calibration = compute_failure_probability(final_score, db)
    except Exception as e:
        logger.warning("final_score.calibration_error", error=str(e)[:200])
    _t = _mark("calibration", _t)

    # ── 12. Time decay ────────────────────────────────────────────────────────
    last_run_ts = None
    if runs:
        last_run_ts = max((r.get("created_at") for r in runs if r.get("created_at")), default=None)
    decay = apply_time_decay(final_score, last_run_ts)
    _mark("decay", _t)

    # ── 8.3 Per-project configurable gate thresholds ─────────────────────────
    _proj_doc = db.projects.find_one(
        {"_id": ObjectId(project_id)},
        {"inconclusive_gate_pct": 1, "behavior_override_pct": 1},
    ) or {}
    _inconclusive_gate = float(_proj_doc.get("inconclusive_gate_pct") or 0.15)
    _behavior_override_gate = float(_proj_doc.get("behavior_override_pct") or 0.20)

    # ── 1.3 Critical / Informational test designation ─────────────────────────
    # Look up is_critical, is_informational, and steps for all test cases in batch
    tc_ids_for_flags = list({str(r.get("test_case_id", "")) for r in runs if r.get("test_case_id")})
    tc_flags: dict[str, dict] = {}
    if tc_ids_for_flags:
        from bson import ObjectId as _ObjId
        for tc_doc in db.test_cases.find(
            {"_id": {"$in": [_ObjId(tid) for tid in tc_ids_for_flags if tid]}},
            {"_id": 1, "title": 1, "is_critical": 1, "is_informational": 1, "steps": 1},
        ):
            tc_flags[str(tc_doc["_id"])] = tc_doc

    # ── 2.1 Minimum assertion gate ────────────────────────────────────────────
    # A test step is "asserting" when its `expected` field is non-empty.
    # Test cases with zero asserting steps pass execution checks but prove nothing.
    no_assertion_titles: list[str] = []
    for tc_id, tc_doc in tc_flags.items():
        if tc_doc.get("is_informational"):
            continue  # informational tests are excluded from scoring anyway
        steps = tc_doc.get("steps", [])
        assertion_count = sum(
            1 for s in steps if str(s.get("expected", "")).strip()
        )
        if steps and assertion_count == 0:
            no_assertion_titles.append(tc_doc.get("title", tc_id))

    # ── Hard gate rules ───────────────────────────────────────────────────────
    # These rules override the numeric score when specific quality failures
    # are detected. They exist to prevent a high numeric score from masking
    # fundamental correctness problems. Rules run after all adjustments so
    # they reflect the final state of the run.
    gate_blocks: list[str] = []
    gate_warnings: list[str] = []

    # Count inconclusive steps across all runs in this batch
    all_step_results = [
        step
        for run in runs
        for step in run.get("results", [])
    ]
    inconclusive_count = sum(1 for s in all_step_results if s.get("status") == "inconclusive")
    total_passed_steps = sum(1 for s in all_step_results if s.get("status") == "passed")
    total_steps = len(all_step_results)

    # Gate 1: Inconclusive ratio > 15% blocks release.
    # A single bad selector in a large suite should not block the entire release;
    # only a systemic verification failure (>15% of steps) is treated as blocking.
    inconclusive_ratio = inconclusive_count / total_steps if total_steps > 0 else 0.0
    if inconclusive_count > 0 and inconclusive_ratio > _inconclusive_gate:
        gate_blocks.append(
            f"{inconclusive_count}/{total_steps} steps ({inconclusive_ratio:.0%}) were inconclusive — "
            "action executed but outcome could not be verified"
        )
        final_score = min(final_score, 49)  # force below any passing threshold
    elif inconclusive_count > 0:
        gate_warnings.append(
            f"{inconclusive_count} inconclusive step(s) ({inconclusive_ratio:.0%} of {total_steps}) — "
            f"outcome unverifiable for these steps; below blocking threshold ({_inconclusive_gate:.0%})"
        )

    # Gate 2: High behavior_override rate — too many steps passed via heuristic only
    if total_passed_steps > 0:
        bo_steps = sum(
            1 for doc in telemetry_docs.values()
            for step in doc.get("steps", [])
            if step.get("verification_level") == "behavior_override"
               and step.get("status") == "passed"
        )
        bo_rate = bo_steps / total_passed_steps if total_passed_steps else 0.0
        if bo_rate > _behavior_override_gate:
            gate_blocks.append(
                f"Behavior override rate {bo_rate:.0%} exceeds {_behavior_override_gate:.0%} — "
                f"{bo_steps}/{total_passed_steps} passed steps verified by keyword heuristic only"
            )
            final_score = min(final_score, 49)
        elif bo_rate > 0.1:
            gate_warnings.append(
                f"Behavior override rate {bo_rate:.0%} — "
                "some steps passed via heuristic only; verify manually"
            )

    # Gate 3: Score below org block_threshold blocks release
    # block_threshold is learned from production outcomes; defaults to 60 for new orgs.
    if final_score < block_threshold and not gate_blocks:
        gate_blocks.append(
            f"Confidence score {final_score} is below the minimum passing threshold ({block_threshold})"
        )

    # Gate 4: Critical test failure — any critical test that failed blocks release
    informational_tc_ids: set[str] = set()
    for run in runs:
        tc_id = str(run.get("test_case_id", ""))
        flags = tc_flags.get(tc_id, {})
        if flags.get("is_informational"):
            informational_tc_ids.add(tc_id)
        elif flags.get("is_critical"):
            run_status = run.get("status", "")
            if run_status in ("failed", "error"):
                title = flags.get("title", tc_id)
                gate_blocks.append(
                    f"Critical test '{title}' failed — release blocked regardless of score"
                )

    # Surface informational test failures as warnings (not blocks)
    for run in runs:
        tc_id = str(run.get("test_case_id", ""))
        if tc_id in informational_tc_ids and run.get("status") in ("failed", "error"):
            flags = tc_flags.get(tc_id, {})
            title = flags.get("title", tc_id)
            gate_warnings.append(
                f"Informational test '{title}' failed — excluded from score, review manually"
            )

    # Gate 5: Visual regression — aggregate changed steps across all runs
    # >3 visual changes → gate warning; any critical test with a visual change → block
    visual_changed_steps: list[dict] = []
    for run in runs:
        tc_id = str(run.get("test_case_id", ""))
        for vc in run.get("visual_changes", []):
            if vc.get("change_detected"):
                visual_changed_steps.append({
                    "tc_id": tc_id,
                    "step_number": vc.get("step_number"),
                    "action": vc.get("action", ""),
                    "is_critical": bool(tc_flags.get(tc_id, {}).get("is_critical")),
                    "test_title": tc_flags.get(tc_id, {}).get("title", tc_id),
                })

    critical_visual = [s for s in visual_changed_steps if s["is_critical"]]
    if critical_visual:
        for s in critical_visual[:3]:
            gate_blocks.append(
                f"Visual regression detected in critical test '{s['test_title']}' "
                f"(step {s['step_number']}): {s['action'][:80]}"
            )
    elif len(visual_changed_steps) > 3:
        gate_warnings.append(
            f"{len(visual_changed_steps)} visual changes detected across {len(runs)} run(s) — "
            "review screenshots before shipping"
        )
    elif visual_changed_steps:
        gate_warnings.append(
            f"{len(visual_changed_steps)} visual change(s) detected — review screenshots"
        )

    # If any hard gate fired, force recommendation to "block"
    if gate_blocks:
        final_decision = "block"
        final_reasons = gate_blocks + final_reasons
        final_grade = _score_to_grade(final_score)

    # Gate warning: test cases with no assertions
    for title in no_assertion_titles[:3]:
        gate_warnings.append(
            f"Test '{title}' has no assertions — steps may pass without verifying behavior"
        )

    if gate_warnings:
        final_reasons = final_reasons + gate_warnings

    # ── 1.1 Minimum Data Gate ─────────────────────────────────────────────────
    # Only block the decision when there are literally zero completed runs —
    # that is the only case where we have nothing to score.
    # Low data quality (limited telemetry / small test suite) is surfaced as a
    # warning reason but does NOT override the decision; the user already sees
    # the score_confidence and data_quality fields in the report.
    insufficient_data = False
    data_quality = score_confidence_result.get("data_quality", "MEDIUM")
    if len(runs) == 0:
        insufficient_data = True
        final_decision = "insufficient_data"
        final_reasons = [
            "No test runs completed — cannot produce a reliable decision."
        ] + final_reasons
    elif data_quality == "LOW":
        # Warn but still produce a real decision.
        sc = score_confidence_result.get("score_confidence", 0.0)
        final_reasons = [
            f"Limited telemetry data (confidence: {sc:.0%}, quality: {data_quality}) — "
            f"score is based on {len(runs)} run(s). Treat result as provisional."
        ] + final_reasons

    _total_ms = round((_time.perf_counter() - _pipeline_start) * 1000, 1)
    logger.info(
        "final_score.pipeline_timing",
        total_ms=_total_ms,
        stages=_stage_timings,
        batch_id=batch_id,
        run_count=len(runs),
    )

    logger.info(
        "final_score.gate_check",
        gate_blocks=len(gate_blocks),
        gate_warnings=len(gate_warnings),
        inconclusive_count=inconclusive_count,
        inconclusive_ratio=round(inconclusive_ratio, 3),
        final_score_after_gate=final_score,
        final_decision=final_decision,
    )

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
        "ai_adjustment_detail": ai_result.get("ai_adjustment_detail"),
        "requirement_coverage": ai_result.get("requirement_coverage", []),

        # Existing signal data
        "signals": report_dict.get("signals", []),
        "root_causes": report_dict.get("root_causes", []),
        "blockers": report_dict.get("blockers", []),
        "batch_summary": report_dict.get("batch_summary", {}),
        "per_run_results": report_dict.get("per_run_results", []),
        "feature_risks": report_dict.get("feature_risks", []),
        "flaky_tests": report_dict.get("flaky_tests", []),
        "timing_anomalies": report_dict.get("timing_anomalies", []),

        # Hard gate results
        "gate_blocks": gate_blocks,
        "gate_warnings": gate_warnings,
        "inconclusive_steps": inconclusive_count,

        # 1.1 Minimum data gate
        "insufficient_data": insufficient_data,

        # 6.1 Route coverage
        "route_coverage": _compute_route_coverage(db, project_id, runs),

        # 2.1 Assertion coverage
        "no_assertion_tests": no_assertion_titles,

        # 1.3 Critical/Informational designation
        "informational_test_ids": list(informational_tc_ids),

        # 2.1 Actionable failure surface — top 3 recommended fixes
        "actionable_steps": _build_actionable_steps(
            gate_blocks=gate_blocks,
            root_causes=report_dict.get("root_causes", []),
            high_risk_steps=risk.get("high_risk_steps", []),
            per_run_results=report_dict.get("per_run_results", []),
        ),

        # 8.1 Score degradation signal — set when any sub-engine failed silently
        "score_degraded": len(_failed_engines) > 0,
        "degraded_engines": _failed_engines,
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
